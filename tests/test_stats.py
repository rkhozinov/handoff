"""Stats tests over real fixture transcripts.

These verify hard invariants that must hold across every real-world session:
  1. Every real user message is preserved verbatim in the brief.
  2. The brief is meaningfully smaller than the input.
  3. Tool_result bodies are NOT in the brief (sample-based check).
"""
from __future__ import annotations

import pytest

from compaction.extract import iter_signal_user_msgs, load_jsonl
from compaction.trim import render_brief


def _brief(*args, **kwargs) -> str:
    """Helper: tier1 + tier2 joined for substring asserts."""
    tier1, tier2 = render_brief(*args, **kwargs)
    return tier1 + "\n" + tier2


def test_signal_user_msgs_preserved_across_fixtures(any_fixtures):
    """Hard invariant: every signal-bearing user msg (non-noise, non-duplicate)
    appears verbatim in the brief. Noise (short acks, skill bodies, prior
    compaction continuations) is filtered intentionally — the full text stays
    in the memory doc archive."""
    if not any_fixtures:
        pytest.skip("no fixtures")
    for label, path in any_fixtures.items():
        entries = load_jsonl(str(path))
        msgs = iter_signal_user_msgs(entries)
        brief = _brief(entries, label, "/test", archive_hash=None)
        missing = [m for m in msgs if m not in brief]
        assert not missing, (
            f"[{label}] {len(missing)} signal user msgs lost. First: {missing[0][:200]!r}"
        )


def test_trim_ratio_bounds(any_fixtures):
    if not any_fixtures:
        pytest.skip("no fixtures")
    for label, path in any_fixtures.items():
        entries = load_jsonl(str(path))
        if not entries:
            continue
        raw = path.read_bytes()
        brief = _brief(entries, label, "/test", archive_hash=None).encode()
        ratio = len(brief) / len(raw)
        assert ratio < 0.5, f"[{label}] brief is {ratio:.1%} of input — trimmer broken"


def test_no_tool_result_bulk_in_brief(any_fixtures):
    """Long tool_result bodies must not leak into the brief verbatim."""
    if not any_fixtures:
        pytest.skip("no fixtures")
    for label, path in any_fixtures.items():
        entries = load_jsonl(str(path))
        longest_body: str = ""
        for e in entries:
            if e.get("type") != "user":
                continue
            c = e.get("message", {}).get("content")
            if not isinstance(c, list):
                continue
            for b in c:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tc = b.get("content")
                text = ""
                if isinstance(tc, list):
                    text = " ".join(
                        blk.get("text", "")
                        for blk in tc
                        if isinstance(blk, dict) and blk.get("type") == "text"
                    )
                elif isinstance(tc, str):
                    text = tc
                if len(text) > len(longest_body):
                    longest_body = text
        if len(longest_body) < 500:
            continue
        brief = _brief(entries, label, "/test", archive_hash=None)
        sample = longest_body[300:500]
        if len(sample.strip()) < 100:
            continue
        assert sample not in brief, f"[{label}] long tool_result body leaked into brief"
