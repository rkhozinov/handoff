"""SQLite session index — the queryable store of /hand:off briefs.

One row per session at `~/.claude/compaction/sessions.db`. Replaces the old
`sessions.log.md` markdown log. The brief `.md` files remain the authoritative
content (what `/hand:on` restores); this DB mirrors their frontmatter PLUS the
trimmed body so the CLI/TUI can review and manage sessions without opening the
files. The DB is fully rebuildable from the briefs via `rebuild_from_briefs`.

Single-machine store: WAL journal mode (the -wal/-shm sidecars are fine when
the file isn't synced across machines) for crash safety + reader/writer
concurrency.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from handoff.lifecycle import (
    FRONTMATTER_KEYS,
    now_iso,
    parse_frontmatter,
    strip_frontmatter,
)

DEFAULT_DB_PATH = "~/.claude/compaction/sessions.db"
DEFAULT_COMPACTION_DIR = "~/.claude/compaction"

# Row column order. The first block mirrors FRONTMATTER_KEYS (every frontmatter
# field becomes a queryable column); the rest is the body blob + bookkeeping.
_FM_COLUMNS = (
    "session_id",
    "status",
    "title",
    "cwd",
    "created",
    "last_resumed",
    "completion_signal",
    "archive_hash",
    "recap",
    "recap_source",
)
COLUMNS = _FM_COLUMNS + ("body", "tokens", "brief_path", "indexed_at")

# Columns selected by list/search renders — never the (potentially large) body.
_LIST_COLUMNS = _FM_COLUMNS + ("tokens", "brief_path", "indexed_at")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    status            TEXT,
    title             TEXT,
    cwd               TEXT,
    created           TEXT,
    last_resumed      TEXT,
    completion_signal TEXT,
    archive_hash      TEXT,
    recap             TEXT,
    recap_source      TEXT,
    body              TEXT,
    tokens            INTEGER,
    brief_path        TEXT,
    indexed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created);
CREATE INDEX IF NOT EXISTS idx_sessions_status  ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd     ON sessions(cwd);
"""

# Columns added after the initial schema — applied as best-effort ALTERs so
# pre-existing DBs migrate in place (CREATE TABLE IF NOT EXISTS won't add them).
_MIGRATIONS = (("tokens", "INTEGER"),)


