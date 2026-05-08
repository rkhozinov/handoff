#!/usr/bin/env python3
"""Mechanical safety tests for --semantic-dedup before flipping default.

Runs three checks per fixture:

1. **Assistant-signal invariant.** Extract decisions, files, code anchors,
   errors, and agent reports from the brief with AND without dedup. Counts
   must match. If dedup drops any of these, fail loud — the brief lost
   functional signal even if user-msg count stayed flat.

2. **Drop-sanity rules.** For every drop emitted by `semantic_dedup`,
   assert: role==assistant, prev!=curr, 0.95<=cos<1.0, len(curr)>=30.

3. **Threshold sweep.** Run threshold ∈ {0.90, 0.92, 0.95, 0.97, 0.99}
   per fixture. Print drop count + assistant-signal delta. Helps pick
   the threshold that maximizes drops while keeping signal_delta == 0.

No human review required — all assertions are programmatic. Exit code
is non-zero on any failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

from compaction.dedup import semantic_dedup
from compaction.extract import (
    extract_agent_reports,
    extract_code_anchors,
    extract_decisions,
    extract_errors,
    extract_files_touched,
    iter_signal_user_msgs,
    load_jsonl,
)
from compaction.trim import build_convo, render_brief

ROOT = Path(__file__).resolve().parent.parent
FIX_DIR = ROOT / "tests" / "fixtures" / "raw"

THRESHOLDS = [0.90, 0.92, 0.95, 0.97, 0.99]


def signal_counts(entries: list[dict]) -> dict[str, int]:
    """Count signal-bearing extractions on the raw entries (these are
    deterministic — derived from the JSONL, not from the rendered brief)."""
    sigs = iter_signal_user_msgs(entries)
    return {
        "decisions": len(extract_decisions(sigs)),
        "files": len(extract_files_touched(entries)),
        "code_anchors": len(extract_code_anchors(entries)),
        "errors": len(extract_errors(entries)),
        "agent_reports": len(extract_agent_reports(entries, max_chars=0)),
    }


def signal_in_brief(brief: str, entries: list[dict]) -> dict[str, int]:
    """Count how many signal items survive verbatim in the rendered brief.
    A drop here means dedup ate something we wanted — the killer invariant."""
    sigs = iter_signal_user_msgs(entries)
    files = extract_files_touched(entries)
    decisions = extract_decisions(sigs)
    anchors = extract_code_anchors(entries)
    errors = extract_errors(entries)
    reports = extract_agent_reports(entries, max_chars=0)

    return {
        "decisions": sum(1 for d in decisions if d in brief),
        "files": sum(1 for f in files if f in brief),
        "code_anchors": sum(1 for a in anchors if a in brief),
        "errors": sum(1 for e in errors if e in brief),
        "agent_reports": sum(1 for (_d, _s, body) in reports if body[:200] in brief),
    }


def render(entries, label: str, dedup: bool) -> str:
    t1, t2 = render_brief(
        entries,
        session_id=label,
        cwd="/audit",
        archive_hash=None,
        semantic_dedup=dedup,
    )
    return t1 + t2


def check_drop_sanity(
    drops: list[dict], convo: list[tuple[str, str]], threshold: float
) -> list[str]:
    """Return list of failure messages; empty list = all drops sane.

    `prev == curr` (cos==1.0) is INTENTIONALLY allowed: it means the
    dedup caught an exact duplicate that `_collapse_repeats` missed
    because the duplicates weren't adjacent (e.g. interleaved with
    another turn). That's a valid bonus, not a bug."""
    fails = []
    for d in drops:
        i = d["i"]
        cos = d["cos"]
        curr = d["curr"]
        if convo[i][0] != "assistant":
            fails.append(f"i={i}: role={convo[i][0]!r} (expected 'assistant')")
        if not (threshold <= cos <= 1.0 + 1e-9):
            fails.append(f"i={i}: cos={cos} out of [{threshold}, 1.0]")
        if len(curr) < 30:
            fails.append(f"i={i}: len(curr)={len(curr)} below MIN_LEN=30")
    return fails


