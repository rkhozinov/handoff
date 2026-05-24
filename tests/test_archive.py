"""Tests for handoff.archive — trim-before-store + marker write."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _fake_memory_modules(store_instance):
    fake_core = types.ModuleType("memory.core")
    fake_core.MemoryStore = MagicMock(return_value=store_instance)
    fake_pkg = types.ModuleType("memory")
    return {"memory": fake_pkg, "memory.core": fake_core}


def _make_store(content_hash: str = "deadbeef" * 8) -> MagicMock:
    instance = MagicMock()
    instance.store_doc.return_value = {"content_hash": content_hash, "status": "stored"}
    return instance


def test_archive_trims_before_storing(tiny_jsonl, marker_home):
    store = _make_store()
    with patch.dict(sys.modules, _fake_memory_modules(store)):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    assert h == "deadbeef" * 8
    store.store_doc.assert_called_once()
    body = store.store_doc.call_args.kwargs["body"]
    assert "U: what is 2+2" in body
    assert "A: 4" in body
    assert '"type": "user"' not in body


def test_archive_uses_session_archive_type_and_tags(tiny_jsonl, marker_home):
    store = _make_store("a" * 16)
    with patch.dict(sys.modules, _fake_memory_modules(store)):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    kw = store.store_doc.call_args.kwargs
    assert kw["doc_type"] == "session-archive"
    assert "source:auto" in kw["tags"]
    assert "session-archive" in kw["tags"]
    assert "project:myproject" in kw["tags"]


def test_archive_writes_marker_on_success(tiny_jsonl, marker_home):
    content_hash = "f" * 64
    store = _make_store(content_hash)
    with patch.dict(sys.modules, _fake_memory_modules(store)):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    assert h == content_hash
    marker = _marker_path(SESSION_ID)
    assert marker.exists()
    txt = marker.read_text(encoding="utf-8")
    assert txt.startswith("archived\n")
    assert content_hash in txt


def test_archive_includes_metadata(tiny_jsonl, marker_home):
    store = _make_store("x" * 16)
    with patch.dict(sys.modules, _fake_memory_modules(store)):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/myproject")

    kw = store.store_doc.call_args.kwargs
    meta = kw["metadata"]
    assert meta["session_id"] == SESSION_ID[:8]
    assert meta["source_jsonl"] == str(tiny_jsonl)


def test_archive_skips_empty_transcript(tmp_path, marker_home):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    store = _make_store()
    with patch.dict(sys.modules, _fake_memory_modules(store)):
        h = archive_full_session(str(empty), SESSION_ID, "/Users/test/proj")

    assert h is None
    store.store_doc.assert_not_called()
    assert not _marker_path(SESSION_ID).exists()


def test_archive_no_marker_on_import_error(tiny_jsonl, marker_home):
    with patch.dict(sys.modules, {"memory": None, "memory.core": None}):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert h is None
    assert not _marker_path(SESSION_ID).exists()


def test_archive_no_marker_on_store_exception(tiny_jsonl, marker_home):
    store = MagicMock()
    store.store_doc.side_effect = RuntimeError("db error")
    with patch.dict(sys.modules, _fake_memory_modules(store)):
        h = archive_full_session(str(tiny_jsonl), SESSION_ID, "/Users/test/proj")

    assert h is None
    assert not _marker_path(SESSION_ID).exists()


def test_archive_unknown_cwd_falls_back(tiny_jsonl, marker_home):
    store = _make_store("z" * 16)
    with patch.dict(sys.modules, _fake_memory_modules(store)):
        archive_full_session(str(tiny_jsonl), SESSION_ID, "")

    tags = store.store_doc.call_args.kwargs["tags"]
    assert "project:unknown" in tags


def test_archive_truncates_summary_at_500(tmp_path, marker_home):
    jsonl = tmp_path / "big.jsonl"
    long_text = "x" * 5000
    entries = [{"type": "user", "message": {"content": long_text}}]
    jsonl.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

    store = _make_store("y" * 16)
    with patch.dict(sys.modules, _fake_memory_modules(store)):
        archive_full_session(str(jsonl), SESSION_ID, "/Users/test/proj")

    summary = store.store_doc.call_args.kwargs["summary"]
    assert len(summary) <= 501
    assert summary.endswith("…")


def test_compute_body_hash_stable():
    assert compute_body_hash("hello") == compute_body_hash("hello")
    assert compute_body_hash("a") != compute_body_hash("b")
