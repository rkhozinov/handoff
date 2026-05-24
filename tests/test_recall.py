"""Tests for the optional memory-recall integration."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from handoff import recall


# ---------- project_tag_from_cwd ----------

def test_project_tag_basename():
    assert recall.project_tag_from_cwd("/Users/x/repos/handoff") == "project:handoff"


def test_project_tag_trailing_slash():
    assert recall.project_tag_from_cwd("/Users/x/repos/foo/") == "project:foo"


# ---------- helpers ----------

def _fake_memory_modules(store_instance):
    """Return a sys.modules patch dict that injects a fake memory.core."""
    fake_core = types.ModuleType("memory.core")
    fake_core.MemoryStore = MagicMock(return_value=store_instance)
    fake_pkg = types.ModuleType("memory")
    return {"memory": fake_pkg, "memory.core": fake_core}


# ---------- store_agent_reports ----------

def test_store_agent_reports_no_sdk_returns_zero():
    with patch.dict(sys.modules, {"memory": None, "memory.core": None}):
        n = recall.store_agent_reports(
            [("desc", "subtype", "x" * 500)], project_tag="project:foo"
        )
    assert n == 0


def test_store_agent_reports_skips_short_stubs():
    fake_instance = MagicMock()
    fake_instance.store.return_value = {"status": "stored", "content_hash": "abc"}
    with patch.dict(sys.modules, _fake_memory_modules(fake_instance)):
        n = recall.store_agent_reports([("d", "s", "tiny")], project_tag="project:foo")
    assert n == 0
    fake_instance.store.assert_not_called()


def test_store_agent_reports_passes_tags_and_type():
    fake_instance = MagicMock()
    fake_instance.store.return_value = {"status": "stored", "content_hash": "abc123"}
    with patch.dict(sys.modules, _fake_memory_modules(fake_instance)):
        n = recall.store_agent_reports(
            [("research X", "explore", "y" * 800)], project_tag="project:foo"
        )
    assert n == 1
    fake_instance.store.assert_called_once()
    call_kw = fake_instance.store.call_args.kwargs
    assert "[Agent Report: research X]" in call_kw["content"]
    assert "project:foo" in call_kw["tags"]
    assert "source:agent-report" in call_kw["tags"]
    assert "agent:explore" in call_kw["tags"]
    assert call_kw["memory_type"] == "learning"


def test_store_agent_reports_swallows_store_exception():
    fake_instance = MagicMock()
    fake_instance.store.side_effect = RuntimeError("db locked")
    with patch.dict(sys.modules, _fake_memory_modules(fake_instance)):
        n = recall.store_agent_reports([("d", "s", "x" * 500)], project_tag="project:foo")
    assert n == 0
