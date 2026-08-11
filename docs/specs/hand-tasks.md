# Spec: `hand tasks` — carry the task list across `/hand:off` → `/hand:on`

**Status:** IMPLEMENTED (2026-08-11) — Plan A. See §3 for the Phase 0 result.
**Date:** 2026-08-10
**Target repo:** `~/repos/handoff` (public, personal)
**Approach:** spec-driven + TDD. Phase 0 is an experiment that can invalidate the design — run it first.

> **Implementation notes (2026-08-11).** Phase 0 passed: **Plan A**. Four
> corrections to what follows, all from reading CC's own task store rather
> than inferring it — the details live in `CLAUDE.md` § "Claude Code's task
> store":
>
> 1. §4.1's `REQUIRED_KEYS` was wrong. CC's zod schema also requires
>    `description`, `blocks` and `blockedBy`, and knows two keys this spec
>    never saw (`owner`, `metadata`). A task missing any required key is
>    dropped **silently** from `TaskList`, so import normalises rather than
>    trusting the bundle.
> 2. §2's "`.highwatermark` semantics are UNKNOWN" is resolved: it holds the
>    highest id ever *deleted*, and CC allocates `max(max_file_id, hwm) + 1`.
>    The "never touch it" rule stands and is now provably safe.
> 3. §4.3's `--only-open` is the **default** on import, not opt-in (`--all`
>    overrides). CC wipes a task list once every task in it is completed, so
>    an all-completed import would restore work that vanishes seconds later.
> 4. The task-list id is `$CLAUDE_CODE_TASK_LIST_ID` → team name →
>    `session-<8hex>`, so `tasks_dir()` gained a `list_id` override.
>
> §9's "`--dry-run` default in the slash command's first run" was dropped in
> favour of a direct import: merge never touches existing tasks and the
> manifest makes re-runs no-ops, so the blast radius doesn't justify the
> extra turn. `--dry-run` remains available on `/hand:tasks`.

---

## 1. Problem

`/hand:off` → `/clear` → `/hand:on` restores the *conversation* but not the
*task list*. Claude Code's `TaskCreate`/`TaskList` state is keyed to the session
id, and `/clear` mints a new one, so every handoff silently drops the todo
graph. In a long session that graph is often the single most compressed
representation of "what's left" — it survives compaction better than prose does,
and it's exactly what gets lost.

Today the only recovery is the agent re-reading the brief and re-creating tasks
by hand, which loses `blockedBy` edges and renumbers everything.

**Goal:** `/hand:on <sid>` restores the brief *and* the task list, with the
dependency graph intact.

---

## 2. Verified facts about the on-disk format

Established by inspection on 2026-08-10 (macOS, Claude Code current build).
Everything in this section is observed, not assumed — but it is *undocumented*,
so treat it as a format that can change under us (see §9 Risks).

**Location:** `~/.claude/tasks/session-<first-8-hex-of-session-uuid>/`

```
~/.claude/tasks/
├── session-9517a280/          # session 9517a280-4d3a-4634-b506-47a6f215767c
│   ├── 1.json
│   ├── 5.json
│   ├── 26.json
│   ├── .lock                  # 0 bytes
│   └── .highwatermark         # "2"
└── … 248 dirs total
```

- Every one of the 248 dirs matches `^session-[0-9a-f]{8}$`. No exceptions.
- Task files are `<id>.json`, ids are **decimal strings starting at 1**, and are
  **not contiguous** (deleted tasks leave holes — this session has no `2.json`).
- The only non-`*.json` entries anywhere across all 248 dirs are `.lock` (59
  occurrences, always 0 bytes) and `.highwatermark` (38 occurrences).

**Task schema** (all keys observed):

```json
{
  "id": "23",
  "subject": "Remove Yehia's Aiven/Timescale admin",
  "description": "MOSTLY DONE 2026-08-05 — all permissions revoked…",
  "activeForm": "Removing Aiven admin",
  "status": "in_progress",
  "blocks": [],
  "blockedBy": ["1"]
}
```

- `id`, `subject`, `status`, `blocks`, `blockedBy` present on every file sampled.
- `activeForm` is **not** universal — absent on some tasks. Treat as optional.
- `blocks` / `blockedBy` hold **id strings**, referencing siblings in the same
  dir. This is the whole reason a naive `cp -r` is wrong on merge.

