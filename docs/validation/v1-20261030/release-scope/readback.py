"""Read local acceptance receipts; do not execute candidates or contact providers.

Run with the repository's dev environment. Original local receipts are required;
they are intentionally not embedded here. The output contains hashes and bounded
aggregates, not private request/response bodies or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
BASE = "3518095dab1e6223bb40d95237e11a3d8e11a0c3"
CORAL_SOURCE = "7ca7742d18f267b5a2805dad59979e328d3a8f36"
RECEIPT_HASHES = {
    "first": "67838a5d04821984adf7d2e412d26890cbb00db62fc87003583ae0274dc63bf4",
    "second": "4f2d3b35bafc2cd9e328c8234a3c622e6003cfdc233afedf876ad7fd091d57b2",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--relay", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    from proteus.api import v1

    review = json.loads(args.review.read_text())
    assert review["source"]["head"] == BASE
    assert git("rev-parse", "HEAD:proteus") == git("rev-parse", BASE + ":proteus")
    assert not review["new_confirmed_engineering_findings"]
    names = sorted(v1.__all__)
    assert len(names) == len(set(names)) == 100
    assert all(hasattr(v1, name) for name in names)

    archive = REPO / "recipes/experiments/single-seed-20261020"
    index = json.loads((archive / "index.json").read_text())
    results = json.loads((archive / "results.json").read_text())
    arms = []
    for arm in ("first", "second"):
        path = args.receipts / f"CORAL-v5_1-{arm}-arm-final-independent-acceptance.json"
        assert digest(path) == RECEIPT_HASHES[arm]
        data = json.loads(path.read_text())
        assert data["source_revision"] == CORAL_SOURCE
        assert data["recomputed_official_cooperation_report_equals_saved"]
        assert data["physical_API_calls"] == 20
        workers = [{k: worker[k] for k in (
            "worker_id", "generations", "restarts", "statuses", "continuity",
            "formal_submissions", "knowledge_publications",
        )} for worker in data["cooperation"]["workers"]]
        assert sum(w["formal_submissions"] for w in workers) == 20
        arms.append({
            "receipt": path.name, "sha256": digest(path),
            "source_revision": data["source_revision"],
            "config_hash": data["config_hash"], "lock_hash": data["lock_hash"],
            "physical_API_calls": data["physical_API_calls"],
            "proposal_status_counts": data["proposal_status_counts"],
            "workers": workers,
            "known_API_upper_USD_not_invoice": data["known_API_cost_upper_USD_not_invoice"],
            "cooperation_report_matches_saved_receipt": True,
            "full_matrix_accepted_in_original": data["full_matrix_accepted"],
        })
        if arm == "second":
            bindings = data["all20_paid_source_code_feedback_bindings"]
            assert len(bindings) == 20
            assert all(b["measured_feedback_count_in_actual_prompt"] == min(b["index"], 5)
                       for b in bindings)

    own_path = args.receipts / "CORAL-v5_1-current-visible-feedback-and-HTTP-independent-audit.json"
    own = json.loads(own_path.read_text())
    assert own["source_revision"] == CORAL_SOURCE
    assert len(own["paid_response_pairings"]) == 20
    assert all(b["measured_feedback_count"] == min(b["index"], 5)
               for b in own["paid_response_pairings"])

    connection = sqlite3.connect(args.relay.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        columns = [r[1] for r in connection.execute("pragma table_info(dispatches)")]
    finally:
        connection.close()
    assert columns == ["request_id", "arm", "body_sha256", "started", "finished",
                       "status", "response_sha256", "response_zlib", "error_type"]

    combee_keys = ("display_name", "baseline_correct", "selected_correct",
                  "physical_API_calls", "known_API_upper_USD_not_invoice",
                  "native_canonical_cost_USD", "known_controller_seconds_across_attempts",
                  "continuous_run_seconds", "source_revision")
    combee = [{k: row[k] for k in combee_keys}
              for name, row in results["additional_accepted_report_metrics"].items()
              if "combee" in name]
    assert len(combee) == 4 and all(r["continuous_run_seconds"] is None for r in combee)
    output = {
        "schema_version": 1,
        "scope": "Local receipt readback; no new remote-run, provider, or candidate execution",
        "source": {"reviewed_merge": BASE, "runtime_tree": git("rev-parse", BASE + ":proteus"),
                   "reviewed_whole_tree": git("rev-parse", BASE + "^{tree}"),
                   "review_receipt_sha256": digest(args.review)},
        "review": {"new_confirmed_engineering_findings": [],
                   "publication_approved": False},
        "api": {"version": v1.API_VERSION, "exports": names,
                "count": len(names), "missing": [], "duplicates": 0,
                "freeze_status": "proposed, pending maintainer release acceptance"},
        "frozen_archive": {"profiles": index["profile_count"], "recipes": index["recipe_count"],
                           "index_sha256": digest(archive / "index.json"),
                           "results_sha256": digest(archive / "results.json")},
        "coral": {
            "arms": arms,
            "own_feedback_first_arm_receipt": {"name": own_path.name, "sha256": digest(own_path),
                                               "matched_request_receipts": 20},
            "own_feedback_second_arm_request_receipts": 20,
            "own_feedback_window": "min(proposal index, 5); verified within archived audit receipts",
            "relay_database": {"name": args.relay.name, "access": "mode=ro",
                               "dispatch_columns": columns, "request_bodies_stored": False},
            "actual_cross_worker_prompt_use": "not established from inspected local receipts",
            "applied_interventions": "not established from inspected local receipts",
            "live_crash_restart": "not established; reported restart counts are zero",
            "caution": "Missing local evidence does not prove behavior never occurred. Publication, own feedback and pause/fresh-fallback remain accepted within their recorded scope.",
        },
        "combee_offline_observations": combee,
        "new_paid_calls": 0, "aws_operations": 0,
    }
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(args.out), "api_exports": len(names),
                      "coral_arms": len(arms), "physical_calls_previously_recorded": 40,
                      "new_paid_calls": 0}))


if __name__ == "__main__":
    main()
