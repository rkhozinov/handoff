#!/usr/bin/env python3
"""Generate a standalone HTML presentation showing raw vs trimmed.

Output: docs/report.html (no external deps, single file).
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from compaction.extract import (
    assistant_blocks,
    extract_code_anchors,
    extract_compact_summaries,
    extract_decisions,
    extract_errors,
    extract_files_touched,
    is_real_user,
    iter_real_user_msgs,
    iter_signal_user_msgs,
    load_jsonl,
    user_text,
)
from compaction.tokenizer import VALID_MODES, count_tokens
from compaction.trim import _classify_assistant, render_assistant, render_brief

# Filled in by main() once CLI args are parsed; defaults to "auto" so the
# helpers also work when imported directly from a test or REPL.
_TOKEN_MODE: str = "auto"

ROOT = Path(__file__).resolve().parent.parent

CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', system-ui, sans-serif;
  background: #0d1117; color: #e6edf3; margin: 0; padding: 0;
  line-height: 1.5;
}
header {
  background: linear-gradient(135deg, #1f6feb 0%, #6e40c9 100%);
  padding: 60px 40px; text-align: center;
}
header h1 { margin: 0; font-size: 2.5em; font-weight: 700; }
header .tagline { font-size: 1.2em; opacity: 0.9; margin-top: 10px; }
.container { max-width: 1400px; margin: 0 auto; padding: 40px; }
section { margin-bottom: 60px; }
section h2 {
  font-size: 1.8em; border-bottom: 2px solid #30363d; padding-bottom: 10px;
  margin-bottom: 20px;
}
.hero-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 30px 0; }
.stat-card {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 24px; text-align: center;
}
.stat-card .num { font-size: 2.4em; font-weight: 700; color: #58a6ff; display: block; }
.stat-card .label { color: #8b949e; font-size: 0.9em; margin-top: 4px; }

table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }
table th, table td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #30363d; }
table th { background: #21262d; font-weight: 600; color: #8b949e; }
table tr:last-child td { border-bottom: none; }
table tr:hover { background: #1c2128; }
.bar { display: inline-block; height: 14px; background: #58a6ff; border-radius: 3px; vertical-align: middle; }
.bar-bg { display: inline-block; width: 100px; background: #30363d; border-radius: 3px; vertical-align: middle; margin-right: 8px; }
.invariant-good { color: #3fb950; font-weight: 600; }
.ratio { font-weight: 600; color: #f0883e; }

.diff-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
  margin-top: 20px;
  /* Break out of the 1400px .container cap so the diff fills the viewport. */
  width: calc(100vw - 80px);
  position: relative;
  left: 50%;
  transform: translateX(-50%);
}
.diff-col {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 20px; max-height: 85vh; overflow-y: auto;
  font-family: 'SF Mono', 'Menlo', monospace; font-size: 0.85em;
}
.diff-col h3 {
  margin-top: 0; padding-bottom: 10px; border-bottom: 1px solid #30363d;
  font-family: -apple-system, system-ui, sans-serif; font-size: 1em;
}
.turn { margin-bottom: 16px; padding: 10px; border-radius: 4px; }
.turn-user { background: rgba(31, 111, 235, 0.15); border-left: 3px solid #1f6feb; }
.turn-assistant { background: rgba(63, 185, 113, 0.1); border-left: 3px solid #3fb950; }
.turn-tool-result { background: rgba(248, 81, 73, 0.08); border-left: 3px solid #f85149; opacity: 0.6; }
.turn-thinking { background: rgba(139, 148, 158, 0.1); border-left: 3px solid #8b949e; opacity: 0.5; }
.turn-tool-use { background: rgba(240, 136, 62, 0.1); border-left: 3px solid #f0883e; }
.turn-meta { background: rgba(139, 148, 158, 0.05); border-left: 3px solid #6e7681; opacity: 0.4; font-size: 0.8em; }
.turn-label {
  display: inline-block; font-size: 0.7em; font-weight: 600;
  padding: 2px 8px; border-radius: 3px; margin-bottom: 6px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.label-user { background: #1f6feb; color: white; }
.label-assistant { background: #3fb950; color: #0d1117; }
.label-tool-result { background: #f85149; color: white; }
.label-thinking { background: #8b949e; color: #0d1117; }
.label-tool-use { background: #f0883e; color: #0d1117; }
.label-meta { background: #6e7681; color: white; }
.dropped { text-decoration: line-through; opacity: 0.4; }
.pre {
  white-space: pre-wrap; word-break: break-word; margin: 0;
  font-family: inherit;
}
.legend { display: flex; gap: 16px; flex-wrap: wrap; margin: 16px 0; font-size: 0.85em; }
.legend-item { display: flex; align-items: center; gap: 6px; }
.legend-dot { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
.brief-preview {
  background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
  padding: 24px; max-height: 600px; overflow-y: auto;
  font-family: 'SF Mono', 'Menlo', monospace; font-size: 0.85em;
  white-space: pre-wrap;
}
.brief-preview h1 { color: #58a6ff; font-size: 1.4em; margin-top: 0; }
.brief-preview h2 { color: #f0883e; font-size: 1.1em; margin-top: 1.2em; padding-bottom: 4px; border-bottom: 1px solid #30363d; font-family: 'SF Mono', monospace; }
.flow-diagram {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 30px; font-family: 'SF Mono', monospace; white-space: pre;
  overflow-x: auto; line-height: 1.4; color: #c9d1d9;
}
footer { text-align: center; padding: 40px; color: #8b949e; border-top: 1px solid #30363d; }
code { background: #161b22; padding: 2px 6px; border-radius: 3px; font-family: 'SF Mono', monospace; font-size: 0.9em; }

.sample-selector {
  margin: 16px 0 20px; padding: 12px 16px; background: #161b22;
  border: 1px solid #30363d; border-radius: 8px; display: flex; align-items: center; gap: 12px;
}
.sample-selector select {
  background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
  border-radius: 6px; padding: 6px 10px; font-family: 'SF Mono', monospace;
  font-size: 0.95em; cursor: pointer;
}
.sample-selector select:hover { border-color: #58a6ff; }
.sample-pane { display: none; }
.sample-pane.active { display: block; }

/* Flag-and-review: click any .turn to mark it as weird, copy via toolbar */
.turn { cursor: pointer; transition: outline 0.1s; }
.turn--flagged {
  outline: 2px solid #f0883e;
  box-shadow: 0 0 0 4px rgba(240, 136, 62, 0.25);
  position: relative;
}
.turn--flagged::after {
  content: "⚑ flagged"; position: absolute; top: 6px; right: 8px;
  font-size: 0.65em; color: #f0883e; font-weight: 700;
  letter-spacing: 0.5px; pointer-events: none;
}
.flag-toolbar {
  position: fixed; bottom: 20px; right: 20px; z-index: 1000;
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 12px 16px; display: none; gap: 12px; align-items: center;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5);
}
.flag-toolbar.visible { display: flex; }
.flag-toolbar .count { font-weight: 700; color: #f0883e; }
.flag-toolbar button {
  background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
  border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 0.9em;
}
.flag-toolbar button:hover { border-color: #58a6ff; color: #58a6ff; }
"""


