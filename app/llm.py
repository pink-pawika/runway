"""The only module that sends a request to the model.

Every call reserves one daily run, redacts user text, prepends the untrusted
document warning, and validates the reply before returning it. Token counts
are stored. The document text and the API key are not.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, create_model
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import BinaryContent, ModelResponse
from pydantic_ai.usage import UsageLimits

from app.db import (
    DailyRunCapReached,
    default_db_path,
    init_db,
    insert_llm_call,
    reserve_daily_run,
)
from app.model import MODEL_TIMEOUT_SECONDS, build_model, scrub
from app.safety import UNTRUSTED_PREAMBLE, redact

MAX_RUNS_PER_DAY = 40
# One retry after the first attempt. The HTTP client does not add more.
LLM_MAX_RETRIES = 1
LLM_TIMEOUT_SECONDS = MODEL_TIMEOUT_SECONDS
# One structured reply can take a couple of model requests. This stops a loop.
_REQUESTS_PER_ATTEMPT = 3
MAX_IMAGE_BYTES = 5_000_000
MAX_IMAGES_PER_CALL = 3
_PURPOSE = re.compile(r"[a-z][a-z0-9_]{0,39}")

# Lecture prices for gpt-6-luna, stored as dollars per 1,000 tokens.
# Input $0.10 / 1M, cached input $0.01 / 1M, output $0.50 / 1M.
# Cache writes are not reported separately by this client.
INPUT_USD_PER_1K = 0.0001
CACHED_INPUT_USD_PER_1K = 0.00001
OUTPUT_USD_PER_1K = 0.0005

_IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
}
_SCHEMA_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}

PING_USER_TEXT = 'Return {"ok": true}'
PING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ok": {"type": "boolean"},
        "injection_suspected": {"type": "boolean"},
    },
    "required": ["ok", "injection_suspected"],
}


class BudgetExceeded(Exception):
    """The daily model-run cap was already reached."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or f"Today's AI limit is {MAX_RUNS_PER_DAY} runs. Try again tomorrow."
        )


class ExtractionFailed(Exception):
    """The model did not return valid JSON after the allowed retry."""


