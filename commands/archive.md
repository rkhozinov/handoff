---
description: Archive a session (hide-but-keep — stays in the DB + brief, hidden from the default list/picker). Pass --unarchive to restore. Sticky across /hand:off.
argument-hint: "<session-id> [--unarchive]"
---

Shelve a session: flips `status:` to `archived` (signal `manual`) in BOTH the
brief file frontmatter and the sessions DB. Archived sessions are kept intact
but hidden from the default `/hand:list` and `/hand:on` picker — view them with
`/hand:list --all` (or `hand list --archived`). Manual `archived` is sticky:
`/hand:off` will not auto-revive it. `--unarchive` restores it to
`in_progress`.

## Resolve + edit

```bash
ARG="$ARGUMENTS"
UNARCH=""
case " $ARG " in
  *' --unarchive '*|*' --unarchive') UNARCH="--unarchive"; ARG="${ARG//--unarchive/}";;
esac
SID="${ARG// /}"

if [ -z "$SID" ]; then
  echo "HANDARCH_ERROR usage: /hand:archive <session-id> [--unarchive]"
  exit 1
fi

cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli archive "$SID" $UNARCH
```

Pass the `HANDARCH_OK` / `HANDARCH_ERROR` line through to the user.
