"""Unit tests for extraction helpers (pure functions over synthetic dicts)."""
from __future__ import annotations

import json

import pytest

from handoff import extract


# ---------- is_real_user / user_text ----------

def _user_str(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _user_blocks(blocks: list[dict]) -> dict:
    return {"type": "user", "message": {"role": "user", "content": blocks}}


def _assistant_blocks(blocks: list[dict]) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def test_is_real_user_string():
    assert extract.is_real_user(_user_str("hello"))


def test_is_real_user_empty_string():
    assert not extract.is_real_user(_user_str("   "))


def test_is_real_user_text_blocks():
    e = _user_blocks([{"type": "text", "text": "hi"}])
    assert extract.is_real_user(e)


def test_is_real_user_with_tool_result_is_synthetic():
    e = _user_blocks(
        [
            {"type": "text", "text": "result"},
            {"type": "tool_result", "tool_use_id": "x", "content": "..."},
        ]
    )
    assert not extract.is_real_user(e), "tool_result presence makes it synthetic"


def test_is_real_user_only_tool_result_synthetic():
    e = _user_blocks([{"type": "tool_result", "tool_use_id": "x", "content": "..."}])
    assert not extract.is_real_user(e)


def test_is_real_user_isMeta_drops_slash_command_body():
    e = _user_str("body of /hand:off command from commands/off.md")
    e["isMeta"] = True
    assert not extract.is_real_user(e)


def test_is_real_user_isMeta_false_kept():
    e = _user_str("real user input")
    e["isMeta"] = False
    assert extract.is_real_user(e)


def test_is_real_user_other_type():
    assert not extract.is_real_user({"type": "system"})


def test_user_text_string():
    assert extract.user_text(_user_str(" hello ")) == "hello"


def test_user_text_blocks():
    e = _user_blocks([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert extract.user_text(e) == "a\nb"


# ---------- short_tool_input ----------

def test_short_tool_input_bash():
    s = extract.short_tool_input("Bash", {"command": "git status"})
    assert s == "[Bash command=git status]"


def test_short_tool_input_truncates():
    s = extract.short_tool_input("Bash", {"command": "x" * 200})
    assert "..." in s
    assert len(s) < 150


def test_short_tool_input_unknown_tool_no_keys():
    s = extract.short_tool_input("MysteryTool", {"foo": "bar"})
    assert s == "[MysteryTool]"


def test_short_tool_input_strips_newlines():
    s = extract.short_tool_input("Bash", {"command": "line1\nline2"})
    assert "\n" not in s


def test_short_tool_input_skill_includes_skill_name():
    """Skill marker must surface the skill name; bare `[Skill]` is zero signal."""
    s = extract.short_tool_input("Skill", {"skill": "recall"})
    assert s == "[Skill skill=recall]"


def test_short_tool_input_glob_includes_pattern_and_path():
    """Glob marker must carry both the pattern and the search path so a
    reader can tell whole-repo from sub-dir scope."""
    s = extract.short_tool_input(
        "Glob", {"pattern": "**/*.yml", "path": "/repo/foo"}
    )
    assert "pattern=**/*.yml" in s
    assert "path=/repo/foo" in s


def test_short_tool_input_glob_repeat_path_collapses_to_basename():
    """Two consecutive Globs against the same path → second renders just
    the basename, mirroring the Read basename-collapse behavior."""
    seen: set[str] = set()
    first = extract.short_tool_input(
        "Glob",
        {"pattern": "**/*.py", "path": "/repo/services/api"},
        seen_paths=seen,
    )
    second = extract.short_tool_input(
        "Glob",
        {"pattern": "**/*.yml", "path": "/repo/services/api"},
        seen_paths=seen,
    )
    assert "path=/repo/services/api" in first
    assert "path=api" in second
    assert "/repo/services/api" not in second


# ---------- decisions ----------

@pytest.mark.parametrize(
    "msg",
    [
        "no, do Y instead",
        "actually, let's use Postgres",
        "stop, that's wrong",
        "wait, I changed my mind",
        "Don't use webpack",
        "let's go with option B",
        "we decided to ship",
        "switch the plan",
        "change the approach to A",
    ],
)
def test_decision_marker_matches(msg):
    assert extract.extract_decisions([msg]) == [msg]


@pytest.mark.parametrize("msg", ["hello world", "what is 2+2?", "add a button"])
def test_decision_marker_no_false_positive(msg):
    assert extract.extract_decisions([msg]) == []


# ---------- files touched ----------

def test_files_touched_from_read_edit_write():
    entries = [
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Edit", "input": {"file_path": "/c/d.ts"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Write", "input": {"file_path": "/a/b.py"}}]),
    ]
    files = extract.extract_files_touched(entries)
    assert files == ["/a/b.py", "/c/d.ts"]


