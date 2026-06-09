"""Tests for `handoff.db` — the SQLite session index."""
from __future__ import annotations

from pathlib import Path

from handoff import db
from handoff.lifecycle import render_frontmatter

SID_A = "8e4178e1-7dc1-4352-b13e-98faeb5c1116"
SID_B = "23509468-67c8-49a9-8808-2574e03c88ce"


def _fm(sid: str, **over) -> dict[str, str | None]:
    fm = {
        "status": "in_progress",
        "title": "session A",
        "session_id": sid,
        "cwd": "/Users/x/repos/handoff",
        "created": "2026-06-04T10:00:00Z",
        "last_resumed": None,
        "completion_signal": "auto-default",
        "archive_hash": "abc123",
        "recap": "Goal: x. Next: y.",
        "recap_source": "llm",
    }
    fm.update(over)
    return fm


def _dbfile(tmp_path: Path) -> Path:
    return tmp_path / "sessions.db"


class TestUpsertGet:
    def test_roundtrip_includes_body(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A), body="BODY TEXT", brief_path="/p/a.md")
            row = db.get_session(conn, SID_A)
        assert row["session_id"] == SID_A
        assert row["status"] == "in_progress"
        assert row["title"] == "session A"
        assert row["body"] == "BODY TEXT"
        assert row["brief_path"] == "/p/a.md"
        assert row["indexed_at"]  # stamped

    def test_tokens_estimated_on_upsert(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A), body="x" * 400)
            row = db.get_session(conn, SID_A)
            listed = db.list_sessions(conn, include_done=True)[0]
        assert row["tokens"] == 100          # 400 chars / 4
        assert listed["tokens"] == 100       # exposed in the list view too

    def test_get_missing_returns_none(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            assert db.get_session(conn, "nope") is None

    def test_idempotent_single_row(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, status="in_progress"), body="v1")
            db.upsert_session(conn, fm=_fm(SID_A, status="done"), body="v2")
            rows = db.list_sessions(conn, include_done=True)
            assert len(rows) == 1
            assert db.get_session(conn, SID_A)["status"] == "done"
            assert db.get_session(conn, SID_A)["body"] == "v2"

    def test_upsert_requires_session_id(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            try:
                db.upsert_session(conn, fm=_fm(SID_A, session_id=None), body="x")
            except ValueError:
                return
        raise AssertionError("expected ValueError for missing session_id")


class TestList:
    def test_orders_newest_first(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, created="2026-06-01T00:00:00Z"), body="")
            db.upsert_session(conn, fm=_fm(SID_B, created="2026-06-08T00:00:00Z"), body="")
            rows = db.list_sessions(conn)
        assert [r["session_id"] for r in rows] == [SID_B, SID_A]

    def test_list_excludes_body(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A), body="SECRET")
            rows = db.list_sessions(conn)
        assert "body" not in rows[0]

    def test_hides_done_by_default(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, status="done"), body="")
            db.upsert_session(conn, fm=_fm(SID_B, status="in_progress"), body="")
            rows = db.list_sessions(conn)
        assert [r["session_id"] for r in rows] == [SID_B]

    def test_include_done(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, status="done"), body="")
            rows = db.list_sessions(conn, include_done=True)
        assert len(rows) == 1

    def test_filters_by_cwd(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, cwd="/a"), body="")
            db.upsert_session(conn, fm=_fm(SID_B, cwd="/b"), body="")
            rows = db.list_sessions(conn, cwd="/b")
        assert [r["session_id"] for r in rows] == [SID_B]

    def test_status_filter_overrides_done_hiding(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, status="done"), body="")
            rows = db.list_sessions(conn, status="done")
        assert len(rows) == 1

    def test_hides_archived_by_default(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, status="archived"), body="")
            db.upsert_session(conn, fm=_fm(SID_B, status="in_progress"), body="")
            default = {r["session_id"] for r in db.list_sessions(conn)}
            with_arch = {r["session_id"] for r in db.list_sessions(conn, include_archived=True)}
        assert default == {SID_B}
        assert with_arch == {SID_A, SID_B}

    def test_archived_hidden_independently_of_done(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, status="archived"), body="")
            db.upsert_session(conn, fm=_fm(SID_B, status="done"), body="")
            # include_done must NOT surface archived rows.
            rows = {r["session_id"] for r in db.list_sessions(conn, include_done=True)}
        assert rows == {SID_B}


