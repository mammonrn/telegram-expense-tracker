"""Guards against the "Thai category stored in the Sheet" regression:
CATEGORIES must stay the English canonical source of truth used for
storage/search/aggregation; CATEGORY_LABELS_TH is display-only.
"""

import re

from config import CATEGORIES, CATEGORY_LABELS_EN, CATEGORY_LABELS_TH, category_display

_ASCII_ONLY = re.compile(r"^[\x00-\x7f]+$")


def test_category_canonical_labels_are_english():
    for key, (emoji, label) in CATEGORIES.items():
        assert _ASCII_ONLY.match(label), f"Category '{key}' has a non-English canonical label: {label!r}"


def test_every_canonical_category_has_a_thai_display_label():
    canonical_labels = {label for _, label in CATEGORIES.values()}
    assert canonical_labels == set(CATEGORY_LABELS_TH.keys())


def test_category_labels_en_is_the_exact_reverse_of_th():
    for en, th in CATEGORY_LABELS_TH.items():
        assert CATEGORY_LABELS_EN[th] == en


def test_category_display_translates_known_category():
    assert category_display("Food") == "อาหาร"
    assert category_display("Bills") == "ค่าบิล"


def test_category_display_passes_through_unknown_value():
    assert category_display("SomeCustomCategory") == "SomeCustomCategory"
