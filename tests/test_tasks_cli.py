"""Tests for the `hand tasks` CLI layer — machine-readable stdout contract.

The /hand:* command bash blocks branch on the first token, so these assertions
are the contract: HANDTASKS_OK / _DRYRUN / _EMPTY / _ERROR, and rc 0 for
everything except a real error.
"""
from __future__ import annotations

import json

from handoff import dbcli, tasks

SID = "9517a280-4d3a-4634-b506-47a6f215767c"
SHORT = "9517a280"
DEST = "a1b2c3d4-0000-0000-0000-000000000000"
DEST_SHORT = "a1b2c3d4"


def _task(id: str, **over) -> dict:
    t = {
        "id": id,
        "subject": f"task {id}",
        "description": f"description of {id}",
        "activeForm": f"doing {id}",
        "status": "pending",
        "blocks": [],
        "blockedBy": [],
    }
    t.update(over)
    return t


def _setup(tmp_path, *tasks_, sid: str = SID) -> tuple[str, str]:
    """Seed a CC task dir. Returns (tasks_dir_root, bundle_dir)."""
    root = tmp_path / "tasks"
    d = root / f"session-{tasks.short_id(sid)}"
    d.mkdir(parents=True)
    for t in tasks_:
        (d / f"{t['id']}.json").write_text(json.dumps(t, indent=2), encoding="utf-8")
    return str(root), str(tmp_path / "bundles")


def _args(root: str, bundles: str) -> list[str]:
    return ["--tasks-dir", root, "--bundle-dir", bundles]


def _dest_files(root: str, sid: str = DEST) -> list[str]:
    d = tasks.tasks_dir(sid, base=root)
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