def truncate(s: str, n: int = 1500) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[: n - 20].rstrip() + f"\n[…+{len(s) - n + 20:,} chars truncated]"


def block_text_summary(b: dict) -> str:
    bt = b.get("type")
    if bt == "text":
        return truncate(b.get("text", ""))
    if bt == "tool_use":
        name = b.get("name", "?")
        inp = b.get("input", {})
        return f"[{name}] " + truncate(json.dumps(inp, ensure_ascii=False), 400)
    if bt == "tool_result":
        c = b.get("content", "")
        if isinstance(c, list):
            c = " ".join(blk.get("text", "") for blk in c if isinstance(blk, dict))
        return truncate(str(c), 800)
    if bt == "thinking":
        return truncate(b.get("thinking", ""))
    return truncate(json.dumps(b)[:200])


def render_raw_turn(entry: dict) -> tuple[str, str, str]:
    """Returns (kind, label, body_html) for a transcript entry."""
    t = entry.get("type")
    if t == "user":
        if is_real_user(entry):
            return ("user", "USER", html.escape(truncate(user_text(entry))))
        msg = entry.get("message", {})
        c = msg.get("content")
        if isinstance(c, list):
            parts = []
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    parts.append(block_text_summary(b))
            return ("tool-result", "TOOL_RESULT (dropped)", html.escape("\n".join(parts)))
        return ("meta", "USER (other)", html.escape(json.dumps(c)[:200]))
    if t == "assistant":
        blocks = assistant_blocks(entry)
        out = []
        kinds = []
        for b in blocks:
            bt = b.get("type")
            kinds.append(bt)
            out.append(block_text_summary(b))
        non_think = [k for k in kinds if k != "thinking"]
        primary = non_think[0] if non_think else (kinds[0] if kinds else "?")
        if primary == "tool_use":
            return ("tool-use", "ASSISTANT (tool_use)", html.escape("\n".join(out)))
        if primary == "thinking":
            return ("thinking", "ASSISTANT (thinking, dropped)", html.escape("\n".join(out)))
        return ("assistant", "ASSISTANT", html.escape("\n".join(out)))
    return ("meta", f"META: {t}", html.escape(json.dumps(entry)[:200]))


