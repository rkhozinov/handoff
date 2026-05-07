"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "fixtures" / "raw"
SCRUBBED = ROOT / "fixtures" / "scrubbed"


def _existing(d: Path) -> dict[str, Path]:
    if not d.is_dir():
        return {}
    return {p.stem: p for p in d.glob("*.jsonl")}


@pytest.fixture(scope="session")
def raw_fixtures() -> dict[str, Path]:
    return _existing(RAW)


@pytest.fixture(scope="session")
def scrubbed_fixtures() -> dict[str, Path]:
    return _existing(SCRUBBED)


@pytest.fixture(scope="session")
def any_fixtures(raw_fixtures, scrubbed_fixtures) -> dict[str, Path]:
    """Prefer raw fixtures locally; fall back to scrubbed in CI."""
    return raw_fixtures or scrubbed_fixtures


def pytest_collection_modifyitems(config, items):
    """Skip stat tests when no fixtures exist."""
    if (RAW.is_dir() and any(RAW.glob("*.jsonl"))) or (
        SCRUBBED.is_dir() and any(SCRUBBED.glob("*.jsonl"))
    ):
        return
    skip = pytest.mark.skip(reason="no fixtures available — run scripts/collect_fixtures.py")
    for item in items:
        if "fixture" in item.keywords or "stats" in item.name:
            item.add_marker(skip)
