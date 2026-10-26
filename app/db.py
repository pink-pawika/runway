"""SQLite storage for SpendSmart.

Amount convention: amount_cents is an integer number of cents and may be
negative. Expenses are positive. Payments, refunds, and income are negative.

The budgets table holds one monthly limit per category. The overall monthly
spending limit from the product spec does not fit in that table, because the
primary key is the category name. That total is the settings key
monthly_limit_cents.

Dates are ISO strings: a transaction date is YYYY-MM-DD, and created_at, ts,
and updated_at are UTC timestamps. academic_year_months is a JSON list of
YYYY-MM strings. daily_runs.day is YYYY-MM-DD.

cost_usd is computed in code from token counts and the editable rates in
app/llm.py. The model never writes this column. A rate of zero stores a
computed cost of 0, which is not a measured price.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.env import REPO_ROOT

PROFILE_KEY = "profile"
MONTHS_KEY = "academic_year_months"
CUSTOM_KEY = "custom_categories"
MONTHLY_LIMIT_KEY = "monthly_limit_cents"
ALLOWED_SETTINGS = frozenset({PROFILE_KEY, MONTHS_KEY, CUSTOM_KEY, MONTHLY_LIMIT_KEY})

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    date TEXT,
    merchant TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'pdf', 'image', 'recurring')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'review', 'expected', 'cancelled')),
    review_reason TEXT,
    batch_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    merchant_key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    category TEXT PRIMARY KEY,
    monthly_limit_cents INTEGER NOT NULL CHECK (monthly_limit_cents >= 0)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1))
);

CREATE TABLE IF NOT EXISTS daily_runs (
    day TEXT PRIMARY KEY,
    count INTEGER NOT NULL CHECK (count >= 0)
);

CREATE TABLE IF NOT EXISTS upload_batches (
    batch_id TEXT PRIMARY KEY,
    statement_total_cents INTEGER
);

CREATE TABLE IF NOT EXISTS upload_files (
    batch_id TEXT PRIMARY KEY,
    mime TEXT NOT NULL,
    image BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_months (
    category TEXT NOT NULL,
    month TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    PRIMARY KEY (category, month)
);

CREATE TABLE IF NOT EXISTS recurring_charges (
    id INTEGER PRIMARY KEY,
    merchant TEXT NOT NULL,
    category TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0)
);

CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_batch ON transactions(batch_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
"""


def default_db_path() -> Path:
    """Database file for this repo. SPENDSMART_DB overrides it for tests."""
    override = os.getenv("SPENDSMART_DB", "").strip()
    if override:
        return Path(override)
    return REPO_ROOT / "data" / "spendsmart.db"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _allow_negative_amounts(conn: sqlite3.Connection) -> None:
    """Rebuild an older transactions table that rejected negative cents."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
    ).fetchone()
    if row is None or not row[0] or "amount_cents >= 0" not in str(row[0]):
        return
    conn.executescript(
        """
        CREATE TABLE transactions_new (
            id INTEGER PRIMARY KEY,
            date TEXT,
            merchant TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            category TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('manual', 'pdf', 'image', 'recurring')),
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            status TEXT NOT NULL CHECK (status IN ('confirmed', 'review', 'expected', 'cancelled')),
            review_reason TEXT,
            batch_id TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO transactions_new (
            id, date, merchant, amount_cents, category, source, confidence,
            status, review_reason, batch_id, created_at
        )
        SELECT
            id, date, merchant, amount_cents, category, source, confidence,
            status, review_reason, batch_id, created_at
        FROM transactions;
        DROP TABLE transactions;
        ALTER TABLE transactions_new RENAME TO transactions;
        CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
        CREATE INDEX IF NOT EXISTS idx_transactions_batch ON transactions(batch_id);
        CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
        """
    )


def init_db(db_path: Path | None = None) -> Path:
    """Create tables and seed profile settings once. Safe to call again."""
    path = (db_path or default_db_path()).resolve()
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        _allow_negative_amounts(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (
                (PROFILE_KEY, "student"),
                (MONTHS_KEY, "[]"),
                (CUSTOM_KEY, "[]"),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return path


def read_settings(db_path: Path) -> dict[str, str]:
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    finally:
        conn.close()
    return {str(row["key"]): str(row["value"]) for row in rows}


def update_settings(
    db_path: Path,
    values: dict[str, str],
    delete_keys: tuple[str, ...] = (),
) -> None:
    """Write known settings keys. User text is bound as parameters."""
    unknown = (set(values) | set(delete_keys)) - ALLOWED_SETTINGS
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    conn = connect(db_path)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            list(values.items()),
        )
        if delete_keys:
            conn.executemany(
                "DELETE FROM settings WHERE key = ?",
                [(key,) for key in delete_keys],
            )
        conn.commit()
    finally:
        conn.close()


def utc_day(now: datetime | None = None) -> str:
    """YYYY-MM-DD in UTC. The daily run cap uses this day."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


