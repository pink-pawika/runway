"""Month lists, edits, review approval, and the total rule."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.ledger import batch_allows_approve_all
from app.main import create_app
from app.totals import counts_toward_total


def _insert(conn: sqlite3.Connection, **overrides: object) -> int:
    row = {
        "date": "2026-10-01",
        "merchant": "Starbucks",
        "amount_cents": 450,
        "category": "Dining out",
        "source": "pdf",
        "confidence": 0.9,
        "status": "confirmed",
        "review_reason": None,
        "batch_id": "batch-a",
        "created_at": "2026-10-24T12:00:00Z",
    }
    row.update(overrides)
    cursor = conn.execute(
        """
        INSERT INTO transactions (
            date, merchant, amount_cents, category, source, confidence,
            status, review_reason, batch_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["date"],
            row["merchant"],
            row["amount_cents"],
            row["category"],
            row["source"],
            row["confidence"],
            row["status"],
            row["review_reason"],
            row["batch_id"],
            row["created_at"],
        ),
    )
    return int(cursor.lastrowid)


class TotalRuleTests(unittest.TestCase):
    def test_confirmed_and_expected_rows_count(self) -> None:
        self.assertTrue(counts_toward_total("confirmed"))
        self.assertTrue(counts_toward_total("expected"))
        self.assertFalse(counts_toward_total("review"))
        self.assertFalse(counts_toward_total("cancelled"))

    def test_approve_all_skips_mismatch_and_injection(self) -> None:
        self.assertTrue(batch_allows_approve_all(["Low confidence category", "Possible duplicate"]))
        self.assertFalse(batch_allows_approve_all(["Total mismatch: extracted $5.00 vs statement $4.00"]))
        self.assertFalse(
            batch_allows_approve_all(["Low confidence category", "Document contained suspicious instructions"])
        )


class LedgerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spendsmart.db"
        init_db(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self) -> dict[str, int]:
        conn = connect(self.path)
        try:
            ids = {
                "spent": _insert(conn, amount_cents=450),
                "review": _insert(
                    conn,
                    merchant="Cafe Luna",
                    amount_cents=1200,
                    status="review",
                    review_reason="Low confidence category",
                    batch_id="batch-safe",
                ),
                "partner": _insert(
                    conn,
                    merchant="Yale Bookstore",
                    amount_cents=300,
                    category="Other",
                    status="review",
                    review_reason="Possible duplicate",
                    batch_id="batch-safe",
                ),
                "mismatch": _insert(
                    conn,
                    merchant="Rent Office",
                    amount_cents=5000,
                    status="review",
                    review_reason="Total mismatch: extracted $80.00 vs statement $50.00",
                    batch_id="batch-bad",
                ),
                "undated": _insert(
                    conn,
                    date=None,
                    merchant="Corner Market",
                    amount_cents=800,
                    created_at="2026-10-03T15:00:00Z",
                ),
                "september": _insert(conn, date="2026-09-02", amount_cents=999, created_at="2026-10-03T15:00:00Z"),
            }
            conn.commit()
            return ids
        finally:
            conn.close()

    def _rule(self, key: str) -> str | None:
        conn = connect(self.path)
        try:
            row = conn.execute(
                "SELECT category FROM merchant_rules WHERE merchant_key = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else str(row["category"])

    def test_month_total_skips_review_and_uses_upload_date(self) -> None:
        self._seed()
        conn = connect(self.path)
        try:
            _insert(conn, merchant="Paycheck", amount_cents=-200000)
            conn.commit()
        finally:
            conn.close()
        with TestClient(create_app(self.path)) as client:
            listed = client.get("/api/transactions", params={"month": "2026-10"})
        self.assertEqual(listed.status_code, 200, listed.text)
        body = listed.json()
        merchants = [row["merchant"] for row in body["transactions"]]
        self.assertIn("Corner Market", merchants)
        self.assertNotIn("Starbucks", [row["merchant"] for row in body["transactions"] if row["date"] == "2026-09-02"])
        self.assertEqual(body["confirmed_total_cents"], 450 + 800)
        self.assertEqual(body["held_count"], 3)
        self.assertNotIn("Paycheck", merchants)
        assumed = next(row for row in body["transactions"] if row["merchant"] == "Corner Market")
        self.assertTrue(assumed["date_assumed"])
        self.assertIsNone(assumed["date"])

    def test_category_edit_saves_a_rule_and_amount_edit_does_not(self) -> None:
        ids = self._seed()
        with TestClient(create_app(self.path)) as client:
            amount = client.patch(
                f"/api/transactions/{ids['spent']}",
                json={"amount_cents": -450},
            )
            self.assertEqual(amount.status_code, 200, amount.text)
            self.assertEqual(amount.json()["amount_cents"], -450)
            self.assertIsNone(self._rule("starbucks"))
            changed = client.patch(
                f"/api/transactions/{ids['spent']}",
                json={"category": "Groceries"},
            )
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(self._rule("starbucks"), "Groceries")
            rejected = client.patch(
                f"/api/transactions/{ids['spent']}",
                json={"amount_cents": 0},
            )
            self.assertEqual(rejected.status_code, 422)
            unknown = client.patch(
                f"/api/transactions/{ids['spent']}",
                json={"category": "Not a category"},
            )
            self.assertEqual(unknown.status_code, 400)
            cleared = client.patch(
                f"/api/transactions/{ids['undated']}",
                json={"date": None},
            )
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertTrue(cleared.json()["date_assumed"])

    def test_approve_saves_a_rule_and_blocks_a_bad_batch(self) -> None:
        ids = self._seed()
        with TestClient(create_app(self.path)) as client:
            queued = client.get("/api/review")
            self.assertEqual(queued.status_code, 200, queued.text)
            body = queued.json()
            self.assertEqual(body["count"], 3)
            flags = {batch["batch_id"]: batch["can_approve_all"] for batch in body["batches"]}
            self.assertTrue(flags["batch-safe"])
            self.assertFalse(flags["batch-bad"])
            approved = client.post(f"/api/review/{ids['review']}/approve")
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.json()["status"], "confirmed")
            self.assertIsNone(approved.json()["review_reason"])
            self.assertEqual(self._rule("cafe luna"), "Dining out")
            again = client.post(f"/api/review/{ids['review']}/approve")
            self.assertEqual(again.status_code, 409)
            removed = client.delete(f"/api/transactions/{ids['partner']}")
            self.assertEqual(removed.status_code, 204)
            missing = client.delete(f"/api/transactions/{ids['partner']}")
            self.assertEqual(missing.status_code, 404)

    def test_home_page_is_served_without_html_injection_helpers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "static" / "app.js").read_text(encoding="utf-8")
        for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
            self.assertNotIn(banned, script)
        with TestClient(create_app(self.path)) as client:
            page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("RUNWAY", page.text)
        self.assertIn("how long will my money last?", page.text)
        self.assertIn("Activity", page.text)
        self.assertIn("Analytics", page.text)
        self.assertIn("I-20", page.text)


if __name__ == "__main__":
    unittest.main()
