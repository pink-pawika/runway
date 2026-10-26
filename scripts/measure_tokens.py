"""Measure extract and categorize tokens on the sample PDFs.

Each file is uploaded twice against a temporary database. The first pass has
no merchant rules. The second pass uses the categories learned from the first
pass. Extract still runs the second time, because the document is read again.
Categorize is skipped when every merchant already has a rule.

This calls the real model. It does not write to the account database.

    .venv/bin/python scripts/measure_tokens.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.categorize import categorize_batch
from app.db import connect, init_db
from app.extract import extract_upload
from app.model import scrub

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = (
    ROOT / "sample_data" / "injection_receipt.pdf",
    ROOT / "sample_data" / "mismatch_statement.pdf",
)


def _remember(db_path: Path, assigned: dict[str, tuple[str, float]]) -> None:
    if not assigned:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect(init_db(db_path))
    try:
        for key, (category, _confidence) in assigned.items():
            conn.execute(
                """
                INSERT INTO merchant_rules (merchant_key, category, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(merchant_key) DO UPDATE SET
                    category = excluded.category,
                    updated_at = excluded.updated_at
                """,
                (key, category, stamp),
            )
        conn.commit()
    finally:
        conn.close()


async def _pass(db_path: Path, data: bytes, label: str, file_name: str, table: list[tuple]) -> None:
    extracted = await extract_upload(data, db_path)
    names = [str(row["merchant"]) for row in extracted["transactions"] if row.get("merchant")]
    assigned, categorized = await categorize_batch(names, db_path)
    table.append((file_name, label, "extract", extracted["usage"]))
    table.append((file_name, label, "categorize", categorized))
    if label == "empty rules":
        _remember(db_path, assigned)


def _cell(usage: dict | None, key: str) -> str:
    if usage is None:
        return "skipped"
    return str(usage[key])


def _print(table: list[tuple]) -> None:
    header = ("file", "pass", "step", "input", "cached", "output", "cost_usd")
    print(" ".join(f"{name:<16}" for name in header))
    for file_name, label, step, usage in table:
        print(
            " ".join(
                [
                    f"{file_name:<16}",
                    f"{label:<16}",
                    f"{step:<16}",
                    f"{_cell(usage, 'input_tokens'):<16}",
                    f"{_cell(usage, 'cached_tokens'):<16}",
                    f"{_cell(usage, 'output_tokens'):<16}",
                    f"{_cell(usage, 'cost_usd'):<16}",
                ]
            )
        )


async def _measure(db_path: Path) -> None:
    init_db(db_path)
    table: list[tuple] = []
    loaded = [(path.name, path.read_bytes()) for path in SAMPLES]
    for name, data in loaded:
        await _pass(db_path, data, "empty rules", name, table)
    for name, data in loaded:
        await _pass(db_path, data, "rules learned", name, table)
    _print(table)
    print("Costs use the lecture rates in app/llm.py. Skipped means that step made no model call.")


def main() -> int:
    missing = [path.name for path in SAMPLES if not path.is_file()]
    if missing:
        print("Missing sample files: " + ", ".join(missing))
        print("Run scripts/make_sample_pdfs.py first.")
        return 1
    try:
        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(_measure(Path(folder) / "tokens.db"))
    except Exception as exc:
        print(scrub(f"{type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
