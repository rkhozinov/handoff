"""Tests for `handoff.tasks` — the pure task-carry-over core.

Fixtures are synthetic. Real task JSON under ~/.claude/tasks names employer
systems and colleagues; this repo is public (CLAUDE.local.md publish hygiene),
so nothing is ever copied in from there.
"""
from __future__ import annotations

import json

import pytest

from handoff import tasks

SID = "9517a280-4d3a-4634-b506-47a6f215767c"
SHORT = "9517a280"
DEST_SID = "a1b2c3d4-0000-0000-0000-000000000000"
DEST_SHORT = "a1b2c3d4"


def _task(id: str, **over) -> dict:
    """Build a task the way CC writes them. Override any field."""
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


def _write_tasks(d, *tasks_) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for t in tasks_:
        (d / f"{t['id']}.json").write_text(json.dumps(t, indent=2), encoding="utf-8")


def _bundle(*tasks_, sid: str = SID) -> dict:
    return tasks.make_bundle(sid, list(tasks_))


def _plan(bundle, dest, **over):
    kw = {"mode": "merge", "only_open": False, "already_imported": {}}
    kw.update(over)
    return tasks.plan_import(bundle, dest, **kw)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
class TestResolution:
    def test_short_id_truncates_full_uuid(self):
        assert tasks.short_id(SID) == SHORT

    def test_short_id_passes_through_8_hex(self):
        assert tasks.short_id(SHORT) == SHORT

    @pytest.mark.parametrize("bad", ["", "nope", "9517", "zzzzzzzz", "9517a28", None])
    def test_short_id_rejects_garbage(self, bad):
        with pytest.raises(ValueError):
            tasks.short_id(bad)

    def test_tasks_dir_composes_prefix(self, tmp_path):
        d = tasks.tasks_dir(SID, base=tmp_path)
        assert d == tmp_path / f"session-{SHORT}"
        assert not d.exists()  # never created as a side effect

    def test_list_id_override_wins(self, tmp_path):
        """CC resolves the list id as $CLAUDE_CODE_TASK_LIST_ID -> team name ->
        session-<short>. An explicit override must bypass the session form."""
        assert tasks.list_id(SID) == f"session-{SHORT}"
        assert tasks.list_id(SID, "my-team") == "my-team"
        assert tasks.tasks_dir(SID, base=tmp_path, list_id="my-team") == tmp_path / "my-team"

    def test_list_id_is_sanitized_like_cc(self):
        """CC applies [^a-zA-Z0-9_-] -> '-' to the list id before using it as a
        directory name. A team name with a slash must not escape the dir."""
        assert tasks.list_id(SID, "team/../evil") == "team----evil"


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
class TestRead:
    def test_read_missing_dir_returns_empty(self, tmp_path):
        got, warns = tasks.read_tasks(tmp_path / "nope")
        assert got == [] and warns == []

    def test_read_empty_dir_returns_empty(self, tmp_path):
        (tmp_path / "d").mkdir()
        got, warns = tasks.read_tasks(tmp_path / "d")
        assert got == [] and warns == []

    def test_read_ignores_lock_and_highwatermark(self, tmp_path):
        d = tmp_path / "d"
        _write_tasks(d, _task("1"))
        (d / ".lock").write_text("", encoding="utf-8")
        (d / ".highwatermark").write_text("7", encoding="utf-8")
        got, warns = tasks.read_tasks(d)
        assert [t["id"] for t in got] == ["1"]
        assert warns == []

    def test_read_ignores_arbitrary_dotfiles(self, tmp_path):
        d = tmp_path / "d"
        _write_tasks(d, _task("1"))
        (d / ".DS_Store").write_text("junk", encoding="utf-8")
        (d / ".hidden.json").write_text("{}", encoding="utf-8")
        got, warns = tasks.read_tasks(d)
        assert [t["id"] for t in got] == ["1"]
        assert warns == []

    def test_read_sorts_numerically_not_lexically(self, tmp_path):
        d = tmp_path / "d"
        _write_tasks(d, _task("10"), _task("2"), _task("1"))
        got, _ = tasks.read_tasks(d)
        assert [t["id"] for t in got] == ["1", "2", "10"]

    def test_read_preserves_unknown_keys(self, tmp_path):
        d = tmp_path / "d"
        _write_tasks(d, _task("1", owner="alice", metadata={"k": "v"}, futureKey=42))
        got, _ = tasks.read_tasks(d)
        assert got[0]["owner"] == "alice"
        assert got[0]["metadata"] == {"k": "v"}
        assert got[0]["futureKey"] == 42

    def test_read_tolerates_missing_activeForm(self, tmp_path):
        d = tmp_path / "d"
        t = _task("1")
        del t["activeForm"]
        _write_tasks(d, t)
        got, warns = tasks.read_tasks(d)
        assert len(got) == 1 and "activeForm" not in got[0] and warns == []

    def test_read_malformed_json_warns_and_continues(self, tmp_path):
        d = tmp_path / "d"
        _write_tasks(d, *[_task(str(i)) for i in range(1, 26)])
        (d / "99.json").write_text("{not json", encoding="utf-8")
        got, warns = tasks.read_tasks(d)
        assert len(got) == 25
        assert len(warns) == 1 and "99.json" in warns[0]

    def test_read_rejects_task_missing_required_key(self, tmp_path):
        """CC validates every file with zod and silently drops failures. A task
        with no `subject` would vanish from TaskList, so exclude it here and
        say why."""
        d = tmp_path / "d"
        bad = _task("2")
        del bad["subject"]
        _write_tasks(d, _task("1"), bad)
        got, warns = tasks.read_tasks(d)
        assert [t["id"] for t in got] == ["1"]
        assert len(warns) == 1 and "subject" in warns[0]

    def test_read_rejects_task_missing_description(self, tmp_path):
        """`description` is required by CC's schema even though the spec's
        original REQUIRED_KEYS omitted it."""
        d = tmp_path / "d"
        bad = _task("2")
        del bad["description"]
        _write_tasks(d, _task("1"), bad)
        got, warns = tasks.read_tasks(d)
        assert [t["id"] for t in got] == ["1"]
        assert len(warns) == 1 and "description" in warns[0]

    def test_read_rejects_invalid_status(self, tmp_path):
        d = tmp_path / "d"
        _write_tasks(d, _task("1"), _task("2", status="cancelled"))
        got, warns = tasks.read_tasks(d)
        assert [t["id"] for t in got] == ["1"]
        assert len(warns) == 1 and "status" in warns[0]


