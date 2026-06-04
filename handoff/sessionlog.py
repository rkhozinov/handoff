"""Global session log: one chronological markdown file across all projects.

`~/.claude/compaction/sessions.log.md` gets one entry per session:

    ## 2026-06-04 session-recap-automation [pending]
    Goal: auto-store recap + global log. Shipped X. Next: commit.
    restore: /hand:on 952e11c2-7476-4cb5-9d5c-8a59d4d834bb (~/repos/handoff)

Heading title = CC's own ai-title from the transcript — a cwd path is
useless when most sessions share one repo dir. Falls back to the
home-collapsed cwd when the transcript carries no title. The restore line
carries the full session id (copy-paste resumable) plus the cwd.

`/hand:off` re-runs are idempotent: an existing entry for the same session
id (matched on the `restore:` line) is replaced in place, never duplicated.
Entries for other sessions are left byte-identical.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_LOG_PATH = "~/.claude/compaction/sessions.log.md"

_RESTORE_RE = re.compile(r"^restore: /hand:on (?P<sid>\S+)", re.MULTILINE)


def _collapse_home(path: str) -> str:
    home = os.path.expanduser("~")
    if home and path.startswith(home):
        return "~" + path[len(home):]
    return path


def _render_entry(
    *,
    session_id: str,
    cwd: str,
    status: str | None,
    recap: str | None,
    created: str | None,
    title: str | None,
) -> str:
    date = (created or "").split("T")[0] or "?"
    head = title or _collapse_home(cwd)
    lines = [f"## {date} {head} [{status or '?'}]"]
    if recap:
        lines.append(recap)
    lines.append(f"restore: /hand:on {session_id} ({_collapse_home(cwd)})")
    return "\n".join(lines) + "\n"


def update_session_log(
    log_path: Path | str | None = None,
    *,
    session_id: str,
    cwd: str,
    status: str | None,
    recap: str | None,
    created: str | None,
    title: str | None = None,
) -> Path:
    """Append (or replace, keyed on the session id in the `restore:` line)
    one entry in the global log.

    Creates the file + parent dir on first use. Returns the log path.
    """
    p = Path(os.path.expanduser(str(log_path or DEFAULT_LOG_PATH)))
    p.parent.mkdir(parents=True, exist_ok=True)

    entry = _render_entry(
        session_id=session_id,
        cwd=cwd,
        status=status,
        recap=recap,
        created=created,
        title=title,
    )

    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    # Split into a preamble + ## blocks; replace the matching sid's block.
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            if current:
                blocks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("".join(current))

    replaced = False
    for i, block in enumerate(blocks):
        m = _RESTORE_RE.search(block)
        if m and m.group("sid") == session_id:
            blocks[i] = entry + "\n"
            replaced = True
            break

    if not replaced:
        if blocks and not blocks[-1].endswith("\n\n"):
            blocks[-1] = blocks[-1].rstrip("\n") + "\n\n"
        blocks.append(entry)

    p.write_text("".join(blocks), encoding="utf-8")
    return p
