---
description: Mark a brief as done (hide from /hand:on picker). Pass session id. Use --reopen to flip back to in_progress.
argument-hint: "<session-id> [--reopen]"
---

Flip the `status:` on a brief in BOTH the brief file frontmatter and the
sessions DB. Manual `done` is sticky — `/hand:off` will not auto-revive it on a
later transcript pass.

## Resolve + edit

```bash
ARG="$ARGUMENTS"
REOPEN=""
case " $ARG " in
  *' --reopen '*|*' --reopen') REOPEN="--reopen"; ARG="${ARG//--reopen/}";;
esac
SID="${ARG// /}"

if [ -z "$SID" ]; then
  echo "HANDDONE_ERROR usage: /hand:done <session-id> [--reopen]"
  exit 1
fi

cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli done "$SID" $REOPEN
```

Pass the `HANDDONE_OK` / `HANDDONE_ERROR` line through to the user — that's all
the feedback they need.
