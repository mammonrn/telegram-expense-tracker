import re
from decimal import Decimal

from handlers.commands import EXPENSE_QUERY_REGEX, _format_totals


def test_format_totals_includes_percentage_breakdown():
    totals = {"Food": Decimal("300.00"), "Bills": Decimal("100.00")}
    text = _format_totals("July 2026", totals)

    assert "300.00" in text
    assert "75.0%" in text  # 300 / 400
    assert "25.0%" in text  # 100 / 400
    assert "400.00" in text  # grand total


def test_format_totals_shows_thai_display_name_not_stored_english_value():
    # totals keys are always the English canonical value read back from
    # the Sheet's Category column - the Telegram-facing text must show
    # the Thai translation, not that raw stored value.
    totals = {"Food": Decimal("100.00")}
    text = _format_totals("July 2026", totals)

    assert "อาหาร" in text
    assert "Food" not in text


def test_format_totals_empty():
    assert "ไม่มีรายการค่าใช้จ่าย" in _format_totals("July 2026", {})


def test_expense_query_regex_matches_thai_phrases():
    assert EXPENSE_QUERY_REGEX.search("สรุปค่าใช้จ่ายหน่อย")
    assert EXPENSE_QUERY_REGEX.search("เดือนนี้ใช้เงินไปเท่าไหร่")
    assert EXPENSE_QUERY_REGEX.search("ขอดูยอดใช้จ่ายหน่อย")


def test_expense_query_regex_matches_english_phrases():
    assert EXPENSE_QUERY_REGEX.search("give me an expense summary")
    assert EXPENSE_QUERY_REGEX.search("how much have I spent")


def test_expense_query_regex_does_not_match_unrelated_text():
    assert not EXPENSE_QUERY_REGEX.search("150")
    assert not EXPENSE_QUERY_REGEX.search("hello there")
