"""Turn an upload or a manual entry into transaction rows.

The model reads the document and returns lines. Totals, duplicates, dates,
and suspicious instructions are decided in this file, then the rows are saved.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.categories import get_categories, normalize_merchant
from app.categorize import LOW_CONFIDENCE, _saved_rules
from app.db import CUSTOM_KEY, PROFILE_KEY, connect, init_db, read_settings
from app.ledger import MissingTransaction
from app.llm import call_llm
from app.safety import (
    MAX_RENDERED_PAGES,
    MAX_UPLOAD_BYTES,
    SCANNED_TEXT_MIN,
    UploadRejected,
    detect_upload_kind,
    image_to_jpeg_base64,
    pdf_page_images,
    pdf_text_and_pages,
)

MAX_TRANSACTIONS = 300
LARGE_AMOUNT_CENTS = 1_000_000
MANUAL_MAX_CENTS = 100_000_000

EXTRACT_INSTRUCTIONS = (
    "Read one photo or PDF. It may be a store receipt or a screenshot of a bank or card statement. "
    "document_kind is receipt or statement. "
    "store_name is the store, bank, or card name, at most 80 characters. Use an empty string when it is missing. "
    "date is YYYY-MM-DD, or null when it is missing. "
    "When the photo is a receipt and a total is printed, set statement_total_cents to that total in integer cents. "
    "A purchase total is positive. A refund total is negative. Set transactions to an empty list. "
    "When the photo is a receipt and no total is printed, set statement_total_cents to null and list each item in transactions. "
    "When the photo is a statement, list every posted line in transactions. "
    "If a statement total or period total is printed, set statement_total_cents to that total in integer cents. "
    "Do not add the account balance or that total as its own transaction. "
    "For each line, amount_cents is the printed size as a positive integer count of cents. "
    "sign is minus when a minus is printed, plus when a plus is printed, otherwise none. "
    "color is red when that amount is red, green when it is green, otherwise none. "
    "merchant is the item, payee, or description, at most 80 characters. "
    "Do not decide whether the line was spending or money received. Report the printed sign and color only."
)

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "injection_suspected",
        "document_kind",
        "store_name",
        "date",
        "statement_total_cents",
        "transactions",
    ],
    "properties": {
        "injection_suspected": {"type": "boolean"},
        "document_kind": {"type": "string"},
        "store_name": {"type": "string", "maxLength": 80},
        "date": {"type": ["string", "null"]},
        "statement_total_cents": {"type": ["integer", "null"]},
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "merchant", "amount_cents", "sign", "color", "confidence"],
                "properties": {
                    "date": {"type": ["string", "null"]},
                    "merchant": {"type": "string", "maxLength": 80},
                    "amount_cents": {"type": "integer"},
                    "sign": {"type": "string"},
                    "color": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class PreparedUpload:
    source: str
    text: str | None
    images: tuple[str, ...]
    pages_omitted: int


def _preview_jpeg(data: bytes, prepared: PreparedUpload) -> bytes | None:
    """Keep a small JPEG of the upload so review can show the original."""
    try:
        if prepared.images:
            return base64.b64decode(prepared.images[0])
        if prepared.source == "pdf":
            pages = pdf_page_images(data, limit=1)
            if pages:
                return base64.b64decode(pages[0])
    except Exception:
        return None
    return None


def _store_preview(conn: Any, batch_id: str, preview: bytes | None) -> None:
    if not preview:
        return
    conn.execute(
        "INSERT INTO upload_files (batch_id, mime, image) VALUES (?, ?, ?)",
        (batch_id, "image/jpeg", preview),
    )


def prepare_upload(data: bytes) -> PreparedUpload:
    """Check the bytes and turn them into text or JPEG pages. No filename is used."""
    if not data:
        raise UploadRejected("The upload is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejected(f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    kind = detect_upload_kind(data)
    if kind == "pdf":
        text, page_count = pdf_text_and_pages(data)
        if len(text.strip()) >= SCANNED_TEXT_MIN:
            return PreparedUpload("pdf", text, (), 0)
        images = tuple(pdf_page_images(data))
        omitted = max(page_count - MAX_RENDERED_PAGES, 0)
        return PreparedUpload("pdf", None, images, omitted)
    return PreparedUpload("image", None, (image_to_jpeg_base64(data),), 0)


def _dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(cents)
    return f"{sign}${value // 100}.{value % 100:02d}"


def _parse_date(value: object) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        return None, "Date could not be read"
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None, "Date could not be read"
    today = datetime.now(timezone.utc).date()
    if parsed > today:
        return text, "Date is in the future"
    if parsed < today - timedelta(days=365):
        return text, "Date is more than 1 year old"
    return text, None


def _duplicate(conn: Any, merchant_key: str, amount_cents: int, tx_date: str | None, seen: set[tuple[str, int, str]]) -> bool:
    if not merchant_key or tx_date is None:
        return False
    marker = (merchant_key, amount_cents, tx_date)
    if marker in seen:
        return True
    rows = conn.execute(
        "SELECT merchant FROM transactions WHERE amount_cents = ? AND date = ?",
        (amount_cents, tx_date),
    ).fetchall()
    return any(normalize_merchant(str(row["merchant"])) == merchant_key for row in rows)


def _review_reasons(
    item: dict[str, Any],
    *,
    shared: list[str],
    conn: Any,
    seen: set[tuple[str, int, str]],
) -> tuple[str, int, str | None, str, float]:
    reasons = list(shared)
    merchant = " ".join(str(item.get("merchant") or "").split())[:80]
    if not merchant:
        reasons.append("Missing merchant")
    amount = int(item["amount_cents"])
    if amount == 0:
        reasons.append("Amount is zero")
    if abs(amount) > LARGE_AMOUNT_CENTS:
        reasons.append("Amount is over $10,000")
    tx_date, date_reason = _parse_date(item.get("date"))
    if date_reason:
        reasons.append(date_reason)
    confidence = float(item["confidence"])
    key = normalize_merchant(merchant)
    if _duplicate(conn, key, amount, tx_date, seen):
        reasons.append("Possible duplicate")
    if key and tx_date is not None:
        seen.add((key, amount, tx_date))
    return merchant, amount, tx_date, "; ".join(reasons), confidence


def signed_amount(amount_cents: int, *, document_kind: str, sign: str, color: str) -> int:
    """Turn a printed amount into ledger cents.

    Receipt lines are spending, so they stay positive. On a statement, a minus
    or red ink is spending. Green ink, or a plus with no minus, is money
    received. A line with no sign and no color is spending. Red or a minus
    wins when both appear.
    """
    magnitude = abs(int(amount_cents))
    if document_kind != "statement":
        return magnitude
    if sign == "minus" or color == "red":
        return magnitude
    if color == "green" or sign == "plus":
        return -magnitude
    return magnitude


def _money_received(*, document_kind: str, sign: str, color: str) -> bool:
    """Green (or a plus) on a statement screenshot is money coming in.

    Those lines are not purchases, so they are left out of the saved list.
    A minus or red ink still counts as spending when both marks appear.
    """
    if document_kind != "statement":
        return False
    if sign == "minus" or color == "red":
        return False
    return color == "green" or sign == "plus"


def _clean_mark(value: object, allowed: set[str]) -> str:
    text = str(value or "none").strip().lower()
    return text if text in allowed else "none"


def _directed_items(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    kind = str(parsed.get("document_kind") or "receipt")
    directed: list[dict[str, Any]] = []
    received = 0
    for item in parsed.get("transactions") or []:
        sign = _clean_mark(item.get("sign"), {"minus", "plus", "none"})
        color = _clean_mark(item.get("color"), {"red", "green", "none"})
        if _money_received(document_kind=kind, sign=sign, color=color):
            received += 1
            continue
        amount = signed_amount(
            int(item["amount_cents"]),
            document_kind=kind,
            sign=sign,
            color=color,
        )
        directed.append({**item, "amount_cents": amount})
    return directed, received


def _rule_assignments(db_path: Path | None, items: list[dict[str, Any]]) -> dict[str, tuple[str, float]]:
    path = init_db(db_path)
    keys: list[str] = []
    for item in items:
        key = normalize_merchant(str(item.get("merchant") or ""))
        if key and key not in keys:
            keys.append(key)
    rules = _saved_rules(path, keys)
    assignments: dict[str, tuple[str, float]] = {}
    for item in items:
        key = normalize_merchant(str(item.get("merchant") or ""))
        if not key or key in assignments:
            continue
        if key in rules:
            assignments[key] = (rules[key], 1.0)
        else:
            assignments[key] = ("Other", float(item.get("confidence") or 0))
    return assignments


def _save_upload(
    prepared: PreparedUpload,
    result: dict[str, Any],
    db_path: Path | None,
    preview: bytes | None = None,
) -> dict[str, Any]:
    """Save a receipt total, or each line when the photo has no total.

    A statement is always saved line by line. A printed balance is not a purchase.
    """
    parsed = result["data"]
    kind = str(parsed.get("document_kind") or "receipt")
    if kind != "statement" and parsed.get("statement_total_cents") is not None:
        return _save_store_total(prepared, result, db_path, preview)
    items, received = _directed_items(parsed)
    if not items:
        if received:
            raise UploadRejected("This screenshot only showed money received, so nothing was added.")
        raise UploadRejected("No amounts could be read from this file.")
    if len(items) > MAX_TRANSACTIONS:
        raise UploadRejected(f"This upload has more than {MAX_TRANSACTIONS} transactions.")
    rewritten = {
        "data": {**parsed, "transactions": items},
        "usage": result["usage"],
    }
    saved = _save_extracted(
        prepared,
        rewritten,
        db_path,
        _rule_assignments(db_path, items),
        preview,
    )
    saved["received_skipped"] = received
    return saved


async def extract_upload(data: bytes, db_path: Path | None = None) -> dict[str, Any]:
    """Validate one upload and save either a receipt total or each statement line."""
    prepared = prepare_upload(data)
    if prepared.text is not None:
        result = await call_llm(
            "extract",
            EXTRACT_INSTRUCTIONS,
            user_text=prepared.text,
            json_schema=EXTRACTION_SCHEMA,
            db_path=db_path,
        )
    else:
        if not prepared.images:
            raise UploadRejected("No pages could be read from this file.")
        result = await call_llm(
            "extract",
            EXTRACT_INSTRUCTIONS,
            user_text="Read the attached photo. It may be a receipt or a statement screenshot.",
            json_schema=EXTRACTION_SCHEMA,
            images=[(image, "image/jpeg") for image in prepared.images],
            db_path=db_path,
        )
    return _save_upload(prepared, result, db_path, _preview_jpeg(data, prepared))


def _category_decision(
    merchant: str,
    extraction_confidence: float,
    assignments: dict[str, tuple[str, float]] | None,
) -> tuple[str, float, str | None]:
    """Return category, stored confidence, and a low-confidence review reason.

    A missing assignment map keeps the extraction confidence. That path is for
    the review-flag checks. An upload always passes the map from categorize_batch.
    """
    if assignments is None:
        return "Other", extraction_confidence, None
    key = normalize_merchant(merchant)
    if not key:
        return "Other", extraction_confidence, None
    category, confidence = assignments.get(key, ("Other", 0.0))
    reason = "Low confidence category" if confidence < LOW_CONFIDENCE else None
    return category, confidence, reason


def _save_extracted(
    prepared: PreparedUpload,
    result: dict[str, Any],
    db_path: Path | None,
    assignments: dict[str, tuple[str, float]] | None = None,
    preview: bytes | None = None,
) -> dict[str, Any]:
    parsed = result["data"]
    items = parsed["transactions"]
    if len(items) > MAX_TRANSACTIONS:
        raise UploadRejected(f"This upload has more than {MAX_TRANSACTIONS} transactions.")
    shared: list[str] = []
    total = parsed["statement_total_cents"]
    if total is not None:
        extracted_sum = sum(int(item["amount_cents"]) for item in items)
        if abs(extracted_sum - int(total)) > 1:
            shared.append(
                "Total mismatch: extracted "
                f"{_dollars(extracted_sum)} vs statement {_dollars(int(total))}"
            )
    if parsed["injection_suspected"]:
        shared.append("Document contained suspicious instructions")
    if prepared.pages_omitted:
        shared.append(f"Only the first {MAX_RENDERED_PAGES} scanned pages were read")

    path = init_db(db_path)
    batch_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    saved: list[dict[str, Any]] = []
    conn = connect(path)
    try:
        seen: set[tuple[str, int, str]] = set()
        for item in items:
            merchant, amount, tx_date, reason, confidence = _review_reasons(
                item, shared=shared, conn=conn, seen=seen
            )
            category, confidence, category_reason = _category_decision(
                merchant, confidence, assignments
            )
            if category_reason:
                reason = f"{reason}; {category_reason}" if reason else category_reason
            status = "review" if reason else "confirmed"
            cursor = conn.execute(
                """
                INSERT INTO transactions (
                    date, merchant, amount_cents, category, source, confidence,
                    status, review_reason, batch_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx_date,
                    merchant,
                    amount,
                    category,
                    prepared.source,
                    confidence,
                    status,
                    reason or None,
                    batch_id,
                    created_at,
                ),
            )
            saved.append(
                {
                    "id": int(cursor.lastrowid),
                    "date": tx_date,
                    "merchant": merchant,
                    "amount_cents": amount,
                    "category": category,
                    "source": prepared.source,
                    "confidence": confidence,
                    "status": status,
                    "review_reason": reason or None,
                    "batch_id": batch_id,
                }
            )
        if saved:
            conn.execute(
                """
                INSERT INTO upload_batches (batch_id, statement_total_cents)
                VALUES (?, ?)
                """,
                (batch_id, int(total) if total is not None else None),
            )
            _store_preview(conn, batch_id, preview)
            conn.commit()
    finally:
        conn.close()
    usage = result["usage"]
    return {
        "batch_id": batch_id if saved else None,
        "saved": len(saved),
        "review_count": sum(1 for row in saved if row["status"] == "review"),
        "pages_omitted": prepared.pages_omitted,
        "statement_total_cents": int(total) if total is not None else None,
        "transactions": saved,
        "usage": usage,
    }


