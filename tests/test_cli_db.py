"""Integration: `handoff.cli` upserts a DB row on a real run, and --no-db skips it."""
from __future__ import annotations

import json
from pathlib import Path

from handoff import cli, db

SID = "abcd1234-0000-0000-0000-00000000ffff"


def _transcript(tmp_path: Path) -> Path:
    p = tmp_path / "t.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user", "content": "Build the thing"}},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done building it."}]},
        },
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _run(tmp_path, monkeypatch, *extra) -> int:
    # HOME drives both the default DB path (~/.claude/compaction/sessions.db)
    # and brief out-dir expansion → fully isolated from the real ~/.claude.
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "out"
    return cli.main(
        [
            "--transcript", str(_transcript(tmp_path)),
            "--session-id", SID,
            "--cwd", "/tmp/proj",
            "--no-archive", "--no-agent-store",
            "--out-dir", str(out),
            *extra,
        ]
    )


def test_cli_writes_db_row(tmp_path, monkeypatch, capsys):
    rc = _run(tmp_path, monkeypatch)
    assert rc == 0
    dbf = tmp_path / ".claude" / "compaction" / "sessions.db"
    assert dbf.exists()
    with db.connect(dbf) as conn:
        row = db.get_session(conn, SID)
    assert row is not None
    assert row["cwd"] == "/tmp/proj"
    assert "Build the thing" in (row["body"] or "")
    # body is frontmatter-stripped — no fence leaked into the DB.
    assert not (row["body"] or "").lstrip().startswith("---")


def test_cli_brief_and_db_consistent(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    brief = (tmp_path / "out" / f"{SID}.md").read_text(encoding="utf-8")
    dbf = tmp_path / ".claude" / "compaction" / "sessions.db"
    with db.connect(dbf) as conn:
        row = db.get_session(conn, SID)
    assert row["status"] in brief  # frontmatter status matches the brief


def test_no_db_skips_upsert(tmp_path, monkeypatch):
    rc = _run(tmp_path, monkeypatch, "--no-db")
    assert rc == 0
    dbf = tmp_path / ".claude" / "compaction" / "sessions.db"
    assert not dbf.exists()  # never touched the DB


def test_default_token_mode_is_chars4_no_heavy_import(tmp_path, monkeypatch):
    """Cold-start guard: /hand:off must not pay the HF `transformers` import
    just to size the (cosmetic) brief. The CLI defaults to the chars4 heuristic;
    'auto'/'hf' stay opt-in via --token-mode. Regression: default was 'auto',
    which loaded transformers (~1.5s + a PyTorch warning) on every run."""
    import sys

    ns = cli.parse_args(["--transcript", "x", "--session-id", SID, "--cwd", "/tmp/p"])
    assert ns.token_mode == "chars4"

    # A default run must not import transformers.
    sys.modules.pop("transformers", None)
    rc = _run(tmp_path, monkeypatch)  # defaults → chars4
    assert rc == 0
    assert "transformers" not in sys.modules
