---
description: Park this session on the on_hold shelf — snapshot it like /hand:off, then mark it on_hold with a note and optional --until YYYY-MM-DD. Or `/hand:hold <session-id> <note>` to hold an existing brief without re-snapshotting.
argument-hint: "[<session-id>] [--until YYYY-MM-DD] <note…>"
---

If `$ARGUMENTS` starts with a session id, it names an existing brief and no
recap is needed. Otherwise compose the same 1–2 sentence recap as `/hand:off`
and substitute it below. Then run:

```bash
set -f; set -- $ARGUMENTS; set +f          # split the note into words; no globbing
SID=""; case "$1" in [0-9a-f]*-*-*-*-*) SID="$1"; shift;; esac
UNTIL=""; if [ "$1" = "--until" ]; then UNTIL="$2"; shift 2; fi
NOTE="$*"
cd ~/repos/handoff
if [ -n "$SID" ]; then
  PYTHONPATH=. python3 -m handoff.dbcli hold "$SID" --note "$NOTE" ${UNTIL:+--until "$UNTIL"}
else
  PYTHONPATH=. python3 -m handoff.dbcli off "${CLAUDE_SESSION_ID}" --cwd "$(pwd -P)" \
    --hold "$NOTE" ${UNTIL:+--until "$UNTIL"} --recap-stdin <<'HANDOFF_RECAP_EOF'
<your recap here>
HANDOFF_RECAP_EOF
fi
```

Show the output verbatim (the `resume:` line is what `/hand:holds` will
print for this session later).