class TestMutators:
    def test_set_status(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A), body="")
            assert db.set_status(conn, SID_A, "done", "manual") is True
            row = db.get_session(conn, SID_A)
        assert row["status"] == "done"
        assert row["completion_signal"] == "manual"

    def test_set_status_unknown_sid(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            assert db.set_status(conn, "nope", "done", "manual") is False

    def test_set_resumed(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, status="pending"), body="")
            assert db.set_resumed(conn, SID_A, status="in_progress", last_resumed="2026-06-09T12:00:00Z")
            row = db.get_session(conn, SID_A)
        assert row["status"] == "in_progress"
        assert row["last_resumed"] == "2026-06-09T12:00:00Z"

    def test_delete(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A), body="")
            assert db.delete_session(conn, SID_A) is True
            assert db.delete_session(conn, SID_A) is False
            assert db.get_session(conn, SID_A) is None


class TestSearch:
    def test_matches_title_recap_body(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A, title="needle here"), body="x")
            db.upsert_session(conn, fm=_fm(SID_B, title="other", recap=None), body="haystack needle")
            by_title = {r["session_id"] for r in db.search_sessions(conn, "needle")}
        assert by_title == {SID_A, SID_B}

    def test_no_match(self, tmp_path):
        with db.connect(_dbfile(tmp_path)) as conn:
            db.upsert_session(conn, fm=_fm(SID_A), body="")
            assert db.search_sessions(conn, "zzzzz") == []


class TestRebuild:
    def _write_brief(self, d: Path, sid: str, body: str = "U: do the thing\n", **fm_over):
        fm = _fm(sid, **fm_over)
        (d / f"{sid}.md").write_text(render_frontmatter(fm) + body, encoding="utf-8")

    def test_rebuild_from_briefs(self, tmp_path):
        briefs = tmp_path / "compaction"
        briefs.mkdir()
        self._write_brief(briefs, SID_A, body="U: alpha\n")
        self._write_brief(briefs, SID_B, body="U: beta\n", status="done")
        with db.connect(_dbfile(tmp_path)) as conn:
            stats = db.rebuild_from_briefs(conn, briefs)
            rows = {r["session_id"] for r in db.list_sessions(conn, include_done=True)}
        assert stats["upserted"] == 2
        assert rows == {SID_A, SID_B}

    def test_rebuild_drops_stale_rows(self, tmp_path):
        briefs = tmp_path / "compaction"
        briefs.mkdir()
        dbf = _dbfile(tmp_path)
        with db.connect(dbf) as conn:
            db.upsert_session(conn, fm=_fm("ghost"), body="")  # no backing file
        self._write_brief(briefs, SID_A)
        with db.connect(dbf) as conn:
            stats = db.rebuild_from_briefs(conn, briefs)
            rows = {r["session_id"] for r in db.list_sessions(conn, include_done=True)}
        assert stats["deleted"] == 1
        assert rows == {SID_A}

    def test_rebuild_skips_aux_and_unfenced(self, tmp_path):
        briefs = tmp_path / "compaction"
        briefs.mkdir()
        self._write_brief(briefs, SID_A)
        (briefs / f"{SID_B}-full.md").write_text("aux", encoding="utf-8")
        (briefs / "consumed-x.md").write_text("aux", encoding="utf-8")
        (briefs / "nofence.md").write_text("no frontmatter here\n", encoding="utf-8")
        with db.connect(_dbfile(tmp_path)) as conn:
            stats = db.rebuild_from_briefs(conn, briefs)
            rows = {r["session_id"] for r in db.list_sessions(conn, include_done=True)}
        assert rows == {SID_A}
        assert stats["skipped"] >= 1  # nofence.md scanned but skipped


def test_connect_creates_empty_db(tmp_path):
    dbf = tmp_path / "nested" / "sessions.db"
    with db.connect(dbf) as conn:
        assert db.list_sessions(conn) == []
    assert dbf.exists()
