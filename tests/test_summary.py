"""Dashboard math: who counts, and which budget state that produces."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app
from app.summary import budget_state, monthly_summary


def _insert(conn: sqlite3.Connection, **overrides: object) -> None:
    row = {
        "date": "2026-10-01",
        "merchant": "Cafe Luna",
        "amount_cents": 450,
        "category": "Dining out",
        "source": "manual",
        "confidence": 1,
        "status": "confirmed",
        "review_reason": None,
        "batch_id": None,
        "created_at": "2026-10-01T12:00:00Z",
    }
    row.update(overrides)
    conn.execute(
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


class StateTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        self.assertEqual(budget_state(699, 1000)["state"], "calm")
        self.assertEqual(budget_state(700, 1000)["state"], "almost")
        self.assertEqual(budget_state(899, 1000)["state"], "almost")
        self.assertEqual(budget_state(900, 1000)["state"], "warning")
        self.assertEqual(budget_state(1000, 1000)["state"], "warning")
        self.assertEqual(budget_state(1000, 1000)["label"], "Almost at limit")
        over = budget_state(1001, 1000)
        self.assertEqual(over["state"], "over")
        self.assertEqual(over["label"], "Over budget by $0.01")
        self.assertEqual(budget_state(500, None)["state"], "no_budget")
        self.assertEqual(budget_state(500, None)["label"], "No budget set")

    def test_labels(self) -> None:
        self.assertEqual(budget_state(100, 1000)["label"], "Well within budget")
        self.assertEqual(budget_state(750, 1000)["label"], "Getting close")


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spendsmart.db"
        init_db(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_expected_counts_and_review_does_not(self) -> None:
        conn = connect(self.path)
        try:
            _insert(conn, amount_cents=450, status="confirmed")
            _insert(conn, amount_cents=50, status="expected", merchant="Rent")
            _insert(conn, amount_cents=999, status="review", merchant="Hidden")
            _insert(conn, amount_cents=80, status="cancelled", merchant="Old")
            _insert(conn, date=None, amount_cents=20, created_at="2026-10-04T12:00:00Z", merchant="Market")
            _insert(conn, date="2026-09-02", amount_cents=5000, merchant="September")
            _insert(conn, amount_cents=-2000, merchant="Paycheck")
            conn.execute(
                "INSERT INTO budgets (category, monthly_limit_cents) VALUES ('Dining out', 1000)"
            )
            conn.commit()
        finally:
            conn.close()
        summary = monthly_summary(self.path, "2026-10")
        dining = next(row for row in summary["categories"] if row["category"] == "Dining out")
        self.assertEqual(dining["spent_cents"], 450 + 50 + 20)
        self.assertEqual(dining["state"], "calm")
        self.assertEqual(summary["spent_cents"], 520)
        self.assertEqual(summary["budget_cents"], 1000)
        self.assertEqual(summary["state"], "calm")
        september = next(point for point in summary["trend"] if point["month"] == "2026-09")
        self.assertEqual(september["spent_cents"], 5000)
        self.assertEqual(len(summary["trend"]), 6)
        self.assertEqual(summary["trend"][-1]["month"], "2026-10")
        self.assertEqual(summary["trend"][-1]["spent_cents"], 520)
        self.assertLessEqual(len(summary["months"]), 6)
        self.assertIn("2026-10", summary["months"])
        self.assertIn("2026-09", summary["months"])
        self.assertIn("You spent $5.20 of a $10.00 budget in October.", summary["blurb"])
        self.assertIn("Dining out is the largest part", summary["blurb"])

    def test_monthly_sum_overrides_category_limits(self) -> None:
        conn = connect(self.path)
        try:
            _insert(conn, amount_cents=450, status="confirmed")
            conn.execute(
                "INSERT INTO budgets (category, monthly_limit_cents) VALUES ('Dining out', 1000)"
            )
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('monthly_limit_cents', '20000')"
            )
            conn.commit()
        finally:
            conn.close()
        summary = monthly_summary(self.path, "2026-10")
        self.assertEqual(summary["budget_cents"], 20000)
        self.assertIn("of a $200.00 budget", summary["blurb"])

    def test_budget_put_rejects_negative_and_clears_with_null(self) -> None:
        with TestClient(create_app(self.path)) as client:
            saved = client.put(
                "/api/budgets",
                json={"limits": [{"category": "Dining out", "monthly_limit_cents": 0}]},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            dining = next(row for row in saved.json()["categories"] if row["category"] == "Dining out")
            self.assertEqual(dining["monthly_limit_cents"], 0)
            rejected = client.put(
                "/api/budgets",
                json={"limits": [{"category": "Dining out", "monthly_limit_cents": -1}]},
            )
            self.assertEqual(rejected.status_code, 422)
            unknown = client.put(
                "/api/budgets",
                json={"limits": [{"category": "Not real", "monthly_limit_cents": 100}]},
            )
            self.assertEqual(unknown.status_code, 400)
            cleared = client.put(
                "/api/budgets",
                json={"limits": [{"category": "Dining out", "monthly_limit_cents": None}]},
            )
            self.assertEqual(cleared.status_code, 200, cleared.text)
            dining = next(row for row in cleared.json()["categories"] if row["category"] == "Dining out")
            self.assertIsNone(dining["monthly_limit_cents"])


if __name__ == "__main__":
    unittest.main()
