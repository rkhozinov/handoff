"""Trim a Claude Code transcript and store it as a memory doc.

The body sent to `memory doc store` is the output of `trim_transcript()`
(tool_result bodies + thinking blocks dropped), NOT the raw JSONL. This
keeps doc storage lean — full raw transcripts averaged ~4 MB each and
ballooned the memory DB before this change.

Writes a marker at ``~/.claude/memory/extracted/<session_id>.marker`` so
the SessionStart auto-archive scanner in memory v1.3.0+ skips the same
session on the next launch.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from handoff.transcript import trim_transcript


def _marker_path(session_id: str) -> Path:
    return Path.home() / ".claude" / "memory" / "extracted" / f"{session_id}.marker"


def archive_full_session(
    transcript_path: str,
    session_id: str,
    cwd: str,
    timeout: int = 30,
    max_chars: int = 200_000,
) -> str | None:
    """Trim transcript → store as memory doc → write scanner marker.

    Returns the stored content_hash (or None on failure; errors logged to
    stderr). Body sent to memory is trimmed text, not raw JSONL.
    """
    project = os.path.basename(cwd) or "unknown"
    sid_short = session_id[:8]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        body = trim_transcript(transcript_path, max_chars=max_chars)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"[archive] trim failed: {e}\n")
        return None

    if not body.strip():
        sys.stderr.write("[archive] trimmed transcript empty; skipping\n")
        return None

    title = f"Session {sid_short} {project} {date}"
    summary = body[:500] + ("…" if len(body) > 500 else "")
    tags = f"source:auto,session-archive,project:{project}"
    metadata = json.dumps({"session_id": sid_short, "source_jsonl": transcript_path})

    try:
        proc = subprocess.run(
            [
                "memory", "doc", "store",
                "--title", title,
                "--summary", summary,
                "--body-file", "-",
                "--type", "session-archive",
                "--tags", tags,
                "--metadata", metadata,
            ],
            input=body,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        sys.stderr.write("[archive] `memory` CLI not on PATH; skipping archive\n")
        return None
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"[archive] memory doc store timed out after {timeout}s\n")
        return None
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"[archive] memory doc store failed: {e.stderr or e}\n")
        return None

    try:
        result = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        sys.stderr.write(f"[archive] unparseable memory doc output: {proc.stdout!r}\n")
        return None

    content_hash = result.get("content_hash")
    if content_hash:
        try:
            marker = _marker_path(session_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"archived\n{content_hash}\n", encoding="utf-8")
        except OSError as e:
            sys.stderr.write(f"[archive] marker write failed: {e}\n")
    return content_hash


def compute_body_hash(body: str) -> str:
    """sha256 of the trimmed body — useful for tests and dedup checks."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
