"""Root-selected ONE full fresh-H0212 I/O-runtime replacement, no automatic queue.

This prepared administrative wrapper cannot release/admit itself. Exact new
source/config/lock/gate hashes and a root-owned per-label marker are mandatory
before any scoped-token read or admission GET. No retries or running migration.
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
from proteus.core.serialization import content_hash
from proteus.examples import combee_formula as module
from proteus.recipes.cli import source_pin

SOURCE_SHA = "21205d1dcdcff2a39dba31998629d22fd1ae94f9"
PREP_SHA = "2890c5271084eb713b28e97d269cfd4a9a6be598e6363bb5de035dcb56ca75de"
REVIEW_SHA = "c41430ad29f0df33b210e3ea344bba740013230f44c48f15f195699bcc786eef"
LABELS = tuple("linux-combee-formula-" + part + "-v5-ledger-io" for part in (
    "offline-naive_parallel", "offline-combee_fixed", "online-naive_parallel"))
FILES = {"preparation_audit_sha256": ROOT / "combee-serialized-io-prepared-full-replacements-v1.json",
    "API_review_sha256": ROOT / "api-ledger-serialized-io-review-v1.json",
    "workflow_gate_sha256": ROOT / "qualified-combee-serialized-io-workflows-v1.json",
    "recovery_gate_sha256": ROOT / "qualified-api-serialized-io-settled-trial-recovery-v1.json",
    "contention_gate_sha256": ROOT / "qualified-ledger-io-real-contention-v1.json"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
        stream.flush(); os.fsync(stream.fileno())


def save(path, value):
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    pending.replace(path)


def verify(label):
    assert label in LABELS
    source = source_pin("proteus", SOURCE)
    assert source.revision == SOURCE_SHA and source.dirty_patch_hash is None
    assert sha(FILES["preparation_audit_sha256"]) == PREP_SHA
    assert sha(FILES["API_review_sha256"]) == REVIEW_SHA
    prep = json.loads(FILES["preparation_audit_sha256"].read_text())
    assert {row["label"] for row in prep["profiles"]} == set(LABELS)
    profile = next(row for row in prep["profiles"] if row["label"] == label)
    assert profile["source_sha"] == SOURCE_SHA
    assert all(sha(Path(f["path"])) == f["sha256"] for f in profile["files"])
    assert all(sha(Path(p)) == h for p, h in profile["protected_original_hashes"].items())
    gate = json.loads(FILES["workflow_gate_sha256"].read_text())
    assert gate["status"] == "passed_unpaid_actual_frozen_workflow_HTTP_qualification"
    assert gate["source"]["revision"] == SOURCE_SHA and gate["paid_calls"] == 0
    assert gate["qualifier_sha256"] == sha(ROOT / "qualify_combee_serialized_io_workflows_v1.py")
    expected = {label.removeprefix("linux-combee-formula-").removesuffix("-v5-ledger-io") for label in LABELS}
    assert {row["label"].removeprefix("fixture-") for row in gate["profiles"]} == expected
    for row in gate["profiles"]:
        assert row["actual_same_run_HTTP_peak"] == row["ledger_actual_reserved_slot_peak"] == 8
        assert row["workflow_child_role_HTTP_peaks"] == {"generator": 8, "reflector": 8}
        assert row["curator_HTTP_peak"] == (2 if row["label"].endswith("combee_fixed") else 1)
        assert row["completed_resume_added_HTTP_calls_or_ledger_events"] == 0
        assert row["duplicate_request_IDs"] == 0
    assert gate["guard_wait_gate"]["no_POST_while_denied"]
    recovery = json.loads(FILES["recovery_gate_sha256"].read_text())
    assert recovery["source"]["revision"] == SOURCE_SHA
    assert recovery["status"] == "passed_unpaid_same_pin_47_1_2_map_recovery"
    assert recovery["committed_results_reused_byte_identical"] == 47
    assert recovery["new_HTTP_calls_on_recovery"] == 2
    assert recovery["completed_resume_new_HTTP_calls"] == recovery["completed_resume_new_ledger_events"] == 0
    assert recovery["qualifier_sha256"] == sha(ROOT / "qualify_api_serialized_io_settled_trial_recovery_v1.py")
    contention = json.loads(FILES["contention_gate_sha256"].read_text())
    assert contention["status"] == "passed_real_30s_SQLite_contention_old_fail_new_queue_UNPAID"
    assert contention["qualifier_sha256"] == sha(ROOT / "qualify_ledger_io_real_contention_v1.py")
    assert contention["paid_calls"] == 0 and len(contention["proofs"]) == 4
    for row in contention["proofs"]:
        assert sha(row["proof_path"]) == row["proof_sha256"]
    review = json.loads(FILES["API_review_sha256"].read_text())
    assert review["source"]["revision"] == SOURCE_SHA
    assert all(sha(item["path"]) == item["sha256"] for item in review["tests"].values())
    _, dataset, _, runs = module.load(Path(profile["root"]))
    assert len(runs) == 1
    _, recipe, lock = runs[0]
    assert content_hash(dataset) == profile["dataset_hash"]
    assert recipe.config_hash == profile["config_hash"] and content_hash(lock) == profile["lock_hash"]
    assert recipe.subject.initial_artifact.to_dict() == profile["H0"]
    assert recipe.subject.initial_artifact.content_hash == "sha256:31b9fe5c8d690797aee55588119ac476773ac7e8535fbc1ee4520918cc1bae27"
    assert recipe.budget.max_concurrency == recipe.method.options["max_workers"] == recipe.execution.max_workers == 8
    assert all(actor.budget.max_concurrency == 8 for actor in recipe.actors.values())
    hashes = {key: sha(p) for key, p in FILES.items()}
    return profile, hashes


def accepted(label):
    profile, hashes = verify(label)
    marker_path = ROOT / "root-acceptances" / (label + "-runtime-release-accepted.json")
    marker = json.loads(marker_path.read_text())
    assert marker["accepted"] is True and marker["label"] == label
    assert marker["scope"] == "single_full_SQLite_IO_replacement_fresh_H0_start_only"
    assert marker["source_sha"] == SOURCE_SHA and marker["launcher_sha256"] == sha(__file__)
    assert marker["config_hash"] == profile["config_hash"] and marker["lock_hash"] == profile["lock_hash"]
    assert all(marker[key] == digest for key, digest in hashes.items())
    assert marker["automatic_other_profile_dispatch"] is False
    assert marker["replaces_failed_attempt"] == profile["original_failed_label"]
    return profile, hashes, marker_path


def fresh(profile):
    assert not (Path(profile["root"]) / "runs").exists()
    assert not (ROOT / "handles" / (profile["label"] + ".json")).exists()
    old = json.loads((ROOT / "handles" / (profile["original_failed_label"] + ".json")).read_text())
    assert old["status"] == "failed"
    assert (Path(old["root"]) / "runs").exists()


def launch(label):
    profile, hashes, marker = accepted(label)
    fresh(profile)
    intent = ROOT / (label + "-runtime-launch-intent.json")
    receipt = ROOT / (label + "-runtime-launch.json")
    assert not intent.exists() and not receipt.exists()
    token = (ROOT / "private-bridge/scoped-token.txt").read_text().strip()
    request = urllib.request.Request("http://127.0.0.1:62125/admission/" + label,
        headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert json.load(response).get("allowed") is True
    once(intent, {"label": label, "created_epoch": time.time(), "launcher_sha256": sha(__file__),
        "marker_sha256": sha(marker), "automatic_retry": False})
    log = ROOT / "logs" / (label + ".log")
    argv = [sys.executable, "-u", str(Path(__file__)), "--execute-once", "--label", label]
    with log.open("xb") as output:
        process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    once(receipt, {"label": label, "pid": process.pid, "argv": argv, "created_epoch": time.time(),
        "source_sha": SOURCE_SHA, "config_hash": profile["config_hash"], "lock_hash": profile["lock_hash"],
        "launcher_sha256": sha(__file__), "root_marker_sha256": sha(marker), **hashes,
        "replaces_failed_attempt": profile["original_failed_label"],
        "fresh_H0": True, "resume": False, "retry": False, "log": str(log)})
    print(json.dumps({"label": label, "pid": process.pid, "receipt": str(receipt)}), flush=True)


def execute(label):
    profile, _, _ = accepted(label)
    intent = json.loads((ROOT / (label + "-runtime-launch-intent.json")).read_text())
    assert intent["label"] == label and intent["launcher_sha256"] == sha(__file__)
    once(ROOT / (label + "-runtime-execute-claim.json"), {"label": label, "pid": os.getpid(),
        "created_epoch": time.time(), "launcher_sha256": sha(__file__)})
    fresh(profile)
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
        print(json.dumps({"status": "static_SQLite_IO_replacement_bundle_verified_NOT_RELEASED_OR_DISPATCHED", "label": args.label,
            "source_sha": SOURCE_SHA, "config_hash": profile["config_hash"], "lock_hash": profile["lock_hash"],
            "launcher_sha256": sha(__file__), **hashes}), flush=True)
    elif args.launch_once:
        launch(args.label)
    else:
        execute(args.label)
