"""The grader's .env sits one folder above the repo. These checks lock that rule."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.env import COURSE_MODEL, OPENAI_KEY, OPENAI_MODEL_KEY, PORTKEY_KEY, load_project_env
from app.model import resolve_model


class EnvPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in (PORTKEY_KEY, OPENAI_KEY, OPENAI_MODEL_KEY)}
        for name in self._saved:
            os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self._tmp.name)
        self.repo = self.parent / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _write(self, folder: Path, text: str) -> None:
        (folder / ".env").write_text(text, encoding="utf-8")

    def test_repo_key_beats_parent_key(self) -> None:
        self._write(self.parent, "PORTKEY_API_KEY=parent-key\n")
        self._write(self.repo, "PORTKEY_API_KEY=repo-key\n")
        loaded = load_project_env(self.repo)
        self.assertEqual(os.environ[PORTKEY_KEY], "repo-key")
        self.assertEqual(loaded.sources[PORTKEY_KEY], "repo .env")

    def test_parent_key_is_used_when_repo_file_is_blank(self) -> None:
        self._write(self.parent, "PORTKEY_API_KEY=parent-key\n")
        self._write(self.repo, "PORTKEY_API_KEY=\n")
        loaded = load_project_env(self.repo)
        self.assertEqual(os.environ[PORTKEY_KEY], "parent-key")
        self.assertEqual(loaded.sources[PORTKEY_KEY], "parent .env")

    def test_the_app_finds_a_temp_env_in_the_parent_folder(self) -> None:
        self._write(self.parent, "PORTKEY_API_KEY=parent-key\n")
        choice = resolve_model(self.repo)
        self.assertEqual(choice.provider, "portkey")
        self.assertEqual(choice.model, COURSE_MODEL)
        self.assertEqual(choice.key_source, "parent .env")
        self.assertNotIn("parent-key", repr(choice))

    def test_parent_key_is_used_when_repo_has_no_file(self) -> None:
        self._write(self.parent, "PORTKEY_API_KEY=parent-key\n")
        loaded = load_project_env(self.repo)
        self.assertEqual(os.environ[PORTKEY_KEY], "parent-key")
        self.assertEqual(loaded.sources[PORTKEY_KEY], "parent .env")
        self.assertFalse(loaded.repo_env_present)
        self.assertTrue(loaded.parent_env_present)

    def test_repo_file_beats_the_shell(self) -> None:
        os.environ[PORTKEY_KEY] = "shell-key"
        self._write(self.repo, "PORTKEY_API_KEY=repo-key\n")
        loaded = load_project_env(self.repo)
        self.assertEqual(os.environ[PORTKEY_KEY], "repo-key")
        self.assertEqual(loaded.sources[PORTKEY_KEY], "repo .env")

    def test_shell_key_is_used_when_no_file_has_one(self) -> None:
        os.environ[PORTKEY_KEY] = "shell-key"
        loaded = load_project_env(self.repo)
        self.assertEqual(os.environ[PORTKEY_KEY], "shell-key")
        self.assertEqual(loaded.sources[PORTKEY_KEY], "process environment")

    def test_course_key_locks_the_model(self) -> None:
        self._write(
            self.repo,
            "PORTKEY_API_KEY=course-key\nOPENAI_API_KEY=own-key\nOPENAI_MODEL=some-other-model\n",
        )
        choice = resolve_model(self.repo)
        self.assertEqual(choice.provider, "portkey")
        self.assertEqual(choice.model, COURSE_MODEL)
        self.assertEqual(choice.key_source, "repo .env")

    def test_own_key_uses_the_named_model(self) -> None:
        self._write(self.repo, "OPENAI_API_KEY=own-key\nOPENAI_MODEL=gpt-4.1\n")
        choice = resolve_model(self.repo)
        self.assertEqual(choice.provider, "openai")
        self.assertEqual(choice.model, "gpt-4.1")

    def test_own_key_without_a_model_name_fails(self) -> None:
        self._write(self.repo, "OPENAI_API_KEY=own-key\n")
        with self.assertRaises(RuntimeError) as caught:
            resolve_model(self.repo)
        self.assertIn("OPENAI_MODEL", str(caught.exception))

    def test_missing_keys_fail_with_the_two_search_locations(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            resolve_model(self.repo)
        message = str(caught.exception)
        self.assertIn("PORTKEY_API_KEY", message)
        self.assertIn("folder above", message)


if __name__ == "__main__":
    unittest.main()
