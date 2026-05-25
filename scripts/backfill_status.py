"""Backfill `status` frontmatter onto existing briefs in ~/.claude/compaction/.

The detector in `handoff.lifecycle` reads JSONL entries, but old briefs may
outlive their source transcripts. This script does a best-effort detection
straight off the rendered brief body — scanning the `U:` lines the trimmer
wrote out — so it can backfill the full corpus without re-parsing transcripts.

Rules:
  * Skip files whose first line is `---` (already has frontmatter).
  * Skip `*-full.md` and `consumed-*.md` — auxiliary, not real briefs.
  * Conservative: never auto-mark `done` based on body inspection alone. Use
    `in_progress` as the fallback; the user can `/hand:done` to clean up.

Usage:
    PYTHONPATH=. python3 scripts/backfill_status.py            # dry-run
    PYTHONPATH=. python3 scripts/backfill_status.py --apply    # write
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python scripts/backfill_status.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handoff.lifecycle import (
    FRONTMATTER_KEYS,
    parse_frontmatter,
    render_frontmatter,
)

COMPACTION_DIR = Path.home() / ".claude" / "compaction"

_DONE_RE = re.compile(
    r"\b("
    r"ship(?:ped|d)?|merged|lgtm|thanks?|thx|done|that\s+worked|all\s+(?:good|set)|"
    r"perfect|finished|wrap(?:ped)?\s*up|complete[d]?|closed?|resolved|fixed"
    r")\b",
    re.IGNORECASE,
)
_QUESTION_PREFIX_RE = re.compile(
    r"^\s*(what|why|how|when|where|which|who|can|could|should|would|do|does|did|is|are|will)\b",
    re.IGNORECASE,
)

USER_LINE_RE = re.compile(r"^U:\s+(.*)$", re.MULTILINE)


def _extract_user_msgs(body: str) -> list[str]:
    """Pull `U: …` lines out of the trimmed convo. Multi-line user msgs are
    merged into the leading `U:` capture — good enough for tail-window
    inspection (we only care about completion keywords / question shape)."""
    return USER_LINE_RE.findall(body)


def detect_status_from_body(body: str) -> tuple[str, str]:
    """Same precedence as `handoff.lifecycle.detect_status`, minus the
    TodoWrite signal (not visible in rendered brief)."""
    msgs = _extract_user_msgs(body)
    tail = msgs[-3:]
    if any(_DONE_RE.search(m) for m in tail):
        return ("done", "backfill-user-msg")
    if tail:
        last = tail[-1].strip()
        if last.endswith("?") or _QUESTION_PREFIX_RE.match(last):
            return ("pending", "backfill-open-q")
    return ("in_progress", "backfill-default")


def _is_brief_file(p: Path) -> bool:
    name = p.name
    if not name.endswith(".md"):
        return False
    if name.endswith("-full.md"):
        return False
    if name.startswith("consumed-"):
        return False
    return True


def _file_mtime_iso(p: Path) -> str:
    ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_id_from_path(p: Path) -> str:
    return p.stem


def _cwd_from_body(body: str) -> str | None:
    r"""Recover `cwd` from the legacy `**Cwd:** \`...\`` header line so the
    backfilled frontmatter matches the original brief."""
    m = re.search(r"^\*\*Cwd:\*\*\s+`([^`]+)`", body, re.MULTILINE)
    return m.group(1) if m else None


def _archive_from_body(body: str) -> str | None:
    m = re.search(r"^\*\*Archive:\*\*\s+memory doc `([0-9a-f]+)`", body, re.MULTILINE)
    return m.group(1) if m else None


def backfill_file(p: Path, apply: bool) -> dict:
    text = p.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        # Already has frontmatter — leave it alone.
        return {"path": str(p), "action": "skip-existing"}
    body = text
    status, signal = detect_status_from_body(body)
    fm: dict[str, str | None] = {
        "status": status,
        "session_id": _session_id_from_path(p),
        "cwd": _cwd_from_body(body),
        "created": _file_mtime_iso(p),
        "last_resumed": None,
        "completion_signal": signal,
        "archive_hash": _archive_from_body(body),
    }
    if apply:
        p.write_text(render_frontmatter(fm) + body, encoding="utf-8")
    return {"path": str(p), "action": "backfill", "status": status, "signal": signal}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="Write frontmatter (default: dry-run)")
    ap.add_argument("--dir", default=str(COMPACTION_DIR), help=f"Brief dir (default: {COMPACTION_DIR})")
    args = ap.parse_args(argv)

    d = Path(args.dir).expanduser()
    if not d.is_dir():
        sys.stderr.write(f"No such dir: {d}\n")
        return 1

    counts: dict[str, int] = {"skip-existing": 0, "backfill": 0}
    by_status: dict[str, int] = {"pending": 0, "in_progress": 0, "done": 0}
    rows: list[dict] = []

    for p in sorted(d.iterdir()):
        if not _is_brief_file(p):
            continue
        result = backfill_file(p, apply=args.apply)
        counts[result["action"]] = counts.get(result["action"], 0) + 1
        if result["action"] == "backfill":
            by_status[result["status"]] = by_status.get(result["status"], 0) + 1
            rows.append(result)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] {d}")
    print(f"  skipped (already has frontmatter): {counts['skip-existing']}")
    print(f"  backfilled:                        {counts['backfill']}")
    print(f"  └─ by status:")
    for k in ("done", "pending", "in_progress"):
        print(f"      {k:13s} {by_status.get(k, 0)}")
    if not args.apply:
        print("\n(use --apply to write frontmatter)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
