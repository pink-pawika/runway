"""Find API keys without ever printing them.

Graders clone this repo into one folder and keep their .env one level above
it. A key in this repo wins. If it is missing here, the parent folder is
used. A key already in the process environment is the last resort.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

COURSE_MODEL = "gpt-6-luna"
PORTKEY_KEY = "PORTKEY_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"
OPENAI_MODEL_KEY = "OPENAI_MODEL"
KEY_NAMES = (PORTKEY_KEY, OPENAI_KEY, OPENAI_MODEL_KEY)

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class EnvLoad:
    """Which env files exist, and where each known key was found."""

    repo_env_present: bool
    parent_env_present: bool
    sources: dict[str, str]


def _nonempty(values: dict[str, str | None]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in values.items():
        if not key or value is None:
            continue
        text = str(value).strip()
        if text:
            cleaned[key] = text
    return cleaned


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return _nonempty(dict(dotenv_values(path)))


def _source(
    name: str,
    repo_values: dict[str, str],
    parent_values: dict[str, str],
    preexisting: dict[str, str],
) -> str:
    if repo_values.get(name):
        return "repo .env"
    if parent_values.get(name):
        return "parent .env"
    if preexisting.get(name):
        return "process environment"
    if os.getenv(name, "").strip():
        return "environment"
    return "missing"


def load_project_env(root: Path | None = None) -> EnvLoad:
    """Load .env from the repo, then fill gaps from the parent folder.

    Empty values are ignored, so a blank line in .env.example cannot hide
    a real key in the other file. File values replace a key inherited from
    the shell, because the grader's test moves .env up one folder.
    """
    root = (root or REPO_ROOT).resolve()
    parent_file = root.parent / ".env"
    repo_file = root / ".env"
    parent_values = _read_env_file(parent_file)
    repo_values = _read_env_file(repo_file)
    preexisting = {name: os.getenv(name, "").strip() for name in KEY_NAMES}

    merged: dict[str, str] = {}
    merged.update(parent_values)
    merged.update(repo_values)
    for key, value in merged.items():
        os.environ[key] = value

    sources = {
        name: _source(name, repo_values, parent_values, preexisting) for name in KEY_NAMES
    }
    return EnvLoad(
        repo_env_present=repo_file.is_file(),
        parent_env_present=parent_file.is_file(),
        sources=sources,
    )
