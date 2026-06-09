"""`hand` — session CLI + the backend the /hand:* command bash blocks call.

Every mutation edits the brief `.md` frontmatter file AND the sessions.db row in
one process, so the authoritative file and its DB mirror never drift. The TUI's
mutating actions call the `do_*` helpers here for the same reason.

Subcommands: done, on, list, show, search, rm, rebuild, tui.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from handoff import db
from handoff.extract import extract_title, load_jsonl
from handoff.lifecycle import (
    now_iso,
    parse_frontmatter,
    render_frontmatter,
    strip_frontmatter,
)

DEFAULT_DIR = "~/.claude/compaction"
DEFAULT_PROJECTS = "~/.claude/projects"

_BADGES = {
    "done": "✓ done       ",
    "pending": "? pending    ",
    "in_progress": "… in_progress",
    "archived": "▣ archived   ",
}


# --------------------------------------------------------------------------- #
# brief-file helpers (file is authoritative; DB mirrors)
# --------------------------------------------------------------------------- #
def _brief_path(sid: str, compaction_dir: str) -> Path:
    return Path(os.path.expanduser(compaction_dir)) / f"{sid}.md"


def _read_split(p: Path) -> tuple[dict[str, str | None], str]:
    text = p.read_text(encoding="utf-8")
    return parse_frontmatter(text), strip_frontmatter(text)


def _write_brief(p: Path, fm: dict[str, str | None], body: str) -> None:
    p.write_text(render_frontmatter(fm) + body, encoding="utf-8")


def _first_user_line(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("U:"):
            return line[2:].strip()
    return None


# --------------------------------------------------------------------------- #
# mutation internals (shared by subcommands + TUI)
# --------------------------------------------------------------------------- #
def do_done(sid: str, *, reopen: bool, compaction_dir: str, db_path=None) -> tuple[bool, str]:
    """Flip a brief to done (or in_progress on reopen) with signal `manual`, in
    both the file and the DB. Returns (ok, message)."""
    p = _brief_path(sid, compaction_dir)
    if not p.is_file():
        return False, f"HANDDONE_ERROR no brief at {p}"
    fm, body = _read_split(p)
    if not fm:
        return False, "HANDDONE_ERROR brief has no frontmatter — run scripts/backfill_status.py first"

    status = "in_progress" if reopen else "done"
    action = "reopened" if reopen else "closed"
    fm["status"] = status
    fm["completion_signal"] = "manual"
    _write_brief(p, fm, body)
    with db.connect(db_path) as conn:
        db.set_status(conn, sid, status, "manual")
    return True, f"HANDDONE_OK action={action} sid={sid} status={status}"


def do_archive(sid: str, *, unarchive: bool, compaction_dir: str, db_path=None) -> tuple[bool, str]:
    """Shelve a session (status `archived`, signal `manual`) or restore it to
    `in_progress`, in both the file and the DB. Archived sessions are hidden
    from the default list/picker but kept intact. Sticky across /hand:off."""
    p = _brief_path(sid, compaction_dir)
    if not p.is_file():
        return False, f"HANDARCH_ERROR no brief at {p}"
    fm, body = _read_split(p)
    if not fm:
        return False, "HANDARCH_ERROR brief has no frontmatter — run scripts/backfill_status.py first"

    status = "in_progress" if unarchive else "archived"
    action = "unarchived" if unarchive else "archived"
    fm["status"] = status
    fm["completion_signal"] = "manual"
    _write_brief(p, fm, body)
    with db.connect(db_path) as conn:
        db.set_status(conn, sid, status, "manual")
    return True, f"HANDARCH_OK action={action} sid={sid} status={status}"


def do_rename(sid: str, title: str, *, compaction_dir: str, db_path=None) -> tuple[bool, str]:
    """Set a session's title in both the brief frontmatter and the DB row."""
    title = (title or "").strip()
    if not title:
        return False, "HANDRENAME_ERROR empty title"
    p = _brief_path(sid, compaction_dir)
    if not p.is_file():
        return False, f"HANDRENAME_ERROR no brief at {p}"
    fm, body = _read_split(p)
    if not fm:
        return False, "HANDRENAME_ERROR brief has no frontmatter"
    fm["title"] = title
    _write_brief(p, fm, body)
    with db.connect(db_path) as conn:
        db.upsert_session(conn, fm=fm, body=body, brief_path=str(p))
    return True, f"HANDRENAME_OK sid={sid} title={title}"


