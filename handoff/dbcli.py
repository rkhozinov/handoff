"""`hand` — session CLI + the backend the /hand:* command bash blocks call.

Every mutation edits the brief `.md` frontmatter file AND the sessions.db row in
one process, so the authoritative file and its DB mirror never drift. The TUI's
mutating actions call the `do_*` helpers here for the same reason.

Subcommands: done, on, list, show, search, rm, rebuild, tui, tasks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from handoff import cli, db, tasks
from handoff.fsutil import atomic_write
from handoff.extract import extract_title, load_jsonl
from handoff.lifecycle import (
    is_due,
    now_iso,
    parse_frontmatter,
    parse_hold_until,
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
    "on_hold": "⏸ on_hold    ",
}


# --------------------------------------------------------------------------- #
# brief-file helpers (file is authoritative; DB mirrors)
# --------------------------------------------------------------------------- #
# A sid is a filename component. Real ones are UUIDs, tests use short slugs;
# what must never pass is a path (`../x`, `a/b`) — `hand rm --file ../../x`
# would unlink outside the compaction dir (review 2026-09-21).
_SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _check_sid(sid: str) -> str:
    if not sid or sid in (".", "..") or not _SID_RE.match(sid):
        raise ValueError(f"bad-session-id sid={sid!r}")
    return sid


def _brief_path(sid: str, compaction_dir: str) -> Path:
    return Path(os.path.expanduser(compaction_dir)) / f"{_check_sid(sid)}.md"


def _read_split(p: Path) -> tuple[dict[str, str | None], str]:
    text = p.read_text(encoding="utf-8")
    return parse_frontmatter(text), strip_frontmatter(text)


def _write_brief(p: Path, fm: dict[str, str | None], body: str) -> None:
    # Atomic: the brief is the authoritative artifact; a half-written one
    # resets `created`/`last_resumed` on the next /hand:off.
    atomic_write(p, render_frontmatter(fm) + body)


def _first_user_line(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("U:"):
            return line[2:].strip()
    return None


# --------------------------------------------------------------------------- #
# mutation internals (shared by subcommands + TUI)
# --------------------------------------------------------------------------- #
def _sync_row(conn, sid: str, matched: bool, fm, body, p: Path) -> None:
    """A file mutation whose UPDATE matched no row (e.g. `hand rm` without
    --file, or a DB rebuilt from a subset) must not print *_OK over a
    missing row — mirror the file into the DB so list/TUI agree with it."""
    if not matched:
        db.upsert_session(conn, fm=fm, body=body, brief_path=str(p))


def do_done(sid: str, *, reopen: bool, compaction_dir: str, db_path=None) -> tuple[bool, str]:
    """Flip a brief to done (or in_progress on reopen) with signal `manual`, in
    both the file and the DB. Returns (ok, message)."""
    try:
        p = _brief_path(sid, compaction_dir)
    except ValueError as e:
        return False, f"HANDDONE_ERROR {e}"
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
        _sync_row(conn, sid, db.set_status(conn, sid, status, "manual"), fm, body, p)
    return True, f"HANDDONE_OK action={action} sid={sid} status={status}"


def do_archive(sid: str, *, unarchive: bool, compaction_dir: str, db_path=None) -> tuple[bool, str]:
    """Shelve a session (status `archived`, signal `manual`) or restore it to
    `in_progress`, in both the file and the DB. Archived sessions are hidden
    from the default list/picker but kept intact. Sticky across /hand:off."""
    try:
        p = _brief_path(sid, compaction_dir)
    except ValueError as e:
        return False, f"HANDARCH_ERROR {e}"
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
        _sync_row(conn, sid, db.set_status(conn, sid, status, "manual"), fm, body, p)
    return True, f"HANDARCH_OK action={action} sid={sid} status={status}"


def do_hold(
    sid: str, *, note: str | None, until: str | None, release: bool,
    compaction_dir: str, db_path=None,
) -> tuple[bool, str]:
    """Shelve a session on hold (status `on_hold`, optional note/deadline) or
    release it back to `in_progress`, in both the file and the DB."""
    try:
        p = _brief_path(sid, compaction_dir)
    except ValueError as e:
        return False, f"HANDHOLD_ERROR {e}"
    if not p.is_file():
        return False, f"HANDHOLD_ERROR no brief at {p}"
    fm, body = _read_split(p)
    if not fm:
        return False, "HANDHOLD_ERROR brief has no frontmatter"

    if release:
        fm["status"] = "in_progress"
        fm["completion_signal"] = "manual"
        fm["hold_until"] = None
        _write_brief(p, fm, body)
        with db.connect(db_path) as conn:
            _sync_row(conn, sid, db.set_hold(conn, sid, status="in_progress", signal="manual",
                                             note=fm.get("hold_note"), until=fm.get("hold_until")), fm, body, p)
        return True, f"HANDHOLD_OK action=released sid={sid}"

    if until is not None and parse_hold_until(until) is None:
        return False, f"HANDHOLD_ERROR reason=bad-date until={until!r} (want YYYY-MM-DD)"
    fm["status"] = "on_hold"
    fm["completion_signal"] = "manual"
    if note:
        fm["hold_note"] = note
    fm["hold_until"] = until
    _write_brief(p, fm, body)
    with db.connect(db_path) as conn:
        _sync_row(conn, sid, db.set_hold(conn, sid, status="on_hold", signal="manual",
                                         note=fm.get("hold_note"), until=fm.get("hold_until")), fm, body, p)
    return True, f"HANDHOLD_OK action=held sid={sid} until={fm.get('hold_until')}"


def do_rename(sid: str, title: str, *, compaction_dir: str, db_path=None) -> tuple[bool, str]:
    """Set a session's title in both the brief frontmatter and the DB row."""
    title = (title or "").strip()
    if not title:
        return False, "HANDRENAME_ERROR empty title"
    try:
        p = _brief_path(sid, compaction_dir)
    except ValueError as e:
        return False, f"HANDRENAME_ERROR {e}"
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
    try:
        p = _brief_path(sid, compaction_dir)
    except ValueError as e:
        return False, f"HANDON_ERROR {e}"
    if not p.is_file():
        return False, f"HANDON_ERROR no brief at {p}"
    fm, body = _read_split(p)
    if not fm:
        return False, "HANDON_ERROR brief has no frontmatter"
    if fm.get("status") == "done":
        return True, f"HANDON_DONE sid={sid} (brief marked done — not flipped)"
    if fm.get("status") == "archived":
        # Archived is a manual, sticky decision; reading the brief is fine,
        # silently un-archiving it is not (/hand:archive --unarchive does that).
        return True, f"HANDON_ARCHIVED sid={sid} (brief archived — not flipped)"

    ts = now_iso()
    fm["status"] = "in_progress"
    fm["last_resumed"] = ts
    # Resuming releases a hold; db.set_resumed already clears hold_until on
    # the DB row, the file must agree.
    fm["hold_until"] = None
    _write_brief(p, fm, body)
    with db.connect(db_path) as conn:
        _sync_row(conn, sid, db.set_resumed(conn, sid, status="in_progress", last_resumed=ts), fm, body, p)
    return True, f"HANDON_OK sid={sid} status=in_progress last_resumed={ts}"


