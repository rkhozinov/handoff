---
description: Explicitly restore the latest /handoff brief for the current session into the conversation. Run after /clear when you want context back. Restoration is always explicit — there is no SessionStart auto-restore hook.
---

Restore the /handoff brief for the **current session** into the conversation.

The brief is keyed on `session_id` — not on cwd, not on branch. Claude Code keeps
the same `session_id` across `/clear` and across `claude -c` resume, so this
deterministically recovers the right brief no matter which worktree or branch
you're in.

Run these steps:

1. Resolve the current session id from the newest JSONL in this cwd's CC
   project dir (the file CC is writing to right now is the one you're in).
   Use the **physical** cwd (`pwd -P`) so symlinked paths like iCloud
   mirrors resolve to the same dir CC indexes under, and replace every
   non-alphanumeric character with `-` to match CC's project-dir encoding
   (it folds `/`, `.`, ` `, `~`, etc. all to `-`):

   ```bash
   REAL_CWD=$(pwd -P)
   ENC=$(printf '%s' "$REAL_CWD" | sed 's/[^A-Za-z0-9-]/-/g')
   PROJECT_DIR="$HOME/.claude/projects/$ENC"
   SID=$(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null | head -1 | xargs -I {} basename {} .jsonl)
   BRIEF="$HOME/.claude/compaction/${SID}.md"
   ```

2. If `BRIEF` exists and is younger than 24 hours, that's the file. Read it
   with the Read tool and treat its contents as ground-truth context for the
   resumed work — Active Goal, Decisions, Open TodoList, Sub-Agent Findings
   all override anything you might otherwise infer.

3. If `BRIEF` does not exist (no /handoff was run for this session) or is
   stale (>24h), fall back: list the most recent briefs in
   `~/.claude/compaction/` and ask the user which one to load:

   ```bash
   ls -lt "$HOME/.claude/compaction/"*.md 2>/dev/null \
     | grep -v -E 'consumed-|-full\.md$' \
     | head -10
   ```

   Show the list with each brief's first-line `# Session Brief — <ts>` header
   for context (run `head -1 <file>` on each).

4. After loading, print one line confirming what was restored:
   `Restored: <session_id> from <ts>, <N> agent reports, <N> decisions`

This command is idempotent — invoking it twice loads the same brief twice.
It does not consume or rename any file, so you can re-run `/handon` whenever
context resets again.
