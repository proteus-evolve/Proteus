"""Public run() phase recovery, without Docker, network, or real credentials."""
from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from proteus.adapters.dsh import DshHarness
from proteus.core import GoalConfig, NEUTRAL
from proteus.core.episode import RunConfig, private_record_dir, run
from proteus.core.run_lock import run_lock
from test_dsh_phase_resume import log


class FakeContainer:
    def __init__(self):
        self.calls = []
        self.failed = False

    def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
        phase = next(p for p in ("observe", "propose", "act", "reflect")
                     if f"MARKER:{p}" in command[-1])
        self.calls.append(phase)
        log(root / f".dsh-state/sessions/attempt-{len(self.calls)}/session.jsonl.zstd", 1)
        candidate = root / "harness" / "work.txt"
        active = next(Path(m[0]) for m in mounts if m[1] == "/workspace")
        assert not (active / "work.txt").exists(), "active H0 must remain frozen"
        if phase == "observe":
            candidate.write_text("observe edits\n")
        if phase == "act" and not self.failed:
            candidate.write_text(candidate.read_text() + "partial act edits\n")
            self.failed = True
            raise subprocess.TimeoutExpired(command, timeout_s)
        if phase == "act":
            assert candidate.read_text() == "observe edits\npartial act edits\n"
            assert "calls already used before this phase: 3" in command[-1]
            assert "cumulative call 9 (6 calls available now)" in command[-1]
            candidate.write_text(candidate.read_text() + "continued act edits\n")
        return subprocess.CompletedProcess(command, 0, "", "")


class LocalDsh(DshHarness):
    def seed(self, harness_root, rng_seed=0):
        harness_root.mkdir(parents=True)
        (harness_root / "AGENTS.md").write_text("A synthetic fixture.")

    def validate_candidate(self, harness_root):
        return ""


def config(tmp_path, sandbox):
    return RunConfig(name="test", root=tmp_path / "run",
        adapter=LocalDsh(key="synthetic", sandbox=sandbox, phase_timeout_s=10),
        disposition=NEUTRAL, goal=GoalConfig(), model="synthetic", episodes=1,
        max_turns=10, hard_max_turns=12,
        phase_turns={"observe": 2, "propose": 2, "act": 3, "reflect": 3},
        announce_budget=True,
        phase_prompts={p: f"MARKER:{p}" for p in ("observe", "propose", "act", "reflect")})


def test_controller_phase_resume_preserves_candidate_trace_and_budget(tmp_path):
    sandbox = FakeContainer()
    cfg = config(tmp_path, sandbox)
    first = run(cfg)
    assert first.episodes_complete == 0 and "timeout" in first.error
    assert not (cfg.root / "harness/work.txt").exists()  # automatic rollback
    assert sandbox.calls == ["observe", "propose", "act"]
    before = (cfg.root / "traces/ep001.json").read_bytes()
    resumed = run(replace(cfg, resume_phase="act"), resume=True)
    assert resumed.episodes_complete == 1 and not resumed.error
    assert sandbox.calls == ["observe", "propose", "act", "act", "reflect"]
    assert resumed.counters["phase_observe_turns"] == 1
    assert resumed.counters["phase_act_turns"] == 2
    assert "continued act edits" in (cfg.root / "harness/work.txt").read_text()
    assert next((cfg.root / "traces").glob("ep001.attempt-*.json")).read_bytes() == before
    record = json.loads((private_record_dir(cfg.root) / "phase_recovery.json").read_text())
    assert record["used_calls"] == 3
    assert record["candidate_commit"] != record["active_commit"]
    assert not (private_record_dir(cfg.root) / "pending_candidate.json").exists()


@pytest.mark.parametrize("change", ["model", "goal", "budget", "phase", "candidate"])
def test_controller_refuses_mismatched_resume_without_execution(tmp_path, change):
    sandbox = FakeContainer()
    cfg = config(tmp_path, sandbox)
    run(cfg)
    altered = replace(cfg, resume_phase="act")
    if change == "model":
        altered.model = "different-model"
    elif change == "goal":
        altered.goal = GoalConfig.of("a different goal")
    elif change == "budget":
        altered.hard_max_turns = 20
    elif change == "phase":
        altered.resume_phase = "propose"
    else:
        (cfg.root / "harness/uncommitted.txt").write_text("a manual edit")
    before = (cfg.root / "traces/ep001.json").read_bytes()
    with pytest.raises(ValueError):
        run(altered, resume=True)
    assert sandbox.calls == ["observe", "propose", "act"]
    assert (cfg.root / "traces/ep001.json").read_bytes() == before


def test_single_writer_lock_rejects_duplicate_controller(tmp_path):
    cfg = config(tmp_path, FakeContainer())
    with run_lock(cfg.root), pytest.raises(ValueError, match="another controller"):
        run(cfg)
    assert not cfg.root.exists()


def test_phase_resume_requires_existing_failure_not_fresh_run(tmp_path):
    cfg = config(tmp_path, FakeContainer())
    with pytest.raises(ValueError, match="exact unfinished checkpoint"):
        run(replace(cfg, resume_phase="act"))
    assert not cfg.root.exists()
