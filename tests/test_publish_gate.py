"""A green historical run or a skipped Codex job must never certify a release."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "verify_release_gate", Path(__file__).resolve().parents[1] / "scripts/verify_release_gate.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


@pytest.mark.parametrize("changed", [
    {"head_sha": "b" * 40}, {"head_branch": "v0.3.0"}, {"event": "workflow_dispatch"},
    {"status": "in_progress"}, {"conclusion": "failure"},
])
def test_release_gate_requires_exact_tag_commit_and_success(changed):
    run = {"head_sha": "a" * 40, "head_branch": "v0.4.0", "event": "push",
           "status": "completed", "conclusion": "success"}
    assert _module.matching_runs([run], tag="v0.4.0", sha="a" * 40) == [run]
    assert not _module.matching_runs([{**run, **changed}], tag="v0.4.0", sha="a" * 40)


@pytest.mark.parametrize("conclusion", ["skipped", "failure", "cancelled", None])
def test_release_gate_requires_a_successful_codex_job(conclusion):
    jobs = [{"name": name, "conclusion": "success"} for name in _module.REQUIRED_JOBS]
    _module.verify_jobs(jobs)
    for job in jobs:
        if job["name"] == "codex":
            job["conclusion"] = conclusion
    with pytest.raises(ValueError, match="codex"):
        _module.verify_jobs(jobs)
