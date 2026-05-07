# claude-compaction

Deterministic transcript trimmer + memory archive that replaces Claude Code's
lossy default `/compact` for long sessions. Slash command: `/handoff`.

## Why

Claude Code's built-in `/compact` runs an LLM summarizer over the entire
conversation. That summarizer paraphrases code, forgets file paths, drops
direction reversals, and ignores user-supplied "preserve X, Y" hints
(those hints are appended as context, not used as summarizer instructions —
the summarizer prompt is fixed and not user-configurable in the CLI).

Real signal in a Claude Code session is:

| Keep verbatim | Drop |
|---|---|
| User messages (intent, decisions, reversals) | Tool result bodies |
| Substantive assistant text | Thinking blocks |
| Code blocks, file paths, line numbers | Procedural narration ("let me check") |
| Errors, exact strings | Repeated tool retries / dead ends |

This project does deterministic trimming instead of LLM paraphrasing, then
archives the **full** untrimmed transcript as a `memory doc` so anything
dropped is recoverable later via `memory doc search`.

## Architecture

```
PreCompact hook (manual + auto)
        │
        ▼
  trim_and_archive  ──┬──▶ memory doc store      (full session, recoverable)
                      └──▶ ~/.claude/compaction/<session>.md  (trimmed brief)
                                                  ▲
                                                  │
                                  SessionStart(matcher: compact)
                                          stdout → injected into resumed context
```

User-facing flow:

```
[long session, ~70% context]
   ↓
/handoff
   ↓
prints brief path + memory doc hash
   ↓
user runs /clear
   ↓
SessionStart(compact) hook auto-injects brief into next message
```

## Install

```bash
git clone https://github.com/<you>/claude-compaction ~/repos/claude-compaction
cd ~/repos/claude-compaction
./install.sh
```

`install.sh` is idempotent: re-running upgrades the symlinks and patches
`~/.claude/settings.json` without duplicating hook entries.

## Usage

Run `/handoff` when the context starts filling. It:

1. Archives the full session as a `memory doc` (recoverable via `memory doc search`)
2. Writes a deterministic trimmed brief to `~/.claude/compaction/<session_id>.md`

Then run `/clear`. The SessionStart(compact) hook auto-injects the brief into
your next message.

If the trimmer dropped something you actually needed, recall the full archive:
`memory doc get <hash>`.

## Stats (real fixtures)

```
fixture        bytes_in  bytes_out  ratio   user_msgs_kept  decisions  files  code  errors
small.jsonl    560 KB    57 KB      10.18%   9/9              4         10    10    5
medium.jsonl    11 MB    96 KB       0.87%  58/58             6         42     2    5
large.jsonl     12 MB   634 KB       5.22% 144/144           23         91    45    5
huge.jsonl      14 MB   618 KB       4.24% 392/392           64        233    41    5
xhuge.jsonl     83 MB   2.6 MB       3.03% 911/911          154       1146   206    5
```

`xhuge` exceeds the SessionStart 25 KB inject budget by 100×; the head is
auto-injected by the hook, full brief stays on disk for `Read`.

Reproduce: `PYTHONPATH=. python3 scripts/bench.py`

## Tests

```bash
pip install -e ".[dev]"
pytest                               # extract + trim + stats over real fixtures
PYTHONPATH=. python3 scripts/bench.py
PYTHONPATH=. python3 scripts/render_html.py   # docs/report.html visualization
```

`tests/fixtures/raw/` (gitignored) holds real transcripts collected via
`scripts/collect_fixtures.py`. `tests/fixtures/scrubbed/` holds redacted
versions safe to commit, produced by `scripts/scrub.py`.

## Hard invariant

Every real user message must appear verbatim in the brief. Tested across
every fixture. If you ever see this fail in `scripts/bench.py`, that's a
bug — the trimmer dropped user intent.

## What this can't do

- Replace the built-in summarizer prompt — only the API SDK exposes
  `instructions` on the compaction beta header (`compact-2026-01-12`),
  not the CC CLI.
- Survive a process death between PreCompact and SessionStart — relies on
  the brief file persisting on disk (it does).

## Context-fill warning

A `Stop` hook (`hooks/context-warn.sh`) parses the last assistant entry's
`usage` field after every turn and prints a one-line warning to the
session when input tokens cross the threshold:

    ⚠️  Context at 78% (157k/200k tokens, threshold 70%). Run /handoff …

CC has no native "context X% full" event, so this approximates by reading
the live JSONL transcript. Lightweight (single `jq` pass on the last
assistant entry's usage block), fires once per turn.

## Configuration

Hook env vars:

- `CLAUDE_COMPACTION_ROOT` — path to this repo (default `~/repos/claude-compaction`)
- `CLAUDE_COMPACTION_PYTHON` — python interpreter (default `python3`)
- `CLAUDE_COMPACTION_BRIEF_DIR` — where briefs live (default `~/.claude/compaction`)
- `CLAUDE_COMPACTION_MAX_BYTES` — SessionStart injection cap (default 25000)
- `CLAUDE_COMPACTION_CONTEXT_THRESHOLD` — context-warn fraction (default `0.70`)
- `CLAUDE_COMPACTION_CONTEXT_WINDOW` — model context window override in tokens
  (default 200000; set to 1000000 if running Opus 4.x with the 1M extended window)
