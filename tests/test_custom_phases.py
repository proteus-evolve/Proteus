from dataclasses import replace
import json

import pytest

from proteus.core import GoalConfig
from proteus.core.budget import make_budget_plan
from proteus.core.continuity import HandoffStore
from proteus.core.episode import run
from test_goals import RecordingHarness, _cfg


def test_custom_phases_are_ordered_context_fresh_and_snapshotted(tmp_path):
    adapter = RecordingHarness()
    cfg = _cfg(tmp_path, adapter, GoalConfig.of("A stated goal"), episodes=2)
    cfg.phases = ("inspect", "act", "verify")
    cfg.phase_prompts = {"inspect": "Inspect the current files.", "verify": "Verify the edits."}
    cfg.max_turns = 12
    cfg.phase_turns = {"inspect": 2, "act": 8, "verify": 2}
    cfg.hard_max_turns = 20
    result = run(cfg)
    assert result.episodes_complete == 2
    assert list(adapter.prompts[1]) == ["inspect", "act", "verify"]
    assert all("A stated goal" in p for p in adapter.prompts[1].values())
    assert set(e.phase for e in adapter.read_trace(cfg.root, 1)) <= set(cfg.phases)
    assert "phase_verify_turns" in result.counters and "phase_reflect_turns" not in result.counters
    run(replace(cfg, episodes=3), start=2)
    with pytest.raises(ValueError, match="contract differs"):
        run(replace(cfg, phases=("act", "inspect", "verify"), episodes=4), start=3)


def test_phase_budget_reserves_configured_suffix():
    plan = make_budget_plan(max_turns=12, hard_max_turns=20,
        phases=("inspect", "act", "verify"),
        phase_turns={"inspect": 2, "act": 8, "verify": 2})
    assert plan.stop_at("inspect", 0) == 2
    assert plan.stop_at("act", 1) == 18
    assert plan.stop_at("verify", 18) == 20
    no_act = make_budget_plan(max_turns=6, phases=("inspect", "verify"),
                             phase_turns={"inspect": 3, "verify": 3})
    assert no_act.stop_at("verify", 1) == 4  # no implicit act/burst phase


@pytest.mark.parametrize("phases", [(), ("act", "act"), ("../escape",), ("Act",), "act"])
def test_invalid_phases_fail_before_seeding(tmp_path, phases):
    cfg = _cfg(tmp_path, RecordingHarness(), GoalConfig())
    with pytest.raises(ValueError):
        run(replace(cfg, phases=phases))
    assert not cfg.root.exists()


def test_unknown_phase_requires_prompt_and_unsupported_adapter_refuses(tmp_path):
    cfg = _cfg(tmp_path, RecordingHarness(), GoalConfig())
    with pytest.raises(ValueError, match="explicit nonempty prompt"):
        run(replace(cfg, phases=("inspect",)))
    cfg.adapter.supports_custom_phases = False
    with pytest.raises(ValueError, match="does not support"):
        run(replace(cfg, phases=("act",)))
    assert not cfg.root.exists()


def test_handoff_reconcile_uses_configured_order(tmp_path):
    store = HandoffStore(tmp_path, phases=("reflect", "review"))
    for phase in ("reflect", "review"):
        start = store.begin(1, phase)
        store.current.write_text("saved " + phase)
        store.finish(start)
    store.reconcile(1)
    assert store.current.read_text().strip() == "saved review"


def test_cli_custom_phases_are_locked_in_manifest(tmp_path):
    from proteus.cli import main
    out = tmp_path / "sweep"
    args = ["run", "--out", str(out), "--seeds", "1", "--episodes", "1",
            "--phases", "inspect,act", "--phase-prompt", "inspect=Inspect the current state."]
    assert main(args) == 0
    condition = json.loads((out / "manifest.json").read_text())["condition"]
    assert condition["phases"] == ["inspect", "act"]
    assert "phase_prompt_sha256" in condition
    assert main(args + ["--on-existing", "resume"]) == 0
    with pytest.raises(SystemExit, match="condition differs"):
        main(args[:-2] + ["--phase-prompt", "inspect=A changed prompt.", "--on-existing", "resume"])