def db_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Expanduser the DB path (default `~/.claude/compaction/sessions.db`)."""
    return Path(os.path.expanduser(str(path or DEFAULT_DB_PATH)))


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the table + indexes if absent, then apply column migrations.
    Idempotent."""
    conn.executescript(_SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    for col, decl in _MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {decl}")


def open_connection(path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open the session DB (mkdir parent, WAL pragma, ensure schema, Row
    factory) and return the live connection. Caller owns commit + close. Use
    this for a long-lived connection (e.g. the TUI); prefer `connect()` for
    one-shot work."""
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


@contextmanager
def connect(path: str | os.PathLike[str] | None = None) -> Iterator[sqlite3.Connection]:
    """One-shot DB context: open, yield, commit on clean exit, rollback on
    exception, always close. An absent DB is created empty — callers (CLI list,
    TUI) get an empty table, never a crash."""
    conn = open_connection(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _estimate_tokens(body: str) -> int:
    """Cheap, deterministic token estimate (~chars/4) for the list view. Kept
    inline — no tokenizer import on the hot upsert/rebuild path."""
    return (len(body) + 3) // 4


def upsert_session(
    conn: sqlite3.Connection,
    *,
    fm: dict[str, str | None],
    body: str,
    brief_path: str | None = None,
) -> None:
    """INSERT OR REPLACE one row, keyed on `fm['session_id']`. Pulls each
    frontmatter field from `fm`, stores the (frontmatter-stripped) `body`, and
    stamps `indexed_at`. Idempotent on session_id — re-runs overwrite."""
    sid = fm.get("session_id")
    if not sid:
        raise ValueError("upsert_session requires fm['session_id']")
    row = {k: fm.get(k) for k in _FM_COLUMNS}
    row["body"] = body
    row["tokens"] = _estimate_tokens(body)
    row["brief_path"] = brief_path
    row["indexed_at"] = now_iso()
    cols = ", ".join(COLUMNS)
    placeholders = ", ".join(f":{c}" for c in COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO sessions ({cols}) VALUES ({placeholders})",
        row,
    )


def get_session(conn: sqlite3.Connection, sid: str) -> dict | None:
    """Full row (including `body`) for a session id, or None."""
    cur = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,))
    r = cur.fetchone()
    return dict(r) if r else None


def list_sessions(
    conn: sqlite3.Connection,
    *,
    cwd: str | None = None,
    status: str | None = None,
    include_done: bool = False,
    include_archived: bool = False,
) -> list[dict]:
    """Rows ordered newest-first (`created` DESC, `indexed_at` DESC tiebreak),
    selecting the small columns only — never the body. Optional `cwd`/`status`
    equality filters. When `status` is None, `done` and `archived` rows are
    excluded unless the matching `include_*` flag is set."""
    cols = ", ".join(_LIST_COLUMNS)
    where: list[str] = []
    params: list[str] = []
    if cwd is not None:
        where.append("cwd = ?")
        params.append(cwd)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    else:
        hidden = []
        if not include_done:
            hidden.append("done")
        if not include_archived:
            hidden.append("archived")
        if hidden:
            placeholders = ", ".join("?" for _ in hidden)
            where.append(f"(status IS NULL OR status NOT IN ({placeholders}))")
            params.extend(hidden)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    cur = conn.execute(
        f"SELECT {cols} FROM sessions{clause} "
        "ORDER BY created DESC, indexed_at DESC",
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def search_sessions(conn: sqlite3.Connection, query: str) -> list[dict]:
    """Substring (case-insensitive LIKE) match over title/recap/body. Returns
    the small columns, newest-first."""
    cols = ", ".join(_LIST_COLUMNS)
    like = f"%{query}%"
    cur = conn.execute(
        f"SELECT {cols} FROM sessions "
        "WHERE title LIKE ? OR recap LIKE ? OR body LIKE ? "
        "ORDER BY created DESC, indexed_at DESC",
        (like, like, like),
    )
    return [dict(r) for r in cur.fetchall()]


def set_status(conn: sqlite3.Connection, sid: str, status: str, signal: str) -> bool:
    """UPDATE status + completion_signal + indexed_at. Returns True if a row
    matched. Does not touch the brief file (dbcli edits the file separately)."""
    cur = conn.execute(
        "UPDATE sessions SET status = ?, completion_signal = ?, indexed_at = ? "
        "WHERE session_id = ?",
        (status, signal, now_iso(), sid),
    )
    return cur.rowcount > 0


def set_resumed(
    conn: sqlite3.Connection,
    sid: str,
    *,
    status: str,
    last_resumed: str,
) -> bool:
    """UPDATE status + last_resumed + indexed_at (for /hand:on). Returns True if
    a row matched."""
    cur = conn.execute(
        "UPDATE sessions SET status = ?, last_resumed = ?, indexed_at = ? "
        "WHERE session_id = ?",
        (status, last_resumed, now_iso(), sid),
    )
    return cur.rowcount > 0


def delete_session(conn: sqlite3.Connection, sid: str) -> bool:
    """DELETE a row. Returns True if a row was removed."""
    cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    return cur.rowcount > 0


def _is_brief_file(p: Path) -> bool:
    """Mirror scripts/backfill_status.py: skip *-full.md and consumed-* — those
    are auxiliary, not real briefs."""
    name = p.name
    if not name.endswith(".md"):
        return False
    if name.endswith("-full.md"):
        return False
    if name.startswith("consumed-"):
        return False
    return True


def rebuild_from_briefs(
    conn: sqlite3.Connection,
    compaction_dir: str | os.PathLike[str],
) -> dict[str, int]:
    """Repopulate the DB from the brief `.md` files in `compaction_dir`.

    Scans every brief file, parses its frontmatter + body, upserts a row.
    Briefs without a frontmatter fence are skipped (run
    scripts/backfill_status.py first). Rows whose session_id no longer has a
    backing file are deleted, so the DB stays a faithful mirror. Returns
    `{'scanned', 'upserted', 'skipped', 'deleted'}`.
    """
    d = Path(os.path.expanduser(str(compaction_dir)))
    scanned = upserted = skipped = 0
    seen: set[str] = set()
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            if not _is_brief_file(p):
                continue
            scanned += 1
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                skipped += 1
                continue
            fm = parse_frontmatter(text)
            if not fm or not fm.get("session_id"):
                # No fence (or no sid) → can't index reliably. Fall back to the
                # filename as the session id only when a fence is present but
                # missing the key; otherwise skip outright.
                if not fm:
                    skipped += 1
                    continue
                fm["session_id"] = p.stem
            upsert_session(
                conn,
                fm=fm,
                body=strip_frontmatter(text),
                brief_path=str(p),
            )
            seen.add(fm["session_id"])
            upserted += 1

    # Drop rows with no backing brief file.
    existing = {r["session_id"] for r in conn.execute("SELECT session_id FROM sessions")}
    deleted = 0
    for sid in existing - seen:
        if delete_session(conn, sid):
            deleted += 1

    return {
        "scanned": scanned,
        "upserted": upserted,
        "skipped": skipped,
        "deleted": deleted,
    }


def main(argv: list[str] | None = None) -> int:
    """`python -m handoff.db rebuild [--dir DIR] [--db PATH]` — backfill/migrate
    the DB from existing briefs (also the ongoing self-heal path)."""
    p = argparse.ArgumentParser(prog="handoff.db", description="Session index DB tools.")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("rebuild", help="Repopulate the DB from brief .md files")
    pr.add_argument("--dir", default=DEFAULT_COMPACTION_DIR, help="Compaction dir to scan")
    pr.add_argument("--db", default=None, help="DB path (default ~/.claude/compaction/sessions.db)")
    args = p.parse_args(argv)

    if args.cmd == "rebuild":
        with connect(args.db) as conn:
            stats = rebuild_from_briefs(conn, args.dir)
        sys.stderr.write(
            f"rebuilt: scanned={stats['scanned']} upserted={stats['upserted']} "
            f"skipped={stats['skipped']} deleted={stats['deleted']}\n"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
