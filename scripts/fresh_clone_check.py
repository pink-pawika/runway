"""Time a fresh folder setup. It does not call the model and does not copy .env.

The copy uses a fake parent key so the lookup can be checked offline.
A real grader key stays out of this script.

    .venv/bin/python scripts/fresh_clone_check.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".venv", ".git", "data", "__pycache__", ".pytest_cache", "screenshots", ".fresh-clone-check"}
LIMIT_SECONDS = 600


def _ignore(folder: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in SKIP or name == ".env" or name.endswith(".db")}
    return ignored


def _run(command: list[str], cwd: Path) -> None:
    print("+", " ".join(command))
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    started = time.monotonic()
    parent = Path(tempfile.mkdtemp(prefix="runway-fresh-"))
    repo = parent / "Runway"
    shutil.copytree(ROOT, repo, ignore=_ignore)
    (parent / ".env").write_text("PORTKEY_API_KEY=dry-run-not-a-real-key\n", encoding="utf-8")
    python = sys.executable
    _run([python, "-m", "venv", ".venv"], repo)
    if sys.platform == "win32":
        venv_python = repo / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = repo / ".venv" / "bin" / "python"
    _run([str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"], repo)
    _run([str(venv_python), "-W", "error::ResourceWarning", "-m", "unittest", "discover", "-s", "tests"], repo)
    check = (
        "from app.model import resolve_model\n"
        "choice = resolve_model()\n"
        "assert choice.key_source == 'parent .env', choice.key_source\n"
        "assert choice.model == 'gpt-6-luna'\n"
        "print('parent .env found')\n"
    )
    _run([str(venv_python), "-c", check], repo)
    elapsed = time.monotonic() - started
    print(f"Elapsed: {elapsed:.0f} seconds")
    print(f"Copy left at: {parent}")
    if elapsed > LIMIT_SECONDS:
        print("Over the 10 minute limit.")
        return 1
    print("Under 10 minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