def do_delete(sid: str, *, compaction_dir: str, remove_file: bool, db_path=None) -> tuple[bool, str]:
    """Delete the DB row. Removes the brief file too only when remove_file is
    set (briefs are restore sources — destructive)."""
    try:
        p = _brief_path(sid, compaction_dir)
    except ValueError as e:
        return False, f"HANDRM_ERROR {e}"
    with db.connect(db_path) as conn:
        removed = db.delete_session(conn, sid)
    extra = ""
    if remove_file:
        if p.is_file():
            p.unlink()
            extra = " file=removed"
    if not removed and not extra:
        return False, f"HANDRM_ERROR no row for sid={sid}"
    return True, f"HANDRM_OK sid={sid} row={'removed' if removed else 'absent'}{extra}"


def do_off(
    sid: str, *, cwd: str, recap: str | None, hold: str | None, until: str | None,
    projects_dir: str, compaction_dir: str, db_path,
    no_archive: bool, no_agent_store: bool, tasks_root: str, bundle_dir: str,
) -> tuple[int, list[str]]:
    """The /hand:off pipeline: locate the transcript, run `cli.run`, export
    tasks (best-effort), optionally place a hold, and render the operator-
    facing block. Contracts pinned in tests/test_dbcli_off_on.py."""
    try:
        sid = _check_sid(sid)
    except ValueError as e:
        return 1, [f"HANDOFF_ERROR {e}"]

    matches = sorted(Path(os.path.expanduser(projects_dir)).glob(f"*/{sid}.jsonl"))
    if not matches:
        return 1, [f"HANDOFF_ERROR sid={sid} transcript="]
    transcript = matches[0]

    cli_args = [
        "--transcript", str(transcript), "--session-id", sid, "--cwd", cwd,
        "--out-dir", compaction_dir,
    ]
    if recap:
        cli_args += ["--recap", recap]
    if no_archive:
        cli_args.append("--no-archive")
    if no_agent_store:
        cli_args.append("--no-agent-store")
    if db_path:
        cli_args += ["--db", db_path]

    try:
        result = cli.run(cli.parse_args(cli_args))
    except Exception as e:  # the brief pipeline exploded — report, don't raise
        sys.stderr.write(f"{e}\n")
        return 1, ["HANDOFF_ERROR: handoff.cli failed (exit=1, brief=[])"]

    fm = result.fm

    tasks_line = "none in this session"
    try:
        _, tlines, _ = do_tasks_export(
            sid, tasks_root=tasks_root, bundle_dir=bundle_dir, out=None, list_id=None
        )
        first = tlines[0] if tlines else ""
        if first.startswith("HANDTASKS_OK"):
            m = re.search(r"tasks=(\d+)", first)
            tasks_line = f"exported ({m.group(1) if m else '?'} tasks)"
        elif first.startswith("HANDTASKS_EMPTY"):
            tasks_line = "none in this session"
        else:
            tasks_line = "skipped"
    except Exception:
        tasks_line = "skipped"

    saved_pct = 100 * (1 - result.brief_bytes / max(1, result.raw_bytes))
    lines = [
        "HANDOFF_OK",
        f"  recap:      {fm.get('recap') or ''}",
        f"  session_id: {sid}",
        f"  brief:      {result.brief_path}",
        f"  size:       brief={result.brief_bytes}B raw={result.raw_bytes}B saved={saved_pct:.1f}%",
        f"  status:     {fm['status']} ({fm['completion_signal']})",
        f"  tasks:      {tasks_line}",
        "  db:         ~/.claude/compaction/sessions.db (row upserted)",
        "",
    ]
    if fm["status"] == "done":
        lines += [
            "Detector marked this session DONE — it's hidden from the",
            f"/hand:on picker. To resume anyway: /hand:on {sid}",
            f"To revive permanently: /hand:done {sid} --reopen",
        ]
    else:
        lines += [
            "Restore with either:",
            f"  /hand:on {sid}",
            f"  /hand:on {result.brief_path}",
            "  park it:   /hand:hold <why> [--until YYYY-MM-DD]",
        ]

    rc = 0
    if hold:
        ok, msg = do_hold(
            sid, note=hold, until=until, release=False,
            compaction_dir=compaction_dir, db_path=db_path,
        )
        lines.append(msg)
        if ok:
            lines.append(f"resume: cd {fm['cwd']} && claude --resume {sid}   |  /hand:on {sid}")
        else:
            rc = 1

    return rc, lines


