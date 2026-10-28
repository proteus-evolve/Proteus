"""Prepare THREE explicit fresh-H0 replacements of FAILED full D0 conditions.

No launch, guard admission change, token access, ledger copy, method reduction,
warm-start, or mutation of original preparation/runtime. Root selects/adopts.
"""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "sources/api-ledger-serialized-io-v1"
BASE_SHA = "d0a9dc848e8978f14fac2e70e57a22998fdf07f5"
SOURCE_SHA = "21205d1dcdcff2a39dba31998629d22fd1ae94f9"
AUDIT = ROOT / "combee-serialized-io-prepared-full-replacements-v1.json"
REQUIRED = (("offline", "naive_parallel"), ("offline", "combee_fixed"), ("online", "naive_parallel"))
sys.path.insert(0, str(SOURCE))
from proteus.core.serialization import content_hash
from proteus.examples import combee_formula as module
from proteus.recipes.cli import installed_packages, source_pin
from proteus.recipes.manifest import SourcePin


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")


def normalized(recipe):
    row = recipe.to_dict()
    for actor in row["actors"].values():
        actor["runtime"]["options"]["base_url"] = "<SCOPED_ROUTE>"
        actor["runtime"]["options"]["dispatch_guard_path"] = "<SCOPED_GUARD>"
    for spec in row["evaluations"]:
        spec["tasks"]["suite"]["options"]["path"] = "<STUDY_TASKS>"
    return row


