---
description: Explicitly restore the latest /handoff brief for the current cwd. Use after /clear when you want context back without relying on the SessionStart hook.
---

Restore the latest /handoff brief for this cwd into the conversation.

Run these steps:

1. Compute the cwd-keyed brief path:
   ```bash
   SLUG="$(pwd | tr '/' '-')"
   BRIEF="$HOME/.claude/compaction/latest-${SLUG}.md"
   ```

2. If the file does not exist or is older than 24 hours, list available
   briefs in `~/.claude/compaction/` (excluding `consumed-*`, `latest-*`,
   `*-full.md`) sorted by mtime, and ask the user which one to load.
   Otherwise proceed with the cwd-keyed brief.

3. Read the brief file with the Read tool. Treat its contents as
   ground-truth context for the resumed work — Active Goal, Decisions,
   Open TodoList, Sub-Agent Findings all override anything you might
   otherwise infer.

4. After loading, print one line confirming what was restored:
   `Restored: <session_id> from <ts>, <N> agent reports, <N> decisions`

This command is idempotent — invoking it twice loads the same brief
twice. Unlike the SessionStart hook, it does NOT consume the symlink,
so you can re-run `/handon` if your context resets again.
