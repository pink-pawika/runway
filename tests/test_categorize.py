"""Category assignment from saved rules and one mocked model call."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.categorize import categorize_batch
from app.db import connect, init_db
from app.extract import PreparedUpload, _save_extracted, extract_upload
from app.safety import UNTRUSTED_PREAMBLE


def _parsed(items: list[dict], total: int | None = None) -> dict:
    return {
        "data": {
            "injection_suspected": False,
            "statement_total_cents": total,
            "transactions": items,
        },
        "usage": {
            "input_tokens": 10,
            "cached_tokens": 0,
            "output_tokens": 4,
            "cost_usd": 0.0,
        },
    }


def _png() -> bytes:
    image = Image.new("RGB", (20, 20), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _usage(tokens: int = 7) -> dict:
    return {
        "input_tokens": tokens,
        "cached_tokens": 0,
        "output_tokens": 3,
        "cost_usd": 0.0,
    }


class CategorizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spendsmart.db"
        init_db(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add_rule(self, key: str, category: str) -> None:
        conn = connect(self.path)
        try:
            conn.execute(
                """
                INSERT INTO merchant_rules (merchant_key, category, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, category, "2026-10-25T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    def _rule_count(self) -> int:
        conn = connect(self.path)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM merchant_rules").fetchone()[0])
        finally:
            conn.close()

    def test_known_merchant_makes_zero_model_calls(self) -> None:
        self._add_rule("starbucks", "Dining out")
        mocked = AsyncMock()
        with patch("app.categorize.call_llm", new=mocked):
            assigned, usage = asyncio.run(
                categorize_batch(
                    ["Starbucks Store 55", "STARBUCKS #1234"],
                    self.path,
                )
            )
        mocked.assert_not_called()
        self.assertIsNone(usage)
        self.assertEqual(assigned, {"starbucks": ("Dining out", 1.0)})
        self.assertEqual(self._rule_count(), 1)

    def test_unknown_batch_makes_exactly_one_call(self) -> None:
        self._add_rule("starbucks", "Dining out")
        seen: list[tuple[str, str, str]] = []

        async def fake_call(purpose, system, user_text=None, **_kwargs):
            seen.append((purpose, system, user_text or ""))
            return {
                "data": {
                    "assignments": [
                        {
                            "merchant_key": "cafe luna",
                            "category": "Dining out",
                            "confidence": 0.91,
                        },
                        {
                            "merchant_key": "yale bookstore",
                            "category": "Books & supplies",
                            "confidence": 0.8,
                        },
                    ]
                },
                "usage": _usage(),
            }

        with patch("app.categorize.call_llm", new=AsyncMock(side_effect=fake_call)):
            assigned, usage = asyncio.run(
                categorize_batch(
                    ["Cafe Luna", "Starbucks Store 55", "Cafe Luna", "Yale Bookstore"],
                    self.path,
                )
            )
        self.assertEqual(len(seen), 1)
        purpose, system, user_text = seen[0]
        self.assertEqual(purpose, "categorize")
        self.assertTrue(system.startswith(UNTRUSTED_PREAMBLE))
        self.assertEqual(user_text.count("merchant_key: cafe luna"), 1)
        self.assertEqual(user_text.count("merchant_key: yale bookstore"), 1)
        self.assertNotIn("starbucks", user_text)
        self.assertEqual(assigned["starbucks"], ("Dining out", 1.0))
        self.assertEqual(assigned["cafe luna"], ("Dining out", 0.91))
        self.assertEqual(assigned["yale bookstore"], ("Books & supplies", 0.8))
        self.assertEqual(usage["input_tokens"], 7)
        self.assertEqual(self._rule_count(), 1)

    def test_invalid_or_missing_category_becomes_other(self) -> None:
        async def fake_call(*_args, **_kwargs):
            return {
                "data": {
                    "assignments": [
                        {
                            "merchant_key": "cafe luna",
                            "category": "Free money",
                            "confidence": 0.99,
                        }
                    ]
                },
                "usage": _usage(),
            }

        with patch("app.categorize.call_llm", new=AsyncMock(side_effect=fake_call)):
            assigned, _usage_info = asyncio.run(
                categorize_batch(["Cafe Luna", "Yale Bookstore"], self.path)
            )
        self.assertEqual(assigned["cafe luna"], ("Other", 0.0))
        self.assertEqual(assigned["yale bookstore"], ("Other", 0.0))
        self.assertEqual(self._rule_count(), 0)

    def test_category_does_not_clear_an_earlier_review_status(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        prepared = PreparedUpload("pdf", "statement", (), 0)
        result = _save_extracted(
            prepared,
            _parsed(
                [
                    {
                        "date": today,
                        "merchant": "Cafe Luna",
                        "amount_cents": 450,
                        "confidence": 0.4,
                    },
                    {
                        "date": today,
                        "merchant": "Payroll",
                        "amount_cents": 100,
                        "confidence": 0.4,
                    },
                ],
                total=450,
            ),
            self.path,
            {
                "cafe luna": ("Dining out", 1.0),
                "payroll": ("Income", 0.69),
            },
        )
        sure, unsure = result["transactions"]
        self.assertEqual(sure["status"], "review")
        self.assertEqual(sure["category"], "Dining out")
        self.assertEqual(sure["confidence"], 1.0)
        self.assertEqual(
            sure["review_reason"],
            "Total mismatch: extracted $5.50 vs statement $4.50",
        )
        self.assertEqual(unsure["status"], "review")
        self.assertEqual(unsure["category"], "Income")
        self.assertEqual(unsure["confidence"], 0.69)
        self.assertIn("Total mismatch", unsure["review_reason"])
        self.assertIn("Low confidence category", unsure["review_reason"])

    def test_upload_skips_the_model_when_every_merchant_is_known(self) -> None:
        self._add_rule("cafe luna", "Dining out")
        extract_calls = AsyncMock(
            return_value=_parsed(
                [
                    {
                        "date": datetime.now(timezone.utc).date().isoformat(),
                        "merchant": "Cafe Luna",
                        "amount_cents": 450,
                        "confidence": 0.4,
                    }
                ],
                total=450,
            )
        )
        categorize_calls = AsyncMock()
        with (
            patch("app.extract.call_llm", new=extract_calls),
            patch("app.categorize.call_llm", new=categorize_calls),
        ):
            saved = asyncio.run(extract_upload(_png(), self.path))
        extract_calls.assert_awaited_once()
        categorize_calls.assert_not_called()
        row = saved["transactions"][0]
        self.assertEqual(row["category"], "Dining out")
        self.assertEqual(row["confidence"], 1.0)
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(saved["usage"]["input_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