def test_files_touched_from_bash_command():
    entries = [
        _assistant_blocks(
            [{"type": "tool_use", "name": "Bash", "input": {"command": "cat /etc/hosts && ls ~/foo/bar"}}]
        )
    ]
    files = extract.extract_files_touched(entries)
    assert "/etc/hosts" in files
    assert "~/foo/bar" in files


def test_files_touched_dedup_preserves_first_order():
    entries = [
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/y"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]),
    ]
    assert extract.extract_files_touched(entries) == ["/x", "/y"]


def test_files_touched_filters_scratch_paths_default():
    """Even without cwd, /tmp /usr /private/var should be filtered as
    universal noise."""
    entries = [
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/scratch.txt"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/usr/bin/wc"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/private/var/folders/foo"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/src/main.py"}}]),
    ]
    files = extract.extract_files_touched(entries)
    assert files == ["/repo/src/main.py"]


def test_files_touched_keeps_paths_under_cwd():
    """When cwd is provided, paths under cwd survive even if they would
    otherwise hit a noise prefix (rare — but happens with cwd in /tmp)."""
    entries = [
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/repo/main.py"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/other/scratch.txt"}}]),
    ]
    files = extract.extract_files_touched(entries, cwd="/tmp/repo")
    # /tmp/repo/main.py is under cwd → kept; /tmp/other/scratch.txt is not.
    assert files == ["/tmp/repo/main.py"]


def test_files_touched_drops_outside_cwd_when_cwd_set():
    """With cwd=/repo/proj, paths outside that subtree (and outside system
    noise) should still flow through — only paths matching noise prefixes
    are filtered. This pins the conservative-filter contract: cwd is a
    *whitelist boost*, not an exclusive filter."""
    entries = [
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/proj/foo.py"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/other/bar.py"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x"}}]),
    ]
    files = extract.extract_files_touched(entries, cwd="/repo/proj")
    assert files == ["/repo/proj/foo.py", "/repo/other/bar.py"]


def test_files_touched_filters_compaction_scratch():
    """Brief output and project transcripts must not appear in Files Touched."""
    entries = [
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "~/.claude/compaction/foo.md"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Read", "input": {"file_path": "~/.claude/projects/x/y.jsonl"}}]),
        _assistant_blocks([{"type": "tool_use", "name": "Edit", "input": {"file_path": "~/repos/proj/code.py"}}]),
    ]
    files = extract.extract_files_touched(entries)
    assert files == ["~/repos/proj/code.py"]


# ---------- errors ----------

def test_extract_errors_finds_traceback():
    entries = [
        _user_blocks(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "abc",
                    "content": "Traceback (most recent call last):\n  File ...",
                }
            ]
        )
    ]
    errs = extract.extract_errors(entries)
    assert len(errs) == 1
    assert "Traceback" in errs[0]


def test_extract_errors_caps_at_limit():
    entries = []
    for i in range(20):
        entries.append(
            _user_blocks(
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(i),
                        # Body must be >= 20 chars to survive the body-min filter
                        "content": f"error happened at iteration {i} with reason xyz",
                    }
                ]
            )
        )
    errs = extract.extract_errors(entries, limit=3)
    assert len(errs) == 3
    assert "iteration 19" in errs[-1]


