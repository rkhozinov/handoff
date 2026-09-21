"""on_hold: the curated "come back later" shelf. Status + hold_note +
hold_until, sticky across /hand:off, exempt from the stale sweep and the
archive prune, released by /hand:on."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from handoff import db, dbcli
from handoff.lifecycle import (
    is_due,
    is_stale,
    parse_frontmatter,
    render_frontmatter,
    resolve_frontmatter,
)

SID = "8e4178e1-7dc1-4352-b13e-98faeb5c1116"
SID2 = "11111111-2222-4333-8444-555555555555"


def _fm(**over):
    fm = {
        "status": "in_progress",
        "title": "t",
        "session_id": SID,
        "cwd": "/Users/x/repos/handoff",
        "created": "2026-06-04T10:00:00Z",
        "last_resumed": None,
        "completion_signal": "auto-default",
        "archive_hash": "abc",
        "recap": "Goal: x. Next: y.",
        "recap_source": "llm",
    }
    fm.update(over)
    return fm


def _setup(tmp_path, **fm_over) -> tuple[Path, Path]:
    d = tmp_path / "compaction"
    d.mkdir()
    fm = _fm(**fm_over)
    (d / f"{fm['session_id']}.md").write_text(render_frontmatter(fm) + "U: hello\n", encoding="utf-8")
    dbf = tmp_path / "sessions.db"
    with db.connect(dbf) as conn:
        db.upsert_session(conn, fm=fm, body="U: hello\n")
    return d, dbf


# --- lifecycle --------------------------------------------------------------
def test_hold_keys_round_trip_frontmatter():
    fm = _fm(status="on_hold", hold_note="wait for PR 12", hold_until="2026-10-01")
    back = parse_frontmatter(render_frontmatter(fm))
    assert back["status"] == "on_hold"
    assert back["hold_note"] == "wait for PR 12"
    assert back["hold_until"] == "2026-10-01"


def test_hold_is_sticky_across_handoff():
    existing = _fm(status="on_hold", completion_signal="manual", hold_note="n", hold_until="2026-10-01")
    out = resolve_frontmatter(
        session_id=SID, cwd="/x", detected_status="in_progress", detected_signal="auto-default",
        archive_hash="h", existing=existing,
    )
    assert out["status"] == "on_hold"
    assert out["completion_signal"] == "manual"
    assert out["hold_note"] == "n"
    assert out["hold_until"] == "2026-10-01"


def test_hold_is_never_stale():
    fm = _fm(status="on_hold", completion_signal="manual", created="2025-01-01T00:00:00Z")
    assert is_stale(fm, now=datetime(2026, 9, 1, tzinfo=timezone.utc)) is False


@pytest.mark.parametrize(
    "status,until,expected",
    [
        ("on_hold", "2026-09-21", True),   # due today
        ("on_hold", "2026-09-20", True),   # overdue
        ("on_hold", "2026-09-22", False),  # tomorrow
        ("on_hold", None, False),          # no deadline
        ("in_progress", "2026-09-20", False),  # released → not due
        ("on_hold", "garbage", False),
    ],
)
def test_is_due(status, until, expected):
    fm = _fm(status=status, hold_until=until)
    assert is_due(fm, today=date(2026, 9, 21)) is expected


# --- dbcli.do_hold ----------------------------------------------------------
def test_do_hold_writes_file_and_db(tmp_path):
    d, dbf = _setup(tmp_path)
    ok, msg = dbcli.do_hold(SID, note="wait for PR", until="2026-10-01", release=False,
                            compaction_dir=str(d), db_path=dbf)
    assert ok and msg.startswith("HANDHOLD_OK"), msg
    fm = parse_frontmatter((d / f"{SID}.md").read_text())
    assert (fm["status"], fm["completion_signal"], fm["hold_note"], fm["hold_until"]) == (
        "on_hold", "manual", "wait for PR", "2026-10-01")
    with db.connect(dbf) as conn:
        row = db.get_session(conn, SID)
    assert (row["status"], row["hold_note"], row["hold_until"]) == ("on_hold", "wait for PR", "2026-10-01")


def test_do_hold_rejects_bad_date(tmp_path):
    d, dbf = _setup(tmp_path)
    ok, msg = dbcli.do_hold(SID, note="x", until="next week", release=False,
                            compaction_dir=str(d), db_path=dbf)
    assert not ok and "bad-date" in msg
    assert parse_frontmatter((d / f"{SID}.md").read_text())["status"] == "in_progress"


def test_do_hold_release_keeps_note_clears_until(tmp_path):
    d, dbf = _setup(tmp_path, status="on_hold", completion_signal="manual",
                    hold_note="n", hold_until="2026-10-01")
    ok, msg = dbcli.do_hold(SID, note=None, until=None, release=True,
                            compaction_dir=str(d), db_path=dbf)
    assert ok and "action=released" in msg
    fm = parse_frontmatter((d / f"{SID}.md").read_text())
    assert (fm["status"], fm["hold_note"], fm["hold_until"]) == ("in_progress", "n", None)
    with db.connect(dbf) as conn:
        row = db.get_session(conn, SID)
    assert (row["status"], row["hold_until"]) == ("in_progress", None)


def test_hand_on_releases_hold(tmp_path):
    d, dbf = _setup(tmp_path, status="on_hold", completion_signal="manual",
                    hold_note="n", hold_until="2026-10-01")
    ok, msg = dbcli.do_resume(SID, compaction_dir=str(d), db_path=dbf)
    assert ok and msg.startswith("HANDON_OK")
    fm = parse_frontmatter((d / f"{SID}.md").read_text())
    assert fm["status"] == "in_progress"
    assert fm["hold_until"] is None
    assert fm["last_resumed"]
    with db.connect(dbf) as conn:
        row = db.get_session(conn, SID)
    assert (row["status"], row["hold_until"]) == ("in_progress", None)


# --- list / holds -----------------------------------------------------------
def test_list_shows_hold_by_default_and_counts_it(tmp_path, capsys):
    d, dbf = _setup(tmp_path, status="on_hold", completion_signal="manual", hold_note="n")
    rc = dbcli.main(["list", "--any-cwd", "--dir", str(d), "--db", str(dbf)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "⏸ on_hold" in out
    assert "on_hold: 1" in out


def test_holds_prints_resume_recipe_due_first(tmp_path, capsys):
    d, dbf = _setup(tmp_path, status="on_hold", completion_signal="manual",
                    hold_note="later one", hold_until="2026-12-01", title="later")
    fm2 = _fm(session_id=SID2, status="on_hold", completion_signal="manual",
              hold_note="soon one", hold_until="2026-01-01", title="soon", cwd="/tmp/proj")
    (d / f"{SID2}.md").write_text(render_frontmatter(fm2) + "U: x\n", encoding="utf-8")
    with db.connect(dbf) as conn:
        db.upsert_session(conn, fm=fm2, body="U: x\n")
    rc = dbcli.main(["holds", "--db", str(dbf), "--dir", str(d)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("soon one") < out.index("later one")
    assert f"cd /tmp/proj && claude --resume {SID2}" in out
    assert f"/hand:on {SID2}" in out
    assert "⏰" in out  # 2026-01-01 is overdue


def test_holds_due_only(tmp_path, capsys):
    d, dbf = _setup(tmp_path, status="on_hold", completion_signal="manual",
                    hold_note="future", hold_until="2999-01-01")
    rc = dbcli.main(["holds", "--due", "--db", str(dbf), "--dir", str(d)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == ""


# --- archive prune ----------------------------------------------------------
def test_prune_keeps_archive_of_hold_regardless_of_age(tmp_path):
    from handoff.archive import _open_archive_hashes

    dbf = tmp_path / "s.db"
    with db.connect(dbf) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, status, archive_hash, created) VALUES (?, ?, ?, ?)",
            (SID, "on_hold", "hash-hold", "2020-01-01T00:00:00Z"),
        )
    assert "hash-hold" in _open_archive_hashes(dbf, open_days=90)
