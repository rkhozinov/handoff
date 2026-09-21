"""`hand review` (idle report), `hand keep`, and sid-prefix resolution —
the manual replacement for the deleted auto-sweep. Nothing here mutates
without an explicit command."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from handoff import db, dbcli
from handoff.lifecycle import parse_frontmatter, render_frontmatter

A = "aaaaaaaa-1111-4111-8111-111111111111"
A2 = "aaaaaaaa-2222-4222-8222-222222222222"  # same 8-char prefix as A
B = "bbbbbbbb-2222-4222-8222-222222222222"
NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(tmp_path, *rows):
    d = tmp_path / "c"
    d.mkdir(exist_ok=True)
    dbf = tmp_path / "s.db"
    with db.connect(dbf) as conn:
        for sid, over in rows:
            fm = {"status": "in_progress", "title": None, "session_id": sid, "cwd": "/repo/one",
                  "created": _iso(40), "last_resumed": None, "completion_signal": "auto-default",
                  "archive_hash": None, "recap": None, "recap_source": None,
                  "hold_note": None, "hold_until": None}
            fm.update(over)
            body = "U: first user line here\n"
            p = d / f"{sid}.md"
            p.write_text(render_frontmatter(fm) + body)
            db.upsert_session(conn, fm=fm, body=body, brief_path=str(p))
    return d, dbf


# --- db.list_idle ------------------------------------------------------------
def test_list_idle_filters_orders_and_computes_days(tmp_path):
    _, dbf = _seed(tmp_path,
                   (A, {"created": _iso(40)}),
                   (B, {"created": _iso(100), "last_resumed": _iso(3)}),   # touched → not idle
                   (A2, {"created": _iso(20), "status": "pending"}),
                   ("cccccccc-3333-4333-8333-333333333333", {"status": "on_hold", "created": _iso(90)}),
                   ("dddddddd-4444-4444-8444-444444444444", {"status": "done", "created": _iso(90)}))
    with db.connect(dbf) as conn:
        rows = db.list_idle(conn, days=14, now=NOW)
    assert [(r["session_id"], r["idle_days"]) for r in rows] == [(A, 40), (A2, 20)]


def test_list_idle_respects_cwd_and_limit(tmp_path):
    _, dbf = _seed(tmp_path, (A, {"cwd": "/repo/one"}), (B, {"cwd": "/repo/two"}))
    with db.connect(dbf) as conn:
        assert [r["session_id"] for r in db.list_idle(conn, days=14, cwd="/repo/two", now=NOW)] == [B]
        assert len(db.list_idle(conn, days=14, limit=1, now=NOW)) == 1


# --- hand review (report only) --------------------------------------------------
def test_review_report_format_and_no_mutation(tmp_path, capsys):
    d, dbf = _seed(tmp_path,
                   (A, {"title": "Fix the widget", "cwd": "/repo/one"}),
                   (B, {"created": _iso(60), "status": "pending", "cwd": "/repo/two"}))
    before = {p.name: p.read_text() for p in d.glob("*.md")}
    rc = dbcli.main(["review", "--days", "14", "--dir", str(d), "--db", str(dbf), "--now", NOW.isoformat()])
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out[0] == ("Idle briefs (open, untouched > 14d): 2 — showing 2, longest idle first. "
                      "Decide per row: hand done|hold|archive|keep <sid8>")
    assert out[1] == ""
    assert out[2] == f"  bbbbbbbb  pending      idle  60d  first user line here  [two]"
    assert out[3] == f"  aaaaaaaa  in_progress  idle  40d  Fix the widget  [one]"
    assert {p.name: p.read_text() for p in d.glob("*.md")} == before


def test_review_limit_and_all(tmp_path, capsys):
    rows = [(f"{i:08x}-0000-4000-8000-000000000000", {"created": _iso(20 + i)}) for i in range(30)]
    d, dbf = _seed(tmp_path, *rows)
    dbcli.main(["review", "--dir", str(d), "--db", str(dbf), "--now", NOW.isoformat()])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("Idle briefs (open, untouched > 14d): 30 — showing 25,")
    assert sum(1 for l in out if l.startswith("  ")) == 25
    dbcli.main(["review", "--all", "--dir", str(d), "--db", str(dbf), "--now", NOW.isoformat()])
    assert sum(1 for l in capsys.readouterr().out.splitlines() if l.startswith("  ")) == 30


def test_review_empty(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (A, {"created": _iso(2)}))
    rc = dbcli.main(["review", "--dir", str(d), "--db", str(dbf), "--now", NOW.isoformat()])
    assert rc == 0 and capsys.readouterr().out.strip() == "No idle briefs (open, untouched > 14d)."


# --- hand keep ------------------------------------------------------------------
def test_keep_stamps_last_resumed_only(tmp_path):
    d, dbf = _seed(tmp_path, (A, {"status": "pending"}))
    ok, msg = dbcli.do_keep(A, compaction_dir=str(d), db_path=dbf)
    assert ok and msg.startswith(f"HANDKEEP_OK sid={A}")
    fm = parse_frontmatter((d / f"{A}.md").read_text())
    assert fm["status"] == "pending" and fm["last_resumed"]
    with db.connect(dbf) as conn:
        row = db.get_session(conn, A)
        assert row["status"] == "pending" and row["last_resumed"] == fm["last_resumed"]
        assert db.list_idle(conn, days=14) == []


# --- sid prefix resolution (CLI layer) -----------------------------------------
def test_prefix_resolves_unique_sid(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (B, {}))
    rc = dbcli.main(["done", "bbbbbbbb", "--dir", str(d), "--db", str(dbf)])
    assert rc == 0 and f"HANDDONE_OK action=closed sid={B}" in capsys.readouterr().out
    assert parse_frontmatter((d / f"{B}.md").read_text())["status"] == "done"


def test_prefix_ambiguous_is_refused(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (A, {}), (A2, {}))
    rc = dbcli.main(["done", "aaaaaaaa", "--dir", str(d), "--db", str(dbf)])
    out = capsys.readouterr().out
    assert rc == 1 and "HANDDONE_ERROR ambiguous prefix aaaaaaaa" in out
    assert parse_frontmatter((d / f"{A}.md").read_text())["status"] == "in_progress"


def test_prefix_unknown_falls_through_to_no_brief(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (A, {}))
    rc = dbcli.main(["done", "ffffffff", "--dir", str(d), "--db", str(dbf)])
    assert rc == 1 and "HANDDONE_ERROR no brief" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", [["hold", "--note", "n"], ["archive"], ["keep"], ["show"], ["on"]])
def test_prefix_accepted_by_other_commands(tmp_path, capsys, cmd):
    d, dbf = _seed(tmp_path, (B, {}))
    rc = dbcli.main([cmd[0], "bbbbbbbb", *cmd[1:], "--dir", str(d), "--db", str(dbf)])
    assert rc == 0, capsys.readouterr().out
    assert B in capsys.readouterr().out
