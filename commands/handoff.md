---
description: Snapshot the current session into a deterministic brief + memory doc archive. Run before /clear when context is filling up. Bypasses Claude Code's lossy /compact.
---

The default `/compact` summarizer paraphrases code, file paths, and decisions. This command bypasses it.

Run the deterministic trimmer and archive the full session as a memory doc.

What it drops:
- `tool_result` bodies (the bulk of the noise)
- `thinking` blocks
- short procedural narration like "let me check", "reading the file", "ok" — these don't add signal beyond the tool_use marker

What it preserves verbatim:
- every real user message (decisions, reversals, intent)
- every substantive assistant text turn
- every code fence
- every file path, line number, and tool_use marker
- decisions, errors, todo state

Steps:

1. Determine the transcript path. Claude Code stores it at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.

2. Run:
   ```bash
   cd ~/repos/claude-compaction && PYTHONPATH=. python3 -m compaction.cli \
     --transcript "$TRANSCRIPT" \
     --session-id "$SESSION_ID" \
     --cwd "$PWD"
   ```

3. Print the brief path and the memory doc hash to the user. Tell them: **run `/clear` next, then `/handon` when you want the brief restored.** Restoration is explicit — no SessionStart hook auto-injection.

If the trimmer dropped something you actually needed, recall it from the full archive: `memory doc get <hash>`.
