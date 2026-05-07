"""CLI entrypoint: `cc-handoff --transcript ... --session-id ... --cwd ...`.

Writes:
  ~/.claude/compaction/<session_id>.md       — tier1 (/handon Read target)
  ~/.claude/compaction/<session_id>-full.md  — tier2 (full trimmed conversation)

/handon resolves the brief by current session_id (read from the newest
JSONL in ~/.claude/projects/<encoded-cwd>/) — no symlinks, no slug.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from compaction.archive import archive_full_session
from compaction.extract import (
    extract_agent_reports,
    extract_decisions,
    iter_signal_user_msgs,
    load_jsonl,
)
from compaction.recall import (
    build_query,
    format_memory_line,
    project_tag_from_cwd,
    search_memories,
    store_agent_reports,
)
from compaction.trim import render_brief


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cc-handoff",
        description="Trim a Claude Code transcript into a Session Brief and archive the full transcript.",
    )
    p.add_argument("--transcript", required=True, help="Path to JSONL transcript")
    p.add_argument("--session-id", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument(
        "--out-dir",
        default=os.path.expanduser("~/.claude/compaction"),
    )
    p.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip memory doc archive (testing only)",
    )
    p.add_argument(
        "--no-recall",
        action="store_true",
        help="Skip memory search recall (default: include top hits in brief)",
    )
    p.add_argument(
        "--recall-limit",
        type=int,
        default=5,
        help="Max number of recalled memories to embed in the brief (default: 5)",
    )
    p.add_argument(
        "--no-agent-store",
        action="store_true",
        help="Skip auto-storing sub-agent reports to memory (default: store)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    transcript = os.path.expanduser(args.transcript)
    if not os.path.isfile(transcript):
        sys.stderr.write(f"Transcript not found: {transcript}\n")
        return 1

    entries = load_jsonl(transcript)
    if not entries:
        sys.stderr.write("Transcript is empty or unreadable\n")
        return 1

    archive_hash = None
    if not args.no_archive:
        archive_hash = archive_full_session(transcript, args.session_id, args.cwd)

    out_dir = Path(os.path.expanduser(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    tier1_path = out_dir / f"{args.session_id}.md"
    tier2_path = out_dir / f"{args.session_id}-full.md"

    recalled_lines: list[str] = []
    if not args.no_recall:
        signal_msgs = iter_signal_user_msgs(entries)
        decisions = extract_decisions(signal_msgs)
        query = build_query(signal_msgs, decisions)
        # Tag-scoped first; if empty, fall back to project-agnostic search so
        # cross-project memories (tools, conventions) still surface.
        tag = project_tag_from_cwd(args.cwd)
        hits = search_memories(query, project_tag=tag, limit=args.recall_limit)
        if not hits and tag:
            hits = search_memories(query, project_tag="", limit=args.recall_limit)
        recalled_lines = [format_memory_line(h) for h in hits]

    tier1, tier2 = render_brief(
        entries,
        session_id=args.session_id,
        cwd=args.cwd,
        archive_hash=archive_hash,
        tier2_path=str(tier2_path),
        recalled_memories=recalled_lines or None,
    )

    # Auto-store sub-agent reports to memory so they survive /clear and become
    # recall-able in future sessions. Use the full (untruncated) bodies.
    agent_stored = 0
    agent_count = 0
    if not args.no_agent_store:
        full_reports = extract_agent_reports(entries, max_chars=0)
        agent_count = len(full_reports)
        agent_stored = store_agent_reports(
            full_reports, project_tag=project_tag_from_cwd(args.cwd)
        )

    tier1_path.write_text(tier1, encoding="utf-8")
    tier2_path.write_text(tier2, encoding="utf-8")

    print(str(tier1_path))
    tier1_bytes = len(tier1.encode("utf-8"))
    tier2_bytes = len(tier2.encode("utf-8"))
    sys.stderr.write(
        f"tier1={tier1_bytes}B (~{tier1_bytes // 4} tok)  "
        f"tier2={tier2_bytes}B (~{tier2_bytes // 4} tok)  "
        f"archive={archive_hash[:12] if archive_hash else 'none'}  "
        f"agent_reports={agent_stored}/{agent_count}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
