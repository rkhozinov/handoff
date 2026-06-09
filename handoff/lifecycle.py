"""Session lifecycle: classify a transcript as pending / in_progress / done.

Conservative bias: when uncertain, return `in_progress` (NOT `done`). A
false-`done` hides the brief from the `/hand:on` picker, which is the
worse failure mode. A false-`in_progress` only adds picker clutter.

Signal precedence (first match wins):

1. `auto-todowrite` — last TodoWrite call had all entries `completed` and no
   `pending` / `in_progress` left  →  done
2. `auto-user-msg`  — any of the last 3 real user messages matches the
   completion regex (ship, merged, lgtm, thanks, etc.)  →  done
3. `auto-open-q`    — last real user message looks like an open question
   (ends with `?` or starts with what/why/how/when/where/which/can/does)
   →  pending
4. `auto-default`   — anything else  →  in_progress

The detector takes the same `entries` list `render_brief` consumes, so no
second JSONL pass is needed.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from handoff.extract import (
    assistant_blocks,
    is_injected_user_msg,
    is_noise_user_msg,
    is_real_user,
    user_text,
)

Status = Literal["pending", "in_progress", "done"]
Signal = Literal[
    "auto-todowrite",
    "auto-user-msg",
    "auto-open-q",
    "auto-default",
    "auto-stale",
    "manual",
    "backfill",
]

STALE_DAYS_DEFAULT = 14

# Hard cap on the recap frontmatter value. Recaps are one-liners; anything
# longer belongs in the brief body.
RECAP_MAX_CHARS = 300

# A completion keyword only counts when the message is terse ("thanks",
# "merged, ship it"). Long messages embed keywords as task CONTENT — live
# false-done (2026-06-04): opening request "i done it manually copying…"
# (280 chars) matched `done`. Tightening = conservative-bias-safe.
DONE_MSG_MAX_CHARS = 100

_DONE_RE = re.compile(
    r"\b("
    r"ship(?:ped|d)?|"
    r"merged|"
    r"lgtm|"
    r"thanks?|"
    r"thx|"
    r"done|"
    r"that\s+worked|"
    r"all\s+good|"
    r"all\s+set|"
    r"perfect|"
    r"finished|"
    r"wrap(?:ped)?\s*up|"
    r"complete[d]?|"
    r"closed?|"
    r"resolved|"
    r"fixed"
    r")\b",
    re.IGNORECASE,
)

_QUESTION_PREFIX_RE = re.compile(
    r"^\s*(what|why|how|when|where|which|who|can|could|should|would|do|does|did|is|are|will)\b",
    re.IGNORECASE,
)


def _last_todowrite_todos(entries: list[dict]) -> list[dict] | None:
    """Return the todo dicts of the LAST TodoWrite call, or None if no
    TodoWrite call appeared in the transcript."""
    last: list[dict] | None = None
    for e in entries:
        if e.get("type") != "assistant":
            continue
        for b in assistant_blocks(e):
            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                continue
            if b.get("name") != "TodoWrite":
                continue
            inp = b.get("input") or {}
            todos = inp.get("todos")
            if not isinstance(todos, list):
                continue
            last = [t for t in todos if isinstance(t, dict)]
    return last


def _last_todowrite_state(entries: list[dict]) -> list[str] | None:
    """Return the `status` field of every todo in the LAST TodoWrite call,
    or None if no TodoWrite call appeared in the transcript."""
    todos = _last_todowrite_todos(entries)
    if todos is None:
        return None
    return [str(t.get("status") or "") for t in todos]


def _last_real_user_msgs(entries: list[dict], n: int) -> list[str]:
    """Walk entries in reverse; return up to N most-recent real user texts
    (oldest-first in the returned list).

    Skips CC-injected pseudo-user text (slash command bodies etc.) — a
    /hand:off skill body contains the literal word "done" and would
    false-`done` every session it runs in. Short acks are KEPT: bare
    "done" / "thanks" from the user is the detector's primary signal."""
    out: list[str] = []
    for e in reversed(entries):
        if not is_real_user(e):
            continue
        t = user_text(e)
        if not t or is_injected_user_msg(t):
            continue
        out.append(t)
        if len(out) >= n:
            break
    out.reverse()
    return out


