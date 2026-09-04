import json
import re
from pathlib import Path

from proteus.adapters.codex import BOOT_TIMEOUT_S, IMAGE, CodexHarness

REPO_ROOT = Path(__file__).resolve().parents[1]


def _repo_file(*parts: str) -> str:
    return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _joined(text: str) -> str:
    """Resolve shell/Dockerfile line continuations, then collapse whitespace to one space."""
    text = re.sub(r"\\\r?\n\s*", " ", text)
    return re.sub(r"\s+", " ", text)


def test_surface_for_path():
    h = object.__new__(CodexHarness)
    assert h._surface_for_path('/workspace/candidate/AGENTS.md') == 'instructions'
    assert h._surface_for_path('/workspace/candidate/.agents/skills/foo/SKILL.md') == 'skills'
    assert h._surface_for_path('/workspace/candidate/src/codex-rs/core/src/lib.rs') == 'loop'
    assert h._surface_for_path('/workspace/task/foo.py') is None


def test_jsonl_trace_maps_codex_exec_events():
    h = object.__new__(CodexHarness)
    lines = [
        {"type": "item.completed", "item": {"id": "1", "type": "command_execution",
          "command": "cargo test", "aggregated_output": "ok", "exit_code": 0,
          "status": "completed"}},
        {"type": "item.completed", "item": {"id": "2", "type": "file_change",
          "changes": [{"path": "/workspace/candidate/src/codex-rs/core/src/lib.rs",
                       "kind": "update"}], "status": "completed"}},
        {"type": "item.completed", "item": {"id": "3", "type": "web_search",
          "query": "codex docs", "action": {}}},
        {"type": "item.completed", "item": {"id": "4", "type": "agent_message",
          "text": "done"}},
    ]
    trace = h._jsonl_trace("\n".join(json.dumps(x) for x in lines), "act")
    assert [e.tool for e in trace] == ["command", "file_change", "web_search", None]
    assert trace[1].surface == "loop"
    assert trace[-1].text == "done"


def test_read_trace_offsets_phases(tmp_path: Path):
    h = object.__new__(CodexHarness)
    sessions = tmp_path / '.codex-state' / 'sessions'
    sessions.mkdir(parents=True)
    (tmp_path / 'traces').mkdir()
    one = json.dumps({"type": "item.completed", "item": {
        "id": "1", "type": "command_execution", "command": "pwd",
        "aggregated_output": "", "exit_code": 0, "status": "completed"}})
    (sessions / 'ep001-observe.jsonl').write_text(one)
    (sessions / 'ep001-act.jsonl').write_text(one)
    (tmp_path / 'traces' / 'ep001.json').write_text(json.dumps({
        'observe': 'ep001-observe.jsonl', 'act': 'ep001-act.jsonl'}))
    trace = h.read_trace(tmp_path, 1)
    assert len(trace) == 2
    assert trace[0].turn == 1 and trace[1].turn == 2
    assert trace[0].phase == 'observe' and trace[1].phase == 'act'


# ------------------------------------------------------------------ boundary gate wiring
# These are structural tests: the candidate gate in the image (boot.sh + Dockerfile) is
# what makes staged activation safe for real Rust edits, so the wiring below is load-bearing
# and must not silently regress (e.g. back to a release-only gate or an mtime-trusting
# rsync that lets Cargo skip genuinely changed files).


def test_boot_gate_compiles_tests_before_release_build():
    boot = _joined(_repo_file("environments", "codex-src", "boot.sh"))
    gate_cmd = "cargo test --locked -p codex-tui -p codex-core -p codex-cli --lib --no-run"
    assert gate_cmd in boot
    assert "--lib --no-run" in boot
    assert "last-test-build.log" in boot
    assert "cargo build --locked -p codex-cli -p codex-code-mode-host --release" in boot
    # the test gate must run before the release build
    assert boot.index(gate_cmd) < boot.index("cargo build --locked -p codex-cli")
    # distinct exit codes so adapter diagnostics distinguish the two failures
    assert "exit 98" in boot  # tests do not compile
    assert "exit 97" in boot  # release build fails


def test_boot_gate_is_offline_single_job_and_mtime_safe():
    boot = _joined(_repo_file("environments", "codex-src", "boot.sh"))
    assert "CARGO_NET_OFFLINE=true" in boot
    assert "CARGO_BUILD_JOBS" in boot
    assert "CARGO_HOME=/usr/local/cargo" in boot
    # the changed candidate is overlaid onto the image-baked /opt/src by content
    # (--checksum) with no timestamp trust, keeping Cargo fingerprints valid
    assert "rsync -rlp --checksum --delete --exclude .git --exclude target" in boot
    assert "/opt/src/" in boot
    assert "rsync -a" not in boot
    # only the validated binary pair is published to run-private state
    assert "BIN_DIR" in boot
    assert "codex-code-mode-host" in boot


def test_boot_gate_runs_as_root_but_phases_do_not_install():
    boot = _joined(_repo_file("environments", "codex-src", "boot.sh"))
    assert "[ \"$(id -u)\" = 0 ]" in boot or 'id -u' in boot
    assert "exit 95" in boot  # non-root container must not compile/install
    codex = _joined(_repo_file("proteus", "adapters", "codex.py"))
    assert "_boundary_sandbox" in codex
    assert "self._boundary_sandbox().run(" in codex  # validation runs as container root
    assert 'replace(self.sandbox.config, user="")' in codex


def test_dockerfile_prewarms_test_profile_for_gate():
    dockerfile = _repo_file("environments", "codex-src", "Dockerfile")
    cmd = "cargo test --locked -p codex-tui -p codex-core -p codex-cli --lib --no-run"
    assert cmd in dockerfile
    assert dockerfile.index(cmd) < dockerfile.index("codex-source.tar")
    assert "codex-code-mode-host" in dockerfile


def test_boot_timeout_and_image_tag():
    assert BOOT_TIMEOUT_S == 3600
    # adapter default image tag must match the documented build tag
    assert IMAGE == "proteus-env-codex-src:test-compile"
    readme = _repo_file("environments", "codex-src", "README.md")
    assert "-t proteus-env-codex-src:test-compile" in readme
