"""Verify the publication and its derived scores; no candidate or model execution."""

import csv
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load(name):
    return json.loads((BASE / name).read_text())


def close(actual, expected):
    assert math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12)


def main():
    manifest = load("MANIFEST.json")
    for name, expected in manifest["files"].items():
        relative = Path(name)
        assert not relative.is_absolute() and ".." not in relative.parts
        assert hashlib.sha256((BASE / relative).read_bytes()).hexdigest() == expected, name
    data = load("data/experiment.json")
    provenance = load("data/provenance.json")
    assert data["status"] == "complete" and data["hidden_case_count"] == 45
    assert data["visible_case_count"] == 12
    assert data["native_selection_uses_hidden"] is False
    assert data["missing_score_imputation"] is False
    assert data["pricing"]["actual_billing_cost_claimed"] is False
    assert data["pricing"]["unknown_usage_imputed"] is False
    for goal, expected in provenance["delivered_goal_sha256"].items():
        assert (
            hashlib.sha256((BASE / "goals" / f"{goal}-GOAL.md").read_bytes()).hexdigest()
            == expected
        )
    cells = {(c["goal_id"], c["method"]): c for c in data["conditions"]}
    assert len(cells) == 6
    for cell in cells.values():
        points = cell["points"]
        assert points[0]["episode"] == 0
        assert all(a["episode"] < b["episode"] for a, b in zip(points, points[1:]))
        for field in ["cumulative_tool_calls", "cumulative_recorded_cost_usd"]:
            assert all(a[field] <= b[field] + 1e-12 for a, b in zip(points, points[1:]))
        for point in points:
            if point.get("submitted") is False:
                assert point["all_45"] is None and point["visible"] is None
            for field in ["all_45", "visible"]:
                assert point[field] is None or 0 <= point[field] <= 1
    audits = load("data/hidden-audit-metadata.json")["reports"]
    assert len(audits) == data["new_eligible_hidden_reports"] == 15
    expected_ids = {
        f"{domain}-{i:02d}"
        for domain, count in data["hidden_domain_case_counts"].items()
        for i in range(1, count + 1)
    }
    for audit in audits:
        assert audit["complete_pack_hashes_verified"] and audit["counts"]["unresolved"] == 0
        assert len(audit["case_ids"]) == 45 and set(audit["case_ids"]) == expected_ids
        assert set(audit["fresh_lanes_verified"]) == {
            "api",
            "safety",
            "browser",
            "headless",
            "configuration",
            "acp",
        }
        assert audit["new_model_calls"] == 0 and audit["source_modified"] is False
        assert audit["hidden_feedback"] is False
        exact = sum(Fraction(d["score_exact"]) for d in audit["dimensions"].values()) / 6
        assert exact == Fraction(audit["all_45_exact"])
        close(float(exact), audit["all_45"])
        goal = audit["report_id"].split("-e")[0]
        point = next(p for p in cells[goal, "meta"]["points"] if p["episode"] == audit["episode"])
        close(point["all_45"], float(exact))
        assert point["commit"] == audit["commit"]
        assert audit["criterion_kernel_sha256"] == provenance["criterion_kernel_sha256"]
    cost_audits = {a["goal"]: a for a in load("data/cost-audit-metadata.json")["audits"]}
    for row in data["summary"]:
        goal = row["goal"]
        audit, meta, pro = cost_audits[goal], cells[goal, "meta"], cells[goal, "proteus"]
        assert audit["recorded_nanodollars"] == round(row["meta_cost_usd"] * 1e9)
        assert audit["target_nanodollars"] == round(row["proteus_cost_usd"] * 1e9)
        crossing = audit["cost_crossing"]
        assert crossing["before_nanodollars"] < audit["target_nanodollars"]
        assert crossing["after_nanodollars"] == audit["recorded_nanodollars"]
        assert crossing["after_nanodollars"] >= audit["target_nanodollars"]
        assert crossing["response_nanodollars"] == (
            crossing["after_nanodollars"] - crossing["before_nanodollars"]
        )
        assert audit["new_unknown_generation"] == [] and audit["model_calls"] == 0
        assert audit["tool_calls"] == row["meta_tool_calls"]
        assert meta["points"][-1]["cumulative_tool_calls"] == row["meta_tool_calls"]
        close(meta["points"][-1]["cumulative_recorded_cost_usd"], row["meta_cost_usd"])
        close(pro["points"][-1]["cumulative_recorded_cost_usd"], row["proteus_cost_usd"])
        latest = next(p for p in reversed(meta["points"]) if p["all_45"] is not None)
        assert latest["episode"] == row["meta_last_measured_episode"]
        close(latest["all_45"], row["meta_last_measured_hidden"])
        chosen = meta["native_selected_endpoint"]
        assert chosen["episode"] == row["meta_native_selected_episode"]
        close(chosen["all_45"], row["meta_native_selected_hidden"])
        assert audit["unsubmitted_cutoff_episode"] == meta["points"][-1]["episode"]
    with (BASE / "data/episode-points.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == sum(len(c["points"]) for c in cells.values()) == 84
    by_id = {(r["goal_id"], r["method"], int(r["episode"])): r for r in rows}
    for cell in cells.values():
        for point in cell["points"]:
            row = by_id[cell["goal_id"], cell["method"], point["episode"]]
            for field in ["all_45", "visible", "cumulative_recorded_cost_usd"]:
                if point[field] is None:
                    assert row[field] == ""
                else:
                    close(float(row[field]), point[field])
    print("Verified publication hashes, goals, 84 points, 15 reports, scores and cost cutoffs.")


if __name__ == "__main__":
    main()
