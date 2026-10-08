"""Offline real-sweep, CLI, presentation contract, and local-server security checks."""
import copy
from http.client import HTTPConnection
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from proteus.cli import main
from proteus.visualize import config
from proteus.visualize.data import Workspace, read_lines, snapshot_units
from proteus.visualize.server import ViewerServer


@pytest.fixture
def sweep(tmp_path):
    root = tmp_path / "sweep"
    assert main(["run", "--harness", "minimal", "--arm", "neutral", "--arm", "review:notes",
                 "--goal", "none", "--episodes", "2", "--seeds", "2", "--out", str(root),
                 "--evaluator", "tool-calls@observe", "--eval-initial", "tool-calls",
                 "--evaluator", "units:notes@hidden"]) == 0
    return root


def test_durable_evidence_and_metadata(sweep):
    workspace = Workspace(sweep)
    catalog = workspace.summary()
    assert len(catalog["runs"]) == 4
    data = workspace.load(catalog["runs"][0]["id"])
    assert not data["synthetic"] and data["status"] == "completed"
    assert data["episodesComplete"] == 2
    assert len(data["episodes"]) == 3
    assert data["episodes"][0]["evaluations"]["tool-calls"]["score"] == 0
    assert data["episodes"][-1]["distance"] > 0
    assert data["episodes"][-1]["calls"] > 0
    assert data["episodes"][-1]["cost"] is None
    assert data["behaviorReferenceEpisode"] == 1
    assert data["episodes"][0]["behavior"] is None
    assert data["episodes"][-1]["selfCheck"] is None
    for ep in data["episodes"]:
        for action in ep["actions"]:
            assert set(action) == {"turn", "phase", "tool", "surface"}
    population = workspace.population(data["id"])
    assert population["measurements"][0]["statistics"]["n_runs"] == 2


def test_hidden_export_and_error_zero(sweep):
    workspace = Workspace(sweep)
    identity = workspace.summary()["runs"][0]["id"]
    run_id = workspace.catalog()[0][0]["record"]["id"]
    path = sweep / "runs" / ".proteus-records" / run_id / "eval_history.json"
    history = json.loads(path.read_text())
    history[-1]["results"][0].update(score=0, status="error", detail="do not export this")
    history[-1]["error"] = "private evaluator failure"
    path.write_text(json.dumps(history))
    assert workspace.load(identity)["episodes"][-1]["score"] is None
    exported = workspace.export(identity)
    assert [s["name"] for s in exported["evaluators"]] == ["tool-calls"]
    assert "units:notes" not in exported["episodes"][-1]["evaluations"]
    assert "error" not in exported["episodes"][-1]
    assert "do not export this" not in json.dumps(exported)
    private = workspace.export(identity, include_hidden=True)
    assert "units:notes" in private["episodes"][-1]["evaluations"]


def test_legacy_native_mapping_is_not_reasoning(sweep):
    workspace = Workspace(sweep)
    item = workspace.catalog()[0][0]
    rid = item["record"]["id"]
    progress_path = sweep / "progress" / (rid + ".jsonl")
    rows = read_lines(progress_path)
    for row in rows:
        row.pop("action_metadata", None)
    progress_path.write_text("".join(json.dumps(r)+"\n" for r in rows))
    run_root = sweep / "runs" / rid
    for ep in (1, 2):
        (run_root / "traces" / f"ep{ep:03d}.jsonl").unlink()
        (run_root / "traces" / f"ep{ep:03d}.json").write_text('{"reasoning":"never read me"}')
    data = workspace.load(item["id"])
    assert data["episodes"][-1]["behavior"] is None
    assert not data["episodes"][-1]["actionsAvailable"]
    assert "never read me" not in json.dumps(data)


def test_catalog_path_confinement_and_partial_append(tmp_path, sweep):
    link = tmp_path / "workspace" / "escape"
    link.parent.mkdir()
    link.symlink_to(sweep, target_is_directory=True)
    summary = Workspace(link.parent).summary()
    assert not summary["runs"] and summary["errors"]
    path = tmp_path / "partial.jsonl"
    path.write_text('{"episode":1}\n{"episode":')
    assert read_lines(path) == [{"episode": 1}]
    path.write_text('{"episode":1}\nnot json\n')
    with pytest.raises(ValueError):
        read_lines(path)


