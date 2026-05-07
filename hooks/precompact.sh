#!/usr/bin/env bash
# PreCompact hook: runs before Claude Code's default summarizer fires.
# Reads transcript_path/session_id/cwd from stdin JSON, runs the trimmer,
# then emits hookSpecificOutput.additionalContext pointing the model at the
# trimmed brief on disk so it ignores the lossy summarizer narrative.
#
# Wire it into ~/.claude/settings.json under "hooks.PreCompact":
#   {"matcher": "manual", "hooks": [{"type":"command","command":"<this-file>"}]}
#   {"matcher": "auto",   "hooks": [{"type":"command","command":"<this-file>"}]}
set -euo pipefail

PROJECT_ROOT="${CLAUDE_COMPACTION_ROOT:-$HOME/repos/claude-compaction}"
PYTHON="${CLAUDE_COMPACTION_PYTHON:-python3}"

input=$(cat)
transcript=$(echo "$input" | jq -r '.transcript_path // empty')
session=$(echo "$input" | jq -r '.session_id // empty')
cwd=$(echo "$input" | jq -r '.cwd // empty')

if [ -z "$transcript" ] || [ -z "$session" ]; then
  echo "[precompact] missing transcript_path or session_id in hook input" >&2
  exit 0
fi

brief_path=$(
  cd "$PROJECT_ROOT" && \
  PYTHONPATH=. "$PYTHON" -m compaction.cli \
    --transcript "$transcript" \
    --session-id "$session" \
    --cwd "${cwd:-$PWD}" 2>/tmp/cc-handoff-stderr.log
) || {
  echo "[precompact] cc-handoff failed; see /tmp/cc-handoff-stderr.log" >&2
  exit 0
}

jq -n --arg path "$brief_path" '{
  hookSpecificOutput: {
    hookEventName: "PreCompact",
    additionalContext: ("Default compaction is unreliable. Deterministic trimmed brief written to " + $path + ". After this turn, use the Read tool to load that file and treat it as ground truth over the summarizer narrative.")
  }
}'
