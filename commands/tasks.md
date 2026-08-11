---
description: Export, inspect, or import a session's Claude Code task list. /hand:off and /hand:on already carry tasks automatically — use this for manual moves between sessions, or to inspect what a brief will restore.
argument-hint: "list|export|import|copy <args>"
---

Claude Code keys its task list to the session id, and `/clear` mints a new one,
so the todo graph — including `blockedBy` edges — is dropped on every handoff.
`/hand:off` exports it next to the brief and `/hand:on` merges it back; this
command is the manual door into the same machinery.

Merge is the default and never touches tasks already in the destination:
imported tasks get fresh ids allocated above the destination's highest, and
every `blocks` / `blockedBy` edge is rewritten through that id map. A reference
to a task that isn't in the bundle is dropped from the array **and reported**.

Completed tasks are skipped by default. Claude Code wipes a task list once
every task in it is completed, so restoring a finished list would restore work
that disappears again seconds later. Pass `--all` when you want it anyway.

## Usage

```
/hand:tasks list   <session-id>
/hand:tasks export <session-id> [--out FILE]
/hand:tasks import <bundle> --to <session-id> [--replace] [--all] [--dry-run]
/hand:tasks copy   --from <session-id> --to <session-id> [same flags]
```

`--to` accepts `${CLAUDE_SESSION_ID}` for "this session". Bundles default to
`~/.claude/compaction/tasks/<session-id>.json`, next to the brief.

`--replace` clears the destination's task files before writing and keeps the
bundle's original ids. It is the one mode that loses data and is never the
default. `--dry-run` prints the plan (id remap, dropped refs, skips) and writes
nothing.

## Run

```bash
cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli tasks $ARGUMENTS
```

Pass the `HANDTASKS_*` line through to the user, plus any `dropped:` /
`warn:` lines under it — those are the only places a lost edge is reported.

`HANDTASKS_EMPTY` is **not** an error (rc 0): a session with no tasks is
normal. Only `HANDTASKS_ERROR` (rc 1) means something actually went wrong.
