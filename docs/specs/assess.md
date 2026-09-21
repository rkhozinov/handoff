# Spec: `hand assess` + `/hand:assess` — bulk assessment of held briefs

Decision (user, 2026-09-21): **assess-only**. One read-only agent per held
brief reports state / next step / blocker / suggestion; the user decides per
row; decisions go through the existing `hand done|keep|hold` commands. No
agent mutates a repo, no code path changes a status (CLAUDE.md ground rule).

Measured population (2026-09-21): 36 `on_hold` rows, 29 in one cwd, brief
bodies 2–306 KB, 2 with open task bundles, 0 with `hold_until`, imported
`hold_note`s are the old `claude --resume …` lines. So the pack must carry
the brief PATH (the agent Reads it; we never inline 300 KB) and the open
tasks (subjects only — that is what the agent can act on).

## 1. `hand assess [sid …] [--limit N] [--all]` (`handoff/dbcli.py`)

Pure report, rc 0, writes nothing (pinned by `test_assess_writes_nothing`).

Selection:
- no sids → `db.list_holds(conn)` order (due first, then newest), capped at
  `--limit` (default `10`); `--all` lifts the cap. Header line:
  `Assess held briefs: <total> — showing <n>. One read-only agent per row; decide with hand done|keep|hold <sid8>.`
  With 0 holds: `No held briefs.` and rc 0.
- explicit sids → each resolved with `resolve_sid` (8-char prefixes; an
  ambiguous or unknown one prints `HANDASSESS_ERROR …` via
  `_resolve_or_report(..., err_prefix="HANDASSESS")` and rc 1 — nothing else
  is printed for that run). Explicit sids are NOT status-gated (assessing an
  `in_progress` brief is harmless and read-only) and NOT capped. Header:
  `Assess briefs: <n>.`

Per row (blank line after each):

```
ASSESS <sid8>  <status>  <title | recap | sid8>  [<cwd basename>]
    cwd:    <cwd>
    brief:  <brief_path>          # or `MISSING` when the .md is gone
    note:   <hold_note>           # line omitted when null
    due:    <hold_until>          # line omitted when null
    tasks:  <k> open — <subject 1>; <subject 2>; …   # line omitted when 0
```

`brief_path` is `<compaction_dir>/<sid>.md` (`args.dir`), existence checked.
Open tasks come from the task bundle `tasks.bundle_path(sid, base=args.bundle_dir)`
(`--bundle-dir`, default `tasks.DEFAULT_BUNDLE_DIR`, same flag name as
`tasks`/`on`): every task whose `status != "completed"`, in bundle order,
subjects joined with `; `. A missing/unreadable bundle → no `tasks:` line.
Label fallback mirrors `_cmd_review` (`title` → `recap` → sid8; do NOT read
the body).

Parser: `assess` subparser via `_add_db_args`, `sid` `nargs="*"`,
`--limit` int default 10, `--all` store_true, `--bundle-dir`.

## 2. `commands/assess.md`

```
---
description: Assess held briefs in bulk — one read-only agent per hold reads the brief and checks the repo, reports state/next/blocker/suggestion; you decide per row. Nothing changes until you answer.
argument-hint: "[sid …] [--limit N] [--all]"
---

Run the pack and show it verbatim:

```bash
set -f
cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli assess $ARGUMENTS
```

Then, in ONE message, spawn one agent per `ASSESS` row (subagent_type
`general-purpose`, name `assess-<sid8>`), all in parallel, each with this
prompt — fill the `<…>` from the row:

> READ-ONLY assessment of a parked Claude Code session. Do not edit, create,
> delete, commit, stash, checkout or run anything that changes state; git
> read commands, `gh pr view/list`, `ls`, `cat` are fine.
> 1. Read the brief at `<brief>` (it is the trimmed prior conversation).
> 2. `cd <cwd>` and check what the brief says was in flight: branch, PR,
>    uncommitted files, the files it was editing. Open tasks: `<tasks line or "none">`.
>    Hold note: `<note or "none">`.
> 3. Reply with exactly this block and nothing else:
>    SID: <sid8>
>    STATE: <1–2 lines: what was in flight, what the repo/PR says now>
>    NEXT: <the one concrete next step, or "none">
>    BLOCKER: <what it waits on, or "none">
>    SUGGEST: done | keep | release
>    WHY: <one line>
>    `done` = the work landed or is obsolete; `release` = ready to resume now;
>    `keep` = still waiting on the blocker.

When all agents have reported, print one table — `sid8 | title | suggest |
next / blocker` — then ask the user what to do, one decision per row, e.g.
"1,3 done · 2 release · rest keep: <note>". Apply ONLY what they answered,
one command per row, and echo each result line:

- done    → `python3 -m handoff.dbcli done <sid8>`
- release → `python3 -m handoff.dbcli hold <sid8> --release`
- keep    → `python3 -m handoff.dbcli hold <sid8> --note "<NEXT / BLOCKER from the report>" [--until YYYY-MM-DD]`
            (re-holding with the assessment as the note is how the finding is kept; bare keep = no command)

Rows they did not mention stay untouched. An agent that fails or returns
no block is shown as `sid8 | title | ? | agent failed` — never guessed.
```

