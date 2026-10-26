"""Upload checks and the code that decides which rows need review."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from app.extract import (
    EXTRACTION_SCHEMA,
    _save_extracted,
    extract_upload,
    keep_statement_total,
    prepare_upload,
    signed_amount,
)
from app.llm import _model_from_schema
from app.ledger import MissingTransaction
from app.main import create_app
from app.safety import UploadRejected, detect_upload_kind


def _png() -> bytes:
    image = Image.new("RGB", (30, 20), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 12 Tf 40 100 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode())
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode())
    out.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(out)


def _parsed(
    items: list[dict],
    total: int | None = None,
    injection: bool = False,
    store_name: str = "",
) -> dict:
    return {
        "data": {
            "injection_suspected": injection,
            "store_name": store_name,
            "date": None,
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


class FileCheckTests(unittest.TestCase):
    def test_type_comes_from_the_bytes(self) -> None:
        self.assertEqual(detect_upload_kind(_png()), "png")
        self.assertEqual(detect_upload_kind(b"%PDF-1.4\n"), "pdf")
        self.assertEqual(detect_upload_kind(b"\xff\xd8\xff\xe0" + b"jpeg"), "jpeg")
        heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8
        self.assertEqual(detect_upload_kind(heic), "heic")
        with self.assertRaises(UploadRejected):
            detect_upload_kind(b"not an image")

    def test_png_becomes_a_jpeg_without_using_a_filename(self) -> None:
        prepared = prepare_upload(_png())
        self.assertEqual(prepared.source, "image")
        import base64

        raw = base64.b64decode(prepared.images[0])
        self.assertTrue(raw.startswith(b"\xff\xd8\xff"))

    def test_text_pdf_keeps_the_words(self) -> None:
        prepared = prepare_upload(_pdf("Cafe Luna Latte 4.50 on 2026-10-01 for the student"))
        self.assertEqual(prepared.source, "pdf")
        self.assertIn("Cafe Luna", prepared.text or "")
        self.assertEqual(prepared.images, ())


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spendsmart.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _save(self, items, total=None, injection=False):
        from app.extract import PreparedUpload

        prepared = PreparedUpload("pdf", "statement", (), 0)
        return _save_extracted(prepared, _parsed(items, total, injection), self.path)

    def test_total_mismatch_and_negative_income_are_saved(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        result = self._save(
            [
                {
                    "date": today,
                    "merchant": "Cafe Luna",
                    "amount_cents": 450,
                    "confidence": 0.9,
                },
                {
                    "date": today,
                    "merchant": "Payroll",
                    "amount_cents": -2000,
                    "confidence": 0.8,
                },
            ],
            total=450,
        )
        self.assertEqual(result["saved"], 2)
        self.assertEqual(result["review_count"], 2)
        self.assertIn("Total mismatch", result["transactions"][0]["review_reason"])
        self.assertEqual(result["transactions"][1]["amount_cents"], -2000)
        self.assertEqual(result["transactions"][0]["category"], "Other")

    def test_duplicate_future_and_large_amounts_go_to_review(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        future = (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()
        old = (datetime.now(timezone.utc).date() - timedelta(days=400)).isoformat()
        first = self._save(
            [{"date": today, "merchant": "Starbucks Store 55", "amount_cents": 450, "confidence": 1}]
        )
        self.assertEqual(first["transactions"][0]["status"], "confirmed")
        second = self._save(
            [
                {"date": today, "merchant": "STARBUCKS #1234", "amount_cents": 450, "confidence": 1},
                {"date": future, "merchant": "Rent", "amount_cents": 100, "confidence": 1},
                {"date": old, "merchant": "Old Store", "amount_cents": 100, "confidence": 1},
                {"date": today, "merchant": "Big", "amount_cents": 2_000_000, "confidence": 1},
                {"date": today, "merchant": "   ", "amount_cents": 0, "confidence": 1},
            ]
        )
        reasons = [row["review_reason"] for row in second["transactions"]]
        self.assertIn("Possible duplicate", reasons[0])
        self.assertIn("Date is in the future", reasons[1])
        self.assertIn("Date is more than 1 year old", reasons[2])
        self.assertIn("Amount is over $10,000", reasons[3])
        self.assertIn("Missing merchant", reasons[4])
        self.assertIn("Amount is zero", reasons[4])

    def test_too_many_rows_are_rejected_and_injection_is_saved_for_review(self) -> None:
        from app.extract import PreparedUpload

        crowded = [
            {"date": None, "merchant": f"Shop {index}", "amount_cents": 100, "confidence": 0.5}
            for index in range(301)
        ]
        with self.assertRaises(UploadRejected):
            self._save(crowded)
        from app.db import connect, init_db

        conn = connect(init_db(self.path))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
        finally:
            conn.close()
        flagged = self._save(
            [{"date": None, "merchant": "Cafe Luna", "amount_cents": 450, "confidence": 0.4}],
            injection=True,
        )
        self.assertEqual(flagged["transactions"][0]["status"], "review")
        self.assertEqual(
            flagged["transactions"][0]["review_reason"],
            "Document contained suspicious instructions",
        )
        again = self._save(
            [{"date": None, "merchant": "Cafe Luna", "amount_cents": 450, "confidence": 0.4}]
        )
        self.assertIsNone(again["transactions"][0]["review_reason"])

    def test_upload_and_manual_endpoints(self) -> None:
        app = create_app(self.path)
        payload = _parsed(
            [
                {
                    "date": datetime.now(timezone.utc).date().isoformat(),
                    "merchant": "Latte",
                    "amount_cents": 450,
                    "confidence": 0.95,
                },
                {
                    "date": datetime.now(timezone.utc).date().isoformat(),
                    "merchant": "Muffin",
                    "amount_cents": 550,
                    "confidence": 0.95,
                },
            ],
            total=1000,
            store_name="Cafe Luna",
        )

        async def fake_call(*_args, **_kwargs):
            return payload

        async def fake_categories(*_args, **_kwargs):
            return {
                "data": {
                    "assignments": [
                        {
                            "merchant_key": "cafe luna",
                            "category": "Dining out",
                            "confidence": 0.95,
                        }
                    ]
                },
                "usage": {
                    "input_tokens": 0,
                    "cached_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                },
            }

        with TestClient(app) as client:
            with (
                patch("app.extract.call_llm", new=AsyncMock(side_effect=fake_call)),
                patch("app.categorize.call_llm", new=AsyncMock(side_effect=fake_categories)),
            ):
                uploaded = client.post(
                    "/api/upload",
                    files={"file": ("ignored-name.pdf", _pdf("Cafe Luna Latte 4.50 paid on 2026-10-01 by card"), "application/octet-stream")},
                )
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            body = uploaded.json()
            self.assertEqual(body["saved"], 1)
            self.assertEqual(body["statement_total_cents"], 1000)
            self.assertEqual(body["review_count"], 0)
            self.assertEqual(body["transactions"][0]["merchant"], "Cafe Luna")
            self.assertEqual(body["transactions"][0]["amount_cents"], 1000)
            self.assertEqual(body["transactions"][0]["category"], "Other")
            self.assertEqual(body["usage"]["input_tokens"], 10)
            saved = client.post(
                "/api/transactions",
                json={
                    "merchant": "  Yale Bookstore  ",
                    "amount_cents": 2599,
                    "category": "Books & supplies",
                    "date": datetime.now(timezone.utc).date().isoformat(),
                },
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["source"], "manual")
            self.assertEqual(saved.json()["category"], "Books & supplies")
            rejected = client.post(
                "/api/transactions",
                json={"merchant": "X", "amount_cents": -5, "category": "Other"},
            )
            self.assertEqual(rejected.status_code, 422)
            unknown = client.post(
                "/api/upload",
                files={"file": ("notes.txt", b"hello this is plain text", "text/plain")},
            )
            self.assertEqual(unknown.status_code, 400)

    def test_keep_statement_total_replaces_line_items(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        saved = self._save(
            [
                {"date": today, "merchant": "Cafe Luna", "amount_cents": 450, "confidence": 0.9},
                {"date": today, "merchant": "Cafe Luna", "amount_cents": 300, "confidence": 0.9},
            ],
            total=1000,
        )
        self.assertEqual(saved["statement_total_cents"], 1000)
        one = keep_statement_total(self.path, saved["batch_id"])
        self.assertEqual(one["amount_cents"], 1000)
        self.assertEqual(one["merchant"], "Cafe Luna")
        self.assertEqual(one["status"], "confirmed")
        self.assertIsNone(one["review_reason"])
        from app.db import connect, init_db

        conn = connect(init_db(self.path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)
        again = keep_statement_total(self.path, saved["batch_id"])
        self.assertEqual(again["id"], one["id"])

        mixed = self._save(
            [
                {"date": today, "merchant": "Cafe Luna", "amount_cents": 400, "confidence": 0.9},
                {"date": today, "merchant": "Bakery", "amount_cents": 600, "confidence": 0.9},
            ],
            total=1000,
        )
        combined = keep_statement_total(self.path, mixed["batch_id"])
        self.assertEqual(combined["merchant"], "Receipt")
        self.assertEqual(combined["amount_cents"], 1000)

        no_total = self._save(
            [
                {"date": today, "merchant": "Cafe Luna", "amount_cents": 100, "confidence": 0.9},
                {"date": today, "merchant": "Cafe Luna", "amount_cents": 200, "confidence": 0.9},
            ]
        )
        with self.assertRaises(UploadRejected):
            keep_statement_total(self.path, no_total["batch_id"])
        with self.assertRaises(MissingTransaction):
            keep_statement_total(self.path, "a" * 32)

        app = create_app(self.path)
        with TestClient(app) as client:
            response = client.post(f"/api/upload/{saved['batch_id']}/as-total")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["amount_cents"], 1000)
            missing = client.post("/api/upload/not-a-real-batch/as-total")
            self.assertEqual(missing.status_code, 404)


class StatementDirectionTests(unittest.TestCase):
    def test_green_or_plus_is_received_and_plain_text_is_spending(self) -> None:
        self.assertEqual(signed_amount(1250, document_kind="statement", sign="minus", color="none"), 1250)
        self.assertEqual(signed_amount(-1250, document_kind="statement", sign="none", color="red"), 1250)
        self.assertEqual(signed_amount(800, document_kind="statement", sign="plus", color="none"), -800)
        self.assertEqual(signed_amount(800, document_kind="statement", sign="none", color="green"), -800)
        self.assertEqual(signed_amount(800, document_kind="statement", sign="none", color="none"), 800)
        self.assertEqual(signed_amount(800, document_kind="statement", sign="minus", color="green"), 800)
        self.assertEqual(signed_amount(500, document_kind="receipt", sign="none", color="green"), 500)

    def test_schema_accepts_a_statement_line(self) -> None:
        model = _model_from_schema(EXTRACTION_SCHEMA)
        parsed = model.model_validate(
            {
                "injection_suspected": False,
                "document_kind": "statement",
                "store_name": "Campus Card",
                "date": None,
                "statement_total_cents": None,
                "transactions": [
                    {
                        "date": None,
                        "merchant": "Dining hall",
                        "amount_cents": 850,
                        "sign": "minus",
                        "color": "red",
                        "confidence": 0.9,
                    }
                ],
            }
        )
        self.assertEqual(parsed.document_kind, "statement")


class UploadShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "spendsmart.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _upload(self, payload: dict) -> dict:
        async def fake_call(*_args, **_kwargs):
            return payload

        with patch("app.extract.call_llm", new=AsyncMock(side_effect=fake_call)):
            return asyncio.run(extract_upload(_png(), self.path))

    def test_a_receipt_total_stays_one_row(self) -> None:
        saved = self._upload(
            _parsed(
                [
                    {"date": None, "merchant": "Latte", "amount_cents": 450, "confidence": 0.9},
                    {"date": None, "merchant": "Muffin", "amount_cents": 550, "confidence": 0.9},
                ],
                total=1000,
                store_name="Cafe Luna",
            )
        )
        self.assertEqual(saved["saved"], 1)
        self.assertEqual(saved["transactions"][0]["merchant"], "Cafe Luna")
        self.assertEqual(saved["transactions"][0]["amount_cents"], 1000)

    def test_a_receipt_without_a_total_saves_each_item_as_spending(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        saved = self._upload(
            _parsed(
                [
                    {"date": today, "merchant": "Latte", "amount_cents": 450, "sign": "none", "color": "none", "confidence": 0.9},
                    {"date": today, "merchant": "Refund cookie", "amount_cents": -200, "sign": "minus", "color": "red", "confidence": 0.9},
                ],
                store_name="Cafe Luna",
            )
        )
        amounts = [row["amount_cents"] for row in saved["transactions"]]
        self.assertEqual(saved["saved"], 2)
        self.assertEqual(amounts, [450, 200])
        self.assertIsNone(saved["statement_total_cents"])

    def test_a_statement_keeps_each_line_and_ignores_the_balance(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        payload = _parsed(
            [
                {"date": today, "merchant": "Dining hall", "amount_cents": 850, "sign": "none", "color": "red", "confidence": 0.95},
                {"date": today, "merchant": "Paycheck", "amount_cents": 20000, "sign": "plus", "color": "green", "confidence": 0.95},
                {"date": today, "merchant": "Transfer", "amount_cents": 1500, "sign": "none", "color": "none", "confidence": 0.95},
            ],
            total=500000,
            store_name="Campus Card",
        )
        payload["data"]["document_kind"] = "statement"
        saved = self._upload(payload)
        by_name = {row["merchant"]: row["amount_cents"] for row in saved["transactions"]}
        self.assertEqual(saved["saved"], 2)
        self.assertEqual(saved["received_skipped"], 1)
        self.assertNotIn("Paycheck", by_name)
        self.assertEqual(by_name["Dining hall"], 850)
        self.assertEqual(by_name["Transfer"], 1500)
        self.assertEqual(saved["statement_total_cents"], 500000)
        self.assertEqual(saved["review_count"], 2)
        for row in saved["transactions"]:
            self.assertIn("Total mismatch", row["review_reason"])

    def test_review_keeps_a_jpeg_of_the_upload(self) -> None:
        payload = _parsed([], total=1000, injection=True, store_name="Cafe Luna")
        saved = self._upload(payload)
        self.assertEqual(saved["review_count"], 1)
        app = create_app(self.path)
        with TestClient(app) as client:
            review = client.get("/api/review")
            self.assertEqual(review.status_code, 200, review.text)
            batch = review.json()["batches"][0]
            self.assertTrue(batch["has_file"])
            image = client.get(f"/api/uploads/{batch['batch_id']}/file")
            self.assertEqual(image.status_code, 200, image.text)
            self.assertEqual(image.headers["content-type"], "image/jpeg")
            self.assertTrue(image.content.startswith(b"\xff\xd8\xff"))

    def test_a_statement_that_is_only_green_is_rejected(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        payload = _parsed(
            [
                {"date": today, "merchant": "Paycheck", "amount_cents": 20000, "sign": "plus", "color": "green", "confidence": 0.95},
            ],
            store_name="Campus Card",
        )
        payload["data"]["document_kind"] = "statement"
        with self.assertRaises(UploadRejected) as caught:
            self._upload(payload)
        self.assertIn("money received", str(caught.exception))

    def test_a_statement_with_no_lines_is_rejected(self) -> None:
        payload = _parsed([], total=500000, store_name="Campus Card")
        payload["data"]["document_kind"] = "statement"
        with self.assertRaises(UploadRejected):
            self._upload(payload)


if __name__ == "__main__":
    unittest.main()