class _AttemptFailed(Exception):
    """One model attempt failed. usage is whatever the provider reported."""

    def __init__(self, usage: TokenUse, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.usage = usage
        self.cause = cause


@dataclass(frozen=True)
class TokenUse:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0


def token_cost(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    *,
    input_rate: float = INPUT_USD_PER_1K,
    cached_rate: float = CACHED_INPUT_USD_PER_1K,
    output_rate: float = OUTPUT_USD_PER_1K,
) -> float:
    """Price one call. Cached tokens are billed at the cached rate, not twice."""
    uncached = max(int(input_tokens) - int(cached_tokens), 0)
    dollars = (
        uncached * input_rate + int(cached_tokens) * cached_rate + int(output_tokens) * output_rate
    ) / 1000
    return round(dollars, 6)


def _check_purpose(purpose: str) -> str:
    if not _PURPOSE.fullmatch(purpose or ""):
        raise ValueError("purpose must be a short label such as extract or ping")
    return purpose


def _system_prompt(system: str) -> str:
    text = (system or "").strip()
    if text.startswith(UNTRUSTED_PREAMBLE):
        return text
    if not text:
        return UNTRUSTED_PREAMBLE
    return f"{UNTRUSTED_PREAMBLE}\n\n{text}"


def _as_float(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return float(value)


def _annotation(schema: dict[str, Any], name: str) -> Any:
    kind = schema.get("type")
    nullable = False
    if isinstance(kind, list):
        nullable = "null" in kind
        kinds = [item for item in kind if item != "null"]
        if len(kinds) != 1:
            raise ValueError(f"{name} must use one type besides null")
        kind = kinds[0]
    if kind == "array":
        item = schema.get("items") or {"type": "string"}
        if not isinstance(item, dict):
            raise ValueError(f"{name} items must be a schema object")
        annotation = list[_annotation(item, f"{name}Item")]  # type: ignore[valid-type]
    elif kind == "object":
        annotation = _model_from_schema(schema, name=name)
    elif kind == "number":
        annotation = Annotated[float, BeforeValidator(_as_float)]
    else:
        annotation = _SCHEMA_TYPES.get(str(kind))
        if annotation is None:
            raise ValueError(f"{name} has an unsupported schema type")
    if nullable:
        return annotation | None
    return annotation


def _model_from_schema(schema: dict[str, Any], *, name: str = "LlmOutput") -> type[BaseModel]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("json_schema must be a JSON object schema")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("json_schema must list properties")
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for field_name, prop in properties.items():
        if not isinstance(field_name, str) or not field_name.isidentifier():
            raise ValueError(f"Unsupported field name: {field_name!r}")
        if not isinstance(prop, dict):
            raise ValueError(f"{field_name} must be a schema object")
        annotation = _annotation(prop, field_name[:1].upper() + field_name[1:])
        bounds: dict[str, Any] = {}
        if "minimum" in prop:
            bounds["ge"] = prop["minimum"]
        if "maximum" in prop:
            bounds["le"] = prop["maximum"]
        if "maxLength" in prop:
            bounds["max_length"] = prop["maxLength"]
        if field_name in required:
            default = Field(..., **bounds) if bounds else ...
        elif bounds:
            default = Field(None, **bounds)
        else:
            default = None
        fields[field_name] = (annotation, default)
    extra = "forbid" if schema.get("additionalProperties") is False else "ignore"
    return create_model(
        name,
        __config__=ConfigDict(extra=extra, strict=True),
        **fields,
    )


def _decode_image(image_b64: str, image_mime: str | None) -> tuple[bytes, str]:
    if not image_mime or not image_mime.strip():
        raise ValueError("image_mime is required when image_b64 is set")
    mime = image_mime.strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in _IMAGE_MIMES:
        raise ValueError("image_mime must be a jpeg, png, webp, gif, or heic image")
    payload = image_b64.strip()
    if payload.startswith("data:") and ";base64," in payload:
        payload = payload.split(";base64,", 1)[1]
    payload = re.sub(r"\s+", "", payload)
    padding = "=" * (-len(payload) % 4)
    try:
        data = base64.b64decode(payload + padding, validate=True)
    except Exception as exc:
        raise ValueError("image_b64 is not valid base64") from exc
    if not data:
        raise ValueError("image is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image is larger than {MAX_IMAGE_BYTES} bytes")
    return data, mime


def _user_content(
    user_text: str | None,
    image_b64: str | None,
    image_mime: str | None,
    images: list[tuple[str, str]] | None,
) -> str | list[Any]:
    parts: list[Any] = []
    if user_text is not None and user_text != "":
        parts.append(redact(user_text))
    blobs: list[tuple[str, str | None]] = []
    if image_b64 is not None:
        blobs.append((image_b64, image_mime))
    for blob in images or []:
        blobs.append((blob[0], blob[1]))
    if len(blobs) > MAX_IMAGES_PER_CALL:
        raise ValueError(f"at most {MAX_IMAGES_PER_CALL} images per call")
    for blob, mime in blobs:
        data, media = _decode_image(blob, mime)
        parts.append(BinaryContent(data=data, media_type=media))
    if not parts:
        raise ValueError("call_llm needs user_text or an image")
    if len(parts) == 1 and isinstance(parts[0], str):
        return parts[0]
    return parts


def _usage_from_messages(messages: list[Any]) -> TokenUse:
    input_tokens = 0
    cached_tokens = 0
    output_tokens = 0
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        usage = message.usage
        input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        cached_tokens += int(getattr(usage, "cache_read_tokens", 0) or 0)
        output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
    return TokenUse(input_tokens, cached_tokens, output_tokens)


def _public_error(exc: Exception) -> str:
    text = scrub(redact(f"{type(exc).__name__}: {exc}"))
    if len(text) > 4000:
        return text[:4000] + "..."
    return text


async def _run_model(
    system: str,
    user_content: str | list[Any],
    output_model: type[BaseModel],
) -> tuple[Any, TokenUse]:
    model, _choice = build_model(max_retries=0, timeout=LLM_TIMEOUT_SECONDS)
    agent: Agent[None, Any] = Agent(
        model,
        output_type=output_model,
        instructions=system,
        retries=0,
        defer_model_check=True,
    )
    with capture_run_messages() as messages:
        try:
            result = await agent.run(
                user_content,
                usage_limits=UsageLimits(request_limit=_REQUESTS_PER_ATTEMPT),
                timeout=LLM_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise _AttemptFailed(_usage_from_messages(messages), exc) from exc
    usage = result.usage
    return result.output, TokenUse(
        input_tokens=int(usage.input_tokens or 0),
        cached_tokens=int(usage.cache_read_tokens or 0),
        output_tokens=int(usage.output_tokens or 0),
    )


def _log(db_path: Path, purpose: str, usage: TokenUse, ok: int) -> float:
    cost = token_cost(usage.input_tokens, usage.cached_tokens, usage.output_tokens)
    insert_llm_call(
        db_path,
        purpose=purpose,
        input_tokens=usage.input_tokens,
        cached_tokens=usage.cached_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        ok=ok,
    )
    return cost


async def call_llm(
    purpose: str,
    system: str,
    user_text: str | None = None,
    image_b64: str | None = None,
    image_mime: str | None = None,
    json_schema: dict[str, Any] | None = None,
    *,
    images: list[tuple[str, str]] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Run one validated model call and return the data plus token usage.

    images is a list of (base64, mime) pairs. One call can include up to
    three images, which is how a scanned statement stays inside one daily run.
    """
    label = _check_purpose(purpose)
    if json_schema is None:
        raise ValueError("json_schema is required")
    output_model = _model_from_schema(json_schema)
    prompt = _system_prompt(system)
    content = _user_content(user_text, image_b64, image_mime, images)
    path = init_db(db_path)
    try:
        reserve_daily_run(path, limit=MAX_RUNS_PER_DAY)
    except DailyRunCapReached:
        raise BudgetExceeded from None

    last_error = "the model did not return valid JSON"
    for attempt in range(LLM_MAX_RETRIES + 1):
        usage = TokenUse()
        try:
            raw, usage = await _run_model(prompt, content, output_model)
            payload = raw.model_dump() if isinstance(raw, BaseModel) else raw
            validated = output_model.model_validate(payload)
        except _AttemptFailed as failure:
            usage = failure.usage
            cause = failure.cause if isinstance(failure.cause, Exception) else failure
            _log(path, label, usage, ok=0)
            last_error = _public_error(cause)
            retryable = isinstance(
                failure.cause,
                (ValidationError, UnexpectedModelBehavior, ModelAPIError, TimeoutError),
            )
            if retryable and attempt < LLM_MAX_RETRIES:
                continue
            raise ExtractionFailed(last_error) from None
        except (ValidationError, UnexpectedModelBehavior, ModelAPIError, TimeoutError) as exc:
            _log(path, label, usage, ok=0)
            last_error = _public_error(exc)
            if attempt >= LLM_MAX_RETRIES:
                raise ExtractionFailed(last_error) from None
            continue
        except Exception as exc:
            _log(path, label, usage, ok=0)
            raise ExtractionFailed(_public_error(exc)) from None
        cost = _log(path, label, usage, ok=1)
        return {
            "data": validated.model_dump(),
            "usage": {
                "input_tokens": usage.input_tokens,
                "cached_tokens": usage.cached_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": cost,
            },
        }
    raise ExtractionFailed(last_error)
