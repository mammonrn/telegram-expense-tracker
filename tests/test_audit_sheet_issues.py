from audit_sheet_issues import find_zero_amount_rows


def _record(amount: str, **overrides) -> dict:
    base = {
        "Date": "2026-07-09", "Time": "14:00:00", "Amount": amount,
        "Bank": "Bangkok Bank", "Sender": "", "Receiver": "",
        "Reference Number": "", "Category": "Food", "Remark": "",
        "Drive URL": "", "Telegram File ID": "", "OCR Confidence": "0.60",
        "User ID": "42",
    }
    base.update(overrides)
    return base


def test_finds_rows_with_zero_amount():
    records = [_record("150.00"), _record("0.00"), _record("0")]
    affected = find_zero_amount_rows(records)
    assert affected == [(3, "0.00"), (4, "0")]


def test_finds_rows_with_unparseable_amount():
    records = [_record("150.00"), _record("not-a-number")]
    affected = find_zero_amount_rows(records)
    assert affected == [(3, "not-a-number")]


def test_finds_rows_with_negative_amount():
    records = [_record("-5.00")]
    assert find_zero_amount_rows(records) == [(2, "-5.00")]


def test_finds_rows_with_empty_amount():
    records = [_record("")]
    assert find_zero_amount_rows(records) == [(2, "")]


def test_no_bad_rows_returns_empty_list():
    records = [_record("150.00"), _record("99.99")]
    assert find_zero_amount_rows(records) == []