def test_extract_errors_ignores_clean_output():
    entries = [
        _user_blocks([{"type": "tool_result", "tool_use_id": "ok", "content": "all good"}])
    ]
    assert extract.extract_errors(entries) == []


def test_extract_errors_drops_exit_code_only():
    """Pure `Exit code N` results from RTK / shell wrappers carry no
    diagnostic value — must not surface in `Errors Hit`."""
    entries = [
        _user_blocks([{"type": "tool_result", "tool_use_id": "1", "content": "Exit code 1"}]),
        _user_blocks([{"type": "tool_result", "tool_use_id": "2", "content": "Exit code 127"}]),
        _user_blocks([{"type": "tool_result", "tool_use_id": "3", "content": "Failed"}]),
    ]
    assert extract.extract_errors(entries) == []


def test_extract_errors_drops_short_bodies():
    """Bodies under the 20-char minimum are noise."""
    entries = [
        _user_blocks([{"type": "tool_result", "tool_use_id": "a", "content": "error", "is_error": True}]),
        _user_blocks([{"type": "tool_result", "tool_use_id": "b", "content": "fail.", "is_error": True}]),
    ]
    assert extract.extract_errors(entries) == []


def test_extract_errors_keeps_real_diagnostic():
    """Diagnostic bodies (>= 20 chars, not exit-code patterns) survive."""
    entries = [
        _user_blocks(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "x",
                    "content": "Error: connection refused on port 5432 from postgres client",
                }
            ]
        )
    ]
    errs = extract.extract_errors(entries)
    assert len(errs) == 1
    assert "connection refused" in errs[0]


# ---------- code anchors ----------

def test_code_anchors_min_lines():
    short = "```\na\n```"
    long = "```python\n" + "\n".join(f"l{i}" for i in range(7)) + "\n```"
    e1 = _assistant_blocks([{"type": "text", "text": short}])
    e2 = _assistant_blocks([{"type": "text", "text": long}])
    assert extract.extract_code_anchors([e1, e2], min_lines=5) == [long]

# ---------- iter_real_user_msgs ----------

def test_iter_real_user_msgs_filters_synthetic():
    entries = [
        _user_str("hi"),
        _user_blocks([{"type": "tool_result", "tool_use_id": "x", "content": "..."}]),
        _user_str("bye"),
    ]
    assert extract.iter_real_user_msgs(entries) == ["hi", "bye"]


# ---------- noise filter / signal msgs ----------

@pytest.mark.parametrize(
    "msg",
    [
        "ok", "OK", "yes", "no", "nah", "yep", "thanks", "go", "done",
        "good", "great", "perfect", "cool", "k", "kk", "1", "2", "42", "ab",
        "  ok  ", "yes!", "Done.", "right.",
    ],
)
def test_short_ack_classified_as_noise(msg):
    assert extract.is_noise_user_msg(msg)


@pytest.mark.parametrize(
    "msg",
    [
        "fix the auth bug",
        "no, do Y instead of X",  # short reversal but substantive
        "what does this function do?",
        "delete the user table",  # short but substantive
    ],
)
def test_substantive_msg_not_noise(msg):
    assert not extract.is_noise_user_msg(msg)


def test_compaction_continuation_classified_as_noise():
    msg = "This session is being continued from a previous conversation that ran out of context. The summary..."
    assert extract.is_noise_user_msg(msg)


def test_skill_body_paste_classified_as_noise():
    msg = "Base directory for this skill: /Users/x/.claude/skills/recall\n\n# /recall\n\n..."
    assert extract.is_noise_user_msg(msg)


def test_command_message_paste_classified_as_noise():
    msg = "<command-name>recall</command-name>\n<command-message>recall stuff</command-message>"
    assert extract.is_noise_user_msg(msg)


