from migrate_thai_categories import find_thai_category_rows


def _record(category: str, **overrides) -> dict:
    base = {
        "Date": "2026-07-09", "Time": "14:00:00", "Amount": "100.00",
        "Bank": "Cash (No Slip)", "Sender": "", "Receiver": "",
        "Reference Number": "", "Category": category, "Remark": "",
        "Drive URL": "", "Telegram File ID": "", "OCR Confidence": "1.00",
        "User ID": "42",
    }
    base.update(overrides)
    return base


def test_finds_rows_with_thai_category():
    records = [
        _record("Food"),          # already English - untouched
        _record("อาหาร"),         # Thai - needs migrating (row 3)
        _record("ค่าบิล"),        # Thai - needs migrating (row 4)
    ]

    affected = find_thai_category_rows(records)

    assert affected == [
        (3, "อาหาร", "Food"),
        (4, "ค่าบิล", "Bills"),
    ]


def test_no_thai_rows_returns_empty_list():
    records = [_record("Food"), _record("Bills"), _record("Other")]
    assert find_thai_category_rows(records) == []


def test_unrecognized_category_left_untouched():
    records = [_record("SomeCustomCategory")]
    assert find_thai_category_rows(records) == []


def test_empty_category_left_untouched():
    records = [_record("")]
    assert find_thai_category_rows(records) == []


def test_row_numbers_account_for_header_row():
    # records[0] corresponds to sheet row 2 (row 1 is the header).
    records = [_record("Food"), _record("ครอบครัว")]
    affected = find_thai_category_rows(records)
    assert affected == [(3, "ครอบครัว", "Family")]
