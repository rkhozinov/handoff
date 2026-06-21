"""Pluggable token counter.

Replaces the legacy `bytes // 4` heuristic that was scattered across the CLI,
the bench script, and the HTML report.

Modes
-----
- ``chars4`` — ``len(text.encode("utf-8")) // 4``. Always available, no
  third-party deps. Fast and dependency-free; what the project shipped with.
- ``hf``     — Offline tokenizer via Hugging Face ``transformers`` using the
  ``Xenova/claude-tokenizer`` model. Lazy-imported on first call so the module
  stays optional. The loaded tokenizer is cached in a module-level dict.
- ``api``    — Anthropic SDK ``client.messages.count_tokens(...)``. Lazy-imported.
  Requires ``ANTHROPIC_API_KEY`` in the environment; raises ``RuntimeError``
  with a clear message otherwise. Will hit the network — never selected by
  ``auto`` to avoid surprise calls.
- ``auto``   — Try ``hf`` first, fall back to ``chars4`` on ``ImportError``.
  This is the default everywhere a token count is rendered.

Usage
-----
    from handoff.tokenizer import count_tokens
    n = count_tokens("hello world")              # auto mode
    n = count_tokens("hi", mode="chars4")        # explicit
"""
from __future__ import annotations

import os
from typing import Any

# Cache for loaded HF tokenizers (one entry per model name) and Anthropic
# clients (one per API key). Module-level so a long-running process pays the
# load cost once.
_HF_TOKENIZERS: dict[str, Any] = {}
_ANTHROPIC_CLIENTS: dict[str, Any] = {}

# Model identifiers — kept here so callers don't have to remember them.
_HF_MODEL = "Xenova/claude-tokenizer"
_ANTHROPIC_MODEL = "claude-opus-4-7"

VALID_MODES = ("auto", "hf", "api", "chars4")


def _count_chars4(text: str) -> int:
    """Cheap heuristic: UTF-8 byte length divided by 4."""
    return len(text.encode("utf-8")) // 4


def _make_hf_counter():
    """Build a ``text -> token_count`` callable for Xenova/claude-tokenizer.

    Prefers ``AutoTokenizer.from_pretrained``. That path breaks under
    ``HF_HUB_OFFLINE=1`` because this is a tokenizer-only repo with no
    ``config.json`` — offline, the harmless 404 for config.json becomes a fatal
    ``LocalEntryNotFoundError``. So we fall back to loading the cached
    ``tokenizer.json`` directly via the ``tokenizers`` lib, which needs no
    config and works entirely from cache. Either source raises if nothing is
    available, and ``auto`` mode falls back to chars4.
    """
    from transformers import AutoTokenizer  # lazy: keeps this an optional dep

    try:
        tok = AutoTokenizer.from_pretrained(_HF_MODEL)
        return lambda text: len(tok.encode(text, add_special_tokens=False))
    except Exception:
        # Offline / no config.json: load the cached fast tokenizer file directly.
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        path = hf_hub_download(_HF_MODEL, "tokenizer.json", local_files_only=True)
        tk = Tokenizer.from_file(path)
        return lambda text: len(tk.encode(text, add_special_tokens=False).ids)


def _count_hf(text: str) -> int:
    """Tokenize with Xenova/claude-tokenizer. Caches the counter (built once
    per process). Raises if neither the online model nor the cached
    tokenizer.json is available — ``auto`` mode catches that and uses chars4."""
    counter = _HF_TOKENIZERS.get(_HF_MODEL)
    if counter is None:
        counter = _make_hf_counter()
        _HF_TOKENIZERS[_HF_MODEL] = counter
    return counter(text)


def _count_api(text: str) -> int:
    """Anthropic SDK ``messages.count_tokens`` round-trip.

    Hits the network. Requires ``ANTHROPIC_API_KEY``. ``RuntimeError`` is raised
    if the key is missing (so we never make a request that's guaranteed to
    401) or if the SDK isn't installed.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "count_tokens(mode='api') requires ANTHROPIC_API_KEY in the environment"
        )

    try:
        import anthropic  # lazy
    except ImportError as e:
        raise RuntimeError(
            "count_tokens(mode='api') requires the `anthropic` package "
            "(`pip install handoff[api]`)."
        ) from e

    client = _ANTHROPIC_CLIENTS.get(api_key)
    if client is None:
        client = anthropic.Anthropic(api_key=api_key)
        _ANTHROPIC_CLIENTS[api_key] = client

    resp = client.messages.count_tokens(
        model=_ANTHROPIC_MODEL,
        messages=[{"role": "user", "content": text}],
    )
    return int(resp.input_tokens)


def count_tokens(text: str, mode: str = "auto") -> int:
    """Return the number of tokens in ``text`` under the chosen ``mode``.

    See module docstring for the full mode reference. Empty input returns 0
    without consulting any backend.
    """
    if mode not in VALID_MODES:
        raise ValueError(
            f"Unknown token-count mode: {mode!r}. Expected one of {VALID_MODES}."
        )
    if not text:
        return 0

    if mode == "chars4":
        return _count_chars4(text)
    if mode == "hf":
        return _count_hf(text)
    if mode == "api":
        return _count_api(text)

    # mode == "auto": prefer the offline HF tokenizer; fall back to chars4 on
    # ANY failure — not just ImportError. transformers can be installed yet
    # still fail at from_pretrained when the model isn't cached and the host is
    # offline (OSError/LocalEntryNotFoundError). The token count is a cosmetic
    # metric, so it must never break the caller. We never auto-select the API
    # mode — that would mean a surprise network call.
    try:
        return _count_hf(text)
    except Exception:
        return _count_chars4(text)
