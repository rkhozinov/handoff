"""Carry a Claude Code task list across `/hand:off` → `/clear` → `/hand:on`.

Pure functions over explicit paths, in the same spirit as `db.py`: no Claude
Code coupling in the logic, no global state, `~` only in module constants. The
CLI layer lives in `dbcli.py`.

`plan_import` (pure) is split from `apply_plan` (the only function that touches
the filesystem) so the merge logic is exhaustively testable without mocking a
filesystem — and so `--dry-run` is free: build the plan, render it, don't apply.

## What we know about CC's on-disk task store

Read out of the CC binary (2.1.226), not documented — treat it as a format that
can change under us:

- `listTasks` re-reads `<tasks-dir>/<list-id>/` on **every** call, so files
  written underneath a live session are picked up. No in-memory cache.
- Every file is validated against a zod schema; a task that fails is dropped
  **silently** (logged, never surfaced). Required: `id`, `subject`,
  `description`, `status` (one of `VALID_STATUSES`), `blocks`, `blockedBy`.
  Optional: `activeForm`, `owner`, `metadata`. That is why `_normalize` fills
  the required keys rather than trusting the bundle.
- `.highwatermark` holds the highest id ever *deleted*; CC allocates the next
  id as `max(max_file_id, highwatermark) + 1`. Allocating from
  `max(dest ids) + 1` therefore can never collide with a future CC id. We
  still never read or write it — same for `.lock`.
- CC wipes the whole list once every task in it is `completed`. Importing an
  all-completed bundle would restore tasks that vanish seconds later, so
  `/hand:on` imports open tasks only.
- `listTasks` does **not** skip dotfiles (only its clear path does), so a
  sidecar inside CC's directory would be read and fail validation on every
  list. The import manifest lives outside `~/.claude/tasks/` for that reason.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from handoff.lifecycle import now_iso

DEFAULT_TASKS_DIR = "~/.claude/tasks"
DEFAULT_BUNDLE_DIR = "~/.claude/compaction/tasks"
SESSION_DIR_PREFIX = "session-"
SHORT_ID_LEN = 8
CONTROL_FILES = frozenset({".lock", ".highwatermark"})
REQUIRED_KEYS = ("id", "subject", "description", "status", "blocks", "blockedBy")
VALID_STATUSES = frozenset({"pending", "in_progress", "completed"})
REF_FIELDS = ("blocks", "blockedBy")
BUNDLE_VERSION = 1
MANIFEST_VERSION = 1

MODE_MERGE = "merge"
MODE_REPLACE = "replace"

_SHORT_RE = re.compile(r"^[0-9a-f]{8}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# CC sanitises the list id before using it as a directory name.
_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def short_id(sid: str) -> str:
    """Full session UUID → the 8-hex key CC names the task dir with. An
    already-8-hex input passes through; anything else raises ValueError."""
    s = (sid or "").strip().lower()
    if _SHORT_RE.match(s):
        return s
    if _UUID_RE.match(s):
        return s[:SHORT_ID_LEN]
    raise ValueError(f"not a session id: {sid!r}")


def list_id(sid: str, override: str | None = None) -> str:
    """CC resolves the task-list id as `$CLAUDE_CODE_TASK_LIST_ID` → team name
    → `session-<short id>`, then sanitises it. `override` covers the first two
    cases, which we can't derive from the session id alone."""
    if override:
        return _UNSAFE_RE.sub("-", override)
    return SESSION_DIR_PREFIX + short_id(sid)


_LIST_ID = list_id  # tasks_dir takes a `list_id` kwarg, which shadows the name


def tasks_dir(
    sid: str, base: str | os.PathLike | None = None, list_id: str | None = None
) -> Path:
    """→ `<base>/<list id>`. Neither created nor required to exist."""
    root = Path(os.path.expanduser(str(base) if base is not None else DEFAULT_TASKS_DIR))
    return root / _LIST_ID(sid, list_id)


def bundle_path(sid: str, base: str | os.PathLike | None = None) -> Path:
    """→ `<bundle dir>/<full session id>.json`, next to the brief."""
    root = Path(os.path.expanduser(str(base) if base is not None else DEFAULT_BUNDLE_DIR))
    return root / f"{sid}.json"


