"""One-time migration: fix Category values that were saved in Thai.

Between the Thai-translation change and this fix, the bot briefly wrote
the *Thai display label* (e.g. "อาหาร") into the Category column instead
of the English canonical value (e.g. "Food") that storage, search, and
stats aggregation all rely on as the single source of truth. Rows saved
during that window silently stopped aggregating with older/newer rows in
the same category.

This script finds every row whose Category is a known Thai label and
rewrites it to the matching English canonical value. Rows already in
English (or containing something unrecognized, e.g. a manually-typed
custom category) are left untouched. Safe to run multiple times.

Usage (run manually, once, after pulling this fix):

    python migrate_thai_categories.py --dry-run   # preview only
    python migrate_thai_categories.py              # apply the fix
"""

from __future__ import annotations

import argparse
import logging

from config import CATEGORY_LABELS_EN, load_config
from sheet import SheetManager

logger = logging.getLogger("expense_bot.migrate")


def find_thai_category_rows(records: list[dict[str, str]]) -> list[tuple[int, str, str]]:
    """Return (row_number, old_thai_value, new_english_value) for every row
    whose Category is a known Thai label.

    `records` is the list returned by `SheetManager.get_all_records()`
    (or `ExpenseDatabase.all_records()`), i.e. one dict per data row with
    row 1 being the header - so row numbers start at 2.
    """
    affected: list[tuple[int, str, str]] = []
    for idx, record in enumerate(records, start=2):
        category = record.get("Category", "")
        english = CATEGORY_LABELS_EN.get(category)
        if english is not None:
            affected.append((idx, category, english))
    return affected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing anything"
    )
    args = parser.parse_args()

    config = load_config()
    sheets = SheetManager(
        credentials_path=config.google_application_credentials,
        spreadsheet_id=config.spreadsheet_id,
    )

    records = sheets.get_all_records()
    affected = find_thai_category_rows(records)

    if not affected:
        print("No Thai category values found in the Category column - nothing to migrate.")
        return

    verb = "Would update" if args.dry_run else "Updating"
    print(f"{verb} {len(affected)} row(s):")
    for row_number, old_value, new_value in affected:
        print(f"  row {row_number}: '{old_value}' -> '{new_value}'")
        if not args.dry_run:
            sheets.update_row(row_number, {"Category": new_value})

    if args.dry_run:
        print("\nDry run only - no changes were made. Re-run without --dry-run to apply.")
    else:
        print(f"\n✅ Migrated {len(affected)} row(s) in the Category column.")


if __name__ == "__main__":
    main()
