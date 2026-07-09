"""Tests for the Tesseract preprocessing and two-pass amount-crop pipeline
(ocr.py). Split into:

- Pure logic tests (geometry, label search, thresholding) - no OCR engine
  needed, run everywhere.
- Real-tesseract integration tests against synthetic slip images - skipped
  automatically if the `tesseract` binary isn't installed, since CI/dev
  environments may not have it (the README documents installing
  `tesseract-ocr`/`tesseract-ocr-tha` as a system dependency).
"""

from __future__ import annotations

import io
import shutil
from decimal import Decimal

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ocr import (
    OCREngine,
    _adaptive_threshold,
    _crop_region_for_direction,
    _find_amount_label_box,
    _upscale_if_small,
)

TESSERACT_AVAILABLE = shutil.which("tesseract") is not None
requires_tesseract = pytest.mark.skipif(
    not TESSERACT_AVAILABLE, reason="tesseract binary not installed"
)


# -- pure logic: upscaling ---------------------------------------------------


def test_upscale_if_small_scales_up_to_min_width():
    img = Image.new("L", (300, 150))
    result = _upscale_if_small(img, min_width=900, max_scale=3.0)
    assert result.size == (900, 450)


def test_upscale_if_small_leaves_large_image_unchanged():
    img = Image.new("L", (2000, 1000))
    result = _upscale_if_small(img, min_width=1200)
    assert result.size == (2000, 1000)


def test_upscale_if_small_caps_at_max_scale():
    img = Image.new("L", (10, 10))
    result = _upscale_if_small(img, min_width=1000, max_scale=3.0)
    assert result.size == (30, 30)


# -- pure logic: adaptive threshold ------------------------------------------


def test_adaptive_threshold_produces_pure_black_and_white_image():
    img = Image.new("L", (100, 100))
    pixels = img.load()
    for x in range(100):
        for y in range(100):
            pixels[x, y] = (x * y) % 256  # arbitrary gradient/noise pattern

    result = _adaptive_threshold(img)

    assert result.mode == "L"
    assert set(result.tobytes()) <= {0, 255}


# -- pure logic: amount label box search -------------------------------------


def _fake_ocr_data(words: list[tuple[str, int, int, int, int]]) -> dict:
    return {
        "text": [w[0] for w in words],
        "left": [w[1] for w in words],
        "top": [w[2] for w in words],
        "width": [w[3] for w in words],
        "height": [w[4] for w in words],
    }


def test_find_amount_label_box_locates_english_label():
    data = _fake_ocr_data(
        [("Bank", 10, 10, 50, 20), ("Amount", 10, 50, 80, 25), ("1,250.00", 10, 90, 100, 30)]
    )
    box = _find_amount_label_box(data)
    assert box == (10, 50, 80, 25)


def test_find_amount_label_box_locates_thai_label():
    data = _fake_ocr_data([("จำนวนเงิน", 15, 40, 90, 22), ("500.50", 15, 70, 90, 28)])
    box = _find_amount_label_box(data)
    assert box == (15, 40, 90, 22)


def test_find_amount_label_box_returns_none_when_absent():
    data = _fake_ocr_data([("Bangkok", 10, 10, 50, 20), ("Bank", 65, 10, 40, 20)])
    assert _find_amount_label_box(data) is None


def test_find_amount_label_box_ignores_blank_words():
    data = _fake_ocr_data([("", 0, 0, 0, 0), ("  ", 0, 0, 0, 0), ("Amount", 5, 5, 60, 20)])
    assert _find_amount_label_box(data) == (5, 5, 60, 20)


# -- pure logic: crop region geometry ----------------------------------------


def test_crop_region_right_extends_to_image_edge():
    label_box = (100, 100, 80, 20)  # left, top, width, height
    box = _crop_region_for_direction((800, 600), label_box, "right")
    x0, y0, x1, y1 = box
    assert x0 == 180  # right edge of the label
    assert x1 == 800  # extends to the image's right edge
    assert y0 < 100 < y1  # vertically straddles the label


def test_crop_region_below_extends_downward_generously():
    label_box = (100, 100, 80, 20)
    box = _crop_region_for_direction((800, 600), label_box, "below")
    x0, y0, x1, y1 = box
    assert y0 == 120  # starts at the bottom of the label
    # generous enough for a large amount font positioned well below a
    # small label, per the real layout this was tuned against
    assert y1 - y0 >= 20 * 5
    assert x1 > x0 + 80


def test_crop_region_clamped_to_image_bounds():
    label_box = (700, 580, 50, 20)
    box = _crop_region_for_direction((800, 600), label_box, "right")
    assert box is not None
    x0, y0, x1, y1 = box
    assert x1 <= 800
    assert y1 <= 600


