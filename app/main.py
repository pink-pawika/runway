"""SpendSmart HTTP API. Startup creates the database."""

from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from app.auth import (
    SESSION_DAYS,
    AccountExists,
    authenticate,
    create_user,
    end_session,
    normalize_email,
    open_user_db,
    start_session,
    user_for_token,
)
from app.categories import PROFILES, get_categories
from app.extract import MANUAL_MAX_CENTS, extract_upload, keep_statement_total, save_manual
from app.i20 import (
    apply_i20,
    build_plan,
    extract_i20,
    failed_extract_response,
    save_recurring_rent,
    suggest_split,
)
from app.ledger import (
    MissingTransaction,
    NotReviewable,
    approve_transaction,
    delete_transaction,
    list_month,
    list_review,
    update_recurring,
    update_transaction,
    upload_file,
)
from app.safety import MAX_UPLOAD_BYTES, UploadRejected
from app.summary import list_budget_form, monthly_summary, save_budgets
from app.db import (
    default_db_path,
    CUSTOM_KEY,
    MONTHLY_LIMIT_KEY,
    MONTHS_KEY,
    PROFILE_KEY,
    init_db,
    read_settings,
    update_settings,
    usage_report,
)
from app.env import REPO_ROOT
from app.model import scrub
from app.llm import MAX_RUNS_PER_DAY, BudgetExceeded, ExtractionFailed, token_cost

def public_message(exc: Exception, fallback: str) -> str:
    """Return a short sentence. Stack traces and file paths stay off the screen."""
    text = scrub(str(exc)).replace("\r", " ").strip()
    lowered = text.lower()
    if (
        not text
        or "\n" in str(exc)
        or "traceback" in lowered
        or "site-packages" in lowered
        or 'file "' in lowered
        or ".py" in lowered
    ):
        return fallback
    if len(text) > 300:
        return fallback
    return text


_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_MAX_MONTHS = 24
_MAX_CUSTOM = 20
_MAX_CATEGORY_LENGTH = 40