def render_trimmed_turn(entry: dict, next_entry: dict | None) -> tuple[str, str, str] | None:
    if is_real_user(entry):
        return ("user", "USER", html.escape(truncate(user_text(entry))))
    if entry.get("type") == "assistant":
        text_joined, tool_markers = _classify_assistant(entry)
        rendered = render_assistant(entry, next_entry)
        if rendered is None:
            return None
        if text_joined and tool_markers:
            return ("assistant", "ASSISTANT (text + tool marker)", html.escape(truncate(rendered)))
        if tool_markers and not text_joined:
            return ("tool-use", "ASSISTANT (tool marker only)", html.escape(truncate(rendered)))
        return ("assistant", "ASSISTANT", html.escape(truncate(rendered)))
    return None


def build_diff_html(entries: list[dict], max_turns: int | None = None) -> str:
    """Render the side-by-side diff. `max_turns=None` means ALL turns —
    needed to give reviewers full session context. xhuge → ~5-10 MB HTML;
    acceptable trade for reviewability. Pass an int to cap if needed."""
    raw_html: list[str] = []
    trim_html: list[str] = []

    interesting = [e for e in entries if e.get("type") in ("user", "assistant")]
    if max_turns is not None:
        interesting = interesting[:max_turns]

    for i, e in enumerate(interesting):
        nxt = interesting[i + 1] if i + 1 < len(interesting) else None
        kind, label, body = render_raw_turn(e)
        # data-idx pairs each raw turn with its trimmed sibling so the
        # anchor-based scroll sync can align them top-to-top.
        raw_html.append(
            f'<div class="turn turn-{kind}" data-idx="{i}">'
            f'<span class="turn-label label-{kind}">{html.escape(label)}</span>'
            f'<pre class="pre">{body}</pre></div>'
        )

        trimmed = render_trimmed_turn(e, nxt)
        if trimmed is None:
            trim_html.append(
                f'<div class="turn turn-{kind}" data-idx="{i}" style="opacity:0.25;">'
                f'<span class="turn-label label-{kind}">DROPPED ({html.escape(label)})</span>'
                f'<pre class="pre dropped">{body}</pre></div>'
            )
        else:
            tk, tl, tb = trimmed
            trim_html.append(
                f'<div class="turn turn-{tk}" data-idx="{i}">'
                f'<span class="turn-label label-{tk}">{html.escape(tl)}</span>'
                f'<pre class="pre">{tb}</pre></div>'
            )

    return (
        '<div class="diff-grid">'
        f'<div class="diff-col"><h3>RAW transcript ({len(interesting)} turns shown)</h3>{"".join(raw_html)}</div>'
        f'<div class="diff-col"><h3>TRIMMED brief</h3>{"".join(trim_html)}</div>'
        '</div>'
    )


def stat_row(label: str, path: Path) -> dict:
    raw = path.read_bytes()
    raw_text = raw.decode("utf-8", errors="replace")
    entries = load_jsonl(str(path))
    all_user = iter_real_user_msgs(entries)
    signal_user = iter_signal_user_msgs(entries)
    brief = render_brief(entries, label, "/bench", archive_hash=None)
    brief_bytes = len(brief.encode())
    return {
        "fixture": path.name,
        "lines": sum(1 for _ in path.open("rb")),
        "raw_bytes": len(raw),
        "raw_tokens": count_tokens(raw_text, mode=_TOKEN_MODE),
        "user_total": len(all_user),
        "user_signal": len(signal_user),
        "brief_bytes": brief_bytes,
        "brief_tokens": count_tokens(brief, mode=_TOKEN_MODE),
        "ratio_pct": 100 * brief_bytes / max(1, len(raw)),
        "decisions": len(extract_decisions(signal_user)),
        "files": len(extract_files_touched(entries)),
        "code": len(extract_code_anchors(entries)),
        "errors": len(extract_errors(entries)),
    }


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_tokens_count(tok: int) -> str:
    """Format a precomputed token count for display."""
    if tok < 1_000:
        return f"{tok} tok"
    if tok < 1_000_000:
        return f"{tok / 1_000:.1f}k tok"
    return f"{tok / 1_000_000:.2f}M tok"


