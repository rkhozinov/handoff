#!/usr/bin/env bash
# Install hooks + slash commands into ~/.claude.
# Symlinks (so updates to this repo propagate without re-running install).
# Settings.json is patched idempotently — re-running is safe.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CC_HOME="${CLAUDE_HOME:-$HOME/.claude}"

echo "Installing claude-compaction from $ROOT into $CC_HOME"

# 1. Make scripts executable
chmod +x "$ROOT/hooks/precompact.sh" "$ROOT/hooks/context-warn.sh"

# 2. Ensure target dirs exist
mkdir -p "$CC_HOME/hooks" "$CC_HOME/commands" "$CC_HOME/compaction"

# 3. Symlink hooks (force-replace if a stale symlink exists)
for h in precompact.sh context-warn.sh; do
  ln -sf "$ROOT/hooks/$h" "$CC_HOME/hooks/$h"
  echo "  hook: $CC_HOME/hooks/$h -> $ROOT/hooks/$h"
done

# Clean up legacy postcompact-restore symlink (the hook is gone — /handon replaces it).
rm -f "$CC_HOME/hooks/postcompact-restore.sh"

# 4. Symlink slash commands
for c in handoff.md handon.md; do
  ln -sf "$ROOT/commands/$c" "$CC_HOME/commands/$c"
  echo "  command: $CC_HOME/commands/$c -> $ROOT/commands/$c"
done

# 5. Patch settings.json (idempotent)
SETTINGS="$CC_HOME/settings.json"
if [ ! -f "$SETTINGS" ]; then
  echo "{}" > "$SETTINGS"
fi

PATCHED=$(python3 - "$SETTINGS" "$CC_HOME/hooks/precompact.sh" "$CC_HOME/hooks/context-warn.sh" <<'PY'
import json, sys
path, precompact_cmd, context_cmd = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)

hooks = data.setdefault("hooks", {})

def has(entry_list, command):
    return any(
        any(h.get("command") == command for h in entry.get("hooks", []))
        for entry in entry_list
    )

# PreCompact: both manual and auto matchers
pre = hooks.setdefault("PreCompact", [])
for matcher in ("manual", "auto"):
    if not any(e.get("matcher") == matcher and has([e], precompact_cmd) for e in pre):
        pre.append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": precompact_cmd, "timeout": 30000}],
        })

# Strip any legacy SessionStart entries that pointed at the removed
# postcompact-restore.sh hook. /handon is now the explicit restore path.
ss = hooks.get("SessionStart")
if isinstance(ss, list):
    cleaned = [
        e for e in ss
        if not any(
            "postcompact-restore.sh" in (h.get("command") or "")
            for h in e.get("hooks", [])
        )
    ]
    if cleaned:
        hooks["SessionStart"] = cleaned
    else:
        hooks.pop("SessionStart", None)

# Stop: context-fill warning. Surfaces a one-line warning in the user's
# transcript when context crosses CLAUDE_COMPACTION_CONTEXT_THRESHOLD
# (default 70%) of the model window.
stop = hooks.setdefault("Stop", [])
if not any(e.get("matcher") in ("", None) and has([e], context_cmd) for e in stop):
    stop.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": context_cmd, "timeout": 3000}],
    })

with open(path, "w") as f:
    json.dump(data, f, indent=2)
print("OK")
PY
)
echo "  settings.json: $PATCHED"

echo
echo "Installed. Test:"
echo "  python3 -m pytest $ROOT/tests/"
echo "  PYTHONPATH=$ROOT python3 $ROOT/scripts/bench.py"
echo
echo "Use /handoff before running /clear."
