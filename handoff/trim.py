"""Trim a transcript into a structured Session Brief.

Drops:
  * tool_result bodies (the bulk of the noise — recoverable from the
    full-session memory doc archive if needed)
  * thinking blocks
  * procedural narration:
      - short text-only turns (<= 80 chars) right before another assistant
        tool_use turn ("let me check the file")
      - text-only turns matching the narration regex regardless of length
        ("ok", "i need to", "reading", "checking", etc.)
  * noise user messages (short acks, skill body re-pastes, prior compaction
    continuations, exact duplicates)

Preserves verbatim:
  * every signal-bearing user message
  * every substantive assistant text turn
  * every code fence
  * every file path / line number / tool_use marker

Output: a single brief file per session (`<session_id>.md`) — header
with session metadata + anti-re-read notice + the trimmed conversation.
The full session transcript stays in the memory doc archive for
forensic recovery if the brief loses something useful.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from handoff.extract import (
    AGENT_REPORT_MIN_CHARS,
    SUBAGENT_TOOL_NAMES,
    assistant_blocks,
    DROP_TOP_TYPES,
    elide_pasted_output,
    is_noise_user_msg,
    is_real_user,
    short_tool_input,
    user_text,
)

NARRATION_RE = re.compile(
    r"^\s*(let me|let's|i'?ll|i'?m\s+going|going\s+to|"
    r"reading|checking|looking|searching|grepping|listing|fetching|"
    r"now\s+(?:i'?ll|let)|first\s+(?:i'?ll|let)|"
    r"i\s+need\s+to|i\s+see|got\s+it|ok|okay)\b",
    re.IGNORECASE,
)

# Per-turn assistant text cap. Prevents one giant turn (e.g. a long
# `Recommendation` block) from dominating the brief. Full reasoning
# stays in the memory doc archive; the brief keeps head + tail marker.
ASSISTANT_TURN_MAX_CHARS = 4_000


def _classify_assistant(
    entry: dict,
    seen_paths: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Return (joined_text, tool_markers) split out of the entry's blocks.
    Thinking blocks are silently dropped here. Per-turn text exceeding
    ASSISTANT_TURN_MAX_CHARS is truncated with a marker so the full body
    stays in the memory doc archive but the brief doesn't bloat."""
    text_parts: list[str] = []
    tool_markers: list[str] = []
    for b in assistant_blocks(entry):
        bt = b.get("type")
        if bt == "text":
            t = b.get("text", "").strip()
            if t:
                text_parts.append(t)
        elif bt == "tool_use":
            tool_markers.append(
                short_tool_input(
                    b.get("name", "?"), b.get("input", {}) or {}, seen_paths=seen_paths
                )
            )
    joined = "\n".join(text_parts).strip()
    if len(joined) > ASSISTANT_TURN_MAX_CHARS:
        elided = len(joined) - ASSISTANT_TURN_MAX_CHARS
        joined = (
            joined[:ASSISTANT_TURN_MAX_CHARS].rstrip()
            + f"\n…[elided {elided // 1000}k of assistant text — full body in memory doc]"
        )
    return (joined, tool_markers)


SHORT_ACK_REPLY_RE = re.compile(
    r"^\s*(done|fixed|got it|right|good|nice|works|works\.?|looks good|"
    r"all set|ready|sure|correct|exactly|that's it|that worked)\b[\s.!?]*$",
    re.IGNORECASE,
)

# Tool markers worth keeping in the brief. Read tells the reader which
# files the assistant inspected — useful reference. Other markers
# (Bash invocations, Edit/Write, ToolSearch, ExitPlanMode, WebFetch,
# Glob, Grep, AskUserQuestion, Skill, …) are mostly noise once the
# tool_result bodies are gone, so they get dropped from the trimmed
# brief. Full list still lives in the memory doc archive.
KEEP_MARKER_TOOLS = frozenset({"Read"})


def _filter_markers(markers: list[str]) -> list[str]:
    """Keep only markers whose tool name is in KEEP_MARKER_TOOLS.
    Marker shape is `[ToolName ...]` (or `[ToolName]` when arg-less)."""
    out: list[str] = []
    for m in markers:
        # Pull the tool name out of the leading `[Name` segment.
        if not m.startswith("["):
            continue
        name = m[1:].split(" ", 1)[0].rstrip("]")
        if name in KEEP_MARKER_TOOLS:
            out.append(m)
    return out


