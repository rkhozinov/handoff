"""Sweep ~/.claude/compaction/ and flip stale briefs to `done`.

A brief is stale when status is `pending` or `in_progress` AND its most-recent
activity (`last_resumed` if set, else `created`) is older than `--days`
(default 14). The flip preserves all other frontmatter; only `status` and
`completion_signal` change.

Reversible: `/hand:done <sid> --reopen` flips back to `in_progress` with a
manual signal (which IS sticky — auto-stale will not re-close it).

Usage:
    PYTHONPATH=. python3 scripts/sweep_stale.py              # dry-run, 14d
    PYTHONPATH=. python3 scripts/sweep_stale.py --apply
    PYTHONPATH=. python3 scripts/sweep_stale.py --days 30 --apply

Wire to cron for hands-off lifecycle hygiene:
    0 6 * * *  cd ~/repos/handoff && PYTHONPATH=. python3 scripts/sweep_stale.py --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handoff.lifecycle import (
    STALE_DAYS_DEFAULT,
    is_stale,
    mark_stale,
    parse_frontmatter,
    render_frontmatter,
    strip_frontmatter,
)

COMPACTION_DIR = Path.home() / ".claude" / "compaction"


def _is_brief_file(p: Path) -> bool:
    name = p.name
    if not name.endswith(".md"):
        return False
    if name.endswith("-full.md"):
        return False
    if name.startswith("consumed-"):
        return False
    return True


def sweep(directory: Path, *, days: int, apply: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    counts = {"scanned": 0, "skipped_no_fm": 0, "skipped_not_stale": 0, "flipped": 0}
    rows: list[dict] = []

    for p in sorted(directory.iterdir()):
        if not _is_brief_file(p):
            continue
        counts["scanned"] += 1
        text = p.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        if not fm:
            counts["skipped_no_fm"] += 1
            continue
        if not is_stale(fm, now=now, days=days):
            counts["skipped_not_stale"] += 1
            continue

        new_fm = mark_stale(fm)
        rows.append({
            "sid": p.stem[:8],
            "was": fm.get("status"),
            "created": fm.get("created"),
            "last_resumed": fm.get("last_resumed"),
        })
        if apply:
            body = strip_frontmatter(text)
            p.write_text(render_frontmatter(new_fm) + body, encoding="utf-8")
        counts["flipped"] += 1

    return {"counts": counts, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    ap.add_argument("--days", type=int, default=STALE_DAYS_DEFAULT,
                    help=f"Idle threshold in days (default: {STALE_DAYS_DEFAULT})")
    ap.add_argument("--dir", default=str(COMPACTION_DIR),
                    help=f"Brief dir (default: {COMPACTION_DIR})")
    args = ap.parse_args(argv)

    d = Path(args.dir).expanduser()
    if not d.is_dir():
        sys.stderr.write(f"No such dir: {d}\n")
        return 1

    result = sweep(d, days=args.days, apply=args.apply)
    c = result["counts"]

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] {d}  (threshold: {args.days}d)")
    print(f"  scanned:         {c['scanned']}")
    print(f"  no frontmatter:  {c['skipped_no_fm']}")
    print(f"  not stale:       {c['skipped_not_stale']}")
    print(f"  flipped → done:  {c['flipped']}")
    if result["rows"]:
        print(f"\n  Flipping these (signal: auto-stale):")
        for r in result["rows"]:
            ref = r["last_resumed"] or r["created"]
            print(f"    {r['sid']}  [{r['was']:11s}]  ref={ref}")
    if not args.apply and c["flipped"]:
        print("\n(use --apply to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
