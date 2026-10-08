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
def codex_evidence(tmp_path):
    root = tmp_path / "run"
    harness = root / "harness"
    source = harness / "src/codex-rs/cli/src/main.rs"
    source.parent.mkdir(parents=True)
    source.write_text("fn main() {}\n")
    snapshot.init(harness)
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
