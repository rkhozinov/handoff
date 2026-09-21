"""`hand assess` — the pack `/hand:assess` feeds to one read-only agent per
held brief (spec-assess.md). A report: it never writes."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from handoff import db, dbcli, tasks
from handoff.lifecycle import render_frontmatter

H1 = "aaaaaaaa-1111-4111-8111-111111111111"
H1B = "aaaaaaaa-2222-4222-8222-222222222222"  # same 8-char prefix as H1
H2 = "bbbbbbbb-2222-4222-8222-222222222222"
OPEN = "cccccccc-3333-4333-8333-333333333333"  # in_progress, not a hold
NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(tmp_path, *rows, write_brief=True):
    d = tmp_path / "c"
    d.mkdir(exist_ok=True)
    dbf = tmp_path / "s.db"
    with db.connect(dbf) as conn:
        for sid, over in rows:
            fm = {"status": "on_hold", "title": None, "session_id": sid, "cwd": "/repo/one",
                  "created": _iso(40), "last_resumed": None, "completion_signal": "manual",
                  "archive_hash": None, "recap": None, "recap_source": None,
                  "hold_note": None, "hold_until": None}
            fm.update(over)
            body = "U: first user line here\n"
            p = d / f"{sid}.md"
            if write_brief:
                p.write_text(render_frontmatter(fm) + body)
            db.upsert_session(conn, fm=fm, body=body, brief_path=str(p))
    return d, dbf


def _run(d, dbf, capsys, *argv):
    rc = dbcli.main(["assess", "--dir", str(d), "--db", str(dbf), "--bundle-dir", str(d / "bundles"), *argv])
    return rc, capsys.readouterr().out


def test_assess_default_is_holds_due_first_capped(tmp_path, capsys):
    rows = [(f"{i:08x}-1111-4111-8111-111111111111", {"created": _iso(i)}) for i in range(1, 13)]
    rows[4] = (rows[4][0], {"created": _iso(5), "hold_until": "2026-09-01"})  # overdue → first
    d, dbf = _seed(tmp_path, *rows, (OPEN, {"status": "in_progress"}))
    rc, out = _run(d, dbf, capsys)
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == (
        "Assess held briefs: 12 — showing 10. One read-only agent per row; "
        "decide with hand done|keep|hold <sid8>."
    )
    heads = [l for l in lines if l.startswith("ASSESS ")]
    assert len(heads) == 10
    assert heads[0].startswith("ASSESS 00000005  on_hold  ")
    assert "cccccccc" not in out  # in_progress rows are not holds


def test_assess_all_lifts_cap(tmp_path, capsys):
    rows = [(f"{i:08x}-1111-4111-8111-111111111111", {}) for i in range(1, 13)]
    d, dbf = _seed(tmp_path, *rows)
    _, out = _run(d, dbf, capsys, "--all")
    assert out.splitlines()[0].startswith("Assess held briefs: 12 — showing 12.")
    assert out.count("ASSESS ") == 12


def test_assess_row_shape(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H2, {"title": "fix the widget", "cwd": "/repo/two",
                                   "hold_note": "waiting on PR 12", "hold_until": "2026-10-01"}))
    _, out = _run(d, dbf, capsys)
    assert out.splitlines()[2:8] == [
        "ASSESS bbbbbbbb  on_hold  fix the widget  [two]",
        "    cwd:    /repo/two",
        f"    brief:  {d / f'{H2}.md'}",
        "    note:   waiting on PR 12",
        "    due:    2026-10-01",
        "",
    ]


def test_assess_label_falls_back_to_recap_then_sid8(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {"recap": "Goal: ship it"}), (H2, {}))
    _, out = _run(d, dbf, capsys)
    assert "ASSESS aaaaaaaa  on_hold  Goal: ship it  [one]" in out
    assert "ASSESS bbbbbbbb  on_hold  bbbbbbbb  [one]" in out


def test_assess_missing_brief_is_reported_not_fatal(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}), write_brief=False)
    rc, out = _run(d, dbf, capsys)
    assert rc == 0
    assert "    brief:  MISSING" in out


def test_assess_open_tasks_from_bundle(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    bdir = d / "bundles"
    bdir.mkdir()
    bundle = tasks.make_bundle(H1, [
        {"id": "1", "subject": "done thing", "description": "", "status": "completed", "blocks": [], "blockedBy": []},
        {"id": "2", "subject": "write the spec", "description": "", "status": "pending", "blocks": [], "blockedBy": []},
        {"id": "3", "subject": "run it", "description": "", "status": "in_progress", "blocks": [], "blockedBy": []},
    ])
    tasks.bundle_path(H1, base=bdir).write_text(json.dumps(bundle))
    _, out = _run(d, dbf, capsys)
    assert "    tasks:  2 open — write the spec; run it" in out


def test_assess_no_tasks_line_when_all_completed_or_no_bundle(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    _, out = _run(d, dbf, capsys)
    assert "tasks:" not in out


def test_assess_explicit_sids_prefix_any_status_uncapped(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H2, {}), (OPEN, {"status": "in_progress"}))
    rc, out = _run(d, dbf, capsys, "bbbbbbbb", OPEN, "--limit", "1")
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == "Assess briefs: 2."
    assert [l.split()[1:3] for l in lines if l.startswith("ASSESS ")] == [
        ["bbbbbbbb", "on_hold"], ["cccccccc", "in_progress"],
    ]


def test_assess_ambiguous_prefix_refused(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}), (H1B, {}))
    rc, out = _run(d, dbf, capsys, "aaaaaaaa")
    assert rc == 1
    assert out.startswith("HANDASSESS_ERROR ambiguous prefix")
    assert "ASSESS " not in out


def test_assess_unknown_sid_refused(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    rc, out = _run(d, dbf, capsys, "dddddddd-4444-4444-8444-444444444444")
    assert rc == 1
    assert out.startswith("HANDASSESS_ERROR no brief sid=dddddddd-4444-4444-8444-444444444444")


def test_assess_no_holds(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (OPEN, {"status": "in_progress"}))
    rc, out = _run(d, dbf, capsys)
    assert rc == 0
    assert out == "No held briefs.\n"


def test_assess_writes_nothing(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {"hold_note": "n"}), (H2, {}))
    before_rows = sqlite3.connect(dbf).execute("SELECT * FROM sessions ORDER BY session_id").fetchall()
    before_files = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in d.iterdir()}
    _run(d, dbf, capsys)
    _run(d, dbf, capsys, "aaaaaaaa")
    assert sqlite3.connect(dbf).execute("SELECT * FROM sessions ORDER BY session_id").fetchall() == before_rows
    assert {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in d.iterdir()} == before_files
