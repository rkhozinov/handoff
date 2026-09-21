"""on_hold surface: custom-title extraction (the name `claude --resume`
takes), TUI hold key, TUI error surfacing, delete confirmation."""
from __future__ import annotations

import asyncio

import pytest

from handoff import db, dbcli, extract
from handoff.lifecycle import parse_frontmatter, render_frontmatter

SID_A = "11111111-1111-1111-1111-111111111111"


# --- extract_title: custom-title (user-set) beats ai-title --------------------
def test_custom_title_beats_ai_title_regardless_of_order():
    entries = [
        {"type": "custom-title", "customTitle": "my-name", "sessionId": SID_A},
        {"type": "ai-title", "aiTitle": "AI guess"},
    ]
    assert extract.extract_title(entries) == "my-name"


def test_last_custom_title_wins():
    entries = [
        {"type": "custom-title", "customTitle": "first"},
        {"type": "custom-title", "customTitle": "second"},
    ]
    assert extract.extract_title(entries) == "second"


def test_ai_title_still_used_without_custom():
    assert extract.extract_title([{"type": "ai-title", "aiTitle": "AI guess"}]) == "AI guess"


def test_extract_custom_title_only():
    entries = [{"type": "ai-title", "aiTitle": "AI"}, {"type": "custom-title", "customTitle": "c"}]
    assert extract.extract_custom_title(entries) == "c"
    assert extract.extract_custom_title([{"type": "ai-title", "aiTitle": "AI"}]) is None


# --- TUI ----------------------------------------------------------------------
textual = pytest.importorskip("textual")
from handoff.tui import HandoffTUI  # noqa: E402


def _seed(tmp_path, **over):
    d = tmp_path / "compaction"
    d.mkdir()
    fm = {
        "status": "in_progress", "title": "t", "session_id": SID_A, "cwd": "/x",
        "created": "2026-06-04T10:00:00Z", "last_resumed": None,
        "completion_signal": "auto-default", "archive_hash": "h",
        "recap": None, "recap_source": None,
    }
    fm.update(over)
    (d / f"{SID_A}.md").write_text(render_frontmatter(fm) + "U: hi\n", encoding="utf-8")
    dbf = tmp_path / "s.db"
    with db.connect(dbf) as conn:
        db.upsert_session(conn, fm=fm, body="U: hi\n")
    return d, dbf


def test_hold_key_prompts_for_note_and_holds(tmp_path):
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            from handoff.tui import PromptScreen
            assert isinstance(app.screen, PromptScreen)
            app.screen.query_one("#prompt-input").value = "wait for PR 12"
            await pilot.press("enter")
            await pilot.pause()
            assert app._rows[0]["status"] == "on_hold"  # still listed (holds are visible)

    asyncio.run(go())
    fm = parse_frontmatter((d / f"{SID_A}.md").read_text())
    assert (fm["status"], fm["hold_note"]) == ("on_hold", "wait for PR 12")


def test_hold_key_on_held_row_releases(tmp_path):
    d, dbf = _seed(tmp_path, status="on_hold", completion_signal="manual",
                   hold_note="n", hold_until="2026-10-01")

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()

    asyncio.run(go())
    fm = parse_frontmatter((d / f"{SID_A}.md").read_text())
    assert (fm["status"], fm["hold_until"]) == ("in_progress", None)


def test_delete_needs_confirmation(tmp_path):
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            from handoff.tui import ConfirmScreen
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("escape")  # decline
            await pilot.pause()
            with db.connect(dbf) as conn:
                assert db.get_session(conn, SID_A) is not None
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("y")  # confirm
            await pilot.pause()
            with db.connect(dbf) as conn:
                assert db.get_session(conn, SID_A) is None

    asyncio.run(go())


def test_mutation_failure_is_surfaced_not_swallowed(tmp_path, monkeypatch):
    d, dbf = _seed(tmp_path)
    (d / f"{SID_A}.md").unlink()  # file gone → do_done returns (False, HANDDONE_ERROR …)
    seen = []

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        monkeypatch.setattr(app, "notify", lambda msg, **kw: seen.append((msg, kw.get("severity"))))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()

    asyncio.run(go())
    assert seen and seen[-1][1] == "error" and "HANDDONE_ERROR" in seen[-1][0]
