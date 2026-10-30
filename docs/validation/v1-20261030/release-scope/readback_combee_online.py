"""Read existing online receipts locally; never execute historical auditor scripts."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[4]
RECEIPTS = {
    "Combee-online-ACE-final-independent-acceptance.json":
        "15c6eba79112bfe0d7b71e8385254e33e3112b8c11201a02f3f4642a455b6b95",
    "Combee-online-naive-212-final-independent-acceptance.json":
        "994dfb1c68d367cea691648d8a54001c0c320883b434605b33b4b06d0b9c2b51",
    "Combee-online-fixed-v6-final-independent-acceptance.json":
        "d07bcdc4d69d54e8bc58f17ecee8b62e5156129723d7f8b10e08a55c9bb0a778",
    "Combee-online-dynamic-v6-final-independent-acceptance.json":
        "1166f85dcb0bfe0bc9ac4e2643ce8cf3a363bf258bec7dc73342ed17850804ac",
}
AUDITORS = {
    "audit_completed_combee_online_ace.py":
        "c7c788fcf72451bba373e1d27023c759092dfdf273c390dd6164518912936a97",
    "audit_completed_combee_online_naive_212.py":
        "986d36cb2167b23e3c41349c75a86b1f1dd2016e011bc4ddcad936064f23ddf7",
    "audit_completed_combee_online_v6.py":
        "4b34a8ba8b89d5a833aad243d518ccf39aaedbcbee814e7e64cc686d57fb98ea",
}


def read(path: Path):
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    root = args.evidence_root
    receipt_root = root / "aws-access-20261020"
    catalog_path = root / "combee-formula-review-20261026/review-results.json"
    rows = []
    for observation in read(catalog_path)["rows"]:
        if not observation["profile"].endswith("online"):
            continue
        name = Path(observation["receipt"]).name
        path = receipt_root / name
        assert sha(path) == observation["receipt_sha256"] == RECEIPTS[name]
        receipt = read(path)
        profile = REPO / "recipes/experiments/single-seed-20261020/profiles" / receipt["label"]
        binding = read(profile / "provenance.json")["bindings"][0]
        assert (binding["config_hash"], binding["lock_hash"]) == (
            receipt["config_hash"], receipt["lock_hash"])
        recipe = read(profile / binding["recipe"])
        assert len(receipt["physical_http_pairs"]) == sum(receipt["roles"].values()) == observation["physical_calls"]
        timing = "wall_seconds" if "wall_seconds" in receipt else "engine_wall_seconds"
        source = receipt.get("source_revision") or receipt["source"]["revision"]
        assert source == binding["sources"][0]["revision"]
        row = {k: observation[k] for k in (
            "profile", "baseline_correct", "optimized_correct", "tasks", "physical_calls",
            "API_upper_USD_not_invoice")}
        row.update({
            "receipt": name, "receipt_sha256": sha(path),
            "label": receipt["label"], "report_sha256": receipt["report_sha256"],
            "config_hash": receipt["config_hash"], "lock_hash": receipt["lock_hash"],
            "source_revision": source, "study_elapsed_seconds": receipt[timing],
            "receipt_timing_field": timing, "canonical_cost_USD": None,
            "quality_scope": "200 pre-learning stream predictions",
            "seed": recipe["seed"], "H0": recipe["subject"]["initial_artifact"]["content_hash"],
            "method_options": recipe["method"]["options"], "budget": recipe["budget"],
            "actor_settings": {role: {k: actor[k] for k in ("model", "model_settings", "session")}
                               for role, actor in recipe["actors"].items()},
            "elapsed_provenance": "historical receipt and auditor source; full original report not reopened",
        })
        if name == "Combee-online-ACE-final-independent-acceptance.json":
            report = receipt_root / "Combee-online-ACE-completed-formal-report.json"
            assert sha(report) == receipt["report_sha256"]
            elapsed = read(report)["runs"][0]["elapsed"]
            assert elapsed["wall_seconds"] == row["study_elapsed_seconds"]
            assert elapsed["interrupted"] is False
            row["formal_report"] = {"name": report.name, "sha256": sha(report), "elapsed": elapsed}
            row["elapsed_provenance"] = "full original local report hash matched, plus historical auditor source"
        rows.append(row)
    assert len(rows) == 4 and {r["receipt"] for r in rows} == set(RECEIPTS)
    assert len({r["H0"] for r in rows}) == 1
    assert all(r["seed"] == 0 and r["tasks"] == 200 and r["actor_settings"] == rows[0]["actor_settings"]
               for r in rows)

    auditors = []
    for name, expected in AUDITORS.items():
        path = receipt_root / name
        assert sha(path) == expected
        tree = ast.parse(path.read_text())
        remote = next(ast.literal_eval(n.value) for n in tree.body
                      if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REMOTE"
                                                         for t in n.targets))
        lines = [i for i, line in enumerate(remote.splitlines(), 1) if "r['elapsed']['wall_seconds']" in line]
        assert lines
        auditors.append({"name": name, "sha256": expected, "embedded_source_lines": lines,
                         "extracts": "r['elapsed']['wall_seconds']", "executed": False})
    witness = root / "combee-formula-review-20261026/scientific-source-hash-comparison.json"
    assert sha(witness) == "e2575c35aa879be02bf0fc8237015fe861294261452bb2106d5cf95c754bded7"
    sources = read(witness)
    assert len(sources) == 2 and list(sources.values())[0] == list(sources.values())[1]
    output = {
        "schema_version": 1,
        "reviewed_merge": "58910e1181d4e7fc0b4a91d9e6a5317f71dd6565",
        "historical_catalog": {"name": catalog_path.name, "sha256": sha(catalog_path)},
        "rows": rows, "auditors": auditors,
        "historical_method_file_hash_witness": {"name": witness.name, "sha256": sha(witness),
            "four_files_identical": True, "old_git_objects_reverified": False,
            "interpretation": "Method/operator source witness only; not identical runtime I/O or performance."},
        "acceptance": "Online quality/API-upper/study-elapsed observations available; R04 fixed-trace and declared-comparison acceptance require separate review.",
        "limits": [
            "Single seed; shared host; baseline and parallel arms use different runtime cores.",
            "Pre-learning stream accuracy, not final-playbook held-out accuracy.",
            "API upper bounds, not invoices or known canonical USD.",
            "Elapsed includes study execution/persistence, not actor-clock sums or paper training latency.",
            "Only ACE full original report reopened; other three values rechecked from historical receipts and auditor sources.",
            "Offline unknown continuous time does not imply missing online elapsed or require paid reruns.",
            "No isolated speedup, significance, paper parity or current-runtime benchmark claim.",
        ],
        "new_provider_calls": 0, "aws_operations": 0, "frozen_records_modified": False,
    }
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"rows": len(rows), "receipt_hashes_matched": True,
                      "ACE_original_report_matched": True, "new_provider_calls": 0}))


if __name__ == "__main__":
    main()
