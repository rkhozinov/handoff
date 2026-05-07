#!/usr/bin/env bash
# SessionStart hook (matchers: compact, clear, resume).
# Finds the trimmed brief for the current cwd and prints it to stdout
# so Claude Code injects it into the next conversation context.
#
# Lookup order:
#   1. ~/.claude/compaction/latest-<cwd_slug>.md   (symlink, survives /clear)
#   2. ~/.claude/compaction/<session_id>.md        (legacy session-keyed lookup)
#
# Wire into ~/.claude/settings.json under "hooks.SessionStart" with matchers
# "compact", "clear", and "resume".
set -euo pipefail

BRIEF_DIR="${CLAUDE_COMPACTION_BRIEF_DIR:-$HOME/.claude/compaction}"
MAX_BYTES="${CLAUDE_COMPACTION_MAX_BYTES:-25000}"

input=$(cat)
session=$(echo "$input" | jq -r '.session_id // empty')
cwd=$(echo "$input" | jq -r '.cwd // empty')

# cwd-keyed lookup first (survives /clear)
if [ -n "$cwd" ]; then
  slug="${cwd//\//-}"
  by_cwd="$BRIEF_DIR/latest-${slug}.md"
  if [ -L "$by_cwd" ] || [ -f "$by_cwd" ]; then
    head -c "$MAX_BYTES" "$by_cwd"
    exit 0
  fi
fi

# fallback: session-keyed
if [ -n "$session" ]; then
  brief="$BRIEF_DIR/${session}.md"
  if [ -f "$brief" ]; then
    head -c "$MAX_BYTES" "$brief"
    exit 0
  fi
fi

exit 0