def do_on_restore(
    args: list[str], *, current_sid: str, show_all: bool,
    compaction_dir: str, db_path, tasks_root: str, bundle_dir: str,
) -> tuple[int, list[str]]:
    """The /hand:on pipeline: resolve each arg to a brief, resume it, merge
    its task bundle into the current session — or print the picker when
    nothing resolved. Contracts pinned in tests/test_dbcli_off_on.py."""
    compaction_path = Path(os.path.expanduser(compaction_dir))
    tokens = [a for a in args if a != "--all"]
    lines: list[str] = []
    resolved: list[Path] = []

    def _resolve(tok: str) -> Path | None:
        p = Path(tok)
        if p.is_file():
            return p
        cand = compaction_path / f"{tok}.md"
        return cand if cand.is_file() else None

    def _emit_resolution(tok: str, p: Path | None) -> None:
        if p is None:
            lines.append(f"BRIEF_MISSING arg={tok} sid={current_sid} show_all={int(show_all)}")
            return
        fm = parse_frontmatter(p.read_text(encoding="utf-8"))
        lines.append(f"BRIEF_PATH={p}")
        lines.append(f"BRIEF_STATUS={(fm or {}).get('status') or ''}")
        resolved.append(p)

    if tokens:
        for tok in tokens:
            _emit_resolution(tok, _resolve(tok))
    else:
        _emit_resolution("", _resolve(current_sid))

    if resolved:
        for p in resolved:
            sid = p.stem
            if not _SID_RE.match(sid):
                continue
            ok, msg = do_resume(sid, compaction_dir=compaction_dir, db_path=db_path)
            lines.append(msg)
            bpath = tasks.bundle_path(sid, base=bundle_dir)
            if not bpath.is_file():
                continue
            bundle, err = _load_bundle(str(bpath))
            if bundle is None:
                lines.append(err)
                continue
            _, tlines, _ = do_tasks_import(
                bundle, current_sid, op="import",
                tasks_root=tasks_root, bundle_dir=bundle_dir, list_id=None,
                mode=tasks.MODE_MERGE, only_open=True, dry_run=False,
            )
            if tlines and not tlines[0].startswith("HANDTASKS_EMPTY"):
                lines.extend(tlines)
        return 0, lines

    # Nothing resolved — the picker. Ordered `created DESC` from the DB
    # (deliberate divergence from the old ls-t/awk picker's mtime order,
    # which also showed `archived`; this one hides it unless --all).
    if show_all:
        lines.append(
            "No brief found. ALL recent briefs (newest first, including done). "
            "Reply with the number or session id."
        )
    else:
        lines.append(
            "No brief found. Open briefs (newest first, done hidden — pass --all "
            "to include). Reply with the number or session id."
        )
    lines.append("")
    with db.connect(db_path) as conn:
        rows = db.list_sessions(conn, cwd=None, include_done=show_all, include_archived=show_all)
        goals: dict[str, str] = {}
        for r in rows[:10]:
            if not r.get("recap"):
                full = db.get_session(conn, r["session_id"])
                line = _first_user_line(full["body"] or "") if full else None
                if line:
                    goals[r["session_id"]] = line

    for i, r in enumerate(rows[:10], start=1):
        sid = r["session_id"]
        status = r.get("status") or "?"
        created = r.get("created") or ""
        bp = r.get("brief_path")
        bpath = Path(bp) if bp else compaction_path / f"{sid}.md"
        size = bpath.stat().st_size if bpath.is_file() else "?"
        lines.append(f"{i:2d}. {sid}  [{status}]  ({created}, {size} bytes)")
        if r.get("cwd"):
            lines.append(f"    cwd:  {r['cwd']}")
        goal = r.get("recap") or goals.get(sid)
        if goal:
            lines.append(f"    goal: {goal[:120]}")
        lines.append("")
    return 1, lines


