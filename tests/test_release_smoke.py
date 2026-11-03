"""Release gates prove recorded activation, not merely a later successful build."""
import importlib.util
import json
from pathlib import Path
import pytest

from proteus.adapters.codex import CodexHarness
from proteus.core import snapshot

_spec = importlib.util.spec_from_file_location(
    "release_smoke_check", Path(__file__).resolve().parents[1] / "scripts/smoke_check.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
codex_runtime_matches_checkpoint = _module.codex_runtime_matches_checkpoint


@pytest.fixture
def codex_evidence(tmp_path, request):
    root = tmp_path / "run"
    harness = root / "harness"
    source = harness / "src/codex-rs/cli/src/main.rs"
    source.parent.mkdir(parents=True)
    source.write_text("fn main() {}\n")
    snapshot.init(harness)
    if getattr(request, "param", "source") == "cache":
        artifact = harness / "src/target/PROTEUS-NOTE"
        artifact.parent.mkdir()
        artifact.write_text("This build artifact does not change the runtime source hash.\n")
    else:
        source.write_text("// PROTEUS-NOTE\nfn main() {}\n")
    snapshot.commit(harness, "episode 1: source edit")
    adapter = object.__new__(CodexHarness)
    sha = adapter._source_hash(harness / "src")
    publication = root / ".codex-builds" / sha
    publication.mkdir(parents=True)
    for name in ("codex", "codex-code-mode-host"):
        (publication / name).write_bytes(b"fixture executable")
        (publication / name).chmod(0o755)
    (publication / "source.sha256").write_text(sha)
    record = root / "traces/ep002-runtime.json"
    record.parent.mkdir()
    record.write_text(json.dumps({"version": 1, "episode": 2,
                                 "active_source_sha256": sha,
                                 "publication": f".codex-builds/{sha}"}))
    # The final candidate can differ from the runtime frozen before episode 2.
    source.write_text("// FINAL CANDIDATE\nfn main() {}\n")
    snapshot.commit(harness, "episode 2: different candidate")
    return adapter, root, record, publication


def test_codex_gate_compares_runtime_to_preceding_checkpoint(codex_evidence):
    adapter, root, _, _ = codex_evidence
    assert codex_runtime_matches_checkpoint(adapter, root, 2)


@pytest.mark.parametrize("codex_evidence", ["cache"], indirect=True)
def test_codex_gate_rejects_cache_only_edit_with_valid_seed_binaries(codex_evidence):
    adapter, root, _, _ = codex_evidence
    assert not codex_runtime_matches_checkpoint(adapter, root, 2)


@pytest.mark.parametrize("corruption", ["hash", "publication", "episode", "missing", "binary"])
def test_codex_gate_rejects_missing_or_mismatched_activation(codex_evidence, corruption):
    adapter, root, record, publication = codex_evidence
    if corruption == "missing":
        record.unlink()
    elif corruption == "binary":
        (publication / "codex-code-mode-host").unlink()
    else:
        data = json.loads(record.read_text())
        key, value = {"hash": ("active_source_sha256", "0" * 64),
                      "publication": ("publication", "../../unrelated"),
                      "episode": ("episode", 1)}[corruption]
        data[key] = value
        record.write_text(json.dumps(data))
    assert not codex_runtime_matches_checkpoint(adapter, root, 2)


@pytest.fixture
def native_inputs(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core.adapter import EpisodeSpec
    from proteus.core.native_inputs import input_directory
    root = tmp_path/'native'
    (root/'harness').mkdir(parents=True)
    adapter = MinimalHarness()
    for episode in (1,2):
        result = adapter.run_episode(EpisodeSpec(root=root,episode=episode,model='mock',
            phase_prompts={'act':'final input'}, phases=('act',)))
        assert result.ok
    return root, input_directory(root)


def test_live_gate_requires_hashed_inputs_for_every_episode(native_inputs):
    root, _ = native_inputs
    assert _module.native_inputs_cover_run(root,'minimal',2)
    assert not _module.native_inputs_cover_run(root,'minimal',3)
    assert not _module.native_inputs_cover_run(root,'codex',2)
    assert not _module.native_inputs_cover_run(root,'aki',2)


def test_harness_gate_separates_evaluator_receipts(native_inputs):
    from types import SimpleNamespace
    from proteus.core.native_inputs import native_input
    root, _ = native_inputs
    with native_input(SimpleNamespace(root=root, episode=0), backend='evaluator', phase=None,
                      scope='native_evaluator', payload={'model_free': True}):
        pass
    assert _module.native_inputs_cover_run(root, 'minimal', 2)


@pytest.mark.parametrize('backend', ['pi', 'dsh'])
@pytest.mark.parametrize('corruption', [None, 'capped', 'cache-only', 'wrong-checkpoint', 'missing', 'unknown', 'failed', 'receipt'])
def test_source_gate_requires_actual_preceding_checkpoint_receipts(tmp_path, backend, corruption):
    from types import SimpleNamespace
    from proteus.core.native_activation import FLAG, source_tree_hash
    from proteus.core.native_inputs import native_input, input_directory
    from proteus.core.serialization import content_hash
    harness = tmp_path/'harness'
    source = harness/'src/packages/main.js'
    source.parent.mkdir(parents=True)
    source.write_text('seed')
    snapshot.init(harness)
    if corruption == 'cache-only':
        target = harness/'src/node_modules/cached'
        target.parent.mkdir()
        target.write_text('ignored')
    else:
        source.write_text('edited')
    snapshot.commit(harness, 'episode 1: edit')
    expected = source_tree_hash(harness/'src')
    source.write_text('final candidate differs from runtime')
    snapshot.commit(harness, 'episode 2: another edit')
    if corruption == 'wrong-checkpoint':
        expected = source_tree_hash(harness/'src')
    # Even a cache for this hash, made after the run, cannot substitute for receipts.
    state = tmp_path/f'.{backend}-state'
    state.mkdir()
    (state/f'dist-{expected}.tar').touch()
    spec = SimpleNamespace(root=tmp_path, episode=2)
    if corruption != 'missing':
        command = [FLAG, expected, '--profile', 'headless', 'input'] if backend == 'dsh' else [FLAG, expected, '-p', 'input']
        with native_input(spec, backend=backend, phase='act', scope='harness_cli', payload=command) as receipt:
            receipt['returncode'] = 23 if corruption == 'failed' else 137 if corruption == 'capped' else 0
            if corruption != 'failed':
                receipt['activation'] = {'version': 1, 'backend': backend, 'mode': 'build',
                    'source_sha256': expected, 'cli_sha256': 'b'*64, 'boot_sha256': 'c'*64}
        outcome = next(input_directory(tmp_path).glob('*.outcome.json'))
        if corruption == 'unknown':
            outcome.unlink()
        elif corruption == 'receipt':
            data = json.loads(outcome.read_text())
            del data['activation']
            data['hash'] = content_hash({k:v for k,v in data.items() if k != 'hash'})
            outcome.write_text(json.dumps(data))
    with native_input(spec, backend='evaluator', phase=None, scope='native_evaluator', payload={}):
        pass
    assert _module.source_runtime_matches_checkpoint(SimpleNamespace(name=backend), tmp_path, 2) == (corruption in (None, 'capped'))


@pytest.mark.parametrize('corruption', ['input','outcome','unknown','opaque','backend','episode'])
def test_live_gate_rejects_missing_corrupt_or_wrong_scope_input_evidence(native_inputs, corruption):
    from proteus.core.serialization import content_hash
    root, directory = native_inputs
    path = next(directory.glob('*.input.json'))
    if corruption in ('outcome','unknown'):
        path = path.with_name(path.name.replace('.input.json','.outcome.json'))
    if corruption=='unknown':
        path.unlink()
    else:
        data = json.loads(path.read_text())
        if corruption=='input':
            data['payload']='changed'
        elif corruption=='outcome':
            data['status']='raised'
        else:
            key,value={'opaque':('scope','opaque_supervisor'),
                       'backend':('backend','codex'),'episode':('episode',999)}[corruption]
            data[key]=value
            data['hash']=content_hash({key:value for key,value in data.items() if key!='hash'})
            outcome_path=path.with_name(path.name.replace('.input.json','.outcome.json'))
            outcome=json.loads(outcome_path.read_text())
            outcome['input_hash']=data['hash']
            outcome['hash']=content_hash({key:value for key,value in outcome.items() if key!='hash'})
            outcome_path.write_text(json.dumps(outcome))
        path.write_text(json.dumps(data))
    assert not _module.native_inputs_cover_run(root,'minimal',2)
