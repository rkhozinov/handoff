---
description: Restore one or more /hand:off briefs into the conversation, along with each session's task list (blockedBy edges intact). Pass the session id (printed by /hand:off) for deterministic restore; pass several ids to stack multiple briefs into the same session. Bare /hand:on only matches the CURRENT session id; otherwise reports BRIEF_MISSING and shows a picker (no silent fallback). Pass --all to include done briefs in the picker.
argument-hint: "[session-id|brief-path ...] [--all]"
---

**ACT NOW.** Run the block, then **Read every `BRIEF_PATH=` with the Read
tool, in the order printed**, before saying anything else. Do not reply
"Ready" or ask what the task is — the task is: restore the brief.

`/clear` mints a new session id, so a brief saved before it lives under the
OLD id — pass that id. Bare `/hand:on` only matches the current session id.

```bash
set -f
cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli on --restore --current "${CLAUDE_SESSION_ID}" $ARGUMENTS
```

The block resolves each argument (session id or path), flips the brief to
`in_progress` (a held brief is released; `done`/`archived` are left alone),
and merges its task bundle into this session. Then:

* `BRIEF_PATH=` / `BRIEF_STATUS=` pairs → Read each path; treat the
  contents as ground truth for the resumed work. Report one line per brief:
  `Restored: <sid> [<status>]`, plus one line per `HANDTASKS_OK`
  (e.g. `Tasks: 12 restored from 9517a280`). `HANDON_DONE` /
  `HANDON_ARCHIVED` → say the brief was loaded but its status was left as
  is (`/hand:done <sid> --reopen` / `/hand:archive <sid> --unarchive` revive).
* `BRIEF_MISSING` next to at least one `BRIEF_PATH` → name the unresolved
  argument(s) and carry on with the rest.
* A picker (`No brief found. …`) → show it verbatim, take the user's pick
  (number or id prefix), and re-run `/hand:on <sid>`.