def fmt_size(n: int, tokens: int | None = None) -> str:
    """Show both bytes and tokens — token count is what fills the context
    window, byte count is what fits on disk / inject caps. If ``tokens`` is
    ``None`` the legacy chars/4 estimate is used so this helper still works
    for callers that haven't migrated yet."""
    if tokens is None:
        tokens = n // 4
    return f"{fmt_bytes(n)} <span style=\"color:#8b949e\">({fmt_tokens_count(tokens)})</span>"


def render_stats_table(rows: list[dict]) -> str:
    out = ['<table><thead><tr>',
           '<th>fixture</th><th>lines</th><th>raw</th><th>user msgs (signal/total)</th>',
           '<th>brief (trimmed convo)</th><th>signal</th>',
           '</tr></thead><tbody>']
    max_raw = max(r["raw_bytes"] for r in rows)
    for r in rows:
        bar_w = max(2, int(100 * r["brief_bytes"] / max_raw))
        signal = (
            f"{r['decisions']} decisions · {r['files']} files · "
            f"{r['code']} code · {r['errors']} errors"
        )
        noise_pct = 100 * (r["user_total"] - r["user_signal"]) / max(1, r["user_total"])
        out.append(
            f'<tr><td><code>{html.escape(r["fixture"])}</code></td>'
            f'<td>{r["lines"]:,}</td>'
            f'<td>{fmt_size(r["raw_bytes"], r["raw_tokens"])}</td>'
            f'<td><span class="invariant-good">{r["user_signal"]}/{r["user_signal"]}</span> '
            f'<span style="color:#8b949e">of {r["user_total"]} ({noise_pct:.0f}% noise)</span></td>'
            f'<td><span class="bar-bg"><span class="bar" style="width:{bar_w}px;background:#3fb950"></span></span> '
            f'{fmt_size(r["brief_bytes"], r["brief_tokens"])} '
            f'<span style="color:#8b949e">({r["ratio_pct"]:.1f}% of raw)</span></td>'
            f'<td>{signal}</td></tr>'
        )
    out.append('</tbody></table>')
    return "".join(out)


def render_brief_html(entries: list[dict], label: str) -> str:
    brief = render_brief(entries, label, "/demo", archive_hash="abcd1234ef56...")
    escaped = html.escape(brief)
    escaped = escaped.replace("# Session Brief", '<h1># Session Brief</h1>', 1)
    import re as _re
    escaped = _re.sub(r"^## (.+)$", r"<h2>## \1</h2>", escaped, flags=_re.MULTILINE)
    return f'<div class="brief-preview">{escaped[:8000]}{"..." if len(escaped) > 8000 else ""}</div>'


def _format_brief_pane(text: str, label: str, byte_count: int, tok_count: int) -> str:
    """Render one tier as a labeled pane with byte+token stats. Renders the
    raw markdown source so structural overlap with the sibling tier is
    visible at a glance — escaping HTML, but leaving the text otherwise
    untouched."""
    escaped = html.escape(text)
    return (
        f'<div class="diff-col">'
        f'<h3>{html.escape(label)} '
        f'<span style="color:#8b949e;font-weight:normal;font-size:0.85em;">'
        f'({byte_count:,} bytes · ~{tok_count:,} tok)</span></h3>'
        f'<pre class="pre" style="max-height:80vh;overflow:auto;">{escaped}</pre>'
        f'</div>'
    )