# --------------------------------------------------------------------------- #
# subcommand handlers
# --------------------------------------------------------------------------- #
def _cmd_done(args) -> int:
    ok, msg = do_done(
        args.sid, reopen=args.reopen, compaction_dir=args.dir, db_path=args.db
    )
    print(msg)
    return 0 if ok else 1


def _cmd_prune_archives(args) -> int:
    from handoff.archive import prune_archives

    stats = prune_archives(
        days=args.days,
        dry_run=not args.apply,
        db_path=args.db,
        limit=args.limit,
        open_days=args.open_days,
    )
    print(json.dumps(stats, indent=2))
    return 1 if stats.get("error") else 0


def _cmd_on(args) -> int:
    if getattr(args, "restore", False):
        rc, lines = do_on_restore(
            args.sid, current_sid=args.current, show_all=args.all,
            compaction_dir=args.dir, db_path=args.db,
            tasks_root=args.tasks_dir, bundle_dir=args.bundle_dir,
        )
        for line in lines:
            print(line)
        return rc
    rc = 0
    for sid in args.sid:
        ok, msg = do_resume(sid, compaction_dir=args.dir, db_path=args.db)
        print(msg)
        if not ok:
            rc = 1
    return rc


def _cmd_off(args) -> int:
    if args.until and not args.hold:
        sys.stderr.write("hand off: --until requires --hold\n")
        return 2
    recap = sys.stdin.read().strip() if args.recap_stdin else None
    rc, lines = do_off(
        args.sid, cwd=args.cwd, recap=recap, hold=args.hold, until=args.until,
        projects_dir=args.projects, compaction_dir=args.dir, db_path=args.db,
        no_archive=args.no_archive, no_agent_store=args.no_agent_store,
        tasks_root=args.tasks_dir, bundle_dir=args.bundle_dir,
    )
    for line in lines:
        print(line)
    return rc


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


