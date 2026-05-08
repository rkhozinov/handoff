"""Semantic dedup for paraphrased adjacent turns.

Drops turns whose embedding is `>= threshold` cosine to one of the prior
`window` same-role turns. Targets retry chains where the same Bash / Read
runs back-to-back with a tweaked flag — exact-match `_collapse_repeats`
misses these but Model2Vec catches them.

Lazy-imports `model2vec` + `numpy`. If either is missing, `semantic_dedup`
returns the input unchanged so the core trimmer remains dep-free.
"""
from __future__ import annotations

_MODEL = None
_LOAD_FAILED = False
_MIN_LEN = 30  # turns shorter than this skip dedup (too noisy for static embeddings)


def _load_model():
    global _MODEL, _LOAD_FAILED
    if _MODEL is not None or _LOAD_FAILED:
        return _MODEL
    try:
        from model2vec import StaticModel  # type: ignore

        _MODEL = StaticModel.from_pretrained("minishlab/potion-base-8M")
    except Exception:
        _LOAD_FAILED = True
        return None
    return _MODEL


def semantic_dedup(
    convo: list[tuple[str, str]],
    threshold: float = 0.95,
    window: int = 10,
) -> tuple[list[tuple[str, str]], int]:
    """Return `(deduped_convo, dropped_count)`.

    Drops an *assistant* turn when its embedding cosine-similarity to one
    of the prior `window` kept assistant turns is >= `threshold`. User
    turns are **never** dropped — they're the signal we're trying to
    preserve, and even apparent repeats (`do it again`, paraphrased
    asks) are intentional. Turns under `_MIN_LEN` characters are passed
    through untouched (static embeddings are unreliable on short strings —
    `kubectl get pods -n prod` vs `-n stage` is 0.93).

    No-op (returns input, 0) when `model2vec` / `numpy` not installed.
    """
    if len(convo) < 2:
        return convo, 0
    try:
        import numpy as np  # type: ignore
    except ImportError:
        return convo, 0
    model = _load_model()
    if model is None:
        return convo, 0

    texts = [t for _, t in convo]
    embs = model.encode(texts)
    if not isinstance(embs, np.ndarray):
        embs = np.asarray(embs)
    norms = np.linalg.norm(embs, axis=1) + 1e-9

    out: list[tuple[str, str]] = []
    kept_idx: list[int] = []
    kept_roles: list[str] = []
    dropped = 0

    for i, (role, text) in enumerate(convo):
        # Never dedup user turns — they're signal we must preserve.
        if role != "assistant" or len(text) < _MIN_LEN:
            out.append((role, text))
            kept_idx.append(i)
            kept_roles.append(role)
            continue
        # consider only the last `window` kept assistant turns
        same_role_recent = [
            kept_idx[j]
            for j in range(len(kept_idx) - 1, -1, -1)
            if kept_roles[j] == "assistant"
        ][:window]
        is_dup = False
        if same_role_recent:
            cur = embs[i]
            prevs = embs[same_role_recent]
            sims = (prevs @ cur) / (norms[same_role_recent] * norms[i])
            if float(sims.max()) >= threshold:
                is_dup = True
        if is_dup:
            dropped += 1
            continue
        out.append((role, text))
        kept_idx.append(i)
        kept_roles.append(role)

    return out, dropped