class SettingsPayload(BaseModel):
    """Profile, academic-year months, custom categories, and the overall monthly cap."""

    profile: str
    academic_year_months: list[str]
    custom_categories: list[str] = Field(default_factory=list)
    monthly_limit_cents: int | None = None

    @field_validator("profile")
    @classmethod
    def clean_profile(cls, value: str) -> str:
        key = value.strip().casefold()
        if key not in PROFILES:
            raise ValueError("profile must be student, personal, or business")
        return key

    @field_validator("academic_year_months")
    @classmethod
    def clean_months(cls, value: list[str]) -> list[str]:
        if len(value) > _MAX_MONTHS:
            raise ValueError(f"academic_year_months cannot list more than {_MAX_MONTHS} months")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            month = item.strip()
            if not _MONTH.fullmatch(month):
                raise ValueError(f"academic year month must be YYYY-MM, got {month!r}")
            if month in seen:
                raise ValueError(f"duplicate month {month}")
            seen.add(month)
            cleaned.append(month)
        return cleaned

    @field_validator("custom_categories")
    @classmethod
    def clean_custom(cls, value: list[str]) -> list[str]:
        if len(value) > _MAX_CUSTOM:
            raise ValueError(f"no more than {_MAX_CUSTOM} custom categories")
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in value:
            name = " ".join(item.split())
            if not name:
                raise ValueError("custom category cannot be blank")
            if len(name) > _MAX_CATEGORY_LENGTH:
                raise ValueError(
                    f"custom category cannot be longer than {_MAX_CATEGORY_LENGTH} characters"
                )
            key = name.casefold()
            if key in seen:
                raise ValueError(f"duplicate custom category {name}")
            seen.add(key)
            cleaned.append(name)
        return cleaned

    @field_validator("monthly_limit_cents")
    @classmethod
    def clean_limit(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0:
            raise ValueError("monthly_limit_cents cannot be negative")
        return value


class CategoriesPayload(BaseModel):
    profile: str
    categories: list[str]


class UsageTotals(BaseModel):
    calls: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float


class UsagePurpose(BaseModel):
    purpose: str
    calls: int
    avg_input_tokens: float
    avg_cached_tokens: float
    avg_output_tokens: float
    avg_cost_usd: float


class UsageLast(BaseModel):
    purpose: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    cost_usd: float


class UsagePayload(BaseModel):
    runs_today: int
    max_runs_per_day: int
    totals: UsageTotals
    by_purpose: list[UsagePurpose]
    last: UsageLast | None = None


class I20Percent(BaseModel):
    category: str
    percent: float


class I20PlanPayload(BaseModel):
    living_expenses_cents: int = Field(ge=0)
    tuition_and_fees_cents: int = Field(ge=0)
    months: int
    tuition_months: list[str] = Field(default_factory=list)
    percents: list[I20Percent] | None = None


class I20NotePayload(BaseModel):
    note: str


class I20MonthlyLimit(BaseModel):
    category: str
    amount_cents: int


class I20Lump(BaseModel):
    month: str
    amount_cents: int


class I20ApplyPayload(BaseModel):
    monthly: list[I20MonthlyLimit]
    lumps: list[I20Lump] = Field(default_factory=list)


class I20RentPayload(BaseModel):
    monthly_living_cents: int = Field(ge=0)
    rent_cents: int = Field(ge=0)


class ManualTransaction(BaseModel):
    merchant: str
    amount_cents: int
    category: str
    date: str | None = None

    @field_validator("merchant")
    @classmethod
    def clean_merchant(cls, value: str) -> str:
        text = " ".join(value.split())
        if not text or len(text) > 80:
            raise ValueError("merchant must be 1 to 80 characters")
        return text

    @field_validator("amount_cents")
    @classmethod
    def clean_amount(cls, value: int) -> int:
        if value <= 0 or value >= MANUAL_MAX_CENTS:
            raise ValueError("amount must be greater than $0 and less than $1,000,000")
        return value

    @field_validator("category")
    @classmethod
    def clean_category(cls, value: str) -> str:
        text = " ".join(value.split())
        if not text:
            raise ValueError("category is required")
        return text


class BudgetLine(BaseModel):
    category: str
    monthly_limit_cents: int | None = None

    @field_validator("category")
    @classmethod
    def clean_budget_category(cls, value: str) -> str:
        text = " ".join(value.split())
        if not text:
            raise ValueError("category is required")
        return text

    @field_validator("monthly_limit_cents")
    @classmethod
    def clean_budget_amount(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0:
            raise ValueError("monthly_limit_cents must be 0 or greater")
        return value


class BudgetsPayload(BaseModel):
    limits: list[BudgetLine]


class RecurringPatch(BaseModel):
    """A repeating charge has no single date."""

    merchant: str | None = None
    amount_cents: int | None = None
    category: str | None = None

    @field_validator("merchant")
    @classmethod
    def clean_recurring_merchant(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(value.split())
        if not text or len(text) > 80:
            raise ValueError("name must be 1 to 80 characters")
        return text

    @field_validator("amount_cents")
    @classmethod
    def clean_recurring_amount(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0 or value >= MANUAL_MAX_CENTS:
            raise ValueError("amount must be under $1,000,000")
        return value


class TransactionPatch(BaseModel):
    """Fields a person can correct. Omitted fields stay as they are."""

    merchant: str | None = None
    amount_cents: int | None = None
    date: str | None = None
    category: str | None = None

    @field_validator("merchant")
    @classmethod
    def clean_merchant_edit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(value.split())
        if not text or len(text) > 80:
            raise ValueError("merchant must be 1 to 80 characters")
        return text

    @field_validator("amount_cents")
    @classmethod
    def clean_amount_edit(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value == 0 or abs(value) >= MANUAL_MAX_CENTS:
            raise ValueError("amount must not be zero and must be under $1,000,000")
        return value

    @field_validator("date")
    @classmethod
    def clean_date_edit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not _DATE.fullmatch(text):
            raise ValueError("date must be YYYY-MM-DD")
        try:
            date.fromisoformat(text)
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD") from None
        return text

    @field_validator("category")
    @classmethod
    def clean_category_edit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(value.split())
        if not text:
            raise ValueError("category is required")
        return text

    @model_validator(mode="after")
    def require_a_change(self) -> "TransactionPatch":
        if not self.model_fields_set:
            raise ValueError("change at least one field")
        if "merchant" in self.model_fields_set and self.merchant is None:
            raise ValueError("merchant must be 1 to 80 characters")
        if "amount_cents" in self.model_fields_set and self.amount_cents is None:
            raise ValueError("amount is required")
        if "category" in self.model_fields_set and self.category is None:
            raise ValueError("category is required")
        return self


def load_settings(db_path: Path) -> SettingsPayload:
    raw = read_settings(db_path)
    months = json.loads(raw.get(MONTHS_KEY, "[]"))
    custom = json.loads(raw.get(CUSTOM_KEY, "[]"))
    limit_text = raw.get(MONTHLY_LIMIT_KEY)
    limit = int(limit_text) if limit_text not in (None, "") else None
    return SettingsPayload(
        profile=raw.get(PROFILE_KEY, "student"),
        academic_year_months=months,
        custom_categories=custom,
        monthly_limit_cents=limit,
    )


def save_settings(db_path: Path, body: SettingsPayload, fields: set[str]) -> SettingsPayload:
    """Profile and months always save. Omitted optional fields stay as they are."""
    values = {
        PROFILE_KEY: body.profile,
        MONTHS_KEY: json.dumps(body.academic_year_months),
    }
    delete_keys: list[str] = []
    if "custom_categories" in fields:
        values[CUSTOM_KEY] = json.dumps(body.custom_categories)
    if "monthly_limit_cents" in fields:
        if body.monthly_limit_cents is None:
            delete_keys.append(MONTHLY_LIMIT_KEY)
        else:
            values[MONTHLY_LIMIT_KEY] = str(body.monthly_limit_cents)
    update_settings(db_path, values, tuple(delete_keys))
    return load_settings(db_path)


COOKIE = "runway_session"


class AuthPayload(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("password")
    @classmethod
    def clean_password(cls, value: str) -> str:
        if len(value) < 8 or len(value) > 200:
            raise ValueError("Password must be 8 to 200 characters.")
        return value


def ledger_path(request: Request) -> Path:
    chosen = getattr(request.state, "db_path", None)
    if isinstance(chosen, Path):
        return chosen
    return request.app.state.db_path


def _session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_DAYS * 24 * 60 * 60,
        path="/",
    )


def create_app(
    db_path: Path | None = None,
    *,
    require_login: bool = False,
    auth_dir: Path | None = None,
    legacy_db: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db_path = init_db(db_path)
        app.state.require_login = require_login
        app.state.auth_dir = auth_dir or (REPO_ROOT / "data")
        app.state.legacy_db = legacy_db
        yield

    app = FastAPI(title="SpendSmart", lifespan=lifespan)

    @app.middleware("http")
    async def attach_user(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and not path.startswith("/api/auth/"):
            user = None
            if request.app.state.require_login:
                user = user_for_token(request.app.state.auth_dir, request.cookies.get(COOKIE, ""))
                if user is None and path != "/api/ready":
                    return JSONResponse(status_code=401, content={"detail": "Log in to continue."})
            if user is not None:
                request.state.user = user
                request.state.db_path = open_user_db(
                    request.app.state.auth_dir,
                    int(user["id"]),
                    request.app.state.legacy_db,
                )
        return await call_next(request)

    @app.exception_handler(BudgetExceeded)
    async def budget_exceeded(_request: Request, exc: BudgetExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": public_message(exc, "Today's AI limit is 40 runs. Try again tomorrow.")},
        )

    @app.exception_handler(UploadRejected)
    async def upload_rejected(_request: Request, exc: UploadRejected) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": public_message(exc, "That file could not be used. Try a smaller PDF or photo.")},
        )

    @app.exception_handler(ExtractionFailed)
    async def extraction_failed(_request: Request, exc: ExtractionFailed) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": public_message(
                    exc,
                    "The document could not be read. Try another photo, or type the amount.",
                )
            },
        )

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            detail = exc.detail
            if not isinstance(detail, str):
                detail = "Check the form and try again."
            return JSONResponse(status_code=exc.status_code, content={"detail": public_message(exc, detail)})
        return JSONResponse(status_code=500, content={"detail": "Something went wrong. Try again."})

    @app.get("/api/ready")
    def ready(request: Request) -> dict[str, object]:
        try:
            from app.model import resolve_model

            resolve_model()
            model_ready = True
            message = ""
        except RuntimeError as exc:
            model_ready = False
            message = public_message(
                exc,
                "Add PORTKEY_API_KEY or OPENAI_API_KEY to .env in this folder or the folder above it, then restart.",
            )
        runs_today = 0
        db_path = getattr(request.state, "db_path", None)
        if isinstance(db_path, Path):
            runs_today = int(usage_report(db_path)["runs_today"])
        return {
            "model_ready": model_ready,
            "model_message": message,
            "runs_today": runs_today,
            "max_runs_per_day": MAX_RUNS_PER_DAY,
            "cap_reached": runs_today >= MAX_RUNS_PER_DAY,
        }

    @app.get("/api/settings", response_model=SettingsPayload)
    def get_settings(request: Request) -> SettingsPayload:
        return load_settings(ledger_path(request))

    @app.put("/api/settings", response_model=SettingsPayload)
    def put_settings(body: SettingsPayload, request: Request) -> SettingsPayload:
        return save_settings(ledger_path(request), body, body.model_fields_set)

    @app.get("/api/categories", response_model=CategoriesPayload)
    def get_category_list(request: Request) -> CategoriesPayload:
        settings = load_settings(ledger_path(request))
        return CategoriesPayload(
            profile=settings.profile,
            categories=get_categories(settings.profile, settings.custom_categories),
        )

    @app.get("/api/usage", response_model=UsagePayload)
    def get_usage(request: Request) -> UsagePayload:
        report = usage_report(ledger_path(request))
        totals = dict(report["totals"])
        totals["cost_usd"] = token_cost(
            int(totals["input_tokens"]),
            int(totals["cached_tokens"]),
            int(totals["output_tokens"]),
        )
        last = None if report["last"] is None else dict(report["last"])
        if last is not None:
            last["cost_usd"] = token_cost(
                int(last["input_tokens"]),
                int(last["cached_tokens"]),
                int(last["output_tokens"]),
            )
        return UsagePayload(
            runs_today=int(report["runs_today"]),
            max_runs_per_day=MAX_RUNS_PER_DAY,
            totals=UsageTotals.model_validate(totals),
            by_purpose=[UsagePurpose.model_validate(row) for row in report["by_purpose"]],
            last=None if last is None else UsageLast.model_validate(last),
        )

    async def _read_upload(file: UploadFile) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = await file.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_UPLOAD_BYTES:
                raise UploadRejected("File is larger than 10 MB.")
            chunks.append(block)
        return b"".join(chunks)

    @app.post("/api/upload")
    async def upload_document(file: UploadFile, request: Request) -> dict[str, object]:
        return await extract_upload(await _read_upload(file), ledger_path(request))

    @app.post("/api/i20/extract")
    async def i20_extract(file: UploadFile, request: Request) -> dict[str, object]:
        data = await _read_upload(file)
        try:
            return await extract_i20(data, ledger_path(request))
        except ExtractionFailed:
            return failed_extract_response()
        finally:
            del data

    @app.post("/api/i20/plan")
    def i20_plan(body: I20PlanPayload) -> dict[str, object]:
        try:
            return build_plan(
                living_expenses_cents=body.living_expenses_cents,
                tuition_and_fees_cents=body.tuition_and_fees_cents,
                months=body.months,
                tuition_months=body.tuition_months,
                percents=None if body.percents is None else [item.model_dump() for item in body.percents],
            )
        except ValueError as exc:
            raise UploadRejected(str(exc)) from None

    @app.post("/api/i20/split")
    async def i20_split(body: I20NotePayload, request: Request) -> dict[str, object]:
        return await suggest_split(body.note, ledger_path(request))

    @app.post("/api/i20/rent")
    def i20_rent(body: I20RentPayload, request: Request) -> dict[str, object]:
        try:
            return save_recurring_rent(
                ledger_path(request),
                body.monthly_living_cents,
                body.rent_cents,
            )
        except ValueError as exc:
            raise UploadRejected(str(exc)) from None

    @app.post("/api/i20/apply")
    def i20_apply(body: I20ApplyPayload, request: Request) -> dict[str, object]:
        try:
            return apply_i20(
                ledger_path(request),
                [item.model_dump() for item in body.monthly],
                [item.model_dump() for item in body.lumps],
            )
        except ValueError as exc:
            raise UploadRejected(str(exc)) from None

    @app.post("/api/upload/{batch_id}/as-total")
    def upload_as_total(batch_id: str, request: Request) -> dict[str, object]:
        if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
            raise HTTPException(status_code=404, detail="Upload not found.")
        try:
            return keep_statement_total(ledger_path(request), batch_id)
        except MissingTransaction:
            raise HTTPException(status_code=404, detail="Upload not found.") from None

    @app.post("/api/transactions")
    def add_transaction(body: ManualTransaction, request: Request) -> dict[str, object]:
        return save_manual(
            merchant=body.merchant,
            amount_cents=body.amount_cents,
            category=body.category,
            tx_date=body.date,
            db_path=ledger_path(request),
        )

    @app.get("/api/transactions")
    def get_transactions(month: str, request: Request) -> dict[str, object]:
        try:
            return list_month(ledger_path(request), month)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.patch("/api/recurring/{charge_id}")
    def patch_recurring(charge_id: int, body: RecurringPatch, request: Request) -> dict[str, object]:
        settings = load_settings(ledger_path(request))
        allowed = get_categories(settings.profile, settings.custom_categories)
        changes = body.model_dump(exclude_unset=True)
        if "category" in changes and changes["category"] not in allowed:
            raise UploadRejected("Choose a category from the current profile.")
        try:
            return update_recurring(ledger_path(request), charge_id, changes)
        except MissingTransaction:
            raise HTTPException(status_code=404, detail="Recurring charge not found.") from None
        except ValueError as exc:
            raise UploadRejected(str(exc)) from None

    @app.patch("/api/transactions/{tx_id}")
    def patch_transaction(tx_id: int, body: TransactionPatch, request: Request) -> dict[str, object]:
        settings = load_settings(ledger_path(request))
        allowed = get_categories(settings.profile, settings.custom_categories)
        changes = body.model_dump(exclude_unset=True)
        if "category" in changes and changes["category"] not in allowed:
            raise UploadRejected("Choose a category from the current profile.")
        try:
            return update_transaction(ledger_path(request), tx_id, changes)
        except MissingTransaction:
            raise HTTPException(status_code=404, detail="Transaction not found.") from None

    @app.delete("/api/transactions/{tx_id}", status_code=204)
    def remove_transaction(tx_id: int, request: Request) -> Response:
        try:
            delete_transaction(ledger_path(request), tx_id)
        except MissingTransaction:
            raise HTTPException(status_code=404, detail="Transaction not found.") from None
        return Response(status_code=204)

    @app.get("/api/summary")
    def get_summary(month: str, request: Request) -> dict[str, object]:
        try:
            return monthly_summary(ledger_path(request), month)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.get("/api/budgets")
    def get_budgets(request: Request) -> dict[str, object]:
        return list_budget_form(ledger_path(request))

    @app.put("/api/budgets")
    def put_budgets(body: BudgetsPayload, request: Request) -> dict[str, object]:
        settings = load_settings(ledger_path(request))
        allowed = get_categories(settings.profile, settings.custom_categories)
        try:
            return save_budgets(
                ledger_path(request),
                [line.model_dump() for line in body.limits],
                allowed,
            )
        except ValueError as exc:
            raise UploadRejected(str(exc)) from None

    @app.get("/api/review")
    def get_review(request: Request) -> dict[str, object]:
        return list_review(ledger_path(request))

    @app.get("/api/uploads/{batch_id}/file")
    def get_upload_file(batch_id: str, request: Request) -> Response:
        if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
            raise HTTPException(status_code=404, detail="Upload not found.")
        image = upload_file(ledger_path(request), batch_id)
        if image is None:
            raise HTTPException(status_code=404, detail="Upload not found.")
        return Response(content=image, media_type="image/jpeg")

    @app.post("/api/review/{tx_id}/approve")
    def approve_review(tx_id: int, request: Request) -> dict[str, object]:
        try:
            return approve_transaction(ledger_path(request), tx_id)
        except MissingTransaction:
            raise HTTPException(status_code=404, detail="Transaction not found.") from None
        except NotReviewable:
            raise HTTPException(status_code=409, detail="Only a review item can be approved.") from None

    def _auth_response(email: str, token: str) -> JSONResponse:
        response = JSONResponse({"email": email})
        _session_cookie(response, token)
        return response

    @app.post("/api/auth/register")
    def register(body: AuthPayload, request: Request) -> JSONResponse:
        try:
            user = create_user(
                request.app.state.auth_dir,
                body.email,
                body.password,
                request.app.state.legacy_db,
            )
        except AccountExists:
            raise HTTPException(
                status_code=409,
                detail="An account with this email already exists. Log in instead.",
            ) from None
        token = start_session(request.app.state.auth_dir, int(user["id"]))
        return _auth_response(str(user["email"]), token)

    @app.post("/api/auth/login")
    def login(body: AuthPayload, request: Request) -> JSONResponse:
        user = authenticate(request.app.state.auth_dir, body.email, body.password)
        if user is None:
            raise HTTPException(status_code=401, detail="Email or password does not match.")
        token = start_session(request.app.state.auth_dir, int(user["id"]))
        return _auth_response(str(user["email"]), token)

    @app.post("/api/auth/logout")
    def logout(request: Request) -> Response:
        end_session(request.app.state.auth_dir, request.cookies.get(COOKIE, ""))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/auth/me")
    def me(request: Request) -> dict[str, str]:
        user = user_for_token(request.app.state.auth_dir, request.cookies.get(COOKIE, ""))
        if user is None:
            raise HTTPException(status_code=401, detail="Log in to continue.")
        return {"email": str(user["email"])}

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    return app


def _auth_dir_from_env() -> Path:
    raw = os.getenv("SPENDSMART_AUTH_DIR", "").strip()
    return Path(raw) if raw else REPO_ROOT / "data"


def _legacy_from_env() -> Path | None:
    raw = os.getenv("SPENDSMART_LEGACY_DB", "").strip()
    if raw:
        return Path(raw)
    if os.getenv("SPENDSMART_AUTH_DIR", "").strip():
        return None
    return default_db_path()


app = create_app(
    require_login=True,
    auth_dir=_auth_dir_from_env(),
    legacy_db=_legacy_from_env(),
)