def _cmd_hold(args) -> int:
    ok, msg = do_hold(
        args.sid, note=args.note, until=args.until, release=args.release,
        compaction_dir=args.dir, db_path=args.db,
    )
    print(msg)
    return 0 if ok else 1


def _cmd_holds(args) -> int:
    with db.connect(args.db) as conn:
        rows = db.list_holds(conn)
    today = date.today()
    if args.due:
        rows = [r for r in rows if is_due(r, today=today)]
    for r in rows:
        sid = r["session_id"]
        due = r.get("hold_until")
        mark = "⏰" if is_due(r, today=today) else "⏸"
        head = f"{mark} {sid[:8]}"
        if due:
            head += f"  due {due}"
        print(f"{head}  {r.get('title') or r.get('recap') or ''}")
        if r.get("hold_note"):
            print(f"    note:   {r['hold_note']}")
        print(f"    resume: cd {r.get('cwd') or '.'} && claude --resume {sid}   |  /hand:on {sid}")
        print()
    return 0


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

    counts = {"pending": 0, "in_progress": 0, "done": 0, "archived": 0, "on_hold": 0}
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
        f"archived: {counts.get('archived', 0)}  "
        f"on_hold: {counts.get('on_hold', 0)}"
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
        rows = [
            r for r in db.list_sessions(conn, include_done=True, include_archived=True)
            if not (r.get("title") or "").strip()
        ]
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


# --------------------------------------------------------------------------- #
# task carry-over (`hand tasks …`)
#
# Machine-readable first token, like HANDOFF_OK / HANDDONE_OK — the /hand:*
# bash blocks branch on it. HANDTASKS_EMPTY is rc 0 on purpose: a session with
# no tasks is normal and must never break /hand:off or /hand:on.
# --------------------------------------------------------------------------- #
def _short(sid: str) -> str:
    try:
        return tasks.short_id(sid)
    except ValueError:
        return sid


def _emit(lines: list[str], warnings: list[str]) -> None:
    for line in lines:
        print(line)
    for w in warnings:
        print(f"  warn: {w}")


def _load_bundle(path: str) -> tuple[dict | None, str]:
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return None, f"HANDTASKS_ERROR reason=no-such-bundle bundle={p}"
    try:
        bundle = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"HANDTASKS_ERROR reason=bad-bundle bundle={p} ({e})"
    if not isinstance(bundle, dict) or not isinstance(bundle.get("tasks"), list):
        return None, f"HANDTASKS_ERROR reason=bad-bundle bundle={p} (no tasks array)"
    return bundle, ""