**`.highwatermark` semantics are UNKNOWN.** Observed `hwm=2` in a dir holding 26
tasks, and `hwm=19` in a dir holding 48. So it is *not* a next-id counter. Most
likely a read/notification marker owned by the reminder system. **Design
consequence: never read it, never write it, never copy it.** Same for `.lock`.

---

## 3. Phase 0 — the experiment that gates the design (DO THIS FIRST)

The entire feature rests on one unverified assumption: **that a running Claude
Code session will pick up task files written underneath it.** If CC loads the
task dir once at session start and holds state in memory, writing files does
nothing (or worse, gets clobbered on the next `TaskUpdate`).

> **RESULT (2026-08-11): PASSED → Plan A.** Answered two ways.
>
> *Statically*, by reading the task store out of the CC binary (2.1.226):
> `listTasks` calls `readdir` on the list directory on **every** invocation
> and re-parses each file. There is no in-memory cache to clobber.
>
> *Live*, by the probe below: a 3-task bundle imported into a running
> session's dir appeared in the very next `TaskList` with its `blockedBy`
> edge intact; the pre-existing CC-created tasks were untouched; a
> subsequent `TaskUpdate` on one of those did not clobber the injected
> files (second probe → ids are file-keyed and safe); and a following
> `TaskCreate` allocated `max+1`, colliding with nothing. Deleting the
> probe tasks bumped `.highwatermark` to the highest deleted id, confirming
> the allocation model in §2.
>
> Plan B (§4.6) is therefore not built. `plan.creation_order` is still
> computed and tested so it stays cheap to reach for.

Run this manually before writing any implementation code.

```bash
# Terminal A: start a fresh CC session in a scratch dir, create one task,
# note its session id (echo $CLAUDE_SESSION_ID or /status).

# Terminal B:
SID=<the 8-hex prefix>
cat > ~/.claude/tasks/session-$SID/99.json <<'JSON'
{"id":"99","subject":"injected probe","description":"phase-0 probe",
 "activeForm":"probing","status":"pending","blocks":[],"blockedBy":[]}
JSON

# Terminal A: ask the agent to run TaskList.
```

| Outcome | Meaning | Design |
|---|---|---|
| Task 99 appears | Dir is re-read per call | **Plan A** — file-level import, §4 |
| Task 99 absent | State is cached in memory | **Plan B** — replay via `TaskCreate`, §4.6 |
| 99 appears then vanishes after a `TaskUpdate` | CC rewrites the dir from memory | **Plan B**, mandatory |

Second probe, only if the first passed — does an id collision corrupt anything?
Inject a file reusing an id that already exists in the live session, then have
the agent `TaskUpdate` a different task, and re-list. If the injected task
survives, ids are file-keyed and safe. If not, the merge must never reuse ids
(which §4.3 already guarantees, but this confirms it).

**Record the result at the top of the implementation PR.** The rest of this spec
assumes Plan A and specifies Plan B as the fallback; both share the same core
module, so Phase 0 does not block writing §4.1–§4.4.

---

## 4. Design

### 4.1 Module split

Follow the existing `db.py` (pure, path-parameterised) / `dbcli.py` (thin CLI,
machine-readable stdout) separation. Same reasoning applies here.

- **`handoff/tasks.py`** — new. Pure functions over explicit paths. Zero Claude
  Code coupling, zero global state, no `~` defaults baked into the logic (only
  into module-level constants). Fully testable with `tmp_path`.
- **`handoff/dbcli.py`** — add a `tasks` subparser with `export` / `import` /
  `copy` / `list` actions, mirroring the existing `do_*` helper style.
- **`commands/tasks.md`** — new `/hand:tasks` slash command.
- **`commands/off.md`, `commands/on.md`** — wire in the export/import steps.

Constants (no magic literals — CLAUDE.md rule):

```python
DEFAULT_TASKS_DIR   = "~/.claude/tasks"
DEFAULT_BUNDLE_DIR  = "~/.claude/compaction/tasks"   # handoff-owned
SESSION_DIR_PREFIX  = "session-"
SHORT_ID_LEN        = 8
CONTROL_FILES       = frozenset({".lock", ".highwatermark"})
REQUIRED_KEYS       = ("id", "subject", "status")
```

### 4.2 Public surface of `handoff/tasks.py`