def test_corrupt_adapter_type_never_imported(sweep):
    manifest_path = sweep / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["condition"]["adapter"]["type"] = "nonexistent_untrusted_module:SideEffect"
    manifest_path.write_text(json.dumps(manifest))
    workspace = Workspace(sweep)
    assert workspace.load(workspace.summary()["runs"][0]["id"])["episodesComplete"] == 2


def test_failed_candidate_is_measured_separately(tmp_path):
    from proteus.adapters.minimal import MinimalHarness
    from proteus.core import GoalConfig, NEUTRAL
    from proteus.sweep import SweepConfig, run_sweep

    class FailingHarness(MinimalHarness):
        def validate_candidate(self, root):
            return "compile failure"

    root = tmp_path / "failed"
    run_sweep(SweepConfig(name="failed",adapter_factory=FailingHarness,
        arms=[NEUTRAL],seeds=1,goal=GoalConfig.no_goal(),root=root,episodes=2))
    workspace = Workspace(root)
    data = workspace.load(workspace.summary()["runs"][0]["id"])
    ep = data["episodes"][1]
    assert ep["accepted"] is False and ep["gate"] == "Failed viability gate"
    assert ep["sha"] != ep["candidateSha"]
    assert ep["distance"] == 0 and ep["step"] == 0
    assert ep["files"] and ep["score"] is None
    assert data["episodesComplete"] == 2


def test_snapshot_measurement_ignores_symlinks_and_supports_root_surface(tmp_path):
    from proteus.core import snapshot
    harness = tmp_path / "run" / "harness"
    harness.mkdir(parents=True)
    (harness / "notes").mkdir()
    (harness / "notes" / "safe.txt").write_text("safe")
    (harness / "notes" / "escape").symlink_to(tmp_path)
    snapshot.init(harness)
    sha = snapshot.commit_for_episode(harness, 0)
    repo = harness.parent / ".snapshot.git"
    measured = snapshot_units(str(repo),sha,json.dumps([{"name":"all","subdir":".","unit":"file"}]))
    assert "notes/safe.txt" in measured["all"]
    assert not any("escape" in key for key in measured["all"])


def test_recorded_cost_has_no_pricing_inference(sweep):
    workspace = Workspace(sweep)
    item = workspace.catalog()[0][0]
    path = sweep / "runs" / ".proteus-records" / item["record"]["id"] / "eval_history.json"
    rows = json.loads(path.read_text())
    for row in rows:
        row["counters"].update(cost_usd=.25, duration_s=5)
    path.write_text(json.dumps(rows))
    data = workspace.load(item["id"])
    assert data["episodes"][-1]["cost"] == .5
    assert data["episodes"][-1]["duration"] == 5
    rows[0]["counters"].pop("cost_usd")
    path.write_text(json.dumps(rows))
    assert workspace.load(item["id"])["episodes"][-1]["cost"] is None


def test_no_goal_evaluator_and_unlimited_configuration():
    c = config.validate({"goal":"", "evaluators":[], "normal":0, "hard":0,
                         "checkpoint":0, "phase_turns":{}})
    argv = config.arguments(c, "safe output")
    assert argv[argv.index("--goal")+1] == "none"
    assert argv[argv.index("--max-turns")+1] == "0"
    assert argv[argv.index("--on-existing")+1] == "refuse"
    assert "--evaluator" not in argv


def test_recognizable_credentials_are_rejected_and_redacted(sweep):
    secret = "sk-" + "a" * 32  # synthetic fixture, never a real credential
    with pytest.raises(ValueError,match="credential-like"):
        config.validate({"goal":"use " + secret})
    path = sweep / "manifest.json"
    value = json.loads(path.read_text())
    value["goal"] = "legacy unsafe goal " + secret
    path.write_text(json.dumps(value))
    workspace = Workspace(sweep)
    exported = workspace.export(workspace.summary()["runs"][0]["id"])
    assert secret not in json.dumps(exported)
    assert "[REDACTED]" in exported["goal"]