def render_assistant(
    entry: dict,
    next_entry: dict | None = None,
    seen_paths: set[str] | None = None,
) -> str | None:
    """Render an assistant turn for the trimmed conversation. Returns None
    when the turn produces nothing worth keeping."""
    text_joined, tool_markers = _classify_assistant(entry, seen_paths=seen_paths)
    # Drop noisy markers (everything except KEEP_MARKER_TOOLS).
    tool_markers = _filter_markers(tool_markers)

    if text_joined:
        same_turn_drop = len(text_joined) <= 80 and tool_markers

        next_is_tool = False
        if next_entry is not None:
            if next_entry.get("type") == "assistant":
                next_text, next_markers = _classify_assistant(next_entry)
                next_is_tool = bool(next_markers) and not next_text
            elif next_entry.get("type") == "user":
                msg = next_entry.get("message", {})
                content = msg.get("content")
                if isinstance(content, list):
                    next_is_tool = any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    )

        adjacent_drop = (
            not tool_markers
            and len(text_joined) <= 80
            and next_is_tool
        )

        # Narration prefix ("Got it.", "Let me check.") used to be droppable,
        # but text turns can carry inline code fences. Code fences are signal —
        # never drop a turn whose text contains one even when the prose around
        # it looks like narration.
        has_code_fence = "```" in text_joined
        narration_drop = (
            not tool_markers
            and not has_code_fence
            and bool(NARRATION_RE.match(text_joined))
        )

        # Short assistant ack/reply with no tool_use: "Done." "Fixed." "Looks good."
        short_reply_drop = (
            not tool_markers
            and len(text_joined) <= 40
            and bool(SHORT_ACK_REPLY_RE.match(text_joined))
        )

        if same_turn_drop or adjacent_drop or narration_drop or short_reply_drop:
            text_joined = ""

    pieces: list[str] = []
    if text_joined:
        pieces.append(text_joined)
    if tool_markers:
        pieces.append(" ".join(_dedup_markers(tool_markers)))
    if not pieces:
        return None
    return "\n".join(pieces)


def _dedup_markers(markers: list[str]) -> list[str]:
    """Collapse runs of identical adjacent tool markers within one turn into
    `[marker] ×N`. Only adjacent identical markers — distinct markers are kept
    in order. Same logic as cross-turn `_collapse_repeats`, but for a single
    assistant turn's marker list."""
    if not markers:
        return markers
    out: list[str] = []
    for m in markers:
        if out:
            prev = out[-1]
            base = prev
            n = 1
            if "×" in prev and prev.rsplit("×", 1)[-1].isdigit():
                base, _, ns = prev.rpartition("×")
                base = base.rstrip()
                n = int(ns)
            if base == m:
                out[-1] = f"{base} ×{n + 1}"
                continue
        out.append(m)
    return out


