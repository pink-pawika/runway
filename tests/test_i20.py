"""I-20 planning math. The model is mocked; the sample file is fake."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.extract import PreparedUpload
from app.i20 import (
    ADVICE_NOTE,
    I20_INSTRUCTIONS,
    I20_SCHEMA,
    apply_i20,
    build_plan,
    default_percents,
    extract_i20,
    save_recurring_rent,
    spending_left,
    validate_split,
)
from app.ledger import list_month
from app.llm import ExtractionFailed
from app.main import create_app
from app.summary import monthly_summary

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data" / "i20_financial.txt"


def _parsed(**overrides: object) -> dict:
    data = {
        "injection_suspected": False,
        "tuition_and_fees_cents": 5_200_000,
        "living_expenses_cents": 1_800_000,
        "dependent_expenses_cents": 0,
        "other_expenses_cents": 120_000,
        "funding_sources": [
            {"name": "On-campus employment", "amount_cents": 300_000},
            {"name": "Family funds", "amount_cents": 6_820_000},
        ],
        "confidence": 0.95,
    }
    data.update(overrides)
    return {
        "data": data,
        "usage": {"input_tokens": 10, "cached_tokens": 0, "output_tokens": 4, "cost_usd": 0.0},
    }


class SampleTests(unittest.TestCase):
    def test_sample_has_financial_lines_and_no_real_identity(self) -> None:
        text = SAMPLE.read_text(encoding="utf-8")
        self.assertIn("not a real I-20", text)
        self.assertIn("Tuition and fees: $52,000", text)
        self.assertIn("Living expenses: $18,000", text)
        self.assertIn("Avery Sample", text)
        self.assertNotIn("name", I20_SCHEMA["required"])
        self.assertNotIn("sevis", I20_SCHEMA["required"])
        self.assertIn("never return the student name", I20_INSTRUCTIONS)
        self.assertIn("Do not give advice about visas, work authorization, or OPT.", I20_INSTRUCTIONS)


class PlanTests(unittest.TestCase):
    def test_nine_months_splits_living_and_keeps_tuition_as_a_lump(self) -> None:
        plan = build_plan(
            living_expenses_cents=1_800_000,
            tuition_and_fees_cents=5_200_000,
            months=9,
            tuition_months=["2026-09", "2027-01"],
        )
        self.assertEqual(plan["monthly_living_cents"], 200_000)
        self.assertEqual(plan["semester_tuition_cents"], 2_600_000)
        monthly = {line["category"]: line["amount_cents"] for line in plan["lines"] if line["cadence"] == "monthly"}
        self.assertEqual(sum(monthly.values()), 200_000)
        self.assertEqual(monthly["Rent & utilities"], 90_000)
        self.assertEqual(monthly["Groceries"], 40_000)
        lumps = [line for line in plan["lines"] if line["cadence"] == "lump"]
        self.assertEqual([line["month"] for line in lumps], ["2026-09", "2027-01"])
        self.assertEqual({line["amount_cents"] for line in lumps}, {2_600_000})
        self.assertEqual(plan["note"], ADVICE_NOTE)

    def test_twelve_months_uses_a_smaller_monthly_living_amount(self) -> None:
        plan = build_plan(
            living_expenses_cents=1_800_000,
            tuition_and_fees_cents=5_200_000,
            months=12,
            tuition_months=[],
        )
        self.assertEqual(plan["monthly_living_cents"], 150_000)
        self.assertEqual(sum(line["amount_cents"] for line in plan["lines"]), 150_000)
        self.assertTrue(all(line["cadence"] == "monthly" for line in plan["lines"]))

    def test_bad_split_falls_back_to_the_default(self) -> None:
        self.assertIsNone(validate_split([{"category": "Groceries", "percent": 100}]))
        self.assertIsNone(
            validate_split(
                [{"category": name, "percent": 10} for name, _percent in default_percents()]
            )
        )
        plan = build_plan(
            living_expenses_cents=1_800_000,
            tuition_and_fees_cents=0,
            months=9,
            tuition_months=[],
            percents=[{"category": "Groceries", "percent": 100}],
        )
        self.assertTrue(plan["used_default_split"])
        rent = next(line for line in plan["lines"] if line["category"] == "Rent & utilities")
        self.assertEqual(rent["amount_cents"], 90_000)


class ExtractTests(unittest.TestCase):
    def test_low_confidence_and_injection_keep_numbers_behind_a_banner(self) -> None:
        calls = AsyncMock(return_value=_parsed(confidence=0.4))
        with (
            patch("app.i20.prepare_upload", return_value=PreparedUpload("pdf", "costs", (), 0)),
            patch("app.i20.call_llm", new=calls),
        ):
            low = asyncio.run(extract_i20(b"bytes", None))
        self.assertEqual(calls.await_args.args[0], "i20_extract")
        self.assertIn("uncertain", low["banner"])
        self.assertEqual(low["values"]["tuition_and_fees_cents"], 5_200_000)
        self.assertNotIn("name", low["values"])
        calls.return_value = _parsed(injection_suspected=True)
        with (
            patch("app.i20.prepare_upload", return_value=PreparedUpload("pdf", "costs", (), 0)),
            patch("app.i20.call_llm", new=calls),
        ):
            flagged = asyncio.run(extract_i20(b"bytes", None))
        self.assertIn("instructions", flagged["banner"])


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spendsmart.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_apply_writes_monthly_limits_and_tuition_only_on_chosen_months(self) -> None:
        plan = build_plan(
            living_expenses_cents=1_800_000,
            tuition_and_fees_cents=5_200_000,
            months=9,
            tuition_months=["2026-09"],
        )
        monthly = [
            {"category": line["category"], "amount_cents": line["amount_cents"]}
            for line in plan["lines"]
            if line["cadence"] == "monthly"
        ]
        lumps = [
            {"month": line["month"], "amount_cents": line["amount_cents"]}
            for line in plan["lines"]
            if line["cadence"] == "lump"
        ]
        apply_i20(self.path, monthly, lumps)
        september = monthly_summary(self.path, "2026-09")
        october = monthly_summary(self.path, "2026-10")
        sept_tuition = next(row for row in september["categories"] if row["category"] == "Tuition & fees")
        oct_tuition = next(row for row in october["categories"] if row["category"] == "Tuition & fees")
        self.assertEqual(sept_tuition["limit_cents"], 2_600_000)
        self.assertIsNone(oct_tuition["limit_cents"])
        rent = next(row for row in october["categories"] if row["category"] == "Rent & utilities")
        self.assertEqual(rent["limit_cents"], 90_000)

    def test_split_route_falls_back_when_the_model_does_not_sum_to_100(self) -> None:
        app = create_app(self.path)
        with TestClient(app) as client:
            with patch(
                "app.i20.call_llm",
                new=AsyncMock(side_effect=ExtractionFailed("no")),
            ):
                response = client.post("/api/i20/split", json={"note": "I cook at home and rent is high."})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["used_default"])
        self.assertEqual(body["percents"], default_percents())

    def test_rent_is_recurring_and_the_rest_is_the_spending_budget(self) -> None:
        self.assertEqual(spending_left(200_000, 90_000), 110_000)
        self.assertEqual(spending_left(200_000, 250_000), 0)
        saved = save_recurring_rent(self.path, 200_000, 90_000)
        self.assertEqual(saved["spending_cents"], 110_000)
        again = save_recurring_rent(self.path, 200_000, 80_000)
        self.assertEqual(again["rent_cents"], 80_000)
        month = list_month(self.path, "2026-10")
        self.assertEqual(month["confirmed_total_cents"], 0)
        self.assertEqual(
            [(row["merchant"], row["category"], row["amount_cents"]) for row in month["recurring"]],
            [("Rent", "Rent & utilities", 80_000)],
        )
        summary = monthly_summary(self.path, "2026-10")
        self.assertEqual(summary["budget_cents"], 120_000)
        self.assertEqual(summary["spent_cents"], 80_000)
        rent_spent = next(row for row in summary["categories"] if row["category"] == "Rent & utilities")
        self.assertEqual(rent_spent["spent_cents"], 80_000)
        with TestClient(create_app(self.path)) as client:
            listed = client.get("/api/transactions", params={"month": "2026-10"})
            charge_id = listed.json()["recurring"][0]["id"]
            edited = client.patch(
                f"/api/recurring/{charge_id}",
                json={"merchant": "Apartment rent", "amount_cents": 100_000},
            )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["merchant"], "Apartment rent")
        self.assertEqual(monthly_summary(self.path, "2026-10")["spent_cents"], 100_000)


if __name__ == "__main__":
    unittest.main()
