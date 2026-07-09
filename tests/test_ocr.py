from datetime import date, time
from decimal import Decimal

from ocr import parse_slip_text


def test_parse_english_slip():
    text = """
    Bangkok Bank
    Transfer Successful
    Date 09/07/2026 Time 14:35
    Amount 1,250.00 THB
    From: John Smith
    To: Jane Doe
    Ref: TXN123456789
    """
    result = parse_slip_text(text, base_confidence=0.9)

    assert result.bank == "Bangkok Bank"
    assert result.amount == Decimal("1250.00")
    assert result.slip_date == date(2026, 7, 9)
    assert result.slip_time == time(14, 35)
    assert result.reference_number == "TXN123456789"
    assert result.confidence > 0.5


def test_parse_thai_slip():
    text = """
    ธนาคารกสิกรไทย
    โอนเงินสำเร็จ
    วันที่ 9 ก.ค. 2569 เวลา 09:15
    จำนวนเงิน 500.50 บาท
    เลขที่รายการ ABC987654321
    """
    result = parse_slip_text(text, base_confidence=0.85)

    assert result.bank == "Kasikornbank (KBank)"
    assert result.amount == Decimal("500.50")
    assert result.slip_date == date(2026, 7, 9)
    assert result.slip_time == time(9, 15)
    assert result.reference_number == "ABC987654321"


def test_parse_slip_missing_fields_lowers_confidence():
    text = "some unrelated OCR noise with no useful fields"
    result = parse_slip_text(text, base_confidence=0.9)

    assert result.amount is None
    assert result.bank is None
    assert result.confidence < 0.6


def test_parse_slip_picks_largest_amount_when_unlabeled():
    text = "Fee 5.00 Total paid 999.00 Reference misc 12.34"
    result = parse_slip_text(text)

    assert result.amount == Decimal("999.00")


# -- regression tests: a parsed amount of zero must never be treated as a
# confidently-extracted value (it silently saved Amount=0 to the sheet
# instead of prompting for manual re-entry - see conversation.py's
# is_amount_valid gating). -------------------------------------------------


def test_parse_slip_labeled_zero_amount_is_not_accepted():
    text = "Bangkok Bank\nAmount 0.00 THB\nDate 09/07/2026"
    result = parse_slip_text(text, base_confidence=0.9)

    assert result.amount is None
    assert not result.field_confidence.get("amount")


def test_parse_slip_falls_through_to_standalone_when_labeled_amount_is_zero():
    # The label matches "0.00" first, but a real amount (150.00) appears
    # elsewhere on the slip - it must still be recovered instead of giving
    # up entirely.
    text = "Amount 0.00 THB\nTotal paid 150.00"
    result = parse_slip_text(text)

    assert result.amount == Decimal("150.00")


def test_parse_slip_standalone_zero_only_returns_none():
    text = "Fee 0.00 Reference 0.00"
    result = parse_slip_text(text)

    assert result.amount is None


def test_parse_slip_zero_amount_never_reports_amount_found():
    # This is the actual protection: conversation.py's is_amount_valid()
    # gate always routes to manual re-entry when result.amount is None,
    # regardless of the blended confidence score. Without the fix, a
    # spurious "Amount 0.00" match would set found["amount"] = True and
    # result.amount = Decimal("0"), which is not None - silently bypassing
    # that gate and saving Amount=0 to the sheet.
    text = "Bangkok Bank\nAmount 0.00 THB\nDate 09/07/2026 Time 14:35"
    result = parse_slip_text(text, base_confidence=0.9)

    assert result.amount is None
    assert "amount" not in result.field_confidence or not result.field_confidence["amount"]
