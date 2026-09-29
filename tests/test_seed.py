from __future__ import annotations

import pytest

import seed
from conftest import PARSEABLE_SEED_EXERCISES


def test_seed_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(seed, "EXERCISES", PARSEABLE_SEED_EXERCISES)
    seed.main()
    first_ids = db.execute("SELECT slug, id FROM exercises ORDER BY slug").fetchall()
    seed.main()
    second_ids = db.execute("SELECT slug, id FROM exercises ORDER BY slug").fetchall()
    assert [tuple(row) for row in first_ids] == [tuple(row) for row in second_ids]
    assert len(first_ids) == len(PARSEABLE_SEED_EXERCISES)
    for table in ("exercise_scores", "loop_regions"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == len(PARSEABLE_SEED_EXERCISES)


@pytest.mark.xfail(
    strict=True,
    reason="Bug: the whole-step-bend-targets exercise fails timeline validation, so `python seed.py` "
    "raises and seeds nothing.",
)
def test_full_seed_succeeds(db):
    seed.main()
    assert db.execute("SELECT COUNT(*) FROM exercises").fetchone()[0] == len(seed.EXERCISES)
