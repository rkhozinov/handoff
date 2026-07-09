"""CLI entrypoint: `handoff --transcript ... --session-id ... --cwd ...`.

Writes one file: `~/.claude/compaction/<session_id>.md` — the trimmed
session brief that `/handon` Reads back.

/handoff supplies session id from `${CLAUDE_SESSION_ID}` and derives
the transcript path from the same id + cwd encoding, so there's no
chance of a session-id ↔ transcript mismatch.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from handoff.archive import archive_full_session
from handoff.extract import extract_agent_reports, extract_title, load_jsonl
from handoff.lifecycle import (
    detect_status,
    extract_recap,
    read_existing_brief,
    render_frontmatter,
    resolve_frontmatter,
)
from handoff.recall import project_tag_from_cwd, store_agent_reports
from handoff.tokenizer import VALID_MODES, count_tokens
from handoff.trim import render_brief


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="handoff",
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
        "--no-agent-store",
        action="store_true",
        help="Skip auto-storing sub-agent reports to memory (default: store)",
    )
    p.add_argument(
        "--recap",
        default=None,
        help=(
            "One-line session recap (LLM-composed by /hand:off). When absent, "
            "a deterministic extraction fallback is used."
        ),
    )
    p.add_argument(
        "--no-db",
        action="store_true",
        help="Skip the sessions.db index upsert (testing only)",
    )
    p.add_argument(
        "--token-mode",
        choices=VALID_MODES,
        default="chars4",
        help=(
            "Tokenizer for the (cosmetic) stderr summary line. "
            "'chars4' (default) is the dependency-free chars/4 heuristic — it "
            "keeps /hand:off cold-start instant and quiet. 'auto'/'hf' load the "
            "offline HF tokenizer (imports `transformers`: ~1.5s + a PyTorch "
            "warning) for an exact count; 'api' uses the anthropic SDK + "
            "ANTHROPIC_API_KEY (network call). The count never affects the brief."
        ),
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
    brief_path = out_dir / f"{args.session_id}.md"

    detected_status, detected_signal = detect_status(entries)
    fm = resolve_frontmatter(
        session_id=args.session_id,
        cwd=args.cwd,
        detected_status=detected_status,
        detected_signal=detected_signal,
        archive_hash=archive_hash,
        existing=read_existing_brief(brief_path),
        recap=args.recap,
        extracted_recap=extract_recap(entries),
        title=extract_title(entries),
    )

    brief = render_brief(
        entries,
        session_id=args.session_id,
        cwd=args.cwd,
        archive_hash=archive_hash,
        frontmatter=render_frontmatter(fm),
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

    brief_path.write_text(brief, encoding="utf-8")

    if not args.no_db:
        from handoff import db
        from handoff.lifecycle import strip_frontmatter

        # brief = frontmatter + body; the DB stores the body only (every
        # frontmatter field is already a typed column).
        with db.connect() as conn:
            db.upsert_session(
                conn,
                fm=fm,
                body=strip_frontmatter(brief),
                brief_path=str(brief_path),
            )

    print(str(brief_path))
    brief_bytes = len(brief.encode("utf-8"))
    brief_tok = count_tokens(brief, mode=args.token_mode)
    sys.stderr.write(
        f"brief={brief_bytes}B (~{brief_tok} tok)  "
        f"archive={archive_hash[:12] if archive_hash else 'none'}  "
        f"agent_reports={agent_stored}/{agent_count}  "
        f"status={fm['status']}({fm['completion_signal']})  "
        f"recap={fm['recap_source'] or 'none'}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