def manifest_path(sid: str, base: str | os.PathLike | None = None) -> Path:
    """Import manifest for a destination session. Deliberately outside CC's
    task dir — see the module docstring."""
    root = Path(os.path.expanduser(str(base) if base is not None else DEFAULT_BUNDLE_DIR))
    return root / "imports" / f"{short_id(sid)}.json"


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def _validate_on_disk(task: object, where: str) -> str | None:
    """Reject exactly what CC's schema rejects. Returns a warning or None."""
    if not isinstance(task, dict):
        return f"{where}: not a JSON object"
    missing = [k for k in REQUIRED_KEYS if k not in task]
    if missing:
        return f"{where}: missing required key(s) {', '.join(missing)}"
    if task["status"] not in VALID_STATUSES:
        return f"{where}: invalid status {task['status']!r}"
    for f in REF_FIELDS:
        if not isinstance(task[f], list):
            return f"{where}: {f} is not a list"
    return None


def read_tasks(d: Path) -> tuple[list[dict], list[str]]:
    """→ (tasks sorted by numeric id, warnings). A missing directory is empty,
    not an error. Skips control files and every dotfile. One malformed file
    warns and is excluded; it never costs us the other 25."""
    tasks: list[dict] = []
    warnings: list[str] = []
    try:
        names = sorted(p.name for p in d.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return [], []
    except OSError as e:
        return [], [f"{d}: {e}"]

    for name in names:
        if name.startswith(".") or name in CONTROL_FILES or not name.endswith(".json"):
            continue
        try:
            task = json.loads((d / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            warnings.append(f"{name}: unreadable ({e})")
            continue
        problem = _validate_on_disk(task, name)
        if problem:
            warnings.append(problem)
            continue
        tasks.append(task)

    tasks.sort(key=lambda t: _numeric_id(t["id"]))
    return tasks, warnings


def _numeric_id(tid: object) -> int:
    try:
        return int(str(tid))
    except (TypeError, ValueError):
        return 0


def next_id(dest_tasks: list[dict]) -> int:
    """First id safe to allocate in the destination. Holes are never
    backfilled — CC allocates from `max(max_file_id, highwatermark) + 1`, and
    a hole may be an id something still references."""
    return max((_numeric_id(t.get("id")) for t in dest_tasks), default=0) + 1


# --------------------------------------------------------------------------- #
# bundle
# --------------------------------------------------------------------------- #
def make_bundle(sid: str, tasks: list[dict]) -> dict:
    """Export is lossless: every key of every task survives verbatim.
    Filtering happens at import time, not here."""
    return {
        "version": BUNDLE_VERSION,
        "source_session_id": sid,
        "created": now_iso(),
        "tasks": [dict(t) for t in tasks],
    }


# --------------------------------------------------------------------------- #
# import plan (pure)
# --------------------------------------------------------------------------- #
@dataclass
class ImportPlan:
    """Everything `apply_plan` needs, and everything `--dry-run` renders."""

    mode: str = MODE_MERGE
    source_session_id: str = ""
    writes: list[dict] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    id_map: dict[str, str] = field(default_factory=dict)
    dropped_refs: list[tuple[str, str, str]] = field(default_factory=list)
    stripped_owners: list[str] = field(default_factory=list)
    creation_order: list[str] = field(default_factory=list)
    manifest_entries: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def imported(self) -> int:
        return len(self.writes)


def manifest_key(source_sid: str, source_task_id: str) -> str:
    return f"{source_sid}::{source_task_id}"


def _normalize(task: object, warnings: list[str]) -> dict | None:
    """Make a bundle task safe for CC's validator, or reject it.

    Fills the keys CC requires but a hand-authored bundle may omit. Rejects
    only what can't be repaired: no id, no subject, or a status outside the
    enum (guessing there would silently change what the task means).
    """
    if not isinstance(task, dict):
        warnings.append("skipped a task that is not a JSON object")
        return None
    tid = str(task.get("id") or "").strip()
    if not tid:
        warnings.append("skipped a task with no id")
        return None
    if not str(task.get("subject") or "").strip():
        warnings.append(f"task {tid}: skipped — no subject")
        return None
    status = task.get("status", "pending")
    if status not in VALID_STATUSES:
        warnings.append(f"task {tid}: skipped — invalid status {status!r}")
        return None

    out = dict(task)
    out["id"] = tid
    out["status"] = status
    out.setdefault("description", "")
    for f in REF_FIELDS:
        refs = out.get(f)
        out[f] = [str(r) for r in refs] if isinstance(refs, list) else []
    return out


def _topological(writes: list[dict]) -> list[str]:
    """Blockers first, so a replay can create a task before anything blocks on
    it. Ties break on numeric id. Tolerates a cycle in a malformed source by
    appending whatever is left — every id comes back exactly once."""
    ids = [t["id"] for t in writes]
    pending = {t["id"]: [r for r in t["blockedBy"] if r in set(ids)] for t in writes}
    order: list[str] = []
    done: set[str] = set()
    while pending:
        ready = sorted(
            (tid for tid, deps in pending.items() if all(d in done for d in deps)),
            key=_numeric_id,
        )
        if not ready:  # cycle — emit the rest in id order and stop
            order.extend(sorted(pending, key=_numeric_id))
            break
        for tid in ready:
            order.append(tid)
            done.add(tid)
            del pending[tid]
    return order


def plan_import(
    bundle: dict,
    dest_tasks: list[dict],
    *,
    mode: str = MODE_MERGE,
    only_open: bool = True,
    already_imported: dict[str, str] | None = None,
) -> ImportPlan:
    """Compute the id remap, rewritten edges, dropped refs and skips. Pure —
    writes nothing, and never mutates `bundle` or `dest_tasks`.

    merge (default): destination tasks are never touched; imported tasks get
    fresh ids from `next_id(dest_tasks)` upward, in source id order.
    replace: bundle tasks keep their original ids and the destination's task
    files are cleared. Opt-in only — it is the one mode that loses data.
    """
    if mode not in (MODE_MERGE, MODE_REPLACE):
        raise ValueError(f"unknown mode: {mode!r}")

    source_sid = str(bundle.get("source_session_id") or "")
    seen = dict(already_imported or {})
    plan = ImportPlan(mode=mode, source_session_id=source_sid)

    candidates: list[dict] = []
    for raw in bundle.get("tasks") or []:
        task = _normalize(raw, plan.warnings)
        if task is None:
            continue
        if only_open and task["status"] == "completed":
            continue
        candidates.append(task)
    candidates.sort(key=lambda t: _numeric_id(t["id"]))

    # A source task already imported into this destination is a no-op, but it
    # stays in the id map so a sibling's edge to it still resolves.
    fresh: list[dict] = []
    for task in candidates:
        key = manifest_key(source_sid, task["id"])
        if mode == MODE_MERGE and key in seen:
            plan.id_map[task["id"]] = seen[key]
            plan.skipped += 1
            continue
        fresh.append(task)

    if mode == MODE_REPLACE:
        plan.deletes = [str(t.get("id")) for t in dest_tasks]
        for task in fresh:
            plan.id_map[task["id"]] = task["id"]
    else:
        nid = next_id(dest_tasks)
        for task in fresh:
            plan.id_map[task["id"]] = str(nid)
            nid += 1

    for task in fresh:
        src_id = task["id"]
        out = dict(task)
        out["id"] = plan.id_map[src_id]
        if "owner" in out:
            # Names an agent on the SOURCE session's team; in the destination
            # it would just make the task look claimed by nobody.
            del out["owner"]
            plan.stripped_owners.append(src_id)
        for f in REF_FIELDS:
            kept = []
            for ref in out[f]:
                if ref in plan.id_map:
                    kept.append(plan.id_map[ref])
                else:
                    plan.dropped_refs.append((src_id, f, ref))
            out[f] = kept
        plan.writes.append(out)
        plan.manifest_entries[manifest_key(source_sid, src_id)] = out["id"]

    plan.creation_order = _topological(plan.writes)
    return plan


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def read_manifest(path: Path) -> dict[str, str]:
    """`(source session, source task id) → dest task id` for every applied
    import. Missing or unreadable → empty, so a lost manifest costs
    idempotency, never the import."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    imports = data.get("imports") if isinstance(data, dict) else None
    return dict(imports) if isinstance(imports, dict) else {}


# --------------------------------------------------------------------------- #
# apply (the only effectful function)
# --------------------------------------------------------------------------- #
def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace. A crash mid-apply must not
    leave a half-written `<id>.json` for CC to choke on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".handoff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_plan(plan: ImportPlan, dest_dir: Path, manifest_path: Path) -> None:
    """Write the plan. Atomic per file. Control files are never touched."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if plan.mode == MODE_REPLACE:
        # Same filter CC's own clear path uses: *.json, dotfiles excluded.
        for p in sorted(dest_dir.iterdir()):
            if p.name.endswith(".json") and not p.name.startswith("."):
                p.unlink()

    for task in plan.writes:
        _atomic_write(dest_dir / f"{task['id']}.json", json.dumps(task, indent=2) + "\n")

    if not plan.manifest_entries:
        return
    manifest_path = Path(manifest_path)
    merged = read_manifest(manifest_path)
    merged.update(plan.manifest_entries)
    _atomic_write(
        manifest_path,
        json.dumps({"version": MANIFEST_VERSION, "imports": merged}, indent=2) + "\n",
    )
