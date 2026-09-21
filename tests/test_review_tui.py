"""TUI side of review: `s` toggles the idle-only filter, `k` keeps."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("textual")

from handoff import db  # noqa: E402
from handoff.lifecycle import parse_frontmatter, render_frontmatter  # noqa: E402
from handoff.tui import HandoffTUI, _item_text  # noqa: E402

A = "aaaaaaaa-1111-4111-8111-111111111111"
B = "bbbbbbbb-2222-4222-8222-222222222222"


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(tmp_path):
    d = tmp_path / "c"; d.mkdir()
    dbf = tmp_path / "s.db"
    with db.connect(dbf) as conn:
        for sid, created in ((A, _iso(40)), (B, _iso(2))):
            fm = {"status": "in_progress", "title": f"t-{sid[:4]}", "session_id": sid, "cwd": "/x",
                  "created": created, "last_resumed": None, "completion_signal": "auto-default",
                  "archive_hash": None, "recap": None, "recap_source": None,
                  "hold_note": None, "hold_until": None}
            (d / f"{sid}.md").write_text(render_frontmatter(fm) + "U: hi\n")
            db.upsert_session(conn, fm=fm, body="U: hi\n")
    return d, dbf


def test_item_text_shows_idle_days_when_idle():
    row = {"status": "in_progress", "title": "t", "created": _iso(40), "session_id": A, "tokens": 10}
    assert "idle 40d" in _item_text(row)
    fresh = dict(row, created=_iso(1))
    assert "idle" not in _item_text(fresh)


def test_s_toggles_idle_filter_and_k_keeps(tmp_path):
    d, dbf = _seed(tmp_path)

    async def go():
        app = HandoffTUI(db_path=dbf, compaction_dir=str(d))
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(app._rows) == 2
            await pilot.press("s")
            await pilot.pause()
            assert [r["session_id"] for r in app._rows] == [A]
            await pilot.press("k")          # keep the idle one → leaves the idle view
            await pilot.pause()
            assert app._rows == []
            await pilot.press("s")
            await pilot.pause()
            assert len(app._rows) == 2

    asyncio.run(go())
    fm = parse_frontmatter((d / f"{A}.md").read_text())
    assert fm["status"] == "in_progress" and fm["last_resumed"]
