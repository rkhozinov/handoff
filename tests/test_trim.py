"""Behavioral tests for render_brief + render_assistant."""
from __future__ import annotations

from compaction.trim import render_assistant, render_brief


def _u(s: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": s}}


def _a(blocks: list[dict]) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _brief(*args, **kwargs) -> str:
    """Helper: join tier1 + tier2 into a single string for substring asserts.
    Production code uses the tuple form via render_brief()."""
    tier1, tier2 = render_brief(*args, **kwargs)
    return tier1 + "\n" + tier2


def test_render_assistant_thinking_dropped():
    e = _a([{"type": "thinking", "thinking": "internal"}])
    assert render_assistant(e) is None


def test_render_assistant_text_kept():
    e = _a([{"type": "text", "text": "Hello world this is substantive"}])
    assert "Hello world" in render_assistant(e)


def test_render_assistant_tool_use_marker():
    e = _a([{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}])
    assert render_assistant(e) == "[Bash command=ls]"


def test_short_narration_dropped_alongside_tool_use():
    """Same-turn drop: short text + tool_use → tool_use only."""
    e = _a(
        [
            {"type": "text", "text": "Let me check"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]
    )
    assert render_assistant(e) == "[Bash command=ls]"


def test_long_text_kept_even_with_tool_use():
    long = "x" * 200
    e = _a(
        [
            {"type": "text", "text": long},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]
    )
    assert long in render_assistant(e)


def test_narration_regex_drops_text_only_turn():
    """Text-only turn matching narration regex → dropped entirely."""
    e = _a([{"type": "text", "text": "Reading the file now"}])
    assert render_assistant(e) is None


def test_substantive_text_only_turn_kept():
    """Text-only turn that is NOT narration → kept verbatim."""
    e = _a([{"type": "text", "text": "The bug is in the auth middleware."}])
    assert render_assistant(e) == "The bug is in the auth middleware."


def test_adjacent_drop_short_text_before_tool_only_turn():
    """Short text-only turn followed by tool-only assistant turn → dropped."""
    cur = _a([{"type": "text", "text": "Now searching"}])
    nxt = _a([{"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}}])
    assert render_assistant(cur, nxt) is None


def test_render_assistant_dedups_repeated_tool_markers_in_turn():
    """Within a single assistant turn, identical adjacent tool markers
    collapse to `[marker] ×N`."""
    e = _a(
        [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]
    )
    out = render_assistant(e)
    assert out == "[Bash command=ls] ×3"


def test_render_assistant_dedup_preserves_distinct_markers():
    """Distinct adjacent markers are not merged."""
    e = _a(
        [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]
    )
    out = render_assistant(e)
    assert out == "[Bash command=ls] [Bash command=pwd] [Bash command=ls]"


def test_hard_truncate_keeps_brief_under_budget():
    """If a brief overflows even after progressive squeeze, hard-truncate
    must clamp it so the SessionStart hook's 25 KB cap can't reject it."""
    from compaction.trim import _hard_truncate_bytes

    big = ("line of text\n" * 5000)
    out = _hard_truncate_bytes(big, 1000, suffix="\n[...trunc]\n")
    assert len(out.encode("utf-8")) <= 1000
    assert out.endswith("[...trunc]\n")


def test_hard_truncate_passthrough_when_under_budget():
    from compaction.trim import _hard_truncate_bytes

    small = "small"
    assert _hard_truncate_bytes(small, 1000, suffix="\n[...]\n") == small


def test_render_brief_includes_subagent_findings_section():
    """Brief must surface synthesized sub-agent reports — the bug we just hit
    where 39 KB of research findings were lost across /handoff."""
    entries = [
        _u("research compaction tools"),
        _a(
            [
                {
                    "type": "tool_use",
                    "id": "t-agent-1",
                    "name": "Agent",
                    "input": {
                        "description": "Research CC compaction hooks",
                        "subagent_type": "general-purpose",
                        "prompt": "investigate",
                    },
                }
            ]
        ),
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t-agent-1",
                        "content": [
                            {
                                "type": "text",
                                "text": "PreCompact hook exists. SessionStart `compact` matcher fires post-compaction. autoCompactEnabled flag. " * 6,
                            }
                        ],
                    }
                ],
            },
        },
    ]
    out = _brief(entries, session_id="s", cwd="/c", archive_hash=None)
    assert "## Sub-Agent Findings" in out
    assert "Research CC compaction hooks" in out
    assert "general-purpose" in out
    assert "PreCompact hook exists" in out


def test_render_brief_subagent_marker_shows_description():
    """Tier1 + tier2 should render `[Agent description=... subagent_type=...]`,
    not bare `[Agent]`."""
    entries = [
        _a(
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Agent",
                    "input": {
                        "description": "Probe redis pool",
                        "subagent_type": "Explore",
                    },
                }
            ]
        ),
    ]
    tier1, tier2 = render_brief(entries, session_id="s", cwd="/c", archive_hash=None)
    combined = tier1 + tier2
    assert "[Agent" in combined
    # Description and subagent_type both surface in the marker.
    assert "Probe redis pool" in combined
    assert "Explore" in combined