# --------------------------------------------------------------------------- #
# Bundle
# --------------------------------------------------------------------------- #
class TestBundle:
    def test_bundle_roundtrip_is_lossless(self, tmp_path):
        d = tmp_path / "d"
        originals = [
            _task("1", owner="alice"),
            _task("5", status="completed", metadata={"x": [1, 2]}),
            _task("7", blockedBy=["5"], blocks=[]),
        ]
        _write_tasks(d, *originals)
        got, _ = tasks.read_tasks(d)
        parsed = json.loads(json.dumps(tasks.make_bundle(SID, got)))
        assert parsed["tasks"] == sorted(originals, key=lambda t: int(t["id"]))

    def test_bundle_records_source_sid_and_version(self):
        b = tasks.make_bundle(SID, [_task("1")])
        assert b["source_session_id"] == SID
        assert b["version"] == tasks.BUNDLE_VERSION
        assert b["created"]


# --------------------------------------------------------------------------- #
# Merge / remap
# --------------------------------------------------------------------------- #
class TestMerge:
    def test_merge_into_empty_dest_starts_at_1(self):
        plan = _plan(_bundle(_task("4"), _task("9")), [])
        assert plan.id_map == {"4": "1", "9": "2"}

    def test_merge_into_nonempty_dest_starts_after_max_id(self):
        """dest 1,2,7 -> first new id 8, not 3. Holes are never backfilled:
        CC allocates from max(max_file_id, highwatermark)+1, so reusing 3 would
        collide with an id the destination may still be holding a reference to."""
        dest = [_task("1"), _task("2"), _task("7")]
        plan = _plan(_bundle(_task("1"), _task("2")), dest)
        assert plan.id_map == {"1": "8", "2": "9"}

    def test_merge_rewrites_blockedBy_through_map(self):
        b = _bundle(_task("1"), _task("5", blockedBy=["1"]))
        plan = _plan(b, [])
        by_id = {t["id"]: t for t in plan.writes}
        assert by_id["2"]["blockedBy"] == ["1"]

    def test_merge_rewrites_blocks_through_map(self):
        b = _bundle(_task("1", blocks=["5"]), _task("5", blockedBy=["1"]))
        plan = _plan(b, [])
        by_id = {t["id"]: t for t in plan.writes}
        assert by_id["1"]["blocks"] == ["2"]

    def test_merge_drops_dangling_ref_and_records_it(self):
        """A ref to an id not in the bundle is dropped from the array AND
        reported — never silently."""
        b = _bundle(_task("5", blockedBy=["9"]))
        plan = _plan(b, [])
        assert plan.writes[0]["blockedBy"] == []
        assert plan.dropped_refs == [("5", "blockedBy", "9")]

    def test_merge_worked_example_from_the_spec(self):
        """dest 1,2; bundle 1,5,7 where 7.blockedBy=[5] and 5.blockedBy=[9]."""
        dest = [_task("1"), _task("2")]
        b = _bundle(_task("1"), _task("5", blockedBy=["9"]), _task("7", blockedBy=["5"]))
        plan = _plan(b, dest)
        assert plan.id_map == {"1": "3", "5": "4", "7": "5"}
        by_id = {t["id"]: t for t in plan.writes}
        assert by_id["4"]["blockedBy"] == []
        assert by_id["5"]["blockedBy"] == ["4"]
        assert plan.dropped_refs == [("5", "blockedBy", "9")]

    def test_merge_preserves_dest_tasks_untouched(self, tmp_path):
        d = tmp_path / "dest"
        dest_tasks = [_task("1"), _task("2", blockedBy=["1"])]
        _write_tasks(d, *dest_tasks)
        before = {p.name: p.read_bytes() for p in d.glob("*.json")}
        plan = _plan(_bundle(_task("1")), dest_tasks)
        tasks.apply_plan(plan, d, tmp_path / "manifest.json")
        after = {p.name: p.read_bytes() for p in d.glob("*.json")}
        for name, data in before.items():
            assert after[name] == data
        assert "3.json" in after

    def test_merge_is_idempotent_via_manifest(self, tmp_path):
        d = tmp_path / "dest"
        d.mkdir()
        manifest = tmp_path / "manifest.json"
        b = _bundle(_task("1"), _task("5", blockedBy=["1"]))

        plan1 = _plan(b, [])
        assert len(plan1.writes) == 2 and plan1.skipped == 0
        tasks.apply_plan(plan1, d, manifest)

        dest_now, _ = tasks.read_tasks(d)
        plan2 = _plan(b, dest_now, already_imported=tasks.read_manifest(manifest))
        assert plan2.writes == [] and plan2.skipped == 2

        tasks.apply_plan(plan2, d, manifest)
        assert len(list(d.glob("*.json"))) == 2

    def test_merge_two_different_sources_into_one_dest(self, tmp_path):
        """The /hand:on <a> <b> case: two bundles stack without collision, and
        each source keeps its own manifest namespace."""
        d = tmp_path / "dest"
        d.mkdir()
        manifest = tmp_path / "manifest.json"
        other_sid = "deadbeef-0000-0000-0000-000000000000"

        b1 = _bundle(_task("1"), _task("2", blockedBy=["1"]))
        p1 = _plan(b1, [], already_imported=tasks.read_manifest(manifest))
        tasks.apply_plan(p1, d, manifest)

        dest_now, _ = tasks.read_tasks(d)
        b2 = _bundle(_task("1"), _task("2", blockedBy=["1"]), sid=other_sid)
        p2 = _plan(b2, dest_now, already_imported=tasks.read_manifest(manifest))
        tasks.apply_plan(p2, d, manifest)

        got, warns = tasks.read_tasks(d)
        assert [t["id"] for t in got] == ["1", "2", "3", "4"] and warns == []
        by_id = {t["id"]: t for t in got}
        assert by_id["2"]["blockedBy"] == ["1"]
        assert by_id["4"]["blockedBy"] == ["3"]

        # Re-running either source is still a no-op.
        dest_now, _ = tasks.read_tasks(d)
        assert _plan(b1, dest_now, already_imported=tasks.read_manifest(manifest)).skipped == 2

    def test_creation_order_is_topological(self):
        """A blocker always precedes its dependent. Needed for the Plan B
        replay path, which must emit addBlockedBy after both ends exist."""
        b = _bundle(
            _task("1", blockedBy=["3"]),
            _task("2", blockedBy=["1"]),
            _task("3"),
        )
        plan = _plan(b, [])
        order = plan.creation_order
        assert set(order) == set(plan.id_map.values())
        pos = {tid: i for i, tid in enumerate(order)}
        by_id = {t["id"]: t for t in plan.writes}
        for tid, t in by_id.items():
            for dep in t["blockedBy"]:
                assert pos[dep] < pos[tid]

    def test_creation_order_survives_a_cycle(self):
        """A malformed source with a blockedBy cycle must still yield every id
        exactly once rather than hanging or dropping tasks."""
        b = _bundle(_task("1", blockedBy=["2"]), _task("2", blockedBy=["1"]))
        plan = _plan(b, [])
        assert sorted(plan.creation_order) == sorted(plan.id_map.values())