def do_tasks_export(
    sid: str, *, tasks_root: str, bundle_dir: str, out: str | None, list_id: str | None
) -> tuple[int, list[str], list[str]]:
    """Read a session's task dir → a bundle file. Lossless: completed tasks
    are kept, because filtering is an import-time decision."""
    try:
        src_dir = tasks.tasks_dir(sid, base=tasks_root, list_id=list_id)
    except ValueError:
        return 1, [f"HANDTASKS_ERROR reason=bad-session-id sid={sid}"], []

    found, warnings = tasks.read_tasks(src_dir)
    if not found:
        return 0, [f"HANDTASKS_EMPTY sid={_short(sid)} (no tasks to export)"], warnings

    dest = Path(os.path.expanduser(out)) if out else tasks.bundle_path(sid, base=bundle_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(tasks.make_bundle(sid, found), indent=2) + "\n", encoding="utf-8"
    )
    return 0, [
        f"HANDTASKS_OK op=export sid={_short(sid)} tasks={len(found)} out={dest}"
    ], warnings


def do_tasks_import(
    bundle: dict,
    dest_sid: str,
    *,
    op: str,
    tasks_root: str,
    bundle_dir: str,
    list_id: str | None,
    mode: str,
    only_open: bool,
    dry_run: bool,
) -> tuple[int, list[str], list[str]]:
    """Merge a bundle into a destination session's task dir."""
    try:
        dest_dir = tasks.tasks_dir(dest_sid, base=tasks_root, list_id=list_id)
        manifest = tasks.manifest_path(dest_sid, base=bundle_dir)
    except ValueError:
        return 1, [f"HANDTASKS_ERROR reason=bad-session-id sid={dest_sid}"], []

    source = str(bundle.get("source_session_id") or "?")
    if not bundle.get("tasks"):
        return 0, [f"HANDTASKS_EMPTY sid={_short(source)} (bundle has no tasks)"], []

    dest_tasks, warnings = tasks.read_tasks(dest_dir)
    plan = tasks.plan_import(
        bundle,
        dest_tasks,
        mode=mode,
        only_open=only_open,
        already_imported=tasks.read_manifest(manifest),
        id_floor=tasks.read_id_floor(dest_dir),
    )
    warnings.extend(plan.warnings)

    token = "HANDTASKS_DRYRUN" if dry_run else "HANDTASKS_OK"
    summary = (
        f"{token} op={op} source={_short(source)} dest={_short(dest_sid)} "
        f"imported={plan.imported} skipped={plan.skipped} "
        f"dropped_refs={len(plan.dropped_refs)} mode={plan.mode}"
    )
    if dry_run:
        summary += " (nothing written)"
    else:
        tasks.apply_plan(plan, dest_dir, manifest)

    lines = [summary]
    for src_id, field, ref in plan.dropped_refs:
        # Either the ref was already dangling at export time, or --only-open
        # filtered its target out. From here the two are indistinguishable.
        lines.append(f"  dropped: task {src_id}.{field} → {ref} (not imported)")
    if plan.stripped_owners:
        lines.append(f"  owner cleared on: {', '.join(plan.stripped_owners)}")
    return 0, lines, warnings


def _cmd_tasks_list(args) -> int:
    try:
        d = tasks.tasks_dir(args.sid, base=args.tasks_dir, list_id=args.list_id)
    except ValueError:
        print(f"HANDTASKS_ERROR reason=bad-session-id sid={args.sid}")
        return 1
    found, warnings = tasks.read_tasks(d)
    if not found:
        _emit([f"HANDTASKS_EMPTY sid={_short(args.sid)} (no tasks)"], warnings)
        return 0

    lines = [f"HANDTASKS_OK op=list sid={_short(args.sid)} tasks={len(found)}", ""]
    for t in found:
        edges = " ".join(
            f"{f}={','.join(t[f])}" for f in tasks.REF_FIELDS if t.get(f)
        )
        lines.append(f"  {t['id']:>3}  {t['status']:<12}  {t['subject']}")
        if edges:
            lines.append(f"       {edges}")
    _emit(lines, warnings)
    return 0