def _store_name(parsed: dict[str, Any], items: list[dict[str, Any]]) -> str:
    store = " ".join(str(parsed.get("store_name") or "").split())[:80]
    if store:
        return store
    names: list[str] = []
    for item in items:
        name = " ".join(str(item.get("merchant") or "").split())[:80]
        if name and name not in names:
            names.append(name)
    if len(names) == 1:
        return names[0]
    return ""


def _store_total(parsed: dict[str, Any], items: list[dict[str, Any]]) -> int:
    total = parsed.get("statement_total_cents")
    if total is not None:
        return int(total)
    if not items:
        raise UploadRejected("No total could be read from this receipt.")
    return sum(int(item["amount_cents"]) for item in items)


def _save_store_total(
    prepared: PreparedUpload,
    result: dict[str, Any],
    db_path: Path | None,
    preview: bytes | None = None,
) -> dict[str, Any]:
    """Save one row: the store name and the receipt total. Item lines are not saved."""
    parsed = result["data"]
    items = list(parsed.get("transactions") or [])
    if len(items) > MAX_TRANSACTIONS:
        raise UploadRejected(f"This upload has more than {MAX_TRANSACTIONS} transactions.")
    total = _store_total(parsed, items)
    store = _store_name(parsed, items)
    tx_date = parsed.get("date")
    if tx_date is None and items:
        dates = {item.get("date") for item in items}
        if len(dates) == 1:
            tx_date = next(iter(dates))
    path = init_db(db_path)
    category = "Other"
    key = normalize_merchant(store)
    if key:
        category = _saved_rules(path, [key]).get(key, "Other")
    shared: list[str] = []
    if parsed.get("injection_suspected"):
        shared.append("Document contained suspicious instructions")
    if prepared.pages_omitted:
        shared.append(f"Only the first {MAX_RENDERED_PAGES} scanned pages were read")
    batch_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect(path)
    try:
        _merchant, amount, clean_date, reason, confidence = _review_reasons(
            {
                "merchant": store,
                "amount_cents": total,
                "date": tx_date,
                "confidence": 1,
            },
            shared=shared,
            conn=conn,
            seen=set(),
        )
        status = "review" if reason else "confirmed"
        cursor = conn.execute(
            """
            INSERT INTO transactions (
                date, merchant, amount_cents, category, source, confidence,
                status, review_reason, batch_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_date,
                _merchant,
                amount,
                category,
                prepared.source,
                confidence,
                status,
                reason or None,
                batch_id,
                created_at,
            ),
        )
        saved = {
            "id": int(cursor.lastrowid),
            "date": clean_date,
            "merchant": _merchant,
            "amount_cents": amount,
            "category": category,
            "source": prepared.source,
            "confidence": confidence,
            "status": status,
            "review_reason": reason or None,
            "batch_id": batch_id,
        }
        conn.execute(
            """
            INSERT INTO upload_batches (batch_id, statement_total_cents)
            VALUES (?, ?)
            """,
            (batch_id, amount),
        )
        _store_preview(conn, batch_id, preview)
        conn.commit()
    finally:
        conn.close()
    return {
        "batch_id": batch_id,
        "saved": 1,
        "review_count": 1 if saved["status"] == "review" else 0,
        "pages_omitted": prepared.pages_omitted,
        "statement_total_cents": amount,
        "transactions": [saved],
        "usage": result["usage"],
    }


def _row_payload(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "date": row["date"],
        "merchant": row["merchant"],
        "amount_cents": int(row["amount_cents"]),
        "category": row["category"],
        "source": row["source"],
        "confidence": float(row["confidence"]),
        "status": row["status"],
        "review_reason": row["review_reason"],
        "batch_id": row["batch_id"],
    }


def _shared_text(rows: list[Any], key: str) -> str | None:
    values: list[str] = []
    for row in rows:
        raw = row[key]
        if raw is None:
            return None
        text = " ".join(str(raw).split())
        if not text:
            return None
        values.append(text)
    if values and all(item == values[0] for item in values):
        return values[0]
    return None


def keep_statement_total(db_path: Path, batch_id: str) -> dict[str, Any]:
    """Replace one upload's line items with its printed total.

    The amount is the total saved when the file was read. Line items are
    removed so the receipt is not counted twice.
    """
    path = init_db(db_path)
    conn = connect(path)
    try:
        batch = conn.execute(
            "SELECT statement_total_cents FROM upload_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if batch is None or batch["statement_total_cents"] is None:
            if batch is None:
                raise MissingTransaction("Upload not found.")
            raise UploadRejected("This receipt has no printed total.")
        total = int(batch["statement_total_cents"])
        rows = conn.execute(
            "SELECT * FROM transactions WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        if not rows:
            raise MissingTransaction("Upload not found.")
        if len(rows) == 1 and int(rows[0]["amount_cents"]) == total:
            return _row_payload(rows[0])

        merchant = _shared_text(rows, "merchant") or "Receipt"
        category = _shared_text(rows, "category") or "Other"
        tx_date = _shared_text(rows, "date")
        reasons: list[str] = []
        pages_note = f"Only the first {MAX_RENDERED_PAGES} scanned pages were read"
        for row in rows:
            for part in str(row["review_reason"] or "").split("; "):
                if part in {"Document contained suspicious instructions", pages_note}:
                    if part not in reasons:
                        reasons.append(part)
        parsed_date, date_reason = _parse_date(tx_date)
        if date_reason:
            reasons.append(date_reason)
        if total == 0:
            reasons.append("Amount is zero")
        if abs(total) > LARGE_AMOUNT_CENTS:
            reasons.append("Amount is over $10,000")
        conn.execute("DELETE FROM transactions WHERE batch_id = ?", (batch_id,))
        if _duplicate(conn, normalize_merchant(merchant), total, parsed_date, set()):
            reasons.append("Possible duplicate")
        status = "review" if reasons else "confirmed"
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = conn.execute(
            """
            INSERT INTO transactions (
                date, merchant, amount_cents, category, source, confidence,
                status, review_reason, batch_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed_date,
                merchant[:80],
                total,
                category,
                rows[0]["source"],
                1.0,
                status,
                "; ".join(reasons) or None,
                batch_id,
                created_at,
            ),
        )
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM transactions WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        return _row_payload(saved)
    finally:
        conn.close()