```python
def short_id(sid: str) -> str
    """Full UUID → 8-hex key. An already-8-hex input passes through.
    Anything else raises ValueError."""

def tasks_dir(sid: str, base: str | os.PathLike | None = None) -> Path
    """→ <base>/session-<short_id>. Does not create or require it."""

def read_tasks(d: Path) -> tuple[list[dict], list[str]]
    """→ (tasks sorted by numeric id, warnings). Missing dir → ([], []).
    Skips CONTROL_FILES and any dotfile. Malformed JSON → warning, not raise."""

def make_bundle(sid: str, tasks: list[dict]) -> dict
    """→ {version, source_session_id, created, tasks: [...]}"""

def plan_import(bundle, dest_tasks, *, mode, only_open, already_imported) -> ImportPlan
    """PURE. Computes id remap, rewritten edges, dropped refs, skips.
    Writes nothing. This is where all the logic under test lives."""

def apply_plan(plan: ImportPlan, dest_dir: Path, manifest_path: Path) -> None
    """The only function that touches the filesystem. Atomic per file."""
```

Splitting `plan_import` (pure) from `apply_plan` (effectful) is what makes the
merge logic exhaustively testable without mocking a filesystem — and it makes
`--dry-run` free: build the plan, render it, don't apply.

### 4.3 Merge semantics — the core logic

**`--merge` (default).** Destination tasks are never touched. Imported tasks get
fresh ids allocated from `max(numeric ids in dest) + 1` upward, in source id
order. Then:

- every `blockedBy` / `blocks` entry is rewritten through the id map;
- a reference to a source id **not in the bundle** (filtered out by
  `--only-open`, or already dangling at export time) is **dropped from the
  array and reported** in `plan.dropped_refs`. Never silently.
- a reference to a *destination* task is impossible by construction — bundles
  only ever reference their own source ids. Don't invent cross-session edges.

Worked example — dest holds ids `1,2`; bundle holds `1,5,7` where `7.blockedBy=["5"]`
and `5.blockedBy=["9"]` (9 not in bundle):

```
map: 1→3, 5→4, 7→5
writes: 3.json, 4.json (blockedBy [] + dropped_ref 5→9), 5.json (blockedBy ["4"])
```

**`--replace`.** Every `*.json` in the destination is deleted, then bundle tasks
are written with their **original ids**. `.lock` and `.highwatermark` are left
exactly as they are. Requires the explicit flag — never the default.

**Idempotency.** A manifest at
`~/.claude/compaction/tasks/imports/<dest-short>.json` records
`(source_session_id, source_task_id) → dest_task_id` for every applied import.
Re-importing the same bundle is a no-op with `skipped=N`.

> The manifest deliberately lives **outside** `~/.claude/tasks/`. A sidecar
> dotfile inside CC's dir would be invisible to Python's `glob("*.json")` but
> *would* match a naive Node `readdir().filter(f => f.endsWith('.json'))`. We
> don't control CC's loader, so we don't put anything in its directory. Same
> reason we never write `.highwatermark`.

**`--only-open`.** Excludes `status == "completed"` from the bundle at import
time (not export — export is lossless). Interacts with dropped refs above.

### 4.4 CLI contract

Machine-readable first token on stdout, matching `HANDOFF_OK` / `HANDDONE_OK`
house style. Slash-command bash blocks branch on these; humans read the rest.

```bash
hand tasks list   <sid>
hand tasks export <sid> [--out FILE]           # default: <BUNDLE_DIR>/<sid>.json
hand tasks import <bundle> --to <sid> [--merge|--replace] [--only-open] [--dry-run]
hand tasks copy   --from <sid> --to <sid> [same flags]     # export|import in one
```

```
HANDTASKS_OK op=import source=9517a280 dest=a1b2c3d4 imported=12 skipped=0 dropped_refs=1 mode=merge
HANDTASKS_DRYRUN op=import … (nothing written)
HANDTASKS_EMPTY sid=9517a280 (no tasks to export)
HANDTASKS_ERROR reason=no-such-session sid=deadbeef
```

Exit codes: `0` ok / empty / dry-run, `1` error. `HANDTASKS_EMPTY` is **not** an
error — a session with no tasks is normal and must not break `/hand:on`.

### 4.5 Wiring into the existing flow

**`off.md`** — new step after the handoff block, non-fatal:

```bash
hand tasks export "$SID" >/dev/null 2>&1 && echo "  tasks:      exported"
```

Failure here must never turn a successful `HANDOFF_OK` into an error. The brief
is the primary artifact; tasks are a bonus.

