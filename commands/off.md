---
description: Snapshot the current session into a deterministic brief + memory doc archive + a task-list bundle. Run before /clear when context is filling up. Bypasses Claude Code's lossy /compact.
---

Compose a 1–2 sentence recap of THIS session from your own context —
`Goal: <what it set out to do>. <current state>. Next: <single next step>.` —
concrete (PR numbers, ticket ids, env names), one line, under 300 chars.
Then run this block with the recap substituted (the heredoc is quoted: any
characters are fine):

```bash
cd ~/repos/handoff && PYTHONPATH=. python3 -m handoff.dbcli off "${CLAUDE_SESSION_ID}" --cwd "$(pwd -P)" --recap-stdin <<'HANDOFF_RECAP_EOF'
<your recap here>
HANDOFF_RECAP_EOF
```

Show the output verbatim. Then tell the user: run `/clear`, and
`/hand:on <session_id>` (the id printed above) restores this session —
bare `/hand:on` only matches the current session id, so copy it.
