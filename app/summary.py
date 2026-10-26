"""Month totals for the dashboard.

Spending uses counts_toward_total, so only confirmed and expected rows are
included. A category with no saved limit is no_budget. Limits are the
standing monthly amounts, one per category.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.categories import get_categories
from app.db import CUSTOM_KEY, MONTHLY_LIMIT_KEY, PROFILE_KEY, connect, init_db, read_settings
from app.projection import alert_sort_key, classify_spending, project_category
from app.totals import counted_statuses

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def shift_month(month: str, delta: int) -> str:
    year, mon = (int(part) for part in month.split("-"))
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    return f"{sign}${value // 100:,}.{value % 100:02d}"


def budget_state(spent_cents: int, limit_cents: int | None) -> dict[str, Any]:
    """Return state, percent, and the short label for one spent/limit pair."""
    if limit_cents is None:
        return {"state": "no_budget", "percent": None, "label": "No budget set"}
    spent = int(spent_cents)
    limit = int(limit_cents)
    if limit == 0:
        if spent <= 0:
            return {"state": "calm", "percent": 0.0, "label": "Well within budget"}
        return {
            "state": "over",
            "percent": None,
            "label": f"Over budget by {_dollars(spent)}",
        }
    ratio = spent / limit
    percent = round(ratio * 1000) / 10
    if ratio < 0.70:
        state, label = "calm", "Well within budget"
    elif ratio < 0.90:
        state, label = "almost", "Getting close"
    elif ratio <= 1:
        state, label = "warning", "Almost at limit"
    else:
        state, label = "over", f"Over budget by {_dollars(spent - limit)}"
    return {"state": state, "percent": percent, "label": label}


def _check_month(month: str) -> str:
    if not _MONTH.fullmatch(month):
        raise ValueError("month must be YYYY-MM")
    return month


def _profile_categories(db_path: Path) -> list[str]:
    raw = read_settings(db_path)
    custom = json.loads(raw.get(CUSTOM_KEY, "[]"))
    return get_categories(raw.get(PROFILE_KEY, "student"), custom)


def _month_of(date_value: str | None, created_at: str) -> str:
    if date_value:
        return date_value[:7]
    return created_at[:7]


def _load_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT date, created_at, amount_cents, category, status FROM transactions"
    ).fetchall()


def _recurring_by_category(conn: sqlite3.Connection) -> dict[str, int]:
    """Rent and other repeating charges count in every month."""
    totals: dict[str, int] = {}
    rows = conn.execute(
        "SELECT category, amount_cents FROM recurring_charges"
    ).fetchall()
    for row in rows:
        name = str(row["category"])
        totals[name] = totals.get(name, 0) + int(row["amount_cents"])
    return totals


def _split_by_category(rows: list[sqlite3.Row], month: str) -> dict[str, dict[str, int]]:
    buckets: dict[str, dict[str, int]] = {}
    for row in rows:
        if _month_of(row["date"], row["created_at"]) != month:
            continue
        if int(row["amount_cents"]) < 0:
            continue
        kind = classify_spending(str(row["status"]), str(row["category"]))
        if kind is None:
            continue
        name = str(row["category"])
        bucket = buckets.setdefault(name, {"committed": 0, "discretionary": 0})
        bucket[kind] += int(row["amount_cents"])
    return buckets


def _spent_by_category(rows: list[sqlite3.Row], month: str) -> dict[str, int]:
    counted = set(counted_statuses())
    totals: dict[str, int] = {}
    for row in rows:
        if row["status"] not in counted:
            continue
        if int(row["amount_cents"]) < 0:
            continue
        if _month_of(row["date"], row["created_at"]) != month:
            continue
        name = str(row["category"])
        totals[name] = totals.get(name, 0) + int(row["amount_cents"])
    return totals


def _limits(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT category, monthly_limit_cents FROM budgets").fetchall()
    return {str(row["category"]): int(row["monthly_limit_cents"]) for row in rows}


def _limits_for_month(conn: sqlite3.Connection, month: str) -> dict[str, int]:
    """Standing limits, with a month lump replacing that category for one month."""
    limits = _limits(conn)
    rows = conn.execute(
        "SELECT category, amount_cents FROM budget_months WHERE month = ?",
        (month,),
    ).fetchall()
    for row in rows:
        limits[str(row["category"])] = int(row["amount_cents"])
    return limits


def tuition_lumps(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT month, amount_cents FROM budget_months
        WHERE category = ?
        ORDER BY month
        """,
        ("Tuition & fees",),
    ).fetchall()
    return [
        {"month": str(row["month"]), "amount_cents": int(row["amount_cents"])}
        for row in rows
    ]


def _selector_months(rows: list[sqlite3.Row], selected: str) -> list[str]:
    found = sorted({_month_of(row["date"], row["created_at"]) for row in rows})
    latest = found[-6:]
    if selected not in latest:
        latest = sorted(set(latest[-5:]) | {selected})
    return latest


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    return f"{sign}${value // 100:,}.{value % 100:02d}"


