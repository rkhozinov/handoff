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
                [{"type": "tool_result", "tool_use_id": str(i), "content": f"error {i}"}]
            )
        )
    errs = extract.extract_errors(entries, limit=3)
    assert len(errs) == 3
    assert "error 19" in errs[-1]


def test_extract_errors_ignores_clean_output():
    entries = [
        _user_blocks([{"type": "tool_result", "tool_use_id": "ok", "content": "all good"}])
    ]
    assert extract.extract_errors(entries) == []


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
