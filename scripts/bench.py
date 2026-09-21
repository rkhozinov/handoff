#!/usr/bin/env python3
"""Benchmark trimmer on fixtures: trim ratio + signal preservation stats.

For each fixture, reports:
  bytes_in, bytes_out, ratio
  real_user_msgs_in, real_user_msgs_kept (must be 100%)
  decisions, files_touched, code_anchors, errors
"""
from __future__ import annotations

import argparse
from pathlib import Path

from handoff.extract import load_jsonl
from handoff.tokenizer import VALID_MODES

from scripts.fixture_stats import fixture_stats

ROOT = Path(__file__).resolve().parent.parent


def stats_for(fixture: Path, token_mode: str = "auto") -> dict:
    entries = load_jsonl(str(fixture))
    return fixture_stats(fixture, entries, token_mode)


def fmt_table(rows: list[dict]) -> str:
    headers = list(rows[0].keys())
    widths = {h: max(len(str(h)), max(len(str(r[h])) for r in rows)) for h in headers}
    lines = []
    lines.append("  ".join(f"{h:>{widths[h]}}" for h in headers))
    lines.append("  ".join("-" * widths[h] for h in headers))
    for r in rows:
        lines.append("  ".join(f"{str(r[h]):>{widths[h]}}" for h in headers))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--fixtures",
        default=str(ROOT / "tests" / "fixtures" / "raw"),
        help="Fixture dir (defaults to raw; falls back to scrubbed)",
    )
    ap.add_argument(
        "--token-mode",
        choices=VALID_MODES,
        default="auto",
        help=(
            "Tokenizer used for tok_in / brief_tok columns. "
            "'auto' (default) uses the HF tokenizer when available and "
            "falls back to chars/4. See handoff.tokenizer for details."
        ),
    )
    args = ap.parse_args(argv)

    fdir = Path(args.fixtures)
    if not (fdir.is_dir() and any(fdir.glob("*.jsonl"))):
        fdir = ROOT / "tests" / "fixtures" / "scrubbed"

    fixtures = sorted(fdir.glob("*.jsonl"))
    if not fixtures:
        # CI runs against a clean checkout where neither raw/ nor scrubbed/
        # fixtures exist (both gitignored). Treat as skipped, not failed —
        # pytest still validates trimmer correctness on synthetic dicts.
        print(f"No fixtures in {fdir} — skipping bench (run scripts/collect_fixtures.py locally).")
        return 0

    rows = [stats_for(f, token_mode=args.token_mode) for f in fixtures]

    print(f"Fixtures from: {fdir}\n")
    print(fmt_table(rows))

    print("\nINVARIANT CHECK: signal_kept == user_signal (must be 100%)")
    bad = [r for r in rows if r["user_signal"] != r["signal_kept"]]
    if bad:
        print(f"  ❌ {len(bad)} rows lost signal user messages:")
        for r in bad:
            print(f"    {r['fixture']:20s} {r['signal_kept']}/{r['user_signal']}")
        return 2
    print("  ✓ all signal user messages preserved across every fixture")
    print("    (noise — short acks, skill bodies, prior compaction continuations,")
    print("     duplicate msgs — is filtered. Full text remains in memory doc archive.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
