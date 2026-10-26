"""Which transactions belong in a spending total.

counts_toward_total is the only place that decides this. Confirmed and
expected rows count. Review rows stay out until they are confirmed.
Cancelled rows stay out too.
"""

from __future__ import annotations

_STATUSES = ("confirmed", "review", "expected", "cancelled")


def counts_toward_total(status: str) -> bool:
    """Return True when a row should be added into dashboard and month totals."""
    return status in {"confirmed", "expected"}


def counted_statuses() -> tuple[str, ...]:
    """Statuses currently included by counts_toward_total."""
    return tuple(status for status in _STATUSES if counts_toward_total(status))
