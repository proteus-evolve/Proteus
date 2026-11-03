"""Read pinned Git objects and validate paper mappings; never run experiments."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "evidence/source-manifest.json").read_text())
    revision = manifest["documentation_snapshot"]
    sources = {}
    for item in manifest["sources"]:
        data = subprocess.check_output(
            ["git", "-C", str(args.repo), "show", f"{revision}:{item['path']}"]
        )
        assert hashlib.sha256(data).hexdigest() == item["sha256"], item["path"]
        sources[item["path"]] = data
    cells = json.loads((root / "evidence/table-cells.json").read_text())
    for cell in cells:
        value = json.loads(sources[cell["source"]])
        for key in cell["json_pointer"].lstrip("/").split("/"):
            key = key.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
        assert value == cell["value"], cell
    audit = json.loads((root / "evidence/coverage-audit.json").read_text())
    table = (root / "tables/system-comparison.tex").read_text()
    symbols = {"yes": r"\yes", "no_builtin": r"\no", "unestablished": r"\unconfirmed"}
    for row in audit["table_rows"]:
        line = next(line for line in table.splitlines() if line.startswith(row["feature"] + " &"))
        actual = re.findall(r"\\(?:yes|no|unconfirmed)\b", line)
        assert actual == [symbols[s] for s in row["statuses"]], row["feature"]
    print(json.dumps({"source_hashes": len(sources), "numeric_values": len(cells),
                      "coverage_judgments": sum(len(r["statuses"]) for r in audit["table_rows"]),
                      "experiments_started": 0, "provider_calls": 0}))


if __name__ == "__main__":
    main()
