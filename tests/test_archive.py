"""Tests for handoff.archive — trim-before-store + marker write.

archive_full_session shells out to the `memory` CLI (`memory doc store`)
rather than importing a `memory` SDK — the CLI is a uv-tool install that
isn't importable from the python3 handoff runs under. Tests mock
`_memory_bin` (binary resolution) and `subprocess.run` (the CLI call).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from handoff.archive import _marker_path, archive_full_session, compute_body_hash


SESSION_ID = "abcd1234-feed-face-cafe-deadbeef0000"
FAKE_BIN = "/fake/bin/memory"


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


def _argv_to_opts(argv: list[str]) -> dict:
    """Parse the `memory doc store` argv into {flag: value}. Assumes every
    option is `--flag value` (the shape archive.py builds)."""
    opts = {}
    argv_rest = argv[3:]  # skip [bin, "doc", "store"]
    for i in range(0, len(argv_rest) - 1, 2):
        opts[argv_rest[i]] = argv_rest[i + 1]
    return opts


def _patch_cli(content_hash: str = "deadbeef" * 8, returncode: int = 0, stdout: str | None = None):
    """Patch _memory_bin + subprocess.run to simulate a successful CLI call.
    Returns the run mock so tests can inspect the argv it was called with.
    Reads the --body-file back so body/summary/tags assertions stay possible."""
    if stdout is None:
        stdout = json.dumps({"content_hash": content_hash, "status": "stored"})

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    run_mock = patch("handoff.archive.subprocess.run", side_effect=fake_run)
    bin_mock = patch("handoff.archive._memory_bin", return_value=FAKE_BIN)
    return bin_mock, run_mock


def _capture_argv(run_calls: list):
    """A subprocess.run replacement that records argv + reads the body file
    (unlinked in the `finally`, so we must read it before returning)."""
    def fake_run(argv, **kwargs):
        opts = _argv_to_opts(argv)
        body = Path(opts["--body-file"]).read_text(encoding="utf-8") if "--body-file" in opts else ""
        run_calls.append({"argv": argv, "opts": opts, "body": body})
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"content_hash": "deadbeef" * 8, "status": "stored"}),
            stderr="",
        )
    return fake_run


def test_archive_trims_before_storing(tiny_jsonl, marker_home):
    calls: list = []
    with patch("handoff.archive._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.archive.subprocess.run", side_effect=_capture_argv(calls)):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    assert h == "deadbeef" * 8
    assert len(calls) == 1
    body = calls[0]["body"]
    assert "U: what is 2+2" in body
    assert "A: 4" in body
    assert '"type": "user"' not in body


def test_archive_uses_session_archive_type_and_tags(tiny_jsonl, marker_home):
    calls: list = []
    with patch("handoff.archive._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.archive.subprocess.run", side_effect=_capture_argv(calls)):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    opts = calls[0]["opts"]
    assert opts["--type"] == "session-archive"
    tags = opts["--tags"].split(",")
    assert "source:auto" in tags
    assert "session-archive" in tags
    assert "project:myproject" in tags


def test_archive_writes_marker_on_success(tiny_jsonl, marker_home):
    content_hash = "f" * 64
    bin_mock, run_mock = _patch_cli(content_hash)
    with bin_mock, run_mock:
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    assert h == content_hash
    marker = _marker_path(SESSION_ID)
    assert marker.exists()
    txt = marker.read_text(encoding="utf-8")
    assert txt.startswith("archived\n")
    assert content_hash in txt


def test_archive_includes_metadata(tiny_jsonl, marker_home):
    calls: list = []
    with patch("handoff.archive._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.archive.subprocess.run", side_effect=_capture_argv(calls)):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    meta = json.loads(calls[0]["opts"]["--metadata"])
    assert meta["session_id"] == SESSION_ID[:8]
    assert meta["source_jsonl"] == str(tiny_jsonl)


def test_archive_skips_empty_transcript(tmp_path, marker_home):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    bin_mock, run_mock = _patch_cli()
    with bin_mock, run_mock as run:
        h = archive_full_session(str(empty), SESSION_ID, "/Users/test/proj")

    assert h is None
    run.assert_not_called()
    assert not _marker_path(SESSION_ID).exists()


def test_archive_no_marker_when_cli_missing(tiny_jsonl, marker_home):
    with patch("handoff.archive._memory_bin", return_value=None):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert h is None
    assert not _marker_path(SESSION_ID).exists()


def test_archive_no_marker_on_cli_nonzero_exit(tiny_jsonl, marker_home):
    bin_mock, run_mock = _patch_cli(returncode=1, stdout="")
    with bin_mock, run_mock:
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert h is None
    assert not _marker_path(SESSION_ID).exists()


def test_archive_no_marker_on_unparseable_output(tiny_jsonl, marker_home):
    bin_mock, run_mock = _patch_cli(stdout="not json")
    with bin_mock, run_mock:
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert h is None
    assert not _marker_path(SESSION_ID).exists()


def test_archive_unknown_cwd_falls_back(tiny_jsonl, marker_home):
    calls: list = []
    with patch("handoff.archive._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.archive.subprocess.run", side_effect=_capture_argv(calls)):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "")

    tags = calls[0]["opts"]["--tags"].split(",")
    assert "project:unknown" in tags


def test_archive_truncates_summary_at_500(tmp_path, marker_home):
    jsonl = tmp_path / "big.jsonl"
    long_text = "x" * 5000
    entries = [{"type": "user", "message": {"content": long_text}}]
    jsonl.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

    calls: list = []
    with patch("handoff.archive._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.archive.subprocess.run", side_effect=_capture_argv(calls)):
        archive_full_session(str(jsonl), SESSION_ID, "/Users/test/proj")

    summary = calls[0]["opts"]["--summary"]
    assert len(summary) <= 501
    assert summary.endswith("…")


def test_archive_body_file_cleaned_up(tiny_jsonl, marker_home):
    """The temp body file must be unlinked after the CLI call, pass or fail."""
    seen: list = []

    def fake_run(argv, **kwargs):
        opts = _argv_to_opts(argv)
        seen.append(opts["--body-file"])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"content_hash": "d" * 16, "status": "stored"}),
            stderr="",
        )

    with patch("handoff.archive._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.archive.subprocess.run", side_effect=fake_run):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert seen, "CLI was never invoked"
    assert not Path(seen[0]).exists()


def test_compute_body_hash_stable():
    assert compute_body_hash("hello") == compute_body_hash("hello")
    assert compute_body_hash("a") != compute_body_hash("b")
