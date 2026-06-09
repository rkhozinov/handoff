"""Headless tests for `handoff.tui`. Skipped when Textual isn't installed
(it's an optional `[tui]` extra)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("textual")

from handoff import db, dbcli  # noqa: E402
from handoff.lifecycle import parse_frontmatter, render_frontmatter  # noqa: E402
from handoff.tui import HandoffTUI  # noqa: E402

SID_A = "11111111-1111-1111-1111-111111111111"
SID_B = "22222222-2222-2222-2222-222222222222"


def _fm(sid, **over):
    fm = {
        "status": "in_progress",
        "title": f"title-{sid[:4]}",
        "session_id": sid,
        "cwd": "/Users/x/repos/handoff",
        "created": "2026-06-04T10:00:00Z",
        "last_resumed": None,
        "completion_signal": "auto-default",
        "archive_hash": "h",
        "recap": "Goal: x. Next: y.",
        "recap_source": "llm",
    }
    fm.update(over)
    return fm


def _seed(tmp_path) -> tuple[Path, Path]:
    """Brief files + DB rows: SID_A in_progress, SID_B done."""
    d = tmp_path / "compaction"
    d.mkdir()
    dbf = tmp_path / "sessions.db"
    with db.connect(dbf) as conn:
        for sid, over in ((SID_A, {}), (SID_B, {"status": "done"})):
            fm = _fm(sid, **over)
            body = f"U: hello {sid[:4]}\n"
            (d / f"{sid}.md").write_text(render_frontmatter(fm) + body, encoding="utf-8")
            db.upsert_session(conn, fm=fm, body=body)
    return d, dbf


def _row_count(app) -> int:
    return len(app.query_one("#list").children)


def test_display_title_fallback():
    from handoff.tui import _display_title

    assert _display_title({"title": "Real", "recap": "x", "session_id": "abc"}) == "Real"
    assert _display_title({"title": None, "recap": "one two three", "session_id": "abc"}) == "one two three"
    assert _display_title({"title": "", "recap": "", "session_id": "abcd1234ef"}) == "abcd1234"


def test_fuzzy_score_basics():
    from handoff.tui import _fuzzy_score

    # Non-subsequence → None.
    assert _fuzzy_score("xyz", "title abc") is None
    # Subsequence matches.
    assert _fuzzy_score("ttl", "title") is not None
    # Contiguous + word-boundary beats scattered.
    contig = _fuzzy_score("data", "database session")
    scattered = _fuzzy_score("data", "d-a-t-zzz-a")
    assert contig > scattered
    # Empty query scores 0 (matches everything).
    assert _fuzzy_score("", "anything") == 0


def test_search_filters_live_and_finds_done(tmp_path):
    # SID_A in_progress (title-1111), SID_B done (title-2222).
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert _row_count(app) == 1            # done hidden by default
            await pilot.press("/")                 # open search bar
            await pilot.pause()
            assert app._search_input().has_class("-visible")
            # Type the done session's title → fuzzy search reaches across
            # statuses even though `a` is off.
            app._search_input().value = "2222"
            await pilot.pause()
            assert _row_count(app) == 1
            assert app._rows[0]["session_id"] == SID_B
            # Clear via esc → back to the unfiltered (done-hidden) view.
            await pilot.press("escape")
            await pilot.pause()
            assert app._query == ""
            assert _row_count(app) == 1
            assert app._rows[0]["session_id"] == SID_A

    asyncio.run(go())


def test_empty_db_shows_placeholder(tmp_path):
    dbf = tmp_path / "empty.db"

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert _row_count(app) == 0
            assert "No sessions" in app._detail_text

    asyncio.run(go())


def test_list_hides_done_then_toggles(tmp_path):
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert _row_count(app) == 1            # done hidden
            await pilot.press("a")
            await pilot.pause()
            assert _row_count(app) == 2            # done shown

    asyncio.run(go())


def test_detail_renders_selected_body(tmp_path):
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            # newest-first; both share created, A upserted first → ordering by
            # indexed_at DESC puts B first, but either way a body is shown.
            assert "hello" in app._detail_text

    asyncio.run(go())


def test_markdown_toggle(tmp_path):
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._markdown is True
            await pilot.press("m")
            await pilot.pause()
            assert app._markdown is False   # flipped to raw
            await pilot.press("m")
            await pilot.pause()
            assert app._markdown is True

    asyncio.run(go())


def test_archive_action_hides_and_toggles_back(tmp_path):
    d, dbf = _seed(tmp_path)  # SID_A in_progress, SID_B done

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert _row_count(app) == 1  # only SID_A (done hidden)
            sid = app._selected_sid()
            await pilot.press("e")       # archive it
            await pilot.pause()
            assert _row_count(app) == 0   # archived now hidden too
            await pilot.press("A")        # reveal archived
            await pilot.pause()
            assert _row_count(app) == 1
            return sid

    sid = asyncio.run(go())
    fm = parse_frontmatter((d / f"{sid}.md").read_text(encoding="utf-8"))
    assert fm["status"] == "archived"


def test_rename_via_modal(tmp_path):
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            sid = app._selected_sid()
            await pilot.press("c")              # open rename modal
            await pilot.pause()
            from handoff.tui import RenameScreen
            assert isinstance(app.screen, RenameScreen)
            inp = app.screen.query_one("#title-input")
            inp.value = "Renamed In TUI"
            await pilot.press("enter")          # submit
            await pilot.pause()
            return sid

    sid = asyncio.run(go())
    fm = parse_frontmatter((d / f"{sid}.md").read_text(encoding="utf-8"))
    assert fm["title"] == "Renamed In TUI"


def test_copy_restore_to_clipboard(tmp_path):
    d, dbf = _seed(tmp_path)
    captured = {}

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        app.copy_to_clipboard = lambda text: captured.setdefault("clip", text)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()

    asyncio.run(go())
    assert captured["clip"].startswith("/hand:on ")
    assert captured["clip"].split()[1] in (SID_A, SID_B)


def test_selection_start_flips_to_raw_and_copy_brief(tmp_path):
    d, dbf = _seed(tmp_path)
    cap = {}

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        app.copy_to_clipboard = lambda t: cap.setdefault("c", t)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._markdown is True
            app._on_selection_started(app.screen)   # drag-select begins
            await pilot.pause()
            assert app._markdown is False           # auto-flipped to selectable raw
            await pilot.press("Y")                   # copy whole brief
            await pilot.pause()

    asyncio.run(go())
    assert "hello" in cap["c"]


def test_mark_done_action_updates_file_and_db(tmp_path):
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            sid = app._selected_sid()
            assert sid is not None
            await pilot.press("d")
            await pilot.pause()
            return sid

    sid = asyncio.run(go())
    fm = parse_frontmatter((d / f"{sid}.md").read_text(encoding="utf-8"))
    assert fm["status"] == "done"
    with db.connect(dbf) as conn:
        assert db.get_session(conn, sid)["status"] == "done"
