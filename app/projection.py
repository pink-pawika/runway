"""Project whether a category will finish the month over its limit.

This is ordinary arithmetic. The model is not called. A projection exists
only for the current month, and only on day 5 or later. Committed spending
is an expected charge, or a charge in rent, tuition, or health insurance.
Everything else that counts toward the total is discretionary and is paced
forward from the daily rate.
"""

from __future__ import annotations

import calendar
from datetime import date

from app.totals import counts_toward_total

COMMITTED_CATEGORIES = frozenset(
    {
        "Rent & utilities",
        "Tuition & fees",
        "Health insurance",
    }
)


def classify_spending(status: str, category: str) -> str | None:
    """Return committed, discretionary, or None when the row is not counted."""
    if not counts_toward_total(status):
        return None
    if status == "expected" or category in COMMITTED_CATEGORIES:
        return "committed"
    return "discretionary"


def project_category(
    *,
    category: str,
    limit_cents: int,
    committed_cents: int,
    discretionary_cents: int,
    month: str,
    today: date,
) -> dict | None:
    """Return the pace for one budget, or None when this month is too early."""
    year_text, month_text = month.split("-")
    year, mon = int(year_text), int(month_text)
    if (today.year, today.month) != (year, mon) or today.day < 5:
        return None

    days_in_month = calendar.monthrange(year, mon)[1]
    days_elapsed = today.day
    days_remaining = days_in_month - today.day
    committed = int(committed_cents)
    discretionary = int(discretionary_cents)
    limit = int(limit_cents)
    spent = committed + discretionary
    rate = discretionary / days_elapsed
    projected = committed + discretionary + _div_round(discretionary * days_remaining, days_elapsed)
    if limit == 0:
        percent = 0.0 if projected <= 0 else None
    else:
        percent = round(projected / limit, 6)
    at_risk = (percent is not None and percent > 1) or (limit == 0 and projected > 0)

    hit_day = _hit_day(
        spent=spent,
        limit=limit,
        discretionary=discretionary,
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        today_day=today.day,
    )
    hit_date = None if hit_day is None else date(year, mon, hit_day).isoformat()
    days_early = None if hit_day is None else days_in_month - hit_day
    message = _message(
        category=category,
        projected=projected,
        limit=limit,
        spent=spent,
        hit_day=hit_day,
        days_early=days_early,
    )
    return {
        "category": category,
        "committed_cents": committed,
        "discretionary_cents": discretionary,
        "spent_cents": spent,
        "limit_cents": limit,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "days_in_month": days_in_month,
        "daily_rate_cents": rate,
        "projected_cents": projected,
        "projected_percent": percent,
        "hit_date": hit_date,
        "hit_day": hit_day,
        "days_early": days_early,
        "at_risk": at_risk,
        "message": message,
        "explain": [
            {"label": "Spent so far", "value": _plain_dollars(spent)},
            {"label": "Committed", "value": _plain_dollars(committed)},
            {"label": "Days elapsed", "value": str(days_elapsed)},
            {"label": "Daily rate", "value": _rate_text(rate)},
        ],
    }


def alert_sort_key(item: dict) -> tuple:
    """Already-over rows first, then the highest projected percent."""
    over = 1 if item["spent_cents"] >= item["limit_cents"] and item["spent_cents"] > 0 else 0
    percent = item["projected_percent"] if item["projected_percent"] is not None else 10**9
    hit = item["hit_day"] if item["hit_day"] is not None else 32
    return (-over, -percent, hit, item["category"])


def _hit_day(
    *,
    spent: int,
    limit: int,
    discretionary: int,
    days_elapsed: int,
    days_in_month: int,
    today_day: int,
) -> int | None:
    if limit <= 0 or spent >= limit or discretionary <= 0:
        return None
    need = limit - spent
    extra_days = (need * days_elapsed + discretionary - 1) // discretionary
    hit = today_day + extra_days
    if hit > days_in_month:
        return None
    return hit


def _div_round(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def _message(
    *,
    category: str,
    projected: int,
    limit: int,
    spent: int,
    hit_day: int | None,
    days_early: int | None,
) -> str:
    amounts = f"Projected: {_plain_dollars(projected)} of {_plain_dollars(limit)}."
    if spent > limit:
        return f"{category} is already over its limit. {amounts}"
    if spent == limit and limit > 0:
        return f"{category} is already at its limit. {amounts}"
    if hit_day is None or days_early is None:
        return f"At this pace, {category} stays within budget. {amounts}"
    early = "1 day early" if days_early == 1 else f"{days_early} days early"
    when = _ordinal(hit_day)
    if days_early == 0:
        return f"At this pace, {category} will hit its limit on the {when}. {amounts}"
    return f"At this pace, {category} will hit its limit on the {when} ({early}). {amounts}"


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _plain_dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    dollars, rest = divmod(value, 100)
    if rest == 0:
        return f"{sign}${dollars:,}"
    return f"{sign}${dollars:,}.{rest:02d}"


def _rate_text(cents_per_day: float) -> str:
    dollars = cents_per_day / 100
    if abs(dollars - round(dollars, 2)) < 1e-9:
        return f"${dollars:,.2f} per day"
    return f"${dollars:,.4f} per day"
