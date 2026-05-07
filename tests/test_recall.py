"""Tests for the optional memory-recall integration."""
from __future__ import annotations

import json

from compaction import recall


# ---------- build_query ----------

def test_build_query_combines_decisions_and_signals():
    sig = ["fix the bug", "use postgres instead"]
    dec = ["use postgres instead"]
    q = recall.build_query(sig, dec)
    # Decisions come first, deduped against signals
    assert "use postgres instead" in q
    assert "fix the bug" in q


def test_build_query_truncates_at_max():
    long_msg = "x" * 500
    q = recall.build_query([long_msg], [], max_chars=100)
    assert len(q) <= 100


def test_build_query_empty():
    assert recall.build_query([], []) == ""


# ---------- project_tag_from_cwd ----------

def test_project_tag_basename():
    assert recall.project_tag_from_cwd("/Users/x/repos/claude-compaction") == "project:claude-compaction"


def test_project_tag_trailing_slash():
    assert recall.project_tag_from_cwd("/Users/x/repos/foo/") == "project:foo"


# ---------- search_memories (subprocess-faked) ----------

def test_search_memories_no_binary_returns_empty(monkeypatch):
    monkeypatch.setattr(recall.shutil, "which", lambda _: None)
    assert recall.search_memories("query") == []


def test_search_memories_empty_query_returns_empty():
    assert recall.search_memories("") == []


def test_search_memories_parses_json(monkeypatch):
    fake_out = json.dumps([
        {"content_hash": "abc123def456789a", "memory_type": "learning",
         "score": 0.9, "content": "useful fact"},
    ])

    class FakeProc:
        returncode = 0
        stdout = fake_out
        stderr = ""

    monkeypatch.setattr(recall.shutil, "which", lambda _: "/fake/memory")
    monkeypatch.setattr(recall.subprocess, "run", lambda *a, **kw: FakeProc())
    out = recall.search_memories("query", project_tag="project:foo")
    assert len(out) == 1
    assert out[0]["content_hash"].startswith("abc")


def test_search_memories_swallows_subprocess_failure(monkeypatch):
    def boom(*a, **kw):
        raise OSError("not found")

    monkeypatch.setattr(recall.shutil, "which", lambda _: "/fake/memory")
    monkeypatch.setattr(recall.subprocess, "run", boom)
    assert recall.search_memories("query") == []


def test_search_memories_swallows_garbage_json(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(recall.shutil, "which", lambda _: "/fake/memory")
    monkeypatch.setattr(recall.subprocess, "run", lambda *a, **kw: FakeProc())
    assert recall.search_memories("query") == []


# ---------- format_memory_line ----------

def test_format_memory_line_basic():
    mem = {
        "content_hash": "abc123def456789",
        "memory_type": "decision",
        "score": 0.85,
        "content": "We chose Postgres because of jsonb support.",
    }
    line = recall.format_memory_line(mem)
    assert "abc123def456" in line
    assert "[decision]" in line
    assert "0.85" in line
    assert "Postgres" in line


def test_store_agent_reports_no_binary_returns_zero(monkeypatch):
    monkeypatch.setattr(recall.shutil, "which", lambda _: None)
    n = recall.store_agent_reports(
        [("desc", "subtype", "x" * 500)], project_tag="project:foo"
    )
    assert n == 0


def test_store_agent_reports_skips_short_stubs(monkeypatch):
    calls = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(recall.shutil, "which", lambda _: "/fake/memory")
    monkeypatch.setattr(recall.subprocess, "run", fake_run)
    n = recall.store_agent_reports([("d", "s", "tiny")], project_tag="project:foo")
    assert n == 0
    assert calls == []


def test_store_agent_reports_passes_tags_and_type(monkeypatch):
    calls = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(recall.shutil, "which", lambda _: "/fake/memory")
    monkeypatch.setattr(recall.subprocess, "run", fake_run)
    n = recall.store_agent_reports(
        [("research X", "explore", "y" * 800)], project_tag="project:foo"
    )
    assert n == 1
    cmd = calls[0]
    # The body bullet starts with `[Agent Report: research X]`.
    assert "[Agent Report: research X]" in cmd[2]
    # Tags include project, source, agent.
    tags_idx = cmd.index("--tags") + 1
    tags = cmd[tags_idx]
    assert "project:foo" in tags
    assert "source:agent-report" in tags
    assert "agent:explore" in tags
    # Type is learning.
    assert cmd[cmd.index("--type") + 1] == "learning"


def test_store_agent_reports_swallows_subprocess_failure(monkeypatch):
    monkeypatch.setattr(recall.shutil, "which", lambda _: "/fake/memory")
    monkeypatch.setattr(
        recall.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("nope"))
    )
    n = recall.store_agent_reports([("d", "s", "x" * 500)], project_tag="project:foo")
    assert n == 0


def test_format_memory_line_truncates_long_body():
    mem = {"content_hash": "h" * 16, "content": "x" * 500, "memory_type": "learning"}
    line = recall.format_memory_line(mem, max_chars=100)
    # body field is bullet body, line itself is a bit longer; just check it ends in ...
    assert line.rstrip().endswith("...")