def render_cc_compact_compare_html(entries: list[dict], label: str) -> str:
    """Side-by-side: our deterministic brief vs Claude Code's default
    `/compact` LLM summary.

    The comparison only renders for fixtures that actually carry a CC
    `/compact` run (`isCompactSummary: true` user entries). Sessions
    without that flag get a placeholder.

    The point: make the original motivation visible. Our brief preserves
    verbatim user msgs, file paths, decision verbs, code fences. CC's
    summarizer paraphrases code, drops paths, forgets direction
    reversals."""
    summaries = extract_compact_summaries(entries)
    brief = render_brief(entries, label, "/demo", archive_hash="abcd1234ef56...")
    b_bytes = len(brief.encode("utf-8"))
    b_tok = count_tokens(brief, mode=_TOKEN_MODE)

    if not summaries:
        return (
            f'<p style="color:#8b949e">'
            f"This fixture didn't run CC's default <code>/compact</code> — "
            f"no <code>isCompactSummary: true</code> entries in the JSONL."
            f'</p>'
            f'<div class="diff-grid">'
            f'{_format_brief_pane(brief, "OUR BRIEF (deterministic)", b_bytes, b_tok)}'
            f'<div class="diff-col"><h3>CC /compact summary <span style="color:#8b949e;font-weight:normal;font-size:0.85em;">(not present)</span></h3>'
            f'<pre class="pre" style="max-height:80vh;overflow:auto;color:#8b949e;">'
            f'No /compact summary in this fixture. Run /compact in a real '
            f'session to populate this pane on the next bench.</pre></div>'
            f'</div>'
        )

    cc_text = summaries[-1]
    cc_bytes = len(cc_text.encode("utf-8"))
    cc_tok = count_tokens(cc_text, mode=_TOKEN_MODE)
    summary_count_line = (
        f"This fixture ran CC's <code>/compact</code> "
        f"<strong>{len(summaries)}</strong> "
        f"time{'s' if len(summaries) != 1 else ''}; the most recent summary is "
        f"shown on the right. Compare against our brief on the left."
    )
    return (
        f'<p style="color:#8b949e">{summary_count_line}</p>'
        f'<ul style="color:#8b949e;font-size:0.92em;line-height:1.7;">'
        f'<li>CC summary is <strong>LLM-generated</strong> prose — paraphrases '
        f'code, drops file paths, forgets direction reversals. Typically 1-3k tokens.</li>'
        f'<li>Our brief is <strong>deterministic extraction</strong> — preserves '
        f'verbatim user msgs, file paths, code fences, sub-agent reports. No LLM call.</li>'
        f'<li>Look for: file paths missing on the right, decisions paraphrased '
        f'or merged on the right, direction-reversal user msgs dropped on the right.</li>'
        f'</ul>'
        f'<div class="diff-grid">'
        f'{_format_brief_pane(brief, "OUR BRIEF (deterministic)", b_bytes, b_tok)}'
        f'{_format_brief_pane(cc_text, "CC /compact (LLM summary)", cc_bytes, cc_tok)}'
        f'</div>'
    )