def test_hard_truncate_handles_multibyte_boundary():
    """Cut must not split a UTF-8 multibyte codepoint."""
    from compaction.trim import _hard_truncate_bytes

    s = "héllo " * 500
    out = _hard_truncate_bytes(s, 200, suffix="\n[...]\n")
    # Round-trip encode/decode must succeed (no broken codepoints).
    out.encode("utf-8").decode("utf-8")
    assert len(out.encode("utf-8")) <= 200


# ---------- render_brief end-to-end ----------

def _build_entries():
    return [
        _u("Build a feature that does X"),
        _a(
            [
                {"type": "thinking", "thinking": "internal noise"},
                {"type": "text", "text": "Let me check the file"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/proj/main.py"}},
            ]
        ),
        # tool_result wrap (synthetic user)
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "lots of code lines"}
                ],
            },
        },
        _u("no, do Y instead"),
        _a(
            [
                {
                    "type": "text",
                    "text": "Got it. Switching approach.\n\n```python\ndef y():\n    return 1\n    pass\n    raise\n    return\n```",
                }
            ]
        ),
        _a([{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "implement Y"}}]),
        _a(
            [
                {
                    "type": "tool_use",
                    "name": "TaskUpdate",
                    "input": {"taskId": "1", "status": "completed"},
                }
            ]
        ),
    ]


def test_render_brief_includes_active_goal():
    brief = _brief(_build_entries(), "sess123", "/proj", archive_hash="HASH")
    assert "no, do Y instead" in brief, "active goal = last real user msg"


def test_render_brief_includes_decisions_section():
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "## Decisions / Direction Reversals" in brief
    assert "no, do Y instead" in brief.split("## Decisions")[1].split("##")[0]


def test_render_brief_files_touched_listed():
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "/proj/main.py" in brief


def test_render_brief_drops_tool_result_bodies():
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "lots of code lines" not in brief, "tool_result body must NOT survive"


def test_render_brief_keeps_thinking_out():
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "internal noise" not in brief


def test_render_brief_drops_short_narration():
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "Let me check the file" not in brief, "narration adjacent to tool_use must be dropped"


def test_render_brief_includes_code_anchor():
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "## Code Anchors" in brief
    assert "def y():" in brief


def test_render_brief_includes_todo_snapshot():
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "## Open TodoList" in brief
    assert "completed" in brief


def test_render_brief_archive_hash_referenced():
    brief = _brief(_build_entries(), "s", "/c", "deadbeef")
    assert "deadbeef" in brief


def test_render_brief_no_archive_falls_back():
    brief = _brief(_build_entries(), "s", "/c", None)
    assert "NOT STORED" in brief


def test_render_brief_includes_session_metadata():
    brief = _brief(_build_entries(), "S-X-Y", "/cwd", "h")
    assert "S-X-Y" in brief
    assert "/cwd" in brief


def test_render_brief_signal_user_msgs_verbatim():
    """Critical invariant: every signal user msg present verbatim in brief.
    (Noise like short acks or skill body re-pastes is intentionally filtered.)"""
    entries = _build_entries()
    brief = _brief(entries, "s", "/c", "h")
    from compaction.extract import iter_signal_user_msgs
    for m in iter_signal_user_msgs(entries):
        assert m in brief, f"signal user msg lost: {m!r}"


def test_render_assistant_caps_long_text():
    """A single very long assistant text turn should be truncated with a marker
    so tier2 doesn't carry 6+ KB of one reasoning block."""
    long_text = "x" * 10_000
    e = _a([{"type": "text", "text": long_text}])
    out = render_assistant(e)
    assert out is not None
    # Cap is 4_000 chars; output should be smaller than input plus marker.
    assert len(out) < 5_000
    assert "[elided" in out
    assert "memory doc" in out


def test_render_assistant_short_text_not_capped():
    e = _a([{"type": "text", "text": "Short substantive analysis result."}])
    out = render_assistant(e)
    assert out == "Short substantive analysis result."


def test_render_brief_drops_file_history_snapshot():
    """File-history-snapshot entries must not appear in tier2 (they bloat
    without adding signal — git already records this)."""
    entries = [
        _u("real user msg about deploying"),
        {"type": "file-history-snapshot", "snapshot": "x" * 5000},
        _a([{"type": "text", "text": "Sure, let me check git status."}]),
    ]
    tier1, tier2 = render_brief(entries, session_id="s", cwd="/c", archive_hash="h")
    # The snapshot blob should not leak into tier2.
    assert "x" * 50 not in tier2
