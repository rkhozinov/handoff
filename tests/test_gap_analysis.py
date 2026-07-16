"""Tests for scripts/gap_analysis.py — the trim-vs-raw signal-loss bench.

scripts/ isn't a package, so load the module by path. Uses a synthetic
transcript + brief (no fixtures needed) so it always runs.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gap_analysis",
    Path(__file__).resolve().parent.parent / "scripts" / "gap_analysis.py",
)
assert _SPEC and _SPEC.loader
gap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gap)


def _entry(role: str, blocks: list[dict]) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": blocks}})


@pytest.fixture
def synthetic(tmp_path) -> tuple[Path, Path]:
    """Raw JSONL with a decision turn, a narration turn, and a tool_result
    carrying an identifier that never reaches the brief."""
    raw = tmp_path / "sess.jsonl"
    lines = [
        _entry("user", [{"type": "text", "text": "fix the archive bug"}]),
        _entry("assistant", [
            {"type": "text",
             "text": "Root cause: archive.py imports a missing SDK. "
                     "Fix: shell out to the memory CLI instead."},
        ]),
        _entry("assistant", [{"type": "text", "text": "Let me check the file."}]),
        _entry("assistant", [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git push"}},
        ]),
        _entry("user", [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "To github:me/repo.git\n  deadbeefcafe1234567890abcdef1234567890abcd  master -> master"},
        ]),
    ]
    raw.write_text("\n".join(lines), encoding="utf-8")

    # Brief keeps the decision turn verbatim, drops the narration + tool_result.
    brief = tmp_path / "sess.md"
    brief.write_text(
        "U: fix the archive bug\n\n"
        "A: Root cause: archive.py imports a missing SDK. "
        "Fix: shell out to the memory CLI instead.\n",
        encoding="utf-8",
    )
    return raw, brief


def test_decision_turn_traceable_narration_dropped(synthetic):
    raw, brief = synthetic
    r = gap.analyze(raw, brief)
    assert r["asst_turns"] == 3
    # Exactly one dropped turn, and it's the narration — not the decision.
    assert len(r["dropped_asst"]) == 1
    assert r["dropped_asst"][0] == "Let me check the file."


def test_tool_result_bytes_counted(synthetic):
    raw, brief = synthetic
    r = gap.analyze(raw, brief)
    assert r["tr_dropped_n"] == 1
    assert r["tr_dropped_bytes"] > 0


def test_missing_identifier_flagged(synthetic):
    raw, brief = synthetic
    r = gap.analyze(raw, brief)
    total, missing = r["id_loss"]["long hashes"]
    # The 40-char SHA lived only in the dropped tool_result → flagged absent.
    assert total == 1
    assert "deadbeefcafe1234567890abcdef1234567890abcd" in missing


def test_short_hash_prefix_counts_as_kept(tmp_path):
    """A long hash is NOT flagged missing if its 8-char prefix is in the brief."""
    sha = "abcdef1234567890abcdef1234567890abcdef12"
    raw = tmp_path / "s.jsonl"
    raw.write_text(_entry("user", [
        {"type": "tool_result", "tool_use_id": "x", "content": f"pushed {sha}"},
    ]), encoding="utf-8")
    brief = tmp_path / "s.md"
    brief.write_text(f"A: landed ({sha[:8]}), done.\n", encoding="utf-8")

    r = gap.analyze(raw, brief)
    _, missing = r["id_loss"]["long hashes"]
    assert missing == []