def _cmd_tasks_export(args) -> int:
    rc, lines, warnings = do_tasks_export(
        args.sid,
        tasks_root=args.tasks_dir,
        bundle_dir=args.bundle_dir,
        out=args.out,
        list_id=args.list_id,
    )
    _emit(lines, warnings)
    return rc


def _cmd_tasks_import(args) -> int:
    bundle, err = _load_bundle(args.bundle)
    if bundle is None:
        print(err)
        return 1
    rc, lines, warnings = do_tasks_import(
        bundle,
        args.to,
        op="import",
        tasks_root=args.tasks_dir,
        bundle_dir=args.bundle_dir,
        list_id=args.list_id,
        mode=tasks.MODE_REPLACE if args.replace else tasks.MODE_MERGE,
        only_open=not args.all,
        dry_run=args.dry_run,
    )
    _emit(lines, warnings)
    return rc


def _cmd_tasks_copy(args) -> int:
    """export | import in one call, without a bundle file on disk."""
    try:
        src_dir = tasks.tasks_dir(
            getattr(args, "from"), base=args.tasks_dir, list_id=args.from_list_id
        )
    except ValueError:
        print(f"HANDTASKS_ERROR reason=bad-session-id sid={getattr(args, 'from')}")
        return 1
    found, warnings = tasks.read_tasks(src_dir)
    if not found:
        _emit(
            [f"HANDTASKS_EMPTY sid={_short(getattr(args, 'from'))} (no tasks to copy)"],
            warnings,
        )
        return 0

    rc, lines, more = do_tasks_import(
        tasks.make_bundle(getattr(args, "from"), found),
        args.to,
        op="copy",
        tasks_root=args.tasks_dir,
        bundle_dir=args.bundle_dir,
        list_id=args.list_id,
        mode=tasks.MODE_REPLACE if args.replace else tasks.MODE_MERGE,
        only_open=not args.all,
        dry_run=args.dry_run,
    )
    _emit(lines, warnings + more)
    return rc


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

    pp = sub.add_parser(
        "prune-archives",
        help="Report (or with --apply delete) session-archive docs older than N days whose brief is finished",
    )
    pp.add_argument("--days", type=int, default=30, help="Retention window (default 30)")
    pp.add_argument("--apply", action="store_true", help="Actually delete (default: report only)")
    pp.add_argument("--limit", type=int, default=None, help="Cap deletions this run")
    pp.add_argument(
        "--open-days",
        type=int,
        default=90,
        help="How long an open brief keeps its archive after it was last touched (default 90)",
    )
    _add_db_args(pp)
    pp.set_defaults(func=_cmd_prune_archives)

    po = sub.add_parser(
        "on", help="Mark a brief resumed (status+last_resumed), or --restore for the full /hand:on pipeline"
    )
    po.add_argument("sid", nargs="*", help="one or more session ids (or, with --restore, ids/paths)")
    po.add_argument(
        "--restore", action="store_true",
        help="Resolve args to briefs, resume + merge tasks, or print the picker (the /hand:on pipeline)",
    )
    po.add_argument("--current", default=None, help="Current session id (--restore)")
    po.add_argument("--all", action="store_true", help="Picker: include done/archived (--restore)")
    po.add_argument("--tasks-dir", default=tasks.DEFAULT_TASKS_DIR, help="CC tasks dir (--restore)")
    po.add_argument("--bundle-dir", default=tasks.DEFAULT_BUNDLE_DIR, help="Bundle dir (--restore)")
    _add_db_args(po)
    po.set_defaults(func=_cmd_on)

    poff = sub.add_parser("off", help="Run the /hand:off pipeline (trim, archive, export tasks, DB upsert)")
    poff.add_argument("sid")
    poff.add_argument("--cwd", required=True)
    poff.add_argument("--recap-stdin", action="store_true", help="Read the recap from stdin")
    poff.add_argument("--hold", default=None, help="Also place the session on hold with this note")
    poff.add_argument("--until", default=None, help="Hold resume-by date YYYY-MM-DD (requires --hold)")
    poff.add_argument("--projects", default=DEFAULT_PROJECTS, help="CC projects dir")
    poff.add_argument("--no-archive", action="store_true")
    poff.add_argument("--no-agent-store", action="store_true")
    poff.add_argument("--tasks-dir", default=tasks.DEFAULT_TASKS_DIR, help="CC tasks dir")
    poff.add_argument("--bundle-dir", default=tasks.DEFAULT_BUNDLE_DIR, help="Bundle dir")
    _add_db_args(poff)
    poff.set_defaults(func=_cmd_off)

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

    ph = sub.add_parser("hold", help="Shelve a session on hold (or --release)")
    ph.add_argument("sid")
    ph.add_argument("--note", default=None, help="Why it's on hold")
    ph.add_argument("--until", default=None, help="Resume-by date, YYYY-MM-DD")
    ph.add_argument("--release", action="store_true", help="Release back to in_progress")
    _add_db_args(ph)
    ph.set_defaults(func=_cmd_hold)

    phs = sub.add_parser("holds", help="List on_hold sessions, due-first")
    phs.add_argument("--due", action="store_true", help="Only sessions due to resume")
    _add_db_args(phs)
    phs.set_defaults(func=_cmd_holds)

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

    _add_tasks_parser(sub)
    return p


