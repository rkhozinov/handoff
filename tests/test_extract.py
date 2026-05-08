"""Unit tests for extraction helpers (pure functions over synthetic dicts)."""
from __future__ import annotations

import json

import pytest

from compaction import extract


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
    e = _user_str("body of /handoff command from commands/handoff.md")
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


# ---------- todos ----------

def test_todo_snapshot_picks_last():
    entries = [
        _assistant_blocks(
            [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "first"}}]
        ),
        _assistant_blocks(
            [{"type": "tool_use", "name": "TaskUpdate", "input": {"taskId": "1", "status": "completed"}}]
        ),
    ]
    snap = extract.extract_todo_snapshot(entries)
    assert snap is not None
    assert json.loads(snap) == {"taskId": "1", "status": "completed"}


def test_todo_snapshot_none_when_no_task_calls():
    entries = [_assistant_blocks([{"type": "text", "text": "hi"}])]
    assert extract.extract_todo_snapshot(entries) is None


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


# ---------- plans saved ----------


def test_extract_plans_saved_basic():
    """Write tool_use under /plans/ → captured."""
    e = _assistant_blocks([
        {
            "type": "tool_use",
            "name": "Write",
            "input": {
                "file_path": "/Users/x/.claude/plans/foo.md",
                "content": "# Plan: do the thing",
            },
        }
    ])
    assert extract.extract_plans_saved([e]) == ["/Users/x/.claude/plans/foo.md"]


def test_extract_plans_saved_ignores_non_plan_writes():
    e = _assistant_blocks([
        {
            "type": "tool_use",
            "name": "Write",
            "input": {"file_path": "/x/foo.md", "content": "not a plan"},
        }
    ])
    assert extract.extract_plans_saved([e]) == []


def test_extract_plans_saved_dedups_repeated_writes_to_same_path():
    """Same plan written twice → one entry."""
    e = _assistant_blocks([
        {
            "type": "tool_use",
            "name": "Write",
            "input": {"file_path": "/p/plans/a.md", "content": "v1"},
        },
        {
            "type": "tool_use",
            "name": "Write",
            "input": {"file_path": "/p/plans/a.md", "content": "v2 final"},
        },
    ])
    assert extract.extract_plans_saved([e]) == ["/p/plans/a.md"]


def test_extract_plans_saved_only_write_tool():
    """Edit doesn't count — Write is the only thing that creates/replaces a plan."""
    e = _assistant_blocks([
        {
            "type": "tool_use",
            "name": "Edit",
            "input": {"file_path": "/p/plans/a.md", "new_string": "x"},
        }
    ])
    assert extract.extract_plans_saved([e]) == []


def test_extract_plans_saved_picks_up_singular_plan_dir():
    """Both `plans` and `plan` directories match (some users/projects use either)."""
    e = _assistant_blocks([
        {
            "type": "tool_use",
            "name": "Write",
            "input": {"file_path": "/p/plan/a.md", "content": "ok"},
        }
    ])
    assert extract.extract_plans_saved([e]) == ["/p/plan/a.md"]


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


# ---------- open question ----------

def test_extract_open_question_finds_last_question_mark():
    e1 = _assistant_blocks([{"type": "text", "text": "I removed the foo. Want me to bump the version too?"}])
    e2 = _assistant_blocks([{"type": "text", "text": "Done. What next? Should I run lint?"}])
    q = extract.extract_open_question([e1, e2])
    assert q == "Should I run lint?"


def test_extract_open_question_phrase_without_question_mark():
    """`next step` phrase qualifies even on a continuation line."""
    e = _assistant_blocks([{"type": "text", "text": "Sweep complete. Next step is your call."}])
    q = extract.extract_open_question([e])
    assert q is not None
    assert "Next step" in q


def test_extract_open_question_returns_none_when_silent():
    e = _assistant_blocks([{"type": "text", "text": "Sweep complete. Wrote the summary."}])
    assert extract.extract_open_question([e]) is None


def test_extract_open_question_walks_back_through_turns():
    """Skip empty/no-question turns until something matches."""
    e1 = _assistant_blocks([{"type": "text", "text": "Want me to revert the migration?"}])
    e2 = _assistant_blocks([{"type": "text", "text": "Reverted."}])
    e3 = _assistant_blocks([{"type": "text", "text": ""}])
    q = extract.extract_open_question([e1, e2, e3])
    assert q == "Want me to revert the migration?"


