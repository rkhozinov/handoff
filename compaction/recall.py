"""Optional memory recall: surface prior memories relevant to the current
session's active goal + recent decisions, so a /clear-and-resume gets the
context that lives outside the transcript.

Best-effort: if `memory` CLI is missing or returns garbage, we silently
emit no memories. The brief is still useful without recall.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Optional


def build_query(signal_msgs: list[str], decisions: list[str], max_chars: int = 240) -> str:
    """Cheap relevance signal: join the last few signal user msgs +
    decisions into one query string. Memory search does its own embedding,
    so we don't need to be precise — we just need on-topic words."""
    parts: list[str] = []
    for src in (decisions, signal_msgs):
        for m in reversed(src):
            t = m.strip().splitlines()[0]
            if t and t not in parts:
                parts.append(t)
            if sum(len(p) for p in parts) > max_chars:
                break
        if sum(len(p) for p in parts) > max_chars:
            break
    q = " ".join(parts)
    return q[:max_chars].strip()


def project_tag_from_cwd(cwd: str) -> str:
    """Return a `project:<basename>` tag matching the storage convention used
    elsewhere in the user's memory store. cwd basename is the project folder
    name (e.g. `claude-compaction`)."""
    base = os.path.basename(os.path.normpath(cwd))
    return f"project:{base}" if base else ""


def search_memories(
    query: str,
    *,
    project_tag: str = "",
    limit: int = 5,
    timeout: float = 3.0,
    memory_bin: Optional[str] = None,
) -> list[dict]:
    """Run `memory search` and return the parsed list. Empty list on any
    failure (binary missing, timeout, JSON garbage, non-zero exit)."""
    if not query:
        return []
    bin_path = memory_bin or shutil.which("memory")
    if not bin_path:
        return []
    cmd = [bin_path, "search", query, "--limit", str(limit)]
    if project_tag:
        cmd += ["--tags", project_tag]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data


def format_memory_line(mem: dict, max_chars: int = 200) -> str:
    """One-line markdown bullet for a memory hit. Keep the hash so the user
    can `memory get <hash>` for full content if needed."""
    h = (mem.get("content_hash") or "")[:12]
    t = mem.get("memory_type", "?")
    score = mem.get("score") or mem.get("similarity") or 0
    body = (mem.get("content") or "").replace("\n", " ").strip()
    if len(body) > max_chars:
        body = body[: max_chars - 3] + "..."
    return f"- `{h}` [{t}] score={score:.2f} — {body}"
