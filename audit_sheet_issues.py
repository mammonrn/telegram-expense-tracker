"""Read-only audit: find existing Sheet rows affected by two now-fixed
bugs - a Category value saved in Thai instead of English, and an Amount
saved as zero (or otherwise unusable) because OCR failed silently instead
of prompting for manual re-entry.

This script only reads and reports; it never writes to the Sheet. Use it
to see exactly which rows need a look before deciding what to do about
them (Thai categories can be auto-fixed with migrate_thai_categories.py;
Amount=0 rows need the real amount looked up manually, e.g. from the
original slip in Drive, since there's no way to recover it after the
fact).

Usage:
    python audit_sheet_issues.py
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from config import load_config
from migrate_thai_categories import find_thai_category_rows
from sheet import SheetManager


def find_zero_amount_rows(records: list[dict[str, str]]) -> list[tuple[int, str]]:
    """Return (row_number, raw_amount) for every row whose Amount is zero,
    negative, or not parseable as a number at all."""
    affected: list[tuple[int, str]] = []
    for idx, record in enumerate(records, start=2):
        raw = record.get("Amount", "")
        try:
            amount = Decimal(str(raw)) if str(raw).strip() else None
        except InvalidOperation:
            amount = None
        if amount is None or amount <= 0:
            affected.append((idx, raw))
    return affected


def main() -> None:
    config = load_config()
    sheets = SheetManager(
        credentials_path=config.google_application_credentials,
        spreadsheet_id=config.spreadsheet_id,
    )
    records = sheets.get_all_records()

    thai_category_rows = find_thai_category_rows(records)
    zero_amount_rows = find_zero_amount_rows(records)

    print("=" * 60)
    print("Sheet audit (read-only - nothing is changed by this script)")
    print("=" * 60)

    if thai_category_rows:
        print(f"\n⚠️  {len(thai_category_rows)} row(s) with a Thai Category value:")
        for row, old_value, new_value in thai_category_rows:
            print(f"  row {row}: Category = '{old_value}' (canonical value: '{new_value}')")
    else:
        print("\n✅ No rows with a Thai Category value.")

    if zero_amount_rows:
        print(f"\n⚠️  {len(zero_amount_rows)} row(s) with Amount = 0 (or unusable):")
        for row, raw_amount in zero_amount_rows:
            print(f"  row {row}: Amount = '{raw_amount}'")
    else:
        print("\n✅ No rows with Amount = 0.")

    if thai_category_rows or zero_amount_rows:
        print(
            "\nNothing was changed by this script.\n"
            "- Thai Category values: fix automatically with "
            "'python migrate_thai_categories.py'.\n"
            "- Amount = 0 rows: the correct amount can't be recovered automatically - "
            "check the Drive URL / Telegram File ID column for the original slip and "
            "correct the value yourself (e.g. via /edit in the bot, or directly in "
            "the Sheet)."
        )
    else:
        print("\nNothing to review - both columns look clean.")


if __name__ == "__main__":
    main()