def test_extract_open_question_caps_long_question():
    long_q = "Should I " + "x " * 200 + "?"
    e = _assistant_blocks([{"type": "text", "text": long_q}])
    q = extract.extract_open_question([e])
    assert q is not None and len(q) <= 280


# ---------- decisions made ----------

def test_extract_decisions_made_picks_up_verbs():
    e = _assistant_blocks([{"type": "text", "text": (
        "Removed the foo middleware.\n"
        "Renaming bar to baz throughout the package.\n"
        "Just a regular sentence here.\n"
        "I'll be force-removing the lockfile too.\n"
    )}])
    out = extract.extract_decisions_made([e])
    assert any("Removed the foo middleware" in d for d in out)
    assert any("Renaming bar to baz" in d for d in out)
    assert any("force-removing" in d.lower() for d in out)
    assert all("regular sentence" not in d for d in out)


def test_extract_decisions_made_picks_up_git_commits():
    e = _assistant_blocks([{
        "type": "tool_use",
        "name": "Bash",
        "input": {"command": "git commit -m 'feat: ship the new endpoint'"},
    }])
    out = extract.extract_decisions_made([e])
    assert out == ["committed: feat: ship the new endpoint"]


def test_extract_decisions_made_dedups_case_insensitive():
    e1 = _assistant_blocks([{"type": "text", "text": "Removing the foo middleware"}])
    e2 = _assistant_blocks([{"type": "text", "text": "removing the foo middleware"}])
    out = extract.extract_decisions_made([e1, e2])
    assert len(out) == 1


def test_extract_decisions_made_caps_at_limit():
    entries = []
    for i in range(20):
        entries.append(_assistant_blocks([{"type": "text", "text": f"Removing thing-{i} now"}]))
    out = extract.extract_decisions_made(entries, limit=5)
    assert len(out) == 5


# ---------- result tables ----------

def test_extract_result_tables_finds_simple_table():
    table = (
        "| col | val |\n"
        "| --- | --- |\n"
        "| a   | 1   |\n"
        "| b   | 2   |\n"
    )
    e = _assistant_blocks([{"type": "text", "text": "Result:\n" + table}])
    out = extract.extract_result_tables([e])
    assert len(out) == 1
    assert "| a" in out[0]
    assert "| b" in out[0]


def test_extract_result_tables_only_walks_last_n_turns():
    """`from_last=2` should not pick up a table buried 3 turns back."""
    table = "| k | v |\n| - | - |\n| x | 1 |\n"
    far_back = _assistant_blocks([{"type": "text", "text": "buried " + table}])
    middle = _assistant_blocks([{"type": "text", "text": "no table here"}])
    recent_a = _assistant_blocks([{"type": "text", "text": "still no table"}])
    recent_b = _assistant_blocks([{"type": "text", "text": "ok done."}])
    out = extract.extract_result_tables([far_back, middle, recent_a, recent_b], from_last=2)
    assert out == []


def test_extract_result_tables_skips_pipe_in_prose():
    """A single pipe-bearing prose line shouldn't masquerade as a table."""
    e = _assistant_blocks([{"type": "text", "text": "she said `|` is the unsafe path."}])
    assert extract.extract_result_tables([e]) == []


def test_extract_result_tables_caps_at_max_tables():
    t1 = "| a | b |\n| - | - |\n| 1 | 2 |\n"
    t2 = "| c | d |\n| - | - |\n| 3 | 4 |\n"
    t3 = "| e | f |\n| - | - |\n| 5 | 6 |\n"
    e = _assistant_blocks([{"type": "text", "text": "\n".join([t1, t2, t3])}])
    out = extract.extract_result_tables([e], max_tables=2)
    assert len(out) == 2


# ---------- code anchor rerank ----------

def test_rerank_code_anchors_brings_relevant_to_top():
    """Anchor matching the goal's keywords should outrank an unrelated one."""
    auth = "```python\ndef login_user(token):\n    if not token: raise AuthError\n    return verify(token)\n```"
    payment = "```python\ndef charge_card(amount):\n    stripe.charge(amount)\n    return True\n```"
    out = extract.rerank_code_anchors_by_goal(
        [payment, auth], goal="fix the authentication token verification bug"
    )
    assert out[0] == auth


