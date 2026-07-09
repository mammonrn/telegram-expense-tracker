"""OCR extraction for bank transfer slips (Thai and English).

Primary engine: Google Cloud Vision (`google-cloud-vision`).
Fallback engine: Tesseract OCR (`pytesseract`), used automatically if Vision
credentials/API are unavailable or raise an error, and for local dev
without a Vision-enabled GCP project. In practice, without Vision billing
enabled, every slip goes through Tesseract - so the Tesseract path below
is where accuracy actually matters most.

Both engines funnel into the same `parse_slip_text` regex/heuristic parser
so downstream code never has to care which engine produced the text.

Tesseract-specific accuracy improvements (no Vision, no paid APIs):

1. General preprocessing before every Tesseract call: upscale small
   images, convert to grayscale, boost contrast, and binarize with a
   *local/adaptive* threshold (each pixel compared against a blurred
   estimate of its own neighborhood, not one global cutoff) - this holds
   up far better than a single global threshold against real slip photos
   with colored backgrounds or graphic overlays behind the text.
2. Two-pass targeted amount extraction: pass 1 runs Tesseract on the
   whole (preprocessed) image and locates the "Amount"/"จำนวนเงิน" label
   via `image_to_data`'s word bounding boxes. Pass 2 crops a region
   around that label (direction guided by the detected bank's typical
   layout), preprocesses just that crop more aggressively (bigger
   upscale, denoise, tighter adaptive threshold), and re-runs Tesseract
   restricted to digits - a small, high-contrast, digit-only crop is
   dramatically easier for Tesseract than the full noisy slip.
3. The crop result is used as a confident amount hint fed into
   `parse_slip_text`; if the label can't be located or the crop doesn't
   yield a valid number, everything falls through to the existing
   full-text heuristics unchanged, and ultimately to the manual re-entry
   prompt in conversation.py - that safety net is never removed.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, time as dt_time
from decimal import Decimal
from typing import Optional

from PIL import Image, ImageChops, ImageFilter, ImageOps

from utils import THAI_MONTHS, ENGLISH_MONTHS, is_amount_valid, parse_amount, thai_year_to_gregorian

logger = logging.getLogger("expense_bot.ocr")

# Known bank names/keywords -> canonical display name. Matched case-insensitively
# against OCR text. Covers major Thai banks (Thai + English slip variants) plus
# a handful of common international ones.
BANK_KEYWORDS: list[tuple[str, str]] = [
    ("กสิกรไทย", "Kasikornbank (KBank)"),
    ("kasikorn", "Kasikornbank (KBank)"),
    ("kbank", "Kasikornbank (KBank)"),
    ("ไทยพาณิชย์", "Siam Commercial Bank (SCB)"),
    ("scb", "Siam Commercial Bank (SCB)"),
    ("กรุงเทพ", "Bangkok Bank"),
    ("bangkok bank", "Bangkok Bank"),
    ("bbl", "Bangkok Bank"),
    ("กรุงไทย", "Krungthai Bank (KTB)"),
    ("krungthai", "Krungthai Bank (KTB)"),
    ("ktb", "Krungthai Bank (KTB)"),
    ("กรุงศรี", "Krungsri (Bank of Ayudhya)"),
    ("krungsri", "Krungsri (Bank of Ayudhya)"),
    ("bay", "Krungsri (Bank of Ayudhya)"),
    ("ทหารไทยธนชาต", "TTB Bank"),
    ("ttb", "TTB Bank"),
    ("ธนชาต", "TTB Bank"),
    ("ออมสิน", "Government Savings Bank"),
    ("gsb", "Government Savings Bank"),
    ("ธ.ก.ส", "BAAC"),
    ("baac", "BAAC"),
    ("ซีไอเอ็มบี", "CIMB Thai"),
    ("cimb", "CIMB Thai"),
    ("ยูโอบี", "UOB"),
    ("uob", "UOB"),
    ("แลนด์ แอนด์ เฮ้าส์", "LH Bank"),
    ("lh bank", "LH Bank"),
    ("promptpay", "PromptPay"),
    ("truemoney", "TrueMoney Wallet"),
    ("chase", "Chase Bank"),
    ("bank of america", "Bank of America"),
    ("wells fargo", "Wells Fargo"),
    ("hsbc", "HSBC"),
    ("citibank", "Citibank"),
]

# Per-bank hint for where the amount value typically sits relative to its
# "Amount"/"จำนวนเงิน" label, tried in order until one crop yields a valid
# number. These are best-effort defaults based on common Thai mobile
# banking app layouts (large amount text usually dominates the slip just
# below a smaller label) - real slip samples should be used to tune this
# further as accuracy data comes in. Unlisted/undetected banks use
# _DEFAULT_AMOUNT_DIRECTIONS.
_BANK_AMOUNT_DIRECTION_HINTS: dict[str, list[str]] = {
    "Kasikornbank (KBank)": ["below", "right"],
    "Siam Commercial Bank (SCB)": ["below", "right"],
    "Krungthai Bank (KTB)": ["below", "right"],
    "Krungsri (Bank of Ayudhya)": ["below", "right"],
    "TTB Bank": ["below", "right"],
    "Bangkok Bank": ["right", "below"],
    "PromptPay": ["below", "right"],
}
_DEFAULT_AMOUNT_DIRECTIONS = ["below", "right"]

# Words/phrases that mark the amount label on a slip, used to locate its
# bounding box in Tesseract's word-level output (see _find_amount_label_box).
# Thai matching uses the shorter "จำนวน" root rather than the full
# "จำนวนเงิน" since Tesseract's Thai word segmentation doesn't reliably
# keep the whole phrase as one recognizable token.
_AMOUNT_LABEL_WORDS = ("amount", "total", "จำนวน")

_DATE_PATTERNS = [
    # 09/07/2026, 09-07-26, 09.07.2026
    re.compile(r"(?P<d>\d{1,2})[/.\-](?P<m>\d{1,2})[/.\-](?P<y>\d{2,4})"),
]
_THAI_DATE_PATTERN = re.compile(
    r"(?P<d>\d{1,2})\s*(?P<month>" + "|".join(map(re.escape, THAI_MONTHS.keys())) + r")\s*(?P<y>\d{2,4})"
)
_ENGLISH_TEXT_DATE_PATTERN = re.compile(
    r"(?P<d>\d{1,2})\s+(?P<month>" + "|".join(map(re.escape, ENGLISH_MONTHS.keys())) + r")\.?,?\s+(?P<y>\d{2,4})",
    re.IGNORECASE,
)
_TIME_PATTERN = re.compile(r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?")
_AMOUNT_LABEL_PATTERN = re.compile(
    r"(?:amount|จำนวนเงิน|จำนวน|total)\D{0,5}([\d,]+\.\d{2}|[\d,]+)", re.IGNORECASE
)
_STANDALONE_AMOUNT_PATTERN = re.compile(r"\b(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2})\b")
# Stricter than parse_amount's general regex: requires a proper "X.XX"
# decimal, since a Thai bank slip amount is always shown with 2 decimal
# places. Used by the amount-crop pass to reject bare-digit OCR noise
# (e.g. a stray "1") that parse_amount alone would otherwise accept.
_CROP_AMOUNT_PATTERN = re.compile(r"(\d{1,3}(?:,\d{3})*\.\d{2})")
_REF_PATTERN = re.compile(
    r"(?:ref(?:erence)?(?:\s*no\.?)?|เลขที่รายการ|รหัสอ้างอิง)\s*[:\-]?\s*([A-Za-z0-9]{6,25})",
    re.IGNORECASE,
)
_NAME_LINE_PATTERN = re.compile(r"^[A-Za-zก-๙\s.]{2,40}$")


@dataclass
class OCRResult:
    """Structured fields extracted from a slip, plus confidence metadata."""

    amount: Optional[Decimal] = None
    bank: Optional[str] = None
    slip_date: Optional[date] = None
    slip_time: Optional[dt_time] = None
    sender: Optional[str] = None
    receiver: Optional[str] = None
    reference_number: Optional[str] = None
    raw_text: str = ""
    confidence: float = 0.0
    engine: str = "unknown"
    field_confidence: dict[str, bool] = field(default_factory=dict)

    @property
    def amount_is_confident(self) -> bool:
        return self.amount is not None and self.field_confidence.get("amount", False)


def parse_slip_text(
    text: str, base_confidence: float = 0.0, amount_hint: Optional[Decimal] = None
) -> OCRResult:
    """Extract structured fields from raw OCR text of a bank slip.

    `base_confidence` is the OCR engine's own average confidence (0-1) if
    available (Vision provides per-symbol confidence; Tesseract provides
    per-word confidence). The final confidence score blends engine
    confidence with how many expected fields were successfully parsed.

    `amount_hint`, if given, is a confidently-extracted amount from a
    separate targeted pass (Tesseract's crop-based second pass around the
    amount label - see OCREngine._extract_amount_hint) and is trusted over
    the full-text heuristics below.
    """
    result = OCRResult(raw_text=text)
    found: dict[str, bool] = {}

    # --- Bank ---
    lowered = text.lower()
    for keyword, canonical in BANK_KEYWORDS:
        if keyword.lower() in lowered or keyword in text:
            result.bank = canonical
            found["bank"] = True
            break

    # --- Amount --- (prefer a labelled "Amount: 1,234.00" over bare numbers)
    #
    # A parsed amount of zero (or less) is treated the same as "nothing
    # found" - never a confidently-extracted value. A real transfer slip
    # is never for ฿0.00; a zero here means OCR misread the digits (or
    # matched a stray "0" near the amount label), not a valid amount. If
    # this were accepted as `found["amount"] = True`, it would inflate
    # the confidence score enough to skip the manual re-entry prompt and
    # silently save Amount=0 to the sheet.
    amount_match = _AMOUNT_LABEL_PATTERN.search(text)
    labeled_amount = parse_amount(amount_match.group(1)) if amount_match else None

    if is_amount_valid(amount_hint):
        result.amount = amount_hint
        found["amount"] = True
    elif is_amount_valid(labeled_amount):
        result.amount = labeled_amount
        found["amount"] = True
    else:
        candidates = _STANDALONE_AMOUNT_PATTERN.findall(text)
        if candidates:
            # Heuristic: the transfer amount is usually the largest decimal
            # figure on the slip (fees/reference numbers are smaller or
            # integer-only).
            parsed = [parse_amount(c) for c in candidates]
            parsed = [p for p in parsed if is_amount_valid(p)]
            if parsed:
                result.amount = max(parsed)
                found["amount"] = False  # unlabeled guess - lower confidence

    # --- Date ---
    thai_match = _THAI_DATE_PATTERN.search(text)
    eng_text_match = _ENGLISH_TEXT_DATE_PATTERN.search(text)
    numeric_match = None
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            numeric_match = m
            break

    if thai_match:
        day = int(thai_match.group("d"))
        month = THAI_MONTHS[thai_match.group("month")]
        year = thai_year_to_gregorian(int(thai_match.group("y")) if len(thai_match.group("y")) == 4 else 2500 + int(thai_match.group("y")))
        try:
            result.slip_date = date(year, month, day)
            found["date"] = True
        except ValueError:
            pass
    elif eng_text_match:
        day = int(eng_text_match.group("d"))
        month = ENGLISH_MONTHS[eng_text_match.group("month").lower()]
        year_raw = int(eng_text_match.group("y"))
        year = year_raw if year_raw >= 100 else 2000 + year_raw
        try:
            result.slip_date = date(year, month, day)
            found["date"] = True
        except ValueError:
            pass
    elif numeric_match:
        day = int(numeric_match.group("d"))
        month = int(numeric_match.group("m"))
        year_raw = int(numeric_match.group("y"))
        if year_raw < 100:
            year = 2000 + year_raw
        else:
            year = thai_year_to_gregorian(year_raw)
        try:
            result.slip_date = date(year, month, day)
            found["date"] = True
        except ValueError:
            # try day/month swapped (some slips use MM/DD)
            try:
                result.slip_date = date(year, day, month)
                found["date"] = True
            except ValueError:
                pass

    # --- Time ---
    time_match = _TIME_PATTERN.search(text)
    if time_match:
        try:
            result.slip_time = dt_time(
                int(time_match.group("h")),
                int(time_match.group("mi")),
                int(time_match.group("s") or 0),
            )
            found["time"] = True
        except ValueError:
            pass

    # --- Reference number ---
    ref_match = _REF_PATTERN.search(text)
    if ref_match:
        result.reference_number = ref_match.group(1)
        found["reference_number"] = True

    # --- Sender / Receiver --- best-effort: look for lines following
    # "From"/"จาก" and "To"/"ถึง"/"ไปยัง" labels.
    sender, receiver = _extract_names(text)
    if sender:
        result.sender = sender
        found["sender"] = True
    if receiver:
        result.receiver = receiver
        found["receiver"] = True

    result.field_confidence = found
    result.confidence = _score_confidence(found, base_confidence)
    return result


def _extract_names(text: str) -> tuple[Optional[str], Optional[str]]:
    sender = None
    receiver = None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        low = line.lower()
        if sender is None and re.search(r"\b(from|จาก|ผู้โอน)\b", low):
            sender = _next_name_value(line, lines, i)
        if receiver is None and re.search(r"\b(to|ถึง|ไปยัง|ผู้รับ)\b", low):
            receiver = _next_name_value(line, lines, i)
    return sender, receiver


def _next_name_value(line: str, lines: list[str], index: int) -> Optional[str]:
    # Value may be on the same line after a colon, or on the next line.
    if ":" in line:
        candidate = line.split(":", 1)[1].strip()
        if candidate:
            return candidate[:60]
    if index + 1 < len(lines):
        candidate = lines[index + 1].strip()
        if _NAME_LINE_PATTERN.match(candidate):
            return candidate[:60]
    return None


def _score_confidence(found: dict[str, bool], base_confidence: float) -> float:
    """Blend engine OCR confidence with parse completeness of key fields."""
    key_fields = ["amount", "date", "bank"]
    hits = sum(1 for f in key_fields if found.get(f))
    completeness = hits / len(key_fields)
    if base_confidence > 0:
        return round(0.6 * base_confidence + 0.4 * completeness, 3)
    return round(completeness, 3)


# --- Tesseract image preprocessing -----------------------------------------
#
# Pure Pillow, no extra dependencies (numpy/opencv). Applied to every
# Tesseract call, not just the amount crop, per the "general preprocessing"
# improvements: upscaling, grayscale, and adaptive (local, not global)
# thresholding are well-established ways to improve Tesseract's accuracy
# on real-world photos with colored or graphic backgrounds.


def _upscale_if_small(img: Image.Image, min_width: int = 1200, max_scale: float = 3.0) -> Image.Image:
    """Upscale an image if it's smaller than Tesseract likes to work with.

    Phone photos of slips are often small relative to how much detail
    Tesseract needs to resolve individual digits; LANCZOS resampling adds
    back some of that resolution. Capped at `max_scale` so a tiny image
    doesn't get blown up into unusable mush.
    """
    width, height = img.size
    if width <= 0 or width >= min_width:
        return img
    scale = min(max_scale, min_width / width)
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return img.resize(new_size, Image.LANCZOS)


def _adaptive_threshold(gray_img: Image.Image, radius: int = 15, offset: int = 10) -> Image.Image:
    """Binarize using a local/adaptive threshold instead of one global cutoff.

    Each pixel is compared against a heavily blurred version of the same
    image (a cheap local-neighborhood mean, computed via Gaussian blur -
    no numpy needed) rather than a single fixed brightness cutoff. A pixel
    noticeably darker than its own local background is treated as text
    (black); everything else becomes background (white). This is what
    lets a slip with a colored banner in one corner and a photo overlay
    in another still binarize sensibly in both regions, where a single
    global threshold would blow out one region or the other.
    """
    background = gray_img.filter(ImageFilter.GaussianBlur(radius))
    # background - gray_img: large/positive where gray_img is darker than
    # its local background (i.e. likely text), ~0 where they're similar.
    darkness = ImageChops.subtract(background, gray_img)
    return darkness.point(lambda p: 0 if p > offset else 255)


def _prepare_for_ocr(img: Image.Image) -> tuple[Image.Image, Image.Image]:
    """General preprocessing pipeline shared by every Tesseract call.

    Returns (grayscale_source, binarized) - both the same size and pixel
    alignment. `grayscale_source` (upscaled + contrast-boosted, but not
    yet binarized) is kept around as the source for the amount crop later,
    since a second threshold pass on already-binarized pixels can only
    lose information, never recover it. `binarized` is what actually gets
    OCR'd for the full-page pass.
    """
    img = _upscale_if_small(img)
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    binarized = _adaptive_threshold(gray)
    return gray, binarized


# --- Two-pass targeted amount extraction ------------------------------------


def _find_amount_label_box(ocr_data: dict) -> Optional[tuple[int, int, int, int]]:
    """Scan Tesseract's word-level output for an amount label.

    Returns its (left, top, width, height) box in the pixel coordinates
    of whichever image was passed to `image_to_data`, or None if no
    label-like word was found.
    """
    words = ocr_data.get("text") or []
    for i, word in enumerate(words):
        word = (word or "").strip()
        if not word:
            continue
        lowered = word.lower()
        if any(kw in lowered or kw in word for kw in _AMOUNT_LABEL_WORDS):
            try:
                return (
                    int(ocr_data["left"][i]),
                    int(ocr_data["top"][i]),
                    int(ocr_data["width"][i]),
                    int(ocr_data["height"][i]),
                )
            except (KeyError, IndexError, ValueError, TypeError):
                continue
    return None


def _crop_region_for_direction(
    image_size: tuple[int, int], label_box: tuple[int, int, int, int], direction: str
) -> Optional[tuple[int, int, int, int]]:
    """Compute a crop rectangle around a label, in the given direction.

    `direction` is "right" (same line, extending to the image's right
    edge - e.g. Bangkok Bank's inline "Amount  1,250.00" layout) or
    "below" (a band underneath the label - e.g. K PLUS-style slips where
    a large amount is centered under a small label). Returns None if the
    resulting box is degenerate.
    """
    img_w, img_h = image_size
    left, top, width, height = label_box
    height = max(height, 1)

    if direction == "right":
        pad_v = int(height * 0.5)
        box = (left + width, max(0, top - pad_v), img_w, min(img_h, top + height + pad_v))
    else:  # "below"
        # The amount is often shown in a much larger font some distance
        # below a small label (common on Thai mobile banking app slips,
        # where the amount is the visual focal point), so this band needs
        # real headroom - both further down and wider than the label
        # itself - not just a couple of label-heights.
        box = (
            max(0, left - width),
            top + height,
            min(img_w, left + width * 6),
            min(img_h, top + height * 9),
        )

    x0, y0, x1, y1 = box
    if x1 - x0 < 5 or y1 - y0 < 5:
        return None
    return box


def _extract_amount_from_crop(crop: Image.Image) -> Optional[Decimal]:
    """Preprocess a small amount-region crop more aggressively than the
    full-page pass, then OCR it restricted to digits only.

    Crops are inherently small and often low-contrast, so this upscales
    further, denoises, and uses a tighter adaptive threshold than the
    full-page pipeline. Tries two page-segmentation modes since a crop
    that's just barely mis-sized for "single line" (psm 7) often works
    fine as "single uniform block" (psm 6), and vice versa.

    Requires a properly decimal-formatted match (Thai bank slips always
    show amounts as "X,XXX.XX", never a bare integer) rather than
    accepting anything `parse_amount` can salvage - a crop region that
    missed the real value entirely (blank space, a stray digit from
    threshold noise) can otherwise OCR to a lone garbage digit like "1",
    which would be a false-positive "confident" amount worse than not
    guessing at all.
    """
    if crop.width < 5 or crop.height < 5:
        return None

    import pytesseract

    gray = ImageOps.grayscale(crop)
    gray = _upscale_if_small(gray, min_width=300, max_scale=4.0)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = gray.filter(ImageFilter.MedianFilter(size=3))  # slight denoise
    binarized = _adaptive_threshold(gray, radius=9, offset=8)

    for psm in ("7", "6"):
        try:
            raw = pytesseract.image_to_string(
                binarized,
                lang="eng",
                config=f"--psm {psm} -c tessedit_char_whitelist=0123456789,.",
            )
        except Exception:  # noqa: BLE001
            logger.warning("Amount crop OCR failed (psm %s)", psm, exc_info=True)
            continue
        match = _CROP_AMOUNT_PATTERN.search(raw)
        if not match:
            continue
        amount = parse_amount(match.group(1))
        if is_amount_valid(amount):
            return amount
    return None


class OCREngine:
    """Runs OCR against image/PDF bytes, preferring Google Vision.

    Falls back to Tesseract automatically on any Vision failure (missing
    credentials, API not enabled, quota, network error) so the bot keeps
    working in degraded mode instead of failing the whole upload.
    """

    def __init__(self, tesseract_cmd: str | None = None) -> None:
        self._vision_client = None
        if tesseract_cmd:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self._init_vision()

    def _init_vision(self) -> None:
        try:
            from google.cloud import vision

            self._vision_client = vision.ImageAnnotatorClient()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google Vision OCR unavailable, will use Tesseract fallback: %s", exc)
            self._vision_client = None

    def extract_text(
        self, image_bytes: bytes, is_pdf: bool = False
    ) -> tuple[str, float, str, Optional[Decimal]]:
        """Return (raw_text, engine_confidence, engine_name, amount_hint).

        `amount_hint` is only ever populated by the Tesseract path's
        targeted crop pass (see `_extract_amount_hint`); Vision doesn't
        need the workaround, so it's always None there.
        """
        if is_pdf:
            image_bytes = _pdf_first_page_to_png(image_bytes)

        if self._vision_client is not None:
            try:
                return self._extract_with_vision(image_bytes)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Vision OCR failed, falling back to Tesseract: %s", exc)

        return self._extract_with_tesseract(image_bytes)

    def _extract_with_vision(self, image_bytes: bytes) -> tuple[str, float, str, Optional[Decimal]]:
        from google.cloud import vision

        image = vision.Image(content=image_bytes)
        response = self._vision_client.text_detection(image=image)
        if response.error.message:
            raise RuntimeError(response.error.message)

        annotations = response.text_annotations
        if not annotations:
            return "", 0.0, "vision", None

        text = annotations[0].description
        # Vision's text_detection doesn't return a single confidence score;
        # approximate using per-page confidence from full_text_annotation.
        confidence = 0.0
        try:
            pages = response.full_text_annotation.pages
            if pages:
                confidence = sum(p.confidence for p in pages) / len(pages)
        except Exception:  # noqa: BLE001
            confidence = 0.0
        return text, confidence, "vision", None

    def _extract_with_tesseract(self, image_bytes: bytes) -> tuple[str, float, str, Optional[Decimal]]:
        import pytesseract
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(image_bytes)) as raw_img:
            original = raw_img.convert("RGB")

        gray_source, binarized = _prepare_for_ocr(original)

        # PSM 11 ("sparse text": find as much text as possible, no
        # paragraph/column structure assumed) instead of Tesseract's
        # default automatic layout analysis (PSM 3). Slips mix scattered
        # text labels with logos/graphics/photos in non-standard layouts,
        # and PSM 3's layout analysis can drop entire text blocks it
        # can't fit into a paragraph structure - verified against a
        # synthetic slip where a decorative graphic caused PSM 3 to miss
        # the "Amount" label and its value completely, while PSM 11
        # found both.
        data = pytesseract.image_to_data(
            binarized, lang="tha+eng", config="--psm 11", output_type=pytesseract.Output.DICT
        )
        words = [w for w in data["text"] if w.strip()]
        confidences = [
            int(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and int(c) >= 0
        ]
        text = " ".join(words)
        avg_conf = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0

        amount_hint = self._extract_amount_hint(gray_source, data, text)

        return text, avg_conf, "tesseract", amount_hint

    def _extract_amount_hint(
        self, gray_source: Image.Image, ocr_data: dict, full_text: str
    ) -> Optional[Decimal]:
        """Two-pass targeted amount extraction: locate the amount label
        from pass 1's word boxes, crop around it (direction guided by the
        detected bank), and re-OCR just that crop restricted to digits.

        Never raises - any failure here just means no hint, and the
        existing full-text heuristics in `parse_slip_text` (and, as a
        last resort, the manual re-entry prompt) take over unchanged.
        """
        try:
            label_box = _find_amount_label_box(ocr_data)
            if label_box is None:
                return None

            detected_bank = None
            lowered = full_text.lower()
            for keyword, canonical in BANK_KEYWORDS:
                if keyword.lower() in lowered or keyword in full_text:
                    detected_bank = canonical
                    break

            directions = _BANK_AMOUNT_DIRECTION_HINTS.get(detected_bank, _DEFAULT_AMOUNT_DIRECTIONS)

            for direction in directions:
                box = _crop_region_for_direction(gray_source.size, label_box, direction)
                if box is None:
                    continue
                crop = gray_source.crop(box)
                amount = _extract_amount_from_crop(crop)
                if amount is not None:
                    logger.info(
                        "Amount crop (%s, bank=%s) extracted %s", direction, detected_bank, amount
                    )
                    return amount
            return None
        except Exception:  # noqa: BLE001
            logger.warning("Amount crop extraction failed, falling back to full-text parsing", exc_info=True)
            return None


def _pdf_first_page_to_png(pdf_bytes: bytes) -> bytes:
    from pdf2image import convert_from_bytes

    pages = convert_from_bytes(pdf_bytes, first_page=1, last_page=1)
    if not pages:
        raise ValueError("PDF has no pages")
    buf = io.BytesIO()
    pages[0].save(buf, format="PNG")
    return buf.getvalue()
