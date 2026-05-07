"""Unit tests for `compaction.segment` — pure boundary detection over
synthetic transcript dicts. No fixtures required."""
from __future__ import annotations

from compaction.segment import (
    _is_clear_boundary,
    _parse_ts,
    segment_transcript,
)


# ---------- helpers (mirror tests/test_extract.py style) ----------

def _user_str(text: str, ts: str | None = None) -> dict:
    e = {"type": "user", "message": {"role": "user", "content": text}}
    if ts is not None:
        e["timestamp"] = ts
    return e


def _assistant_text(text: str, ts: str | None = None) -> dict:
    e = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }
    if ts is not None:
        e["timestamp"] = ts
    return e


def _last_prompt(payload: str) -> dict:
    return {"type": "last-prompt", "lastPrompt": payload}


# ---------- _parse_ts ----------

def test_parse_ts_zulu():
    assert _parse_ts("2026-05-07T21:04:58.374Z") is not None


def test_parse_ts_offset():
    assert _parse_ts("2026-05-07T21:04:58+00:00") is not None


def test_parse_ts_none_for_garbage():
    assert _parse_ts("not-a-timestamp") is None


def test_parse_ts_none_for_empty():
    assert _parse_ts("") is None


def test_parse_ts_none_for_non_string():
    assert _parse_ts(None) is None
    assert _parse_ts(12345) is None


# ---------- _is_clear_boundary ----------

def test_is_clear_boundary_user_msg_exact():
    assert _is_clear_boundary(_user_str("/clear"))


def test_is_clear_boundary_user_msg_with_args():
    assert _is_clear_boundary(_user_str("/clear  some args"))


def test_is_clear_boundary_user_msg_leading_whitespace():
    assert _is_clear_boundary(_user_str("   /clear"))


def test_is_clear_boundary_user_msg_substring_does_not_count():
    # `/clear` must START the user msg, not appear mid-text
    assert not _is_clear_boundary(_user_str("please run /clear later"))


def test_is_clear_boundary_last_prompt_payload():
    assert _is_clear_boundary(_last_prompt("/clear"))


def test_is_clear_boundary_last_prompt_with_extra_text():
    assert _is_clear_boundary(_last_prompt("user said /clear then idled"))


def test_is_clear_boundary_assistant_text_ignored():
    # Assistant blocks never count; only user msgs and last-prompt
    assert not _is_clear_boundary(_assistant_text("/clear"))


def test_is_clear_boundary_non_dict():
    assert not _is_clear_boundary("not-a-dict")  # type: ignore[arg-type]
    assert not _is_clear_boundary(None)  # type: ignore[arg-type]


# ---------- segment_transcript ----------

def test_empty_returns_empty_list():
    """Documented choice: prefer [] over [(0, 0)] so callers that
    iterate `for s, e in segments` produce nothing for empty input."""
    assert segment_transcript([]) == []


def test_single_user_msg_one_segment():
    assert segment_transcript([_user_str("hello", "2026-05-07T10:00:00Z")]) == [(0, 1)]


def test_two_msgs_within_idle_window_single_segment():
    entries = [
        _user_str("first", "2026-05-07T10:00:00Z"),
        _user_str("second", "2026-05-07T10:10:00Z"),  # 10 min later
    ]
    assert segment_transcript(entries) == [(0, 2)]


def test_two_msgs_across_idle_gap_split():
    entries = [
        _user_str("first",  "2026-05-07T10:00:00Z"),
        _user_str("second", "2026-05-07T11:00:00Z"),  # 60 min > 30 min default
    ]
    assert segment_transcript(entries) == [(0, 1), (1, 2)]


def test_clear_boundary_mid_stream():
    entries = [
        _user_str("topic A start",  "2026-05-07T10:00:00Z"),
        _assistant_text("...working on A", "2026-05-07T10:00:30Z"),
        _user_str("/clear",          "2026-05-07T10:01:00Z"),
        _user_str("topic B start",  "2026-05-07T10:01:30Z"),
    ]
    # Segment 0 is the pre-clear chunk; segment 1 starts AT the /clear entry.
    assert segment_transcript(entries) == [(0, 2), (2, 4)]