def spending_blurb(
    month: str,
    spent_cents: int,
    budget_cents: int | None,
    categories: list[dict[str, Any]],
) -> str:
    """One or two factual sentences about the month. No advice."""
    name = _MONTH_NAMES[int(month.split("-")[1]) - 1]
    if spent_cents == 0:
        return f"No spending recorded for {name} yet."
    if budget_cents is None:
        first = f"You spent {_money(spent_cents)} in {name}."
    else:
        first = f"You spent {_money(spent_cents)} of a {_money(budget_cents)} budget in {name}."
    ranked = [row for row in categories if int(row["spent_cents"]) > 0]
    if not ranked:
        return first
    top = max(ranked, key=lambda row: int(row["spent_cents"]))
    return f"{first} {top['category']} is the largest part, at {_money(int(top['spent_cents']))}."


def monthly_summary(db_path: Path | None, month: str) -> dict[str, Any]:
    """Spent, limits, and budget state for one month, plus a 6-month trend."""
    selected = _check_month(month)
    path = init_db(db_path)
    names = _profile_categories(path)
    conn = connect(path)
    try:
        rows = _load_rows(conn)
        limits = _limits_for_month(conn, selected)
        limit_row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (MONTHLY_LIMIT_KEY,),
        ).fetchone()
        recurring = _recurring_by_category(conn)
    finally:
        conn.close()
    saved_sum = None
    if limit_row is not None and str(limit_row["value"]).strip() != "":
        saved_sum = int(limit_row["value"])

    spent = _spent_by_category(rows, selected)
    split = _split_by_category(rows, selected)
    for name, amount in recurring.items():
        parts = split.setdefault(name, {"committed": 0, "discretionary": 0})
        parts["committed"] += amount
    today = datetime.now(timezone.utc).date()
    ordered = list(names)
    for extra in sorted(set(spent) | set(limits) | set(recurring)):
        if extra not in ordered:
            ordered.append(extra)
    categories = []
    for name in ordered:
        amount = int(spent.get(name, 0)) + int(recurring.get(name, 0))
        limit = limits.get(name)
        projection = None
        if limit is not None:
            parts = split.get(name, {"committed": 0, "discretionary": 0})
            projection = project_category(
                category=name,
                limit_cents=limit,
                committed_cents=parts["committed"],
                discretionary_cents=parts["discretionary"],
                month=selected,
                today=today,
            )
        categories.append(
            {
                "category": name,
                "spent_cents": amount,
                "limit_cents": limit,
                **budget_state(amount, limit),
                "projection": projection,
            }
        )
    spent_total = sum(item["spent_cents"] for item in categories)
    set_limits = [item["limit_cents"] for item in categories if item["limit_cents"] is not None]
    if saved_sum is not None:
        budget_total = saved_sum
    else:
        budget_total = sum(set_limits) if set_limits else None
    trend = []
    for offset in range(-5, 1):
        point = shift_month(selected, offset)
        trend.append(
            {
                "month": point,
                "spent_cents": sum(_spent_by_category(rows, point).values()) + sum(recurring.values()),
            }
        )
    return {
        "month": selected,
        "months": _selector_months(rows, selected),
        "spent_cents": spent_total,
        "budget_cents": budget_total,
        "blurb": spending_blurb(selected, spent_total, budget_total, categories),
        **budget_state(spent_total, budget_total),
        "categories": categories,
        "alerts": sorted(
            (
                item["projection"]
                for item in categories
                if item["projection"] is not None and item["projection"]["at_risk"]
            ),
            key=alert_sort_key,
        ),
        "trend": trend,
    }


def list_budget_form(db_path: Path | None) -> dict[str, Any]:
    path = init_db(db_path)
    conn = connect(path)
    try:
        limits = _limits(conn)
        lumps = tuition_lumps(conn)
    finally:
        conn.close()
    categories = [
        {"category": name, "monthly_limit_cents": limits.get(name)}
        for name in _profile_categories(path)
    ]
    return {"categories": categories, "tuition_lumps": lumps}


def save_budgets(db_path: Path | None, limits: list[dict[str, Any]], allowed: list[str]) -> dict[str, Any]:
    """Upsert the sent limits. A null amount removes that category's limit."""
    allowed_set = set(allowed)
    seen: set[str] = set()
    for line in limits:
        name = line["category"]
        if name not in allowed_set:
            raise ValueError(f"Unknown category: {name}")
        if name in seen:
            raise ValueError(f"Duplicate category: {name}")
        seen.add(name)
    path = init_db(db_path)
    conn = connect(path)
    try:
        for line in limits:
            amount = line["monthly_limit_cents"]
            if amount is None:
                conn.execute("DELETE FROM budgets WHERE category = ?", (line["category"],))
            else:
                conn.execute(
                    """
                    INSERT INTO budgets (category, monthly_limit_cents)
                    VALUES (?, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        monthly_limit_cents = excluded.monthly_limit_cents
                    """,
                    (line["category"], int(amount)),
                )
        conn.commit()
    finally:
        conn.close()
    return list_budget_form(path)
