"""Shared trim-stats computation for `bench.py` and `render_html.py`
(spec-findings.md H, #10) — was duplicated as `bench.stats_for` /
`render_html.stat_row`; both are now thin wrappers over this."""
from __future__ import annotations

from pathlib import Path

from handoff.extract import (
    extract_code_anchors,
    extract_decisions,
    extract_errors,
    extract_files_touched,
    iter_real_user_msgs,
    iter_signal_user_msgs,
)
from handoff.tokenizer import count_tokens
from handoff.trim import render_brief


def fixture_stats(path: Path, entries: list[dict], token_mode: str) -> dict:
    """`entries` is passed in (already `load_jsonl`'d) so callers that need
    the parsed transcript for other purposes don't parse it twice."""
    raw = path.read_bytes()
    raw_text = raw.decode("utf-8", errors="replace")
    all_user = iter_real_user_msgs(entries)
    signal_user = iter_signal_user_msgs(entries)

    brief = render_brief(
        entries,
        session_id=path.stem,
        cwd="/bench",
        archive_hash=None,
    )
    brief_bytes = len(brief.encode("utf-8"))

    signal_kept = sum(1 for m in signal_user if m in brief)

    return {
        "fixture": path.name,
        "bytes_in": len(raw),
        "tok_in": count_tokens(raw_text, mode=token_mode),
        "brief_b": brief_bytes,
        "brief_tok": count_tokens(brief, mode=token_mode),
        "ratio_pct": round(100 * brief_bytes / max(1, len(raw)), 2),
        "user_total": len(all_user),
        "user_signal": len(signal_user),
        "signal_kept": signal_kept,
        "decisions": len(extract_decisions(signal_user)),
        "files": len(extract_files_touched(entries)),
        "code_anchors": len(extract_code_anchors(entries)),
        "errors": len(extract_errors(entries)),
    }
