"""Third batch from the 2026-09-21 review: the deferred one-liners that had
a real failure mode."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from handoff import db, dbcli
from handoff.lifecycle import parse_frontmatter, render_frontmatter

SID = "8e4178e1-7dc1-4352-b13e-98faeb5c1116"
SID2 = "11111111-2222-4333-8444-555555555555"


def _fm(sid=SID, **over):
    fm = {"status": "in_progress", "title": "t", "session_id": sid, "cwd": "/x",
          "created": "2026-06-04T10:00:00Z", "last_resumed": None,
          "completion_signal": "auto-default", "archive_hash": None,
          "recap": None, "recap_source": None, "hold_note": None, "hold_until": None}
    fm.update(over)
    return fm


# LIKE wildcards: `_` and `%` in a query must match literally.
def test_search_treats_underscore_and_percent_literally(tmp_path):
    dbf = tmp_path / "s.db"
    with db.connect(dbf) as conn:
        db.upsert_session(conn, fm=_fm(title="hello_world"), body="")
        db.upsert_session(conn, fm=_fm(sid=SID2, title="helloXworld 50%"), body="")
        assert [r["session_id"] for r in db.search_sessions(conn, "hello_")] == [SID]
        assert [r["session_id"] for r in db.search_sessions(conn, "50%")] == [SID2]
        assert [r["session_id"] for r in db.search_sessions(conn, "_")] == [SID]  # literal underscore only


# backfill-titles must not skip archived rows forever.
def test_backfill_titles_visits_archived_rows(tmp_path):
    d = tmp_path / "c"; d.mkdir()
    projects = tmp_path / "p" / "-x"; projects.mkdir(parents=True)
    fm = _fm(status="archived", completion_signal="manual", title=None)
    (d / f"{SID}.md").write_text(render_frontmatter(fm) + "U: hi\n")
    (projects / f"{SID}.jsonl").write_text(json.dumps({"type": "ai-title", "aiTitle": "found"}) + "\n")
    dbf = tmp_path / "s.db"
    with db.connect(dbf) as conn:
        db.upsert_session(conn, fm=fm, body="U: hi\n", brief_path=str(d / f"{SID}.md"))
    stats = dbcli.do_backfill_titles(compaction_dir=str(d), projects_dir=str(tmp_path / "p"), db_path=dbf)
    assert stats["updated"] == 1
    assert parse_frontmatter((d / f"{SID}.md").read_text())["title"] == "found"


# A file mutation with no DB row must leave the DB in sync, not print *_OK
# over a missing row.
def test_do_done_upserts_row_when_db_has_none(tmp_path):
    d = tmp_path / "c"; d.mkdir()
    (d / f"{SID}.md").write_text(render_frontmatter(_fm()) + "U: hi\n")
    dbf = tmp_path / "s.db"
    ok, msg = dbcli.do_done(SID, reopen=False, compaction_dir=str(d), db_path=dbf)
    assert ok
    with db.connect(dbf) as conn:
        row = db.get_session(conn, SID)
    assert row and row["status"] == "done" and row["completion_signal"] == "manual"


def test_do_resume_upserts_row_when_db_has_none(tmp_path):
    d = tmp_path / "c"; d.mkdir()
    (d / f"{SID}.md").write_text(render_frontmatter(_fm()) + "U: hi\n")
    dbf = tmp_path / "s.db"
    ok, _ = dbcli.do_resume(SID, compaction_dir=str(d), db_path=dbf)
    assert ok
    with db.connect(dbf) as conn:
        row = db.get_session(conn, SID)
    assert row and row["last_resumed"]


# Two same-day /hand:off runs must not both prune: stamp before pruning.
def test_auto_prune_stamps_before_pruning(tmp_path, monkeypatch):
    from handoff import archive

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    stamp = archive._prune_stamp()
    seen = []

    def fake_prune(**kw):
        seen.append(stamp.exists())
        return {"deleted": 0}

    with patch("handoff.archive.prune_archives", side_effect=fake_prune):
        archive.maybe_prune_archives()
    assert seen == [True]
