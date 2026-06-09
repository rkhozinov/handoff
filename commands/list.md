---
description: List session briefs grouped by status. Default hides done. Pass --all to include done. Filters by current cwd unless --any-cwd.
argument-hint: "[--all] [--any-cwd]"
---

Query the sessions DB (`~/.claude/compaction/sessions.db`) and render the
briefs grouped by status. One row per session: short sid, status badge, date,
cwd basename, and a goal hint (the recap, falling back to the first `U: …`
line). The DB is populated by `/hand:off`; if it looks empty, run
`PYTHONPATH=. python3 -m handoff.dbcli rebuild` to backfill from brief files.

## Render

```bash
ARG="$ARGUMENTS"
FLAGS=""
case " $ARG " in *' --all '*|*' --all') FLAGS="$FLAGS --all";; esac
case " $ARG " in *' --any-cwd '*|*' --any-cwd') FLAGS="$FLAGS --any-cwd";; esac

cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli list \
  --cwd "$(pwd -P)" $FLAGS
```

Show the user the rendered table verbatim — no extra commentary needed. If
they want to resume one, they'll run `/hand:on <sid>` themselves.
