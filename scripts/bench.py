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

from compaction.extract import (
    extract_code_anchors,
    extract_decisions,
    extract_errors,
    extract_files_touched,
    iter_real_user_msgs,
    iter_signal_user_msgs,
    load_jsonl,
)
from compaction.tokenizer import VALID_MODES, count_tokens
from compaction.trim import render_brief

ROOT = Path(__file__).resolve().parent.parent


def stats_for(fixture: Path, token_mode: str = "auto", semantic_dedup: bool = False) -> dict:
    raw = fixture.read_bytes()
    raw_text = raw.decode("utf-8", errors="replace")
    entries = load_jsonl(str(fixture))
    all_user = iter_real_user_msgs(entries)
    signal_user = iter_signal_user_msgs(entries)

    tier1, tier2 = render_brief(
        entries,
        session_id=fixture.stem,
        cwd="/bench",
        archive_hash=None,
        semantic_dedup=semantic_dedup,
    )
    tier1_bytes = len(tier1.encode("utf-8"))
    tier2_bytes = len(tier2.encode("utf-8"))
    combined = tier1 + tier2

    signal_kept = sum(1 for m in signal_user if m in combined)

    # Token counts now go through the pluggable tokenizer in
    # compaction.tokenizer. Default mode "auto" prefers the offline HF
    # tokenizer (Xenova/claude-tokenizer) and falls back to the legacy
    # chars/4 heuristic if transformers is not installed.
    return {
        "fixture": fixture.name,
        "bytes_in": len(raw),
        "tok_in": count_tokens(raw_text, mode=token_mode),
        "tier1_b": tier1_bytes,
        "tier1_tok": count_tokens(tier1, mode=token_mode),
        "tier2_b": tier2_bytes,
        "tier2_tok": count_tokens(tier2, mode=token_mode),
        "ratio_pct": round(100 * (tier1_bytes + tier2_bytes) / max(1, len(raw)), 2),
        "tier1_fits_25k": "✓" if tier1_bytes <= 25_000 else "✗",
        "user_total": len(all_user),
        "user_signal": len(signal_user),
        "signal_kept": signal_kept,
        "decisions": len(extract_decisions(signal_user)),
        "files": len(extract_files_touched(entries)),
        "code_anchors": len(extract_code_anchors(entries)),
        "errors": len(extract_errors(entries)),
    }


def fmt_table(rows: list[dict]) -> str:
    headers = list(rows[0].keys())
    widths = {h: max(len(str(h)), max(len(str(r[h])) for r in rows)) for h in headers}
    lines = []
    lines.append("  ".join(f"{h:>{widths[h]}}" for h in headers))
    lines.append("  ".join("-" * widths[h] for h in headers))
    for r in rows:
        lines.append("  ".join(f"{str(r[h]):>{widths[h]}}" for h in headers))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--fixtures",
        default=str(ROOT / "tests" / "fixtures" / "raw"),
        help="Fixture dir (defaults to raw; falls back to scrubbed)",
    )
    ap.add_argument(
        "--semantic-dedup",
        action="store_true",
        help="Enable Model2Vec adjacent-paraphrase dedup before rendering.",
    )
    ap.add_argument(
        "--token-mode",
        choices=VALID_MODES,
        default="auto",
        help=(
            "Tokenizer used for tok_in / tier{1,2}_tok columns. "
            "'auto' (default) uses the HF tokenizer when available and "
            "falls back to chars/4. See compaction.tokenizer for details."
        ),
    )
    args = ap.parse_args()

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

    rows = [
        stats_for(f, token_mode=args.token_mode, semantic_dedup=args.semantic_dedup)
        for f in fixtures
    ]

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