def _add_tasks_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tasks-dir", default=tasks.DEFAULT_TASKS_DIR, help="CC tasks dir")
    p.add_argument("--bundle-dir", default=tasks.DEFAULT_BUNDLE_DIR, help="Bundle dir")
    p.add_argument(
        "--list-id",
        default=None,
        help=(
            "Task-list id override (an agent-team session names its dir after "
            "the team, not the session). On import/copy this is the DESTINATION."
        ),
    )


def _add_tasks_import_args(p: argparse.ArgumentParser) -> None:
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--merge", action="store_true", help="Default: keep dest tasks")
    mode.add_argument(
        "--replace",
        action="store_true",
        help="Clear the dest task files first (loses data — opt-in)",
    )
    p.add_argument(
        "--only-open",
        dest="only_open",
        action="store_true",
        help="Skip completed tasks (the default; accepted for explicitness)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help=(
            "Import completed tasks too. CC wipes a list once every task in it "
            "is completed, so an all-completed import can vanish on its own."
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Plan only, write nothing")


def _add_tasks_parser(sub) -> None:
    """`hand tasks …` — carry a task list across /hand:off → /hand:on."""
    pt = sub.add_parser("tasks", help="Export/import a session's task list")
    act = pt.add_subparsers(dest="action", required=True)

    pl = act.add_parser("list", help="Show a session's tasks")
    pl.add_argument("sid")
    _add_tasks_args(pl)
    pl.set_defaults(func=_cmd_tasks_list)

    pe = act.add_parser("export", help="Write a session's tasks to a bundle")
    pe.add_argument("sid")
    pe.add_argument("--out", default=None, help="Bundle path (default: <bundle-dir>/<sid>.json)")
    _add_tasks_args(pe)
    pe.set_defaults(func=_cmd_tasks_export)

    pi = act.add_parser("import", help="Merge a bundle into a session's tasks")
    pi.add_argument("bundle")
    pi.add_argument("--to", required=True, help="Destination session id")
    _add_tasks_args(pi)
    _add_tasks_import_args(pi)
    pi.set_defaults(func=_cmd_tasks_import)

    pc = act.add_parser("copy", help="export | import in one call")
    pc.add_argument("--from", required=True, help="Source session id")
    pc.add_argument("--to", required=True, help="Destination session id")
    pc.add_argument("--from-list-id", default=None, help="Source task-list id override")
    _add_tasks_args(pc)
    _add_tasks_import_args(pc)
    pc.set_defaults(func=_cmd_tasks_copy)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