def test_rerank_drops_bottom_half_when_zero_score():
    """Anchors with no overlap to the goal are dropped entirely.

    Note: the tokenizer captures `migrate_users` as ONE token, so we use
    plain English words (`# migrate user`) in the anchor body to ensure
    deterministic overlap with the goal."""
    rel = "```python\n# migrate user records here\ndef fn(): pass\n```"
    junk1 = "```\nlorem ipsum dolor sit amet consectetur adipiscing\n```"
    junk2 = "```\nfoo bar baz qux quux corge garply waldo\n```"
    out = extract.rerank_code_anchors_by_goal(
        [junk1, rel, junk2], goal="run user migrate plan"
    )
    assert out == [rel]


def test_rerank_noop_when_goal_empty():
    """Empty goal → preserve original order, drop nothing."""
    a, b, c = "```\nfoo\n```", "```\nbar\n```", "```\nbaz\n```"
    out = extract.rerank_code_anchors_by_goal([a, b, c], goal="")
    assert out == [a, b, c]


def test_rerank_keeps_all_when_no_overlap():
    """Goal tokens overlap nothing → keep originals (don't strand reader)."""
    a = "```\nfoo bar\n```"
    out = extract.rerank_code_anchors_by_goal([a], goal="completely unrelated topic")
    assert out == [a]


def test_synthesize_active_goal_picks_state_line_with_arrow():
    """Numeric arrow notation `277 → 26` is the canonical state marker."""
    e = _assistant_blocks([{"type": "text", "text": (
        "Sweep complete. Reduced worktrees 277 → 26.\n"
        "Want me to keep going on the review pile?"
    )}])
    g = extract.synthesize_active_goal([e], fallback="user fallback msg")
    assert g is not None
    # Must include both the state and the pending question.
    assert "277 → 26" in g
    assert "review pile" in g


def test_synthesize_active_goal_uses_verb_number_pattern():
    """`dropped 252` is a state marker even without arrow notation."""
    e = _assistant_blocks([{"type": "text", "text": (
        "Done. Dropped 252 stale worktrees and kept 25 active ones."
    )}])
    g = extract.synthesize_active_goal([e], fallback=None)
    assert g is not None
    assert "Dropped 252" in g


def test_synthesize_active_goal_fallback_when_no_numbers():
    """Read-only audit session w/ no numeric outcomes → fall back to user msg."""
    e = _assistant_blocks([{"type": "text", "text": "Looked at the auth flow. Looks ok."}])
    g = extract.synthesize_active_goal([e], fallback="check whether auth is reasonable")
    assert g == "check whether auth is reasonable"


def test_synthesize_active_goal_returns_none_when_nothing_to_show():
    """No state, no fallback → None (caller should omit the section)."""
    e = _assistant_blocks([{"type": "text", "text": "ok"}])
    g = extract.synthesize_active_goal([e], fallback=None)
    assert g is None


def test_synthesize_active_goal_caps_at_max_chars():
    """Multi-sentence states must not blow tier1 budget."""
    long_state = "Reduced 1000 → 1, " + "x " * 200 + "?"
    e = _assistant_blocks([{"type": "text", "text": long_state}])
    g = extract.synthesize_active_goal([e], fallback=None, max_chars=120)
    assert g is not None and len(g) <= 120


def test_rerank_drops_bottom_half_among_nonzero():
    """4 anchors of decreasing relevance — drop bottom half + zero scores.
    Use plain-English token bodies so the regex tokenizer (which treats
    `auth_check_token` as one identifier) doesn't fight us."""
    auth1 = "```py\n# auth token validate handler\ndef fn(): return ok\n```"  # 3 matches
    auth2 = "```py\n# token validate refresh\ndef fn(): pass\n```"             # 2 matches
    weaker = "```py\n# helper that handles a token\nresult ok\n```"            # 1 match
    weakest = "```py\n# unrelated user session\nresult ok\n```"                # 0 matches
    out = extract.rerank_code_anchors_by_goal(
        [weakest, weaker, auth2, auth1],
        goal="auth token validate",
    )
    # 3 nonzero items, drop bottom half → keep ceil(3/2) = 2.
    assert out[0] == auth1
    assert out[1] == auth2
    assert len(out) == 2
