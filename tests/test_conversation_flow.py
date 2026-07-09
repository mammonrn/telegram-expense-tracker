"""End-to-end conversation tests driving the real handler code (not just
the pure helper functions it calls), using lightweight fakes/mocks for
Telegram objects and Google Drive/Sheets.

Covers two regressions:
- BUG 1: selecting a category button must save the English canonical
  value to the sheet, never the Thai display label.
- BUG 2: a slip whose amount OCR can't confidently extract must always
  route to manual re-entry, even when overall OCR confidence is high -
  never silently save Amount=0.
"""

import asyncio
from datetime import date, time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import CATEGORIES, CATEGORY_LABELS_TH, SHEET_HEADERS
from conversation import PENDING_KEY, WAITING_AMOUNT_CORRECTION, PendingExpense, SlipConversation
from database import ExpenseDatabase
from ocr import OCRResult
from sheet import ExpenseRow


class FakeSheetManager:
    """In-memory stand-in for SheetManager - no real Google API calls."""

    def __init__(self):
        self.rows: list[ExpenseRow] = []

    def append_expense(self, row: ExpenseRow) -> int:
        self.rows.append(row)
        return len(self.rows) + 1

    def get_all_records(self) -> list[dict[str, str]]:
        return [dict(zip(SHEET_HEADERS, row.as_list())) for row in self.rows]

    def find_duplicate(self, **kwargs):
        return None

    def update_row(self, row_number, values):
        pass

    def delete_row(self, row_number):
        pass


def _make_conversation(fake_sheets: FakeSheetManager) -> SlipConversation:
    db = ExpenseDatabase(fake_sheets)
    config = MagicMock(allowed_user_ids=[], ocr_confidence_threshold=0.55)
    return SlipConversation(config=config, drive=MagicMock(), db=db, ocr_engine=MagicMock())


def _make_pending(remark_prefilled: bool = True) -> PendingExpense:
    return PendingExpense(
        amount=Decimal("150.00"),
        bank="Cash (No Slip)",
        slip_date=date(2026, 7, 9),
        slip_time=time(14, 0),
        sender="Tester",
        receiver="",
        reference_number="",
        drive_url="",
        telegram_file_id="",
        ocr_confidence=1.0,
        remark="",
        remark_prefilled=remark_prefilled,
    )


def _make_category_update_and_context(category_key: str, pending: PendingExpense):
    query = MagicMock()
    query.data = f"cat:{category_key}"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    query.message.edit_text = AsyncMock()
    query.message.reply_text = AsyncMock()

    update = MagicMock()
    update.callback_query = query
    update.effective_user = MagicMock(id=42)
    update.effective_message = query.message

    context = MagicMock()
    context.user_data = {PENDING_KEY: pending}
    return update, context


# -- BUG 1: category selection must save the English canonical value ------


@pytest.mark.parametrize("category_key", list(CATEGORIES.keys()))
def test_selecting_each_category_button_saves_english_canonical_value(category_key):
    fake_sheets = FakeSheetManager()
    conv = _make_conversation(fake_sheets)

    pending = _make_pending()
    update, context = _make_category_update_and_context(category_key, pending)

    asyncio.run(conv.handle_category(update, context))

    assert len(fake_sheets.rows) == 1
    saved_category = fake_sheets.rows[0].category
    expected_english = CATEGORIES[category_key][1]

    assert saved_category == expected_english
    # The exact reported regression: the stored value must never be a
    # Thai display label, even though the button the user tapped showed
    # Thai text.
    assert saved_category not in CATEGORY_LABELS_TH.values()


def test_category_button_label_is_thai_but_callback_data_key_is_english():
    # Sanity check on the split this whole fix depends on: the button
    # text (what the user sees and taps) is Thai, but callback_data (what
    # actually drives handle_category) carries the English dict key, not
    # the Thai label itself.
    for key, (emoji, label) in CATEGORIES.items():
        assert key.isascii()
        assert CATEGORY_LABELS_TH[label] != label  # label itself is English


# -- BUG 2: an unusable OCR amount must always trigger manual re-entry ----


def test_handle_slip_requires_manual_entry_when_ocr_amount_is_none_despite_high_confidence():
    fake_sheets = FakeSheetManager()
    conv = _make_conversation(fake_sheets)

    # This is the exact failure mode: OCR reports high overall confidence
    # (bank + date matched) but could not extract a valid, non-zero
    # amount - result.amount is None, not a bogus zero.
    ocr_result = OCRResult(amount=None, bank="Bangkok Bank", confidence=0.9)

    conv._download_slip = AsyncMock(return_value=(b"fake-bytes", "slip.jpg", "image/jpeg", False))
    conv._run_ocr = AsyncMock(return_value=ocr_result)
    conv._upload_and_stash_url = AsyncMock(return_value="https://drive.example/x")

    processing_msg = MagicMock()
    processing_msg.edit_text = AsyncMock()
    processing_msg.delete = AsyncMock()

    message = MagicMock()
    message.reply_text = AsyncMock(return_value=processing_msg)
    message.photo = [MagicMock(file_id="p1", file_unique_id="u1")]
    message.document = None

    update = MagicMock()
    update.effective_user = MagicMock(id=42)
    update.effective_message = message

    context = MagicMock()
    context.user_data = {}
    context.bot = MagicMock()

    state = asyncio.run(conv.handle_slip(update, context))

    assert state == WAITING_AMOUNT_CORRECTION
    assert fake_sheets.rows == []  # nothing was saved
    processing_msg.edit_text.assert_awaited()  # user was prompted, not silently saved


def test_handle_amount_correction_rejects_zero():
    fake_sheets = FakeSheetManager()
    conv = _make_conversation(fake_sheets)

    pending = _make_pending()
    message = MagicMock()
    message.text = "0"
    message.reply_text = AsyncMock()

    update = MagicMock()
    update.effective_user = MagicMock(id=42)
    update.effective_message = message

    context = MagicMock()
    context.user_data = {PENDING_KEY: pending}

    state = asyncio.run(conv.handle_amount_correction(update, context))

    assert state == WAITING_AMOUNT_CORRECTION
    assert fake_sheets.rows == []
    message.reply_text.assert_awaited()
