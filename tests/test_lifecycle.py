"""Tests for `handoff.lifecycle` — status detector + frontmatter helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from handoff.lifecycle import (
    FRONTMATTER_KEYS,
    RECAP_MAX_CHARS,
    STALE_DAYS_DEFAULT,
    detect_status,
    extract_recap,
    is_stale,
    mark_stale,
    parse_frontmatter,
    read_existing_brief,
    render_frontmatter,
    resolve_frontmatter,
    sanitize_recap,
    strip_frontmatter,
)


# ---------------------------------------------------------------------------
# Mini-entry builders. Real transcripts have far more fields, but the
# detector only looks at `type`, `message.content`, `isMeta`, and the
# `tool_use` blocks. Keep the test scaffolding minimal.
# ---------------------------------------------------------------------------

def U(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def A_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def A_todowrite(statuses: list[str]) -> dict:
    todos = [{"status": s, "content": "x"} for s in statuses]
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}}],
        },
    }


# ---------------------------------------------------------------------------
# detect_status — signal precedence
# ---------------------------------------------------------------------------

class TestDetectStatus:
    def test_todowrite_all_complete_wins(self):
        entries = [
            U("start the refactor"),
            A_todowrite(["pending", "pending"]),
            A_text("working on it"),
            A_todowrite(["completed", "completed"]),
            U("anything else you need?"),  # would otherwise be `pending`
        ]
        assert detect_status(entries) == ("done", "auto-todowrite")

    def test_todowrite_with_pending_falls_through(self):
        entries = [
            U("start"),
            A_todowrite(["completed", "in_progress"]),
        ]
        # No user-msg signal, no question → in_progress
        assert detect_status(entries) == ("in_progress", "auto-default")

    def test_user_msg_done_keyword(self):
        for kw in ["thanks", "lgtm", "merged", "shipped", "all good", "fixed", "perfect"]:
            entries = [U("do the thing"), A_text("done"), U(kw)]
            status, sig = detect_status(entries)
            assert status == "done", f"keyword {kw!r}"
            assert sig == "auto-user-msg"

    def test_user_msg_done_within_last_3(self):
        # "thanks" is 3rd-to-last but still counted
        entries = [
            U("do the thing"),
            A_text("ok"),
            U("thanks"),
            A_text("you bet"),
            U("one more — what about edge case X"),
            A_text("here's how"),
            U("got it"),
        ]
        assert detect_status(entries)[0] == "done"

    def test_open_question_pending(self):
        entries = [U("what's the right way to handle Y?")]
        assert detect_status(entries) == ("pending", "auto-open-q")

    def test_question_prefix_without_question_mark(self):
        # "why does X fail" — interrogative prefix, no terminal ?
        entries = [U("why does the build fail on master")]
        assert detect_status(entries) == ("pending", "auto-open-q")

    def test_default_in_progress(self):
        entries = [
            U("here is some context"),
            A_text("noted"),
            U("now do step 1"),
        ]
        assert detect_status(entries) == ("in_progress", "auto-default")

    def test_empty_transcript(self):
        assert detect_status([]) == ("in_progress", "auto-default")

    def test_done_keyword_beats_question(self):
        # Last msg is a question, but the 2nd-to-last is "thanks". Order in
        # the last-3 window: both are matched; done regex hits first signal.
        entries = [
            U("ok thanks for that"),
            A_text("welcome"),
            U("what's next?"),
        ]
        # Question signal would mark pending, but completion signal has
        # higher precedence (rule 2 before rule 3).
        assert detect_status(entries)[0] == "done"

    def test_isolated_word_done(self):
        # Bare "done" as a user reply marks the session done.
        entries = [U("run the tests"), A_text("passing"), U("done")]
        assert detect_status(entries) == ("done", "auto-user-msg")

    def test_skill_body_with_done_word_ignored(self):
        # Live-session regression (2026-06-04): /hand:off skill body contains
        # the literal word "done" — injected as a pseudo-user msg, it must
        # NOT mark the session done.
        skill_body = (
            "<command-message>hand:off</command-message>\n"
            "<command-name>/hand:off</command-name>\n"
            "Detector marked this session DONE — hidden from picker.\n"
        )
        entries = [U("keep working on the feature"), A_text("ok"), U(skill_body)]
        assert detect_status(entries) == ("in_progress", "auto-default")

    def test_long_msg_with_done_keyword_not_done(self):
        # Live regression (2026-06-04): opening request "i done it manually
        # copying…" (280 chars) false-done'd the session. Keywords embedded
        # in long task descriptions are content, not completion signals.
        long_msg = (
            "I'm thinking that on handoff we should store the log with short "
            "description of the session. i done it manually copying it from a "
            "session with recap generated by a claude. let's think what can we do"
        )
        entries = [U(long_msg), A_text("exploring")]
        assert detect_status(entries) == ("in_progress", "auto-default")

    def test_terse_done_msg_still_counts(self):
        entries = [U("do the thing"), A_text("ok"), U("merged, thanks!")]
        assert detect_status(entries) == ("done", "auto-user-msg")

    def test_skill_body_question_not_pending(self):
        # Injected body ending with "?" must not flip the session to pending.
        body = "<command-name>/foo</command-name>\nWhat does this do?"
        entries = [U("implement the thing"), U(body)]
        assert detect_status(entries) == ("in_progress", "auto-default")


# ---------------------------------------------------------------------------
# Frontmatter round-trip
# ---------------------------------------------------------------------------

class TestFrontmatter:
    def test_parse_render_roundtrip(self):
        fm = {
            "status": "in_progress",
            "session_id": "abc-123",
            "cwd": "/tmp/x",
            "created": "2026-05-25T10:00:00Z",
            "last_resumed": None,
            "completion_signal": "auto-default",
            "archive_hash": "deadbeef",
        }
        text = render_frontmatter(fm) + "# body\n"
        parsed = parse_frontmatter(text)
        for k in FRONTMATTER_KEYS:
            assert parsed.get(k) == fm.get(k), f"key {k}"

    def test_parse_missing_returns_empty(self):
        assert parse_frontmatter("# just a body\n") == {}

    def test_strip_frontmatter(self):
        text = render_frontmatter({"status": "done"}) + "# body\n"
        assert strip_frontmatter(text) == "# body\n"

    def test_strip_no_frontmatter_passthrough(self):
        assert strip_frontmatter("plain body") == "plain body"

    def test_null_literal_parsed_as_none(self):
        text = "---\nstatus: pending\nlast_resumed: null\n---\nbody\n"
        parsed = parse_frontmatter(text)
        assert parsed["status"] == "pending"
        assert parsed["last_resumed"] is None

    def test_render_preserves_unknown_keys(self):
        fm = {"status": "done", "custom_key": "x"}
        text = render_frontmatter(fm)
        assert "custom_key: x" in text

    def test_render_known_keys_in_order(self):
        text = render_frontmatter({"status": "done"})
        lines = [l for l in text.splitlines() if ":" in l]
        keys = [l.split(":", 1)[0] for l in lines]
        assert keys == list(FRONTMATTER_KEYS)

    def test_read_existing_brief_missing_file(self, tmp_path: Path):
        assert read_existing_brief(tmp_path / "nope.md") == {}

    def test_read_existing_brief(self, tmp_path: Path):
        p = tmp_path / "b.md"
        p.write_text("---\nstatus: done\ncreated: 2026-01-01T00:00:00Z\n---\nbody\n")
        fm = read_existing_brief(p)
        assert fm["status"] == "done"
        assert fm["created"] == "2026-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# resolve_frontmatter — merge detector output with existing brief
# ---------------------------------------------------------------------------

class TestResolveFrontmatter:
    BASE = dict(
        session_id="sid",
        cwd="/tmp",
        archive_hash="hash",
        detected_status="in_progress",
        detected_signal="auto-default",
    )

    def test_fresh_brief_uses_detector(self):
        fm = resolve_frontmatter(**self.BASE, existing={})  # type: ignore[arg-type]
        assert fm["status"] == "in_progress"
        assert fm["completion_signal"] == "auto-default"
        assert fm["created"] is not None
        assert fm["last_resumed"] is None

    def test_preserves_existing_created(self):
        existing = {"created": "2026-01-01T00:00:00Z", "last_resumed": "2026-01-02T00:00:00Z"}
        fm = resolve_frontmatter(**self.BASE, existing=existing)  # type: ignore[arg-type]
        assert fm["created"] == "2026-01-01T00:00:00Z"
        assert fm["last_resumed"] == "2026-01-02T00:00:00Z"

    def test_manual_done_wins_over_detector(self):
        existing = {"status": "done", "completion_signal": "manual"}
        fm = resolve_frontmatter(**self.BASE, existing=existing)  # type: ignore[arg-type]
        assert fm["status"] == "done"
        assert fm["completion_signal"] == "manual"

    def test_auto_done_is_overridable_by_detector(self):
        # Previous run auto-detected done; current run detects in_progress.
        # Auto-done is NOT sticky (only manual is) — detector wins.
        existing = {"status": "done", "completion_signal": "auto-user-msg"}
        fm = resolve_frontmatter(**self.BASE, existing=existing)  # type: ignore[arg-type]
        assert fm["status"] == "in_progress"
        assert fm["completion_signal"] == "auto-default"

    def test_archive_hash_always_fresh(self):
        existing = {"archive_hash": "old"}
        fm = resolve_frontmatter(**self.BASE, existing=existing)  # type: ignore[arg-type]
        assert fm["archive_hash"] == "hash"


# ---------------------------------------------------------------------------
# recap — sanitize / extract / resolve precedence
# ---------------------------------------------------------------------------

class TestSanitizeRecap:
    def test_none_and_empty(self):
        assert sanitize_recap(None) is None
        assert sanitize_recap("") is None
        assert sanitize_recap("   \n\t ") is None

    def test_collapses_newlines_and_runs(self):
        assert sanitize_recap("Goal: x.\n  Next:\t y.") == "Goal: x. Next: y."

    def test_caps_length(self):
        out = sanitize_recap("a" * (RECAP_MAX_CHARS * 2))
        assert out is not None
        assert len(out) <= RECAP_MAX_CHARS
        assert out.endswith("…")

    def test_short_passthrough(self):
        assert sanitize_recap("Goal: ship PR #42.") == "Goal: ship PR #42."


class TestExtractRecap:
    def test_empty_transcript(self):
        assert extract_recap([]) is None

    def test_goal_only(self):
        entries = [U("cut portal latency"), A_text("on it")]
        assert extract_recap(entries) == "cut portal latency"

    def test_goal_plus_open_todo(self):
        todos = [
            {"status": "completed", "content": "profile cold path"},
            {"status": "in_progress", "content": "merge PR #2511"},
        ]
        entries = [
            U("cut portal latency"),
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "TodoWrite", "input": {"todos": todos}}],
                },
            },
        ]
        assert extract_recap(entries) == "cut portal latency | next: merge PR #2511"

    def test_all_todos_complete_no_next(self):
        entries = [U("do the thing"), A_todowrite(["completed", "completed"])]
        assert extract_recap(entries) == "do the thing"

    def test_skips_injected_command_body(self):
        # Live regression (2026-06-04): first "user" msg was a /clear command
        # wrapper → garbage recap in sessions.log.md.
        entries = [
            U("<command-name>/clear</command-name> <command-message>clear</command-message>"),
            U("ok"),  # short ack — also not a goal
            U("cut portal latency"),
        ]
        assert extract_recap(entries) == "cut portal latency"

    def test_only_noise_msgs_no_recap(self):
        entries = [U("<command-name>/clear</command-name>"), U("ok")]
        assert extract_recap(entries) is None


class TestResolveRecap:
    BASE = dict(
        session_id="sid",
        cwd="/tmp",
        archive_hash="hash",
        detected_status="in_progress",
        detected_signal="auto-default",
    )

    def test_llm_arg_wins(self):
        existing = {"recap": "old extracted", "recap_source": "extracted"}
        fm = resolve_frontmatter(**self.BASE, existing=existing, recap="Goal: x. Next: y.")  # type: ignore[arg-type]
        assert fm["recap"] == "Goal: x. Next: y."
        assert fm["recap_source"] == "llm"

    def test_llm_arg_beats_existing_llm(self):
        existing = {"recap": "old llm", "recap_source": "llm"}
        fm = resolve_frontmatter(**self.BASE, existing=existing, recap="new llm")  # type: ignore[arg-type]
        assert fm["recap"] == "new llm"

    def test_existing_llm_not_downgraded_by_extracted(self):
        existing = {"recap": "good llm recap", "recap_source": "llm"}
        fm = resolve_frontmatter(
            **self.BASE, existing=existing, extracted_recap="weak fallback"  # type: ignore[arg-type]
        )
        assert fm["recap"] == "good llm recap"
        assert fm["recap_source"] == "llm"

    def test_extracted_used_when_nothing_else(self):
        fm = resolve_frontmatter(**self.BASE, existing={}, extracted_recap="goal | next: x")  # type: ignore[arg-type]
        assert fm["recap"] == "goal | next: x"
        assert fm["recap_source"] == "extracted"

    def test_existing_extracted_preserved_without_fresh(self):
        existing = {"recap": "old extracted", "recap_source": "extracted"}
        fm = resolve_frontmatter(**self.BASE, existing=existing)  # type: ignore[arg-type]
        assert fm["recap"] == "old extracted"
        assert fm["recap_source"] == "extracted"

    def test_no_recap_anywhere(self):
        fm = resolve_frontmatter(**self.BASE, existing={})  # type: ignore[arg-type]
        assert fm["recap"] is None
        assert fm["recap_source"] is None

    def test_llm_recap_sanitized(self):
        fm = resolve_frontmatter(**self.BASE, existing={}, recap="line1\nline2")  # type: ignore[arg-type]
        assert fm["recap"] == "line1 line2"

    def test_fresh_title_wins(self):
        existing = {"title": "old title"}
        fm = resolve_frontmatter(**self.BASE, existing=existing, title="new title")  # type: ignore[arg-type]
        assert fm["title"] == "new title"

    def test_existing_title_preserved_when_no_fresh(self):
        existing = {"title": "old title"}
        fm = resolve_frontmatter(**self.BASE, existing=existing)  # type: ignore[arg-type]
        assert fm["title"] == "old title"

    def test_no_title_anywhere(self):
        fm = resolve_frontmatter(**self.BASE, existing={})  # type: ignore[arg-type]
        assert fm["title"] is None


# ---------------------------------------------------------------------------
# is_stale / mark_stale
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _fm(status: str, *, created: datetime | None = None, last_resumed: datetime | None = None,
        signal: str = "auto-default") -> dict:
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None
    return {
        "status": status,
        "session_id": "x",
        "cwd": "/x",
        "created": iso(created),
        "last_resumed": iso(last_resumed),
        "completion_signal": signal,
        "archive_hash": None,
    }


class TestStale:
    def test_fresh_in_progress_not_stale(self):
        fm = _fm("in_progress", created=NOW - timedelta(days=2))
        assert not is_stale(fm, now=NOW)

    def test_old_in_progress_no_resume_is_stale(self):
        fm = _fm("in_progress", created=NOW - timedelta(days=20))
        assert is_stale(fm, now=NOW)

    def test_old_pending_is_stale(self):
        fm = _fm("pending", created=NOW - timedelta(days=20))
        assert is_stale(fm, now=NOW)

    def test_recent_resume_keeps_old_brief_fresh(self):
        # Created 30d ago, but resumed 5d ago → not stale.
        fm = _fm(
            "in_progress",
            created=NOW - timedelta(days=30),
            last_resumed=NOW - timedelta(days=5),
        )
        assert not is_stale(fm, now=NOW)

    def test_old_resume_still_stale(self):
        # Resumed 30d ago → stale even though it was touched once.
        fm = _fm(
            "in_progress",
            created=NOW - timedelta(days=60),
            last_resumed=NOW - timedelta(days=30),
        )
        assert is_stale(fm, now=NOW)

    def test_done_never_stale(self):
        fm = _fm("done", created=NOW - timedelta(days=365))
        assert not is_stale(fm, now=NOW)

    def test_threshold_boundary_inclusive(self):
        # Exactly N days old → stale (>= comparison).
        fm = _fm("in_progress", created=NOW - timedelta(days=STALE_DAYS_DEFAULT))
        assert is_stale(fm, now=NOW)

    def test_threshold_just_under_not_stale(self):
        fm = _fm("in_progress",
                 created=NOW - timedelta(days=STALE_DAYS_DEFAULT - 1, hours=23))
        assert not is_stale(fm, now=NOW)

    def test_custom_days_threshold(self):
        fm = _fm("in_progress", created=NOW - timedelta(days=20))
        assert is_stale(fm, now=NOW, days=10)
        assert not is_stale(fm, now=NOW, days=30)

    def test_missing_created_no_decision(self):
        fm = _fm("in_progress")  # both created + last_resumed None
        assert not is_stale(fm, now=NOW)

    def test_garbage_iso_no_decision(self):
        fm = _fm("in_progress")
        fm["created"] = "not a date"
        assert not is_stale(fm, now=NOW)

    def test_mark_stale_returns_done(self):
        fm = _fm("in_progress", created=NOW - timedelta(days=20))
        new = mark_stale(fm)
        assert new["status"] == "done"
        assert new["completion_signal"] == "auto-stale"
        # Original untouched
        assert fm["status"] == "in_progress"

    def test_mark_stale_preserves_other_fields(self):
        fm = _fm("in_progress", created=NOW - timedelta(days=20))
        fm["archive_hash"] = "deadbeef"
        new = mark_stale(fm)
        assert new["archive_hash"] == "deadbeef"
        assert new["session_id"] == "x"
