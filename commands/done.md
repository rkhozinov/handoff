---
description: Mark a brief as done (hide from /hand:on picker). Pass session id. Use --reopen to flip back to in_progress.
argument-hint: "<session-id> [--reopen]"
---

Flip the `status:` frontmatter line on a brief. Manual `done` is sticky —
`/hand:off` will not auto-revive it on a later transcript pass.

## Resolve + edit

```bash
ARG="$ARGUMENTS"
COMPACTION_DIR="$HOME/.claude/compaction"

REOPEN=0
case " $ARG " in
  *' --reopen '*|*' --reopen') REOPEN=1; ARG="${ARG//--reopen/}";;
esac
SID="${ARG// /}"

if [ -z "$SID" ]; then
  echo "HANDDONE_ERROR usage: /hand:done <session-id> [--reopen]"
  exit 1
fi

BRIEF="$COMPACTION_DIR/$SID.md"
if [ ! -f "$BRIEF" ]; then
  echo "HANDDONE_ERROR no brief at $BRIEF"
  exit 1
fi

if ! head -1 "$BRIEF" | grep -q '^---$'; then
  echo "HANDDONE_ERROR brief has no frontmatter — run scripts/backfill_status.py first"
  exit 1
fi

if [ "$REOPEN" -eq 1 ]; then
  NEW_STATUS="in_progress"
  NEW_SIGNAL="manual"
  ACTION="reopened"
else
  NEW_STATUS="done"
  NEW_SIGNAL="manual"
  ACTION="closed"
fi

# Edit lines INSIDE the leading --- … --- block only.
awk -v st="$NEW_STATUS" -v sig="$NEW_SIGNAL" '
  BEGIN { in_fm = 0; seen = 0 }
  /^---$/ {
    if (!seen) { in_fm = !in_fm; if (!in_fm) seen = 1 }
    print; next
  }
  in_fm && /^status:/           { print "status: " st; next }
  in_fm && /^completion_signal:/{ print "completion_signal: " sig; next }
  { print }
' "$BRIEF" > "$BRIEF.tmp" && mv "$BRIEF.tmp" "$BRIEF"

echo "HANDDONE_OK action=$ACTION sid=$SID status=$NEW_STATUS"
```

Pass through the `HANDDONE_OK` / `HANDDONE_ERROR` line to the user — that's
all the feedback they need.