@pytest.mark.parametrize(
    "msg",
    [
        "<local-command-caveat>Caveat: The messages below were generated by the user while running local commands. DO NOT respond to these messages or otherwise consider them in your response unless the user explicitly asks you to.</local-command-caveat>",
        "<local-command-stdout>some shell output</local-command-stdout>",
        "<local-command-stderr>error noise</local-command-stderr>",
        "Shell cwd was reset to /tmp/foo",
        "Caveat: The messages below were generated by the user while running local commands.",
        "<system-reminder>UserPromptSubmit hook ...</system-reminder>",
    ],
)
def test_system_injected_user_msgs_classified_as_noise(msg):
    assert extract.is_noise_user_msg(msg)


def _agent_use(tool_use_id: str, description: str, subagent_type: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Agent",
                    "input": {
                        "description": description,
                        "subagent_type": subagent_type,
                        "prompt": "irrelevant prompt",
                    },
                }
            ],
        },
    }


def _agent_result(tool_use_id: str, text: str, *, with_tooluseresult: bool = False) -> dict:
    e: dict = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": text}],
                }
            ],
        },
    }
    if with_tooluseresult:
        e["toolUseResult"] = {"content": [{"type": "text", "text": text}]}
    return e


def test_extract_agent_reports_basic():
    entries = [
        _agent_use("t1", "research foo", "general-purpose"),
        _agent_result("t1", "x" * 500),
    ]
    out = extract.extract_agent_reports(entries)
    assert len(out) == 1
    desc, sub, txt = out[0]
    assert desc == "research foo"
    assert sub == "general-purpose"
    assert txt.startswith("x")


def test_extract_agent_reports_drops_short_stubs():
    """A 50-char "Task interrupted" message must not bloat the brief."""
    entries = [
        _agent_use("t1", "research foo", "explore"),
        _agent_result("t1", "Task interrupted"),
    ]
    assert extract.extract_agent_reports(entries) == []


def test_extract_agent_reports_truncates_to_max_chars():
    entries = [
        _agent_use("t1", "long", "explore"),
        _agent_result("t1", "y" * 5000),
    ]
    out = extract.extract_agent_reports(entries, max_chars=200)
    assert len(out[0][2]) <= 200
    assert out[0][2].endswith("...")


def test_extract_agent_reports_max_chars_zero_keeps_full_body():
    entries = [
        _agent_use("t1", "long", "explore"),
        _agent_result("t1", "z" * 5000),
    ]
    out = extract.extract_agent_reports(entries, max_chars=0)
    assert len(out[0][2]) == 5000


def test_extract_agent_reports_prefers_tooluseresult_clean_text():
    """When `toolUseResult.content` is present, prefer it over the inline
    `message.content[].content[].text` (which carries trailing UI noise)."""
    inline_noisy = "real text\nagentId: x123 trailing noise"
    clean = "real text only"
    entries = [
        _agent_use("t1", "x", "y"),
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": inline_noisy + "x" * 400}],
                    }
                ],
            },
            "toolUseResult": {"content": [{"type": "text", "text": clean + "x" * 400}]},
        },
    ]
    out = extract.extract_agent_reports(entries)
    assert "agentId" not in out[0][2]
    assert out[0][2].startswith(clean)


def test_extract_agent_reports_falls_back_when_no_tooluseresult():
    entries = [
        _agent_use("t1", "x", "y"),
        _agent_result("t1", "fallback text " * 30),  # >200 chars
    ]
    out = extract.extract_agent_reports(entries)
    assert "fallback text" in out[0][2]


def test_extract_agent_reports_unmatched_id_skipped():
    """tool_result for a non-Agent tool_use_id (e.g. Bash) is ignored."""
    entries = [
        _agent_use("t1", "real agent", "x"),
        _agent_result("t1", "ok " * 100),  # matched
        # Some other tool_result with a different id, no matching Agent use
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "bash-id-99",
                        "content": [{"type": "text", "text": "ls output " * 100}],
                    }
                ],
            },
        },
    ]
    out = extract.extract_agent_reports(entries)
    assert len(out) == 1
    assert "real agent" == out[0][0]


