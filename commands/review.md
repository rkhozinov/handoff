---
description: Review idle open briefs (untouched > 14 days) and decide per row — done, hold, archive, or keep. Report first; nothing changes until you answer.
argument-hint: "[--days N] [--all]"
---

Run the report and show it verbatim:

```bash
set -f
cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli review $ARGUMENTS
```

Then ask the user what to do, one decision per row, e.g.
"1,4 done · 2 hold: waiting on PR 12 · rest keep". Apply ONLY what they
answered, one command per row, and echo each result line:

- done    → `python3 -m handoff.dbcli done <sid8>`
- hold    → `python3 -m handoff.dbcli hold <sid8> --note "<their words>" [--until YYYY-MM-DD]`
- archive → `python3 -m handoff.dbcli archive <sid8>`
- keep    → `python3 -m handoff.dbcli keep <sid8>` (resets the idle clock; status unchanged)

Rows they did not mention stay untouched. An `*_ERROR ambiguous prefix`
means two sessions share those 8 chars — show both full ids from the DB
(`hand search <sid8>`) and ask which.