**`on.md`** — after the brief Read, for each restored sid:

```bash
hand tasks import "$HOME/.claude/compaction/tasks/<sid>.json" \
  --to "$CLAUDE_SESSION_ID" --merge
```

Multi-brief restore (`/hand:on <a> <b>`) already stacks briefs; task import must
stack too — merge mode with per-source manifest keys makes this work without
collision. Add a test for it.

Report one line: `Tasks: 12 restored from 9517a280 (1 blocked-by ref dropped)`.

### 4.6 Plan B — replay via `TaskCreate` (if Phase 0 fails)

If CC won't re-read the dir, the file-level import is dead for *live* sessions
and the design degrades to: `hand tasks export` still produces the bundle, and
`/hand:tasks` renders it as an ordered instruction list that the agent replays
through real `TaskCreate` / `TaskUpdate addBlockedBy` calls.

Slower and consumes tokens, but **schema-proof** — it goes through the public
tool interface, so it cannot break when CC changes its on-disk format.

Everything in §4.2/§4.3 is reused unchanged: the id remap is exactly what a
replay needs to emit `addBlockedBy` in the right order. Only `apply_plan` is
bypassed. Build the core first; the fork costs nothing.

Topological ordering is required for replay (a task must exist before something
blocks on it) — `plan_import` should expose `plan.creation_order` regardless of
which plan wins, and it should be tested either way.

---

## 5. TDD plan

Red → green, in this order. Each bullet is one test; write it failing first.
Run with `PYTHONPATH=. python3 -m pytest tests/ -q`.

### `tests/test_tasks.py` — core (no CLI, no CC)

**Resolution**
1. `test_short_id_truncates_full_uuid`
2. `test_short_id_passes_through_8_hex`
3. `test_short_id_rejects_garbage` — `""`, `"nope"`, `"9517"` → `ValueError`
4. `test_tasks_dir_composes_prefix`

**Reading**
5. `test_read_missing_dir_returns_empty` — no raise
6. `test_read_empty_dir_returns_empty`
7. `test_read_ignores_lock_and_highwatermark`
8. `test_read_ignores_arbitrary_dotfiles`
9. `test_read_sorts_numerically_not_lexically` — ids `2,10` must come back `2,10`, not `10,2`
10. `test_read_preserves_unknown_keys` — a task with a future key survives round-trip
11. `test_read_tolerates_missing_activeForm`
12. `test_read_malformed_json_warns_and_continues` — one bad file doesn't lose the other 25
13. `test_read_rejects_task_missing_required_key` — warning, excluded from list

**Bundle**
14. `test_bundle_roundtrip_is_lossless` — `read → make_bundle → json → parse` preserves every field of every task
15. `test_bundle_records_source_sid_and_version`

**Merge / remap — the meat**
16. `test_merge_into_empty_dest_starts_at_1`
17. `test_merge_into_nonempty_dest_starts_after_max_id` — dest `1,2,7` → first new id `8`, *not* `3`
18. `test_merge_rewrites_blockedBy_through_map`
19. `test_merge_rewrites_blocks_through_map`
20. `test_merge_drops_dangling_ref_and_records_it` — `plan.dropped_refs` non-empty
21. `test_merge_preserves_dest_tasks_untouched`
22. `test_merge_is_idempotent_via_manifest` — second `plan_import` yields `imported=0, skipped=N`
23. `test_merge_two_different_sources_into_one_dest` — the `/hand:on <a> <b>` case
24. `test_creation_order_is_topological` — a blocker always precedes its dependent

**Replace**
25. `test_replace_clears_dest_json_files`
26. `test_replace_keeps_original_ids`
27. `test_replace_leaves_control_files_untouched` — assert `.lock` + `.highwatermark` byte-identical after

**Filtering**
28. `test_only_open_excludes_completed`
29. `test_only_open_drops_refs_to_excluded_tasks`

**Safety**
30. `test_dry_run_writes_nothing` — snapshot dest dir listing + mtimes before/after
31. `test_source_dir_is_never_mutated` — hash the source dir before and after every op
32. `test_write_is_atomic` — simulate failure mid-apply; no partial `.json` left behind
33. `test_apply_creates_dest_dir_if_absent`

### `tests/test_tasks_cli.py` — CLI layer

Mirror `tests/test_dbcli.py`: build a `tmp_path` fixture, call
`dbcli.main([...])`, assert on `rc` and `capsys` tokens.

