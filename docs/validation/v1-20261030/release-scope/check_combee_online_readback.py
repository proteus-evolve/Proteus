"""Check unchanged output and rejection of catalog-only metric drift, using local receipts."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


HERE = Path(__file__).resolve().parent
FIELDS = ("baseline_correct", "optimized_correct", "tasks", "API_upper_USD_not_invoice")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    root = args.evidence_root.resolve()
    catalog = root / "combee-formula-review-20261026/review-results.json"
    original = catalog.read_bytes()
    data = json.loads(original)
    online = [i for i, row in enumerate(data["rows"]) if row["profile"].endswith("online")]
    assert len(online) == 4
    receipt_hashes = {
        Path(data["rows"][i]["receipt"]).name: data["rows"][i]["receipt_sha256"] for i in online
    }
    cases = []
    with TemporaryDirectory(prefix="proteus-online-readback-check-") as directory:
        temp = Path(directory)
        (temp / "aws-access-20261020").symlink_to(root / "aws-access-20261020", target_is_directory=True)
        copy = temp / "combee-formula-review-20261026"
        copy.mkdir()
        (copy / "scientific-source-hash-comparison.json").symlink_to(
            catalog.parent / "scientific-source-hash-comparison.json")
        changed_catalog = copy / "review-results.json"
        changed_catalog.write_bytes(original)

        def invoke(output: Path):
            return subprocess.run(
                [sys.executable, str(HERE / "readback_combee_online.py"),
                 "--evidence-root", str(temp), "--out", str(output)],
                text=True, capture_output=True, check=False, timeout=30)

        control = temp / "control.json"
        completed = invoke(control)
        assert completed.returncode == 0, completed.stderr
        assert control.read_bytes() == (HERE / "combee-online-evidence.json").read_bytes()
        for index in online:
            for field in FIELDS:
                modified = deepcopy(data)
                modified["rows"][index][field] = 0
                changed_catalog.write_text(json.dumps(modified) + "\n")
                output = temp / f"drift-{index}-{field}.json"
                completed = invoke(output)
                assert completed.returncode != 0, (index, field, "drift accepted")
                assert "Catalog metric disagrees with pinned receipt:" in completed.stderr
                assert completed.stderr.rstrip().endswith(f": {field}"), completed.stderr
                assert not output.exists(), "Rejected input must not publish an output"
                cases.append({"profile": data["rows"][index]["profile"], "changed_field": field,
                              "exit_code": completed.returncode, "output_created": False})
        control_sha = hashlib.sha256(control.read_bytes()).hexdigest()
    assert catalog.read_bytes() == original
    for name, expected in receipt_hashes.items():
        assert hashlib.sha256((root / "aws-access-20261020" / name).read_bytes()).hexdigest() == expected
    result = {
        "control_output_byte_identical": True, "control_output_sha256": control_sha,
        "catalog_only_drift_cases_rejected": len(cases), "cases": cases,
        "original_catalog_and_receipts_unchanged": True, "new_provider_calls": 0,
        "aws_operations": 0,
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: result[k] for k in (
        "control_output_byte_identical", "catalog_only_drift_cases_rejected",
        "original_catalog_and_receipts_unchanged", "new_provider_calls")}))


if __name__ == "__main__":
    main()
