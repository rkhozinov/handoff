---
description: Explicitly restore the latest /handoff brief for the current session into the conversation. Run after /clear when you want context back. Restoration is always explicit — there is no SessionStart auto-restore hook.
---

Restore the /handoff brief for the **current session** into the conversation.

The brief is keyed on `session_id` — not on cwd, not on branch. Claude Code keeps
the same `session_id` across `/clear` and across `claude -c` resume, so this
deterministically recovers the right brief no matter which worktree or branch
you're in.

Run this single bash block — do **not** split it into multiple shell calls,
because `SID` and `BRIEF` are shell variables that don't survive across
separate invocations:

```bash
REAL_CWD=$(pwd -P)
ENC=$(printf '%s' "$REAL_CWD" | sed 's/[^A-Za-z0-9-]/-/g')
PROJECT_DIR="$HOME/.claude/projects/$ENC"
SID=$(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null | head -1 | xargs -I {} basename {} .jsonl)
BRIEF="$HOME/.claude/compaction/${SID}.md"
if [ -n "$SID" ] && [ -f "$BRIEF" ]; then
  echo "BRIEF_PATH=$BRIEF"
else
  echo "BRIEF_MISSING sid=$SID"
fi
```

Decision tree based on the script's stdout:

* `BRIEF_PATH=<path>` printed → that's the file. Read it with the Read
  tool and treat its contents as ground-truth context for the resumed
  work — Active Goal, Open Question, Decisions Made, Conversation Arc,
  Plans Saved, Sub-Agent Findings all override anything you might
  otherwise infer.

* `BRIEF_MISSING ...` printed → no /handoff was ever run for this
  session. Fall back: list recent briefs and ask the user which one
  to load:

  ```bash
  ls -lt "$HOME/.claude/compaction/"*.md 2>/dev/null \
    | grep -v -E 'consumed-|-full\.md$' \
    | head -10
  ```

  Show the list with each brief's first-line `# Session Brief — <ts>`
  header for context (`head -1 <file>` per brief).

After loading, print one line confirming what was restored:
`Restored: <session_id> from <ts>, <N> decisions`.

There is **no freshness gate**. With `session_id` as the lookup key,
existence of the brief is equivalent to correctness — there's no risk
of loading a different session's brief by accident. A brief that hasn't
been refreshed in days is still the right brief for THIS session.

This command is idempotent — invoking it twice loads the same brief
twice. It does not consume or rename any file, so you can re-run
`/handon` whenever context resets again.
