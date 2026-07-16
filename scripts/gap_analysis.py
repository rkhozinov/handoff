#!/usr/bin/env python3
"""Gap analysis: what does the trim drop that the raw transcript had?

Answers "is the deterministic trim still good, or is it losing signal?" by
diffing each rendered brief against its raw JSONL. This is the bench that
gates whether an LLM compression/summary layer is worth building — run in
2026-07 across 3 real sessions (small/huge/medium) it showed the trim drops
only narration + re-readable file bodies, so no such layer was built.

Per session it reports:
  - size reduction (raw bytes -> brief bytes)
  - assistant text turns dropped, and a sample (should be narration only)
  - tool_result bytes dropped (expected: large; contents re-readable on disk)
  - identifiers (py file:line, TICKET-123, long hashes) present in dropped
    tool_results but ABSENT from the brief, by kind

The identifier counts are a *smell*, not a pass/fail: most dropped ids are
noise (grep incidentals, unrelated branch SHAs, search-score floats). Read
the dropped-turn sample — if any is a decision rather than narration, the
narration filter has regressed.

Usage:
  # all briefs in ~/.claude/compaction that have a recoverable raw JSONL
  PYTHONPATH=. python3 scripts/gap_analysis.py

  # a specific session
  PYTHONPATH=. python3 scripts/gap_analysis.py --session <sid>

  # explicit raw + brief (e.g. a bench fixture)
  PYTHONPATH=. python3 scripts/gap_analysis.py --raw x.jsonl --brief x.md
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from handoff.extract import assistant_blocks, load_jsonl

COMPACTION_DIR = Path.home() / ".claude" / "compaction"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Identifier kinds we check for silent loss. Report-only; not pass/fail.
ID_KINDS = {
    "py file:line": re.compile(r"[A-Za-z0-9_./-]+\.py:[0-9]+"),
    "tickets": re.compile(r"\b[A-Z]{2,4}-[0-9]+\b"),
    "long hashes": re.compile(r"\b[0-9a-f]{40,64}\b"),
}
# A short-form hash counts as "kept" if its 8-char prefix is in the brief
# (briefs routinely abbreviate SHAs). Applied only to the long-hash kind.
SHORT_PREFIX = 8


def raw_for_session(sid: str) -> Path | None:
    hits = list(PROJECTS_DIR.rglob(f"{sid}.jsonl"))
    return hits[0] if hits else None


def _tool_results(entries: list[dict]) -> list[str]:
    """Every tool_result body text (user-role blocks), which the trim drops."""
    out: list[str] = []
    for e in entries:
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_result":
                continue
            r = b.get("content", "")
            if isinstance(r, list):
                r = " ".join(x.get("text", "") for x in r if isinstance(x, dict))
            out.append(str(r))
    return out


def _assistant_texts(entries: list[dict]) -> list[str]:
    out: list[str] = []
    for e in entries:
        for b in assistant_blocks(e):
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(b["text"].strip())
    return out


def analyze(raw_path: Path, brief_path: Path) -> dict:
    entries = load_jsonl(str(raw_path))
    brief = brief_path.read_text(encoding="utf-8")

    asst = _assistant_texts(entries)
    # A turn is "traceable" if its opening survives in the brief. First 40
    # chars is enough to distinguish a kept substantive turn from a dropped
    # narration one (narration turns are short and don't recur verbatim).
    dropped_asst = [t for t in asst if t[:40] not in brief]

    tool_results = _tool_results(entries)
    tr_text = "\n".join(tool_results)

    id_loss: dict[str, tuple[int, list[str]]] = {}
    for kind, rx in ID_KINDS.items():
        found = set(rx.findall(tr_text))
        missing = [
            f
            for f in found
            if f not in brief
            and not (kind == "long hashes" and f[:SHORT_PREFIX] in brief)
        ]
        id_loss[kind] = (len(found), sorted(missing))

    return {
        "raw_bytes": raw_path.stat().st_size,
        "brief_bytes": brief_path.stat().st_size,
        "asst_turns": len(asst),
        "dropped_asst": dropped_asst,
        "tr_dropped_n": len(tool_results),
        "tr_dropped_bytes": sum(len(x) for x in tool_results),
        "id_loss": id_loss,
    }


def _print_report(label: str, r: dict, sample: int = 25) -> None:
    raw_kb, brief_kb = r["raw_bytes"] // 1024, r["brief_bytes"] // 1024
    cut = 100 - 100 * r["brief_bytes"] // max(r["raw_bytes"], 1)
    print(f"\n{'=' * 70}\n### {label}")
    print(f"size: {raw_kb}KB raw -> {brief_kb}KB brief ({cut}% cut)")
    print(f"assistant turns: {r['asst_turns']}, dropped: {len(r['dropped_asst'])}")
    print(f"tool_results dropped: {r['tr_dropped_n']} ({r['tr_dropped_bytes'] // 1024}KB)")
    for kind, (total, missing) in r["id_loss"].items():
        print(f"  {kind}: {total} in dropped content, {len(missing)} absent from brief")
        for m in missing[:6]:
            print(f"      - {m}")
    print("  dropped assistant turns (should be narration only):")
    for t in r["dropped_asst"][:sample]:
        print("      *", t[:90].replace("\n", " "))


def _iter_session_briefs() -> list[tuple[str, Path, Path]]:
    out = []
    for bf in sorted(COMPACTION_DIR.glob("*.md")):
        sid = bf.stem
        if not SID_RE.match(sid):
            continue
        raw = raw_for_session(sid)
        if raw:
            out.append((sid, raw, bf))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", help="analyze one session id")
    ap.add_argument("--raw", help="explicit raw JSONL path (needs --brief)")
    ap.add_argument("--brief", help="explicit brief .md path (needs --raw)")
    args = ap.parse_args()

    if args.raw or args.brief:
        if not (args.raw and args.brief):
            ap.error("--raw and --brief must be given together")
        _print_report(Path(args.raw).stem, analyze(Path(args.raw), Path(args.brief)))
        return

    if args.session:
        raw = raw_for_session(args.session)
        brief = COMPACTION_DIR / f"{args.session}.md"
        if not raw:
            ap.error(f"no raw JSONL found for session {args.session}")
        if not brief.exists():
            ap.error(f"no brief at {brief}")
        _print_report(args.session, analyze(raw, brief))
        return

    sessions = _iter_session_briefs()
    if not sessions:
        print("No briefs with a recoverable raw JSONL found.")
        return
    for sid, raw, brief in sessions:
        _print_report(sid, analyze(raw, brief))


if __name__ == "__main__":
    main()