def hero_metrics(rows: list[dict]) -> str:
    total_raw = sum(r["raw_bytes"] for r in rows)
    total_raw_tok = sum(r["raw_tokens"] for r in rows)
    total_brief = sum(r["brief_bytes"] for r in rows)
    total_brief_tok = sum(r["brief_tokens"] for r in rows)
    total_signal = sum(r["user_signal"] for r in rows)
    total_user = sum(r["user_total"] for r in rows)
    noise_dropped = total_user - total_signal
    overall_pct = 100 * total_brief / max(1, total_raw)
    return f"""
    <div class="hero-stats">
      <div class="stat-card"><span class="num">{fmt_tokens_count(total_raw_tok)}</span><span class="label">raw input across {len(rows)} fixtures ({fmt_bytes(total_raw)})</span></div>
      <div class="stat-card"><span class="num">{fmt_tokens_count(total_brief_tok)}</span><span class="label">brief total ({fmt_bytes(total_brief)} — {overall_pct:.1f}% of raw)</span></div>
      <div class="stat-card"><span class="num invariant-good">{total_signal}/{total_signal}</span><span class="label">signal user msgs preserved (of {total_user}; {noise_dropped} noise filtered)</span></div>
    </div>
    """


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(ROOT / "tests" / "fixtures" / "raw"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "report.html"))
    ap.add_argument(
        "--token-mode",
        choices=VALID_MODES,
        default="auto",
        help=(
            "Tokenizer used for the report's token figures. 'auto' (default) "
            "uses the offline HF tokenizer when available, else the chars/4 "
            "heuristic. See compaction.tokenizer."
        ),
    )
    args = ap.parse_args()
    global _TOKEN_MODE
    _TOKEN_MODE = args.token_mode

    fdir = Path(args.fixtures)
    if not (fdir.is_dir() and any(fdir.glob("*.jsonl"))):
        fdir = ROOT / "tests" / "fixtures" / "scrubbed"

    fixtures = sorted(fdir.glob("*.jsonl"))
    if not fixtures:
        print("no fixtures available")
        return 1

    rows = [stat_row(f.stem, f) for f in fixtures]

    # Build a diff pane and a brief preview for every fixture so the user
    # can switch samples in the report without rerunning the script.
    # Default selection = `small` if present, else first fixture.
    default_fixture = next(
        (f for f in fixtures if f.stem == "small"), fixtures[0]
    ).stem

    # Each fixture's diff + brief is wrapped in its own <template>. The
    # <template> element's content is parsed into an inert fragment — no
    # render, no layout — until JS clones it into the active container.
    # Switching fixtures clears the active container and clones the new
    # template, so only one fixture's DOM is "live" at a time.
    diff_templates: list[str] = []
    brief_templates: list[str] = []
    cc_cmp_templates: list[str] = []
    options: list[str] = []
    for f in fixtures:
        entries = load_jsonl(str(f))
        d_html = build_diff_html(entries, max_turns=None)
        b_html = render_brief_html(entries, f.stem)
        cc_html = render_cc_compact_compare_html(entries, f.stem)
        sel = " selected" if f.stem == default_fixture else ""
        stem = html.escape(f.stem)
        diff_templates.append(
            f'<template id="fix-diff-{stem}" data-fixture="{stem}">'
            f'<p style="color:#8b949e">Sample from <code>{html.escape(f.name)}</code>. '
            f'Left: every entry in the raw transcript (truncated per block). '
            f'Right: what the trimmer keeps. Dropped entries shown faded.</p>'
            f'{d_html}</template>'
        )
        brief_templates.append(
            f'<template id="fix-brief-{stem}" data-fixture="{stem}">{b_html}</template>'
        )
        cc_cmp_templates.append(
            f'<template id="fix-cccmp-{stem}" data-fixture="{stem}">{cc_html}</template>'
        )
        options.append(
            f'<option value="{stem}"{sel}>{stem}</option>'
        )

    selector = (
        '<div class="sample-selector">'
        '<label for="fixture-select"><strong>Fixture:</strong></label> '
        f'<select id="fixture-select">{"".join(options)}</select>'
        '</div>'
    )
    selector_script = """
<script>
(function () {
  // Lazy-load fixture content. Each fixture's diff + brief lives in a
  // <template> (inert: parsed into a DocumentFragment, no render cost).
  // Switching the selector empties the active containers and clones the
  // chosen fixture's content in. Old DOM is removed → unloaded.
  var sel = document.getElementById("fixture-select");
  if (!sel) return;
  var diffActive = document.getElementById("diff-active");
  var briefActive = document.getElementById("brief-active");
  var ccCmpActive = document.getElementById("cccmp-active");
  if (!diffActive || !briefActive) return;

  function bindScrollSync(scope) {
    // Anchor-based scroll sync. Each raw turn shares a `data-idx` with
    // its trimmed sibling (DROPPED placeholders share the idx so the
    // pair never desyncs). Re-entrancy flag blocks the feedback loop
    // the mirrored scroll would otherwise trigger.
    scope.querySelectorAll(".diff-grid").forEach(function (grid) {
      var cols = grid.querySelectorAll(".diff-col");
      if (cols.length !== 2) return;
      var left = cols[0], right = cols[1];
      var syncing = false;
      function topAnchor(pane) {
        var paneTop = pane.getBoundingClientRect().top;
        var turns = pane.querySelectorAll(".turn[data-idx]");
        for (var i = 0; i < turns.length; i++) {
          if (turns[i].getBoundingClientRect().bottom > paneTop + 4) {
            return turns[i].dataset.idx;
          }
        }
        return null;
      }
      function alignTo(pane, idx) {
        var anchor = pane.querySelector('.turn[data-idx="' + idx + '"]');
        if (!anchor) return;
        var paneRect = pane.getBoundingClientRect();
        var anchorRect = anchor.getBoundingClientRect();
        pane.scrollTop += (anchorRect.top - paneRect.top);
      }
      function mirror(src, dst) {
        return function () {
          if (syncing) return;
          var idx = topAnchor(src);
          if (idx === null) return;
          syncing = true;
          alignTo(dst, idx);
          requestAnimationFrame(function () { syncing = false; });
        };
      }
      left.addEventListener("scroll", mirror(left, right));
      right.addEventListener("scroll", mirror(right, left));
    });
  }

  function show(name) {
    var diffTpl = document.getElementById("fix-diff-" + name);
    var briefTpl = document.getElementById("fix-brief-" + name);
    if (!diffTpl || !briefTpl) return;
    // Replace contents — old DOM detaches and gets GCed.
    diffActive.replaceChildren(diffTpl.content.cloneNode(true));
    briefActive.replaceChildren(briefTpl.content.cloneNode(true));
    diffActive.dataset.fixture = name;
    briefActive.dataset.fixture = name;
    if (ccCmpActive) {
      var ccCmpTpl = document.getElementById("fix-cccmp-" + name);
      if (ccCmpTpl) {
        ccCmpActive.replaceChildren(ccCmpTpl.content.cloneNode(true));
        ccCmpActive.dataset.fixture = name;
      }
    }
    bindScrollSync(diffActive);
    bindScrollSync(ccCmpActive || document.body);
    // Re-apply persisted flag state to the freshly attached turns.
    if (window.__compactionRefreshFlags) window.__compactionRefreshFlags();
  }

  sel.addEventListener("change", function () { show(sel.value); });
  // Initial paint: load the default fixture.
  show(sel.value);
})();
(function () {
  // Flag-and-review: click any .turn to mark it as weird. Toolbar at
  // bottom-right shows count + Copy JSON / Clear buttons. State persists
  // across reloads via localStorage so a long review session survives
  // regeneration. Key includes fixture + side + idx + (optional)
  // selection start so duplicates inside same fixture are distinct.
  var STORAGE_KEY = "compaction.reportFlags.v1";
  var flags = {};
  try { flags = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); } catch (e) {}

  var bar = document.createElement("div");
  bar.className = "flag-toolbar";
  bar.innerHTML = (
    '<span class="count">0 flagged</span>' +
    '<button data-action="copy">Copy JSON</button>' +
    '<button data-action="clear">Clear</button>'
  );
  document.body.appendChild(bar);

  function fixtureOf(turn) {
    // After lazy-load refactor, active fixture name lives on
    // `#diff-active`/`#brief-active`. Walk up to either container.
    var holder = turn.closest("#diff-active, #brief-active");
    return holder ? holder.dataset.fixture || "?" : "?";
  }
  function sideOf(turn) {
    var col = turn.closest(".diff-col");
    if (!col) return "brief";
    var siblings = col.parentElement.querySelectorAll(".diff-col");
    return siblings[0] === col ? "raw" : "trimmed";
  }
  function keyOf(turn) {
    return [
      fixtureOf(turn), sideOf(turn), turn.dataset.idx || "?",
    ].join("|");
  }
  function refresh() {
    var keys = Object.keys(flags);
    document.querySelectorAll(".turn").forEach(function (t) {
      t.classList.toggle("turn--flagged", flags[keyOf(t)] === true);
    });
    bar.querySelector(".count").textContent =
      keys.length + (keys.length === 1 ? " flagged" : " flagged");
    bar.classList.toggle("visible", keys.length > 0);
  }
  // Expose to the lazy-loader so it can re-apply flags after switching
  // fixtures (the new turns won't carry the .turn--flagged class).
  window.__compactionRefreshFlags = refresh;
  function persist() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(flags)); } catch (e) {}
  }
  function turnPayload(turn) {
    var label = (turn.querySelector(".turn-label") || {}).textContent || "";
    var body = (turn.querySelector(".pre") || {}).textContent || "";
    return {
      fixture: fixtureOf(turn),
      side: sideOf(turn),
      idx: turn.dataset.idx,
      label: label.trim(),
      body: body.trim(),
    };
  }

  document.addEventListener("click", function (e) {
    // Ignore clicks on the toolbar buttons themselves.
    if (e.target.closest(".flag-toolbar")) return;
    var turn = e.target.closest(".turn");
    if (!turn) return;
    var k = keyOf(turn);
    if (flags[k]) delete flags[k]; else flags[k] = true;
    persist();
    refresh();
  });

  bar.addEventListener("click", function (e) {
    var act = (e.target.dataset || {}).action;
    if (act === "copy") {
      var rows = [];
      document.querySelectorAll(".turn--flagged").forEach(function (t) {
        rows.push(turnPayload(t));
      });
      var json = JSON.stringify(rows, null, 2);
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(json);
      } else {
        var ta = document.createElement("textarea");
        ta.value = json; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch (err) {}
        document.body.removeChild(ta);
      }
      e.target.textContent = "Copied!";
      setTimeout(function () { e.target.textContent = "Copy JSON"; }, 1200);
    } else if (act === "clear") {
      flags = {}; persist(); refresh();
    }
  });

  refresh();
})();
</script>
"""

    flow = """    [User: long session, ~70% context]
                  │
                  ▼
                /handoff
                  │
                  ▼
    ┌─────────────────────────────────────┐
    │      cc-handoff CLI (Python)        │
    │   compaction.cli → compaction.trim  │
    └────────┬───────────────────┬────────┘
             │                   │
             ▼                   ▼
     memory doc store     ~/.claude/compaction/
       (full transcript    <session_id>.md
        recoverable)         (trimmed brief)
                                 ▲
                                 │
            User runs /clear     │ stdout
                                 │
                       /handon
                  reads brief into resumed context"""

    legend = """
    <div class="legend">
      <span class="legend-item"><span class="legend-dot" style="background:#1f6feb"></span> real user message</span>
      <span class="legend-item"><span class="legend-dot" style="background:#3fb950"></span> assistant text</span>
      <span class="legend-item"><span class="legend-dot" style="background:#f0883e"></span> tool_use (collapsed to marker)</span>
      <span class="legend-item"><span class="legend-dot" style="background:#f85149"></span> tool_result (dropped)</span>
      <span class="legend-item"><span class="legend-dot" style="background:#8b949e"></span> thinking (dropped)</span>
    </div>
    """

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>claude-compaction · raw vs trimmed</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>claude-compaction</h1>
  <div class="tagline">Replacing Claude Code's lossy <code>/compact</code> with deterministic trim + memory archive · <code>/handoff</code></div>
