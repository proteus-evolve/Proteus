import json
import re
import subprocess
from pathlib import Path

import pytest

from proteus.adapters.codex import BOOT_TIMEOUT_S, IMAGE, CodexHarness

REPO_ROOT = Path(__file__).resolve().parents[1]


def _repo_file(*parts: str) -> str:
    return (REPO_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _joined(text: str) -> str:
    """Resolve shell/Dockerfile line continuations, then collapse whitespace to one space."""
    text = re.sub(r"\\\r?\n\s*", " ", text)
    return re.sub(r"\s+", " ", text)


def _fake_publication(adapter, run_root: Path, harness: Path) -> tuple[str, Path]:
    source_hash, publication = adapter._publication_for(run_root, harness)
    publication.mkdir(parents=True, exist_ok=True)
    (publication / "codex").write_bytes(b"codex")
    (publication / "codex-code-mode-host").write_bytes(b"host")
    (publication / "codex").chmod(0o755)
    (publication / "codex-code-mode-host").chmod(0o755)
    (publication / "source.sha256").write_text(source_hash + "\n", encoding="utf-8")
    return source_hash, publication


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


def _jsonl(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _patch_event(paths):
    return {"type": "item.completed", "item": {
        "id": "patch-1", "type": "file_change", "status": "completed",
        "changes": [{"path": path, "kind": "update"} for path in paths],
    }}


def _seeded_codex(tmp_path, sandbox):
    """Exercise the real episode/trace path with only a fixture binary publication."""
    class SeededCodex(CodexHarness):
        def seed(self, harness_root, rng_seed=0):
            source = Path(harness_root) / "src" / "codex-rs"
            source.mkdir(parents=True, exist_ok=True)
            (source / "version.txt").write_text("fixture source")
            (Path(harness_root) / "AGENTS.md").write_text("# Fixture\n")
            _fake_publication(self, Path(harness_root).parent, Path(harness_root))

    auth = tmp_path / "fixture-auth.json"
    auth.write_text("{}")
    return SeededCodex(auth_file=auth, sandbox=sandbox)


def test_multifile_patch_spends_one_call_in_budget_measurement_and_progress(tmp_path):
    from proteus.core import EvaluatorSpec, GoalConfig, NEUTRAL
    from proteus.core.episode import RunConfig, run
    from proteus.core.evaluators import tool_calls
    from proteus.measure.stream import tool_stream

    paths = [f"/workspace/candidate/notes/{i}.md" for i in range(12)]
    stops = []

    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            if not stops:
                patch = _patch_event(paths)
                output = _jsonl({**patch, "type": "item.started"},
                                {**patch, "type": "item.updated"}, patch)
            else:
                output = _jsonl({"type": "item.completed", "item": {
                    "id": "command-1", "type": "command_execution", "command": "pwd",
                    "status": "completed", "exit_code": 0,
                }})
            log = Path(root) / ".codex-state" / "sessions" / Path(command[1]).name
            log.write_text(output)
            stopped = stop_check()
            stops.append(stopped)
            return subprocess.CompletedProcess(command, 137 if stopped else 0, output, "")

    adapter = _seeded_codex(tmp_path, Sandbox())
    cfg = RunConfig(
        name="codex-count", adapter=adapter, disposition=NEUTRAL,
        goal=GoalConfig.of(evaluators=(EvaluatorSpec(name="calls", run=tool_calls("calls")),)),
        root=tmp_path / "run", model="mock", episodes=1, max_turns=2,
        progress_path=tmp_path / "progress.jsonl",
    )
    result = run(cfg)
    assert result.episodes_complete == 1 and not result.error
    assert stops == [False, True]  # One patch leaves room for propose's command.
    trace = adapter.read_trace(cfg.root, 1)
    assert tool_stream(trace) == ["file_change", "command"]
    assert trace[0].surface == "notes"
    assert [change["path"] for change in trace[0].params["changes"]] == paths
    assert result.eval_history[0]["results"][0]["score"] == 2
    progress = json.loads(cfg.progress_path.read_text())
    assert progress["turns"] == progress["tool_calls"] == 2
    assert progress["counters"]["phase_observe_turns"] == 1
    assert progress["counters"]["phase_propose_turns"] == 1
    assert progress["counters"]["turn_capped"] is True


def test_patch_retains_mixed_surface_attribution_without_extra_calls():
    adapter = object.__new__(CodexHarness)
    paths = ["/workspace/candidate/AGENTS.md", "/workspace/candidate/src/main.rs"]
    trace = adapter._jsonl_trace(_jsonl(_patch_event(paths)), "act")
    assert len(trace) == 1 and trace[0].tool == "file_change"
    assert trace[0].surface is None
    assert [change["surface"] for change in trace[0].params["changes"]] == [
        "instructions", "loop",
    ]


def test_diagnostics_do_not_spend_calls_or_turn_recovered_errors_into_failures(tmp_path):
    from proteus.core.adapter import EpisodeSpec

    output = _jsonl(
        {"type": "error", "message": "retryable transport error"},
        {"type": "item.completed", "item": {
            "id": "warning-1", "type": "error", "message": "deprecation notice",
        }},
        {"type": "turn.completed", "usage": {}},
    )

    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            return subprocess.CompletedProcess(command, 0, output, "")

    adapter = _seeded_codex(tmp_path, Sandbox())
    root = tmp_path / "run"
    adapter.seed(root / "harness")
    result = adapter.run_episode(EpisodeSpec(
        root=root, episode=1, model="", phase_prompts={}, phases=("observe",),
    ))
    assert result.ok and result.turns == 0
    trace = adapter.read_trace(root, 1)
    assert len(trace) == 2 and all(event.tool is None for event in trace)
    log = root / ".codex-state" / "sessions" / "ep001-observe.jsonl"
    assert adapter._live_calls(log) == 0


@pytest.mark.parametrize(("events", "stderr", "expected"), [
    ([{"type": "turn.failed", "error": {"message": "quota exhausted"}}],
     "unrelated startup warning", "quota exhausted"),
    ([{"type": "error", "message": "connection failed"}], "", "connection failed"),
    ([], "permission denied api_key=fixture_stderr_secret",
     "permission denied api_key=[REDACTED]"),
    ([], "", "Codex exited without an error diagnostic"),
])
def test_failed_episode_retains_jsonl_diagnostic_or_stderr_fallback(
        tmp_path, events, stderr, expected):
    from proteus.core.adapter import EpisodeSpec

    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            # Exercise stdout fallback, including an incomplete final JSONL frame.
            return subprocess.CompletedProcess(command, 1, _jsonl(*events) + '{"type":', stderr)

    adapter = _seeded_codex(tmp_path, Sandbox())
    root = tmp_path / "run"
    adapter.seed(root / "harness")
    result = adapter.run_episode(EpisodeSpec(root=root, episode=1, model="", phase_prompts={}))
    assert not result.ok and result.turns == 0
    assert result.error == f"phase observe: exit 1: {expected}"
    handoff = json.loads((root / ".proteus-state/handoffs/ep001/observe.json").read_text())
    assert handoff["interrupted"] is True


def test_native_failure_is_redacted_and_reaches_every_resumed_phase(tmp_path):
    from proteus.core import GoalConfig, NEUTRAL
    from proteus.core.episode import RunConfig, pending_candidate_path, run

    secret = "fixture_provider_secret"
    message = f"quota exhausted api_key={secret}"
    prompts = []
    notices = []

    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            prompts.append(command[-1])
            if len(prompts) == 1:
                return subprocess.CompletedProcess(command, 1, _jsonl(
                    {"type": "error", "message": "reconnecting"},
                    {"type": "turn.failed", "error": {"message": message}},
                ), "")
            notices.append((Path(root) / ".proteus-state/handoff.md").read_text())
            return subprocess.CompletedProcess(command, 0, "", "")

    adapter = _seeded_codex(tmp_path, Sandbox())
    cfg = RunConfig(name="codex-failure", adapter=adapter, disposition=NEUTRAL,
                    goal=GoalConfig.no_goal(), root=tmp_path / "run", model="mock", episodes=1)
    failed = run(cfg)
    assert failed.episodes_complete == 0 and "quota exhausted" in failed.error
    pending = pending_candidate_path(cfg.root).read_text()
    trace = adapter.read_trace(cfg.root, 1)
    assert "quota exhausted" in pending and "[REDACTED]" in pending
    assert secret not in pending and secret not in failed.error
    assert all(secret not in event.text for event in trace)

    resumed = run(cfg, resume=True)
    assert resumed.episodes_complete == 1 and not resumed.error
    assert len(prompts) == 5 and len(notices) == 4
    assert all("quota exhausted" in text and secret not in text
               for text in prompts[1:] + notices)


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
    assert "/opt/proteus-cargo-build-jobs" in boot
    assert "CARGO_HOME=/usr/local/cargo" in boot
    # the changed candidate is overlaid onto the image-baked /opt/src by content
    # (--checksum) with no timestamp trust, keeping Cargo fingerprints valid
    assert "rsync -rlp --checksum --delete --exclude .git --exclude target" in boot
    assert "/opt/src/" in boot
    assert "rsync -a" not in boot
    # candidate-controlled build code can only publish into a dedicated output mount
    assert "OUTPUT=/output" in boot
    assert '"$OUTPUT/codex"' in boot
    assert '"$STATE/bin"' not in boot
    assert "codex-code-mode-host" in boot
    # Preserve the image's profile-specific V8 build-script fingerprints: test uses
    # the stable aliases, while release uses the checksum-verified source paths.
    assert "RELEASE_V8_ARCHIVE" in boot and "RELEASE_V8_BINDING" in boot
    assert "find /opt/rusty-v8 -maxdepth 1 -type f" in boot
    assert boot.index("RELEASE_V8_ARCHIVE") > boot.index("cargo test --locked")


def test_boot_gate_runs_as_root_but_phases_do_not_install():
    boot = _joined(_repo_file("environments", "codex-src", "boot.sh"))
    assert "[ \"$(id -u)\" != 0 ]" in boot or 'id -u' in boot
    assert "exit 95" in boot  # non-root container must not compile/install
    codex = _joined(_repo_file("proteus", "adapters", "codex.py"))
    assert "_boundary_sandbox" in codex
    assert "self._boundary_sandbox().run(" in codex  # validation runs as container root
    assert 'network="none"' in codex


def test_boundary_sandbox_drops_model_credentials_mounts_and_docker_args(tmp_path):
    from proteus.sandbox import DockerSandbox, SandboxConfig

    supplied = DockerSandbox(SandboxConfig(
        network="host",
        image="custom-codex",
        mem_limit="8g",
        cpus="3",
        extra_mounts=((str(tmp_path), "/secret"),),
        env_passthrough=("OPENAI_API_KEY",),
        env={"LITERAL_SECRET": "secret"},
        entrypoint=("/usr/local/bin/proteus-codex",),
        workdir="/model-workdir",
        user="1234:5678",
        extra_args=("--privileged",),
    ))
    boundary = CodexHarness(sandbox=supplied)._boundary_sandbox()

    assert boundary.config.network == "none"
    assert boundary.config.image == "custom-codex"
    assert boundary.config.mem_limit == "8g" and boundary.config.cpus == "3"
    assert boundary.config.entrypoint == ()
    assert boundary.config.extra_mounts == ()
    assert boundary.config.env_passthrough == ()
    assert boundary.config.env == {}
    assert boundary.config.extra_args == ()
    assert boundary.config.workdir == "" and boundary.config.user == ""


def test_boundary_validation_uses_read_only_source_and_separate_output(tmp_path):
    harness = tmp_path / "harness"
    source = harness / "src" / "codex-rs"
    source.mkdir(parents=True)
    (source / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    calls = []

    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            calls.append((root, command, env, mounts))
            output = Path(next(mount[0] for mount in mounts if mount[1] == "/output"))
            (output / "codex").write_bytes(b"codex")
            (output / "codex-code-mode-host").write_bytes(b"host")
            return subprocess.CompletedProcess(command, 0, "", "")

    adapter = CodexHarness(sandbox=Sandbox())
    assert adapter.check_boot(harness) == ""

    _, command, env, mounts = calls[0]
    assert command[0] == "--proteus-validate"
    assert env == {}
    assert mounts[0] == (str(harness.resolve()), "/workspace", "ro")
    assert mounts[1][1] == "/output" and len(mounts) == 2
    assert all(mount[1] not in {"/state", "/codex-state"} for mount in mounts)
    source_hash, publication = adapter._publication_for(tmp_path, harness)
    assert adapter._publication_is_valid(publication, source_hash)


def test_boundary_validation_timeout_is_a_viability_error(tmp_path):
    harness = tmp_path / "harness"
    source = harness / "src" / "codex-rs"
    source.mkdir(parents=True)
    (source / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")

    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            raise subprocess.TimeoutExpired(command, timeout_s)

    error = CodexHarness(sandbox=Sandbox()).check_boot(harness)

    assert error == f"self-edited Codex source build timed out after {BOOT_TIMEOUT_S}s"


def test_source_hash_tracks_symlink_targets(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    target = source / "one"
    target.write_text("same contents", encoding="utf-8")
    # Real target directories are excluded, but a symlink with that name is a source
    # entry and must match boot.sh/find's -type l behavior.
    link = source / "target"
    try:
        link.symlink_to("one")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    before = CodexHarness._source_hash(source)
    link.unlink()
    link.symlink_to("two")
    assert CodexHarness._source_hash(source) != before


def test_model_phases_mount_only_the_active_publication_read_only(tmp_path):
    run_root = tmp_path / "run"
    harness = run_root / "harness"
    active = run_root / "active"
    for root, value in ((harness, "candidate"), (active, "accepted")):
        source = root / "src" / "codex-rs"
        source.mkdir(parents=True)
        (source / "version.txt").write_text(value, encoding="utf-8")
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    calls = []

    class Sandbox:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            calls.append(mounts)
            return subprocess.CompletedProcess(command, 0, "", "")

    adapter = CodexHarness(auth_file=auth, sandbox=Sandbox())
    _, publication = _fake_publication(adapter, run_root, active)
    from proteus.core.adapter import EpisodeSpec
    result = adapter.run_episode(EpisodeSpec(
        root=run_root, episode=1, model="", phase_prompts={}, active_root=active,
    ))

    assert result.ok and calls
    runtime = json.loads((run_root / "traces/ep001-runtime.json").read_text())
    assert runtime == {"version": 1, "episode": 1,
                       "active_source_sha256": adapter._source_hash(active / "src"),
                       "publication": publication.relative_to(run_root).as_posix()}
    for mounts in calls:
        assert (str(publication), "/opt/proteus-bin", "ro") in mounts
        assert all(mount[1] != "/opt/proteus-bin" or mount[2] == "ro" for mount in mounts)


def test_viable_rejected_candidate_does_not_change_next_active_publication(tmp_path):
    from proteus.core import EpisodeResult, EvaluatorSpec, GoalConfig, NEUTRAL
    from proteus.core.episode import RunConfig, run
    from proteus.core.goal import EvalResult

    class CachedCodex(CodexHarness):
        def __init__(self):
            super().__init__(sandbox=object())
            self.active_hashes = []
            self.candidate_hashes = []

        def seed(self, harness_root, rng_seed=0):
            harness = Path(harness_root)
            (harness / "src" / "codex-rs").mkdir(parents=True)
            (harness / "src" / "codex-rs" / "version.txt").write_text("seed")
            (harness / "AGENTS.md").write_text("# Agent\n")
            for sub in ("notes", "tools", ".agents/skills"):
                (harness / sub).mkdir(parents=True, exist_ok=True)
            _fake_publication(self, harness.parent, harness)

        def run_episode(self, spec):
            active = Path(spec.active_root)
            source_hash, publication = self._publication_for(Path(spec.root), active)
            assert self._publication_is_valid(publication, source_hash)
            self.active_hashes.append(source_hash)
            candidate = Path(spec.root) / "harness"
            (candidate / "src" / "codex-rs" / "version.txt").write_text(
                f"candidate-{spec.episode}"
            )
            return EpisodeResult(episode=spec.episode, ok=True)

        def validate_candidate(self, harness_root):
            source_hash, _ = _fake_publication(
                self, Path(harness_root).parent, Path(harness_root)
            )
            self.candidate_hashes.append(source_hash)
            return ""

    def scorer(trace, context):
        return EvalResult(name="score", score={1: 1.0, 2: 0.0, 3: 1.0}[context.episode])

    adapter = CachedCodex()
    result = run(RunConfig(
        name="codex-cache", adapter=adapter, disposition=NEUTRAL,
        goal=GoalConfig.of(evaluators=(EvaluatorSpec(name="score", run=scorer),),
                           selection="accept_reject"),
        root=tmp_path / "run", model="mock", episodes=3,
    ))

    assert [row["accepted"] for row in result.eval_history] == [True, False, True]
    assert adapter.active_hashes == [
        adapter.active_hashes[0], adapter.candidate_hashes[0], adapter.candidate_hashes[0]
    ]
    assert adapter.candidate_hashes[1] != adapter.active_hashes[2]


def test_dockerfile_prewarms_test_profile_for_gate():
    dockerfile = _repo_file("environments", "codex-src", "Dockerfile")
    cmd = "cargo test --locked -p codex-tui -p codex-core -p codex-cli --lib --no-run"
    assert cmd in dockerfile
    assert dockerfile.index(cmd) < dockerfile.index("codex-source.tar")
    assert "codex-code-mode-host" in dockerfile
    assert "/opt/proteus-cargo-build-jobs" in dockerfile
    boot = _repo_file("environments", "codex-src", "boot.sh")
    assert "-type l" in boot and "readlink -z" in boot
    assert "proteus-codex --proteus-source-hash /opt/src" in dockerfile


def test_dockerfile_and_build_docs_pin_linux_amd64():
    dockerfile = _repo_file("environments", "codex-src", "Dockerfile")
    readme = _repo_file("environments", "codex-src", "README.md")
    assert "ARG CODEX_PLATFORM=linux/amd64" in dockerfile
    assert "FROM --platform=${CODEX_PLATFORM}" in dockerfile
    assert "docker build --platform linux/amd64" in _joined(readme)
    assert "x86_64-unknown-linux-gnu" in readme


def test_boot_timeout_and_image_tag():
    assert BOOT_TIMEOUT_S == 7200
    # adapter default image tag must match the documented build tag
    assert IMAGE == "proteus-env-codex-src:test-compile"
    readme = _repo_file("environments", "codex-src", "README.md")
    assert "-t proteus-env-codex-src:test-compile" in readme