def test_last_prompt_clear_boundary():
    entries = [
        _user_str("first",   "2026-05-07T10:00:00Z"),
        _last_prompt("/clear"),
        _user_str("after",   "2026-05-07T10:01:00Z"),
    ]
    assert segment_transcript(entries) == [(0, 1), (1, 3)]


def test_idle_gap_and_clear_combined():
    entries = [
        _user_str("a1", "2026-05-07T10:00:00Z"),
        _user_str("a2", "2026-05-07T10:05:00Z"),
        # 90-min idle gap → boundary at index 2
        _user_str("b1", "2026-05-07T11:35:00Z"),
        _user_str("b2", "2026-05-07T11:36:00Z"),
        # explicit /clear → boundary at index 4
        _user_str("/clear and switch", "2026-05-07T11:37:00Z"),
        _user_str("c1", "2026-05-07T11:38:00Z"),
    ]
    segs = segment_transcript(entries)
    # Segments cover [0,2), [2,4), [4,6) — sorted, contiguous, non-empty.
    assert segs == [(0, 2), (2, 4), (4, 6)]


def test_custom_idle_gap_seconds_honored():
    entries = [
        _user_str("a", "2026-05-07T10:00:00Z"),
        _user_str("b", "2026-05-07T10:02:00Z"),  # 120s
    ]
    # With default (1800s) → one segment.
    assert segment_transcript(entries) == [(0, 2)]
    # With idle_gap_seconds=60 the 120s delta now exceeds it → split.
    assert segment_transcript(entries, idle_gap_seconds=60) == [(0, 1), (1, 2)]


def test_malformed_timestamps_do_not_crash():
    entries = [
        _user_str("a", "2026-05-07T10:00:00Z"),
        _user_str("b", "not-a-timestamp"),
        _user_str("c", "also-bogus"),
        _user_str("d", "2026-05-07T10:01:00Z"),
    ]
    # No idle gap should be detected because bad timestamps are
    # skipped (treated as "no gap"). Result: single segment.
    segs = segment_transcript(entries)
    assert segs == [(0, 4)]


def test_missing_timestamps_no_gap():
    entries = [
        _user_str("a"),
        _user_str("b"),
        _user_str("c"),
    ]
    assert segment_transcript(entries) == [(0, 3)]


def test_consecutive_clear_boundaries_collapsed():
    """Two /clear in a row shouldn't produce an empty segment between
    them. The second /clear opens a new segment that contains both
    /clear msgs minus the first; specifically every emitted range is
    non-empty."""
    entries = [
        _user_str("topic", "2026-05-07T10:00:00Z"),
        _user_str("/clear", "2026-05-07T10:01:00Z"),
        _user_str("/clear", "2026-05-07T10:02:00Z"),
        _user_str("after", "2026-05-07T10:03:00Z"),
    ]
    segs = segment_transcript(entries)
    # All ranges must be non-empty and cover [0, 4) contiguously.
    assert all(s < e for s, e in segs)
    flat = [i for s, e in segs for i in range(s, e)]
    assert flat == [0, 1, 2, 3]
    assert segs[0] == (0, 1)


def test_boundary_at_index_zero_does_not_create_empty_first_segment():
    """A /clear as entry 0 must not produce an empty (0, 0) range —
    the range simply starts there and the brief has one segment."""
    entries = [
        _user_str("/clear", "2026-05-07T10:00:00Z"),
        _user_str("after", "2026-05-07T10:01:00Z"),
    ]
    assert segment_transcript(entries) == [(0, 2)]


def test_segments_cover_every_entry_exactly_once():
    """Property: union of half-open ranges == [0, len(entries)) with
    no gaps and no overlaps."""
    entries = [
        _user_str("a", "2026-05-07T10:00:00Z"),
        _user_str("b", "2026-05-07T10:01:00Z"),
        _user_str("/clear", "2026-05-07T10:02:00Z"),
        _user_str("c", "2026-05-07T13:00:00Z"),  # also crosses idle gap
        _user_str("d", "2026-05-07T13:01:00Z"),
    ]
    segs = segment_transcript(entries)
    # Contiguous: starts[0]=0, every end == next start, last end == n.
    assert segs[0][0] == 0
    assert segs[-1][1] == len(entries)
    for (a, b), (c, d) in zip(segs, segs[1:]):
        assert b == c