def do_resume(sid: str, *, compaction_dir: str, db_path=None) -> tuple[bool, str]:
    """/hand:on file+DB update: flip pending/in_progress → in_progress and stamp
    last_resumed. A `done` brief is left untouched (user must --reopen)."""
    p = _brief_path(sid, compaction_dir)
    if not p.is_file():
        return False, f"HANDON_ERROR no brief at {p}"
    fm, body = _read_split(p)
    if not fm:
        return False, "HANDON_ERROR brief has no frontmatter"
    if fm.get("status") == "done":
        return True, f"HANDON_DONE sid={sid} (brief marked done — not flipped)"

    ts = now_iso()
    fm["status"] = "in_progress"
    fm["last_resumed"] = ts
    _write_brief(p, fm, body)
    with db.connect(db_path) as conn:
        db.set_resumed(conn, sid, status="in_progress", last_resumed=ts)
    return True, f"HANDON_OK sid={sid} status=in_progress last_resumed={ts}"


def do_delete(sid: str, *, compaction_dir: str, remove_file: bool, db_path=None) -> tuple[bool, str]:
    """Delete the DB row. Removes the brief file too only when remove_file is
    set (briefs are restore sources — destructive)."""
    with db.connect(db_path) as conn:
        removed = db.delete_session(conn, sid)
    extra = ""
    if remove_file:
        p = _brief_path(sid, compaction_dir)
        if p.is_file():
            p.unlink()
            extra = " file=removed"
    if not removed and not extra:
        return False, f"HANDRM_ERROR no row for sid={sid}"
    return True, f"HANDRM_OK sid={sid} row={'removed' if removed else 'absent'}{extra}"


# --------------------------------------------------------------------------- #
# subcommand handlers
# --------------------------------------------------------------------------- #
def _cmd_done(args) -> int:
    ok, msg = do_done(
        args.sid, reopen=args.reopen, compaction_dir=args.dir, db_path=args.db
    )
    print(msg)
    return 0 if ok else 1


def _cmd_on(args) -> int:
    ok, msg = do_resume(args.sid, compaction_dir=args.dir, db_path=args.db)
    print(msg)
    return 0 if ok else 1


def _cmd_rename(args) -> int:
    ok, msg = do_rename(
        args.sid, " ".join(args.title), compaction_dir=args.dir, db_path=args.db
    )
    print(msg)
    return 0 if ok else 1


def _cmd_archive(args) -> int:
    ok, msg = do_archive(
        args.sid, unarchive=args.unarchive, compaction_dir=args.dir, db_path=args.db
    )
    print(msg)
    return 0 if ok else 1


def _cmd_unarchive(args) -> int:
    ok, msg = do_archive(
        args.sid, unarchive=True, compaction_dir=args.dir, db_path=args.db
    )
    print(msg)
    return 0 if ok else 1


def _cmd_list(args) -> int:
    cwd = None if args.any_cwd else (args.cwd or os.getcwd())
    with db.connect(args.db) as conn:
        rows = db.list_sessions(
            conn, cwd=cwd, include_done=args.all, include_archived=args.archived or args.all
        )
        # Fill goal fallback (first U: line) only for rows lacking a recap.
        goals: dict[str, str] = {}
        for r in rows:
            if not r.get("recap"):
                full = db.get_session(conn, r["session_id"])
                line = _first_user_line(full["body"] or "") if full else None
                if line:
                    goals[r["session_id"]] = line

    print(f"\nSession briefs in {os.path.expanduser(DEFAULT_DIR)}")
    if not args.all:
        print("(hiding done — pass --all to include)")
    if not args.any_cwd:
        print(f"(cwd={cwd} — pass --any-cwd to widen)")
    print()

    counts = {"pending": 0, "in_progress": 0, "done": 0, "archived": 0}
    for r in rows:
        status = r.get("status") or "?"
        counts[status] = counts.get(status, 0) + 1
        badge = _BADGES.get(status, f"{status}        ")
        short = (r["session_id"] or "")[:8]
        date = (r.get("created") or "").split("T")[0] or "?"
        cwd_base = os.path.basename((r.get("cwd") or "").rstrip("/"))
        print(f"  {short}  {badge}  {date}  {cwd_base}")
        goal = r.get("recap") or goals.get(r["session_id"])
        if goal:
            print(f"              {goal[:120]}")

    print(
        f"\n  pending: {counts.get('pending', 0)}  "
        f"in_progress: {counts.get('in_progress', 0)}  "
        f"done: {counts.get('done', 0)}  "
        f"archived: {counts.get('archived', 0)}"
    )
    return 0