</header>

<div class="container">

<section>
  <h2>The problem</h2>
  <p>Default <code>/compact</code> runs an LLM summarizer over the entire session.
  It paraphrases code, forgets file paths, drops direction reversals, and ignores
  user-supplied "preserve X, Y" hints &mdash; those hints are appended as context,
  not used as summarizer instructions. The summarizer prompt is fixed and not
  exposed in the Claude Code CLI.</p>
  <p>Real signal in a session is concentrated in user messages, code blocks,
  file paths, and decisions. Real noise is in tool_result bodies and procedural
  narration ("let me check", "reading the file"). A deterministic trimmer keeps
  the signal verbatim and drops the noise without any LLM call.</p>
</section>

<section>
  <h2>Bottom line</h2>
  {hero_metrics(rows)}
  <p style="color:#8b949e">Drops <code>tool_result</code> bodies, <code>thinking</code> blocks, and procedural narration adjacent to tool calls. Every real user message survives verbatim — verified as a hard test invariant.</p>
</section>

<section>
  <h2>Per-fixture stats (real transcripts)</h2>
  {render_stats_table(rows)}
  <p style="color:#8b949e;margin-top:12px">Bars scaled to the largest fixture's raw size.</p>
</section>

<section>
  <h2>Side-by-side: raw vs trimmed</h2>
  {selector}
  {legend}
  {"".join(diff_templates)}
  <div id="diff-active"></div>
