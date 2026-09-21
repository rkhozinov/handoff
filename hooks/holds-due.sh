#!/usr/bin/env bash
# SessionStart nudge: print held sessions whose hold_until has passed.
# Silent when nothing is due — CC injects hook stdout into context.
cd "$HOME/repos/handoff" 2>/dev/null || exit 0
OUT=$(PYTHONPATH=. python3 -m handoff.dbcli holds --due 2>/dev/null) || exit 0
[ -n "$OUT" ] && printf '⏰ holds due:\n%s\n' "$OUT"
exit 0
