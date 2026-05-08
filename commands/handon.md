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
  session. Fall back: render an enriched list of recent briefs so the
  user can pick the right one based on cwd, active goal, and open
  question — not just the session id.

  Run this single block; it walks the 10 newest non-consumed briefs
  and emits one structured stanza per brief:

  ```bash
  printf 'Recent briefs (newest first). Reply with the number or the session id of the brief to load.\n\n'
  i=0
  # Only list session-id briefs (UUID-shaped basenames). Filters out
  # legacy `latest-<slug>` symlinks, `consumed-*`, `-full.md`, and any
  # test fixture names like `prev-session-test.md`.
  for f in $(ls -t "$HOME/.claude/compaction/"*.md 2>/dev/null \
              | grep -E '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md$' \
              | grep -v -E '/consumed-|-full\.md$'); do
    i=$((i+1))
    [ "$i" -gt 10 ] && break
    sid=$(basename "$f" .md)
    age=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$f" 2>/dev/null \
          || stat -c '%y' "$f" 2>/dev/null | cut -c1-16)
    size=$(wc -c <"$f" | tr -d ' ')
    cwd=$(grep -m1 '^\*\*Cwd:\*\*' "$f" | sed 's/.*`\(.*\)`.*/\1/')
    goal=$(awk '/^## Active Goal/{flag=1;next} /^##/{flag=0} flag && NF{print; exit}' "$f")
    openq=$(awk '/^## Open Question/{flag=1;next} /^##/{flag=0} flag && NF{print; exit}' "$f")
    printf '%2d. %s  (%s, %s bytes)\n' "$i" "$sid" "$age" "$size"
    [ -n "$cwd" ]   && printf '    cwd:  %s\n' "$cwd"
    [ -n "$goal" ]  && printf '    goal: %s\n' "$goal"
    [ -n "$openq" ] && printf '    open: %s\n' "$openq"
    printf '\n'
  done
  ```

  Then ask the user "Which brief?" — accept either the number from the
  list or a session id prefix. Read that file with the Read tool.

After loading, print one line confirming what was restored:
`Restored: <session_id> from <ts>, <N> decisions`.

There is **no freshness gate**. With `session_id` as the lookup key,
existence of the brief is equivalent to correctness — there's no risk
of loading a different session's brief by accident. A brief that hasn't
been refreshed in days is still the right brief for THIS session.

This command is idempotent — invoking it twice loads the same brief
twice. It does not consume or rename any file, so you can re-run
`/handon` whenever context resets again.
