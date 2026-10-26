"""Accounts keep a separate ledger. Passwords are stored only as a hash."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.legacy = root / "legacy.db"
        init_db(self.legacy)
        conn = connect(self.legacy)
        try:
            conn.execute(
                """
                INSERT INTO transactions (
                    date, merchant, amount_cents, category, source, confidence,
                    status, review_reason, batch_id, created_at
                ) VALUES ('2026-10-03', 'Campus cafe', 450, 'Dining out', 'manual', 1, 'confirmed', NULL, NULL, '2026-10-03T12:00:00Z')
                """
            )
            conn.commit()
        finally:
            conn.close()
        self.app = create_app(
            require_login=True,
            auth_dir=root / "auth",
            legacy_db=self.legacy,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_account_keeps_existing_spending_and_the_next_starts_empty(self) -> None:
        with TestClient(self.app) as client:
            blocked = client.get("/api/transactions", params={"month": "2026-10"})
            self.assertEqual(blocked.status_code, 401)
            created = client.post(
                "/api/auth/register",
                json={"email": "A@Example.com", "password": "runway-pass"},
            )
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["email"], "a@example.com")
            mine = client.get("/api/transactions", params={"month": "2026-10"})
            self.assertEqual(mine.status_code, 200, mine.text)
            self.assertEqual([row["merchant"] for row in mine.json()["transactions"]], ["Campus cafe"])
            short = client.post(
                "/api/auth/register",
                json={"email": "short@example.com", "password": "tiny"},
            )
            self.assertEqual(short.status_code, 422)
            duplicate = client.post(
                "/api/auth/register",
                json={"email": "a@example.com", "password": "runway-pass"},
            )
            self.assertEqual(duplicate.status_code, 409)
            client.post("/api/auth/logout")
            self.assertEqual(client.get("/api/auth/me").status_code, 401)
            wrong = client.post(
                "/api/auth/login",
                json={"email": "a@example.com", "password": "wrong-pass"},
            )
            self.assertEqual(wrong.status_code, 401)
            again = client.post(
                "/api/auth/login",
                json={"email": "a@example.com", "password": "runway-pass"},
            )
            self.assertEqual(again.status_code, 200, again.text)
            self.assertEqual(
                client.get("/api/transactions", params={"month": "2026-10"}).json()["transactions"][0]["merchant"],
                "Campus cafe",
            )
            client.post("/api/auth/logout")
            other = client.post(
                "/api/auth/register",
                json={"email": "b@example.com", "password": "another-pass"},
            )
            self.assertEqual(other.status_code, 200, other.text)
            theirs = client.get("/api/transactions", params={"month": "2026-10"})
            self.assertEqual(theirs.json()["transactions"], [])
        auth_file = Path(self._tmp.name) / "auth" / "auth.db"
        conn = sqlite3.connect(auth_file)
        try:
            stored = conn.execute("SELECT password_hash FROM users").fetchall()
        finally:
            conn.close()
        self.assertTrue(stored)
        self.assertNotIn("runway-pass", " ".join(str(row[0]) for row in stored))


if __name__ == "__main__":
    unittest.main()
