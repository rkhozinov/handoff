---
description: Restore a /hand:off brief into the conversation. Pass the session id (printed by /hand:off) for deterministic restore. Bare /hand:on falls back to the newest brief in this cwd, which is unreliable when multiple sessions share the directory.
argument-hint: "[session-id|brief-path]"
---

Restore a /hand:off brief into the conversation.

**Recommended: pass the session id `/hand:off` printed.** That's the
only deterministic way to pick the right brief when this cwd has
multiple parallel sessions:

```
/hand:on ff6c4f4a-1a60-4a2c-9b96-abd23445b743
```

`/clear` creates a **new session id**, so the brief saved by the
pre-clear `/hand:off` lives under the OLD session id. Bare `/hand:on`
auto-discovery walks newest-to-oldest jsonls in this cwd, but if
several sessions ran handoffs today it picks the most recent one,
which may not be the session you wanted. Pass the session id to
remove the guesswork.

Behavior:

* `/hand:on <session-id>` — Resolve to `~/.claude/compaction/<sid>.md`
  and Read it. **Recommended path.**
* `/hand:on <full-path>` — Read the absolute path directly. Useful
  when the brief lives outside the default compaction dir.
* `/hand:on` (no args) — Auto-discover. Tries `${CLAUDE_SESSION_ID}.md`
  first; if missing (typical after /clear), walks JSONLs in this
  cwd's project dir from newest to oldest and reads the first one
  with a saved brief; falls back to a picker if none match.
  **Non-deterministic when multiple sessions share the cwd.**

## Resolution

Run this single bash block. It accepts an optional argument
(`$ARGUMENTS`) and prints either `BRIEF_PATH=<path>` or
`BRIEF_MISSING <reason>`. The outer agent branches on stdout, never
on shell variable state across calls.

```bash
ARG="$ARGUMENTS"
COMPACTION_DIR="$HOME/.claude/compaction"

resolve_brief() {
  # 1. Explicit path argument wins.
  if [ -n "$ARG" ]; then
    P="$ARG"
    [ "${P#/}" = "$P" ] && P="$PWD/$P"  # relative → absolute
    if [ -f "$P" ]; then
      printf '%s' "$P"; return 0
    fi
    # Maybe the user passed just a session id; try that.
    if [ -f "$COMPACTION_DIR/$ARG.md" ]; then
      printf '%s' "$COMPACTION_DIR/$ARG.md"; return 0
    fi
    return 1
  fi

  # 2. Current session id (rare success case — only when /hand:off was
  # run AFTER the most recent /clear).
  CUR="$COMPACTION_DIR/${CLAUDE_SESSION_ID}.md"
  if [ -f "$CUR" ]; then
    printf '%s' "$CUR"; return 0
  fi

  # 3. Walk this cwd's project dir from newest to oldest, return the
  # first session that has a saved brief. This recovers the pre-clear
  # session automatically.
  REAL_CWD=$(pwd -P)
  ENC=$(printf '%s' "$REAL_CWD" | sed 's/[^A-Za-z0-9-]/-/g')
  PROJECT_DIR="$HOME/.claude/projects/$ENC"
  for jl in $(ls -t "$PROJECT_DIR"/*.jsonl 2>/dev/null); do
    SID=$(basename "$jl" .jsonl)
    [ -f "$COMPACTION_DIR/$SID.md" ] && { printf '%s' "$COMPACTION_DIR/$SID.md"; return 0; }
  done
  return 1
}

if BRIEF=$(resolve_brief); then
  echo "BRIEF_PATH=$BRIEF"
else
  echo "BRIEF_MISSING arg=$ARG sid=${CLAUDE_SESSION_ID}"
fi
```

Decision tree based on stdout:

* `BRIEF_PATH=<path>` → Read it with the Read tool. Treat its contents
  as ground-truth context for the resumed work — Active Goal, Open
  Question, Decisions Made, Conversation Arc, Plans Saved, Sub-Agent
  Findings all override anything you'd otherwise infer.

* `BRIEF_MISSING ...` → No brief found by either explicit path or
  auto-discovery. Show the enriched picker so the user can choose:

  ```bash
  printf 'No brief found for current cwd. Recent briefs (newest first). Reply with the number or session id.\n\n'
  i=0
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

  Accept the user's pick (number or session id prefix) and re-run
  `/hand:on <path>` for the chosen brief.

After loading, print one line confirming what was restored:
`Restored: <session_id> from <ts>, <N> decisions`.

This command is idempotent — invoking it twice loads the same brief
twice. It does not consume or rename any file, so re-run `/hand:on`
whenever context resets again.
