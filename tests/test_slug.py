"""Tests for compaction.slug — pure slug shape + git branch detection."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from compaction.slug import cwd_slug, detect_branch


class TestCwdSlug:
    def test_cwd_only_no_branch(self):
        assert cwd_slug("/Users/x/repos/foo") == "-Users-x-repos-foo"

    def test_cwd_only_back_compat_with_old_writer(self):
        # Old writer used `cwd.replace("/", "-")` with no second arg —
        # behavior must match exactly when branch is None.
        old = "/Users/x/repos/foo".replace("/", "-")
        assert cwd_slug("/Users/x/repos/foo") == old
        assert cwd_slug("/Users/x/repos/foo", None) == old

    def test_cwd_with_simple_branch(self):
        assert cwd_slug("/Users/x/foo", "master") == "-Users-x-foo--master"

    def test_branch_with_slash(self):
        # feat/foo-bar → feat-foo-bar
        assert cwd_slug("/Users/x/foo", "feat/foo-bar") == "-Users-x-foo--feat-foo-bar"

    def test_branch_strips_unsafe_chars(self):
        # Spaces, colons, anything outside [A-Za-z0-9._-] gets stripped.
        assert cwd_slug("/Users/x/foo", "weird name:1.0") == "-Users-x-foo--weirdname1.0"

    def test_branch_only_unsafe_falls_back_to_cwd(self):
        # If the branch reduces to empty after stripping, drop the suffix
        # rather than producing `<cwd>--`.
        assert cwd_slug("/Users/x/foo", "@@@") == "-Users-x-foo"

    def test_empty_branch_string_treated_as_none(self):
        assert cwd_slug("/Users/x/foo", "") == "-Users-x-foo"


class TestDetectBranch:
    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"},
        )

    def test_non_git_dir_returns_none(self, tmp_path: Path):
        assert detect_branch(str(tmp_path)) is None

    def test_returns_branch_name(self, tmp_path: Path):
        self._git(tmp_path, "init", "-q", "-b", "main")
        self._git(tmp_path, "config", "user.email", "t@t")
        self._git(tmp_path, "config", "user.name", "t")
        self._git(tmp_path, "commit", "--allow-empty", "-q", "-m", "init")
        assert detect_branch(str(tmp_path)) == "main"

    def test_branch_with_slash(self, tmp_path: Path):
        self._git(tmp_path, "init", "-q", "-b", "main")
        self._git(tmp_path, "config", "user.email", "t@t")
        self._git(tmp_path, "config", "user.name", "t")
        self._git(tmp_path, "commit", "--allow-empty", "-q", "-m", "init")
        self._git(tmp_path, "checkout", "-q", "-b", "feat/foo")
        assert detect_branch(str(tmp_path)) == "feat/foo"

    def test_detached_head_returns_none(self, tmp_path: Path):
        self._git(tmp_path, "init", "-q", "-b", "main")
        self._git(tmp_path, "config", "user.email", "t@t")
        self._git(tmp_path, "config", "user.name", "t")
        self._git(tmp_path, "commit", "--allow-empty", "-q", "-m", "init")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self._git(tmp_path, "checkout", "-q", "--detach", sha)
        assert detect_branch(str(tmp_path)) is None


class TestCliEntryPoint:
    def test_module_invocation_prints_slug(self, tmp_path: Path):
        # `python -m compaction.slug <cwd>` should emit the resolved slug.
        repo_root = Path(__file__).resolve().parent.parent
        r = subprocess.run(
            ["python3", "-m", "compaction.slug", str(tmp_path)],
            capture_output=True,
            text=True,
            cwd=repo_root,
            env={**os.environ, "PYTHONPATH": str(repo_root)},
        )
        assert r.returncode == 0, r.stderr
        # Non-git tmp dir → slug == cwd.replace("/", "-")
        assert r.stdout.strip() == str(tmp_path).replace("/", "-")

    def test_module_invocation_missing_arg_errors(self):
        repo_root = Path(__file__).resolve().parent.parent
        r = subprocess.run(
            ["python3", "-m", "compaction.slug"],
            capture_output=True,
            text=True,
            cwd=repo_root,
            env={**os.environ, "PYTHONPATH": str(repo_root)},
        )
        assert r.returncode != 0
        assert "usage" in r.stderr.lower()
