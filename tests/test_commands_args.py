"""The commands/*.md bash blocks are what CC actually runs; `$ARGUMENTS` is
substituted TEXTUALLY before execution. These tests do the same substitution
and run each block against a `python3` shim that prints its argv, so the
shell-side argument handling is pinned end to end (spec-findings.md §G)."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CMDS = ROOT / "commands"
SID_A = "6c789343-0da0-4da4-ac83-4fe9ea4e611e"
SID_B = "9517a280-0000-4000-8000-000000000000"


def _block(name: str) -> str:
    text = (CMDS / f"{name}.md").read_text(encoding="utf-8")
    m = re.search(r"```bash\n(.*?)```", text, re.S)
    assert m, f"{name}.md has no bash block"
    return m.group(1)


def _run(tmp_path: Path, name: str, arguments: str) -> str:
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "python3").write_text('#!/bin/sh\nprintf "ARGV:"; for a in "$@"; do printf " [%s]" "$a"; done; echo\n')
    (shim / "python3").chmod(0o755)
    (tmp_path / "x.md").write_text("")  # a glob target for `*`
    script = _block(name).replace("$ARGUMENTS", arguments).replace("${CLAUDE_SESSION_ID}", SID_A)
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"}
    proc = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)
    return proc.stdout + proc.stderr


@pytest.mark.parametrize("name,err", [("done", "HANDDONE_ERROR"), ("archive", "HANDARCH_ERROR")])
def test_two_sids_are_refused_not_concatenated(tmp_path, name, err):
    out = _run(tmp_path, name, f"{SID_A} {SID_B}")
    assert f"{err} one session id" in out
    assert "ARGV:" not in out  # never reached python


@pytest.mark.parametrize("name,flag", [("done", "--reopen"), ("archive", "--unarchive")])
def test_flag_and_sid_pass_through(tmp_path, name, flag):
    out = _run(tmp_path, name, f"{SID_A} {flag}")
    assert f"[{SID_A}] [{flag}]" in out


@pytest.mark.parametrize("name", ["holds", "tasks", "review", "on"])
def test_star_argument_is_not_globbed(tmp_path, name):
    out = _run(tmp_path, name, "*")
    assert "[*]" in out
    assert "x.md" not in out


def test_every_unquoted_arguments_expansion_has_set_f():
    for md in sorted(CMDS.glob("*.md")):
        block = _block(md.stem)
        if re.search(r"(?<![\"'])\$ARGUMENTS(?![\"'])", block):
            assert "set -f" in block, f"{md.name} expands $ARGUMENTS unquoted without set -f"
