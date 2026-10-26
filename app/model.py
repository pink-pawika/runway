"""Build the course model client.

The course key, PORTKEY_API_KEY, can call gpt-6-luna only. A personal
OPENAI_API_KEY is used when the course key is absent, and then OPENAI_MODEL
names the model being paid for. gpt-6-luna on Portkey accepts tools through
the Responses API, so every later agent uses that client.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from openai import AsyncOpenAI
from portkey_ai import PORTKEY_GATEWAY_URL, createHeaders
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.env import COURSE_MODEL, OPENAI_KEY, OPENAI_MODEL_KEY, PORTKEY_KEY, load_project_env

# app/llm.py owns retries. The HTTP client does not retry on its own.
MODEL_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class ModelChoice:
    """Which provider and model will run. The key itself stays in the environment."""

    provider: str
    model: str
    key_name: str
    key_source: str


def resolve_model(root: Path | None = None) -> ModelChoice:
    """Choose the provider after loading .env from the repo, then the parent folder."""
    loaded = load_project_env(root)
    if os.getenv(PORTKEY_KEY, "").strip():
        return ModelChoice(
            provider="portkey",
            model=COURSE_MODEL,
            key_name=PORTKEY_KEY,
            key_source=loaded.sources[PORTKEY_KEY],
        )
    if os.getenv(OPENAI_KEY, "").strip():
        model_name = os.getenv(OPENAI_MODEL_KEY, "").strip()
        if not model_name:
            raise RuntimeError(
                "OPENAI_API_KEY is set and PORTKEY_API_KEY is empty. "
                "Set OPENAI_MODEL to the model you are paying for."
            )
        return ModelChoice(
            provider="openai",
            model=model_name,
            key_name=OPENAI_KEY,
            key_source=loaded.sources[OPENAI_KEY],
        )
    raise RuntimeError(
        "No API key found. Put PORTKEY_API_KEY or OPENAI_API_KEY in .env "
        "in this folder, or in the folder above it."
    )


def scrub(text: str) -> str:
    """Remove key material from an error before it is printed."""
    redacted = text
    for name in (PORTKEY_KEY, OPENAI_KEY):
        value = os.getenv(name, "").strip()
        if len(value) >= 8:
            redacted = redacted.replace(value, "[redacted]")
    return re.sub(r"Bearer\s+\S+", "Bearer [redacted]", redacted)


def build_model(
    root: Path | None = None,
    *,
    max_retries: int = 0,
    timeout: float = MODEL_TIMEOUT_SECONDS,
) -> tuple[OpenAIResponsesModel, ModelChoice]:
    """Return a Responses-API model. The API key is not included in the result.

    This builds the client. app/llm.py is the only module that sends a request.
    """
    choice = resolve_model(root)
    api_key = os.environ[choice.key_name].strip()
    if choice.provider == "portkey":
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=PORTKEY_GATEWAY_URL,
            default_headers=dict(createHeaders(api_key=api_key, provider="openai")),
            timeout=timeout,
            max_retries=max_retries,
        )
    else:
        client = AsyncOpenAI(
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )
    model = OpenAIResponsesModel(choice.model, provider=OpenAIProvider(openai_client=client))
    return model, choice