def _manual_date(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        date.fromisoformat(text)
    except ValueError:
        raise UploadRejected("Date must be YYYY-MM-DD.") from None
    return text


def save_manual(
    *,
    merchant: str,
    amount_cents: int,
    category: str,
    tx_date: str | None,
    db_path: Path,
) -> dict[str, Any]:
    """Save one typed transaction. The amount is positive cents."""
    raw = read_settings(db_path)
    custom = json.loads(raw.get(CUSTOM_KEY, "[]"))
    allowed = get_categories(raw.get(PROFILE_KEY, "student"), custom)
    if category not in allowed:
        raise UploadRejected("Choose a category from the current profile.")
    cleaned = _manual_date(tx_date)
    batch_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = connect(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO transactions (
                date, merchant, amount_cents, category, source, confidence,
                status, review_reason, batch_id, created_at
            ) VALUES (?, ?, ?, ?, 'manual', 1, 'confirmed', NULL, ?, ?)
            """,
            (cleaned, merchant, amount_cents, category, batch_id, created_at),
        )
        conn.commit()
        row_id = int(cursor.lastrowid)
    finally:
        conn.close()
    return {
        "id": row_id,
        "date": cleaned,
        "merchant": merchant,
        "amount_cents": amount_cents,
        "category": category,
        "source": "manual",
        "confidence": 1,
        "status": "confirmed",
        "review_reason": None,
        "batch_id": batch_id,
    }
