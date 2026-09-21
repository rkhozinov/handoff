"""Ground rule (2026-09-21): no code path changes a brief's status or
deletes anything without a user-issued command. These pin the two paths
that used to break it."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from handoff import cli

ROOT = Path(__file__).resolve().parent.parent
SID = "abcd1234-0000-0000-0000-00000000ffff"


def test_handoff_never_prunes_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    with patch("handoff.archive.prune_archives", side_effect=AssertionError("prune ran during /hand:off")):
        rc = cli.main(["--transcript", str(t), "--session-id", SID, "--cwd", "/p",
                       "--no-archive", "--no-agent-store", "--no-db", "--out-dir", str(tmp_path / "o")])
    assert rc == 0
    assert not hasattr(__import__("handoff.archive", fromlist=["x"]), "maybe_prune_archives")


def test_prune_archives_cli_is_dry_run_unless_apply(tmp_path, capsys):
    from handoff import dbcli

    with patch("handoff.archive.prune_archives", return_value={"deleted": 0, "dry_run": True}) as pr:
        dbcli.main(["prune-archives", "--db", str(tmp_path / "s.db")])
        assert pr.call_args.kwargs["dry_run"] is True
        dbcli.main(["prune-archives", "--apply", "--db", str(tmp_path / "s.db")])
        assert pr.call_args.kwargs["dry_run"] is False


def test_sweep_and_auto_stale_are_gone():
    from handoff import lifecycle

    assert not (ROOT / "scripts" / "sweep_stale.py").exists()
    assert not hasattr(lifecycle, "mark_stale")
    assert "auto-stale" not in lifecycle.Signal.__args__
