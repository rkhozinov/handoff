"""Optional memory recall: store sub-agent reports back into the user's
memory store for later retrieval.

Best-effort: if the `memory` CLI is missing or errors, we silently skip the
store. The brief is still useful without it.
"""
from __future__ import annotations

import json
import os
import subprocess

from handoff.archive import _memory_bin


def project_tag_from_cwd(cwd: str) -> str:
    """Return a `project:<basename>` tag matching the storage convention used
    elsewhere in the user's memory store. cwd basename is the project folder
    name (e.g. `handoff`)."""
    base = os.path.basename(os.path.normpath(cwd))
    return f"project:{base}" if base else ""


def store_agent_reports(
    reports: list[tuple[str, str, str]],
    *,
    project_tag: str,
    min_chars: int = 200,
) -> int:
    """Auto-store sub-agent reports as `learning` memory entries.

    Tags each one with `project:<basename>`, `source:agent-report`, and
    `agent:<subagent_type>` so the recall pipeline can surface them in
    future sessions' briefs.

    The memory store dedups by content hash, so re-running /handoff on the
    same session is safe — duplicate reports collapse to no-ops.

    Returns the count of stored entries. Best-effort: missing `memory` CLI,
    errors → counted as failed but never raise.
    """
    if not reports:
        return 0
    mem = _memory_bin()
    if mem is None:
        return 0
    stored = 0
    for desc, sub, text in reports:
        if not text or len(text) < min_chars:
            continue
        body = f"[Agent Report: {desc.strip() or '(no description)'}] {text}"
        tags = []
        if project_tag:
            tags.append(project_tag)
        tags.append("source:agent-report")
        if sub:
            tags.append(f"agent:{sub}")
        # Pass content via stdin (`-`): reports run long, past the argv limit.
        try:
            proc = subprocess.run(
                [mem, "store", "-", "--type", "learning", "--tags", ",".join(tags)],
                input=body, capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        # store dedups by content hash; a skipped duplicate still "stored".
        if result.get("status") not in ("error", "rejected"):
            stored += 1
    return stored