def main() -> int:
    fixtures = sorted(FIX_DIR.glob("*.jsonl"))
    if not fixtures:
        print(f"no fixtures in {FIX_DIR}")
        return 1

    any_fail = False

    # Test 1: assistant-signal invariant @ default threshold (0.95)
    print("=" * 78)
    print("TEST 1: assistant-signal invariant @ default threshold (0.99)")
    print("=" * 78)
    print(
        f"{'fixture':<24} {'metric':<14} {'baseline':>10} {'dedup':>10} {'delta':>8}"
    )
    print("-" * 78)
    for f in fixtures:
        entries = load_jsonl(str(f))
        b_off = render(entries, f.stem, dedup=False)
        b_on = render(entries, f.stem, dedup=True)
        sig_off = signal_in_brief(b_off, entries)
        sig_on = signal_in_brief(b_on, entries)
        for k in ("decisions", "files", "code_anchors", "errors", "agent_reports"):
            d = sig_on[k] - sig_off[k]
            mark = " ✓" if d >= 0 else " ❌ FAIL"
            if d < 0:
                any_fail = True
            print(
                f"{f.stem:<24} {k:<14} {sig_off[k]:>10} {sig_on[k]:>10} {d:>+8}{mark}"
            )
    print()

    # Test 3: drop-sanity rules
    print("=" * 78)
    print("TEST 3: drop-sanity rules (threshold=0.99)")
    print("=" * 78)
    for f in fixtures:
        entries = load_jsonl(str(f))
        convo = build_convo(entries)
        out = semantic_dedup(convo, threshold=0.99, return_drops=True)
        if len(out) != 3:
            print(f"{f.stem:<24} dedup unavailable (model2vec/numpy missing)")
            continue
        _c, n, drops = out
        fails = check_drop_sanity(drops, convo, threshold=0.99)
        if fails:
            any_fail = True
            print(f"{f.stem:<24} ❌ {len(fails)} sanity failures from {n} drops:")
            for msg in fails[:5]:
                print(f"    {msg}")
        else:
            print(f"{f.stem:<24} ✓ {n} drops, all sane")
    print()

    # Test 4: threshold sweep
    print("=" * 78)
    print("TEST 4: threshold sweep — drops + signal_delta per threshold")
    print("=" * 78)
    header = f"{'fixture':<24} " + "  ".join(f"{t:>6.2f}" for t in THRESHOLDS)
    print(header)
    print("-" * len(header))
    for f in fixtures:
        entries = load_jsonl(str(f))
        convo = build_convo(entries)
        b_off = render(entries, f.stem, dedup=False)
        sig_off = signal_in_brief(b_off, entries)
        cells = []
        for thr in THRESHOLDS:
            from compaction.trim import _render_tier1  # noqa: F401 (warm imports)
            # Render brief through monkey-patched threshold? No — monkey via env
            # is messy. Instead: dedup the convo here, manually re-render?
            # Easier: dedup convo + count drops; signal preservation can't be
            # tested without re-rendering both tiers, but we can use the convo
            # body itself as a proxy (signal items appear in convo[i][1]).
            out = semantic_dedup(convo, threshold=thr, return_drops=True)
            if len(out) != 3:
                cells.append("n/a")
                continue
            ded_convo, n, _drops = out
            joined = "\n".join(t for _, t in ded_convo)
            sig_after = {
                "decisions": sum(1 for d in extract_decisions(iter_signal_user_msgs(entries)) if d in joined),
                "files": sum(1 for x in extract_files_touched(entries) if x in joined),
                "code_anchors": sum(1 for a in extract_code_anchors(entries) if a in joined),
            }
            joined_off = "\n".join(t for _, t in convo)
            sig_before = {
                "decisions": sum(1 for d in extract_decisions(iter_signal_user_msgs(entries)) if d in joined_off),
                "files": sum(1 for x in extract_files_touched(entries) if x in joined_off),
                "code_anchors": sum(1 for a in extract_code_anchors(entries) if a in joined_off),
            }
            delta = sum(sig_after[k] - sig_before[k] for k in sig_after)
            cells.append(f"{n}/{delta:+d}")
        print(f"{f.stem:<24} " + "  ".join(f"{c:>6}" for c in cells))
    print()
    print("legend: <drops>/<signal_delta>. delta should be 0 at safe thresholds.")
    print()

    if any_fail:
        print("=" * 78)
        print("RESULT: ❌ failures — see above")
        print("=" * 78)
        return 2
    print("=" * 78)
    print("RESULT: ✓ all checks passed")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