34. `test_export_prints_ok_and_writes_bundle`
35. `test_export_empty_session_prints_HANDTASKS_EMPTY_rc0`
36. `test_import_prints_counts`
37. `test_import_dry_run_prints_DRYRUN_and_writes_nothing`
38. `test_copy_end_to_end`
39. `test_unknown_session_prints_ERROR_rc1`
40. `test_replace_requires_explicit_flag` — merge is the default, verify it

### Fixtures

**Synthetic only.** This repo is public (`CLAUDE.local.md` publish hygiene). Do
not copy real task JSON from `~/.claude/tasks/` into `tests/` — the real
subjects name employer systems and colleagues. Build tasks in-test with a
`_task(id, **over)` helper, the way `test_dbcli.py` builds frontmatter with
`_fm(**over)`.

---

## 6. Acceptance criteria

- [ ] Phase 0 result recorded; Plan A or Plan B chosen explicitly.
- [ ] All tests above green; existing ~150 tests still green.
- [ ] `scripts/bench.py` invariant unaffected (this feature doesn't touch the trimmer).
- [ ] Manual end-to-end: session with ≥3 tasks including a `blockedBy` edge →
      `/hand:off` → `/clear` → `/hand:on <sid>` → `TaskList` shows all tasks
      **with the edge intact**.
- [ ] Manual: `/hand:on` into a session that *already* has tasks → both sets
      present, no id collision, no clobbered edges.
- [ ] Manual: `/hand:on` for a session with zero tasks → no error, brief still restores.
- [ ] `hand tasks import` run twice → second is a no-op.
- [ ] `CLAUDE.md` "Module layout" gains a `handoff/tasks.py` entry; the
      `/hand:off` and `/hand:on` contract sections mention task carry-over.
- [ ] `README.md` architecture diagram updated (tasks travel with the brief).
- [ ] `.claude-plugin/plugin.json` version bumped; description mentions tasks.

---

## 7. Out of scope (v1)

- Syncing tasks *back* from a resumed session to the original brief.
- Any UI for tasks in the Textual TUI.
- Merging task *content* (dedup by subject, conflict resolution). Merge is
  id-level only; two similar tasks stay two tasks.
- Cross-machine sync. Same single-machine assumption as `sessions.db`.
- Storing tasks inside `sessions.db`. The bundle is a plain JSON file next to
  the brief, for the same reason briefs are `.md`: recoverable without the DB.

---

## 8. Rollback

Nothing here mutates existing handoff state. Reverting = drop `handoff/tasks.py`,
the `tasks` subparser, `commands/tasks.md`, and the two wiring lines in
`off.md`/`on.md`. Bundles under `~/.claude/compaction/tasks/` are inert files;
delete or leave them.

A bad *import* is recoverable in merge mode (delete the `<id>.json` files the
manifest lists) but **not** in replace mode. Hence replace being opt-in.

---

## 9. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| CC caches task state in memory (Phase 0 fails) | High | Plan B replay, §4.6 — costs tokens, gains schema-independence |
| CC changes the on-disk task format | Medium | Format is undocumented and unversioned. Bundle carries `version`; `read_tasks` warns rather than crashes on unknown shapes; Plan B is immune |
| 8-hex short-id collision across 248+ dirs | Low | Birthday bound is small but nonzero, and the dir name carries no full sid to disambiguate. This is CC's scheme, not ours — accept, document |
| Writing while CC holds `.lock` | Medium | Never touch `.lock`. If Phase 0 shows racing is possible, add a "import only into a session with no in-flight task writes" note rather than implementing lock protocol |
| Import corrupts an active session's task list | Medium | `--dry-run` default in the slash command's first run; merge never reuses an existing id |

---

## 10. Execution order

1. **Phase 0 experiment** (§3) — 15 min, decides Plan A vs B.
2. `handoff/tasks.py` + `tests/test_tasks.py` — tests 1–33, TDD, no CLI yet.
3. `dbcli` subparser + `tests/test_tasks_cli.py` — tests 34–40.
4. `commands/tasks.md` (`/hand:tasks`), then wire `off.md` / `on.md`.
5. Manual acceptance run (§6).
6. Docs: `CLAUDE.md`, `README.md`, `plugin.json` bump.

Steps 2–3 are pure and independent of Phase 0's outcome — start there if the
experiment has to wait.
