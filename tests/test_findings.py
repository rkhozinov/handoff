"""Regressions for the deferred 2026-09-21 review findings (spec-findings.md).
Each test names its spec section. Written red-first; constants below were
re-derived from the failing runs, not guessed."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from handoff import cli, extract, lifecycle, trim
from handoff.extract import elide_pasted_output, is_injected_user_msg

SID = "8e4178e1-7dc1-4352-b13e-98faeb5c1116"


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _asst(text, *tools):
    blocks = [{"type": "text", "text": text}] + [
        {"type": "tool_use", "name": n, "id": i, "input": inp} for n, i, inp in tools
    ]
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _tool_result(*blocks, tur=None):
    e = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": txt}]}
        for tid, txt in blocks
    ]}}
    if tur is not None:
        e["toolUseResult"] = {"content": [{"type": "text", "text": tur}]}
    return e


# ---------------------------------------------------------------- A. injected


def test_injected_marker_quoted_late_is_real():
    msg = "please fix the docs: the file says `<command-name>` is a marker we should document " + "x" * 100
    assert not is_injected_user_msg(msg)


def test_injected_marker_at_head_still_dropped():
    assert is_injected_user_msg("<command-name>/foo</command-name>\nbody")
    assert is_injected_user_msg("Base directory for this skill: /x\n" + "y" * 300)


# --------------------------------------------------------- B. fence-safe cuts


def _fenced_turn():
    body = "intro\n```python\n" + "x = 1\n" * 900 + "```\nafter"
    return _asst(body)


def test_assistant_truncation_closes_open_fence():
    text, _ = trim._classify_assistant(_fenced_turn())
    head = text.split("\n…[elided")[0]
    assert head.count("```") % 2 == 0
    assert head.endswith("```")


def test_assistant_truncation_cuts_at_newline():
    text, _ = trim._classify_assistant(_fenced_turn())
    head = text.split("\n…[elided")[0]
    # the last content line before the closing fence must be a whole line
    lines = head.split("\n")
    assert lines[-1] == "```"  # the fence the cut opened, closed
    assert lines[-2] == "x = 1"  # not "x =" — today's cut is mid-line
    assert len(head) <= trim.ASSISTANT_TURN_MAX_CHARS + len("\n```")


def test_pasted_elision_cuts_at_newline():
    lead = "look at this\n❯ run\n"
    text = lead + "".join(f"Status: line {i} of the paste\n" for i in range(40))
    out = elide_pasted_output(text)
    head = out.split("\n…[elided")[0]
    assert head.endswith("of the paste")  # whole line, not a mid-line cut
    assert len(head) <= extract.PASTED_PRESERVE_CHARS


# ------------------------------------------------------- C. transcript overflow


def _jsonl(tmp_path: Path, n_turns: int) -> Path:
    p = tmp_path / "t.jsonl"
    rows = []
    for i in range(n_turns):
        rows.append({"type": "user", "message": {"content": f"user turn {i} " + "u" * 50}})
        rows.append({"type": "assistant", "message": {"content": [{"type": "text", "text": f"assistant {i} " + "a" * 50}]}})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_transcript_overflow_reports_remainder(tmp_path):
    from handoff.transcript import trim_transcript

    p = _jsonl(tmp_path, 3)  # 6 entries, each rendered line ≈ 66 chars
    out = trim_transcript(p, max_chars=150)  # room for 2 full lines, cut on the 3rd
    assert out.endswith("…[transcript continues 4 lines]")


def test_transcript_overflow_reads_file_once(tmp_path):
    from handoff import transcript

    p = _jsonl(tmp_path, 3)
    calls = []
    real = transcript._iter_jsonl

    def counting(path):
        calls.append(path)
        return real(path)

    with patch("handoff.transcript._iter_jsonl", side_effect=counting):
        transcript.trim_transcript(p, max_chars=150)
    assert len(calls) == 1


# ------------------------------------------------------- D. archive metadata


def test_project_tag_sanitised():
    from handoff.recall import project_tag_from_cwd

    assert project_tag_from_cwd("/x/my proj,v2") == "project:my-proj-v2"
    assert project_tag_from_cwd("/x/handoff") == "project:handoff"


def test_archive_tag_and_source_are_sanitised(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from handoff.archive import archive_full_session

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = tmp_path / "proj" / "s.jsonl"
    p.parent.mkdir()
    p.write_text(json.dumps({"type": "user", "message": {"content": "hello there"}}) + "\n")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"content_hash": "ab" * 32}), stderr="")

    with patch("handoff.archive._memory_bin", return_value="/fake/memory"), \
         patch("handoff.archive.subprocess.run", side_effect=fake_run):
        archive_full_session(str(p), SID, "/Users/test/my proj,v2")

    argv = calls[0]
    tags = argv[argv.index("--tags") + 1]
    meta = json.loads(argv[argv.index("--metadata") + 1])
    assert tags.split(",") == ["source:auto", "session-archive", "project:my-proj-v2"]
    assert meta["source_jsonl"] == "~/proj/s.jsonl"


# ---------------------------------------------------- E. detect_status + tasks


def _task(status):
    return {"id": "1", "subject": "s", "description": "d", "status": status, "blocks": [], "blockedBy": []}


def test_detect_status_all_tasks_completed_is_done():
    entries = [_user("start"), _user("hmm")]
    assert lifecycle.detect_status(entries, [_task("completed"), _task("completed")]) == ("done", "auto-tasks")


def test_detect_status_open_tasks_veto_user_done():
    entries = [_user("start"), _user("done")]
    assert lifecycle.detect_status(entries) == ("done", "auto-user-msg")  # unchanged without tasks
    assert lifecycle.detect_status(entries, [_task("completed"), _task("pending")]) == ("in_progress", "auto-default")


def test_detect_status_no_tasks_falls_through():
    entries = [_user("start"), _user("what next?")]
    assert lifecycle.detect_status(entries, []) == ("pending", "auto-open-q")


def test_cli_run_reads_task_dir(tmp_path, monkeypatch):
    from handoff import tasks

    monkeypatch.setenv("HOME", str(tmp_path))
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps(r) for r in [_user("build it"), _user("done")]) + "\n")
    tasks_root = tmp_path / "tasks"
    d = tasks.tasks_dir(SID, base=tasks_root)
    d.mkdir(parents=True)
    (d / "1.json").write_text(json.dumps(_task("pending")))
    out = tmp_path / "out"
    args = cli.parse_args([
        "--transcript", str(t), "--session-id", SID, "--cwd", "/tmp/p",
        "--no-archive", "--no-agent-store", "--no-db", "--out-dir", str(out),
        "--tasks-dir", str(tasks_root),
    ])
    res = cli.run(args)
    assert res.fm["status"] == "in_progress"
    assert res.fm["completion_signal"] == "auto-default"


# --------------------------------------------------------------- F. trimmer


def test_short_signal_survives_same_turn_drop():
    e = _asst("`max-limit=0` means 0bps, not unlimited.", ("Read", "1", {"file_path": "/a"}))
    out = trim.render_assistant(e)
    assert out is not None and "`max-limit=0`" in out


def test_short_narration_still_dropped():
    e = _asst("Now commit and push.", ("Read", "1", {"file_path": "/a"}))
    assert trim.render_assistant(e) == "[Read file_path=/a]"
    e2 = _asst("Good, the `form` is loaded. Let me fill it in.", ("Read", "1", {"file_path": "/a"}))
    assert trim.render_assistant(e2) == "[Read file_path=/a]"


def test_snapshot_between_turns_does_not_change_trim():
    a = _asst("Fix is in db.py:12 — the WHERE clause.")
    u = _tool_result(("1", "ok"))
    snap = {"type": "file-history-snapshot"}
    assert trim.build_convo([a, snap, u]) == trim.build_convo([a, u])


def test_tooluseresult_not_applied_to_sibling_blocks():
    spawn = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Agent", "id": "A", "input": {"description": "one"}},
        {"type": "tool_use", "name": "Agent", "id": "B", "input": {"description": "two"}},
    ]}}
    res = _tool_result(("A", "REPORT-A " * 40), ("B", "REPORT-B " * 40), tur="REPORT-A " * 40)
    convo = trim.build_convo([spawn, res])
    assert [c[1].split("\n")[0] for c in convo] == ["[Sub-agent report: one]", "[Sub-agent report: two]"]
    assert "REPORT-B" in convo[1][1]
    assert "REPORT-A" not in convo[1][1]


def test_tool_result_texts_helper():
    lone = _tool_result(("A", "noisy body"), tur="clean body")
    assert extract.tool_result_texts(lone) == [("A", "clean body")]
    pair = _tool_result(("A", "a body"), ("B", "b body"), tur="clean body")
    assert extract.tool_result_texts(pair) == [("A", "a body"), ("B", "b body")]


# ---------------------------------------------------------- H. dev scripts


def test_fixture_stats_shared(tmp_path):
    from scripts import bench, render_html
    from scripts.fixture_stats import fixture_stats

    p = _jsonl(tmp_path, 2)
    entries = extract.load_jsonl(str(p))
    shared = fixture_stats(p, entries, "chars4")
    assert bench.stats_for(p, "chars4") == shared
    row = render_html.stat_row(p.stem, p, entries)
    assert row["brief_bytes"] == shared["brief_b"]
    assert row["user_signal"] == shared["user_signal"]


def test_render_html_loads_each_fixture_once(tmp_path, monkeypatch):
    from scripts import render_html

    p = _jsonl(tmp_path, 2)
    calls = []
    real = render_html.load_jsonl
    monkeypatch.setattr(render_html, "load_jsonl", lambda path: (calls.append(path), real(path))[1])
    render_html.main(["--fixtures", str(tmp_path), "--out", str(tmp_path / "r.html"), "--token-mode", "chars4"])
    assert calls.count(str(p)) == 1


def test_short_signal_survives_adjacent_drop():
    a = _asst("Bug — `delete` removed ALL matches.")
    u = _tool_result(("1", "ok"))
    assert trim.build_convo([a, u]) == [("assistant", "Bug — `delete` removed ALL matches.")]
