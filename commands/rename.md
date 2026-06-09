---
description: Rename a session's title in the brief frontmatter + the sessions DB. Pass the session id then the new title.
argument-hint: "<session-id> <new title>"
---

Set a session's `title:`. Writes BOTH the brief file frontmatter and the
sessions DB row so the list/TUI reflect it immediately.

## Resolve + edit

```bash
ARG="$ARGUMENTS"
SID="${ARG%% *}"          # first token
TITLE="${ARG#* }"         # the rest

if [ -z "$SID" ] || [ "$SID" = "$ARG" ]; then
  echo "HANDRENAME_ERROR usage: /hand:rename <session-id> <new title>"
  exit 1
fi

cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli rename "$SID" $TITLE
```

Pass the `HANDRENAME_OK` / `HANDRENAME_ERROR` line through to the user.
