#!/usr/bin/env python3
"""Scrub raw fixtures into committable scrubbed fixtures.

Redactions (deterministic, reversible NOT a goal):
  - Absolute home paths   → /Users/USER
  - Email addresses       → user@example.com
  - URLs with auth        → https://USER:TOKEN@host
  - GitHub tokens (ghp_, ghs_, gho_, github_pat_)  → ghp_REDACTED
  - AWS access keys (AKIA / ASIA prefix)           → AKIAREDACTED
  - Generic high-entropy hex tokens >= 32 chars    → REDACTED_HEX
  - JWT-shaped strings (3 b64 segments . separated)→ REDACTED_JWT
  - Bearer headers / api-key headers               → REDACTED_HEADER

Preserves trimmer-relevant signal (relative paths, code blocks, decisions).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HOME_PATH = re.compile(r"/Users/[A-Za-z0-9._-]+")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
URL_AUTH = re.compile(r"(https?://)([^:@/\s]+):([^@/\s]+)@")
GH_TOKEN = re.compile(r"\b(ghp|ghs|gho|ghu|ghr)_[A-Za-z0-9]{30,}\b")
GH_PAT = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")
AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")
LONG_HEX = re.compile(r"\b[a-f0-9]{32,}\b")
BEARER = re.compile(r"(?i)\b(bearer|api[-_]?key|x-api-key|authorization)\s*[:=]\s*['\"]?[A-Za-z0-9._\-+/=]{16,}['\"]?")


def scrub_text(text: str) -> str:
    text = HOME_PATH.sub("/Users/USER", text)
    text = URL_AUTH.sub(r"\1USER:TOKEN@", text)
    text = GH_PAT.sub("github_pat_REDACTED", text)
    text = GH_TOKEN.sub(r"\1_REDACTED", text)
    text = AWS_KEY.sub("AKIAREDACTED00000000", text)
    text = JWT.sub("REDACTED_JWT", text)
    text = BEARER.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + ": REDACTED_HEADER", text)
    text = EMAIL.sub("user@example.com", text)
    text = LONG_HEX.sub("REDACTED_HEX", text)
    return text


def scrub_value(v):
    if isinstance(v, str):
        return scrub_text(v)
    if isinstance(v, list):
        return [scrub_value(x) for x in v]
    if isinstance(v, dict):
        return {k: scrub_value(val) for k, val in v.items()}
    return v


def scrub_jsonl(src: Path, dst: Path, max_lines: int | None = None) -> tuple[int, int]:
    """Returns (lines_in, lines_out). When max_lines is set, only the first
    N parseable lines are written (keeps committed fixtures small while
    preserving real-world structure)."""
    in_n, out_n = 0, 0
    with src.open("r", encoding="utf-8", errors="replace") as fi, dst.open("w", encoding="utf-8") as fo:
        for line in fi:
            in_n += 1
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj = scrub_value(obj)
            fo.write(json.dumps(obj, ensure_ascii=False) + "\n")
            out_n += 1
            if max_lines is not None and out_n >= max_lines:
                break
    return in_n, out_n


DEFAULT_MAX_LINES = {
    "small": None,    # keep full
    "medium": 500,
    "large": 500,
    "huge": 500,
    "xhuge": 500,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    base = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
    ap.add_argument("--src", default=str(base / "raw"))
    ap.add_argument("--dst", default=str(base / "scrubbed"))
    ap.add_argument(
        "--full",
        action="store_true",
        help="Skip per-fixture line caps (commits full-size scrubbed copies — bloats repo)",
    )
    args = ap.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)
    dst.mkdir(parents=True, exist_ok=True)

    if not src.is_dir():
        print(f"raw fixtures dir missing: {src}")
        return 1

    files = sorted(src.glob("*.jsonl"))
    if not files:
        print(f"no .jsonl in {src} — run collect_fixtures.py first")
        return 1

    for sf in files:
        df = dst / sf.name
        max_lines = None if args.full else DEFAULT_MAX_LINES.get(sf.stem)
        in_n, out_n = scrub_jsonl(sf, df, max_lines=max_lines)
        size = df.stat().st_size // 1024
        cap = f"(cap={max_lines})" if max_lines else "(full)"
        print(f"  {sf.name:20s}  {in_n:>6,d} → {out_n:>6,d} lines  {size:>6,d} KB  {cap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
