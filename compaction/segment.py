"""Split a Claude Code transcript into topic segments.

Real long sessions span many topics — the user runs `/clear` mid-session,
walks away for hours, switches projects. A single chronological reduction
over the whole stream loses nuance because every topic competes for the
same tier1 budget.

This module emits half-open `(start, end)` ranges over a list of CC
transcript entries so future code can summarize each segment
independently. **No reduction logic lives here** — only boundary
detection. Integration with `trim.py` is intentionally separate.

Two boundary heuristics:

  1. `/clear` boundary — a fresh-context marker the user issues
     manually. Detected on either:
       - a real user msg whose text starts with `/clear`, OR
       - a CC `last-prompt` entry whose payload contains the literal
         string `/clear`.
     We check both shapes defensively because the on-disk encoding has
     varied across CC versions and isn't 100% stable.
  2. Idle gap — when consecutive ISO-8601 timestamps are more than
     `idle_gap_seconds` apart, the user has clearly walked away and
     come back to a different task.

Conventions:
  - Ranges are half-open: `entries[start:end]`.
  - A boundary STARTS the new segment. The boundary entry (the /clear
    msg or the first entry after a long gap) lives at the head of the
    new segment.
  - Empty segments are never emitted; if a boundary fires before any
    content, it collapses into the next segment.
  - For an empty input list, returns `[]` (NOT `[(0, 0)]`) so callers
    can iterate with `for s, e in segments: ...` and produce nothing.
"""
from __future__ import annotations

from datetime import datetime

from compaction.extract import is_real_user, user_text


def _parse_ts(s: object) -> datetime | None:
    """Best-effort ISO-8601 parser.

    Returns None for anything that isn't a parseable string. CC writes
    timestamps like `2026-05-07T21:04:58.374Z`; `fromisoformat` accepts
    that on 3.11+ via the trailing-Z support, but we strip the `Z` to
    stay portable to older runtimes.
    """
    if not isinstance(s, str) or not s:
        return None
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _is_clear_boundary(entry: dict) -> bool:
    """True if `entry` represents a `/clear` event.

    Two shapes accepted:
      1. A real user msg whose text starts with `/clear` (the user
         literally typed it).
      2. A `last-prompt` CC bookkeeping entry whose payload mentions
         `/clear` literally — some CC versions strip the slash command
         out of the user msg and only record it on this side-channel.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("type") == "last-prompt":
        # `lastPrompt` is the typical key; fall back to scanning the
        # whole serialized entry for the literal so we don't miss
        # variant shapes.
        lp = entry.get("lastPrompt")
        if isinstance(lp, str) and "/clear" in lp:
            return True
        # Defensive: scan top-level string values for `/clear`.
        for v in entry.values():
            if isinstance(v, str) and "/clear" in v:
                return True
        return False
    if is_real_user(entry):
        text = user_text(entry).lstrip()
        if text.startswith("/clear"):
            return True
    return False


def segment_transcript(
    entries: list[dict],
    idle_gap_seconds: int = 1800,
) -> list[tuple[int, int]]:
    """Return half-open `(start, end)` ranges segmenting `entries`.

    Boundaries fire on either:
      1. A `/clear` marker — a real user msg starting with `/clear`,
         or a `last-prompt` entry whose payload mentions `/clear`.
      2. An idle gap — `entries[i].timestamp - entries[i-1].timestamp`
         exceeds `idle_gap_seconds`. Only consecutive entries that
         BOTH carry parseable ISO-8601 timestamps participate; missing
         or malformed timestamps are treated as "no gap" so a single
         broken entry doesn't fragment an otherwise-contiguous run.

    A new segment starts AT the boundary entry. Segments are non-empty;
    a boundary firing before any content is collapsed into the next
    segment (i.e. consecutive boundaries don't produce empty ranges).

    Always returns at least one segment covering the whole list, except
    for an empty input which returns `[]`.
    """
    if not entries:
        return []

    # Indices where a new segment STARTS. 0 is always a start; each
    # detected boundary appends one strictly-increasing index.
    starts: list[int] = [0]

    prev_ts: datetime | None = _parse_ts(entries[0].get("timestamp"))

    for i in range(1, len(entries)):
        entry = entries[i]

        # Idle gap check — uses prev_ts which is the last
        # successfully-parsed timestamp seen.
        cur_ts = _parse_ts(entry.get("timestamp"))
        gap = False
        if prev_ts is not None and cur_ts is not None:
            if (cur_ts - prev_ts).total_seconds() > idle_gap_seconds:
                gap = True

        # /clear check is independent of the gap check; both can fire
        # on the same entry but we only record one start index.
        is_boundary = gap or _is_clear_boundary(entry)

        if is_boundary:
            # Each boundary opens a non-empty segment because the
            # boundary entry itself is content (the /clear msg, or the
            # first entry after the gap). Two consecutive boundaries
            # give two single-entry segments — never an empty range.
            starts.append(i)

        # Roll prev_ts forward only when current entry has a parseable
        # ts. A malformed ts means the next iteration compares against
        # the last good ts, which is the safest behavior — we don't
        # want to fragment a run because of one corrupt line.
        if cur_ts is not None:
            prev_ts = cur_ts

    # Build half-open ranges from starts.
    n = len(entries)
    out: list[tuple[int, int]] = []
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else n
        if e > s:
            out.append((s, e))
    return out
