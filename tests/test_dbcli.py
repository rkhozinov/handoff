"""Tests for `handoff.dbcli` — file+DB mutations stay in sync."""
from __future__ import annotations

from pathlib import Path

from handoff import db, dbcli
from handoff.lifecycle import parse_frontmatter, render_frontmatter

SID = "8e4178e1-7dc1-4352-b13e-98faeb5c1116"


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
    """Make a brief file + a DB row. Returns (compaction_dir, db_path)."""
    d = tmp_path / "compaction"
    d.mkdir()
    fm = _fm(**fm_over)
    (d / f"{SID}.md").write_text(render_frontmatter(fm) + "U: hello\n", encoding="utf-8")
    dbf = tmp_path / "sessions.db"
    with db.connect(dbf) as conn:
        db.upsert_session(conn, fm=fm, body="U: hello\n")
    return d, dbf


def _file_status(d: Path) -> dict:
    return parse_frontmatter((d / f"{SID}.md").read_text(encoding="utf-8"))


class TestDone:
    def test_done_edits_file_and_db(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        rc = dbcli.main(["done", SID, "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "HANDDONE_OK" in out and "status=done" in out
        fm = _file_status(d)
        assert fm["status"] == "done" and fm["completion_signal"] == "manual"
        with db.connect(dbf) as conn:
            assert db.get_session(conn, SID)["status"] == "done"

    def test_reopen(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path, status="done", completion_signal="manual")
        rc = dbcli.main(["done", SID, "--reopen", "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "reopened" in capsys.readouterr().out
        assert _file_status(d)["status"] == "in_progress"
        with db.connect(dbf) as conn:
            assert db.get_session(conn, SID)["status"] == "in_progress"

    def test_missing_brief_errors(self, tmp_path, capsys):
        d = tmp_path / "compaction"
        d.mkdir()
        rc = dbcli.main(["done", SID, "--dir", str(d), "--db", str(tmp_path / "s.db")])
        assert rc == 1
        assert "HANDDONE_ERROR" in capsys.readouterr().out


class TestOn:
    def test_flips_in_progress_and_stamps(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path, status="pending", last_resumed=None)
        rc = dbcli.main(["on", SID, "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "HANDON_OK" in capsys.readouterr().out
        fm = _file_status(d)
        assert fm["status"] == "in_progress"
        assert fm["last_resumed"] is not None
        with db.connect(dbf) as conn:
            row = db.get_session(conn, SID)
        assert row["status"] == "in_progress" and row["last_resumed"]

    def test_declines_when_done(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path, status="done")
        rc = dbcli.main(["on", SID, "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "HANDON_DONE" in capsys.readouterr().out
        assert _file_status(d)["status"] == "done"  # untouched

    def test_multiple_sids(self, tmp_path, capsys):
        """Several briefs restored into one session; a bad sid doesn't stop
        the good ones (rc is still 1 so the caller sees the partial miss)."""
        d, dbf = _setup(tmp_path, status="pending", last_resumed=None)
        other = "11111111-2222-3333-4444-555555555555"
        fm = _fm(session_id=other, status="pending", last_resumed=None)
        (d / f"{other}.md").write_text(
            render_frontmatter(fm) + "U: second\n", encoding="utf-8"
        )
        with db.connect(dbf) as conn:
            db.upsert_session(conn, fm=fm, body="U: second\n")

        rc = dbcli.main(["on", SID, other, "nope", "--dir", str(d), "--db", str(dbf)])
        out = capsys.readouterr().out
        assert rc == 1
        assert out.count("HANDON_OK") == 2
        assert "HANDON_ERROR" in out
        with db.connect(dbf) as conn:
            for sid in (SID, other):
                assert db.get_session(conn, sid)["status"] == "in_progress"


class TestListShowSearchRm:
    def test_list_renders_table(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        rc = dbcli.main(["list", "--any-cwd", "--db", str(dbf)])
        assert rc == 0
        out = capsys.readouterr().out
        assert SID[:8] in out
        assert "in_progress: 1" in out

    def test_show(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        rc = dbcli.main(["show", SID, "--db", str(dbf)])
        assert rc == 0
        out = capsys.readouterr().out
        assert SID in out and "U: hello" in out

    def test_search(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path, title="findme")
        rc = dbcli.main(["search", "findme", "--db", str(dbf)])
        assert rc == 0
        assert SID[:8] in capsys.readouterr().out

    def test_rm_row_only_keeps_file(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        rc = dbcli.main(["rm", SID, "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "HANDRM_OK" in capsys.readouterr().out
        with db.connect(dbf) as conn:
            assert db.get_session(conn, SID) is None
        assert (d / f"{SID}.md").exists()  # file preserved

    def test_rm_with_file(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        rc = dbcli.main(["rm", SID, "--file", "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "file=removed" in capsys.readouterr().out
        assert not (d / f"{SID}.md").exists()


class TestRename:
    def test_rename_edits_file_and_db(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        rc = dbcli.main(["rename", SID, "New", "Shiny", "Title", "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "HANDRENAME_OK" in capsys.readouterr().out
        assert _file_status(d)["title"] == "New Shiny Title"
        with db.connect(dbf) as conn:
            assert db.get_session(conn, SID)["title"] == "New Shiny Title"

    def test_rename_empty_rejected(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        ok, msg = dbcli.do_rename(SID, "   ", compaction_dir=str(d), db_path=str(dbf))
        assert ok is False and "empty" in msg


class TestArchive:
    def test_archive_edits_file_and_db(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        rc = dbcli.main(["archive", SID, "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "HANDARCH_OK" in capsys.readouterr().out
        assert _file_status(d)["status"] == "archived"
        with db.connect(dbf) as conn:
            assert db.get_session(conn, SID)["status"] == "archived"
            # hidden from the default list, shown with --archived
            assert db.list_sessions(conn) == []
            assert len(db.list_sessions(conn, include_archived=True)) == 1

    def test_unarchive(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path, status="archived", completion_signal="manual")
        rc = dbcli.main(["unarchive", SID, "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "unarchived" in capsys.readouterr().out
        assert _file_status(d)["status"] == "in_progress"

    def test_archive_sticky_across_handoff(self, tmp_path):
        # A later /hand:off must not un-archive a manual archived brief.
        from handoff.lifecycle import resolve_frontmatter

        existing = {"status": "archived", "completion_signal": "manual"}
        fm = resolve_frontmatter(
            session_id=SID, cwd="/x", detected_status="in_progress",
            detected_signal="auto-default", archive_hash=None, existing=existing,
        )
        assert fm["status"] == "archived"
        assert fm["completion_signal"] == "manual"


class TestBackfillTitles:
    def test_recovers_title_from_transcript(self, tmp_path, capsys):
        import json

        d, dbf = _setup(tmp_path, title=None)
        # Build a fake CC transcript at projects/<enc-cwd>/<sid>.jsonl
        projects = tmp_path / "projects"
        enc = "-Users-x-repos-handoff"  # matches _fm cwd /Users/x/repos/handoff
        tdir = projects / enc
        tdir.mkdir(parents=True)
        (tdir / f"{SID}.jsonl").write_text(
            "\n".join(
                json.dumps(e)
                for e in [
                    {"type": "user", "message": {"role": "user", "content": "hi"}},
                    {"type": "ai-title", "aiTitle": "Recovered Title"},
                ]
            ),
            encoding="utf-8",
        )
        rc = dbcli.main(
            ["backfill-titles", "--dir", str(d), "--db", str(dbf), "--projects", str(projects)]
        )
        assert rc == 0
        assert "updated=1" in capsys.readouterr().out
        assert _file_status(d)["title"] == "Recovered Title"
        with db.connect(dbf) as conn:
            assert db.get_session(conn, SID)["title"] == "Recovered Title"

    def test_no_transcript_left_untouched(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path, title=None)
        rc = dbcli.main(
            ["backfill-titles", "--dir", str(d), "--db", str(dbf), "--projects", str(tmp_path / "nope")]
        )
        assert rc == 0
        assert "no_transcript=1" in capsys.readouterr().out
        assert _file_status(d)["title"] is None


class TestRebuild:
    def test_rebuild_subcommand(self, tmp_path, capsys):
        d, dbf = _setup(tmp_path)
        # wipe the row, rebuild from the file
        with db.connect(dbf) as conn:
            db.delete_session(conn, SID)
        rc = dbcli.main(["rebuild", "--dir", str(d), "--db", str(dbf)])
        assert rc == 0
        assert "upserted=1" in capsys.readouterr().out
        with db.connect(dbf) as conn:
            assert db.get_session(conn, SID) is not None
