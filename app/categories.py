"""Category lists for the three profiles, plus merchant-rule keys.

Student is the core list plus the student add-ons. Business is the core list
plus the business add-ons. Personal is the core list alone. Custom names are
appended after the profile list, and a custom name that repeats a built-in
category is dropped.
"""

from __future__ import annotations

import re

CORE = (
    "Groceries",
    "Dining out",
    "Transport",
    "Rent & utilities",
    "Shopping",
    "Health",
    "Entertainment",
    "Subscriptions",
    "Travel",
    "Income",
    "Transfers",
    "Other",
)

STUDENT = (
    "Tuition & fees",
    "Books & supplies",
    "Health insurance",
    "Visa & immigration fees",
    "Flights home",
    "Family transfers",
)

BUSINESS = (
    "Inventory",
    "Software & tools",
    "Marketing",
    "Payroll",
    "Office & equipment",
    "Taxes & fees",
)

PROFILES = ("student", "personal", "business")

# Whole words removed so "Starbucks Store 55" and "STARBUCKS #1234" share a key.
# "store" is included because the two Starbucks forms in the spec have to match.
_DROPPED_TOKENS = frozenset(
    {"inc", "llc", "ltd", "co", "corp", "company", "incorporated", "store", "stores"}
)


def get_categories(profile: str, custom: list[str] | None = None) -> list[str]:
    """Return category names for a profile, with custom names at the end."""
    key = profile.strip().casefold()
    if key == "student":
        names = [*CORE, *STUDENT]
    elif key == "personal":
        names = [*CORE]
    elif key == "business":
        names = [*CORE, *BUSINESS]
    else:
        raise ValueError("profile must be student, personal, or business")

    seen = {name.casefold() for name in names}
    for item in custom or []:
        cleaned = " ".join(item.split())
        if not cleaned or cleaned.casefold() in seen:
            continue
        names.append(cleaned)
        seen.add(cleaned.casefold())
    return names


def normalize_merchant(name: str) -> str:
    """Build the merchant_rules key.

    Lowercase the name, delete digits, store numbers, and punctuation, then
    drop legal suffixes and the word "store". "STARBUCKS #1234" and
    "Starbucks Store 55" both become "starbucks".
    """
    text = name.casefold()
    text = re.sub(r"[\'’]", "", text)
    text = re.sub(r"[^a-z\s]", " ", text)
    tokens = [token for token in text.split() if token not in _DROPPED_TOKENS]
    return " ".join(tokens)
