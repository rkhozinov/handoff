"""Optional memory recall: store sub-agent reports back into the user's
memory CLI for later retrieval.

Best-effort: if `memory` CLI is missing or returns garbage, we silently
skip the store. The brief is still useful without it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional


def project_tag_from_cwd(cwd: str) -> str:
    """Return a `project:<basename>` tag matching the storage convention used
    elsewhere in the user's memory store. cwd basename is the project folder
    name (e.g. `claude-compaction`)."""
    base = os.path.basename(os.path.normpath(cwd))
    return f"project:{base}" if base else ""


def store_agent_reports(
    reports: list[tuple[str, str, str]],
    *,
    project_tag: str,
    timeout: float = 5.0,
    memory_bin: Optional[str] = None,
    min_chars: int = 200,
) -> int:
    """Auto-store sub-agent reports as `learning` memory entries.

    Tags each one with `project:<basename>`, `source:agent-report`, and
    `agent:<subagent_type>` so the recall pipeline can surface them in
    future sessions' briefs.

    The memory store dedups by content hash, so re-running /handoff on the
    same session is safe — duplicate reports collapse to no-ops.

    Returns the count of `memory store` calls that exited 0. Best-effort:
    missing `memory` binary, timeouts, non-zero exits → counted as failed
    but never raise.
    """
    if not reports:
        return 0
    bin_path = memory_bin or shutil.which("memory")
    if not bin_path:
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
        cmd = [bin_path, "store", body, "--type", "learning"]
        if tags:
            cmd += ["--tags", ",".join(tags)]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if proc.returncode == 0:
            stored += 1
    return stored
