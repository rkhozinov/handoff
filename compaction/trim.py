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

Output is tiered:
  * tier1 — must-have summary (~<=20 KB target): metadata, archive ref,
    active goal, last N decisions, files touched, open todos, errors,
    code anchors, last 20 signal user msgs. Suitable for `/handon` Read
    injection (25 KB hard cap).
  * tier2 — full trimmed conversation transcript with squeezed markers.
    Lives on disk, referenced from tier1 metadata for on-demand `Read`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from compaction.extract import (
    SUBAGENT_TOOL_NAMES,
    assistant_blocks,
    extract_agent_reports,
    DROP_TOP_TYPES,
    elide_pasted_output,
    extract_code_anchors,
    extract_decisions,
    extract_errors,
    extract_files_touched,
    extract_plans_saved,
    extract_todo_snapshot,
    is_noise_user_msg,
    is_real_user,
    iter_signal_user_msgs,
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

# Tier-1 budget. /handon Read caps at 25 KB; leave headroom.
TIER1_BUDGET_BYTES = 20_000

# Per-turn assistant text cap in tier2. Prevents one giant turn (e.g. a
# long `Recommendation` block) from dominating the brief. Whole reasoning
# stays in the memory doc archive; tier2 keeps a head + tail marker.
ASSISTANT_TURN_MAX_CHARS = 4_000


def _classify_assistant(
    entry: dict,
    seen_paths: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Return (joined_text, tool_markers) split out of the entry's blocks.
    Thinking blocks are silently dropped here. Per-turn text exceeding
    ASSISTANT_TURN_MAX_CHARS is truncated with a marker so the full body
    stays in the memory doc archive but tier2 doesn't bloat."""
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

        narration_drop = not tool_markers and bool(NARRATION_RE.match(text_joined))

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


def _render_tier1(
    *,
    iso: str,
    session_id: str,
    cwd: str,
    archive_hash: str | None,
    tier2_path: str | None,
    signal_msgs: list[str],
    decisions: list[str],
    files: list[str],
    todos: str | None,
    errors: list[str],
    code_anchors: list[str],
    decision_limit: int,
    last_user_msgs: int,
    code_anchor_limit: int,
    file_limit: int = 50,
    user_msg_max_chars: int = 500,
    decision_max_chars: int = 300,
    code_anchor_max_chars: int = 800,
    active_goal_max_chars: int = 1500,
    recalled_memories: list[str] | None = None,
    agent_reports: list[tuple[str, str, str]] | None = None,
    agent_report_limit: int = 5,
    agent_report_max_chars: int = 1500,
    plans: list[str] | None = None,
) -> str:
    archive_line = (
        f"Full session: memory doc `{archive_hash}` — recall via `memory doc get {archive_hash}`"
        if archive_hash
        else "Full session archive: NOT STORED"
    )
    tier2_line = (
        f"Full conversation (trimmed): `{tier2_path}` — open with Read tool when more context needed"
        if tier2_path
        else "Full conversation: not written to disk"
    )

    out: list[str] = []
    out.append(f"# Session Brief — {iso}")
    out.append(f"\n**Session:** `{session_id}`  \n**Cwd:** `{cwd}`")
    out.append(f"\n## Archive\n{archive_line}\n{tier2_line}")
    active_goal = signal_msgs[-1] if signal_msgs else "(no signal user messages found)"
    out.append(f"\n## Active Goal\n{_truncate(active_goal, active_goal_max_chars)}")

    if recalled_memories:
        out.append("\n## Relevant Prior Memories")
        for line in recalled_memories:
            out.append(line)

    if decisions:
        out.append("\n## Decisions / Direction Reversals")
        for d in decisions[-decision_limit:]:
            out.append(f"- {_truncate(d, decision_max_chars)}")

    if todos:
        out.append("\n## Open TodoList")
        out.append("```json")
        out.append(_truncate(todos, 2000))
        out.append("```")

    if plans:
        # Plan files saved during the session — only the path is recorded.
        # The file persists on disk; reader can `Read` it when needed.
        out.append(f"\n## Plans Saved ({len(plans)})")
        for path in plans:
            out.append(f"- `{path}`")

    if errors:
        out.append("\n## Errors Hit")
        for err in errors:
            out.append("```")
            out.append(_truncate(err, 400))
            out.append("```")

    if agent_reports:
        kept_reports = agent_reports[-agent_report_limit:]
        out.append(f"\n## Sub-Agent Findings ({len(kept_reports)})")
        for desc, sub, txt in kept_reports:
            header = desc.strip() or "(no description)"
            sub_part = f" — {sub}" if sub else ""
            out.append(f"\n### {header}{sub_part}")
            out.append(_truncate(txt, agent_report_max_chars))

    if files:
        capped_files = files[:file_limit]
        omitted = len(files) - len(capped_files)
        out.append(
            f"\n## Files Touched ({len(capped_files)}"
            + (f"; +{omitted} more in tier2" if omitted > 0 else "")
            + ")"
        )
        for f in capped_files:
            out.append(f"- `{f}`")

    if signal_msgs:
        kept = signal_msgs[-last_user_msgs:]
        out.append(f"\n## Last {len(kept)} Signal User Messages")
        for m in kept:
            out.append(f"- {_truncate(m, user_msg_max_chars)}")

    if code_anchors:
        kept_anchors = code_anchors[-code_anchor_limit:]
        out.append(f"\n## Code Anchors ({len(kept_anchors)})")
        for fence in kept_anchors:
            out.append(_truncate(fence, code_anchor_max_chars))

    return "\n".join(out) + "\n"


def _render_tier2(
    iso: str,
    session_id: str,
    cwd: str,
    convo: list[tuple[str, str]],
) -> str:
    out: list[str] = []
    out.append(f"# Session Conversation (trimmed) — {iso}")
    out.append(f"\n**Session:** `{session_id}`  \n**Cwd:** `{cwd}`\n")
    for role, text in convo:
        marker = "U:" if role == "user" else "A:"
        out.append(f"\n{marker} {text}")
    return "\n".join(out) + "\n"


def build_convo(entries: list[dict]) -> list[tuple[str, str]]:
    """Run the trimmer's convo-build phase and return `(role, text)` tuples
    after `_collapse_repeats` but BEFORE semantic dedup. Exposed so the
    audit / debug path (`render_html` dedup section) can replay the exact
    same convo without re-rendering both tiers."""
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
                from compaction.extract import _agent_report_text_from_tooluseresult

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
                    if len(text) >= 200:
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
    tier2_path: str | None = None,
    code_anchor_limit: int = 10,
    decision_limit: int = 10,
    last_user_msgs: int = 20,
    recalled_memories: list[str] | None = None,
    agent_report_limit: int = 5,
) -> tuple[str, str]:
    """Render both tiers. Returns (tier1, tier2).

    tier1 = compact summary intended for /handon Read injection (~<=20 KB).
    tier2 = full trimmed conversation, written to a separate file and
            referenced from tier1 for on-demand Read.
    """
    signal_msgs = iter_signal_user_msgs(entries)
    decisions = extract_decisions(signal_msgs)
    files = extract_files_touched(entries)
    todos = extract_todo_snapshot(entries)
    plans = extract_plans_saved(entries)
    errors = extract_errors(entries)
    code_anchors = extract_code_anchors(entries)
    # Tier1 gets truncated reports (1500 char cap); tier2 keeps full bodies.
    agent_reports_tier1 = extract_agent_reports(entries, max_chars=1500)
    agent_reports_tier2 = extract_agent_reports(entries, max_chars=0)

    convo = build_convo(entries)

    iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    tier1 = _render_tier1(
        iso=iso,
        session_id=session_id,
        cwd=cwd,
        archive_hash=archive_hash,
        tier2_path=tier2_path,
        signal_msgs=signal_msgs,
        decisions=decisions,
        files=files,
        todos=todos,
        errors=errors,
        code_anchors=code_anchors,
        decision_limit=decision_limit,
        last_user_msgs=last_user_msgs,
        code_anchor_limit=code_anchor_limit,
        recalled_memories=recalled_memories,
        agent_reports=agent_reports_tier1,
        agent_report_limit=agent_report_limit,
        plans=plans,
    )

    # Progressive trim if tier1 over budget. Order: shrink files → fewer
    # code anchors → fewer user msgs → tighter per-section caps. Agent
    # report cap also tightens because reports can be 1500 chars × 5 = 7.5 KB.
    def _attempt(file_lim, code_lim, user_msg_lim, code_chars, user_chars,
                 agent_lim, agent_chars):
        return _render_tier1(
            iso=iso, session_id=session_id, cwd=cwd, archive_hash=archive_hash,
            tier2_path=tier2_path, signal_msgs=signal_msgs, decisions=decisions,
            files=files, todos=todos, errors=errors, code_anchors=code_anchors,
            decision_limit=decision_limit, last_user_msgs=user_msg_lim,
            code_anchor_limit=code_lim, file_limit=file_lim,
            code_anchor_max_chars=code_chars, user_msg_max_chars=user_chars,
            recalled_memories=recalled_memories,
            agent_reports=agent_reports_tier1,
            agent_report_limit=agent_lim,
            agent_report_max_chars=agent_chars,
            plans=plans,
        )

    for params in [
        (50, 10, 20, 800, 500, 5, 1500),   # default
        (30, 5, 15, 400, 300, 5, 800),     # 1st squeeze
        (20, 3, 10, 200, 200, 3, 500),     # 2nd squeeze
        (10, 2, 5, 100, 120, 2, 300),      # 3rd squeeze
    ]:
        tier1 = _attempt(*params)
        if len(tier1.encode("utf-8")) <= TIER1_BUDGET_BYTES:
            break

    # Hard cap: if even the tightest squeeze overflows TIER1_BUDGET_BYTES,
    # byte-truncate so /handon's 25 KB Read budget can't reject the whole
    # brief. The full content stays in tier2 and the memory doc.
    if len(tier1.encode("utf-8")) > TIER1_BUDGET_BYTES:
        tier1 = _hard_truncate_bytes(
            tier1,
            TIER1_BUDGET_BYTES,
            suffix="\n\n[...truncated; read tier2 file for full brief]\n",
        )

    tier2 = _render_tier2(iso, session_id, cwd, convo)
    return tier1, tier2


def _hard_truncate_bytes(s: str, max_bytes: int, suffix: str) -> str:
    """Truncate `s` so its UTF-8 encoding fits in `max_bytes`, leaving room
    for `suffix`. Cuts on a line boundary when possible so the result stays
    parseable as Markdown."""
    suffix_b = suffix.encode("utf-8")
    budget = max_bytes - len(suffix_b)
    if budget <= 0:
        return suffix
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    head = encoded[:budget]
    # Avoid splitting a multi-byte UTF-8 codepoint; back up to a safe boundary.
    while head and (head[-1] & 0xC0) == 0x80:
        head = head[:-1]
    text = head.decode("utf-8", errors="ignore")
    nl = text.rfind("\n")
    if nl > budget // 2:
        text = text[:nl]
    return text + suffix