def _cmd_show(args) -> int:
    with db.connect(args.db) as conn:
        row = db.get_session(conn, args.sid)
    if not row:
        print(f"HANDSHOW_ERROR no row for sid={args.sid}")
        return 1
    print(f"session_id: {row['session_id']}")
    for k in ("status", "title", "cwd", "created", "last_resumed", "recap"):
        print(f"{k}: {row.get(k)}")
    print("---")
    print(row.get("body") or "")
    return 0


def _cmd_search(args) -> int:
    with db.connect(args.db) as conn:
        rows = db.search_sessions(conn, args.query)
    if not rows:
        print(f"no matches for {args.query!r}")
        return 0
    for r in rows:
        status = r.get("status") or "?"
        badge = _BADGES.get(status, status)
        date = (r.get("created") or "").split("T")[0] or "?"
        print(f"  {r['session_id'][:8]}  {badge}  {date}  {r.get('title') or ''}")
        if r.get("recap"):
            print(f"              {r['recap'][:120]}")
    return 0


def _cmd_rm(args) -> int:
    ok, msg = do_delete(
        args.sid, compaction_dir=args.dir, remove_file=args.file, db_path=args.db
    )
    print(msg)
    return 0 if ok else 1


def _transcript_path(sid: str, cwd: str, projects_dir: str) -> Path:
    """Derive CC's transcript path from sid + cwd, matching the project-dir
    encoding used by /hand:off (`[^A-Za-z0-9-]` → `-`)."""
    enc = re.sub(r"[^A-Za-z0-9-]", "-", cwd)
    return Path(os.path.expanduser(projects_dir)) / enc / f"{sid}.jsonl"


def do_backfill_titles(
    *, compaction_dir: str, projects_dir: str, db_path=None
) -> dict[str, int]:
    """For every session missing a title, read its transcript, extract CC's
    ai-title, and write it into BOTH the brief frontmatter and the DB row.
    Sessions whose transcript is gone or carries no ai-title are left as-is
    (the TUI/list fall back to recap/sid for display)."""
    stats = {"scanned": 0, "updated": 0, "no_transcript": 0, "no_title": 0}
    with db.connect(db_path) as conn:
        rows = [r for r in db.list_sessions(conn, include_done=True) if not (r.get("title") or "").strip()]
        for r in rows:
            stats["scanned"] += 1
            sid = r["session_id"]
            cwd = r.get("cwd") or ""
            tpath = _transcript_path(sid, cwd, projects_dir)
            if not tpath.is_file():
                stats["no_transcript"] += 1
                continue
            title = extract_title(load_jsonl(str(tpath)))
            if not title:
                stats["no_title"] += 1
                continue
            brief = Path(r.get("brief_path") or _brief_path(sid, compaction_dir))
            if brief.is_file():
                fm, body = _read_split(brief)
                if fm:
                    fm["title"] = title
                    _write_brief(brief, fm, body)
                    db.upsert_session(conn, fm=fm, body=body, brief_path=str(brief))
                    stats["updated"] += 1
                    continue
            # No usable brief file — patch the DB row's title directly.
            conn.execute(
                "UPDATE sessions SET title = ?, indexed_at = ? WHERE session_id = ?",
                (title, now_iso(), sid),
            )
            stats["updated"] += 1
    return stats


