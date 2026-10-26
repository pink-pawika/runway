"""Database rules and the settings endpoints."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.categories import BUSINESS, CORE, STUDENT, get_categories
from app.db import init_db
from app.main import create_app


def _insert(conn: sqlite3.Connection, **overrides: object) -> None:
    row = {
        "date": "2026-10-01",
        "merchant": "Starbucks",
        "amount_cents": 450,
        "category": "Dining out",
        "source": "manual",
        "confidence": 1,
        "status": "confirmed",
        "review_reason": None,
        "batch_id": None,
        "created_at": "2026-10-24T12:00:00Z",
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


class CategoryListTests(unittest.TestCase):
    def test_profiles_add_the_right_groups(self) -> None:
        student = get_categories("student")
        personal = get_categories("personal")
        business = get_categories("business")
        self.assertEqual(student, [*CORE, *STUDENT])
        self.assertEqual(personal, list(CORE))
        self.assertEqual(business, [*CORE, *BUSINESS])
        self.assertIn("Flights home", student)
        self.assertNotIn("Payroll", student)
        self.assertIn("Payroll", business)
        self.assertNotIn("Flights home", business)

    def test_custom_names_append_without_repeating_a_built_in(self) -> None:
        names = get_categories("Student", ["Lab fees", "groceries", "Lab fees"])
        self.assertEqual(names[-1], "Lab fees")
        self.assertEqual(names.count("Groceries"), 1)
        self.assertNotIn("groceries", names)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spendsmart.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_init_is_safe_to_call_twice_and_keeps_saved_settings(self) -> None:
        init_db(self.path)
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = ?",
                ("personal", "profile"),
            )
            conn.commit()
        finally:
            conn.close()
        init_db(self.path)
        conn = sqlite3.connect(self.path)
        try:
            profile = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                ("profile",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(profile, "personal")

    def test_amount_and_source_checks(self) -> None:
        init_db(self.path)
        conn = sqlite3.connect(self.path)
        try:
            _insert(conn, merchant="O'Reilly", amount_cents=0, status="review", date=None)
            for status in ("confirmed", "review", "expected", "cancelled"):
                _insert(conn, status=status, merchant=status)
            _insert(conn, amount_cents=-250, merchant="Refund")
            stored = conn.execute(
                "SELECT amount_cents FROM transactions WHERE merchant = ?",
                ("Refund",),
            ).fetchone()[0]
            self.assertEqual(stored, -250)
            with self.assertRaises(sqlite3.IntegrityError):
                _insert(conn, source="email")
        finally:
            conn.close()

    def test_settings_and_categories_endpoints(self) -> None:
        app = create_app(self.path)
        with TestClient(app) as client:
            first = client.get("/api/settings")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(
                first.json(),
                {
                    "profile": "student",
                    "academic_year_months": [],
                    "custom_categories": [],
                    "monthly_limit_cents": None,
                },
            )
            student = client.get("/api/categories")
            self.assertEqual(student.status_code, 200)
            self.assertIn("Tuition & fees", student.json()["categories"])
            self.assertNotIn("Inventory", student.json()["categories"])

            saved = client.put(
                "/api/settings",
                json={
                    "profile": "Business",
                    "academic_year_months": ["2026-08", "2026-09"],
                    "custom_categories": ["Lab fees"],
                    "monthly_limit_cents": 150000,
                },
            )
            self.assertEqual(saved.status_code, 200)
            body = saved.json()
            self.assertEqual(body["profile"], "business")
            self.assertEqual(body["academic_year_months"], ["2026-08", "2026-09"])
            self.assertEqual(body["monthly_limit_cents"], 150000)

            categories = client.get("/api/categories").json()
            self.assertEqual(categories["profile"], "business")
            self.assertIn("Payroll", categories["categories"])
            self.assertIn("Lab fees", categories["categories"])
            self.assertNotIn("Flights home", categories["categories"])

            kept = client.put(
                "/api/settings",
                json={"profile": "personal", "academic_year_months": ["2026-08"]},
            )
            self.assertEqual(kept.status_code, 200)
            self.assertEqual(kept.json()["custom_categories"], ["Lab fees"])
            self.assertEqual(kept.json()["monthly_limit_cents"], 150000)

            cleared = client.put(
                "/api/settings",
                json={
                    "profile": "personal",
                    "academic_year_months": ["2026-08"],
                    "monthly_limit_cents": None,
                },
            )
            self.assertEqual(cleared.status_code, 200)
            self.assertIsNone(cleared.json()["monthly_limit_cents"])

            bad_month = client.put(
                "/api/settings",
                json={"profile": "student", "academic_year_months": ["2026-13"]},
            )
            self.assertEqual(bad_month.status_code, 422)
            negative = client.put(
                "/api/settings",
                json={
                    "profile": "student",
                    "academic_year_months": [],
                    "monthly_limit_cents": -1,
                },
            )
            self.assertEqual(negative.status_code, 422)


if __name__ == "__main__":
    unittest.main()
