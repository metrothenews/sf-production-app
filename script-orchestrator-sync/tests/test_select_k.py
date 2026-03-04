from __future__ import annotations

from render_agent.select_k import select_k


def test_select_k_shuffle_seeded_is_deterministic() -> None:
    pool = [f"id{i}" for i in range(20)]
    a = select_k(pool, 5, "shuffle_seeded", "seed-1")
    b = select_k(pool, 5, "shuffle_seeded", "seed-1")
    assert a == b


def test_select_k_shuffle_seeded_changes_with_seed() -> None:
    pool = [f"id{i}" for i in range(20)]
    a = select_k(pool, 5, "shuffle_seeded", "seed-1")
    b = select_k(pool, 5, "shuffle_seeded", "seed-2")
    assert a != b
