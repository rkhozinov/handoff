---
description: Restore a /hand:off brief into the conversation. Pass the session id (printed by /hand:off) for deterministic restore. Bare /hand:on only matches the CURRENT session id; otherwise reports BRIEF_MISSING and shows a picker (no silent fallback). Pass --all to include done briefs in the picker.
argument-hint: "[session-id|brief-path] [--all]"
---

**ACT NOW — this is a command, not a description.** The moment you see
`/hand:on`, run the resolution bash block under **## Resolution** below,
then **Read the resolved brief with the Read tool** before doing or
saying anything else. Do NOT reply "Ready" or ask "What task?" — the
task is: restore the brief. Only after the brief is Read do you report
what was restored and continue. The prose between here and ## Resolution
is reference; the imperative is: resolve → Read → report.

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
  and Read it. Flips the brief's `status:` frontmatter from
  `pending`/`in_progress` → `in_progress` and stamps `last_resumed`.
  **Recommended path.**
* `/hand:on <full-path>` — Read the absolute path directly. Useful
  when the brief lives outside the default compaction dir.
* `/hand:on` (no args) — Try `${CLAUDE_SESSION_ID}.md` only. If
  missing (typical after /clear), report BRIEF_MISSING and show the
  picker. **No silent fallback to other sessions' briefs.**
* `/hand:on --all` — When the picker fires, include briefs marked
  `status: done` (normally hidden). Useful when the auto-detector
  was wrong and you want to revive a closed brief.

## Resolution

Run this single bash block. It accepts an optional argument
(`$ARGUMENTS`) and prints either `BRIEF_PATH=<path>` or
`BRIEF_MISSING <reason>`. The outer agent branches on stdout, never
on shell variable state across calls.

```bash
ARG="$ARGUMENTS"
COMPACTION_DIR="$HOME/.claude/compaction"

SHOW_ALL=0
case " $ARG " in
  *' --all '*|*' --all') SHOW_ALL=1; ARG="${ARG//--all/}";;
esac
ARG="${ARG// /}"

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
  STATUS=$(awk '/^---$/{c++;next} c==1 && /^status:/{print $2; exit}' "$BRIEF")
  echo "BRIEF_STATUS=$STATUS"
else
  echo "BRIEF_MISSING arg=$ARG sid=${CLAUDE_SESSION_ID} show_all=$SHOW_ALL"
fi
```

Decision tree based on stdout:

* `BRIEF_PATH=<path>` followed by `BRIEF_STATUS=<status>` → Read the
  brief with the Read tool. Treat its contents as ground-truth
  context for the resumed work.

  After Read, update BOTH the brief file frontmatter and the sessions
  DB row (status → `in_progress`, stamp `last_resumed`) via dbcli. It
  declines to flip a `done` brief on its own and prints `HANDON_DONE`:

  ```bash
  SID=$(basename "$BRIEF" .md)
  cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli on "$SID" \
    --dir "$(dirname "$BRIEF")"
  ```

  If `BRIEF_STATUS` is `done`, load the brief anyway (user asked
  explicitly by sid) — `dbcli on` will print `HANDON_DONE` and leave it
  untouched. Warn the user that the brief was marked done and that
  `/hand:done <sid> --reopen` will revive it permanently.

* `BRIEF_MISSING ...` → No brief found by either explicit path or
  auto-discovery. Show the enriched picker so the user can choose:

  ```bash
  if [ "$SHOW_ALL" -eq 1 ]; then
    printf 'No brief found. ALL recent briefs (newest first, including done). Reply with the number or session id.\n\n'
  else
    printf 'No brief found. Open briefs (newest first, done hidden — pass --all to include). Reply with the number or session id.\n\n'
  fi
  i=0
  for f in $(ls -t "$HOME/.claude/compaction/"*.md 2>/dev/null \
              | grep -E '/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md$' \
              | grep -v -E '/consumed-|-full\.md$'); do
    status=$(awk '/^---$/{c++;next} c==1 && /^status:/{print $2; exit}' "$f")
    if [ "$status" = "done" ] && [ "$SHOW_ALL" -ne 1 ]; then
      continue
    fi
    i=$((i+1))
    [ "$i" -gt 10 ] && break
    sid=$(basename "$f" .md)
    age=$(awk '/^---$/{c++;next} c==1 && /^created:/{sub(/^created: */,""); print; exit}' "$f")
    [ -z "$age" ] && age=$(stat -f '%Sm' -t '%Y-%m-%dT%H:%MZ' "$f" 2>/dev/null \
                           || stat -c '%y' "$f" 2>/dev/null | cut -c1-16)
    size=$(wc -c <"$f" | tr -d ' ')
    cwd=$(awk '/^---$/{c++;next} c==1 && /^cwd:/{sub(/^cwd: */,""); print; exit}' "$f")
    [ -z "$cwd" ] && cwd=$(grep -m1 '^\*\*Cwd:\*\*' "$f" | sed 's/.*`\(.*\)`.*/\1/')
    goal=$(awk '/^U:/{sub(/^U: */,""); print; exit}' "$f")
    badge="$status"
    [ -z "$badge" ] && badge="?"
    printf '%2d. %s  [%s]  (%s, %s bytes)\n' "$i" "$sid" "$badge" "$age" "$size"
    [ -n "$cwd" ]  && printf '    cwd:  %s\n' "$cwd"
    [ -n "$goal" ] && printf '    goal: %s\n' "$(printf '%s' "$goal" | cut -c1-120)"
    printf '\n'
  done
  ```

  Accept the user's pick (number or session id prefix) and re-run
  `/hand:on <path>` for the chosen brief.

After loading, print one line confirming what was restored:
`Restored: <session_id> [<status>] from <created_ts>`.

This command is idempotent for already-restored briefs — invoking it
twice loads the same brief twice and just updates `last_resumed`.
