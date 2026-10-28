"""NEW independent full fresh-H0 preparations for the three FAILED927 studies.

No model calls, credential reads, admission changes, launch or journal repair.
The first two are review candidates; online-dynamic stays separately held even
though its preserved inner journals now explicitly record SQLite lock errors.
Never edits the original THREE212 preparations, wrappers, bundles or markers.
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

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "sources/api-ledger-serialized-io-v1"
LOOKUP = ROOT / "sources/api-ledger-lookup-v1"
BASE_SHA = "927afb593cb8e6ae88f163ecc83da5c196c4d948"
SOURCE_SHA = "21205d1dcdcff2a39dba31998629d22fd1ae94f9"
H0 = "sha256:31b9fe5c8d690797aee55588119ac476773ac7e8535fbc1ee4520918cc1bae27"
AUDIT = ROOT / "combee-lookup-serialized-io-full-preparations-v1.json"
REQUIRED = (("online", "combee_fixed"), ("offline", "combee_dynamic"), ("online", "combee_dynamic"))
sys.path.insert(0, str(SOURCE))
from proteus.artifacts.store import ArtifactStore
from proteus.core.serialization import content_hash
from proteus.examples import combee_formula as module
from proteus.recipes.cli import installed_packages, source_pin
from proteus.recipes.manifest import SourcePin


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ref(path):
    path = Path(path)
    return {"path": str(path), "sha256": sha(path)}


def once(path, value):
    encoded = json.dumps(value, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as out:
        out.write(encoded)


def normalized(recipe):
    value = recipe.to_dict()
    for actor in value["actors"].values():
        actor["runtime"]["options"]["base_url"] = "<SCOPED_ROUTE>"
        actor["runtime"]["options"]["dispatch_guard_path"] = "<SCOPED_GUARD>"
    for evaluation in value["evaluations"]:
        evaluation["tasks"]["suite"]["options"]["path"] = "<STUDY_TASKS>"
    return value


def frozen_collateral():
    names = ("prepare_combee_serialized_io_replacements_v1.py",
        "combee-serialized-io-prepared-full-replacements-v1.json",
        "api-ledger-serialized-io-review-v1.json", "launch_combee_serialized_io_once_v1.py",
        "launch_combee_serialized_io_once_v2.py", "combee-serialized-io-release-review-bundle-v1.json",
        "combee-serialized-io-release-review-bundle-v2.json")
    files = [ROOT / name for name in names]
    for part in ("online-naive_parallel", "offline-naive_parallel", "offline-combee_fixed"):
        label = "linux-combee-formula-" + part + "-v5-ledger-io"
        study = ROOT / "formal" / label
        files += [study / name for name in ("study.json", "tasks.json", "provenance.json")]
        files += list(study.glob("recipes/*.json")) + list(study.glob("locks/*.json"))
        files.append(ROOT / "guards" / (label + ".json"))
        files += list((ROOT / "root-acceptances").glob(label + "-*.json"))
    # Existing scientific source files only, never hot ledgers/handles.
    files += [SOURCE / "proteus/core/ledger.py", LOOKUP / "proteus/core/ledger.py"]
    assert all(path.is_file() for path in files)
    return {str(path): sha(path) for path in files}


def main():
    os.umask(0o077)
    assert not AUDIT.exists()
    source, lookup = source_pin("proteus", SOURCE), source_pin("proteus", LOOKUP)
    assert source.revision == SOURCE_SHA and source.dirty_patch_hash is None
    assert lookup.revision == BASE_SHA and lookup.dirty_patch_hash is None
    changed = subprocess.check_output(["git", "-C", str(SOURCE), "diff", "--name-only", BASE_SHA, SOURCE_SHA], text=True).splitlines()
    assert changed == ["proteus/core/ledger.py"]
    collateral = frozen_collateral()
    review_path = ROOT / "api-ledger-serialized-io-review-v1.json"
    review = json.loads(review_path.read_text())
    assert review["source"]["revision"] == SOURCE_SHA
    for item in review["tests"].values():
        assert sha(item["path"]) == item["sha256"]
    assert sum(item["passed"] for item in review["tests"].values()) == 105
    assert sum(item["skipped"] for item in review["tests"].values()) == 2
    gates = {}
    for name in ("api-ledger-serialized-io-source-v1.json", "qualified-ledger-io-real-contention-v1.json",
        "qualified-api-serialized-io-settled-trial-recovery-v1.json", "qualified-combee-serialized-io-workflows-v1.json"):
        path = ROOT / name
        assert sha(path) == review["gates"][name]["sha256"]
        gates[name] = ref(path)
    targeted_path = ROOT / "qualified-combee-lookup-replacement-targets-v3.json"
    targeted = json.loads(targeted_path.read_text())
    assert targeted["status"] == "passed_targeted212_three_lookup_replacement_tiny_full_workflows_and_PBL"
    assert targeted["source"]["revision"] == SOURCE_SHA
    assert targeted["qualifier_sha256"] == sha(ROOT / "qualify_combee_lookup_replacement_targets_v3.py")
    assert targeted["physical_interwindow_full_learning_curation_postevaluation_before_next_prediction_checked"]
    assert {item["label"] for item in targeted["profiles"]} == {f"fixture-{mode}-{variant}" for mode, variant in REQUIRED}
    for item in targeted["profiles"]:
        assert item["same_run_actual_HTTP_peak"] == item["ledger_reserved_slot_peak"] == 8
        assert item["workflow_child_role_peaks"] == {"generator": 8, "reflector": 8}
        assert item["completed_resume_added_HTTP_calls_or_events"] == 0
        assert all(check["actual_succeeded_postcuration_rollouts"] == check["tasks"] for check in item["postcuration_prediction_checks"])
        if "online-" in item["label"]:
            assert len(item["online_PBL_checks"]) == 2
            assert item["online_PBL_checks"][1]["previous_window_complete_physical_order"]["all_previous_window_managed_actor_settlements_before_next_prediction_cohort_started"]
    gates[targeted_path.name] = ref(targeted_path)
    boundary = ROOT / "lookup-two-failures-branch-boundaries-owner-v1.json"
    third = ROOT / "lookup-online-dynamic-original-workflow-errors-owner-v1.json"
    third_data = json.loads(third.read_text())
    assert third_data["has_recorded_inner_database_locked_error"]
    assert len(third_data["affected_workflow_branches"]) == 3
    assert all(item["cached_workflow_original_diagnostic"] == "OperationalError: database is locked" for item in third_data["affected_workflow_branches"])
    helper = SourcePin(name="unpaid-lookup-failure-full212-preparation-v1",
        repository="local-content-addressed:" + Path(__file__).name, revision=sha(__file__))
    profiles = []
    for mode, variant in REQUIRED:
        old_label = f"linux-combee-formula-{mode}-{variant}-v4-lookup"
        label = f"linux-combee-formula-{mode}-{variant}-v6-ledger-io"
        old, new = ROOT / "formal" / old_label, ROOT / "formal" / label
        handle_path = ROOT / "handles" / (old_label + ".json")
        handle = json.loads(handle_path.read_text())
        assert handle["status"] == "failed" and not Path(f"/proc/{handle['pid']}").exists()
        assert not new.exists() and not (ROOT / "handles" / (label + ".json")).exists()
        study, dataset, _, old_runs = module.load(old, check_runtime=False)
        assert len(old_runs) == 1
        old_entry, old_recipe, old_lock = old_runs[0]
        assert any(pin.revision == BASE_SHA and pin.dirty_patch_hash is None for pin in old_lock.sources)
        old_run = old / "runs" / old_entry.name
        originals = [old / name for name in ("study.json", "tasks.json", "provenance.json")]
        originals += list(old.glob("recipes/*.json")) + list(old.glob("locks/*.json"))
        originals += [handle_path, old_run / "manifest.json", old_run / "private/ledger.sqlite3"]
        protected = {str(path): sha(path) for path in originals}
        assert Counter(row.task.split for row in dataset.records) == {"train": 500, "validation": 300, "test": 200}
        assert study.settings.seeds == (0,) and study.settings.modes == (mode,) and study.settings.variants == (variant,)
        assert study.settings.max_num_rounds == 3 and study.settings.batch_size == 50 and study.settings.eval_steps == 500
        assert study.settings.max_workers == 8 and study.settings.online_eval_frequency == 15
        assert old_recipe.method.options["num_epochs"] == 1
        assert old_recipe.subject.initial_artifact.content_hash == H0
        if variant == "combee_dynamic":
            assert old_recipe.method.options["dynamic_batch"]["max_batch_size"] == 100
        actors = {}
        guard = ROOT / "guards" / (label + ".json")
        once(guard, {"version": 2, "admission_url": "http://127.0.0.1:62125/admission/" + label, "token_env": "PROTEUS_RELAY_TOKEN"})
        for role, actor in old_recipe.actors.items():
            assert actor.model == "deepseek-flash" and actor.model_settings["max_tokens"] == 4096
            assert actor.model_settings["temperature"] == 0 and actor.budget.max_concurrency == 8
            options = dict(actor.runtime.options)
            options.update(base_url="http://127.0.0.1:62125/" + label + "/v1", dispatch_guard_path=str(guard))
            actors[role] = replace(actor, runtime=replace(actor.runtime, options=options))
        settings = replace(study.settings, **actors)
        provenance = json.loads((old / "provenance.json").read_text())
        provenance["Linux_profile"] = {**provenance["Linux_profile"], "id": label, "source": source.to_dict()}
        held = mode == "online" and variant == "combee_dynamic"
        provenance["independent_failed927_FULL_replacement_preparation"] = {
            "source_base": BASE_SHA, "source": SOURCE_SHA, "original_preparation": str(old),
            "status": "SEPARATELY_HELD_ROOT_REVIEW_REQUIRED" if held else "NOT_DISPATCHED_ROOT_SELECTION_REQUIRED",
            "actual_scientific_run": False, "same_full_dataset_seed0_epoch1_original_H0_recipe_and_8way_caps": True,
            "original_failed_PID": handle["pid"], "original_failed_diagnostic": handle.get("diagnostic"),
            "original_FAILED_history_paid_replies_NULL_unknowns_and_ledger_preserved": True,
            "fresh_original_H0_not_terminal_resume_or_warm_start_or_splice": True,
            "no_new_paid_calibration_or_admission_changes": True,
            "runtime_change": "927 to212 process-local canonical-ledger-path all-connection I/O serialization only",
            "no_universal_interprocess_contention_or_success_guarantee": True}
        module.prepare(new, dataset, settings, sources=[source, helper], packages=installed_packages(), provenance=provenance)
        _, new_data, _, runs = module.load(new)
        assert len(runs) == 1
        entry, recipe, lock = runs[0]
        assert content_hash(new_data) == content_hash(dataset) and normalized(recipe) == normalized(old_recipe)
        assert recipe.subject.initial_artifact == old_recipe.subject.initial_artifact and recipe.budget == old_recipe.budget
        ArtifactStore(new / "artifact-store", module.registry()).files(recipe.subject.initial_artifact)
        assert recipe.execution.max_workers == recipe.method.options["max_workers"] == recipe.budget.max_concurrency == 8
        assert all(actor.budget.max_concurrency == 8 for actor in recipe.actors.values())
        assert all(sha(path) == digest for path, digest in protected.items())
        assert not (new / "runs").exists()
        files = [new / name for name in ("study.json", "tasks.json", "provenance.json")]
        files += [guard, new / "recipes" / (entry.name + ".json"), new / "locks" / (entry.name + ".json")]
        profiles.append({"label": label, "original_failed_label": old_label, "root": str(new),
            "status": "prepared_SEPARATE_HOLD_NOT_DISPATCHED" if held else "prepared_NOT_DISPATCHED_NOT_SELECTED",
            "separate_third_condition_hold": held, "source_sha": SOURCE_SHA,
            "config_hash": recipe.config_hash, "lock_hash": content_hash(lock), "dataset_hash": content_hash(dataset),
            "dataset_counts": dict(Counter(row.task.split for row in dataset.records)),
            "learn_samples": 200 if mode == "online" else 500, "H0": recipe.subject.initial_artifact.to_dict(),
            "method_options": recipe.to_dict()["method"]["options"], "budget": recipe.budget.to_dict(),
            "normalized_recipe_exactly_matches_failed927_except_scoped_paths": True,
            "actual_new_store_H0_manifest_and_blobs_rehashed": True,
            "protected_original_hashes": protected, "files": [ref(path) for path in files]})
    assert all(sha(path) == digest for path, digest in collateral.items())
    assert source_pin("proteus", SOURCE) == source and source_pin("proteus", LOOKUP) == lookup
    once(AUDIT, {"status": "NEW_independent_three_FULL_failed927_preparations_NOT_DISPATCHED_third_separately_HELD",
        "created_UTC": datetime.now(timezone.utc).isoformat(), "helper_sha256": sha(__file__),
        "source": source.to_dict(), "source_base": BASE_SHA, "changed_files": changed,
        "existing_105_passed_2_optional_skipped_tests_referenced_NOT_rerun": ref(review_path),
        "reference_gate_files": gates, "exact_failure_boundary_evidence": [ref(boundary), ref(third)],
        "profiles": profiles, "frozen_existing_THREE212_preparations_wrappers_bundles_markers_and_static_profiles": collateral,
        "existing_scientific_sources_and_active_profiles_not_modified": True,
        "original_failure_and_paid_NULL_unknown_history_preserved_not_rewritten": True,
        "token_reads_paid_calls_admission_changes_or_scientific_launches": 0,
        "root_review_separate_exact_marker_and_actual_slot_release_required": True,
        "new_online_fixed200_priority_then_offline_dynamic500_third_kept_separate": True})
    print(json.dumps({"status": "prepared_NOT_DISPATCHED", "audit": str(AUDIT), "sha256": sha(AUDIT),
        "profiles": [{key: row[key] for key in ("label", "config_hash", "lock_hash", "status")} for row in profiles]}), flush=True)


if __name__ == "__main__":
    main()
