---
description: Mark a brief as done (hide from /hand:on picker). Pass session id. Use --reopen to flip back to in_progress.
argument-hint: "<session-id> [--reopen]"
---

Flip the `status:` on a brief in BOTH the brief file frontmatter and the
sessions DB. Manual `done` is sticky — `/hand:off` will not auto-revive it on a
later transcript pass.

## Resolve + edit

```bash
set -f; set -- $ARGUMENTS; set +f
FLAG=""; SID=""
for a in "$@"; do
  case "$a" in
    --reopen) FLAG="--reopen";;
    -*) echo "HANDDONE_ERROR unknown flag $a"; exit 1;;
    *) [ -n "$SID" ] && { echo "HANDDONE_ERROR one session id at a time (got '$SID' and '$a')"; exit 1; }; SID="$a";;
  esac
done
[ -z "$SID" ] && { echo "HANDDONE_ERROR usage: /hand:done <session-id> [--reopen]"; exit 1; }

cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli done "$SID" $FLAG
```

Pass the `HANDDONE_OK` / `HANDDONE_ERROR` line through to the user — that's all
the feedback they need.
