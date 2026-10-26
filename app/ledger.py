"""Read and edit saved transactions.

Month lists treat a missing date as the upload day. Approving a review row,
or saving a new category, stores a merchant rule. The model is not called.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.categories import normalize_merchant
from app.db import connect, init_db
from app.totals import counts_toward_total

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_BLOCKED_BATCH = ("Total mismatch", "Document contained suspicious instructions")


class MissingTransaction(Exception):
    """No transaction has this id."""


class NotReviewable(Exception):
    """Approve was used on a row that is not waiting for review."""


def batch_allows_approve_all(reasons: list[str | None]) -> bool:
    """A batch can be approved together only when every reason is safe.

    A total that does not match, or a document that tried to give the model
    instructions, has to be checked one row at a time.
    """
    if not reasons:
        return False
    for reason in reasons:
        text = reason or ""
        if any(marker in text for marker in _BLOCKED_BATCH):
            return False
    return True


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _public_row(row: sqlite3.Row) -> dict[str, Any]:
    tx_date = row["date"]
    return {
        "id": int(row["id"]),
        "date": tx_date,
        "created_at": row["created_at"],
        "date_assumed": tx_date is None,
        "merchant": row["merchant"],
        "amount_cents": int(row["amount_cents"]),
        "category": row["category"],
        "status": row["status"],
        "review_reason": row["review_reason"],
        "batch_id": row["batch_id"],
        "source": row["source"],
        "confidence": float(row["confidence"]),
    }


def _save_rule(conn: sqlite3.Connection, merchant: str, category: str) -> None:
    key = normalize_merchant(merchant)
    if not key:
        return
    conn.execute(
        """
        INSERT INTO merchant_rules (merchant_key, category, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(merchant_key) DO UPDATE SET
            category = excluded.category,
            updated_at = excluded.updated_at
        """,
        (key, category, _now()),
    )


def list_month(db_path: Path, month: str) -> dict[str, Any]:
    """Return one month, using the upload month when a row has no date."""
    if not _MONTH.fullmatch(month):
        raise ValueError("month must be YYYY-MM")
    path = init_db(db_path)
    conn = connect(path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM transactions
            WHERE (date IS NOT NULL AND substr(date, 1, 7) = ?)
               OR (date IS NULL AND substr(created_at, 1, 7) = ?)
            ORDER BY COALESCE(date, substr(created_at, 1, 10)) DESC, id DESC
            """,
            (month, month),
        ).fetchall()
        recurring_rows = conn.execute(
            "SELECT id, merchant, category, amount_cents FROM recurring_charges ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    items = [_public_row(row) for row in rows if int(row["amount_cents"]) >= 0]
    recurring = [
        {
            "id": int(row["id"]),
            "merchant": str(row["merchant"]),
            "category": str(row["category"]),
            "amount_cents": int(row["amount_cents"]),
        }
        for row in recurring_rows
    ]
    included = [item for item in items if counts_toward_total(item["status"])]
    held = [item for item in items if not counts_toward_total(item["status"])]
    totals: dict[str, int] = {}
    for item in included:
        totals[item["category"]] = totals.get(item["category"], 0) + int(item["amount_cents"])
    by_category = [
        {"category": name, "total_cents": amount}
        for name, amount in sorted(totals.items(), key=lambda pair: (-abs(pair[1]), pair[0]))
    ]
    return {
        "month": month,
        "confirmed_total_cents": sum(int(item["amount_cents"]) for item in included),
        "held_count": len(held),
        "held_cents": sum(int(item["amount_cents"]) for item in held),
        "by_category": by_category,
        "transactions": items,
        "recurring": recurring,
    }


def update_recurring(db_path: Path, charge_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    """Edit one repeating charge. It stays out of the transactions table."""
    if not changes:
        raise ValueError("change at least one field")
    path = init_db(db_path)
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT * FROM recurring_charges WHERE id = ?",
            (charge_id,),
        ).fetchone()
        if row is None:
            raise MissingTransaction
        merchant = changes.get("merchant", row["merchant"])
        amount = int(changes.get("amount_cents", row["amount_cents"]))
        category = changes.get("category", row["category"])
        if amount < 0 or amount >= 100_000_000:
            raise ValueError("amount must be under $1,000,000")
        conn.execute(
            """
            UPDATE recurring_charges
            SET merchant = ?, amount_cents = ?, category = ?
            WHERE id = ?
            """,
            (merchant, amount, category, charge_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT id, merchant, category, amount_cents FROM recurring_charges WHERE id = ?",
            (charge_id,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "id": int(updated["id"]),
        "merchant": str(updated["merchant"]),
        "category": str(updated["category"]),
        "amount_cents": int(updated["amount_cents"]),
    }


def update_transaction(db_path: Path, tx_id: int, changes: dict[str, Any]) -> dict[str, Any]:
    """Apply a validated edit. A category change stores a merchant rule."""
    if not changes:
        raise ValueError("change at least one field")
    path = init_db(db_path)
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if row is None:
            raise MissingTransaction
        merchant = changes.get("merchant", row["merchant"])
        amount = changes.get("amount_cents", row["amount_cents"])
        tx_date = changes["date"] if "date" in changes else row["date"]
        category = changes.get("category", row["category"])
        conn.execute(
            """
            UPDATE transactions
            SET merchant = ?, amount_cents = ?, date = ?, category = ?
            WHERE id = ?
            """,
            (merchant, amount, tx_date, category, tx_id),
        )
        if "category" in changes:
            _save_rule(conn, str(merchant), str(category))
        conn.commit()
        updated = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    finally:
        conn.close()
    return _public_row(updated)


def delete_transaction(db_path: Path, tx_id: int) -> None:
    path = init_db(db_path)
    conn = connect(path)
    try:
        row = conn.execute("SELECT batch_id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if row is None:
            raise MissingTransaction
        batch_id = row["batch_id"]
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
        if batch_id:
            left = conn.execute(
                "SELECT 1 FROM transactions WHERE batch_id = ? LIMIT 1",
                (batch_id,),
            ).fetchone()
            if left is None:
                conn.execute("DELETE FROM upload_files WHERE batch_id = ?", (batch_id,))
        conn.commit()
    finally:
        conn.close()


def upload_file(db_path: Path, batch_id: str) -> bytes | None:
    """Return the JPEG saved with an upload, or None when this batch has none."""
    path = init_db(db_path)
    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT image FROM upload_files WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return bytes(row["image"])


def list_review(db_path: Path) -> dict[str, Any]:
    path = init_db(db_path)
    conn = connect(path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM transactions
            WHERE status = 'review'
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        items = [_public_row(row) for row in rows if int(row["amount_cents"]) >= 0]
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for item in items:
            key = item["batch_id"] or f"row-{item['id']}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)
        batch_ids = [key for key in order if not key.startswith("row-")]
        has_file: set[str] = set()
        if batch_ids:
            placeholders = ", ".join("?" for _ in batch_ids)
            found = conn.execute(
                f"SELECT batch_id FROM upload_files WHERE batch_id IN ({placeholders})",
                batch_ids,
            ).fetchall()
            has_file = {str(row["batch_id"]) for row in found}
    finally:
        conn.close()
    batches = []
    for key in order:
        group = groups[key]
        batch_id = group[0]["batch_id"]
        batches.append(
            {
                "batch_id": batch_id,
                "has_file": batch_id is not None and batch_id in has_file,
                "can_approve_all": batch_id is not None
                and batch_allows_approve_all([item["review_reason"] for item in group]),
                "items": group,
            }
        )
    return {"count": len(items), "batches": batches}


def approve_transaction(db_path: Path, tx_id: int) -> dict[str, Any]:
    """Confirm one review row and remember its category for the next upload."""
    path = init_db(db_path)
    conn = connect(path)
    try:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if row is None:
            raise MissingTransaction
        if row["status"] != "review":
            raise NotReviewable
        conn.execute(
            """
            UPDATE transactions
            SET status = 'confirmed', review_reason = NULL
            WHERE id = ?
            """,
            (tx_id,),
        )
        _save_rule(conn, str(row["merchant"]), str(row["category"]))
        conn.commit()
        updated = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    finally:
        conn.close()
    return _public_row(updated)
