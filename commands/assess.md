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
`general-purpose`, model `"sonnet"`, name `assess-<sid8>`), all in parallel,
each with this prompt — fill the `<…>` from the row:

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

- done    → `python3 -m handoff.dbcli reviewed <sid8> --note "<STATE — NEXT / BLOCKER>" --then done`
- release → `python3 -m handoff.dbcli reviewed <sid8> --note "<STATE — NEXT / BLOCKER>" --then release`
- keep    → `python3 -m handoff.dbcli reviewed <sid8> --note "<STATE — NEXT / BLOCKER>" --then keep [--until YYYY-MM-DD]`
            (re-holding with the assessment as the note is how the finding is kept; bare keep = no command)

Rows they did not mention stay untouched. An agent that fails or returns
no block is shown as `sid8 | title | ? | agent failed` — never guessed.
