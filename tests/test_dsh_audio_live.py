import importlib.util
import json
import time
from pathlib import Path

from proteus.core import snapshot


SCRIPT = Path(__file__).parents[1] / "scripts" / "dsh_audio_live.py"
SPEC = importlib.util.spec_from_file_location("dsh_audio_live", SCRIPT)
LIVE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(LIVE)


class TraceDouble:
    def read_trace(self, root, episode):
        from proteus.core.adapter import ActionEvent
        return [
            ActionEvent(1, "act", "write", "loop", {"file_path": "/workspace/src/audio.ts", "content": "secret"}),
            ActionEvent(2, "reflect", None, None, {}, "private model prose"),
        ]


def test_missing_sweep_is_scheduled(tmp_path):
    payload = LIVE.build_payload(tmp_path)
    assert payload["status"] == "scheduled"
    assert payload["episodes"] == []
    assert payload["baseline"]["release"] == "dsh-v0.1.0-rc.8"


def test_launcher_state_reports_running_and_stale_state_reports_paused(tmp_path):
    state = tmp_path / "live-state.json"
    state.write_text(json.dumps({
        "status": "running", "heartbeat_at": time.time(), "proteus_version": "0.1.0",
    }))
    assert LIVE.build_payload(tmp_path)["status"] == "running"
    state.write_text(json.dumps({
        "status": "running", "heartbeat_at": time.time() - LIVE.HEARTBEAT_STALE_S - 1,
        "proteus_version": "0.1.0",
    }))
    assert LIVE.build_payload(tmp_path)["status"] == "paused"


def test_export_uses_snapshots_and_redacts_arguments(tmp_path):
    root = tmp_path / "sweep"
    run_id = "run-123456789abc"
    run_root = root / "runs" / run_id
    harness = run_root / "harness"
    harness.mkdir(parents=True)
    (harness / "AGENTS.md").write_text("seed")
    snapshot.init(harness)
    (harness / "src").mkdir()
    (harness / "src" / "audio.ts").write_text("export const audio = true")
    snapshot.commit(harness, "episode 1: accepted")
    (root / "progress").mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({
        "name": "dsh-audio", "episodes": 2, "goal": "learn audio",
        "runs": [{"id": run_id, "arm": "neutral", "seed": 0}],
    }))
    (root / "progress" / f"{run_id}.jsonl").write_text(json.dumps({
        "ts": 1, "episode": 1, "ok": True, "turns": 2, "tool_calls": 1,
        "units": {"loop": 1}, "accepted": True,
        "scores": {"dsh-audio-capability": 0.25},
    }) + "\n")

    payload = LIVE.build_payload(root, adapter=TraceDouble())
    episode = payload["episodes"][0]
    assert payload["status"] == "running"
    assert episode["changes"]["added"] == ["src/audio.ts"]
    assert episode["steps"] == [["act", "write", "src/audio.ts"]]
    assert "secret" not in json.dumps(payload)
    assert "private model prose" not in json.dumps(payload)