class DailyRunCapReached(Exception):
    """The UTC day already has the maximum number of model runs."""


def reserve_daily_run(db_path: Path, *, limit: int, day: str | None = None) -> int:
    """Count one run if the day is still under the cap.

    A rejected call does not increment the counter. The check and the
    increment happen in one transaction.
    """
    if limit < 1:
        raise ValueError("daily run limit must be at least 1")
    run_day = day or utc_day()
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO daily_runs(day, count) VALUES (?, 0) ON CONFLICT(day) DO NOTHING",
            (run_day,),
        )
        updated = conn.execute(
            "UPDATE daily_runs SET count = count + 1 WHERE day = ? AND count < ?",
            (run_day, limit),
        )
        if updated.rowcount != 1:
            conn.execute("ROLLBACK")
            raise DailyRunCapReached
        run_count = int(
            conn.execute(
                "SELECT count FROM daily_runs WHERE day = ?",
                (run_day,),
            ).fetchone()["count"]
        )
        conn.execute("COMMIT")
        return run_count
    except DailyRunCapReached:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def insert_llm_call(
    db_path: Path,
    *,
    purpose: str,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    cost_usd: float,
    ok: int,
) -> None:
    """Store token counts for one model attempt. The prompt is not stored."""
    if ok not in (0, 1):
        raise ValueError("ok must be 0 or 1")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO llm_calls (
                ts, purpose, input_tokens, cached_tokens, output_tokens, cost_usd, ok
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                purpose,
                int(input_tokens),
                int(cached_tokens),
                int(output_tokens),
                float(cost_usd),
                int(ok),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def usage_report(db_path: Path, *, day: str | None = None) -> dict[str, object]:
    """Totals and per-purpose averages from llm_calls, plus today's run count."""
    run_day = day or utc_day()
    conn = connect(db_path)
    try:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS calls,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost_usd), 0) AS cost_usd
            FROM llm_calls
            """
        ).fetchone()
        purposes = conn.execute(
            """
            SELECT
                purpose,
                COUNT(*) AS calls,
                AVG(input_tokens) AS avg_input_tokens,
                AVG(cached_tokens) AS avg_cached_tokens,
                AVG(output_tokens) AS avg_output_tokens,
                AVG(cost_usd) AS avg_cost_usd
            FROM llm_calls
            GROUP BY purpose
            ORDER BY purpose
            """
        ).fetchall()
        runs = conn.execute(
            "SELECT count FROM daily_runs WHERE day = ?",
            (run_day,),
        ).fetchone()
        last = conn.execute(
            """
            SELECT purpose, input_tokens, cached_tokens, output_tokens, cost_usd
            FROM llm_calls
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    return {
        "runs_today": int(runs["count"]) if runs else 0,
        "last": None
        if last is None
        else {
            "purpose": str(last["purpose"]),
            "input_tokens": int(last["input_tokens"]),
            "cached_tokens": int(last["cached_tokens"]),
            "output_tokens": int(last["output_tokens"]),
            "cost_usd": float(last["cost_usd"] or 0),
        },
        "totals": {
            "calls": int(totals["calls"]),
            "input_tokens": int(totals["input_tokens"]),
            "cached_tokens": int(totals["cached_tokens"]),
            "output_tokens": int(totals["output_tokens"]),
            "cost_usd": float(totals["cost_usd"]),
        },
        "by_purpose": [
            {
                "purpose": str(row["purpose"]),
                "calls": int(row["calls"]),
                "avg_input_tokens": float(row["avg_input_tokens"]),
                "avg_cached_tokens": float(row["avg_cached_tokens"]),
                "avg_output_tokens": float(row["avg_output_tokens"]),
                "avg_cost_usd": float(row["avg_cost_usd"] or 0),
            }
            for row in purposes
        ],
    }
