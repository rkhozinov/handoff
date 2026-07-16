"""Tests for the optional memory-recall integration.

store_agent_reports shells out to the `memory` CLI (`memory store -`, content
via stdin) rather than importing a `memory` SDK. Tests mock `_memory_bin`
(binary resolution) and `subprocess.run` (the CLI call).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from handoff import recall


FAKE_BIN = "/fake/bin/memory"


# ---------- project_tag_from_cwd ----------

def test_project_tag_basename():
    assert recall.project_tag_from_cwd("/Users/x/repos/handoff") == "project:handoff"


def test_project_tag_trailing_slash():
    assert recall.project_tag_from_cwd("/Users/x/repos/foo/") == "project:foo"


# ---------- helpers ----------

def _capture_calls(calls: list, status: str = "stored", returncode: int = 0):
    """subprocess.run replacement recording {argv, input(=stdin body)} per call."""
    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "input": kwargs.get("input", "")})
        return SimpleNamespace(
            returncode=returncode,
            stdout='{"status": "%s", "content_hash": "abc123"}' % status,
            stderr="",
        )
    return fake_run


# ---------- store_agent_reports ----------

def test_store_agent_reports_no_cli_returns_zero():
    with patch("handoff.recall._memory_bin", return_value=None):
        n = recall.store_agent_reports(
            [("desc", "subtype", "x" * 500)], project_tag="project:foo"
        )
    assert n == 0


def test_store_agent_reports_skips_short_stubs():
    calls: list = []
    with patch("handoff.recall._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.recall.subprocess.run", side_effect=_capture_calls(calls)):
        n = recall.store_agent_reports([("d", "s", "tiny")], project_tag="project:foo")
    assert n == 0
    assert calls == []


def test_store_agent_reports_passes_tags_and_type():
    calls: list = []
    with patch("handoff.recall._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.recall.subprocess.run", side_effect=_capture_calls(calls)):
        n = recall.store_agent_reports(
            [("research X", "explore", "y" * 800)], project_tag="project:foo"
        )
    assert n == 1
    assert len(calls) == 1
    argv, body = calls[0]["argv"], calls[0]["input"]
    assert argv[:3] == [FAKE_BIN, "store", "-"]
    assert argv[argv.index("--type") + 1] == "learning"
    tags = argv[argv.index("--tags") + 1].split(",")
    assert "project:foo" in tags
    assert "source:agent-report" in tags
    assert "agent:explore" in tags
    assert "[Agent Report: research X]" in body


def test_store_agent_reports_swallows_nonzero_exit():
    calls: list = []
    with patch("handoff.recall._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.recall.subprocess.run", side_effect=_capture_calls(calls, returncode=1)):
        n = recall.store_agent_reports([("d", "s", "x" * 500)], project_tag="project:foo")
    assert n == 0


def test_store_agent_reports_swallows_run_exception():
    def boom(argv, **kwargs):
        raise OSError("no such binary")
    with patch("handoff.recall._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.recall.subprocess.run", side_effect=boom):
        n = recall.store_agent_reports([("d", "s", "x" * 500)], project_tag="project:foo")
    assert n == 0


def test_store_agent_reports_rejected_not_counted():
    calls: list = []
    with patch("handoff.recall._memory_bin", return_value=FAKE_BIN), \
         patch("handoff.recall.subprocess.run", side_effect=_capture_calls(calls, status="rejected")):
        n = recall.store_agent_reports([("d", "s", "x" * 500)], project_tag="project:foo")
    assert n == 0
