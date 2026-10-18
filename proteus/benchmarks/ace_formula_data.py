"""Prepare the exact upstream ACE Formula 500/300/200 split without inference.

Source dataset bytes are fetched from the pinned upstream commit, never bundled
in the Proteus distribution. Order, targets and reference parsing stay unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

from proteus.artifacts.store import publish_bytes
from proteus.benchmarks.ace_formula import PROCESSOR_HASH, task_input
from proteus.core.contracts import require
from proteus.core.serialization import canonical_json, content_hash
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.methods.ace.playbook import REPOSITORY, REVISION


VERSION = "ace-formula-82709de-data-1"
DATASET_HASH = "sha256:9b1663fe5cf0c1cadf62ad1b093f11cbb1517b6973337584e18dc8de28278582"
SOURCES = (
    {"split": "train", "file": "formula_train_subset_500.jsonl", "rows": 500,
     "sha256": "aea676feb2354462ba22d3c2c73a18b780e1193b05597beaeb0250ae9021b482"},
    {"split": "validation", "file": "formula_val_subset_300.jsonl", "rows": 300,
     "sha256": "09948f7f24af5f2331f69a047ef086649873756cbf8626d19d23029346e98933"},
    {"split": "test", "file": "formula_test.jsonl", "rows": 200,
     "sha256": "079919e9631e50ba0489d2be1221ecf59952a9230ff91c5325c27b37ed99e342"},
)


def source_url(source):
    return f"https://raw.githubusercontent.com/ace-agent/ace/{REVISION}/eval/finance/data/{source['file']}"


def fetch(source, cache, *, checkout=None):
    path = Path(cache) / source["file"]
    if checkout is not None:
        data = (Path(checkout) / "eval/finance/data" / source["file"]).read_bytes()
    elif path.exists():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(source_url(source), timeout=60) as response:
            data = response.read(20 * 1024 * 1024 + 1)
    require(hashlib.sha256(data).hexdigest() == source["sha256"],
            f"Formula source checksum differs: {source['file']}")
    publish_bytes(path, data)
    return path


def from_rows(pools):
    require(set(pools) == {source["split"] for source in SOURCES}, "Formula requires train, validation and test")
    records, seen = [], set()
    for source in SOURCES:
        rows = pools[source["split"]]
        require(len(rows) == source["rows"], f"Formula {source['split']} requires all {source['rows']} rows")
        for index, raw in enumerate(rows):
            public = task_input(raw)
            fingerprint = content_hash(public)
            require(fingerprint not in seen, "duplicate Formula input across declared splits")
            seen.add(fingerprint)
            records.append(TaskRecord(task=Task(id=f"ace-formula:{source['split']}:{index:06d}",
                version=REVISION, split=source["split"], input={"question": public.question, "context": public.context}),
                reference=raw["target"]))
    return TaskDataset(version=VERSION, records=tuple(records))


def prepare(out, cache, *, checkout=None):
    pools = {source["split"]: [json.loads(line) for line in fetch(source, cache, checkout=checkout).read_text().splitlines()
                              if line.strip()] for source in SOURCES}
    dataset = from_rows(pools)
    require(content_hash(dataset) == DATASET_HASH, "Formula prepared dataset differs from frozen reference")
    provenance = {"repository": REPOSITORY, "revision": REVISION, "dataset_hash": DATASET_HASH,
        "dataset_version": VERSION, "processor_sha256": PROCESSOR_HASH,
        "sources": [{**source, "url": source_url(source)} for source in SOURCES],
        "split": {"algorithm": "upstream 500/300/200 files in original line order; no shuffle/subsampling",
                  "task_ids": {source["split"]: [r.task.id for r in dataset.records if r.task.split == source["split"]]
                               for source in SOURCES}},
        "license_notice": "The upstream repository includes Apache-2.0 LICENSE.txt. These dataset files have no "
                          "separate license declaration at the pinned revision; retain upstream provenance. "
                          "Dataset bytes are downloaded, not redistributed in Proteus."}
    out = Path(out).resolve()
    publish_bytes(out / "tasks.json", (dataset.to_json() + "\n").encode())
    publish_bytes(out / "provenance.json", (canonical_json(provenance) + "\n").encode())
    return out / "tasks.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--source", type=Path, help="optional pinned ACE checkout; file hashes are still required")
    args = parser.parse_args()
    print(prepare(args.out, args.cache, checkout=args.source))


if __name__ == "__main__":
    main()
