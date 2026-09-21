"""`hand off` / `hand on --restore` — the logic that used to live in the
/hand:off, /hand:hold and /hand:on bash blocks. Contracts pinned verbatim
from running those blocks on 2026-09-21 (see spec-commands-to-python.md)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from handoff import db, dbcli, tasks
from handoff.lifecycle import parse_frontmatter, render_frontmatter

A = "aaaaaaaa-1111-4111-8111-111111111111"
B = "bbbbbbbb-2222-4222-8222-222222222222"
CUR = "dddddddd-4444-4444-8444-444444444444"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects" / "-x").mkdir(parents=True)
    (tmp_path / ".claude" / "compaction").mkdir(parents=True)
    return tmp_path


def _transcript(home: Path, sid: str, *, last_user="build widget") -> Path:
    rows = [
        {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user", "content": "build widget"}},
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "Built it; tests pass."}]}},
        {"type": "user", "cwd": "/tmp/proj", "message": {"role": "user", "content": last_user}},
    ]
    p = home / ".claude" / "projects" / "-x" / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _off(home, sid, **kw):
    opts = dict(cwd="/somewhere/else", recap="Goal: widget. Next: ship.", hold=None, until=None,
                projects_dir=str(home / ".claude" / "projects"),
                compaction_dir=str(home / ".claude" / "compaction"),
                db_path=str(home / "s.db"), no_archive=True, no_agent_store=True,
                tasks_root=str(home / ".claude" / "tasks"),
                bundle_dir=str(home / ".claude" / "compaction" / "tasks"))
    opts.update(kw)
    return dbcli.do_off(sid, **opts)


# --- hand off ------------------------------------------------------------------
def test_off_block_matches_contract(home):
    _transcript(home, A)
    rc, lines = _off(home, A)
    brief = home / ".claude" / "compaction" / f"{A}.md"
    assert rc == 0
    assert lines[0] == "HANDOFF_OK"
    assert lines[1] == "  recap:      Goal: widget. Next: ship."
    assert lines[2] == f"  session_id: {A}"
    assert lines[3] == f"  brief:      {brief}"
    assert lines[4].startswith("  size:       brief=") and "raw=" in lines[4] and "saved=" in lines[4]
    assert lines[5] == "  status:     in_progress (auto-default)"
    assert lines[6] == "  tasks:      none in this session"
    assert lines[7] == "  db:         ~/.claude/compaction/sessions.db (row upserted)"
    assert lines[8] == ""
    assert lines[9:13] == [
        "Restore with either:",
        f"  /hand:on {A}",
        f"  /hand:on {brief}",
        "  park it:   /hand:hold <why> [--until YYYY-MM-DD]",
    ]
    fm = parse_frontmatter(brief.read_text())
    assert fm["cwd"] == "/tmp/proj"  # transcript cwd beats the shell's
    assert fm["recap"] == "Goal: widget. Next: ship."
    with db.connect(home / "s.db") as conn:
        assert db.get_session(conn, A)["status"] == "in_progress"


def test_off_recap_with_shell_metachars_is_data(home):
    _transcript(home, A)
    rc, lines = _off(home, A, recap="it's 'quoted'; echo INJECTED")
    assert rc == 0
    assert parse_frontmatter((home / ".claude/compaction" / f"{A}.md").read_text())["recap"] == "it's 'quoted'; echo INJECTED"


def test_off_done_session_prints_done_tail(home):
    _transcript(home, A, last_user="merged, thanks")
    rc, lines = _off(home, A)
    assert lines[5] == "  status:     done (auto-user-msg)"
    assert lines[9] == "Detector marked this session DONE — it's hidden from the"
    assert f"To resume anyway: /hand:on {A}" in lines[10]
    assert lines[11] == f"To revive permanently: /hand:done {A} --reopen"


def test_off_missing_transcript(home):
    rc, lines = _off(home, B)
    assert rc == 1
    assert lines == [f"HANDOFF_ERROR sid={B} transcript="]


def test_off_bad_sid(home):
    rc, lines = _off(home, "../x")
    assert rc == 1 and lines[0].startswith("HANDOFF_ERROR")


def test_off_pipeline_failure_is_reported_not_raised(home, monkeypatch):
    _transcript(home, A)

    def boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr("handoff.cli.run", boom)
    rc, lines = _off(home, A)
    assert rc == 1
    assert lines[0].startswith("HANDOFF_ERROR: handoff.cli failed")


def test_off_exports_tasks_when_present(home):
    _transcript(home, A)
    tdir = home / ".claude" / "tasks" / f"session-{A[:8]}"
    tdir.mkdir(parents=True)
    (tdir / "1.json").write_text(json.dumps({"id": "1", "subject": "s", "description": "d",
                                             "status": "pending", "blocks": [], "blockedBy": []}))
    rc, lines = _off(home, A)
    assert lines[6] == "  tasks:      exported (1 tasks)"
    assert (home / ".claude" / "compaction" / "tasks" / f"{A}.json").is_file()


def test_off_task_export_failure_is_best_effort(home, monkeypatch):
    _transcript(home, A)
    monkeypatch.setattr(dbcli, "do_tasks_export", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    rc, lines = _off(home, A)
    assert rc == 0 and lines[6] == "  tasks:      skipped"


def test_off_with_hold(home):
    _transcript(home, A)
    rc, lines = _off(home, A, hold="wait for * review", until="2026-10-01")
    assert rc == 0
    assert any(l.startswith(f"HANDHOLD_OK action=held sid={A} until=2026-10-01") for l in lines)
    assert lines[-1] == f"resume: cd /tmp/proj && claude --resume {A}   |  /hand:on {A}"
    fm = parse_frontmatter((home / ".claude/compaction" / f"{A}.md").read_text())
    assert (fm["status"], fm["hold_note"], fm["hold_until"]) == ("on_hold", "wait for * review", "2026-10-01")


def test_off_with_hold_bad_date(home):
    _transcript(home, A)
    rc, lines = _off(home, A, hold="n", until="soon")
    assert rc == 1
    assert (home / ".claude/compaction" / f"{A}.md").is_file()  # snapshot kept
    assert any("HANDHOLD_ERROR reason=bad-date" in l for l in lines)


def test_off_cli_recap_stdin(home, monkeypatch, capsys):
    _transcript(home, A)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("from stdin 'q'\n"))
    rc = dbcli.main(["off", A, "--cwd", "/x", "--recap-stdin", "--no-archive", "--no-agent-store",
                     "--projects", str(home / ".claude/projects"), "--dir", str(home / ".claude/compaction"),
                     "--db", str(home / "s.db"), "--tasks-dir", str(home / "t"),
                     "--bundle-dir", str(home / "b")])
    assert rc == 0
    assert "  recap:      from stdin 'q'" in capsys.readouterr().out
    assert parse_frontmatter((home / ".claude/compaction" / f"{A}.md").read_text())["recap"] == "from stdin 'q'"


# --- hand on --restore ----------------------------------------------------------
def _brief(home, sid, status="in_progress", **over):
    fm = {"status": status, "title": f"t-{sid[:4]}", "session_id": sid, "cwd": "/tmp/proj",
          "created": "2026-06-04T10:00:00Z", "last_resumed": None,
          "completion_signal": "auto-default", "archive_hash": None,
          "recap": "Goal: widget. Next: ship.", "recap_source": "llm",
          "hold_note": None, "hold_until": None}
    fm.update(over)
    p = home / ".claude" / "compaction" / f"{sid}.md"
    p.write_text(render_frontmatter(fm) + "U: build widget\n")
    with db.connect(home / "s.db") as conn:
        db.upsert_session(conn, fm=fm, body="U: build widget\n", brief_path=str(p))
    return p


def _on(home, args, *, current=CUR, show_all=False):
    return dbcli.do_on_restore(
        args, current_sid=current, show_all=show_all,
        compaction_dir=str(home / ".claude" / "compaction"), db_path=str(home / "s.db"),
        tasks_root=str(home / ".claude" / "tasks"),
        bundle_dir=str(home / ".claude" / "compaction" / "tasks"))


def test_on_resolution_contract(home):
    pa = _brief(home, A)
    pb = _brief(home, B, status="done")
    rc, lines = _on(home, [A, "nope", str(pb)])
    assert rc == 0
    assert lines[:5] == [
        f"BRIEF_PATH={pa}", "BRIEF_STATUS=in_progress",
        f"BRIEF_MISSING arg=nope sid={CUR} show_all=0",
        f"BRIEF_PATH={pb}", "BRIEF_STATUS=done",
    ]
    assert any(l.startswith(f"HANDON_OK sid={A}") for l in lines)
    assert any(l.startswith(f"HANDON_DONE sid={B}") for l in lines)
    assert not any(l.startswith("No brief found") for l in lines)
    assert parse_frontmatter(pa.read_text())["last_resumed"]
    assert parse_frontmatter(pb.read_text())["status"] == "done"


def test_on_no_args_uses_current_sid(home):
    p = _brief(home, A)
    rc, lines = _on(home, [], current=A)
    assert rc == 0 and lines[:2] == [f"BRIEF_PATH={p}", "BRIEF_STATUS=in_progress"]


def test_on_nothing_resolved_prints_picker_done_hidden(home):
    _brief(home, A)
    _brief(home, B, status="done", created="2026-07-01T00:00:00Z")
    rc, lines = _on(home, [])
    assert rc == 1
    assert lines[0] == f"BRIEF_MISSING arg= sid={CUR} show_all=0"
    assert lines[1] == "No brief found. Open briefs (newest first, done hidden — pass --all to include). Reply with the number or session id."
    assert lines[2] == ""
    assert lines[3].startswith(f" 1. {A}  [in_progress]  (2026-06-04T10:00:00Z, ") and lines[3].endswith(" bytes)")
    assert lines[4] == "    cwd:  /tmp/proj"
    assert lines[5] == "    goal: Goal: widget. Next: ship."
    assert lines[6] == ""
    assert not any(B in l for l in lines)


def test_on_picker_all_includes_done_newest_first_hides_nothing(home):
    _brief(home, A)
    _brief(home, B, status="done", created="2026-07-01T00:00:00Z")
    rc, lines = _on(home, [], show_all=True)
    assert lines[0] == f"BRIEF_MISSING arg= sid={CUR} show_all=1"
    assert lines[1] == "No brief found. ALL recent briefs (newest first, including done). Reply with the number or session id."
    assert lines[3].startswith(f" 1. {B}  [done]")
    assert lines[7].startswith(f" 2. {A}  [in_progress]")


def test_on_picker_hides_archived_unless_all(home):
    """Deliberate divergence from the old ls-t/awk picker, which showed archived."""
    _brief(home, A, status="archived", completion_signal="manual")
    rc, lines = _on(home, [])
    assert not any(A in l for l in lines)
    rc, lines = _on(home, [], show_all=True)
    assert any(A in l for l in lines)


def test_on_picker_caps_at_10(home):
    for i in range(12):
        _brief(home, f"{i:08x}-0000-4000-8000-000000000000", created=f"2026-06-{i+1:02d}T00:00:00Z")
    rc, lines = _on(home, [])
    rows = [l for l in lines if re.match(r"^ ?\d+\. [0-9a-f]{8}-", l)]
    assert len(rows) == 10
    assert rows[0].startswith(" 1. ") and rows[9].startswith("10. ")  # %2d, as the old awk printf


def test_on_merges_task_bundle_into_current(home):
    _brief(home, A)
    bdir = home / ".claude" / "compaction" / "tasks"
    bdir.mkdir(parents=True)
    bundle = tasks.make_bundle(A, [{"id": "1", "subject": "s", "description": "d", "status": "pending",
                                    "blocks": [], "blockedBy": []}])
    (bdir / f"{A}.json").write_text(json.dumps(bundle))
    rc, lines = _on(home, [A])
    assert any(l.startswith(f"HANDTASKS_OK op=import source={A[:8]} dest={CUR[:8]} imported=1") for l in lines)
    found, _ = tasks.read_tasks(home / ".claude" / "tasks" / f"session-{CUR[:8]}")
    assert [t["subject"] for t in found] == ["s"]


def test_on_missing_bundle_is_silent(home):
    _brief(home, A)
    rc, lines = _on(home, [A])
    assert not any("HANDTASKS" in l for l in lines)


def test_on_cli_restore_flag(home, capsys):
    p = _brief(home, A)
    rc = dbcli.main(["on", A, "--restore", "--current", CUR, "--dir", str(home / ".claude/compaction"),
                     "--db", str(home / "s.db"), "--tasks-dir", str(home / "t"),
                     "--bundle-dir", str(home / "b")])
    out = capsys.readouterr().out
    assert rc == 0 and f"BRIEF_PATH={p}" in out and f"HANDON_OK sid={A}" in out


def test_on_without_restore_unchanged(home, capsys):
    _brief(home, A)
    rc = dbcli.main(["on", A, "--dir", str(home / ".claude/compaction"), "--db", str(home / "s.db")])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith(f"HANDON_OK sid={A}") and "BRIEF_PATH" not in out
