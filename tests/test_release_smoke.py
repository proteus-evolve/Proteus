"""The Codex release gate must prove activation, not merely a later successful build."""
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
