"""Phase recovery preserves prior work and never refills the tool budget."""
import json
import subprocess

import pytest

from proteus.adapters.dsh import DshHarness
from proteus.adapters.dsh_resume import DshPhaseResume
from proteus.core.adapter import EpisodeSpec
from proteus.core.budget import make_budget_plan
from proteus.core.continuity import HandoffStore


def log(path, count, kind="completed"):
    try:
        from compression import zstd
        compress = zstd.compress
    except ImportError:
        import zstandard
        compress = zstandard.ZstdCompressor().compress
    rows = [{"type": "turn/start", "data": {"turn": 1}}]
    rows += [{"type": "tool/call", "data": {"name": "bash", "arguments": "{}", "turn": 1}} for _ in range(count)]
    rows += [{"type": "turn/end", "data": {"reason": {"kind": kind}, "turn": 1}}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(compress(
        "".join(json.dumps(row) + "\n" for row in rows).encode()))


def failed_attempt(root):
    mapping = {phase: ["sessions/" + phase] for phase in ("observe", "propose", "act")}
    for phase, count in (("observe", 1), ("propose", 1), ("act", 2)):
        log(root / ".dsh-state/sessions" / phase / "session.jsonl.zstd", count,
            "error" if phase == "act" else "completed")
    (root / "traces").mkdir()
    (root / "traces/ep001.json").write_text(json.dumps(mapping))
    store = HandoffStore(root)
    for phase in ("observe", "propose", "act"):
        start = store.begin(1, phase)
        store.current.write_text("# Findings\nRecorded phase plan: " + phase + ".\n")
        store.finish(start, interrupted=phase == "act")
    return DshPhaseResume.capture(root, 1, "act", timeout_s=11)


def spec(root):
    (root / "active").mkdir(exist_ok=True)
    (root / "harness").mkdir(exist_ok=True)
    return EpisodeSpec(root=root, episode=1, model="synthetic",
        phase_prompts={phase: "MARKER:" + phase for phase in ("observe", "propose", "act", "reflect")},
        active_root=root / "active", max_turns=10, hard_max_turns=12,
        phase_turns={"observe": 2, "propose": 2, "act": 4, "reflect": 2},
        checkpoint_turns=1, announce_budget=True, continuity_mode="framework")


def test_native_phase_resume_skips_prefix_and_charges_old_calls(tmp_path):
    recovery = failed_attempt(tmp_path)
    assert recovery.used_calls == 4 and recovery.phase_start_calls == 2
    original = (tmp_path / "traces/ep001.json").read_bytes()
    store = HandoffStore(tmp_path)
    old_handoff = (store.history / "ep001/act.json").read_bytes()
    store.reconcile(0)  # What core does while restoring valid E0.
    store.set_controller_notice("Prior transport failure: STREAM_CLOSED")
    calls = []

    class Container:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            phase = command[-1].split("MARKER:")[-1]
            calls.append((phase, timeout_s, command[-1]))
            if phase == "act":
                assert "Recorded phase plan: act" in store.current.read_text()
                assert "STREAM_CLOSED" in store.current.read_text()
                assert "calls already used before this phase: 4" in command[-1]
                assert "cumulative call 10 (6 calls available now)" in command[-1]
            log(root / (".dsh-state/sessions/retry-" + phase + "/session.jsonl.zstd"),
                6 if phase == "act" else 2)
            assert stop_check()
            return subprocess.CompletedProcess(command, 137, "", "budget stop")

    adapter = DshHarness(key="synthetic", sandbox=Container(), phase_timeout_s=120,
                         phase_resume=recovery)
    result = adapter.run_episode(spec(tmp_path))
    assert result.ok and result.turns == 12 and result.counters["turn_capped"]
    assert result.counters["prior_attempt_tool_calls"] == 4
    assert result.counters["phase_act_turns"] == 8
    assert [row[:2] for row in calls] == [("act", 11), ("reflect", 120)]
    assert adapter.phase_resume is None
    updated = json.loads((tmp_path / "traces/ep001.json").read_text())
    assert len(updated["act"]) == 2 and updated["observe"] == recovery.mapping["observe"]
    archive = list((tmp_path / "traces").glob("ep001.attempt-*.json"))
    assert len(archive) == 1 and archive[0].read_bytes() == original
    assert (store.history / "ep001/act.json").read_bytes() == old_handoff
    assert (store.history / "ep001/act-02.json").exists()
    recovery.verify(tmp_path, 1)  # Prior native evidence was not overwritten.


def test_handoff_restore_is_explicit_hashed_and_keeps_controller_notice(tmp_path):
    recovery = failed_attempt(tmp_path)
    store = HandoffStore(tmp_path)
    store.reconcile(0)
    assert not store.latest.exists()
    store.set_controller_notice("new controller fact")
    store.restore_phase_handoff(1, "act", 1, recovery.handoff_sha256)
    assert "Recorded phase plan: act" in store.latest.read_text()
    assert "new controller fact" in store.latest.read_text()
    with pytest.raises(ValueError, match="changed"):
        store.restore_phase_handoff(1, "act", 1, "0" * 64)


@pytest.mark.parametrize("tamper", ["session", "handoff", "mapping", "wrong-root", "wrong-episode"])
def test_mismatched_evidence_cannot_start_recovery(tmp_path, tamper):
    recovery = failed_attempt(tmp_path)
    if tamper == "session":
        (tmp_path / ".dsh-state/sessions/act/session.jsonl.zstd").write_bytes(b"changed")
    elif tamper == "handoff":
        (tmp_path / ".proteus-state/handoffs/ep001/act.json").write_text("{}")
    elif tamper == "mapping":
        (tmp_path / "traces/ep001.json").write_text("{}")
    with pytest.raises(ValueError):
        recovery.verify(tmp_path / "wrong" if tamper == "wrong-root" else tmp_path,
                        2 if tamper == "wrong-episode" else 1, original_trace=True)


def test_recovered_reflect_budget_does_not_refill(tmp_path):
    recovery = failed_attempt(tmp_path)
    mapping = json.loads((tmp_path / "traces/ep001.json").read_text())
    mapping["reflect"] = ["sessions/reflect"]
    log(tmp_path / ".dsh-state/sessions/reflect/session.jsonl.zstd", 1, "error")
    (tmp_path / "traces/ep001.json").write_text(json.dumps(mapping))
    store = HandoffStore(tmp_path)
    store.finish(store.begin(1, "reflect"), interrupted=True)
    recovery = DshPhaseResume.capture(tmp_path, 1, "reflect", timeout_s=5)
    assert recovery.used_calls == 5 and recovery.phase_start_calls == 4

    class Container:
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            assert "cumulative call 6 (1 calls available now)" in command[-1]
            log(root / ".dsh-state/sessions/retry-reflect/session.jsonl.zstd", 1)
            assert stop_check()
            return subprocess.CompletedProcess(command, 137, "", "")

    result = DshHarness(key="synthetic", sandbox=Container(), phase_resume=recovery).run_episode(spec(tmp_path))
    assert result.ok and result.turns == 6


def test_new_budget_header_rejects_invalid_original_phase_start():
    plan = make_budget_plan(max_turns=10)
    for value in (-1, 11, True):
        with pytest.raises(ValueError):
            plan.prompt("act", 10, 1, phase_start_used=value)


def test_recovery_and_trace_keep_complete_events_before_truncated_tail(tmp_path):
    failed_attempt(tmp_path)
    path = tmp_path / ".dsh-state/sessions/act/session.jsonl.zstd"
    # A complete native frame followed by the beginning of another frame.
    path.write_bytes(path.read_bytes() + b"\x28\xb5\x2f\xfd")
    recovery = DshPhaseResume.capture(tmp_path, 1, "act", timeout_s=11)
    assert recovery.used_calls == 4
    trace = DshHarness(key="synthetic").read_trace(tmp_path, 1)
    assert sum(bool(event.tool) for event in trace) == 4


def test_recovery_rejects_undecodable_sessions(tmp_path):
    failed_attempt(tmp_path)
    (tmp_path / ".dsh-state/sessions/act/session.jsonl.zstd").write_bytes(b"not a native log")
    with pytest.raises(ValueError, match="decodable"):
        DshPhaseResume.capture(tmp_path, 1, "act", timeout_s=11)
