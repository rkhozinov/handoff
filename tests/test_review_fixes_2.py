"""Second batch of 2026-09-21 review regressions: trimmer, cli
ordering, task-id allocation, backfill."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from handoff import cli, tasks
from handoff.extract import is_noise_user_msg
from handoff.trim import NARRATION_MAX_CHARS, render_assistant

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SID = "8e4178e1-7dc1-4352-b13e-98faeb5c1116"


def _assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


# #5 — narration drop is length-gated. Measured on fixtures: every
# NARRATION_RE-matching turn > 500 chars was a diagnosis, ≤ 200 were all
# "let me check…" filler; 200 is the cut.
def test_long_turn_starting_like_narration_is_kept():
    text = "Looking at the diff, the root cause is that the handler returns early. " * 8
    assert len(text) > NARRATION_MAX_CHARS
    assert render_assistant(_assistant(text)) is not None


def test_short_narration_still_dropped():
    assert render_assistant(_assistant("Let me check the ingress config.")) is None


# #6 — generic 1–3 char alternation dropped real replies.
@pytest.mark.parametrize("msg", ["why?", "CI?", "rm", "2", "7"])
def test_short_real_replies_are_signal(msg):
    assert is_noise_user_msg(msg) is False


@pytest.mark.parametrize("msg", ["ok", "yes", "thanks!", "k", "kk", "ty", "np", "yep."])
def test_explicit_acks_are_noise(msg):
    assert is_noise_user_msg(msg) is True






# #10 — a failing agent-report store must not cost the brief.
def test_brief_written_even_when_agent_store_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "Build it"}}) + "\n")

    def boom(*a, **k):
        raise RuntimeError("memory CLI exploded")

    monkeypatch.setattr(cli, "store_agent_reports", boom)
    out = tmp_path / "out"
    rc = cli.main(["--transcript", str(t), "--session-id", SID, "--cwd", "/p",
                   "--no-archive", "--no-db", "--out-dir", str(out)])
    assert rc == 0
    assert (out / f"{SID}.md").is_file()


def test_store_agent_reports_tolerates_non_dict_json(monkeypatch):
    from handoff import recall
    from types import SimpleNamespace

    monkeypatch.setattr(recall, "_memory_bin", lambda: "/bin/memory")
    monkeypatch.setattr(recall.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="[1, 2]", stderr=""))
    reports = [("d", "scout", "x" * 300)]
    assert recall.store_agent_reports(reports, project_tag="p") == 0


# #11 — a manifest entry whose destination task is gone no longer blocks re-import.
def test_manifest_entry_pointing_at_missing_dest_task_is_ignored():
    src = tasks.make_bundle(SID, [{"id": "1", "subject": "s", "description": "d", "status": "pending",
                                   "blocks": [], "blockedBy": []}])
    plan = tasks.plan_import(src, [], mode="merge", only_open=False,
                             already_imported={tasks.manifest_key(SID, "1"): "5"})
    assert (plan.imported, plan.skipped) == (1, 0)


# #12 — id floor honours .highwatermark and malformed-but-present files.
def test_read_id_floor_uses_highwatermark_and_unreadable_files(tmp_path):
    d = tmp_path / "list"
    d.mkdir()
    (d / "1.json").write_text("{}")
    (d / "7.json").write_text("{not json")
    (d / ".highwatermark").write_text("9\n")
    assert tasks.read_id_floor(d) == 9


def test_next_id_never_below_floor():
    assert tasks.next_id([{"id": "3"}], floor=9) == 10
    assert tasks.next_id([{"id": "12"}], floor=9) == 13


def test_plan_import_allocates_above_floor():
    src = tasks.make_bundle(SID, [{"id": "1", "subject": "s", "description": "d", "status": "pending",
                                   "blocks": [], "blockedBy": []}])
    plan = tasks.plan_import(src, [], mode="merge", only_open=False, already_imported={}, id_floor=9)
    assert plan.id_map["1"] == "10"


# #16 — backfill never guesses `done` from body text (its own docstring).
def test_backfill_never_marks_done_from_body():
    import backfill_status

    body = "U: start\nU: the fixed-width bug is still there\n"
    assert backfill_status.detect_status_from_body(body)[0] != "done"