def _collapse_repeats(convo: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Collapse runs of identical adjacent entries into a single entry with
    a `×N` suffix. Common case: assistant rereads the same file 5+ times in
    a row while diagnosing a bug."""
    out: list[tuple[str, str]] = []
    for role, text in convo:
        if out and out[-1][0] == role and out[-1][1] == text:
            # bump count via inline marker
            prev_role, prev_text = out[-1]
            if prev_text.endswith("×2"):
                # already collapsed once; just bump
                base, _, n = prev_text.rpartition("×")
                try:
                    new_n = int(n) + 1
                    out[-1] = (prev_role, f"{base}×{new_n}")
                    continue
                except ValueError:
                    pass
            elif "×" in prev_text and prev_text.rsplit("×", 1)[-1].isdigit():
                base, _, n = prev_text.rpartition("×")
                out[-1] = (prev_role, f"{base}×{int(n) + 1}")
                continue
            out[-1] = (prev_role, f"{prev_text} ×2")
        else:
            out.append((role, text))
    return out


def _truncate(s: str, max_chars: int, suffix: str = " […]") -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars - len(suffix)].rstrip() + suffix



_ANTI_REREAD_NOTICE = """## ⚠ Authoritative record — don't re-explore

This brief is the prior session's full conversation, trimmed. Treat
it as ground truth:

- Files already Read here (look for `[Read file_path=...]` markers) →
  do **not** re-Read them unless the user explicitly asks. Assume
  contents match what was Read.
- Bash commands shown here with their output discussed → do **not**
  re-run them just to recover context.
- Sub-agent reports (look for `[Sub-agent report: ...]` blocks) →
  treat findings as authoritative; do **not** re-dispatch the same
  agent.
- Directories already explored via Glob/Grep → do **not** re-walk
  them to "refresh context".

If you genuinely need fresh state (file may have changed since the
prior session, command output is time-sensitive), **ask the user
first** before re-fetching.
"""


def _render_brief(
    iso: str,
    session_id: str,
    cwd: str,
    archive_hash: str | None,
    convo: list[tuple[str, str]],
) -> str:
    out: list[str] = []
    out.append(f"# Session Brief — {iso}")
    out.append(f"\n**Session:** `{session_id}`  \n**Cwd:** `{cwd}`")
    if archive_hash:
        out.append(
            f"**Archive:** memory doc `{archive_hash}` "
            f"(full raw via `memory doc get {archive_hash}`)"
        )
    out.append("")
    out.append(_ANTI_REREAD_NOTICE)
    out.append("\n## Conversation\n")
    for role, text in convo:
        marker = "U:" if role == "user" else "A:"
        out.append(f"\n{marker} {text}")
    return "\n".join(out) + "\n"


def build_convo(entries: list[dict]) -> list[tuple[str, str]]:
    """Run the trimmer's convo-build phase and return `(role, text)` tuples
    after `_collapse_repeats`. Exposed so the audit / debug path
    (`render_html` dedup section) can replay the exact same convo."""
    agent_use_meta: dict[str, tuple[str, str]] = {}
    for e in entries:
        if e.get("type") != "assistant":
            continue
        for b in assistant_blocks(e):
            if b.get("type") == "tool_use" and b.get("name") in SUBAGENT_TOOL_NAMES:
                inp = b.get("input") or {}
                agent_use_meta[str(b.get("id") or "")] = (
                    str(inp.get("description") or ""),
                    str(inp.get("subagent_type") or ""),
                )

    seen_user: set[str] = set()
    seen_paths: set[str] = set()
    convo: list[tuple[str, str]] = []
    for i, e in enumerate(entries):
        nxt = entries[i + 1] if i + 1 < len(entries) else None
        if e.get("type") in DROP_TOP_TYPES:
            continue
        if is_real_user(e):
            t = user_text(e)
            if not t or is_noise_user_msg(t):
                continue
            t = elide_pasted_output(t)
            if t in seen_user:
                continue
            seen_user.add(t)
            convo.append(("user", t))
        elif e.get("type") == "user":
            c = e.get("message", {}).get("content")
            if isinstance(c, list):
                from handoff.extract import _agent_report_text_from_tooluseresult

                tur_text = _agent_report_text_from_tooluseresult(e.get("toolUseResult"))
                for b in c:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    meta = agent_use_meta.get(str(b.get("tool_use_id") or ""))
                    if not meta:
                        continue
                    text = tur_text
                    if not text:
                        tc = b.get("content")
                        if isinstance(tc, list):
                            text = "\n".join(
                                blk.get("text", "")
                                for blk in tc
                                if isinstance(blk, dict) and blk.get("type") == "text"
                            )
                        elif isinstance(tc, str):
                            text = tc
                    text = (text or "").strip()
                    if len(text) >= AGENT_REPORT_MIN_CHARS:
                        desc = meta[0] or "(no description)"
                        sub = f" {meta[1]}" if meta[1] else ""
                        convo.append(("assistant", f"[Sub-agent report:{sub} {desc}]\n{text}"))
        elif e.get("type") == "assistant":
            r = render_assistant(e, nxt, seen_paths=seen_paths)
            if r:
                convo.append(("assistant", r))

    return _collapse_repeats(convo)


def render_brief(
    entries: list[dict],
    session_id: str,
    cwd: str,
    archive_hash: str | None,
) -> str:
    """Render the session brief: anti-re-read header + trimmed conversation."""
    convo = build_convo(entries)
    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return _render_brief(iso, session_id, cwd, archive_hash, convo)