def test_crop_region_returns_none_for_degenerate_box():
    # label sitting right at the image's bottom-right corner leaves no
    # room for a "below" crop.
    label_box = (790, 595, 5, 4)
    assert _crop_region_for_direction((800, 600), label_box, "below") is None


# -- real-tesseract integration: synthetic slip images -----------------------


def _draw_base_slip(size=(900, 600)) -> Image.Image:
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font_big = ImageFont.load_default(size=40)
    font_med = ImageFont.load_default(size=28)
    draw.text((30, 20), "Bangkok Bank", fill=(20, 20, 20), font=font_med)
    draw.text((30, 140), "Amount", fill=(30, 30, 30), font=font_med)
    draw.text((30, 190), "1,250.00", fill=(0, 0, 0), font=font_big)
    draw.text((30, 420), "Date 09/07/2026 Time 14:35", fill=(20, 20, 20), font=font_med)
    return img


def _with_graphic_overlay(img: Image.Image) -> Image.Image:
    """Soft-edged colored blobs overlapping the amount text, simulating a
    decorative graphic/character illustration behind the transaction
    details on a real slip template."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.ellipse([120, 160, 420, 280], fill=(255, 120, 160, 140))
    odraw.ellipse([250, 140, 500, 260], fill=(120, 180, 255, 140))
    odraw.ellipse([60, 200, 260, 320], fill=(255, 210, 90, 120))
    overlay = overlay.filter(ImageFilter.GaussianBlur(8))
    composited = img.convert("RGBA")
    composited.alpha_composite(overlay)
    return composited.convert("RGB")


def _to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@requires_tesseract
def test_naive_tesseract_fails_on_graphic_overlay_slip():
    """Baseline: proves the overlay is genuinely hard, not a trivial case -
    a single plain OCR pass (no preprocessing) can't read the amount."""
    import pytesseract

    image_bytes = _to_png_bytes(_with_graphic_overlay(_draw_base_slip()))
    with Image.open(io.BytesIO(image_bytes)) as img:
        raw_text = pytesseract.image_to_string(img.convert("RGB"), lang="eng")

    assert "1,250.00" not in raw_text
    assert "1250.00" not in raw_text


@requires_tesseract
def test_two_pass_crop_recovers_amount_from_graphic_overlay_slip():
    """The actual regression/feature test: the new preprocessing + crop
    pipeline recovers the amount that a naive pass (previous test) can't."""
    image_bytes = _to_png_bytes(_with_graphic_overlay(_draw_base_slip()))

    engine = OCREngine()
    engine._vision_client = None  # force the Tesseract path deterministically

    text, confidence, engine_name, amount_hint = engine.extract_text(image_bytes)

    assert engine_name == "tesseract"
    assert amount_hint == Decimal("1250.00")


@requires_tesseract
def test_two_pass_crop_works_for_right_direction_layout():
    """Same-line "Amount   1,250.00" layout (e.g. Bangkok Bank's hint),
    not just the below-label layout."""
    img = Image.new("RGB", (900, 400), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font_med = ImageFont.load_default(size=28)
    draw.text((30, 20), "Bangkok Bank", fill=(20, 20, 20), font=font_med)
    draw.text((30, 100), "Amount", fill=(30, 30, 30), font=font_med)
    draw.text((220, 100), "1,250.00", fill=(0, 0, 0), font=font_med)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.ellipse([200, 60, 500, 180], fill=(255, 150, 180, 140))
    overlay = overlay.filter(ImageFilter.GaussianBlur(6))
    composited = img.convert("RGBA")
    composited.alpha_composite(overlay)
    image_bytes = _to_png_bytes(composited.convert("RGB"))

    engine = OCREngine()
    engine._vision_client = None

    _, _, _, amount_hint = engine.extract_text(image_bytes)

    assert amount_hint == Decimal("1250.00")


@requires_tesseract
def test_no_amount_hint_when_no_label_present():
    """Safety net: if there's no recognizable amount label at all, the
    crop pass must not fabricate a hint - falls through to the existing
    full-text/manual-entry path unchanged."""
    img = Image.new("RGB", (600, 300), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font_med = ImageFont.load_default(size=28)
    draw.text((30, 20), "Bangkok Bank", fill=(20, 20, 20), font=font_med)
    draw.text((30, 100), "Date 09/07/2026", fill=(20, 20, 20), font=font_med)
    image_bytes = _to_png_bytes(img)

    engine = OCREngine()
    engine._vision_client = None

    _, _, _, amount_hint = engine.extract_text(image_bytes)

    assert amount_hint is None