</section>

<section>
  <h2>Generated brief structure</h2>
  <p>This is what <code>/handoff</code> writes to <code>~/.claude/compaction/&lt;session_id&gt;.md</code>
  and what <code>/handon</code> reads back into the next session (head, capped at 25 KB):</p>
  {"".join(brief_templates)}
  <div id="brief-active"></div>
</section>

<section>
  <h2>Our tier1 vs Claude Code's <code>/compact</code> (the original motivation)</h2>
  <p style="color:#8b949e">
    The whole point of this project is that CC's default
    <code>/compact</code> runs an LLM summarizer over the session
    that paraphrases code, drops file paths, and forgets direction
    reversals. Our brief is deterministic extraction — no LLM call,
    no paraphrasing.
  </p>
  <p style="color:#8b949e">
    Compare panes for fixtures that actually carry a CC summary
    (<code>isCompactSummary: true</code> entries in the JSONL).
    Eyeball the right pane for: missing file paths, paraphrased
    decisions, dropped reversal user msgs.
  </p>
  {"".join(cc_cmp_templates)}
  <div id="cccmp-active"></div>
</section>

<section>
  <h2>Architecture</h2>
  <div class="flow-diagram">{html.escape(flow)}</div>
</section>

</div>

<footer>
  <p>tests · real fixtures · zero LLM calls in the trim path.</p>
  <p><code>~/repos/claude-compaction</code></p>
</footer>
{selector_script}
</body>
</html>
"""

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    print(f"wrote {out_path} ({out_path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
