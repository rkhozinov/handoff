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
set -f; set -- $ARGUMENTS; set +f
FLAG=""; SID=""
for a in "$@"; do
  case "$a" in
    --unarchive) FLAG="--unarchive";;
    -*) echo "HANDARCH_ERROR unknown flag $a"; exit 1;;
    *) [ -n "$SID" ] && { echo "HANDARCH_ERROR one session id at a time (got '$SID' and '$a')"; exit 1; }; SID="$a";;
  esac
done
[ -z "$SID" ] && { echo "HANDARCH_ERROR usage: /hand:archive <session-id> [--unarchive]"; exit 1; }

cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli archive "$SID" $FLAG
```

Pass the `HANDARCH_OK` / `HANDARCH_ERROR` line through to the user.
