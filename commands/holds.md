---
description: List on_hold sessions, due-first. Pass --due to show only sessions past their resume-by date.
argument-hint: "[--due]"
---

Run:

```bash
cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli holds $ARGUMENTS
```

Show the output verbatim. If `--due` was passed and the output is empty,
say so in one line — nothing is due.