# --------------------------------------------------------------------------- #
# Replace
# --------------------------------------------------------------------------- #
class TestReplace:
    def test_replace_clears_dest_json_files(self, tmp_path):
        d = tmp_path / "dest"
        _write_tasks(d, _task("1"), _task("2"), _task("3"))
        plan = _plan(_bundle(_task("7")), [_task("1"), _task("2"), _task("3")], mode="replace")
        tasks.apply_plan(plan, d, tmp_path / "m.json")
        assert sorted(p.name for p in d.glob("*.json")) == ["7.json"]

    def test_replace_keeps_original_ids(self):
        b = _bundle(_task("4"), _task("9", blockedBy=["4"]))
        plan = _plan(b, [_task("1")], mode="replace")
        assert plan.id_map == {"4": "4", "9": "9"}
        by_id = {t["id"]: t for t in plan.writes}
        assert by_id["9"]["blockedBy"] == ["4"]

    def test_replace_leaves_control_files_untouched(self, tmp_path):
        d = tmp_path / "dest"
        _write_tasks(d, _task("1"))
        (d / ".lock").write_bytes(b"")
        (d / ".highwatermark").write_bytes(b"19")
        plan = _plan(_bundle(_task("7")), [_task("1")], mode="replace")
        tasks.apply_plan(plan, d, tmp_path / "m.json")
        assert (d / ".lock").read_bytes() == b""
        assert (d / ".highwatermark").read_bytes() == b"19"


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #
class TestOnlyOpen:
    def test_only_open_excludes_completed(self):
        b = _bundle(_task("1", status="completed"), _task("2", status="in_progress"))
        plan = _plan(b, [], only_open=True)
        assert plan.id_map == {"2": "1"}
        assert [t["subject"] for t in plan.writes] == ["task 2"]

    def test_only_open_drops_refs_to_excluded_tasks(self):
        b = _bundle(_task("1", status="completed"), _task("2", blockedBy=["1"]))
        plan = _plan(b, [], only_open=True)
        assert plan.writes[0]["blockedBy"] == []
        assert plan.dropped_refs == [("2", "blockedBy", "1")]


