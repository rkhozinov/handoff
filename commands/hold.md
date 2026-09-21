---
description: Park this session: snapshot it like /hand:off, then mark it on_hold with a note (and optional --until YYYY-MM-DD). Also `/hand:hold <session-id> <note>` to hold an existing brief without re-snapshotting.
argument-hint: "[<session-id>] [--until YYYY-MM-DD] <note…>"
---

The default `/compact` summarizer paraphrases code, file paths, and decisions. This command bypasses it.

Run the deterministic trimmer and archive the full session as a memory doc,
then mark it `on_hold` — a curated "come back later" shelf, distinct from
`done`.

## Steps

### 1. Compose the recap

Before running the bash block, write a 1–2 sentence recap of THIS session
from your own context. Shape:

> Goal: <what the session set out to do>. <current state — what shipped /
> where it stands>. Next: <single next step>.

Keep it under 300 chars, one line, concrete (PR numbers, ticket ids, env
names). This recap lands in the brief frontmatter and the sessions DB
(`~/.claude/compaction/sessions.db`). Skip this step if `$ARGUMENTS` starts
with an existing session id — that brief already has a recap.

### 2. Run the hold

Run this single bash block, substituting your recap into `RECAP` and your
`<why / what next>` note into `NOTE`.

```bash
# Snapshot block copied from commands/off.md — keep in sync (off.md is the source of truth).
RECAP=$(cat <<'HANDOFF_RECAP_EOF'
<your 1-2 sentence recap here>
HANDOFF_RECAP_EOF
)

# Parse $ARGUMENTS: [<session-id>] [--until YYYY-MM-DD] <note…>
ARGS="$ARGUMENTS"
SKIP_SNAPSHOT=""
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
set -f            # a note like "retry * after deploy" must not glob-expand
set -- $ARGS
set +f
FIRST="$1"
if printf '%s' "$FIRST" | grep -qE "$UUID_RE"; then
  SID="$FIRST"
  SKIP_SNAPSHOT=1
  shift
else
  SID="${CLAUDE_SESSION_ID}"
fi

UNTIL=""
REST=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--until" ]; then
    UNTIL="$2"
    shift 2
  else
    REST="$REST $1"
    shift
  fi
done
NOTE="${REST# }"

if [ -z "$SKIP_SNAPSHOT" ]; then
  REAL_CWD=$(pwd -P)
  # CC fixes the transcript dir at session start; the shell cwd can drift
  # (e.g. into a git worktree) mid-session. The session id is a unique UUID and
  # is the transcript filename, so locate the jsonl by id, not by slugifying the
  # (possibly drifted) cwd.
  TRANSCRIPT=$(find "$HOME/.claude/projects" -maxdepth 2 -name "$SID.jsonl" 2>/dev/null | head -1)

  if [ -z "$SID" ] || [ ! -f "$TRANSCRIPT" ]; then
    echo "HANDOFF_ERROR sid=$SID transcript=$TRANSCRIPT"
    exit 1
  fi

  ERRLOG=$(mktemp)
  BRIEF_PATH=$(
    cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.cli \
      --transcript "$TRANSCRIPT" \
      --session-id "$SID" \
      --cwd "$REAL_CWD" \
      --recap "$RECAP" 2>"$ERRLOG"
  )
  RC=$?
  # Fail loud: a non-zero CLI, empty stdout, or missing brief must NOT print
  # HANDOFF_OK. Otherwise a broken run looks like a successful one.
  if [ "$RC" -ne 0 ] || [ -z "$BRIEF_PATH" ] || [ ! -f "$BRIEF_PATH" ]; then
    echo "HANDOFF_ERROR: handoff.cli failed (exit=$RC, brief=[$BRIEF_PATH])."
    echo "  transcript: $TRANSCRIPT"
    echo "  re-run the block above; if it persists, run the CLI directly to see stderr."
    cat "$ERRLOG"
    rm -f "$ERRLOG"
    exit 1
  fi
  rm -f "$ERRLOG"

  # Carry the task list too, so /hand:on can restore the todo graph (blockedBy
  # edges included) alongside the brief. Strictly best-effort: the brief is the
  # primary artifact and a task-export failure must never turn a successful
  # snapshot into an error.
  cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli tasks \
    export "$SID" >/dev/null 2>&1
fi

cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli hold "$SID" --note "$NOTE" ${UNTIL:+--until "$UNTIL"}
RC=$?
if [ "$RC" -eq 0 ]; then
  # The brief's cwd is authoritative (the CLI takes it from the transcript,
  # not from this shell, which may have drifted into a worktree).
  BRIEF_CWD=$(awk '/^---$/{c++;next} c==1 && /^cwd:/{sub(/^cwd: */,""); print; exit}' "$HOME/.claude/compaction/$SID.md")
  echo "resume: cd ${BRIEF_CWD:-$(pwd -P)} && claude --resume $SID   |  /hand:on $SID"
fi
```

Pass the `HANDHOLD_OK` / `HANDHOLD_ERROR` line (and the resume hint) through
to the user.
