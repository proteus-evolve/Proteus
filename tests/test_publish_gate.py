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
    jobs = [{"name": name, "status": "completed", "conclusion": "success"} for name in _module.REQUIRED_JOBS]
    _module.verify_jobs(jobs)
    for job in jobs:
        if job["name"] == "codex":
            job["conclusion"] = conclusion
    with pytest.raises(ValueError, match="codex"):
        _module.verify_jobs(jobs)


class GateEvidence:
    def __init__(self):
        self.runs = {}
        self.jobs = {}
        self.calls = []
        for number, (workflow, required) in enumerate(_module.REQUIRED_WORKFLOWS.items(), 100):
            self.runs[workflow] = [{"id": number, "head_sha": "a"*40, "head_branch": "v1.0.0",
                "event": "push", "status": "completed", "conclusion": "success",
                "html_url": f"https://example.invalid/runs/{number}"}]
            self.jobs[number] = [{"name": name, "status": "completed", "conclusion": "success",
                "head_sha": "a"*40, "run_id": number} for name in required]

    def fetch(self, path):
        self.calls.append(path)
        parts = path.split('?')[0].split('/')
        if parts[:2] == ['actions', 'workflows']:
            assert 'branch=v1.0.0' in path and 'event=push' in path
            return {'workflow_runs': self.runs[parts[2]]}
        assert 'filter=latest' in path
        return {'jobs': self.jobs[int(parts[2])]}

    def verify(self):
        return _module.verify_workflows(self.fetch, tag='v1.0.0', sha='a'*40)


def test_missing_native_source_workflow_blocks_otherwise_successful_release():
    evidence = GateEvidence()
    evidence.runs['native-source-integration.yml'] = []
    with pytest.raises(ValueError, match='native-source-integration.yml'):
        evidence.verify()


@pytest.mark.parametrize('backend', ['pi', 'dsh'])
def test_each_native_source_backend_is_required_for_release(backend):
    evidence = GateEvidence()
    workflow = 'native-source-integration.yml'
    evidence.runs[workflow] = [{**evidence.runs['ci.yml'][0], 'id': 800}]
    evidence.jobs[800] = [
        {'name': f'Pinned native source ({name})', 'status': 'completed',
         'conclusion': 'skipped' if name == backend else 'success',
         'head_sha': 'a'*40, 'run_id': 800} for name in ('pi', 'dsh')]
    with pytest.raises(ValueError, match=workflow):
        evidence.verify()


def test_release_requires_all_four_workflows_with_all_required_jobs():
    evidence = GateEvidence()
    assert set(evidence.verify()) == {'release-smoke.yml', 'ci.yml', 'harbor.yml',
                                      'native-source-integration.yml'}
    assert len(evidence.calls) == 8


@pytest.mark.parametrize('workflow', ['release-smoke.yml', 'ci.yml', 'harbor.yml',
                                      'native-source-integration.yml'])
@pytest.mark.parametrize('changed', [
    {'head_sha': 'b'*40}, {'head_branch': 'dev/1.0.0'}, {'event': 'pull_request'},
    {'status': 'in_progress'}, {'conclusion': 'cancelled'},
])
def test_other_workflow_success_cannot_replace_missing_exact_release_run(workflow, changed):
    evidence = GateEvidence()
    evidence.runs[workflow][0].update(changed)
    with pytest.raises(ValueError, match=workflow):
        evidence.verify()


@pytest.mark.parametrize('workflow,job_name', [
    ('release-smoke.yml', 'codex'), ('ci.yml', 'Installed wheel and sdist'),
    ('ci.yml', 'test (3.14)'), ('ci.yml', 'Pinned AIME data and DSPy contract'),
    ('harbor.yml', 'Pinned Harbor and real Docker trials'),
    ('native-source-integration.yml', 'Pinned native source (pi)'),
    ('native-source-integration.yml', 'Pinned native source (dsh)'),
])
@pytest.mark.parametrize('changed', [
    {'conclusion': 'skipped'}, {'conclusion': 'failure'}, {'status': 'in_progress'},
    {'head_sha': 'b'*40}, {'run_id': 12345},
])
def test_required_job_must_finish_successfully_on_its_exact_run(workflow, job_name, changed):
    evidence = GateEvidence()
    run = evidence.runs[workflow][0]['id']
    next(job for job in evidence.jobs[run] if job['name']==job_name).update(changed)
    with pytest.raises(ValueError, match=workflow):
        evidence.verify()


@pytest.mark.parametrize('workflow', ['ci.yml', 'native-source-integration.yml'])
def test_partial_runs_cannot_pool_jobs_to_certify_a_workflow(workflow):
    evidence = GateEvidence()
    original = evidence.runs[workflow][0]
    evidence.runs[workflow].append({**original, 'id': 500})
    split = len(evidence.jobs[original['id']]) // 2
    evidence.jobs[500] = [dict(job, run_id=500) for job in evidence.jobs[original['id']][split:]]
    evidence.jobs[original['id']] = evidence.jobs[original['id']][:split]
    with pytest.raises(ValueError, match=workflow):
        evidence.verify()


@pytest.mark.parametrize('workflow', ['ci.yml', 'native-source-integration.yml'])
def test_a_complete_later_attempt_can_pass_without_pooling_previous_jobs(workflow):
    evidence = GateEvidence()
    original = evidence.runs[workflow][0]
    evidence.runs[workflow].append({**original, 'id': 500})
    evidence.jobs[500] = [dict(job, run_id=500) for job in evidence.jobs[original['id']]]
    evidence.jobs[original['id']][0]['conclusion'] = 'skipped'
    assert len(evidence.verify()) == 4


@pytest.mark.parametrize('workflow', ['harbor.yml', 'native-source-integration.yml'])
def test_lookup_errors_do_not_return_partial_gate_approval(workflow):
    evidence = GateEvidence()
    def unavailable(path):
        if workflow in path:
            raise OSError('unavailable')
        return evidence.fetch(path)
    with pytest.raises(OSError):
        _module.verify_workflows(unavailable, tag='v1.0.0', sha='a'*40)
