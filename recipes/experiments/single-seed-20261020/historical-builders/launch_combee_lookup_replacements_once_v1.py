"""NEW independent once-only failed927-to212 full replacement launcher.

No automatic selection/queue/retry/resume. Every exact label requires its own
root marker and admission. Third online-dynamic additionally requires explicit
separate root review/release; a passed toy gate does not release it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "sources/api-ledger-serialized-io-v1"
sys.path.insert(0, str(SOURCE))
from proteus.artifacts.store import ArtifactStore
from proteus.core.serialization import content_hash
from proteus.examples import combee_formula as module
from proteus.recipes.cli import source_pin

SOURCE_SHA = "21205d1dcdcff2a39dba31998629d22fd1ae94f9"
PREP_SHA = "4600e45108e723511fc23498ec2d490aef09c95975af1328cab3221103c16932"
REVIEW_SHA = "c41430ad29f0df33b210e3ea344bba740013230f44c48f15f195699bcc786eef"
TARGETED_SHA = "2ed3e36f5497f82a6536903ae2dc4cc496fefbe8089c310530eb76d0c73211e3"
THIRD_SHA = "2a53fb91effac22458cbaf5894f3b6ee1765a7d4d663ad7de119f63bcbed54f7"
LABELS = tuple("linux-combee-formula-" + part + "-v6-ledger-io" for part in (
    "online-combee_fixed", "offline-combee_dynamic", "online-combee_dynamic"))
SCOPE = "single_full_failed927_to212_fresh_H0_replacement_only"
FILES = {"preparation_audit_sha256": ROOT / "combee-lookup-serialized-io-full-preparations-v1.json",
    "reference_API_review_sha256": ROOT / "api-ledger-serialized-io-review-v1.json",
    "targeted_workflow_gate_sha256": ROOT / "qualified-combee-lookup-replacement-targets-v3.json",
    "third_inner_errors_sha256": ROOT / "lookup-online-dynamic-original-workflow-errors-owner-v1.json"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def once(path, value):
    encoded = json.dumps(value, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def save(path, value):
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    pending.replace(path)


def verify(label):
    assert label in LABELS
    assert Path(module.__file__).resolve().is_relative_to(SOURCE)
    source = source_pin("proteus", SOURCE)
    assert source.revision == SOURCE_SHA and source.dirty_patch_hash is None
    assert sha(FILES["preparation_audit_sha256"]) == PREP_SHA
    assert sha(FILES["reference_API_review_sha256"]) == REVIEW_SHA
    assert sha(FILES["targeted_workflow_gate_sha256"]) == TARGETED_SHA
    assert sha(FILES["third_inner_errors_sha256"]) == THIRD_SHA
    prep = json.loads(FILES["preparation_audit_sha256"].read_text())
    assert prep["helper_sha256"] == sha(ROOT / "prepare_combee_lookup_failed_replacements_v1.py")
    assert prep["source"]["revision"] == SOURCE_SHA
    assert {row["label"] for row in prep["profiles"]} == set(LABELS)
    for path, digest in prep["frozen_existing_THREE212_preparations_wrappers_bundles_markers_and_static_profiles"].items():
        assert sha(path) == digest
    for item in [*prep["reference_gate_files"].values(), *prep["exact_failure_boundary_evidence"]]:
        assert sha(item["path"]) == item["sha256"]
    review = json.loads(FILES["reference_API_review_sha256"].read_text())
    assert review["source"]["revision"] == SOURCE_SHA
    assert sum(item["passed"] for item in review["tests"].values()) == 105
    assert all(sha(item["path"]) == item["sha256"] for item in review["tests"].values())
    gate = json.loads(FILES["targeted_workflow_gate_sha256"].read_text())
    assert gate["status"] == "passed_targeted212_three_lookup_replacement_tiny_full_workflows_and_PBL"
    assert gate["source"]["revision"] == SOURCE_SHA
    assert gate["qualifier_sha256"] == sha(ROOT / "qualify_combee_lookup_replacement_targets_v3.py")
    assert gate["physical_interwindow_full_learning_curation_postevaluation_before_next_prediction_checked"]
    for row in gate["profiles"]:
        assert row["same_run_actual_HTTP_peak"] == row["ledger_reserved_slot_peak"] == 8
        assert row["workflow_child_role_peaks"] == {"generator": 8, "reflector": 8}
        assert row["completed_resume_added_HTTP_calls_or_events"] == 0
        assert all(check["actual_succeeded_postcuration_rollouts"] == check["tasks"] for check in row["postcuration_prediction_checks"])
    third = json.loads(FILES["third_inner_errors_sha256"].read_text())
    assert third["has_recorded_inner_database_locked_error"]
    assert all(row["cached_workflow_original_diagnostic"] == "OperationalError: database is locked" for row in third["affected_workflow_branches"])
    profile = next(row for row in prep["profiles"] if row["label"] == label)
    assert profile["source_sha"] == SOURCE_SHA
    assert all(sha(item["path"]) == item["sha256"] for item in profile["files"])
    assert all(sha(path) == digest for path, digest in profile["protected_original_hashes"].items())
    _, dataset, _, runs = module.load(Path(profile["root"]))
    assert len(runs) == 1
    _, recipe, lock = runs[0]
    assert content_hash(dataset) == profile["dataset_hash"]
    assert recipe.config_hash == profile["config_hash"] and content_hash(lock) == profile["lock_hash"]
    assert recipe.subject.initial_artifact.to_dict() == profile["H0"]
    assert recipe.subject.initial_artifact.content_hash == "sha256:31b9fe5c8d690797aee55588119ac476773ac7e8535fbc1ee4520918cc1bae27"
    ArtifactStore(Path(profile["root"]) / "artifact-store", module.registry()).files(recipe.subject.initial_artifact)
    assert recipe.budget.to_dict() == profile["budget"]
    assert recipe.execution.max_workers == recipe.method.options["max_workers"] == recipe.budget.max_concurrency == 8
    assert all(actor.budget.max_concurrency == 8 for actor in recipe.actors.values())
    hashes = {key: sha(path) for key, path in FILES.items()}
    return profile, hashes


def marker_path(label):
    return ROOT / "root-acceptances" / (label + "-lookup-replacement-release-accepted.json")


def accepted(label):
    profile, hashes = verify(label)
    path = marker_path(label)
    marker = json.loads(path.read_text())
    assert marker["accepted"] is True and marker["label"] == label and marker["scope"] == SCOPE
    assert marker["source_sha"] == SOURCE_SHA and marker["launcher_sha256"] == sha(__file__)
    assert marker["config_hash"] == profile["config_hash"] and marker["lock_hash"] == profile["lock_hash"]
    assert all(marker[key] == digest for key, digest in hashes.items())
    assert marker["automatic_other_profile_dispatch"] is False
    assert marker["replaces_failed_attempt"] == profile["original_failed_label"]
    if profile["separate_third_condition_hold"]:
        assert marker.get("separate_third_condition_root_review_and_release") is True
    return profile, hashes, path


def fresh(profile):
    assert not (Path(profile["root"]) / "runs").exists()
    assert not (ROOT / "handles" / (profile["label"] + ".json")).exists()
    old = json.loads((ROOT / "handles" / (profile["original_failed_label"] + ".json")).read_text())
    assert old["status"] == "failed"
    assert not Path(f"/proc/{old['pid']}").exists(), "original failed driver PID still exists; no fresh replacement"
    assert (Path(old["root"]) / "runs").exists()


def launch(label):
    profile, hashes, marker = accepted(label)
    fresh(profile)
    intent = ROOT / (label + "-lookup-replacement-launch-intent.json")
    receipt = ROOT / (label + "-lookup-replacement-launch.json")
    claim = ROOT / (label + "-lookup-replacement-execute-claim.json")
    log = ROOT / "logs" / (label + ".log")
    assert not any(path.exists() for path in (intent, receipt, claim, log))
    token = (ROOT / "private-bridge/scoped-token.txt").read_text().strip()
    request = urllib.request.Request("http://127.0.0.1:62125/admission/" + label,
        headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.load(response).get("allowed") is True
    once(intent, {"label": label, "created_epoch": time.time(), "launcher_sha256": sha(__file__),
        "marker_sha256": sha(marker), "config_hash": profile["config_hash"], "lock_hash": profile["lock_hash"], "automatic_retry": False})
    argv = [sys.executable, "-u", str(Path(__file__)), "--execute-once", "--label", label]
    with log.open("xb") as output:
        process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    once(receipt, {"label": label, "pid": process.pid, "argv": argv, "created_epoch": time.time(),
        "source_sha": SOURCE_SHA, "config_hash": profile["config_hash"], "lock_hash": profile["lock_hash"],
        "launcher_sha256": sha(__file__), "root_marker_sha256": sha(marker), **hashes,
        "replaces_failed_attempt": profile["original_failed_label"], "fresh_H0": True, "resume": False, "retry": False, "log": str(log)})
    print(json.dumps({"label": label, "pid": process.pid, "receipt": str(receipt)}), flush=True)


def execute(label):
    profile, _, marker = accepted(label)
    intent = json.loads((ROOT / (label + "-lookup-replacement-launch-intent.json")).read_text())
    assert intent["label"] == label and intent["launcher_sha256"] == sha(__file__)
    assert intent["marker_sha256"] == sha(marker)
    assert intent["config_hash"] == profile["config_hash"] and intent["lock_hash"] == profile["lock_hash"]
    fresh(profile)
    once(ROOT / (label + "-lookup-replacement-execute-claim.json"), {"label": label, "pid": os.getpid(),
        "created_epoch": time.time(), "launcher_sha256": sha(__file__)})
    os.environ["PROTEUS_RELAY_TOKEN"] = (ROOT / "private-bridge/scoped-token.txt").read_text().strip()
    handle = ROOT / "handles" / (label + ".json")
    record = {**profile, "pid": os.getpid(), "status": "running", "started_epoch": time.time(),
        "launcher_sha256": sha(__file__), "replaces_failed_attempt": profile["original_failed_label"]}
    once(handle, record)
    try:
        result = module.execute(Path(profile["root"]), resume=False)
        once(ROOT / "results" / (label + ".json"), result)
        save(handle, {**record, "status": "completed", "finished_epoch": time.time()})
    except BaseException as exc:
        save(handle, {**record, "status": "failed", "finished_epoch": time.time(),
            "diagnostic": type(exc).__name__ + ": " + str(exc)[:2000]})
        raise


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", choices=LABELS, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect-prepared", action="store_true")
    mode.add_argument("--launch-once", action="store_true")
    mode.add_argument("--execute-once", action="store_true")
    args = parser.parse_args()
    if args.inspect_prepared:
        profile, hashes = verify(args.label)
        fresh(profile)
        print(json.dumps({"status": "NEW_static_bundle_verified_NOT_RELEASED_OR_DISPATCHED", "label": args.label,
            "source_sha": SOURCE_SHA, "config_hash": profile["config_hash"], "lock_hash": profile["lock_hash"],
            "separate_third_condition_hold": profile["separate_third_condition_hold"], "launcher_sha256": sha(__file__), **hashes}), flush=True)
    elif args.launch_once:
        launch(args.label)
    else:
        execute(args.label)
