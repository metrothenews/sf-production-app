from __future__ import annotations

import pytest

from so_sync.repositories import normalize_enabled_value, select_job_from_sync_jobs


def _job(job_id: str, enabled: str) -> dict[str, str]:
    return {"job_id": job_id, "enabled": enabled}


def test_job_id_provided_and_exists_selects_job() -> None:
    jobs = [_job("default", "TRUE"), _job("other", "FALSE")]
    selected, source = select_job_from_sync_jobs(jobs, job_id="default")
    assert selected["job_id"] == "default"
    assert source == "env"


def test_job_id_provided_and_missing_raises_known_ids() -> None:
    jobs = [_job("default", "TRUE"), _job("other", "FALSE")]
    with pytest.raises(ValueError) as exc:
        select_job_from_sync_jobs(jobs, job_id="missing")
    msg = str(exc.value)
    assert "JOB_ID 'missing' not found in Sync_Jobs." in msg
    assert "default" in msg


def test_job_id_unset_with_exactly_one_enabled_selects_it() -> None:
    jobs = [_job("default", "TRUE"), _job("other", "FALSE")]
    selected, source = select_job_from_sync_jobs(jobs, job_id=None)
    assert selected["job_id"] == "default"
    assert source == "auto-selection"


def test_job_id_unset_with_zero_enabled_raises_exact_message() -> None:
    jobs = [_job("default", "FALSE"), _job("other", "no"), _job("x", "")]
    with pytest.raises(ValueError, match=r"^No enabled jobs in Sync_Jobs \(set enabled=TRUE on exactly one row\)\.$"):
        select_job_from_sync_jobs(jobs, job_id=None)


def test_job_id_unset_with_multiple_enabled_raises_exact_message() -> None:
    jobs = [_job("a", "TRUE"), _job("b", "1"), _job("c", "FALSE")]
    with pytest.raises(ValueError, match=r"^Multiple enabled jobs enabled; disable extras or set JOB_ID\.$"):
        select_job_from_sync_jobs(jobs, job_id=None)


def test_enabled_normalization_matrix() -> None:
    truthy = ["TRUE", "true", "1", "yes", "y"]
    falsey = ["FALSE", "false", "0", "no", "n", "", " ", None]
    for value in truthy:
        assert normalize_enabled_value(value) is True
    for value in falsey:
        assert normalize_enabled_value(value) is False
