"""Textual 2-pane TUI for reviewing /hand:off sessions.

Left pane: a list of handoffs (multiline records — title wraps over a dim
date/status line), newest-first, fixed width. Right pane: the selected brief,
markdown-rendered (toggle with `m`) and scrollable.

Reads from sessions.db over a single long-lived connection (no per-keystroke
reconnect). Mutating actions route through `handoff.dbcli.do_*` so the brief
`.md` files stay authoritative.

Navigation: `j/k` (or arrows) move the list; `enter`/`tab`/click focus the
brief pane; `esc` returns to the list. `/` opens a live fuzzy filter over
title/recap/sid (across all statuses); `esc` clears it.

Textual is an optional dependency (`pip install -e '.[tui]'`); this module is
lazy-imported by `hand tui`.
"""
from __future__ import annotations

import shutil
import subprocess

from rich.markdown import Markdown as RichMarkdown
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from handoff import db, dbcli

# Above this body size, full-markdown render costs too much even via Rich;
# render the head styled + a note. Covers the rare giant brief (p99 ~100KB).
_MD_RENDER_CAP = 120_000
# Debounce: holding a cursor key shouldn't render every brief it skims past.
_SHOW_DEBOUNCE = 0.12
_STATUS_ICON = {
    "pending": "[dim]○[/]",       # empty — not started
    "in_progress": "[yellow]◐[/]",  # half — active
    "done": "[green]●[/]",        # full — complete
    "archived": "[blue]▣[/]",     # boxed — shelved
}
_EMPTY = "No sessions — run `/hand:off` or `hand rebuild`."


def _display_title(row: dict) -> str:
    """Title → first words of recap → short sid. Keeps the list readable for
    older briefs that predate the title field."""
    title = (row.get("title") or "").strip()
    if title:
        return title
    recap = (row.get("recap") or "").strip()
    if recap:
        words = recap.split()
        return " ".join(words[:10]) + ("…" if len(words) > 10 else "")
    return (row.get("session_id") or "")[:8]


def _fmt_tokens(n) -> str:
    if not n:
        return "?"
    n = int(n)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _item_text(row: dict) -> str:
    status = row.get("status") or "?"
    icon = _STATUS_ICON.get(status, "[dim]·[/]")
    date = (row.get("created") or "").split("T")[0] or "?"
    title = escape(_display_title(row))
    meta = f"{date} · ~{_fmt_tokens(row.get('tokens'))} tok"
    return f"{icon} {title}\n  [dim]{meta}[/dim]"


def _haystack(row: dict) -> str:
    """Searchable text for a row: title + recap + session id."""
    parts = (row.get("title"), row.get("recap"), row.get("session_id"))
    return " ".join(p for p in parts if p)


def _fuzzy_score(query: str, text: str) -> int | None:
    """Subsequence fuzzy match. Returns a relevance score (higher = better) if
    every char of `query` appears in `text` in order, else None. Rewards
    contiguous runs and word-boundary hits so `db` beats `d…b…` and a title
    starting with the query ranks above a mid-word match."""
    q = query.lower()
    t = text.lower()
    if not q:
        return 0
    score = 0
    ti = 0
    prev = -2
    run = 0
    first = None
    for qc in q:
        found = t.find(qc, ti)
        if found == -1:
            return None
        if first is None:
            first = found
        if found == prev + 1:
            run += 1
            score += 8 + run          # contiguous streak — grows with length
        else:
            run = 0
            score += 2
            if found == 0 or t[found - 1] in " ·-/_.":
                score += 6            # landed on a word boundary
        prev = found
        ti = found + 1
    score -= max(0, (prev - first + 1) - len(q))   # tighter cluster wins
    return score


