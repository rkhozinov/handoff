"""Compute the cwd-keyed slug used for `latest-<slug>.md` brief symlinks.

Centralized so the CLI (Python) and the `/handon` command (shell, via
`python -m compaction.slug <cwd>`) agree byte-for-byte.

Slug shape:
  - cwd only:        `<cwd_with_slashes_to_dashes>`
  - cwd + branch:    `<cwd_slug>--<branch_slug>`

Branch is appended whenever a non-detached HEAD is detected so that the
same worktree directory does not collide across unrelated feature branches.
"""
from __future__ import annotations

import re
import subprocess
import sys

_BRANCH_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def cwd_slug(cwd: str, branch: str | None = None) -> str:
    """Encode cwd (and optional branch) into a stable filename slug.

    Pure / deterministic. No I/O. Mirrors how Claude Code names project
    dirs (slashes → dashes); branch suffix uses `--` as separator so the
    cwd portion stays unambiguous when split.
    """
    base = cwd.replace("/", "-")
    if not branch:
        return base
    b = branch.replace("/", "-")
    b = _BRANCH_SAFE.sub("", b)
    if not b:
        return base
    return f"{base}--{b}"


def detect_branch(cwd: str) -> str | None:
    """Return the current git branch for `cwd`, or None.

    Returns None when:
      - `cwd` is not inside a git repo
      - `git` is missing
      - HEAD is detached (`rev-parse --abbrev-ref HEAD` prints `HEAD`)
      - the call times out or otherwise errors
    """
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out or out == "HEAD":
        return None
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: python -m compaction.slug <cwd>\n")
        return 2
    cwd = args[0]
    print(cwd_slug(cwd, detect_branch(cwd)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