def test_cadence_and_custom_phases_roundtrip():
    c = config.validate({"normal":10, "hard":12, "checkpoint":1,
        "phase_names":["explore","act"], "phase_turns":{"explore":3,"act":7},
        "phase_prompts":{"explore":"Inspect and preserve evidence."},
        "evaluators":[{"spec":"tool-calls","visibility":"hidden","at":[0,2,4],
                       "selection_eligible":False}]})
    argv = config.arguments(c, "output")
    assert "tool-calls@hidden" in argv
    assert "tool-calls=0,2,4" in argv
    assert "explore,act" in argv
    assert "tool-calls" in argv[argv.index("--selection-exclude")+1]


@pytest.mark.parametrize("change", [
    {"api_key":"not allowed"}, {"harness":"malicious:Adapter"},
    {"normal":True}, {"phase_turns":{"observe":300}},
    {"checkpoint":2,"announce_budget":False}, {"phases":"not a field"},
    {"evaluators":[{"spec":"tool-calls","every":0}]},
    {"evaluators":[{"spec":"tool-calls","visibility":"public"}]},
    {"evaluators":[{"spec":"contains:../escape:needle"}]},
])
def test_invalid_config_rejected(change):
    with pytest.raises((ValueError, TypeError)):
        config.validate(change)


@pytest.fixture
def server(tmp_path):
    with ViewerServer(tmp_path, 0) as instance:
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        yield instance
        instance.shutdown()
        thread.join(timeout=2)


def request(server, path, *, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", server.port, timeout=10)
    all_headers = {"Authorization":"Bearer "+server.token,
                   "Content-Type":"application/json", **(headers or {})}
    conn.request("POST" if body is not None else "GET", path,
                 json.dumps(body) if body is not None else None, all_headers)
    result = conn.getresponse()
    status, raw = result.status, result.read()
    conn.close()
    return status, raw


def test_server_rejects_cross_origin_and_never_serves_files(server):
    assert request(server,"/api/workspace", headers={"Host":"attacker.example"})[0] == 403
    assert request(server,"/api/session", headers={"Origin":"https://attacker.example"})[0] == 403
    assert request(server,"/api/workspace", headers={"Authorization":""})[0] == 401
    assert request(server,"/api/workspace", headers={"Sec-Fetch-Site":"cross-site"})[0] == 403
    assert request(server,"/manifest.json")[0] == 404
    assert request(server,"/../README.md")[0] == 404
    assert request(server,"/api/runs", body={})[0] == 403
    status, raw = request(server,"/api/validate", body={})
    assert status == 200 and json.loads(raw)["valid"]
    for asset in ("/","/app.js","/styles.css","/model.js"):
        assert request(server,asset)[0] == 200


def test_authenticated_api_returns_actual_sweep(sweep):
    with ViewerServer(sweep,0) as server:
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            status, raw = request(server,"/api/workspace")
            assert status == 200
            identity = json.loads(raw)["runs"][0]["id"]
            status, raw = request(server,"/api/run?id="+identity)
            assert status == 200 and json.loads(raw)["episodesComplete"] == 2
            status, raw = request(server,"/api/export?id="+identity)
            assert status == 200 and not json.loads(raw)["exportPolicy"]["hiddenIncluded"]
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_explicit_offline_launch_uses_existing_cli(tmp_path):
    server = ViewerServer(tmp_path,0,allow_run_control=True)
    c = copy.deepcopy(config.DEFAULT)
    c.update(episodes=1, normal=20, hard=0, checkpoint=0, phase_turns={}, announce_budget=False)
    try:
        job = server.launches.start(c)
        process = server.launches.jobs[job["id"]]["process"]
        assert process.wait(timeout=20) == 0
        assert server.launches.status()[0]["status"] == "completed"
        data = Workspace(tmp_path)
        assert len(data.summary()["runs"]) == 1
        assert data.load(data.summary()["runs"][0]["id"])["episodesComplete"] == 1
    finally:
        server.server_close()


def test_install_assets_and_cli_help():
    output = subprocess.run([sys.executable,"-m","proteus.cli","visualize","--help"],
                            capture_output=True,text=True,check=True).stdout
    assert "--allow-run-control" in output
    from proteus.visualize import server
    for path, kind in server.ASSETS.values():
        assert (Path(server.__file__).parent / "static" / path).is_file()