class RenameScreen(ModalScreen[str]):
    """Centered input to rename a session's title. Dismisses with the new title,
    or None on cancel (esc)."""

    CSS = """
    RenameScreen { align: center middle; }
    #box {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: thick $accent;
    }
    #box Static { padding-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: str):
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="box"):
            yield Static("Rename session  ([dim]enter[/] save · [dim]esc[/] cancel)")
            yield Input(value=self._current, id="title-input")

    def on_mount(self) -> None:
        inp = self.query_one("#title-input", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class HandoffTUI(App):
    CSS = """
    /* Inactive list dims to grey; focusing it restores full colour + accent
       border, so the active pane is unmistakable. */
    #list {
        width: 46;
        border-right: solid $primary-darken-2;
        opacity: 0.55;
    }
    #list:focus {
        opacity: 1.0;
        border-right: solid $accent;
    }
    #list > ListItem { padding: 0 1; }
    #list > ListItem:even { background: $surface; }
    #list > ListItem:odd  { background: $panel; }
    /* Cursor row must win over the zebra bg — defined last, same specificity. */
    #list > ListItem.-highlight { background: $accent; color: $text; }
    #detail { width: 1fr; padding: 0 1; }
    #brief { width: 100%; }
    /* Fuzzy-search bar: hidden until `/`, docked above the panes. */
    #search { display: none; dock: top; height: 3; border: tall $accent; }
    #search.-visible { display: block; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("/", "search", "Search"),
        Binding("a", "toggle_done", "Done filter"),
        Binding("A", "toggle_archived", "Archived filter"),
        Binding("e", "archive", "Archive/unarchive"),
        Binding("c", "rename", "Rename"),
        Binding("m", "toggle_markdown", "MD on/off"),
        Binding("y", "copy_restore", "Copy restore"),
        Binding("Y", "copy_brief", "Copy brief"),
        Binding("enter,tab,l", "focus_detail", "Open brief"),
        Binding("escape,h", "focus_list", "Back to list"),
        Binding("d", "mark_done", "Done"),
        Binding("r", "reopen", "Reopen"),
        Binding("x", "delete", "Delete"),
        Binding("g", "scroll_top", "Top"),
        Binding("G", "scroll_bottom", "Bottom"),
        Binding("ctrl+d", "half_down", "½ down", show=False),
        Binding("ctrl+u", "half_up", "½ up", show=False),
        Binding("ctrl+f,pagedown", "page_down", "Page down", show=False),
        Binding("ctrl+b,pageup", "page_up", "Page up", show=False),
        Binding("ctrl+r", "reload", "Refresh", show=False),
        Binding("j", "nav_down", "Down", show=False),
        Binding("k", "nav_up", "Up", show=False),
    ]

    def __init__(self, *, db_path=None, compaction_dir=dbcli.DEFAULT_DIR):
        super().__init__()
        self._db_path = db_path
        self._dir = compaction_dir
        self._include_done = False
        self._include_archived = False
        self._rows: list[dict] = []
        self._pool: list[dict] = []
        self._query = ""
        self._conn = None
        self._detail_text = ""
        self._markdown = True
        self._cur_sid = None
        self._show_timer = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="fuzzy filter — type to search, enter to keep, esc to clear", id="search")
        with Horizontal():
            yield ListView(id="list")
            with VerticalScroll(id="detail"):
                yield Static(_EMPTY, id="brief", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self._conn = db.open_connection(self._db_path)
        await self._reload()
        self._list().focus()
        # Markdown-rendered text isn't selectable (Rich renderable exposes no
        # text map). When the user starts a mouse drag-select, flip the brief
        # to raw text so the selection captures real characters.
        try:
            self.screen.text_selection_started_signal.subscribe(
                self, self._on_selection_started
            )
        except Exception:
            pass

    def on_unmount(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- helpers ---------------------------------------------------------- #
    def _list(self) -> ListView:
        return self.query_one("#list", ListView)

    def _selected_sid(self) -> str | None:
        child = self._list().highlighted_child
        return child.name if child is not None else None

    def _selected_row(self) -> dict | None:
        sid = self._selected_sid()
        return next((r for r in self._rows if r["session_id"] == sid), None)

    # -- data ------------------------------------------------------------- #
    async def _reload(self) -> None:
        # Pool holds everything (incl. done + archived) so a fuzzy search can
        # surface any session regardless of the browse filters; toggles only
        # narrow the no-query view.
        self._pool = db.list_sessions(
            self._conn, include_done=True, include_archived=True
        )
        await self._repopulate()

    def _visible_rows(self) -> list[dict]:
        q = self._query.strip()
        if q:
            scored = [
                (s, r)
                for r in self._pool
                if (s := _fuzzy_score(q, _haystack(r))) is not None
            ]
            scored.sort(key=lambda t: -t[0])   # best match first; stable on ties
            return [r for _, r in scored]
        rows = self._pool
        if not self._include_done:
            rows = [r for r in rows if r.get("status") != "done"]
        if not self._include_archived:
            rows = [r for r in rows if r.get("status") != "archived"]
        return rows

    async def _repopulate(self) -> None:
        self._rows = self._visible_rows()
        lv = self._list()
        await lv.clear()
        for r in self._rows:
            await lv.append(ListItem(Static(_item_text(r)), name=r["session_id"]))
        if self._query.strip():
            self.sub_title = (
                f"filter {self._query.strip()!r} · {len(self._rows)} match"
                f" · md {'on' if self._markdown else 'off'}"
            )
        else:
            hidden = []
            if not self._include_done:
                hidden.append("done")
            if not self._include_archived:
                hidden.append("archived")
            self.sub_title = (
                f"{len(self._rows)} session(s)"
                f"{(' · hidden: ' + '+'.join(hidden)) if hidden else ''}"
                f" · md {'on' if self._markdown else 'off'}"
            )
        if self._rows:
            lv.index = 0
            self._show(self._rows[0]["session_id"])
        else:
            self._cur_sid = None
            self._detail_text = _EMPTY
            self.query_one("#brief", Static).update(_EMPTY)

    def _render_detail(self, body: str) -> None:
        st = self.query_one("#brief", Static)
        if not self._markdown:
            st.update(body)
            return
        if len(body) > _MD_RENDER_CAP:
            body = body[:_MD_RENDER_CAP] + (
                f"\n\n---\n\n*[truncated for display — {len(body)} chars total; "
                "press `m` for raw, or `hand show <sid>` for the full brief]*"
            )
        st.update(RichMarkdown(body))

    def _show(self, sid: str) -> None:
        row = db.get_session(self._conn, sid)
        body = (row or {}).get("body") or "(brief body missing)"
        self._cur_sid = sid
        self._detail_text = body
        self._render_detail(body)
        self.query_one("#detail", VerticalScroll).scroll_home(animate=False)

    def _schedule_show(self, sid: str) -> None:
        if self._show_timer is not None:
            self._show_timer.stop()
        self._show_timer = self.set_timer(_SHOW_DEBOUNCE, lambda: self._show(sid))

    # -- events ----------------------------------------------------------- #
    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # Arrow/j-k skim → debounced render of the brief the cursor lands on.
        if event.item is not None and event.item.name:
            self._schedule_show(event.item.name)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Click or enter → render now + jump focus into the brief pane.
        if event.item is not None and event.item.name:
            if self._show_timer is not None:
                self._show_timer.stop()
            self._show(event.item.name)
            self.query_one("#detail", VerticalScroll).focus()

    # -- search ----------------------------------------------------------- #
    def _search_input(self) -> Input:
        return self.query_one("#search", Input)

    def action_search(self) -> None:
        inp = self._search_input()
        inp.add_class("-visible")
        inp.value = self._query
        inp.focus()
        inp.cursor_position = len(inp.value)

    def _close_search(self, *, clear: bool) -> None:
        self._search_input().remove_class("-visible")
        if clear and self._query:
            self._query = ""
            self.run_worker(self._repopulate(), exclusive=True)

    def on_input_changed(self, event: Input.Changed) -> None:
        # Live fuzzy filter as the user types. Guard on id — the rename modal
        # has its own Input on a separate screen.
        if event.input.id == "search":
            self._query = event.value
            self.run_worker(self._repopulate(), exclusive=True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self._search_input().remove_class("-visible")  # keep filter, hide bar
            self._list().focus()

    # -- actions ---------------------------------------------------------- #
    def action_focus_detail(self) -> None:
        self.query_one("#detail", VerticalScroll).focus()

    def action_focus_list(self) -> None:
        # esc/h while the search bar is up → clear the filter and return.
        if self._search_input().has_class("-visible"):
            self._close_search(clear=True)
        self._list().focus()

    async def action_toggle_done(self) -> None:
        self._include_done = not self._include_done
        await self._reload()

    async def action_toggle_archived(self) -> None:
        self._include_archived = not self._include_archived
        await self._reload()

    def action_rename(self) -> None:
        row = self._selected_row()
        if not row:
            return
        sid = row["session_id"]
        current = row.get("title") or ""

        def _done(new_title: str | None) -> None:
            if new_title and new_title != current:
                dbcli.do_rename(
                    sid, new_title, compaction_dir=self._dir, db_path=self._db_path
                )
                self.notify(f"renamed {sid[:8]}")
                self.run_worker(self._reload(), exclusive=False)

        self.push_screen(RenameScreen(current), _done)

    async def action_archive(self) -> None:
        # Toggle: archive a live session, or unarchive an archived one.
        row = self._selected_row()
        if not row:
            return
        unarchive = row.get("status") == "archived"
        dbcli.do_archive(
            row["session_id"], unarchive=unarchive,
            compaction_dir=self._dir, db_path=self._db_path,
        )
        self.notify(f"{'unarchived' if unarchive else 'archived'} {row['session_id'][:8]}")
        await self._reload()

    def action_toggle_markdown(self) -> None:
        self._markdown = not self._markdown
        self.notify(f"markdown {'on' if self._markdown else 'off (raw)'}")
        if self._cur_sid:
            self._render_detail(self._detail_text)

    async def action_reload(self) -> None:
        await self._reload()

    def _detail_focused(self) -> bool:
        return self.focused is self.query_one("#detail", VerticalScroll)

    def action_nav_down(self) -> None:
        # j scrolls the brief when it's focused, else moves the list cursor.
        if self._detail_focused():
            self.query_one("#detail", VerticalScroll).scroll_down(animate=False)
        else:
            self._list().action_cursor_down()

    def action_nav_up(self) -> None:
        if self._detail_focused():
            self.query_one("#detail", VerticalScroll).scroll_up(animate=False)
        else:
            self._list().action_cursor_up()

    def action_scroll_top(self) -> None:
        if self._detail_focused():
            self.query_one("#detail", VerticalScroll).scroll_home(animate=False)
        elif self._rows:
            self._list().index = 0

    def action_scroll_bottom(self) -> None:
        if self._detail_focused():
            self.query_one("#detail", VerticalScroll).scroll_end(animate=False)
        elif self._rows:
            self._list().index = len(self._rows) - 1

    def _half_page(self, direction: int) -> None:
        det = self.query_one("#detail", VerticalScroll)
        step = max(1, det.size.height // 2)
        det.scroll_relative(y=direction * step, animate=False)

    def action_half_down(self) -> None:
        self._half_page(1)

    def action_half_up(self) -> None:
        self._half_page(-1)

    def action_page_down(self) -> None:
        self.query_one("#detail", VerticalScroll).scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        self.query_one("#detail", VerticalScroll).scroll_page_up(animate=False)

    def _copy(self, text: str) -> None:
        # OSC-52 (works over SSH where supported) + macOS pbcopy fallback.
        try:
            self.copy_to_clipboard(text)
        except Exception:
            pass
        pb = shutil.which("pbcopy")
        if pb:
            try:
                subprocess.run([pb], input=text.encode(), check=False)
            except Exception:
                pass

    def action_copy_restore(self) -> None:
        sid = self._selected_sid()
        if not sid:
            return
        cmd = f"/hand:on {sid}"
        self._copy(cmd)
        self.notify(f"copied: {cmd}")

    def action_copy_brief(self) -> None:
        if self._detail_text:
            self._copy(self._detail_text)
            self.notify("copied full brief to clipboard")

    def _on_selection_started(self, _screen) -> None:
        # Drag-select needs real text under the cursor → drop to raw mode.
        if self._markdown:
            self._markdown = False
            self._render_detail(self._detail_text)
            self.notify("raw mode — text selectable (cmd+c to copy, m to re-render)")

    async def _mutate(self, fn) -> None:
        sid = self._selected_sid()
        if not sid:
            return
        fn(sid)
        await self._reload()

    async def action_mark_done(self) -> None:
        await self._mutate(
            lambda sid: dbcli.do_done(
                sid, reopen=False, compaction_dir=self._dir, db_path=self._db_path
            )
        )

    async def action_reopen(self) -> None:
        await self._mutate(
            lambda sid: dbcli.do_done(
                sid, reopen=True, compaction_dir=self._dir, db_path=self._db_path
            )
        )

    async def action_delete(self) -> None:
        await self._mutate(
            lambda sid: dbcli.do_delete(
                sid, compaction_dir=self._dir, remove_file=False, db_path=self._db_path
            )
        )


def main(*, db_path=None, compaction_dir=dbcli.DEFAULT_DIR) -> None:
    HandoffTUI(db_path=db_path, compaction_dir=compaction_dir).run()


if __name__ == "__main__":
    main()
