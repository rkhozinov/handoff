"""Regressions pinned by the 2026-09-21 review. Each test names the finding."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from handoff import db, dbcli
from handoff.lifecycle import (
    detect_status,
    is_stale,
    parse_frontmatter,
    render_frontmatter,
)

SID = "8e4178e1-7dc1-4352-b13e-98faeb5c1116"


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


# #2 — a question can never count as a completion signal, even when it
# contains a completion keyword ("is it fixed?").
@pytest.mark.parametrize("msg", ["is it fixed?", "done?", "did it close the PR?"])
def test_question_with_done_keyword_is_pending_not_done(msg):
    assert detect_status([_user("start work"), _user(msg)]) == ("pending", "auto-open-q")


def test_terse_done_msg_still_done():
    assert detect_status([_user("start work"), _user("merged, thanks")]) == ("done", "auto-user-msg")


# #3 — manual signal is sticky against the stale sweep (CLAUDE.md contract).
def test_manual_reopened_brief_is_never_stale():
    fm = {"status": "in_progress", "completion_signal": "manual", "created": "2026-01-01T00:00:00Z"}
    assert is_stale(fm, now=datetime(2026, 9, 1, tzinfo=timezone.utc)) is False


def test_auto_brief_is_stale():
    fm = {"status": "in_progress", "completion_signal": "auto-default", "created": "2026-01-01T00:00:00Z"}
    assert is_stale(fm, now=datetime(2026, 9, 1, tzinfo=timezone.utc)) is True


# #7 — a newline inside a value must not inject a second key.
def test_render_frontmatter_flattens_newlines():
    fm = {"status": "in_progress", "title": "a\nstatus: done", "session_id": SID}
    back = parse_frontmatter(render_frontmatter(fm))
    assert back["status"] == "in_progress"
    assert back["title"] == "a status: done"


# #8 — /hand:on must not silently un-archive.
def test_do_resume_leaves_archived_untouched(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    fm = {"status": "archived", "completion_signal": "manual", "session_id": SID, "cwd": "/x"}
    (d / f"{SID}.md").write_text(render_frontmatter(fm) + "U: hi\n", encoding="utf-8")
    dbf = tmp_path / "s.db"
    with db.connect(dbf) as conn:
        db.upsert_session(conn, fm=fm, body="U: hi\n")
    ok, msg = dbcli.do_resume(SID, compaction_dir=str(d), db_path=dbf)
    assert ok and msg.startswith("HANDON_ARCHIVED")
    assert parse_frontmatter((d / f"{SID}.md").read_text())["status"] == "archived"
    with db.connect(dbf) as conn:
        assert db.get_session(conn, SID)["status"] == "archived"


# #13 — a sid is a filename component, never a path.
@pytest.mark.parametrize("bad", ["../../x", "a/b", "..", "", "x\ny"])
def test_brief_path_rejects_path_like_sid(bad, tmp_path):
    with pytest.raises(ValueError):
        dbcli._brief_path(bad, str(tmp_path))


def test_do_done_reports_bad_sid_instead_of_raising(tmp_path):
    ok, msg = dbcli.do_done("../x", reopen=False, compaction_dir=str(tmp_path), db_path=tmp_path / "s.db")
    assert not ok and "bad-session-id" in msg


# #9 — brief writes are atomic: no temp file left, content replaced whole.
def test_write_brief_leaves_no_tmp(tmp_path):
    p = tmp_path / f"{SID}.md"
    dbcli._write_brief(p, {"status": "done", "session_id": SID}, "U: x\n")
    assert p.read_text().endswith("U: x\n")
    assert os.listdir(tmp_path) == [f"{SID}.md"]


def test_write_brief_keeps_old_content_when_write_fails(tmp_path, monkeypatch):
    p = tmp_path / f"{SID}.md"
    p.write_text("OLD", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("handoff.fsutil.os.replace", boom)
    with pytest.raises(OSError):
        dbcli._write_brief(p, {"status": "done", "session_id": SID}, "NEW\n")
    assert p.read_text() == "OLD"
    assert os.listdir(tmp_path) == [f"{SID}.md"]


# Found by the /hand:assess e2e (2026-09-21): a one-line transcript "ship the
# widget" was classified done/auto-user-msg — bare `ship` is an imperative
# request, not a completion. `shipped` / `ship it` still count.
@pytest.mark.parametrize("msg,expected", [
    ("ship the widget", ("in_progress", "auto-default")),
    ("shipped", ("done", "auto-user-msg")),
    ("ship it", ("done", "auto-user-msg")),
])
def test_bare_ship_is_a_request_not_completion(msg, expected):
    assert detect_status([_user("start"), _user(msg)]) == expected
