"""Behavioral tests for render_brief + render_assistant."""
from __future__ import annotations

from compaction.trim import render_assistant, render_brief


def _u(s: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": s}}


def _a(blocks: list[dict]) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _brief(*args, **kwargs) -> str:
    """Helper: invoke render_brief() and return its single string.
    The tier1/tier2 split was removed when the brief was simplified to
    just the trimmed conversation; render_brief() now returns one str."""
    return render_brief(*args, **kwargs)


def test_render_assistant_thinking_dropped():
    e = _a([{"type": "thinking", "thinking": "internal"}])
    assert render_assistant(e) is None


def test_render_assistant_text_kept():
    e = _a([{"type": "text", "text": "Hello world this is substantive"}])
    assert "Hello world" in render_assistant(e)


def test_render_assistant_read_marker_kept():
    """Read markers survive — they tell the reader which files were
    inspected. Other markers are dropped (see KEEP_MARKER_TOOLS)."""
    e = _a([{"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}}])
    assert render_assistant(e) == "[Read file_path=/x.py]"


def test_render_assistant_drops_non_read_markers():
    """Bash, Edit, Write, ToolSearch, ExitPlanMode, etc. produce empty
    output unless paired with substantive text — they're noise once the
    tool_result body is gone."""
    for name, inp in [
        ("Bash", {"command": "ls"}),
        ("Edit", {"file_path": "/x.py"}),
        ("Write", {"file_path": "/x.py"}),
        ("ToolSearch", {}),
        ("ExitPlanMode", {}),
        ("WebFetch", {"url": "https://example.com"}),
        ("Glob", {"pattern": "**/*.py"}),
    ]:
        e = _a([{"type": "tool_use", "name": name, "input": inp}])
        assert render_assistant(e) is None, f"{name} marker should be dropped"


def test_short_narration_dropped_alongside_read_tool_use():
    """Same-turn drop: short narration text + Read tool_use → marker only."""
    e = _a(
        [
            {"type": "text", "text": "Let me check"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
        ]
    )
    assert render_assistant(e) == "[Read file_path=/x.py]"


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
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
        ]
    )
    out = render_assistant(e)
    assert out == "[Read file_path=/x.py] ×3"


def test_render_assistant_dedup_preserves_distinct_markers():
    """Distinct adjacent markers are not merged."""
    e = _a(
        [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/y.py"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
        ]
    )
    out = render_assistant(e, seen_paths=set())
    # Second occurrence of /x.py collapses to basename (existing behavior).
    assert out == "[Read file_path=/x.py] [Read file_path=/y.py] [Read file_path=x.py]"


def test_render_brief_includes_subagent_findings_inline():
    """Sub-agent reports are spliced into the brief convo as `[Sub-agent
    report: ...]` blocks. Pinned to guarantee 39 KB of research findings
    don't get lost across /handoff."""
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
    assert "[Sub-agent report:" in out
    assert "Research CC compaction hooks" in out
    assert "general-purpose" in out
    assert "PreCompact hook exists" in out


def test_render_brief_subagent_dispatch_dropped_when_no_report():
    """Agent dispatch with no tool_result attached has nothing to surface
    in the brief (KEEP_MARKER_TOOLS = {Read})."""
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
    out = render_brief(entries, session_id="s", cwd="/c", archive_hash=None)
    # No bare/full Agent marker should leak through.
    assert "[Agent" not in out


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


def test_render_brief_keeps_user_msgs_verbatim():
    """User msgs land in the convo prose verbatim — including the most
    recent direction reversal. The brief is the trimmed convo, so any
    real user msg that survives the noise filter shows up as `U: ...`."""
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "U: Build a feature that does X" in brief
    assert "U: no, do Y instead" in brief


def test_render_brief_includes_anti_reread_header():
    """The anti-re-read notice is the load-bearing instruction for
    /handon-restored sessions. Must always appear."""
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "Authoritative record" in brief
    assert "do **not** re-Read" in brief


def test_render_brief_files_appear_via_read_marker():
    """File paths show up as `[Read file_path=...]` markers in the convo
    when the agent Read them."""
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


def test_render_brief_keeps_code_fence():
    """Code fences are preserved verbatim in the convo prose."""
    brief = _brief(_build_entries(), "s", "/c", "h")
    assert "def y():" in brief


def test_render_brief_archive_hash_referenced():
    brief = _brief(_build_entries(), "s", "/c", "deadbeef")
    assert "deadbeef" in brief


def test_render_brief_no_archive_omits_line():
    """When archive_hash is None, the brief simply omits the Archive
    line — no synthetic placeholder. Less noise."""
    brief = _brief(_build_entries(), "s", "/c", None)
    assert "Archive:" not in brief


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
    """File-history-snapshot entries must not appear in the brief (they
    bloat without adding signal — git already records this)."""
    entries = [
        _u("real user msg about deploying"),
        {"type": "file-history-snapshot", "snapshot": "x" * 5000},
        _a([{"type": "text", "text": "Sure, let me check git status."}]),
    ]
    out = render_brief(entries, session_id="s", cwd="/c", archive_hash="h")
    # The snapshot blob should not leak into the brief.
    assert "x" * 50 not in out