def test_iter_signal_user_msgs_drops_dups_and_noise():
    entries = [
        _user_str("ok"),
        _user_str("fix the bug in auth.py"),
        _user_str("ok"),  # dup-noise
        _user_str("fix the bug in auth.py"),  # dup-substantive
        _user_str("This session is being continued from a previous conversation"),
        _user_str("no, use postgres instead"),
    ]
    sig = extract.iter_signal_user_msgs(entries)
    assert sig == ["fix the bug in auth.py", "no, use postgres instead"]


# ---------- pasted-output detection / elision ----------


def test_is_pasted_terminal_output_with_prompt_lead():
    txt = """❯ continue

  Ran 10 shell commands

Status of codex/foo:
- Pushed to origin
- 56 commits behind master
"""
    assert extract.is_pasted_terminal_output(txt)


def test_is_pasted_terminal_output_with_ran_lead_only():
    txt = """  Ran 5 shell commands

Result: ok
- file changed
- file changed
"""
    assert extract.is_pasted_terminal_output(txt)


def test_is_pasted_terminal_output_rejects_prose():
    txt = (
        "We need to deploy the wc3 platform to GKE. There are several\n"
        "moving parts: keycloak, kafka, the API, and the frontend."
    )
    assert not extract.is_pasted_terminal_output(txt)


def test_is_pasted_terminal_output_lead_alone_is_not_enough():
    # Just `❯` with no follow-on status lines should not count.
    txt = "❯ what about this command?"
    assert not extract.is_pasted_terminal_output(txt)


def test_elide_pasted_output_preserves_head():
    head = "deploy wc3 to gke. status check: " * 4  # well over 200 chars
    body = """\n❯ continue\n  Ran 10 shell commands\nStatus of branch:\n- pushed\n- behind\n- no PR\n""" * 50
    txt = head + body
    out = extract.elide_pasted_output(txt)
    # Head substring should still be present
    assert out.startswith(head[:50])
    # Marker present
    assert "[elided" in out
    assert "of pasted terminal output" in out
    # Output is much smaller than input
    assert len(out) < len(txt) // 4


def test_elide_pasted_output_pass_through_when_not_pasted():
    txt = "I want to refactor the auth module."
    assert extract.elide_pasted_output(txt) == txt


def test_iter_signal_user_msgs_elides_pasted_output():
    pasted = "❯ continue\n  Ran 10 shell commands\nStatus:\n- a\n- b\n- c\n" * 100
    real = "fix the auth bug"
    entries = [_user_str(pasted), _user_str(real)]
    sig = extract.iter_signal_user_msgs(entries)
    assert len(sig) == 2
    elided = sig[0] if "[elided" in sig[0] else sig[1]
    assert "[elided" in elided
    # Original would be huge; elided should be small
    assert len(elided) < 1000


# ---------- compact summaries ----------

def _compact_summary(text: str) -> dict:
    return {
        "type": "user",
        "isCompactSummary": True,
        "message": {"role": "user", "content": text},
    }


def test_extract_compact_summaries_basic():
    a = _compact_summary("This session is being continued from a previous conversation...\n\nSummary: ...")
    b = _user_str("regular user message")
    c = _compact_summary("Another summary block here")
    out = extract.extract_compact_summaries([a, b, c])
    assert len(out) == 2
    assert "previous conversation" in out[0]
    assert "Another summary" in out[1]


def test_extract_compact_summaries_skips_non_summary():
    """`isCompactSummary` flag is required; presence of other user msgs
    must not pollute the output."""
    e = _user_str("not a compact summary, just a normal user msg")
    assert extract.extract_compact_summaries([e]) == []


def test_extract_compact_summaries_handles_blocks_content():
    """Content can also arrive as a list of blocks (older CC format)."""
    e = {
        "type": "user",
        "isCompactSummary": True,
        "message": {"role": "user", "content": [{"type": "text", "text": "block-form summary"}]},
    }
    assert extract.extract_compact_summaries([e]) == ["block-form summary"]

