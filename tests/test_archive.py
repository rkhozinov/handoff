"""Tests for handoff.archive — trim-before-store + marker write."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from handoff import archive
from handoff.archive import _marker_path, archive_full_session, compute_body_hash


SESSION_ID = "abcd1234-feed-face-cafe-deadbeef0000"


@pytest.fixture
def tiny_jsonl(tmp_path: Path) -> Path:
    """A minimal JSONL with one user + one assistant turn."""
    p = tmp_path / "session.jsonl"
    entries = [
        {"type": "user", "message": {"content": "what is 2+2"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "4"}]}},
    ]
    p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return p


@pytest.fixture
def marker_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect $HOME so marker files land in tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _fake_proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_archive_trims_before_storing(tiny_jsonl, marker_home):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return _fake_proc(json.dumps({"content_hash": "deadbeef" * 8}))

    with patch.object(subprocess, "run", side_effect=fake_run):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    assert h == "deadbeef" * 8
    body = captured["input"]
    assert "U: what is 2+2" in body
    assert "A: 4" in body
    # Trimmed body must NOT contain raw JSONL braces of the original entries
    assert '"type": "user"' not in body


def test_archive_uses_session_archive_type_and_tags(tiny_jsonl, marker_home):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc(json.dumps({"content_hash": "a" * 16}))

    with patch.object(subprocess, "run", side_effect=fake_run):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    cmd = captured["cmd"]
    assert "--type" in cmd
    assert cmd[cmd.index("--type") + 1] == "session-archive"
    assert "--tags" in cmd
    tags_val = cmd[cmd.index("--tags") + 1]
    assert tags_val == "source:auto,session-archive,project:myproject"
    assert "--body-file" in cmd
    assert cmd[cmd.index("--body-file") + 1] == "-"


def test_archive_writes_marker_on_success(tiny_jsonl, marker_home):
    content_hash = "f" * 64

    with patch.object(
        subprocess,
        "run",
        return_value=_fake_proc(json.dumps({"content_hash": content_hash})),
    ):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    assert h == content_hash
    marker = _marker_path(SESSION_ID)
    assert marker.exists()
    txt = marker.read_text(encoding="utf-8")
    assert txt.startswith("archived\n")
    assert content_hash in txt


def test_archive_includes_metadata(tiny_jsonl, marker_home):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc(json.dumps({"content_hash": "x" * 16}))

    with patch.object(subprocess, "run", side_effect=fake_run):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    cmd = captured["cmd"]
    assert "--metadata" in cmd
    meta = json.loads(cmd[cmd.index("--metadata") + 1])
    assert meta["session_id"] == SESSION_ID[:8]
    assert meta["source_jsonl"] == str(tiny_jsonl)


def test_archive_skips_empty_transcript(tmp_path, marker_home):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    with patch.object(subprocess, "run") as mock_run:
        h = archive_full_session(str(empty), SESSION_ID, "/Users/test/proj")

    assert h is None
    mock_run.assert_not_called()
    assert not _marker_path(SESSION_ID).exists()


def test_archive_no_marker_on_memory_cli_failure(tiny_jsonl, marker_home):
    with patch.object(subprocess, "run", side_effect=FileNotFoundError):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert h is None
    assert not _marker_path(SESSION_ID).exists()


def test_archive_no_marker_on_called_process_error(tiny_jsonl, marker_home):
    err = subprocess.CalledProcessError(returncode=1, cmd=["memory"], stderr="boom")
    with patch.object(subprocess, "run", side_effect=err):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert h is None
    assert not _marker_path(SESSION_ID).exists()


def test_archive_no_marker_on_timeout(tiny_jsonl, marker_home):
    err = subprocess.TimeoutExpired(cmd=["memory"], timeout=30)
    with patch.object(subprocess, "run", side_effect=err):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert h is None
    assert not _marker_path(SESSION_ID).exists()


def test_archive_unknown_cwd_falls_back(tiny_jsonl, marker_home):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc(json.dumps({"content_hash": "z" * 16}))

    with patch.object(subprocess, "run", side_effect=fake_run):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "")

    cmd = captured["cmd"]
    tags = cmd[cmd.index("--tags") + 1]
    assert tags == "source:auto,session-archive,project:unknown"


def test_archive_truncates_summary_at_500(tmp_path, marker_home):
    """Summary capped at 500 chars + ellipsis when body is longer."""
    jsonl = tmp_path / "big.jsonl"
    long_text = "x" * 5000
    entries = [
        {"type": "user", "message": {"content": long_text}},
    ]
    jsonl.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc(json.dumps({"content_hash": "y" * 16}))

    with patch.object(subprocess, "run", side_effect=fake_run):
        archive_full_session(str(jsonl), SESSION_ID, "/Users/test/proj")

    summary = captured["cmd"][captured["cmd"].index("--summary") + 1]
    assert len(summary) <= 501  # 500 + ellipsis char
    assert summary.endswith("…")


def test_compute_body_hash_stable():
    assert compute_body_hash("hello") == compute_body_hash("hello")
    assert compute_body_hash("a") != compute_body_hash("b")
