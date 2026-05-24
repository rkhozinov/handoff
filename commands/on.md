---
description: Restore a /hand:off brief into the conversation. Pass the session id (printed by /hand:off) for deterministic restore. Bare /hand:on only matches the CURRENT session id; otherwise reports BRIEF_MISSING and shows a picker (no silent fallback).
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
will NOT silently auto-pick a stale brief from another session — it
only loads a brief keyed to the CURRENT session id. Otherwise it
reports BRIEF_MISSING and shows a picker. Pass the session id
explicitly to restore across `/clear`.

Behavior:

* `/hand:on <session-id>` — Resolve to `~/.claude/compaction/<sid>.md`
  and Read it. **Recommended path.**
* `/hand:on <full-path>` — Read the absolute path directly. Useful
  when the brief lives outside the default compaction dir.
* `/hand:on` (no args) — Try `${CLAUDE_SESSION_ID}.md` only. If
  missing (typical after /clear), report BRIEF_MISSING and show the
  picker. **No silent fallback to other sessions' briefs.**

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

  # 2. Current session id only (rare success — only when /hand:off was
  # run AFTER the most recent /clear). No silent fallback to other
  # sessions' briefs — that produced wrong-brief footguns. If missing,
  # fall through to BRIEF_MISSING + picker so the user picks.
  CUR="$COMPACTION_DIR/${CLAUDE_SESSION_ID}.md"
  if [ -f "$CUR" ]; then
    printf '%s' "$CUR"; return 0
  fi
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
