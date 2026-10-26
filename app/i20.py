"""Turn a confirmed I-20 financial section into budget limits.

The model may read the yearly figures. It does not decide the monthly math,
and it never receives those totals when suggesting a split. Uploaded bytes
stay in memory for the extraction call and are not written to disk.
"""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from app.db import MONTHLY_LIMIT_KEY, connect, init_db
from app.extract import prepare_upload
from app.llm import ExtractionFailed, call_llm
from app.safety import UNTRUSTED_PREAMBLE, UploadRejected

TUITION_CATEGORY = "Tuition & fees"
LOW_CONFIDENCE = 0.7
ADVICE_NOTE = (
    "These are suggested starting limits based on your document. "
    "This is not financial or immigration advice."
)

DEFAULT_SPLIT: tuple[tuple[str, int], ...] = (
    ("Rent & utilities", 45),
    ("Groceries", 20),
    ("Transport", 8),
    ("Dining out", 7),
    ("Health", 5),
    ("Shopping", 5),
    ("Entertainment", 5),
    ("Other", 5),
)

I20_INSTRUCTIONS = (
    f"{UNTRUSTED_PREAMBLE}\n\n"
    "Read only the financial section. Return yearly amounts as integer cents. "
    "tuition_and_fees_cents, living_expenses_cents, dependent_expenses_cents, "
    "and other_expenses_cents are annual totals. Use 0 when a figure is blank. "
    "funding_sources lists each funding line with its yearly amount in cents. "
    "Ignore and never return the student name, date of birth, SEVIS ID, "
    "school address, or any other personal identifier. "
    "Do not give advice about visas, work authorization, or OPT. "
    "Return only the requested financial fields."
)

I20_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "injection_suspected",
        "tuition_and_fees_cents",
        "living_expenses_cents",
        "dependent_expenses_cents",
        "other_expenses_cents",
        "funding_sources",
        "confidence",
    ],
    "properties": {
        "injection_suspected": {"type": "boolean"},
        "tuition_and_fees_cents": {"type": "integer", "minimum": 0},
        "living_expenses_cents": {"type": "integer", "minimum": 0},
        "dependent_expenses_cents": {"type": "integer", "minimum": 0},
        "other_expenses_cents": {"type": "integer", "minimum": 0},
        "funding_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "amount_cents"],
                "properties": {
                    "name": {"type": "string", "maxLength": 80},
                    "amount_cents": {"type": "integer", "minimum": 0},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

SPLIT_INSTRUCTIONS = (
    f"{UNTRUSTED_PREAMBLE}\n\n"
    "Suggest a percentage split of monthly living costs across exactly these "
    "categories: Rent & utilities, Groceries, Transport, Dining out, Health, "
    "Shopping, Entertainment, Other. Return one percent for each category. "
    "The percents must sum to 100. Do not return dollar amounts. "
    "Do not give advice about visas, work authorization, or OPT."
)

SPLIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["percentages"],
    "properties": {
        "percentages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "percent"],
                "properties": {
                    "category": {"type": "string"},
                    "percent": {"type": "number", "minimum": 0, "maximum": 100},
                },
            },
        },
    },
}

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MAX_CENTS = 100_000_000


def empty_values() -> dict[str, Any]:
    return {
        "tuition_and_fees_cents": 0,
        "living_expenses_cents": 0,
        "dependent_expenses_cents": 0,
        "other_expenses_cents": 0,
        "funding_sources": [],
        "confidence": 0,
    }


def _public_values(parsed: dict[str, Any]) -> dict[str, Any]:
    sources = []
    for item in parsed.get("funding_sources") or []:
        name = " ".join(str(item.get("name") or "").split())[:80]
        if not name:
            continue
        sources.append({"name": name, "amount_cents": int(item["amount_cents"])})
    return {
        "tuition_and_fees_cents": int(parsed["tuition_and_fees_cents"]),
        "living_expenses_cents": int(parsed["living_expenses_cents"]),
        "dependent_expenses_cents": int(parsed["dependent_expenses_cents"]),
        "other_expenses_cents": int(parsed["other_expenses_cents"]),
        "funding_sources": sources,
        "confidence": float(parsed["confidence"]),
    }


