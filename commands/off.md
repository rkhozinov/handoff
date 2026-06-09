---
description: Snapshot the current session into a deterministic brief + memory doc archive. Run before /clear when context is filling up. Bypasses Claude Code's lossy /compact.
---

The default `/compact` summarizer paraphrases code, file paths, and decisions. This command bypasses it.

Run the deterministic trimmer and archive the full session as a memory doc.

What it drops:
- `tool_result` bodies (the bulk of the noise)
- `thinking` blocks
- short procedural narration like "let me check", "reading the file", "ok" — these don't add signal beyond the tool_use marker

What it preserves verbatim:
- every real user message (decisions, reversals, intent)
- every substantive assistant text turn
- every code fence
- every file path, line number, and tool_use marker
- decisions, errors, todo state

## Steps

### 1. Compose the recap

Before running the bash block, write a 1–2 sentence recap of THIS session
from your own context. Shape:

> Goal: <what the session set out to do>. <current state — what shipped /
> where it stands>. Next: <single next step>.

Keep it under 300 chars, one line, concrete (PR numbers, ticket ids, env
names). Do NOT use single quotes (`'`) in the text — it's embedded in a
single-quoted shell var. This recap lands in the brief frontmatter and the
sessions DB (`~/.claude/compaction/sessions.db`).

### 2. Run the handoff

Run this single bash block, substituting your recap into `RECAP`. The
session id comes straight from
`${CLAUDE_SESSION_ID}` — Claude Code's own substitution, so there's
no ambiguity about which session is being archived. The transcript
path is derived from the same session id + cwd, matching CC's
project-dir encoding (`[^A-Za-z0-9-]` → `-`).

```bash
RECAP='<your 1-2 sentence recap here>'
SID="${CLAUDE_SESSION_ID}"
REAL_CWD=$(pwd -P)
ENC=$(printf '%s' "$REAL_CWD" | sed 's/[^A-Za-z0-9-]/-/g')
TRANSCRIPT="$HOME/.claude/projects/$ENC/$SID.jsonl"

if [ -z "$SID" ] || [ ! -f "$TRANSCRIPT" ]; then
  echo "HANDOFF_ERROR sid=$SID transcript=$TRANSCRIPT"
  exit 1
fi

BRIEF_PATH=$(
  cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.cli \
    --transcript "$TRANSCRIPT" \
    --session-id "$SID" \
    --cwd "$REAL_CWD" \
    --recap "$RECAP"
)
STATUS=$(awk '/^---$/{c++;next} c==1 && /^status:/{print $2; exit}' "$BRIEF_PATH")
SIGNAL=$(awk '/^---$/{c++;next} c==1 && /^completion_signal:/{print $2; exit}' "$BRIEF_PATH")
echo "HANDOFF_OK"
echo "  recap:      $RECAP"
echo "  session_id: $SID"
echo "  brief:      $BRIEF_PATH"
echo "  status:     $STATUS ($SIGNAL)"
echo "  db:         ~/.claude/compaction/sessions.db (row upserted)"
echo
case "$STATUS" in
  done)
    echo "Detector marked this session DONE — it's hidden from the"
    echo "/hand:on picker. To resume anyway: /hand:on $SID"
    echo "To revive permanently: /hand:done $SID --reopen"
    ;;
  *)
    echo "Restore with either:"
    echo "  /hand:on $SID"
    echo "  /hand:on $BRIEF_PATH"
    ;;
esac
```

Surface the **session id** to the user prominently — that's the
safest deterministic key for /hand:on. Many cwds carry multiple
parallel sessions, so picking the right brief by anything other than
its session id is guesswork.

Tell the user:

> Run `/clear` to free context, then `/hand:on <session_id>` when you
> want THIS session's brief restored. The session id was just printed
> above — copy it. (Bare `/hand:on` exists but auto-discovery picks the
> newest brief for this cwd, which is non-deterministic when several
> sessions share the directory.)

If the trimmer dropped something you actually needed, recall it from
the full archive: `memory doc get <hash>` (hash printed on stderr).