class TestExport:
    def test_export_prints_ok_and_writes_bundle(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path, _task("1"), _task("5", blockedBy=["1"]))
        rc = dbcli.main(["tasks", "export", SID, *_args(root, bundles)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "HANDTASKS_OK" in out and "op=export" in out and "tasks=2" in out

        bundle = json.loads(tasks.bundle_path(SID, base=bundles).read_text())
        assert bundle["source_session_id"] == SID
        assert [t["id"] for t in bundle["tasks"]] == ["1", "5"]

    def test_export_is_lossless_including_completed(self, tmp_path, capsys):
        """Filtering is an import-time concern; the bundle keeps everything."""
        root, bundles = _setup(tmp_path, _task("1", status="completed"), _task("2"))
        assert dbcli.main(["tasks", "export", SID, *_args(root, bundles)]) == 0
        bundle = json.loads(tasks.bundle_path(SID, base=bundles).read_text())
        assert [t["status"] for t in bundle["tasks"]] == ["completed", "pending"]

    def test_export_honours_out(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path, _task("1"))
        out_file = tmp_path / "custom" / "b.json"
        rc = dbcli.main(
            ["tasks", "export", SID, "--out", str(out_file), *_args(root, bundles)]
        )
        assert rc == 0 and out_file.is_file()

    def test_export_empty_session_prints_HANDTASKS_EMPTY_rc0(self, tmp_path, capsys):
        """A session with no tasks is normal and must not break /hand:off."""
        root, bundles = _setup(tmp_path)
        rc = dbcli.main(["tasks", "export", SID, *_args(root, bundles)])
        assert rc == 0
        assert "HANDTASKS_EMPTY" in capsys.readouterr().out
        assert not tasks.bundle_path(SID, base=bundles).exists()

    def test_export_unknown_session_prints_EMPTY_rc0(self, tmp_path, capsys):
        """No task dir at all is the same story as an empty one."""
        root, bundles = _setup(tmp_path)
        other = "deadbeef-0000-0000-0000-000000000000"
        rc = dbcli.main(["tasks", "export", other, *_args(root, bundles)])
        assert rc == 0
        assert "HANDTASKS_EMPTY" in capsys.readouterr().out


class TestImport:
    def _bundle(self, tmp_path, *tasks_, sid: str = SID) -> str:
        p = tmp_path / "b.json"
        p.write_text(json.dumps(tasks.make_bundle(sid, list(tasks_))), encoding="utf-8")
        return str(p)

    def test_import_prints_counts(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path, _task("1"), _task("5", blockedBy=["1"]))
        rc = dbcli.main(["tasks", "import", b, "--to", DEST, *_args(root, bundles)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "HANDTASKS_OK" in out and "op=import" in out
        assert f"source={SHORT}" in out and f"dest={DEST_SHORT}" in out
        assert "imported=2" in out and "skipped=0" in out and "mode=merge" in out
        assert _dest_files(root) == ["1.json", "2.json"]

    def test_import_reports_dropped_refs(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path, _task("5", blockedBy=["9"]))
        assert dbcli.main(["tasks", "import", b, "--to", DEST, *_args(root, bundles)]) == 0
        assert "dropped_refs=1" in capsys.readouterr().out

    def test_import_dry_run_prints_DRYRUN_and_writes_nothing(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path, _task("1"))
        rc = dbcli.main(
            ["tasks", "import", b, "--to", DEST, "--dry-run", *_args(root, bundles)]
        )
        assert rc == 0
        assert "HANDTASKS_DRYRUN" in capsys.readouterr().out
        assert _dest_files(root) == []
        assert not tasks.manifest_path(DEST, base=bundles).exists()

    def test_import_is_idempotent(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path, _task("1"), _task("2"))
        argv = ["tasks", "import", b, "--to", DEST, *_args(root, bundles)]
        assert dbcli.main(argv) == 0
        capsys.readouterr()
        assert dbcli.main(argv) == 0
        out = capsys.readouterr().out
        assert "imported=0" in out and "skipped=2" in out
        assert _dest_files(root) == ["1.json", "2.json"]

    def test_only_open_is_the_default(self, tmp_path, capsys):
        """CC wipes a list once every task in it is completed, so restoring
        completed tasks would restore work that vanishes seconds later."""
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path, _task("1", status="completed"), _task("2"))
        assert dbcli.main(["tasks", "import", b, "--to", DEST, *_args(root, bundles)]) == 0
        assert "imported=1" in capsys.readouterr().out
        assert _dest_files(root) == ["1.json"]
        written = json.loads((tasks.tasks_dir(DEST, base=root) / "1.json").read_text())
        assert written["subject"] == "task 2"

    def test_all_flag_includes_completed(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path, _task("1", status="completed"), _task("2"))
        rc = dbcli.main(
            ["tasks", "import", b, "--to", DEST, "--all", *_args(root, bundles)]
        )
        assert rc == 0
        assert "imported=2" in capsys.readouterr().out
        assert _dest_files(root) == ["1.json", "2.json"]

    def test_replace_requires_explicit_flag(self, tmp_path, capsys):
        """Merge is the default; replace is the one mode that loses data."""
        root, bundles = _setup(tmp_path)
        dest_dir = tasks.tasks_dir(DEST, base=root)
        dest_dir.mkdir(parents=True)
        (dest_dir / "1.json").write_text(json.dumps(_task("1", subject="keep me")))

        b = self._bundle(tmp_path, _task("4"))
        assert dbcli.main(["tasks", "import", b, "--to", DEST, *_args(root, bundles)]) == 0
        assert "mode=merge" in capsys.readouterr().out
        assert _dest_files(root) == ["1.json", "2.json"]
        assert json.loads((dest_dir / "1.json").read_text())["subject"] == "keep me"

        rc = dbcli.main(
            ["tasks", "import", b, "--to", DEST, "--replace", *_args(root, bundles)]
        )
        assert rc == 0
        assert "mode=replace" in capsys.readouterr().out
        assert _dest_files(root) == ["4.json"]

    def test_import_missing_bundle_prints_ERROR_rc1(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        rc = dbcli.main(
            ["tasks", "import", str(tmp_path / "nope.json"), "--to", DEST, *_args(root, bundles)]
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "HANDTASKS_ERROR" in out and "reason=no-such-bundle" in out

    def test_import_malformed_bundle_prints_ERROR_rc1(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        rc = dbcli.main(["tasks", "import", str(p), "--to", DEST, *_args(root, bundles)])
        assert rc == 1
        assert "reason=bad-bundle" in capsys.readouterr().out

    def test_import_empty_bundle_prints_EMPTY_rc0(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path)
        rc = dbcli.main(["tasks", "import", b, "--to", DEST, *_args(root, bundles)])
        assert rc == 0
        assert "HANDTASKS_EMPTY" in capsys.readouterr().out

    def test_import_bad_dest_sid_prints_ERROR_rc1(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path, _task("1"))
        rc = dbcli.main(["tasks", "import", b, "--to", "nope", *_args(root, bundles)])
        assert rc == 1
        assert "reason=bad-session-id" in capsys.readouterr().out

    def test_import_honours_list_id_override(self, tmp_path, capsys):
        """An agent-team session names its task dir after the team, not the
        session — CC resolves $CLAUDE_CODE_TASK_LIST_ID / team name first."""
        root, bundles = _setup(tmp_path)
        b = self._bundle(tmp_path, _task("1"))
        rc = dbcli.main(
            ["tasks", "import", b, "--to", DEST, "--list-id", "my-team", *_args(root, bundles)]
        )
        assert rc == 0
        assert (tmp_path / "tasks" / "my-team" / "1.json").is_file()


class TestCopyAndList:
    def test_copy_end_to_end(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path, _task("1"), _task("5", blockedBy=["1"]))
        rc = dbcli.main(
            ["tasks", "copy", "--from", SID, "--to", DEST, *_args(root, bundles)]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "HANDTASKS_OK" in out and "op=copy" in out and "imported=2" in out
        assert _dest_files(root) == ["1.json", "2.json"]
        edge = json.loads((tasks.tasks_dir(DEST, base=root) / "2.json").read_text())
        assert edge["blockedBy"] == ["1"]

    def test_copy_empty_source_prints_EMPTY_rc0(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        rc = dbcli.main(["tasks", "copy", "--from", SID, "--to", DEST, *_args(root, bundles)])
        assert rc == 0
        assert "HANDTASKS_EMPTY" in capsys.readouterr().out

    def test_list_renders_tasks(self, tmp_path, capsys):
        root, bundles = _setup(
            tmp_path, _task("1", subject="first"), _task("5", subject="second", blockedBy=["1"])
        )
        rc = dbcli.main(["tasks", "list", SID, *_args(root, bundles)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "first" in out and "second" in out and "blockedBy" in out

    def test_list_empty_prints_EMPTY_rc0(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        rc = dbcli.main(["tasks", "list", SID, *_args(root, bundles)])
        assert rc == 0
        assert "HANDTASKS_EMPTY" in capsys.readouterr().out

    def test_unknown_session_prints_ERROR_rc1(self, tmp_path, capsys):
        root, bundles = _setup(tmp_path)
        rc = dbcli.main(["tasks", "list", "deadbeef-nope", *_args(root, bundles)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "HANDTASKS_ERROR" in out and "reason=bad-session-id" in out
