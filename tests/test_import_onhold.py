"""scripts/import_onhold.py — turn a hand-kept `claude --resume …` list into
real briefs held on_hold. Synthetic data only (the real file names people)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from handoff import db
from handoff.lifecycle import parse_frontmatter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import import_onhold  # noqa: E402

SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
SID_B = "bbbbbbbb-2222-4222-8222-222222222222"
SID_C = "cccccccc-3333-4333-8333-333333333333"


def _transcript(projects: Path, sid: str, *, cwd: str, title: str | None, age: int = 0) -> Path:
    d = projects / "-Users-x-proj"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "user", "cwd": cwd, "sessionId": sid,
             "message": {"role": "user", "content": "please build the widget"}},
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "Built the widget, tests pass."}]}}]
    if title:
        rows.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    p = d / f"{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


# --- parse_line ----------------------------------------------------------------
@pytest.mark.parametrize(
    "line,expect",
    [
        (f'claude --resume "my-name"', ("my-name", None)),
        (f"claude --resume {SID_A}", (SID_A, None)),
        (f'claude --resume "my-name" ( /hand:on {SID_B} ) from dir: /x', ("my-name", SID_B)),
        (f"claude --resume {SID_A} /hand:on {SID_A}", (SID_A, SID_A)),
        (f"claude --resume {SID_A}  /Users/x/repos/video", (SID_A, None)),
        ("# comment", None),
        ("", None),
    ],
)
def test_parse_line(line, expect):
    got = import_onhold.parse_line(line)
    assert (None if got is None else (got.key, got.explicit_sid)) == expect


# --- resolve -----------------------------------------------------------------
def test_resolve_prefers_explicit_sid_then_newest_transcript(tmp_path):
    projects = tmp_path / "projects"
    _transcript(projects, SID_A, cwd="/p", title="dup", age=100)
    _transcript(projects, SID_B, cwd="/p", title="dup", age=10)   # newest
    _transcript(projects, SID_C, cwd="/q", title="solo")
    index = import_onhold.build_index(projects)

    r = import_onhold.resolve(import_onhold.parse_line('claude --resume "dup"'), index)
    assert r.sid == SID_B and set(r.others) == {SID_A}

    r = import_onhold.resolve(import_onhold.parse_line(f'claude --resume "dup" /hand:on {SID_A}'), index)
    assert r.sid == SID_A

    r = import_onhold.resolve(import_onhold.parse_line('claude --resume "solo"'), index)
    assert r.sid == SID_C and r.others == [] and r.cwd == "/q"

    r = import_onhold.resolve(import_onhold.parse_line('claude --resume "nope"'), index)
    assert r.sid is None and r.reason == "no-transcript-with-that-title"

    r = import_onhold.resolve(import_onhold.parse_line(f"claude --resume {SID_C}"), index)
    assert r.sid == SID_C and r.cwd == "/q"


# --- run ---------------------------------------------------------------------
def test_run_creates_brief_and_holds_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate ~/.claude/tasks etc.
    projects = tmp_path / "projects"
    _transcript(projects, SID_A, cwd="/p", title="one")
    _transcript(projects, SID_B, cwd="/q", title=None)
    todo = tmp_path / "todo"
    todo.write_text(f'claude --resume "one"\nclaude --resume {SID_B}\nclaude --resume "ghost"\n')
    comp = tmp_path / "compaction"
    dbf = tmp_path / "s.db"

    report = import_onhold.run(todo, projects_dir=projects, compaction_dir=comp, db_path=dbf, dry_run=False, archive=False)

    assert [r.sid for r in report.held] == [SID_A, SID_B]
    assert [r.key for r in report.unresolved] == ["ghost"]
    for sid, cwd, line in ((SID_A, "/p", 'claude --resume "one"'), (SID_B, "/q", f"claude --resume {SID_B}")):
        fm = parse_frontmatter((comp / f"{sid}.md").read_text())
        assert fm["status"] == "on_hold"
        assert fm["cwd"] == cwd
        assert fm["hold_note"] == line
    assert parse_frontmatter((comp / f"{SID_A}.md").read_text())["title"] == "one"
    with db.connect(dbf) as conn:
        assert db.get_session(conn, SID_A)["status"] == "on_hold"


def test_run_is_idempotent_and_keeps_existing_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / "projects"
    _transcript(projects, SID_A, cwd="/p", title="one")
    todo = tmp_path / "todo"
    todo.write_text('claude --resume "one"\n')
    comp = tmp_path / "compaction"
    dbf = tmp_path / "s.db"
    import_onhold.run(todo, projects_dir=projects, compaction_dir=comp, db_path=dbf, dry_run=False, archive=False)
    first = (comp / f"{SID_A}.md").read_text()
    report = import_onhold.run(todo, projects_dir=projects, compaction_dir=comp, db_path=dbf, dry_run=False, archive=False)
    assert report.held and report.held[0].snapshot == "kept-existing"
    assert (comp / f"{SID_A}.md").read_text() == first


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / "projects"
    _transcript(projects, SID_A, cwd="/p", title="one")
    todo = tmp_path / "todo"
    todo.write_text('claude --resume "one"\n')
    comp = tmp_path / "compaction"
    report = import_onhold.run(todo, projects_dir=projects, compaction_dir=comp, db_path=tmp_path / "s.db", dry_run=True, archive=False)
    assert report.held[0].sid == SID_A
    assert not comp.exists()