def main():
    os.umask(0o077)
    assert not AUDIT.exists()
    source = source_pin("proteus", SOURCE)
    original = source_pin("proteus", ROOT / "sources/api")
    assert original.revision == BASE_SHA and original.dirty_patch_hash is None
    assert source.revision == SOURCE_SHA and source.dirty_patch_hash is None
    changed = subprocess.check_output(["git", "-C", str(SOURCE), "diff", "--name-only", BASE_SHA, SOURCE_SHA], text=True).splitlines()
    assert changed == ["proteus/core/ledger.py"]
    patch = subprocess.check_output(["git", "-C", str(SOURCE), "diff", "--binary", BASE_SHA, SOURCE_SHA])
    source_receipt_path = ROOT / "api-ledger-serialized-io-source-v1.json"
    source_receipt = json.loads(source_receipt_path.read_text())
    assert source_receipt["source_revision"] == SOURCE_SHA
    assert source_receipt["base_revision"] == "927afb593cb8e6ae88f163ecc83da5c196c4d948"
    assert source_receipt["protected_sources_unchanged"] is True
    assert sha(SOURCE / "proteus/core/ledger.py") == source_receipt["source_ledger_sha256"] == "15d5f05824e8587cdc8061dda2540bfd63b48165266d05d7b883aec3eb9335d5"
    assert sha(ROOT / "ledger-serialized-io-only-v1.patch") == source_receipt["patch_sha256"] == "fd49ee746a9d4796907bc0768e5adf6e2cf87bcbfb8a25fa6e0733f2d26160f1"
    xml = ROOT / "api-ledger-serialized-io-existing-and-differential-tests-v1.xml"
    cases = list(ET.parse(xml).iter("testcase"))
    assert len(cases) == 102 and sum(c.find("skipped") is not None for c in cases) == 2
    assert not any(c.find("failure") is not None or c.find("error") is not None for c in cases)
    structure_path = ROOT / "api-ledger-serialized-io-structure-tests-v2.xml"
    structure_cases = list(ET.parse(structure_path).iter("testcase"))
    assert len(structure_cases) == 5
    assert not any(c.find(tag) is not None for c in structure_cases for tag in ("error", "failure", "skipped"))
    contention_path = ROOT / "qualified-ledger-io-real-contention-v1.json"
    contention = json.loads(contention_path.read_text())
    assert contention["status"] == "passed_real_30s_SQLite_contention_old_fail_new_queue_UNPAID"
    assert len(contention["proofs"]) == 4 and contention["paid_calls"] == 0
    for proof in contention["proofs"]:
        assert sha(proof["proof_path"]) == proof["proof_sha256"]
        assert proof["pragmas"] == {"busy_timeout": 30000, "journal_mode": "delete", "synchronous": 2}
        assert all(row["status"] == ("started" if proof["version"] == "new" else "failed") for row in proof["outcomes"])
    workflow_path = ROOT / "qualified-combee-serialized-io-workflows-v1.json"
    workflow = json.loads(workflow_path.read_text())
    assert workflow["status"] == "passed_unpaid_actual_frozen_workflow_HTTP_qualification"
    assert workflow["source"]["revision"] == SOURCE_SHA and workflow["paid_calls"] == 0
    assert {row["label"] for row in workflow["profiles"]} == {f"fixture-{mode}-{variant}" for mode, variant in REQUIRED}
    assert all(row["actual_same_run_HTTP_peak"] == row["ledger_actual_reserved_slot_peak"] == 8
        and row["workflow_child_role_HTTP_peaks"] == {"generator": 8, "reflector": 8}
        and row["completed_resume_added_HTTP_calls_or_ledger_events"] == 0 for row in workflow["profiles"])
    gate_path = ROOT / "qualified-api-serialized-io-settled-trial-recovery-v1.json"
    gate = json.loads(gate_path.read_text())
    assert gate["status"] == "passed_unpaid_same_pin_47_1_2_map_recovery"
    assert gate["source"]["revision"] == SOURCE_SHA
    assert gate["actual_loopback_HTTP_peak"] == 8 and gate["committed_results_reused_byte_identical"] == 47
    assert gate["new_HTTP_calls_on_recovery"] == 2 and gate["completed_resume_new_HTTP_calls"] == 0
    helper = SourcePin(name="unpaid-api-serialized-io-full-replacement-preparation-v1",
        repository="local-content-addressed:" + Path(__file__).name, revision=sha(__file__))
    profiles = []
    for mode, variant in REQUIRED:
        old_label = f"linux-combee-formula-{mode}-{variant}-v3-1"
        label = f"linux-combee-formula-{mode}-{variant}-v5-ledger-io"
        old, new = ROOT / "formal" / old_label, ROOT / "formal" / label
        handle = json.loads((ROOT / "handles" / (old_label + ".json")).read_text())
        assert handle["status"] == "failed"
        assert (old / "runs").is_dir() and not new.exists()
        protected = {str(p): sha(p) for p in (old / "study.json", old / "tasks.json", old / "provenance.json",
            *old.glob("recipes/*.json"), *old.glob("locks/*.json"), ROOT / "handles" / (old_label + ".json"))}
        study, dataset, _, runs = module.load(old, check_runtime=False)
        assert len(runs) == 1
        _, old_recipe, _ = runs[0]
        original_run = old / "runs" / runs[0][0].name
        protected.update({str(p): sha(p) for p in (original_run / "manifest.json", original_run / "private/ledger.sqlite3")})
        assert study.settings.seeds == (0,) and study.settings.modes == (mode,) and study.settings.variants == (variant,)
        assert Counter(r.task.split for r in dataset.records) == {"train": 500, "validation": 300, "test": 200}
        assert old_recipe.subject.initial_artifact.content_hash == "sha256:31b9fe5c8d690797aee55588119ac476773ac7e8535fbc1ee4520918cc1bae27"
        assert study.settings.max_num_rounds == 3 and study.settings.eval_steps == 500 and study.settings.batch_size == 50
        assert study.settings.max_workers == 8 and study.settings.online_eval_frequency == 15
        actors = {}
        guard_path = ROOT / "guards" / (label + ".json")
        once(guard_path, {"version": 2, "admission_url": "http://127.0.0.1:62125/admission/" + label,
            "token_env": "PROTEUS_RELAY_TOKEN"})
        for role, actor in old_recipe.actors.items():
            assert actor.model == "deepseek-flash" and actor.model_settings["max_tokens"] == 4096
            assert actor.budget.max_concurrency == 8
            options = dict(actor.runtime.options)
            options.update(base_url="http://127.0.0.1:62125/" + label + "/v1", dispatch_guard_path=str(guard_path))
            actors[role] = replace(actor, runtime=replace(actor.runtime, options=options))
        settings = replace(study.settings, **actors)
        provenance = json.loads((old / "provenance.json").read_text())
        provenance["Linux_profile"] = {**provenance["Linux_profile"], "id": label, "source": source.to_dict()}
        provenance["isolated_runtime_FULL_replacement_preparation"] = {
            "source_base": BASE_SHA, "source": SOURCE_SHA, "original_preparation": str(old),
            "status": "NOT_DISPATCHED_ROOT_SELECTION_REQUIRED", "actual_scientific_run": False,
            "runtime_change": "Lookup927 plus process-local per-canonical-file ALL connection I/O serialization, SQLite30s/FULL/schema/accounting unchanged",
            "full_algorithm_data_H0_model_workers_caps_unchanged": True,
            "not_an_independent_second_seed_or_warm_start": True,
            "original_FAILED_profile_and_ledger_preserved_byte_identical": True,
            "original_failed_status": handle["status"], "original_failed_PID": handle["pid"],
            "original_failed_diagnostic": handle.get("diagnostic"),
            "full_replacement_from_original_H0_NOT_warm_start_or_snapshot_splice": True,
            "old_paid_history_and_global_external_budget_authority_retained": True,
            "not_original_lock_compatible_or_native_terminal_journal_resume": True}
        module.prepare(new, dataset, settings, sources=[source, helper], packages=installed_packages(), provenance=provenance)
        _, new_data, _, prepared = module.load(new)
        assert len(prepared) == 1 and content_hash(new_data) == content_hash(dataset)
        entry, recipe, lock = prepared[0]
        assert normalized(recipe) == normalized(old_recipe)
        assert recipe.subject.initial_artifact == old_recipe.subject.initial_artifact
        assert recipe.budget == old_recipe.budget
        assert recipe.execution.max_workers == recipe.method.options["max_workers"] == recipe.budget.max_concurrency == 8
        assert all(sha(Path(p)) == h for p, h in protected.items())
        assert not (new / "runs").exists()
        assert not (ROOT / "root-acceptances" / (label + "-release-accepted.json")).exists()
        assert not (ROOT / "handles" / (label + ".json")).exists()
        files = [new / "study.json", new / "tasks.json", new / "provenance.json", guard_path,
            new / "recipes" / (entry.name + ".json"), new / "locks" / (entry.name + ".json")]
        profiles.append({"label": label, "original_failed_label": old_label,
            "root": str(new), "status": "prepared_runtime_candidate_NOT_DISPATCHED_NOT_SELECTED",
            "source_sha": source.revision, "config_hash": recipe.config_hash, "lock_hash": content_hash(lock),
            "dataset_hash": content_hash(dataset), "H0": recipe.subject.initial_artifact.to_dict(),
            "method_options": recipe.to_dict()["method"]["options"], "budget": recipe.budget.to_dict(),
            "normalized_recipe_exactly_matches_original_except_scoped_paths": True,
            "protected_original_hashes": protected,
            "files": [{"path": str(p), "sha256": sha(p)} for p in files]})
    assert source_pin("proteus", SOURCE) == source and source_pin("proteus", ROOT / "sources/api") == original
    once(AUDIT, {"status": "prepared_THREE_full_FAILED_condition_replacements_NOT_DISPATCHED",
        "created_UTC": datetime.now(timezone.utc).isoformat(), "source": source.to_dict(),
        "base_sha": BASE_SHA, "helper_sha256": sha(__file__), "changed_files": changed,
        "patch_sha256": hashlib.sha256(patch).hexdigest(), "tests_passed": 100, "tests_skipped": 2,
        "unpaid_50_task_8_worker_recovery_gate": {"path": str(gate_path), "sha256": sha(gate_path)},
        "profiles": profiles, "original_FAILED_attempts_ledger_and_paid_history_retained": True,
        "original_terminal_journals_NOT_claimed_resumed_or_repaired": True,
        "source_preparation": {"path": str(source_receipt_path), "sha256": sha(source_receipt_path)},
        "actual30s_real_contention_gate": {"path": str(contention_path), "sha256": sha(contention_path)},
        "lock_structure_tests": {"path": str(structure_path), "sha256": sha(structure_path), "passed": 5},
        "actual8worker_workflows_gate": {"path": str(workflow_path), "sha256": sha(workflow_path)},
        "paid_requests_admission_changes_or_token_access": 0, "launch_function_absent": True,
        "source_venv_live_job_recipe_lock_or_ledger_hot_edits": False,
        "root_review_and_separate_exact_paid_admission_required": True,
        "remaining_external_process_or_raw_SQLite_reader_contention_risk": True})
    print(json.dumps({"status": "prepared_THREE_full_FAILED_condition_replacements_NOT_DISPATCHED",
        "audit": str(AUDIT), "sha256": sha(AUDIT), "profiles": len(profiles)}), flush=True)


if __name__ == "__main__":
    main()