## 3. Tests — `tests/test_assess.py` (already written, RED)

Seed helper = `tests/test_review_cmd.py::_seed`. Pinned: default order +
cap + header counts; `--all`; explicit sid8 prefix incl. non-hold and
ambiguous; `MISSING` brief; `tasks:` line from a bundle with one completed
and two open tasks; nothing written (DB dump + brief mtimes equal before and
after); `No held briefs.`.

## 4. Docs

CLAUDE.md: add `/hand:assess` to the contracts list and a two-line "Assess"
paragraph under "Review — the manual triage loop" (same ground rule, agents
are read-only, decisions are the existing commands). `commands/holds.md`
description gains "… `/hand:assess` to triage them in bulk". Plugin version
bump is done by the spec author, not the builder.

## 5. `hand reviewed` — the decision step (user decision 2026-09-21: versioned copy, same sid)

Every reviewed brief keeps its ORIGINAL text verbatim: `/hand:off` on a
resumed session rewrites `<sid>.md` in place, so without a copy the
pre-review brief is gone the next time the session is snapshotted. The lead
(this session) and sonnet teammates only read; `hand reviewed` is the one
command that records a decision, and it is typed per row by the user's answer.

`hand reviewed <sid8> --note "<assessment>" --then done|release|keep [--until YYYY-MM-DD]`

1. `_resolve_or_report(..., err_prefix="HANDREVIEWED")`; brief must exist
   (`HANDREVIEWED_ERROR no brief at <path>`, rc 1). `--then` is required
   (argparse `choices`).
2. Copy the brief BYTES verbatim (not re-rendered) to
   `<dir>/reviewed/<sid>.<YYYYMMDDTHHMMSSZ>.md` via `fsutil.atomic_write`
   (`mkdir parents`). Timestamp = `lifecycle.now_iso()`-style UTC with the
   `-`/`:` stripped so the name sorts. Never overwrite: if the name exists
   (two reviews in one second) append `-1`, `-2`, ….
3. Note → `lifecycle.sanitize_recap(note)` (one line, `RECAP_MAX_CHARS`), it
   becomes `hold_note` for ALL three outcomes (the assessment is the history
   even when the brief is done).
4. Apply, reusing the existing do_* (never re-implement the file+DB write):
   - `done`    → set `hold_note` then `do_done(sid, …)`
   - `release` → set `hold_note` then `do_hold(sid, note=None, until=None, release=True, …)`
   - `keep`    → `do_hold(sid, note=<sanitized>, until=args.until, release=False, …)`
   "set hold_note then" = `_read_split` → `fm["hold_note"] = note` →
   `_write_brief` → the do_* call (which re-reads the file and syncs the DB).
5. Print `HANDREVIEWED_OK sid=<sid> then=<done|release|keep> copy=<copy path>`
   followed by the do_* result line. rc 0.

`hand assess` rows gain, when `<dir>/reviewed/<sid>.*.md` exist:
`    reviewed: <n> version(s), last <YYYY-MM-DDTHH:MM:SSZ>` (parsed back from
the newest filename). `hand show <sid>` prints one `reviewed: <path>` line per
copy after the `recap:` line, before `---`.

`commands/assess.md` apply-table becomes (replaces the three bullets in §2):

- done    → `python3 -m handoff.dbcli reviewed <sid8> --note "<STATE — NEXT / BLOCKER>" --then done`
- release → `… --then release`
- keep    → `… --then keep [--until YYYY-MM-DD]`

and the agent spawn uses `model: "sonnet"` (user's call: sonnet teammates, the
lead synthesises). Everything else in §2 unchanged.

Tests: `tests/test_reviewed.py` (RED): copy is byte-identical to the
original and the original is then changed; second review in the same second
gets `-1`; each `--then` outcome's status/hold_note/hold_until; `--until`
only honoured with `keep`; missing brief; ambiguous prefix; `hand assess`
`reviewed:` line; `hand show` lists copies; a `/hand:off` re-run afterwards
does not touch the copy (`test_offs_rerun_leaves_copy` uses `cli.run` with
`--out-dir <dir>` and a tiny transcript).