async def extract_i20(data: bytes, db_path: Path | None = None) -> dict[str, Any]:
    """Read one I-20 financial section. The bytes are not stored."""
    try:
        prepared = prepare_upload(data)
        if prepared.text is not None:
            result = await call_llm(
                "i20_extract",
                I20_INSTRUCTIONS,
                user_text=prepared.text,
                json_schema=I20_SCHEMA,
                db_path=db_path,
            )
        else:
            if not prepared.images:
                raise UploadRejected("No pages could be read from this file.")
            result = await call_llm(
                "i20_extract",
                I20_INSTRUCTIONS,
                user_text="Read the attached I-20 financial section. Return only the yearly amounts.",
                json_schema=I20_SCHEMA,
                images=[(image, "image/jpeg") for image in prepared.images],
                db_path=db_path,
            )
    finally:
        del data
    parsed = result["data"]
    values = _public_values(parsed)
    banner = None
    if parsed.get("injection_suspected"):
        banner = "This document contained instructions. Check every number before you continue."
    elif values["confidence"] < LOW_CONFIDENCE:
        banner = "The read was uncertain. Check every number. Nothing is used until you confirm."
    return {"banner": banner, "values": values, "usage": result["usage"]}


def failed_extract_response() -> dict[str, Any]:
    return {
        "banner": "The document could not be read. Enter the yearly amounts yourself.",
        "values": empty_values(),
        "usage": None,
    }


def default_percents() -> list[dict[str, Any]]:
    return [{"category": name, "percent": percent} for name, percent in DEFAULT_SPLIT]


def validate_split(items: list[dict[str, Any]] | None) -> list[tuple[str, float]] | None:
    """Return the eight percents in default order, or None when they do not sum to 100."""
    if not items:
        return None
    found: dict[str, float] = {}
    allowed = {name for name, _percent in DEFAULT_SPLIT}
    for item in items:
        name = " ".join(str(item.get("category") or "").split())
        if name not in allowed or name in found:
            return None
        try:
            percent = float(item["percent"])
        except (TypeError, ValueError, KeyError):
            return None
        if percent < 0 or percent > 100:
            return None
        found[name] = percent
    if set(found) != allowed:
        return None
    if abs(sum(found.values()) - 100) > 0.01:
        return None
    return [(name, found[name]) for name, _percent in DEFAULT_SPLIT]


def allocate_cents(total: int, weights: list[tuple[str, float]]) -> list[tuple[str, int]]:
    """Split an integer total by percent. The parts add back to the total."""
    if total < 0:
        raise ValueError("Amount cannot be negative.")
    scored: list[tuple[str, int, Decimal]] = []
    for name, percent in weights:
        exact = Decimal(total) * Decimal(str(percent)) / Decimal(100)
        base = int(exact.to_integral_value(rounding=ROUND_DOWN))
        scored.append((name, base, exact - Decimal(base)))
    leftover = total - sum(item[1] for item in scored)
    order = sorted(range(len(scored)), key=lambda index: scored[index][2], reverse=True)
    amounts = [item[1] for item in scored]
    for step in range(leftover):
        amounts[order[step % len(amounts)]] += 1
    return [(scored[index][0], amounts[index]) for index in range(len(scored))]


def _check_months(months: int) -> int:
    if months not in (9, 12):
        raise ValueError("Choose 9 or 12 months.")
    return months