# --------------------------------------------------------------------------- #
# CC schema conformance — a task that fails zod is dropped silently by CC
# --------------------------------------------------------------------------- #
class TestSchemaConformance:
    def test_write_fills_required_keys_for_cc_schema(self, tmp_path):
        """A bundle hand-authored without description/blocks/blockedBy must
        still produce files CC will accept."""
        b = {
            "version": tasks.BUNDLE_VERSION,
            "source_session_id": SID,
            "created": "2026-08-10T00:00:00Z",
            "tasks": [{"id": "1", "subject": "bare", "status": "pending"}],
        }
        plan = _plan(b, [])
        written = plan.writes[0]
        for k in tasks.REQUIRED_KEYS:
            assert k in written
        assert written["description"] == ""
        assert written["blocks"] == [] and written["blockedBy"] == []

        d = tmp_path / "dest"
        tasks.apply_plan(plan, d, tmp_path / "m.json")
        got, warns = tasks.read_tasks(d)
        assert len(got) == 1 and warns == []

    def test_plan_skips_task_with_invalid_status(self):
        b = _bundle(_task("1"), _task("2", status="cancelled"))
        plan = _plan(b, [])
        assert plan.id_map == {"1": "1"}
        assert any("status" in w for w in plan.warnings)

    def test_owner_is_stripped_on_import(self):
        """`owner` names an agent in the SOURCE session's team; it means
        nothing in the destination and would leave the task looking claimed."""
        b = _bundle(_task("1", owner="researcher"))
        plan = _plan(b, [])
        assert "owner" not in plan.writes[0]
        assert plan.stripped_owners == ["1"]

    def test_metadata_and_unknown_keys_survive_import(self):
        b = _bundle(_task("1", metadata={"k": "v"}, futureKey=[1]))
        plan = _plan(b, [])
        assert plan.writes[0]["metadata"] == {"k": "v"}
        assert plan.writes[0]["futureKey"] == [1]


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #
class TestSafety:
    def test_dry_run_writes_nothing(self, tmp_path):
        d = tmp_path / "dest"
        _write_tasks(d, _task("1"))
        manifest = tmp_path / "m.json"
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()}
        _plan(_bundle(_task("1")), [_task("1")])  # planning alone must not write
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()}
        assert before == after
        assert not manifest.exists()

    def test_source_dir_is_never_mutated(self, tmp_path):
        src = tmp_path / "src"
        _write_tasks(src, _task("1"), _task("5", blockedBy=["1"]))
        before = {p.name: p.read_bytes() for p in src.iterdir()}

        got, _ = tasks.read_tasks(src)
        b = tasks.make_bundle(SID, got)
        plan = _plan(b, [])
        tasks.apply_plan(plan, tmp_path / "dest", tmp_path / "m.json")

        after = {p.name: p.read_bytes() for p in src.iterdir()}
        assert before == after

    def test_apply_does_not_mutate_the_bundle(self):
        b = _bundle(_task("1"), _task("5", blockedBy=["1"]))
        snapshot = json.dumps(b, sort_keys=True)
        _plan(b, [_task("1"), _task("2")])
        assert json.dumps(b, sort_keys=True) == snapshot

    def test_write_is_atomic(self, tmp_path, monkeypatch):
        """A crash mid-apply must leave no partial <id>.json — CC would try to
        parse it, fail zod, and log a spurious error."""
        d = tmp_path / "dest"
        d.mkdir()
        plan = _plan(_bundle(_task("1"), _task("2"), _task("3")), [])

        real_replace = tasks.os.replace
        calls = {"n": 0}

        def boom(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real_replace(src, dst)

        monkeypatch.setattr(tasks.os, "replace", boom)
        with pytest.raises(OSError):
            tasks.apply_plan(plan, d, tmp_path / "m.json")

        for p in d.iterdir():
            if p.name.endswith(".json"):
                json.loads(p.read_text(encoding="utf-8"))  # never half-written
        assert not any(p.name.endswith(".tmp") for p in d.iterdir())

    def test_apply_creates_dest_dir_if_absent(self, tmp_path):
        d = tmp_path / "nested" / "dest"
        plan = _plan(_bundle(_task("1")), [])
        tasks.apply_plan(plan, d, tmp_path / "m.json")
        assert (d / "1.json").is_file()

    def test_apply_never_writes_into_the_cc_task_dir(self, tmp_path):
        """The manifest lives outside ~/.claude/tasks on purpose: CC's
        listTasks does NOT skip dotfiles, so a sidecar there would be read,
        fail zod, and log an error every list."""
        d = tmp_path / "dest"
        manifest = tmp_path / "elsewhere" / "m.json"
        tasks.apply_plan(_plan(_bundle(_task("1")), []), d, manifest)
        assert manifest.is_file()
        assert sorted(p.name for p in d.iterdir()) == ["1.json"]

    def test_empty_bundle_is_a_no_op_not_an_error(self, tmp_path):
        d = tmp_path / "dest"
        _write_tasks(d, _task("1"))
        plan = _plan(_bundle(), [_task("1")])
        assert plan.writes == [] and plan.id_map == {}
        tasks.apply_plan(plan, d, tmp_path / "m.json")
        assert sorted(p.name for p in d.iterdir()) == ["1.json"]
