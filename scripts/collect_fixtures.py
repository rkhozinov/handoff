#!/usr/bin/env python3
"""Pick representative transcripts from ~/.claude/projects and copy them to
tests/fixtures/raw/ as named fixtures (small / medium / large / huge).

The raw dir is gitignored — these may contain PII / secrets. Run scrub.py
afterwards to produce the committed scrubbed versions in tests/fixtures/scrubbed/.
"""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
TARGET_BUCKETS = [
    ("small", 200, 800),         # short focused session
    ("medium", 800, 2_500),      # typical day
    ("large", 2_500, 6_000),     # heavy session
    ("huge", 6_000, 15_000),     # multi-day median
    ("xhuge", 15_000, 1_000_000),  # stress: massive session, take MAX not median
]


def line_count(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        for _ in f:
            n += 1
    return n


def collect(dst: Path) -> dict[str, Path]:
    """Pick the largest .jsonl in each bucket."""
    candidates: list[tuple[Path, int]] = []
    for p in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            lines = line_count(p)
        except OSError:
            continue
        if lines >= 100:
            candidates.append((p, lines))

    candidates.sort(key=lambda t: t[1])

    picked: dict[str, Path] = {}
    for label, lo, hi in TARGET_BUCKETS:
        in_bucket = [p for p, n in candidates if lo <= n < hi]
        if not in_bucket:
            continue
        # xhuge = take the MAX (stress test). Others = median (typical case).
        chosen = in_bucket[-1] if label == "xhuge" else in_bucket[len(in_bucket) // 2]
        target = dst / f"{label}.jsonl"
        shutil.copy2(chosen, target)
        picked[label] = chosen
    return picked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dst",
        default=str(Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "raw"),
    )
    args = ap.parse_args()

    dst = Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)

    picked = collect(dst)
    if not picked:
        print("No fixtures collected (no .jsonl files matched).")
        return 1
    for label, src in picked.items():
        target = dst / f"{label}.jsonl"
        size_kb = target.stat().st_size // 1024
        lines = line_count(target)
        print(f"  {label:6s}  {lines:>7,d} lines  {size_kb:>6,d} KB  ← {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
