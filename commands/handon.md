---
description: Explicitly restore the latest /handoff brief for the current cwd. Run this after /clear when you want context back. There is no SessionStart auto-restore hook — restoration is always explicit.
---

Restore the latest /handoff brief for this cwd into the conversation.

Run these steps:

1. Compute the cwd-keyed brief path. Use the central slug helper so the
   shape matches what `/handoff` writes (cwd + branch fingerprint when in
   a git checkout):
   ```bash
   SLUG="$(cd ~/repos/claude-compaction && PYTHONPATH=. python3 -m compaction.slug "$PWD" 2>/dev/null || echo "${PWD//\//-}")"
   BRIEF="$HOME/.claude/compaction/latest-${SLUG}.md"
   ```
   The `||` fallback preserves restore behavior if the helper is unavailable.

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
twice. It does not consume or rename the symlink, so you can re-run
`/handon` whenever context resets again.
