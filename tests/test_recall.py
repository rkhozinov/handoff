"""Tests for the optional memory-recall integration."""
from __future__ import annotations

from handoff import recall


# ---------- project_tag_from_cwd ----------

def test_project_tag_basename():
    assert recall.project_tag_from_cwd("/Users/x/repos/handoff") == "project:handoff"


def test_project_tag_trailing_slash():
    assert recall.project_tag_from_cwd("/Users/x/repos/foo/") == "project:foo"


# ---------- store_agent_reports ----------

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
