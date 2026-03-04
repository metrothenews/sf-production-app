from __future__ import annotations

import hashlib
import random
from typing import Any


def _seed_to_int(seed: Any) -> int:
    raw = str(seed)
    if raw.strip() == "":
        return random.SystemRandom().randint(0, 2**31 - 1)
    try:
        return int(raw)
    except Exception:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return int(digest[:16], 16)


def select_k(pool: list[str], k: int, mode: str, seed: str | int | None) -> list[str]:
    ordered = [str(x) for x in pool if str(x)]
    if k <= 0 or not ordered:
        return []
    if len(ordered) <= k:
        return ordered
    mode_norm = str(mode or "shuffle_seeded").strip().lower()
    if mode_norm == "first":
        return ordered[:k]
    if mode_norm == "random":
        rng = random.Random(_seed_to_int(seed)) if str(seed or "").strip() else random.SystemRandom()
        return rng.sample(ordered, k)
    stable = sorted(ordered)
    rng = random.Random(_seed_to_int(seed)) if str(seed or "").strip() else random.SystemRandom()
    rng.shuffle(stable)
    return stable[:k]
