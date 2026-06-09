---
description: Launch the 2-pane session TUI — left pane lists handoffs (title/date/status), right pane shows the selected brief, scrollable. Reads the sessions DB.
---

Launch the Textual TUI for reviewing and managing /hand:off sessions. Left
pane: handoff list (title, date, status), newest-first. Right pane: the
selected brief body, scrollable. Keys: `j/k` navigate, `a` show/hide done, `d`
done, `r` reopen, `x` delete (DB row only), `g` refresh, `q` quit.

Needs the optional Textual dependency — if it's missing the command prints the
install hint (`pip install -e '.[tui]'`).

```bash
cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli tui
```

This is an interactive full-screen app — the user drives it directly; just
launch it.