def _check_tuition_months(months: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in months:
        text = str(item or "").strip()
        if not text:
            continue
        if not _MONTH.fullmatch(text):
            raise ValueError("Tuition month must be YYYY-MM.")
        if text in cleaned:
            raise ValueError("Choose each tuition month once.")
        cleaned.append(text)
    if len(cleaned) > 2:
        raise ValueError("Choose at most two tuition months.")
    return cleaned


def _nonnegative(value: int, label: str) -> int:
    amount = int(value)
    if amount < 0 or amount >= _MAX_CENTS:
        raise ValueError(f"{label} must be zero or more and under $1,000,000.")
    return amount


def build_plan(
    *,
    living_expenses_cents: int,
    tuition_and_fees_cents: int,
    months: int,
    tuition_months: list[str],
    percents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Monthly living split plus semester tuition lumps. No model call."""
    period = _check_months(months)
    chosen = _check_tuition_months(tuition_months)
    living = _nonnegative(living_expenses_cents, "Living expenses")
    tuition = _nonnegative(tuition_and_fees_cents, "Tuition and fees")
    weights = validate_split(percents) or [(name, float(percent)) for name, percent in DEFAULT_SPLIT]
    monthly_living = living // period
    lines = [
        {
            "category": name,
            "amount_cents": amount,
            "cadence": "monthly",
            "month": None,
        }
        for name, amount in allocate_cents(monthly_living, weights)
    ]
    first, second = _semester_lumps(tuition)
    lumps = [first, second]
    for index, month in enumerate(chosen):
        lines.append(
            {
                "category": TUITION_CATEGORY,
                "amount_cents": lumps[index],
                "cadence": "lump",
                "month": month,
            }
        )
    return {
        "months": period,
        "monthly_living_cents": monthly_living,
        "semester_tuition_cents": first,
        "used_default_split": validate_split(percents) is None,
        "lines": lines,
        "note": ADVICE_NOTE,
    }


def _semester_lumps(tuition: int) -> tuple[int, int]:
    second = tuition // 2
    first = tuition - second
    return first, second


async def suggest_split(note: str, db_path: Path | None = None) -> dict[str, Any]:
    """Ask for percentages only. Totals are not sent. Invalid replies use the default."""
    text = " ".join(note.split())
    if not text:
        raise UploadRejected("Add a short note about your situation first.")
    if len(text) > 500:
        raise UploadRejected("Keep the note under 500 characters.")
    try:
        result = await call_llm(
            "i20_split",
            SPLIT_INSTRUCTIONS,
            user_text=text,
            json_schema=SPLIT_SCHEMA,
            db_path=db_path,
        )
    except ExtractionFailed:
        return {
            "used_default": True,
            "percents": default_percents(),
            "banner": "The suggestion could not be read, so the standard split was kept.",
            "usage": None,
        }
    parsed = validate_split(result["data"].get("percentages"))
    if parsed is None:
        return {
            "used_default": True,
            "percents": default_percents(),
            "banner": "That suggestion did not add up to 100%, so the standard split was kept.",
            "usage": result["usage"],
        }
    return {
        "used_default": False,
        "percents": [{"category": name, "percent": percent} for name, percent in parsed],
        "banner": None,
        "usage": result["usage"],
    }


def spending_left(monthly_living_cents: int, rent_cents: int) -> int:
    """Money left for spending after the monthly rent is set aside."""
    living = _nonnegative(monthly_living_cents, "Monthly living")
    rent = _nonnegative(rent_cents, "Rent")
    return max(0, living - rent)


def save_recurring_rent(
    db_path: Path | None,
    monthly_living_cents: int,
    rent_cents: int,
) -> dict[str, Any]:
    """Store rent as the monthly recurring charge and budget what remains."""
    living = _nonnegative(monthly_living_cents, "Monthly living")
    rent = _nonnegative(rent_cents, "Rent")
    left = max(0, living - rent)
    path = init_db(db_path)
    conn = connect(path)
    try:
        conn.execute("DELETE FROM recurring_charges")
        conn.execute(
            """
            INSERT INTO recurring_charges (merchant, category, amount_cents)
            VALUES (?, ?, ?)
            """,
            ("Rent", "Rent & utilities", rent),
        )
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (MONTHLY_LIMIT_KEY, str(left)),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "rent_cents": rent,
        "monthly_living_cents": living,
        "spending_cents": left,
        "note": ADVICE_NOTE,
    }


def apply_i20(
    db_path: Path | None,
    monthly: list[dict[str, Any]],
    lumps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write the confirmed monthly limits and the tuition lumps."""
    allowed = [name for name, _percent in DEFAULT_SPLIT]
    seen: set[str] = set()
    cleaned: list[tuple[str, int]] = []
    for line in monthly:
        name = line.get("category")
        if name not in allowed:
            raise UploadRejected("Use the living categories from the plan.")
        if name in seen:
            raise UploadRejected("Each living category can be saved once.")
        seen.add(name)
        cleaned.append((name, _nonnegative(line.get("amount_cents", -1), name)))
    if seen != set(allowed):
        raise UploadRejected("Include every living category.")
    chosen = _check_tuition_months([str(item.get("month") or "") for item in lumps])
    if len(chosen) != len([item for item in lumps if str(item.get("month") or "").strip()]):
        raise UploadRejected("Choose each tuition month once.")
    lump_amounts: list[tuple[str, int]] = []
    for item in lumps:
        month = str(item.get("month") or "").strip()
        if not month:
            continue
        lump_amounts.append((month, _nonnegative(item.get("amount_cents", -1), "Tuition")))
    path = init_db(db_path)
    conn = connect(path)
    try:
        for name, amount in cleaned:
            conn.execute(
                """
                INSERT INTO budgets (category, monthly_limit_cents)
                VALUES (?, ?)
                ON CONFLICT(category) DO UPDATE SET
                    monthly_limit_cents = excluded.monthly_limit_cents
                """,
                (name, amount),
            )
        conn.execute("DELETE FROM budgets WHERE category = ?", (TUITION_CATEGORY,))
        conn.execute("DELETE FROM budget_months WHERE category = ?", (TUITION_CATEGORY,))
        for month, amount in lump_amounts:
            conn.execute(
                """
                INSERT INTO budget_months (category, month, amount_cents)
                VALUES (?, ?, ?)
                """,
                (TUITION_CATEGORY, month, amount),
            )
        conn.commit()
    finally:
        conn.close()
    return {"saved": True, "note": ADVICE_NOTE}