def _cmd_backfill_titles(args) -> int:
    stats = do_backfill_titles(
        compaction_dir=args.dir, projects_dir=args.projects, db_path=args.db
    )
    print(
        f"titles: scanned={stats['scanned']} updated={stats['updated']} "
        f"no_transcript={stats['no_transcript']} no_title={stats['no_title']}"
    )
    return 0


def _cmd_rebuild(args) -> int:
    with db.connect(args.db) as conn:
        stats = db.rebuild_from_briefs(conn, args.dir)
    print(
        f"rebuilt: scanned={stats['scanned']} upserted={stats['upserted']} "
        f"skipped={stats['skipped']} deleted={stats['deleted']}"
    )
    return 0


def _cmd_tui(args) -> int:
    try:
        from handoff import tui
    except ImportError:
        sys.stderr.write(
            "TUI needs the optional Textual dependency.\n"
            "Install it with:  pip install -e '.[tui]'\n"
        )
        return 1
    tui.main(db_path=args.db, compaction_dir=args.dir)
    return 0


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #
def _add_db_args(p: argparse.ArgumentParser, *, dir_too: bool = True) -> None:
    if dir_too:
        p.add_argument("--dir", default=DEFAULT_DIR, help="Compaction dir")
    else:
        p.set_defaults(dir=DEFAULT_DIR)
    p.add_argument("--db", default=None, help="DB path override")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hand", description="Session index CLI + TUI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("done", help="Mark a brief done (or --reopen)")
    pd.add_argument("sid")
    pd.add_argument("--reopen", action="store_true")
    _add_db_args(pd)
    pd.set_defaults(func=_cmd_done)

    po = sub.add_parser("on", help="Mark a brief resumed (status+last_resumed)")
    po.add_argument("sid")
    _add_db_args(po)
    po.set_defaults(func=_cmd_on)

    pl = sub.add_parser("list", help="List sessions grouped/filtered")
    pl.add_argument("--all", action="store_true", help="Include done + archived")
    pl.add_argument("--archived", action="store_true", help="Include archived")
    pl.add_argument("--any-cwd", action="store_true", help="All cwds")
    pl.add_argument("--cwd", default=None, help="Filter to this cwd")
    _add_db_args(pl)
    pl.set_defaults(func=_cmd_list)

    prn = sub.add_parser("rename", help="Set a session's title")
    prn.add_argument("sid")
    prn.add_argument("title", nargs="+", help="New title (unquoted words ok)")
    _add_db_args(prn)
    prn.set_defaults(func=_cmd_rename)

    pa = sub.add_parser("archive", help="Shelve a session (hide-but-keep)")
    pa.add_argument("sid")
    pa.add_argument("--unarchive", action="store_true", help="Restore instead")
    _add_db_args(pa)
    pa.set_defaults(func=_cmd_archive)

    pua = sub.add_parser("unarchive", help="Restore an archived session")
    pua.add_argument("sid")
    _add_db_args(pua)
    pua.set_defaults(func=_cmd_unarchive)

    ps = sub.add_parser("show", help="Print a session's metadata + body")
    ps.add_argument("sid")
    _add_db_args(ps)
    ps.set_defaults(func=_cmd_show)

    psr = sub.add_parser("search", help="Substring search title/recap/body")
    psr.add_argument("query")
    _add_db_args(psr)
    psr.set_defaults(func=_cmd_search)

    pr = sub.add_parser("rm", help="Delete a session row (--file also removes the brief)")
    pr.add_argument("sid")
    pr.add_argument("--file", action="store_true", help="Also delete the brief .md")
    _add_db_args(pr)
    pr.set_defaults(func=_cmd_rm)

    pb = sub.add_parser("rebuild", help="Rebuild the DB from brief files")
    _add_db_args(pb)
    pb.set_defaults(func=_cmd_rebuild)

    pbt = sub.add_parser("backfill-titles", help="Recover missing titles from transcripts")
    pbt.add_argument("--projects", default=DEFAULT_PROJECTS, help="CC projects dir")
    _add_db_args(pbt)
    pbt.set_defaults(func=_cmd_backfill_titles)

    pt = sub.add_parser("tui", help="Launch the 2-pane TUI")
    _add_db_args(pt)
    pt.set_defaults(func=_cmd_tui)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
