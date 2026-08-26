"""Aki adapter, measure path — pure parsing, no Aki checkout required."""

import json

from proteus.adapters.aki import AkiHarness
from proteus.core.disposition import NEUTRAL, record, review


def _write_trace(root, episode, events):
    tdir = root / "traces"
    tdir.mkdir(parents=True, exist_ok=True)
    path = tdir / f"ep{episode:03d}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


def test_arm_mapping():
    a = AkiHarness()
    assert a._arm_label(NEUTRAL) == "C0_neutral"
    assert a._arm_label(review("memory")) == "Cmem_memory"
    assert a._arm_label(review("tools")) == "Ctol_tools"
    assert a._arm_label(record("skills")) == "L4skl_skills"


def test_trace_parsing_and_phase_mapping(tmp_path):
    _write_trace(tmp_path, 1, [
        {"event": "phase_start", "iteration": 0, "data": {"phase": "observe"}},
        {"event": "tool_call", "iteration": 1, "data": {"tool_name": "memory_list", "params": {}}},
        {"event": "phase_start", "iteration": 0, "data": {"phase": "select_and_act"}},
        {"event": "tool_call", "iteration": 2,
         "data": {"tool_name": "memory_write", "params": {"key": "k"}}},
        {"event": "reply", "iteration": 3, "data": {"content": "done"}},
        {"event": "episode_status", "data": {"status": "complete", "error": ""}},
        {"event": "episode_end", "data": {"counters": {"turns_used": 3}}},
    ])
    a = AkiHarness()
    trace = a.read_trace(tmp_path, 1)
    assert [e.tool for e in trace] == ["memory_list", "memory_write", None]
    assert trace[1].phase == "act"            # select_and_act -> act
    assert trace[1].surface == "memory"       # write tool mapped to its surface
    assert trace[0].surface is None           # read tool: no surface
    status, counters = a._episode_outcome(tmp_path, 1)
    assert status["status"] == "complete"
    assert counters["turns_used"] == 3


def test_fingerprint_changes_with_loop(tmp_path):
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "loop.py").write_text("A = 1\n")
    a = AkiHarness()
    f1 = a.disposition_fingerprint(harness)
    (harness / "loop.py").write_text("A = 2\n")
    assert a.disposition_fingerprint(harness) != f1
