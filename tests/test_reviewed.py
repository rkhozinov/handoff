"""`hand reviewed` — records a per-row review decision while preserving the
ORIGINAL brief verbatim under <dir>/reviewed/ (spec-assess.md §5). Same sid
throughout: `claude --resume` and `/hand:on` keep working."""
from __future__ import annotations

import json
import re
from unittest.mock import patch

from handoff import cli, db, dbcli
from handoff.lifecycle import parse_frontmatter

from tests.test_assess import H1, H1B, H2, _seed

COPY_RE = re.compile(r"^" + re.escape(H1) + r"\.(\d{8}T\d{6}Z)(?:-\d+)?\.md$")


def _fm(d, sid):
    return parse_frontmatter((d / f"{sid}.md").read_text())


def _run(d, dbf, capsys, *argv):
    rc = dbcli.main(["reviewed", "--dir", str(d), "--db", str(dbf), *argv])
    return rc, capsys.readouterr().out


def _copies(d, sid=H1):
    # `-1.md` sorts before `.md` lexicographically; creation order is Z.md, Z-1.md
    return sorted((d / "reviewed").glob(f"{sid}.*.md"), key=lambda p: (len(p.name), p.name)) if (d / "reviewed").is_dir() else []


def test_reviewed_copies_original_bytes_then_changes_live(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {"hold_note": "old note"}))
    original = (d / f"{H1}.md").read_bytes()
    rc, out = _run(d, dbf, capsys, "aaaaaaaa", "--note", "PR 12 merged; nothing left", "--then", "done")
    assert rc == 0
    copies = _copies(d)
    assert len(copies) == 1 and COPY_RE.match(copies[0].name)
    assert copies[0].read_bytes() == original
    assert (d / f"{H1}.md").read_bytes() != original
    lines = out.splitlines()
    assert lines[0] == f"HANDREVIEWED_OK sid={H1} then=done copy={copies[0]}"
    assert lines[1].startswith("HANDDONE_OK")


def test_reviewed_then_done(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    _run(d, dbf, capsys, H1, "--note", "landed", "--then", "done")
    fm = _fm(d, H1)
    assert (fm["status"], fm["completion_signal"], fm["hold_note"]) == ("done", "manual", "landed")
    with db.connect(dbf) as conn:
        row = db.get_session(conn, H1)
    assert (row["status"], row["hold_note"]) == ("done", "landed")


def test_reviewed_then_release(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {"hold_until": "2026-10-01"}))
    _run(d, dbf, capsys, H1, "--note", "unblocked — resume", "--then", "release")
    fm = _fm(d, H1)
    assert (fm["status"], fm["hold_note"], fm["hold_until"]) == ("in_progress", "unblocked — resume", None)
    with db.connect(dbf) as conn:
        assert db.get_session(conn, H1)["status"] == "in_progress"


def test_reviewed_then_keep_with_until(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    _, out = _run(d, dbf, capsys, H1, "--note", "waits on vendor", "--then", "keep", "--until", "2026-11-01")
    fm = _fm(d, H1)
    assert (fm["status"], fm["hold_note"], fm["hold_until"]) == ("on_hold", "waits on vendor", "2026-11-01")
    assert "HANDHOLD_OK action=held" in out


def test_reviewed_until_ignored_unless_keep(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    _run(d, dbf, capsys, H1, "--note", "n", "--then", "release", "--until", "2026-11-01")
    assert _fm(d, H1)["hold_until"] is None


def test_reviewed_note_is_single_line(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    _run(d, dbf, capsys, H1, "--note", "STATE: x\nNEXT: y\n  BLOCKER: z", "--then", "keep")
    assert _fm(d, H1)["hold_note"] == "STATE: x NEXT: y BLOCKER: z"


def test_reviewed_same_second_gets_suffix(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    with patch("handoff.dbcli.now_iso", return_value="2026-09-21T12:00:00Z"):
        _run(d, dbf, capsys, H1, "--note", "a", "--then", "keep")
        _run(d, dbf, capsys, H1, "--note", "b", "--then", "keep")
    assert [p.name for p in _copies(d)] == [f"{H1}.20260921T120000Z.md", f"{H1}.20260921T120000Z-1.md"]


def test_reviewed_missing_brief_and_ambiguous(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}), (H1B, {}))
    rc, out = _run(d, dbf, capsys, "aaaaaaaa", "--note", "n", "--then", "keep")
    assert rc == 1 and out.startswith("HANDREVIEWED_ERROR ambiguous prefix")
    rc, out = _run(d, dbf, capsys, H2, "--note", "n", "--then", "keep")
    assert rc == 1 and out.startswith(f"HANDREVIEWED_ERROR no brief at {d / f'{H2}.md'}")
    assert not _copies(d) and not _copies(d, H2)


def test_assess_shows_reviewed_versions(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    with patch("handoff.dbcli.now_iso", return_value="2026-09-21T12:00:00Z"):
        _run(d, dbf, capsys, H1, "--note", "a", "--then", "keep")
    with patch("handoff.dbcli.now_iso", return_value="2026-09-22T08:30:15Z"):
        _run(d, dbf, capsys, H1, "--note", "b", "--then", "keep")
    dbcli.main(["assess", "--dir", str(d), "--db", str(dbf), "--bundle-dir", str(d / "bundles")])
    assert "    reviewed: 2 version(s), last 2026-09-22T08:30:15Z" in capsys.readouterr().out


def test_show_lists_copies(tmp_path, capsys):
    d, dbf = _seed(tmp_path, (H1, {}))
    with patch("handoff.dbcli.now_iso", return_value="2026-09-21T12:00:00Z"):
        _run(d, dbf, capsys, H1, "--note", "a", "--then", "keep")
    dbcli.main(["show", H1, "--dir", str(d), "--db", str(dbf)])
    lines = capsys.readouterr().out.splitlines()
    i = lines.index("---")
    assert lines[i - 1] == f"reviewed: {d / 'reviewed' / f'{H1}.20260921T120000Z.md'}"


def test_off_rerun_leaves_copy(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d, dbf = _seed(tmp_path, (H1, {}))
    _run(d, dbf, capsys, H1, "--note", "a", "--then", "keep")
    copy = _copies(d)[0]
    before = copy.read_bytes()
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "new work"}}) + "\n")
    cli.run(cli.parse_args(["--transcript", str(t), "--session-id", H1, "--cwd", "/repo/one",
                            "--no-archive", "--no-agent-store", "--db", str(dbf), "--out-dir", str(d)]))
    assert "new work" in (d / f"{H1}.md").read_text()
    assert copy.read_bytes() == before and len(_copies(d)) == 1


def test_reviewed_note_is_capped(tmp_path, capsys):
    # render_frontmatter already flattens newlines; the cap is sanitize_recap's job
    d, dbf = _seed(tmp_path, (H1, {}))
    _run(d, dbf, capsys, H1, "--note", "w " * 400, "--then", "keep")
    assert len(_fm(d, H1)["hold_note"]) <= 300