def detect_status(entries: list[dict]) -> tuple[Status, Signal]:
    """Classify a transcript's end-state. See module docstring for the
    precedence rules. Returns `(status, signal)` so the caller can record
    WHY a session was marked done — useful for the manual override path
    and for debugging false positives."""

    # 1. TodoWrite final state.
    todos = _last_todowrite_state(entries)
    if todos is not None and todos:
        if all(s == "completed" for s in todos):
            return ("done", "auto-todowrite")

    # 2. Last N user messages — explicit completion language. Only terse
    #    messages count (see DONE_MSG_MAX_CHARS).
    last_msgs = _last_real_user_msgs(entries, n=3)
    if last_msgs and any(
        len(m.strip()) <= DONE_MSG_MAX_CHARS and _DONE_RE.search(m)
        for m in last_msgs
    ):
        return ("done", "auto-user-msg")

    # 3. Final user message looks like an open question.
    if last_msgs:
        final = last_msgs[-1].strip()
        if final.endswith("?") or _QUESTION_PREFIX_RE.match(final):
            return ("pending", "auto-open-q")

    # 4. Fallback — resumable work, no strong terminal signal either way.
    return ("in_progress", "auto-default")


def sanitize_recap(text: str | None) -> str | None:
    """Collapse a recap to a single frontmatter-safe line: whitespace runs
    (incl. newlines) become single spaces, capped at `RECAP_MAX_CHARS`.
    Empty / whitespace-only input → None."""
    if not text:
        return None
    flat = re.sub(r"\s+", " ", text).strip()
    if not flat:
        return None
    if len(flat) > RECAP_MAX_CHARS:
        flat = flat[: RECAP_MAX_CHARS - 1].rstrip() + "…"
    return flat


def extract_recap(entries: list[dict]) -> str | None:
    """Deterministic recap fallback for direct CLI runs (no `--recap`).

    Shape: `<first real user msg> | next: <first open todo>` — the goal as
    the user phrased it, plus the next actionable step if a TodoWrite trail
    exists. Much weaker than the LLM-composed recap, but better than nothing.
    """
    goal: str | None = None
    for e in entries:
        if not is_real_user(e):
            continue
        t = user_text(e)
        # Full noise filter here (unlike the detector): short acks and
        # injected command bodies both make useless goals.
        if t and not is_noise_user_msg(t):
            goal = t
            break
    if goal is None:
        return None

    todos = _last_todowrite_todos(entries)
    next_step: str | None = None
    if todos:
        for t in todos:
            if str(t.get("status") or "") != "completed":
                next_step = str(t.get("content") or "").strip() or None
                break

    recap = goal if next_step is None else f"{goal} | next: {next_step}"
    return sanitize_recap(recap)


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FM_KEY_RE = re.compile(r"^([a-z_]+):\s*(.*)$")

# Frontmatter keys, written in this order. Single source of truth.
FRONTMATTER_KEYS = (
    "status",
    "title",
    "session_id",
    "cwd",
    "created",
    "last_resumed",
    "completion_signal",
    "archive_hash",
    "recap",
    "recap_source",
)


def now_iso() -> str:
    """UTC `YYYY-MM-DDTHH:MM:SSZ` — same shape used elsewhere in handoff."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_frontmatter(text: str) -> dict[str, str | None]:
    """Pull the leading `---`-fenced YAML block off a brief. Returns `{}` if
    no fence is present. Only flat `key: value` lines are recognised — that's
    all the brief frontmatter ever contains. `null` literal → Python `None`."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str | None] = {}
    for raw in m.group(1).splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        km = _FM_KEY_RE.match(line)
        if not km:
            continue
        k = km.group(1)
        v = km.group(2).strip()
        if v == "" or v.lower() == "null":
            out[k] = None
        else:
            out[k] = v
    return out


