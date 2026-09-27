import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_diagnostics_print_seed_error_and_redact_provider_credentials(tmp_path):
    sys.path.insert(0, str(ROOT))
    from scripts.canary_diagnostics import render

    root = tmp_path / "canary"
    root.mkdir()
    (root / "seeds.jsonl").write_text(json.dumps({
        "arm": "neutral", "seed": 0, "episodes_complete": 0,
        "error": "phase observe: exit 1: provider failed for sk-live-token-123456",
    }) + "\n")
    report = render(root, upstream_sha="a" * 40, bake_outcome="success",
                    live_outcome="failure", secret="sk-live-token-123456")

    assert "phase observe: exit 1: provider failed" in report
    assert "upstream_sha: " + "a" * 40 in report
    assert "[REDACTED]" in report
    assert "sk-live-token-123456" not in report
    assert "trace" not in report


def test_diagnostics_explains_missing_sweep_without_failing(tmp_path):
    sys.path.insert(0, str(ROOT))
    from scripts.canary_diagnostics import render

    report = render(tmp_path, source_outcome="failure", bake_outcome="skipped")
    assert "upstream_sha: unavailable" in report
    assert "source_outcome: failure" in report
    assert "sweep did not write seeds.jsonl" in report


def test_diagnostics_cli_writes_artifact_file(tmp_path):
    sweep = tmp_path / "sweep"
    sweep.mkdir()
    (sweep / "seeds.jsonl").write_text(json.dumps({
        "arm": "neutral", "seed": 0, "episodes_complete": 0,
        "error": "provider unavailable",
    }) + "\n")
    output = tmp_path / "artifacts" / "diagnostics.txt"
    env = dict(os.environ, UPSTREAM_SHA="abc123", BAKE_OUTCOME="success",
               LIVE_OUTCOME="failure")
    subprocess.run([sys.executable, str(ROOT / "scripts/canary_diagnostics.py"),
                    str(sweep), "--output", str(output)], check=True, env=env)
    assert "upstream_sha: abc123" in output.read_text()
    assert "provider unavailable" in output.read_text()
