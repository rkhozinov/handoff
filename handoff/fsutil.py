"""Filesystem helpers shared by every module that writes an authoritative
file (briefs, task files, bundles, manifests)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace. A crash mid-write must not
    leave a truncated file: a half-written task `<id>.json` makes CC choke,
    and a half-written brief resets `created`/`last_resumed` on the next
    /hand:off."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".handoff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
