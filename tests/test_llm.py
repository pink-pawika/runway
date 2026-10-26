"""The model wrapper reserves a run, redacts text, and stores counts only."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import init_db, reserve_daily_run
from app.llm import (
    MAX_RUNS_PER_DAY,
    UNTRUSTED_PREAMBLE,
    BudgetExceeded,
    ExtractionFailed,
    TokenUse,
    _run_model,
    call_llm,
    token_cost,
)
from app.main import create_app
from app.safety import UNTRUSTED_PREAMBLE as SAFETY_PREAMBLE


class CostTests(unittest.TestCase):
    def test_cached_tokens_are_not_billed_twice(self) -> None:
        cost = token_cost(
            1000,
            200,
            500,
            input_rate=3,
            cached_rate=1,
            output_rate=6,
        )
        self.assertEqual(cost, 5.6)

    def test_lecture_rates_price_a_known_call(self) -> None:
        # 1,000 input tokens at $0.10 per 1M, with no cached or output tokens.
        self.assertEqual(token_cost(1000, 0, 0), 0.0001)
        self.assertEqual(token_cost(0, 0, 1000), 0.0005)


class WrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spendsmart.db"
        init_db(self.path)
        self._original = _run_model
        self.seen: list[tuple[str, object]] = []

    def tearDown(self) -> None:
        import app.llm as llm

        llm._run_model = self._original
        self._tmp.cleanup()

    def _install(self, runner) -> None:
        import app.llm as llm

        llm._run_model = runner

    def test_daily_cap_stops_at_the_limit_without_counting_the_blocked_call(self) -> None:
        for _ in range(MAX_RUNS_PER_DAY):
            reserve_daily_run(self.path, limit=MAX_RUNS_PER_DAY)
        with self.assertRaises(BudgetExceeded):
            import asyncio

            asyncio.run(
                call_llm(
                    "ping",
                    "Set ok to true.",
                    user_text="hello",
                    json_schema={
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                    db_path=self.path,
                )
            )
        report = __import__("app.db", fromlist=["usage_report"]).usage_report(self.path)
        self.assertEqual(report["runs_today"], MAX_RUNS_PER_DAY)

    def test_redacts_text_adds_preamble_and_does_not_store_the_document(self) -> None:
        async def runner(system, user_content, output_model):
            self.seen.append((system, user_content))
            return {"ok": True, "injection_suspected": False}, TokenUse(10, 2, 4)

        self._install(runner)
        import asyncio

        result = asyncio.run(
            call_llm(
                "extract",
                "Read the slip.",
                user_text="email secret@yale.edu card 4111111111111111",
                json_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "ok": {"type": "boolean"},
                        "injection_suspected": {"type": "boolean"},
                    },
                    "required": ["ok", "injection_suspected"],
                },
                db_path=self.path,
            )
        )
        system, sent = self.seen[0]
        self.assertTrue(system.startswith(UNTRUSTED_PREAMBLE))
        self.assertIn("Read the slip.", system)
        self.assertIn("[REDACTED-EMAIL]", sent)
        self.assertIn("****1111", sent)
        self.assertNotIn("secret@yale.edu", sent)
        self.assertEqual(result["data"]["ok"], True)
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertEqual(result["usage"]["cached_tokens"], 2)
        raw = self.path.read_bytes()
        self.assertNotIn(b"secret@yale.edu", raw)
        self.assertNotIn(b"4111111111111111", raw)
        self.assertNotIn(b"[REDACTED-EMAIL]", raw)

    def test_invalid_json_retries_once_then_fails(self) -> None:
        calls = {"n": 0}

        async def runner(system, user_content, output_model):
            calls["n"] += 1
            return {"ok": "nope", "card": "4111111111111111"}, TokenUse(3, 0, 1)

        self._install(runner)
        import asyncio

        with self.assertRaises(ExtractionFailed) as caught:
            asyncio.run(
                call_llm(
                    "extract",
                    "",
                    user_text="a slip",
                    json_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                    db_path=self.path,
                )
            )
        self.assertEqual(calls["n"], 2)
        self.assertNotIn("4111111111111111", str(caught.exception))
        report = __import__("app.db", fromlist=["usage_report"]).usage_report(self.path)
        self.assertEqual(report["totals"]["calls"], 2)
        self.assertEqual(report["runs_today"], 1)

    def test_usage_endpoint_averages_each_purpose(self) -> None:
        from app.db import insert_llm_call

        insert_llm_call(
            self.path,
            purpose="extract",
            input_tokens=10,
            cached_tokens=0,
            output_tokens=4,
            cost_usd=0,
            ok=1,
        )
        insert_llm_call(
            self.path,
            purpose="extract",
            input_tokens=30,
            cached_tokens=10,
            output_tokens=6,
            cost_usd=0,
            ok=0,
        )
        insert_llm_call(
            self.path,
            purpose="ping",
            input_tokens=5,
            cached_tokens=0,
            output_tokens=2,
            cost_usd=0,
            ok=1,
        )
        app = create_app(self.path)
        with TestClient(app) as client:
            body = client.get("/api/usage").json()
        self.assertEqual(body["max_runs_per_day"], MAX_RUNS_PER_DAY)
        self.assertEqual(body["totals"]["calls"], 3)
        self.assertEqual(body["totals"]["input_tokens"], 45)
        extract = next(row for row in body["by_purpose"] if row["purpose"] == "extract")
        self.assertEqual(extract["calls"], 2)
        self.assertEqual(extract["avg_input_tokens"], 20)
        self.assertEqual(extract["avg_cached_tokens"], 5)

    def test_unexpected_errors_stay_friendly(self) -> None:
        app = create_app(self.path)

        @app.get("/api/boom")
        def boom() -> None:
            raise RuntimeError('Traceback (most recent call last):\n  File "app/main.py", line 1')

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/boom")
            ready = client.get("/api/ready")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Something went wrong. Try again.")
        self.assertNotIn("Traceback", response.text)
        self.assertEqual(ready.status_code, 200)
        self.assertIn("model_ready", ready.json())

    def test_budget_error_is_http_429(self) -> None:
        app = create_app(self.path)

        @app.get("/api/test-budget")
        def over_budget() -> None:
            raise BudgetExceeded()

        with TestClient(app) as client:
            response = client.get("/api/test-budget")
        self.assertEqual(response.status_code, 429)
        self.assertIn("40 runs", response.json()["detail"])

    def test_preamble_constant_matches_the_safety_module(self) -> None:
        self.assertIn("injection_suspected=true", SAFETY_PREAMBLE)
        self.assertEqual(UNTRUSTED_PREAMBLE, SAFETY_PREAMBLE)


if __name__ == "__main__":
    unittest.main()
