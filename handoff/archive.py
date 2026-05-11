"""Wrapper around `memory doc store` for full-session archive."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def archive_full_session(
    transcript_path: str,
    session_id: str,
    cwd: str,
    timeout: int = 30,
) -> str | None:
    """Store the raw transcript as a memory doc. Returns content_hash or
    None on failure (logs to stderr)."""
    project = os.path.basename(cwd) or "unknown"
    title = (
        f"Session {session_id[:8]} {project} "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    )
    summary = (
        f"Full session transcript archive for {project}, session {session_id[:8]}"
    )
    tags = f"session,project:{project},compaction-archive"

    try:
        proc = subprocess.run(
            [
                "memory", "doc", "store",
                "--title", title,
                "--summary", summary,
                "--body-file", transcript_path,
                "--type", "transcript",
                "--tags", tags,
            ],
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
    return result.get("content_hash")
