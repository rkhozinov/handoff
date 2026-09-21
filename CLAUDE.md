# handoff — Claude Code instructions

Read README.md first for project intent. This file = working notes for
Claude sessions in this repo.

## Architecture (single-brief, post-2026-05)

- One output file per /hand:off: `~/.claude/compaction/<session_id>.md`.
- Header (session, cwd, archive hash) + anti-re-read notice + trimmed
  conversation. **No tier1/tier2 split** — that was ripped on `a377986`
  after empirical review showed the trimmed convo more useful than the
  extracted-sections summary. Don't reintroduce tiers without strong
  evidence.
- The trimmer keeps verbatim: every signal user msg, every substantive
  assistant text turn (capped at `ASSISTANT_TURN_MAX_CHARS = 4_000`),
  every code fence, every Read marker, every sub-agent report.
- Drops: tool_result bodies, thinking blocks, narration ("let me check"),
  noise user msgs (acks, skill bodies, prior compaction continuations,
  `<task-notification>` async pings).

## Hard invariant

`signal_kept == user_signal` (100%) across all fixtures. Enforced by
`scripts/bench.py`. If a trimmer change drops a real user msg, that's a
regression — fix the filter, don't relax the invariant.

## Module layout

- `handoff/extract.py` — pure extraction helpers over JSONL entries
  (`is_real_user`, `user_text`, `assistant_blocks`, `iter_*_user_msgs`,
  `short_tool_input`, `extract_agent_reports`, `extract_decisions/...`
  used by bench/report stats only).
- `handoff/trim.py` — `render_brief(entries, sid, cwd, archive_hash)`.
  `build_convo` exposed for the report's audit panel.
- `handoff/cli.py` — the snapshot pipeline: `run(args) -> OffResult`
  (load_jsonl → archive → render → atomic write → upsert DB row;
  brief is written BEFORE the best-effort agent-report store) and a thin
  `main` for direct use. `dbcli.do_off` calls `run` in-process.