def strip_frontmatter(text: str) -> str:
    """Return `text` with any leading `---`-fenced block removed."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text
    return text[m.end():]


def render_frontmatter(fm: dict[str, str | None]) -> str:
    """Render a frontmatter dict as a `---`-fenced YAML block. Keys appear in
    `FRONTMATTER_KEYS` order; unknown keys are appended after (alpha-sorted)
    so callers can stash extra metadata without losing it on round-trip."""
    lines = ["---"]
    known = set(FRONTMATTER_KEYS)
    for k in FRONTMATTER_KEYS:
        v = fm.get(k)
        lines.append(f"{k}: {'null' if v is None else v}")
    for k in sorted(set(fm) - known):
        v = fm[k]
        lines.append(f"{k}: {'null' if v is None else v}")
    lines.append("---\n")
    return "\n".join(lines)


def _parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 string written by `now_iso()` back into a datetime.
    Accepts both `…Z` and `…+00:00` suffixes. Returns None on garbage."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def is_stale(
    fm: dict[str, str | None],
    *,
    now: datetime | None = None,
    days: int = STALE_DAYS_DEFAULT,
) -> bool:
    """A brief is stale when it's still `pending` or `in_progress` AND the
    most-recent activity timestamp (`last_resumed` if set, else `created`)
    is older than `days`. Manual statuses (`done` + signal `manual`) are
    never considered stale — the user's call wins."""
    status = fm.get("status")
    if status not in ("pending", "in_progress"):
        return False
    if (now or datetime.now(timezone.utc)) is None:
        return False
    now = now or datetime.now(timezone.utc)

    ref_iso = fm.get("last_resumed") or fm.get("created")
    ref = _parse_iso(ref_iso)
    if ref is None:
        return False  # No timestamp → can't decide; leave alone.

    age = now - ref
    return age.days >= days


def mark_stale(fm: dict[str, str | None]) -> dict[str, str | None]:
    """Return a copy of `fm` with status flipped to `done` and
    `completion_signal: auto-stale`. Caller owns persistence."""
    out = dict(fm)
    out["status"] = "done"
    out["completion_signal"] = "auto-stale"
    return out


def read_existing_brief(brief_path: Path) -> dict[str, str | None]:
    """Read frontmatter from an existing brief on disk. Empty dict if the
    file doesn't exist or has no frontmatter."""
    try:
        text = brief_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return parse_frontmatter(text)


def resolve_frontmatter(
    *,
    session_id: str,
    cwd: str,
    detected_status: Status,
    detected_signal: Signal,
    archive_hash: str | None,
    existing: dict[str, str | None],
    recap: str | None = None,
    extracted_recap: str | None = None,
    title: str | None = None,
) -> dict[str, str | None]:
    """Merge detector output with any pre-existing frontmatter.

    Rules:
      * `created`: preserve from existing if present, else now.
      * `last_resumed`: preserve as-is (only `/hand:on` flips it).
      * `status` + `completion_signal`: if existing is a MANUAL `done` or
        `archived`, the user's decision wins — never auto-revive/un-archive.
        Otherwise the fresh detection overrides.
      * `session_id`, `cwd`, `archive_hash`: always take the fresh value.
      * `title`: fresh value (CC's ai-title from the transcript) wins;
        existing preserved when the transcript carries none.
      * `recap`: precedence `recap` arg (LLM-composed via `--recap`) >
        existing LLM recap > `extracted_recap` (deterministic fallback) >
        existing extracted recap. An LLM recap is never downgraded to an
        extracted one on re-run.
    """
    out: dict[str, str | None] = {
        "session_id": session_id,
        "cwd": cwd,
        "created": existing.get("created") or now_iso(),
        "last_resumed": existing.get("last_resumed"),
        "archive_hash": archive_hash,
        "title": title or existing.get("title"),
    }

    llm_recap = sanitize_recap(recap)
    if llm_recap:
        out["recap"] = llm_recap
        out["recap_source"] = "llm"
    elif existing.get("recap") and existing.get("recap_source") == "llm":
        out["recap"] = existing["recap"]
        out["recap_source"] = "llm"
    elif sanitize_recap(extracted_recap):
        out["recap"] = sanitize_recap(extracted_recap)
        out["recap_source"] = "extracted"
    elif existing.get("recap"):
        out["recap"] = existing["recap"]
        out["recap_source"] = existing.get("recap_source") or "extracted"
    else:
        out["recap"] = None
        out["recap_source"] = None

    if (
        existing.get("completion_signal") == "manual"
        and existing.get("status") in ("done", "archived")
    ):
        out["status"] = existing["status"]
        out["completion_signal"] = "manual"
    else:
        out["status"] = detected_status
        out["completion_signal"] = detected_signal

    return out