- `handoff/archive.py` — trims the transcript and stores it as a
  `session-archive` memory doc, then prunes old ones. `/hand:off` is the only
  producer of these (~12/day) and nothing removed them, so the doc store had
  reached 689 archives / 52.9 MB. `prune_archives()` deletes archives older
  than `days` (30) unless their brief is still open, and an open brief only
  counts while it has been touched within `open_days` (90) — `in_progress` is
  set by `/hand:on` and cleared only by `/hand:done`, so it never decays on its
  own, and treating it as permanent protection would pin 56% of archives
  forever. Liveness is `last_resumed` falling back to `created`.
  Pruning is MANUAL: `hand prune-archives [--days N] [--open-days N]` reports;
  `--apply` deletes (soft — memory's 30-day purge window is the undo). It
  used to run automatically on every `/hand:off` (`maybe_prune_archives`,
  daily throttle, 50/run); removed 2026-09-21 under the ground rule below.
- `handoff/db.py` — SQLite session index (`~/.claude/compaction/sessions.db`,
  WAL, one row per session: all frontmatter fields + trimmed `body`).
  `connect`/`upsert_session`/`get_session`/`list_sessions`/`list_idle`/`list_holds`/`search_sessions`/
  `set_status`/`set_resumed`/`delete_session`/`rebuild_from_briefs`. Brief
  `.md` files stay authoritative; DB mirrors them and is rebuildable.
- `handoff/dbcli.py` — `hand` CLI + the backend the /hand:* command bash
  blocks call. `done`/`on`/`list`/`show`/`search`/`rm`/`rebuild`/`tui`/
  `tasks {list,export,import,copy}`.
  Every mutation edits the brief frontmatter file AND the DB row in one
  process (`do_done`/`do_resume`/`do_delete`) so they never drift.
- `handoff/tui.py` — Textual 2-pane TUI (optional `[tui]` extra,
  lazy-imported). Left list pane, right scrollable brief. Reads the DB;
  mutating keys route through `dbcli.do_*`.
- `handoff/tasks.py` — task carry-over across `/hand:off` → `/hand:on`.
  Pure, path-parameterised, zero Claude Code coupling: `short_id`/`list_id`/
  `tasks_dir`/`bundle_path`/`manifest_path` resolve paths, `read_tasks`
  reads CC's dir, `make_bundle` exports, `plan_import` computes the id
  remap + rewritten edges + dropped refs, `apply_plan` is the ONLY
  effectful function (atomic per file). Keep that split — it's what makes
  `--dry-run` free and the merge logic testable without a fake filesystem.
- `handoff/recall.py` — `project_tag_from_cwd` + `store_agent_reports`.
  That's it. Older `build_query`/`search_memories`/`format_memory_line`
  were ripped (no callers post-tier1).
- `handoff/tokenizer.py` — pluggable `count_tokens(text, mode)`:
  `chars4` (default fallback), `hf` (Xenova/claude-tokenizer), `api`
  (Anthropic SDK), `auto` (HF if importable else chars4). Lazy imports —
  don't hoist `transformers`/`anthropic` to module level.
- `handoff/fsutil.py` — `atomic_write(path, text)` (temp file + `os.replace`).
  Every authoritative file — briefs, task `<id>.json`, bundles, manifests —
  goes through it; a truncated brief resets `created`/`last_resumed` on the
  next `/hand:off`.
- `handoff/__init__.py` — empty marker.
- `scripts/bench.py`, `scripts/render_html.py` — dev tools, not shipped
  via `/hand:off`.

## Don't reintroduce

- `handoff/segment.py` was deleted (B2 idea, never wired).
- `tier1`/`tier2` vocabulary in docstrings, comments, or symbols.
- `**_legacy_kwargs` shims on `render_brief`.
- Magic literals for the agent-report cutoff — use `AGENT_REPORT_MIN_CHARS`.
- Filesystem access inside `tasks.plan_import`. It is pure on purpose;
  `apply_plan` is the only effectful function in that module.
- Task ids reused from holes in the destination. Merge allocates strictly
  above `max(dest ids)` — a hole may be an id something still references.

## Magic constants worth knowing

- `extract.AGENT_REPORT_MIN_CHARS = 200` — sub-agent reports below this
  are dropped (stub noise).
- `extract.PASTED_PRESERVE_CHARS = 200` — terminal-output paste elision.
- `extract.DEFAULT_TOOL_VALUE_LIMIT = 100`, `TOOL_VALUE_LIMITS["Bash"] = 60`.
- `trim.ASSISTANT_TURN_MAX_CHARS = 4_000`.
- `tasks.REQUIRED_KEYS` / `VALID_STATUSES` — mirror CC's zod schema. Don't
  trim them to the subset the spec originally guessed; a task missing
  `description` is invisible in `TaskList`.
- `tasks.CONTROL_FILES = {".lock", ".highwatermark"}` — read-never,
  write-never.
- `_ANTHROPIC_MODEL = "claude-opus-4-7"` in tokenizer.py — bump when
  newer model lands.

## Test + bench workflow

```bash
PYTHONPATH=. python3 -m pytest tests/ -q          # ~360 tests
PYTHONPATH=. python3 scripts/bench.py             # invariant check across 6 fixtures
PYTHONPATH=. python3 scripts/render_html.py       # docs/report.html (gitignored)
```

End-to-end smoke:

```bash
PYTHONPATH=. python3 -m handoff.cli \
  --transcript tests/fixtures/raw/small.jsonl \
  --session-id smoke --cwd /tmp \
  --no-archive --no-db --out-dir /tmp/smoke
```

Mutation-checking gotcha (bit us 2026-09-21): a mutant that keeps the same
byte length (`rows[:10]` → `rows[:12]`) restored within the same second is
NOT recompiled — Python's pyc check is mtime+size — so the suite keeps
running the mutant. `find . -name __pycache__ -prune -exec rm -rf {} +`
after restoring, or run pytest with `-p no:cacheprovider -B` …
`PYTHONDONTWRITEBYTECODE=1` during mutation runs.
Restore a mutant by copying the saved file back, never by
string-replacing the mutation in reverse: `s.replace(new, old)` hits every
other occurrence of `new` too (2026-09-21: turned the `auto-todowrite`
return into `auto-tasks` and pasted a condition into four `last_msgs`).

`tests/fixtures/raw/` is gitignored (PII). Fixture-dependent tests skip
when raw fixtures are absent.

## /hand:off and /hand:on contracts

The command files under `commands/` are one-call stubs: description + a
single `hand …` invocation + what to do with its stdout. All logic lives in
`handoff/dbcli.py` behind `tests/test_dbcli_off_on.py`, whose expected
strings were captured verbatim from the old bash blocks on 2026-09-21 —
change the output shape there first, never in the `.md`.

- `/hand:off` (`commands/off.md`) → `hand off "$CLAUDE_SESSION_ID" --cwd
  "$(pwd -P)" --recap-stdin <<'EOF' … EOF`. `dbcli.do_off` locates
  `<projects>/*/<sid>.jsonl` by session id alone (the shell cwd can drift
  into a worktree; the transcript's own `cwd` wins), runs `cli.run`
  in-process, exports the task bundle best-effort (a failure there is
  `tasks: skipped`, never an error), and prints the `HANDOFF_OK` block +
  restore/park hints, or `HANDOFF_ERROR …` rc 1. The recap arrives on stdin
  from a quoted heredoc, so there is no shell-quoting rule for the LLM to
  follow (the old `RECAP='…'` was both a syntax trap and command execution).
- `/hand:hold` (`commands/hold.md`) → `hand off … --hold "<note>" [--until D]`,
  or `hand hold <sid> --note …` when the first argument is an existing
  session id. Six lines of argument routing in bash, nothing else.
- `/hand:on` (`commands/on.md`) → `hand on --restore --current
  "$CLAUDE_SESSION_ID" $ARGUMENTS`. `dbcli.do_on_restore` resolves each
  token (path, then `<compaction>/<sid>.md`), prints one
  `BRIEF_PATH=`/`BRIEF_STATUS=` pair per hit and `BRIEF_MISSING arg=<x>`
  per miss, flips each hit via `do_resume` (`done`/`archived` are left
  alone and say so), merges its task bundle into the current session
  (`--merge --only-open`; a missing bundle is silent), and — only when
  nothing resolved — prints the picker from the DB (`created DESC`, max 10,
  `done`+`archived` hidden unless `--all`; the old `ls -t`+awk picker
  ordered by mtime and showed archived). The `.md` then tells Claude to
  Read every `BRIEF_PATH` before speaking. Bare `/hand:on` only matches the
  current session id; `/clear` mints a new one, so pass the printed id.
- `/hand:done <sid> [--reopen]` (`commands/done.md`) — manual status flip,
  `completion_signal: manual`, sticky: a later `/hand:off` will NOT
  auto-revive it.
- `/hand:list [--all] [--any-cwd]` (`commands/list.md`) — grouped by
  status, current cwd, done hidden by default.
- `/hand:hold` / `/hand:holds` — see "on_hold" below; `/hand:review` — see
  "Review" below.
- `/hand:tasks` (`commands/tasks.md`) — manual door into the same
  export/import machinery `/hand:off` and `/hand:on` drive automatically.

## Claude Code's task store (undocumented — verified, not assumed)

Read out of the CC binary (2.1.226) on 2026-08-11 and confirmed by a live
injection probe. `handoff/tasks.py` is built on these five facts; re-derive
them before assuming any of it still holds on a newer CC.

1. **`listTasks` re-reads `<tasks-dir>/<list-id>/` on every call.** No
   in-memory cache, so files written under a live session are picked up
   immediately. This is what makes file-level import viable at all — the
   spec's Plan B (replay via `TaskCreate`) is unnecessary. `plan_import`
   still exposes `creation_order` (topological) so Plan B stays cheap to
   reach for if this ever changes.
2. **Every file is validated against a zod schema and a failure is dropped
   SILENTLY** (logged, never surfaced). Required: `id`, `subject`,
   `description`, `status` ∈ {pending, in_progress, completed}, `blocks`,
   `blockedBy`. Optional: `activeForm`, `owner`, `metadata`. A task written
   without `description` simply vanishes from `TaskList` with no
   diagnostic — hence `_normalize` fills the required keys rather than
   trusting the bundle. Unknown keys are stripped by zod on read but
   survive our round-trip.
3. **`.highwatermark` is the highest id ever _deleted_.** CC allocates the
   next id as `max(max_file_id, highwatermark) + 1`. Allocating from
   `max(dest ids) + 1` therefore can never collide with a future CC id.
   We still never read or write it — same for `.lock`.
4. **CC wipes the whole list once every task in it is `completed`** (a
   background timer). That's why import skips completed tasks by default:
   restoring a finished list would restore work that vanishes seconds
   later.
5. **The list id is `$CLAUDE_CODE_TASK_LIST_ID` → team name →
   `session-<sid[:8]>`**, sanitized with `[^a-zA-Z0-9_-] → "-"`. An
   agent-team session renames its dir to the team name, so `tasks_dir()`
   takes a `list_id` override — don't hardcode the `session-` form.

`listTasks` does NOT skip dotfiles (only its clear path does), so a sidecar
inside CC's dir would be read and fail validation on every list. The import
manifest lives at `~/.claude/compaction/tasks/imports/<dest-short>.json`,
outside CC's directory, for exactly that reason. **Never write anything
into `~/.claude/tasks/` that isn't a valid `<id>.json`.**

## Session lifecycle (frontmatter)

Every brief carries a YAML frontmatter block written by `render_brief`:

```yaml
---
status: in_progress       # pending | in_progress | done | archived | on_hold
title: <CC ai-title from transcript, or null>
session_id: <sid>
cwd: <abs path>
created: <iso8601>
last_resumed: <iso8601 or null>
completion_signal: auto-tasks | auto-todowrite | auto-user-msg | auto-open-q | auto-default | manual | backfill-*
archive_hash: <memory doc hash>
recap: <one-line session summary or null>
recap_source: llm | extracted | null
hold_note: <why parked / what next, or null>
hold_until: <YYYY-MM-DD or null>
---
```

Detector lives in `handoff/lifecycle.py:detect_status`. Precedence
(first match wins):

0. `auto-tasks`     — session's task dir: all tasks completed → `done`;
   any open task vetoes `auto-user-msg` below
1. `auto-todowrite` — last TodoWrite call: all entries completed → `done`
2. `auto-user-msg`  — any of last 3 user msgs match completion regex → `done`
3. `auto-open-q`    — final user msg looks like a question → `pending`
4. `auto-default`   — fallback → `in_progress`

A message that reads as a question (`?` suffix or a wh-/aux-verb prefix)
never counts as completion, even if it contains a keyword — "is it fixed?"
was classified `done` before 2026-09-21 (`_looks_like_question`).

**Conservative bias:** uncertainty → `in_progress`, NEVER `done`.
False-`done` hides briefs from `/hand:on` (bad); false-`in_progress`
just clutters the picker (mild). Don't loosen the regex without strong
evidence.

`/hand:off` re-runs are idempotent: `created` and `last_resumed` are
preserved from the existing brief; `status` is re-detected unless the
current value is a manual `done` / `archived` / `on_hold`
(`STICKY_MANUAL_STATUSES` — then the user wins).

## on_hold — the "come back later" shelf

`in_progress` is not a shelf: the live DB had 384 of them, most abandoned.
`on_hold` is an explicit, curated park with a reason and an optional
deadline. It is a **status** (not a flag) because every filter — `hand
list`, the `/hand:on` picker, `is_stale`, `archive._open_archive_hashes`,
TUI icons — keys on status.

- `/hand:hold <note> [--until YYYY-MM-DD]` = `hand off … --hold` (recap,
  brief, archive, task bundle, then the hold). Guarantees a brief exists.
  `/hand:hold <sid> <note>` holds an existing brief without re-snapshotting.
- `/hand:holds [--due]` (`hand holds`) is the retrieval view, all cwds,
  soonest deadline first, each row with a paste-ready
  `cd <cwd> && claude --resume <sid>   |  /hand:on <sid>`. `--due` prints
  nothing when nothing is due — `hooks/holds-due.sh` runs it on
  `SessionStart` so an overdue hold nags at the next session start.
- `/hand:on <sid>` releases a hold: status → `in_progress`, `hold_until`
  cleared, `hold_note` kept as history. `hand hold <sid> --release` does the
  same without restoring.
- Held briefs never show up in `hand review` (status gate) and their
  archives are kept by `prune_archives` regardless of `open_days`
  (`ALWAYS_KEEP_STATUSES`) — an explicit hold is the user saying "I will
  come back".
- `extract_title` prefers the LAST `custom-title` entry (user-set via
  `/rename`, what `claude --resume "<name>"` accepts) over `ai-title`.
- `scripts/import_onhold.py` is the one-off migration from a hand-kept
  `claude --resume …` list. Measured 2026-09-21: 6 of 17 names mapped to
  2–3 session ids (a resumed session re-writes `custom-title` with its own
  `sessionId`), so an explicit `/hand:on <sid>` on the line wins, else the
  newest transcript; the rest are reported as `others`.

## Recap + session DB

- `recap` is the ONE non-deterministic field: `/hand:off` (off.md)
  instructs session Claude to compose a 1–2 sentence
  `Goal → current → next` line and passes it via `--recap`
  (`recap_source: llm`). Without `--recap`, the CLI falls back to
  `lifecycle.extract_recap` — first signal user msg + first open todo
  (`recap_source: extracted`). Sanitized by `sanitize_recap`
  (single line, `RECAP_MAX_CHARS = 300`).
- Precedence in `resolve_frontmatter`: `--recap` > existing llm recap >
  fresh extracted > existing extracted. An llm recap is NEVER
  downgraded to extracted on re-run.
- `title` comes from `extract.extract_title` — the LAST `ai-title`
  entry in the transcript (CC's own session title, deterministic).
  Shown as the TUI/list heading; cwd alone is useless when most
  sessions share one repo dir.
- `cli.py` upserts one row per session into the SQLite index
  (`handoff/db.py:upsert_session`, keyed on `session_id`): all
  frontmatter fields + the frontmatter-stripped `body` + `brief_path` +
  `indexed_at`. `--no-db` skips it (testing). Re-runs `INSERT OR
  REPLACE`, never duplicate. This **replaced** the old
  `sessions.log.md` markdown log (`handoff/sessionlog.py`, deleted) —
  don't reintroduce a markdown log.
- The DB is a derived mirror: brief `.md` files are authoritative
  (what `/hand:on` Reads back). `hand rebuild` /
  `python3 -m handoff.dbcli rebuild` repopulates the DB from the briefs
  and drops rows with no backing file — the migration + self-heal path.
- `/hand:list`, `/hand:on`, `/hand:done` all go through
  `handoff.dbcli`, which edits the brief frontmatter file AND the DB row
  together so they stay in sync. `/hand:list` prefers `recap:` over the
  first `U:` line as the goal hint.
- WAL journal mode — single-machine store; not designed for
  cross-machine sync (the -wal/-shm sidecars + concurrent writes would
  corrupt a synced copy). If the DB ever looks wrong, delete it and
  `rebuild`.

Migration: `scripts/backfill_status.py` handles briefs without
frontmatter (run it first); then `dbcli rebuild` indexes them. Backfill
only reads the rendered brief body (no JSONL), so the TodoWrite signal
isn't available; it's conservative.

## Ground rule: nothing changes status or deletes without a user command

No code path flips a brief's status, deletes a brief, or deletes an archive
unless the user issued the command or pressed the key. `/hand:off` only
classifies its OWN session (the detector, conservative bias) and never
touches other briefs. Rejected under this rule on 2026-09-21: the
`scripts/sweep_stale.py` auto-close (idle > 14 d → `done`, was cron-able)
and the automatic archive prune on the `/hand:off` path. Reason: a false
positive silently costs real work and the user cannot review what an
automatic pass did. Idle briefs are surfaced for a human decision by
`hand review` (see "Review") instead.

`is_stale(fm, days)` / `idle_days(fm)` remain as the idle predicates
`hand review` and the TUI use to list candidates; nothing acts on them.

## Review — the manual triage loop

`hand review [--days 14] [--cwd P] [--limit 25] [--all]` (`/hand:review`)
prints open briefs idle > N days, longest idle first, and writes nothing.
Decisions are commands, one per row: `hand done <sid8>`, `hand hold <sid8>
--note …`, `hand archive <sid8>`, `hand keep <sid8>` (stamps `last_resumed`
only — "not now", status unchanged). Every command accepts an 8-char sid
prefix; the DB already has one real 8-char collision, so an ambiguous
prefix is refused (`*_ERROR ambiguous prefix`), never guessed — resolution
lives in `dbcli.resolve_sid`, CLI layer only; `do_*` take full sids. TUI:
`s` = idle-only filter, `k` = keep, idle rows show `idle Nd`
(`tui.IDLE_DAYS`). `/hand:review` shows the report, asks, and applies only
what the user answered.

## Known follow-ups (not blocking)

From the 2026-09-21 review, deferred (verified, low impact). Fixed the same
day, not deferred: LIKE escaping in `search_sessions`, `backfill-titles`
visiting `archived` rows, every `do_*` mutation upserting the row from the
file when its UPDATE matched nothing (`_sync_row`), prune stamp written
before pruning. Kept on purpose: the `archived` status (0 users today, but
it is the only "hide forever without calling it done" bucket; `on_hold` is
the "come back" one) and `install.sh` as the legacy non-marketplace path
(links only off/on; the plugin cache is how commands ship).

Second pass, same day (`spec-findings.md`, `tests/test_findings.py`,
`tests/test_commands_args.py`): ten more fixed — `is_injected_user_msg`
scans only the first `INJECTED_SCAN_CHARS` (40; measured: all 33 injected
msgs across the fixtures carry the marker at char 0, the spec's 80 still
matched a marker quoted at char 36), `cut_at_line` (truncation ends on a
whole line and closes an odd fence), transcript overflow counts the
remainder from the live generator, archive `source_jsonl` home-relative +
`project_tag_from_cwd` sanitised, `detect_status(entries, tasks)` reads the
session's task dir (`auto-tasks`; open tasks veto `auto-user-msg`),
trimmer `_short_signal` (7 of 263 dropped ≤80-char turns rescued, all
findings), `nxt` skips `DROP_TOP_TYPES`, `tool_result_texts` (the
`toolUseResult` text only stands in for a lone block), `done.md`/`archive.md`
refuse two sids, `set -f` everywhere `$ARGUMENTS` is unquoted,
`scripts/fixture_stats.py` shared by bench + render_html, render_html
parses each fixture once.

Measured and deliberately NOT fixed:

- `hand list` N+1 (`get_session` per row without a recap): 0.12 s wall
  over 612 rows, 74 of them recap-less. Local SQLite; a second query path
  buys nothing.
- `load_jsonl` materialises the whole file: six consumers walk `entries`
  (`detect_status`, `extract_recap`, `extract_title`, `render_brief`,
  `extract_agent_reports`, `cwd_from_entries`). Streaming = six passes or
  a redesign. No OOM observed on the 86 MB fixture; revisit on the first.
- Folding bench's `extract_*` fan-out: bench is 45 s wall and the time is
  `load_jsonl` + `render_brief` + tokenizer on xhuge, not the four
  extract passes.

## Publish hygiene

This repo is public under the personal GitHub account. Keep it free of
employer / company / colleague data. Local-only publish-hygiene rules
(naming the exact identifiers to avoid) live in `CLAUDE.local.md`, which
is gitignored. Read that file before pushing anything that touches
fixtures, examples, or docs.
