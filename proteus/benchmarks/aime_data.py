"""Prepare GEPA's full AIME split from immutable, checksummed public datasets.

PyArrow is needed only by this preparation command. The solver reads JSON using
the core runtime. Dataset contents are downloaded, never bundled with Proteus.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import urllib.request
from pathlib import Path

from proteus.artifacts.store import publish_bytes
from proteus.core.contracts import require
from proteus.core.serialization import canonical_json, content_hash
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord


VERSION = "gepa-aime-8b0ce6c-data-1"
DATASET_HASH = "sha256:b083036dea44fdc3b025ac9b29f1198ff2529f1065cc8982f2c880542825a8cf"
SOURCES = (
    {"file": "train-val.parquet", "repository": "AI-MO/aimo-validation-aime",
     "revision": "13f9e12f613e720c2a2b2f345dd04b998a29494d",
     "sha256": "025484a99fea498e7d0c3b0ee42afcbec0176405c19c5dbf557b9f6ca6445675",
     "rows": 90, "license": "Apache-2.0"},
    {"file": "test.parquet", "repository": "MathArena/aime_2025",
     "revision": "c94da77eb22bbd6439e62a323bec18493a421302",
     "sha256": "9f9066ff48ad2e31f9bf1b1ac6d5e80693195f987985f2859f89dd25ffa51c2d",
     "rows": 30, "license": "CC-BY-NC-SA-4.0"},
)


def source_url(source):
    return (f"https://huggingface.co/datasets/{source['repository']}/resolve/"
            f"{source['revision']}/data/train-00000-of-00001.parquet")


def fetch(source, cache):
    path = Path(cache).resolve() / source["file"]
    if path.exists():
        data = path.read_bytes()
    else:
        with urllib.request.urlopen(source_url(source), timeout=60) as response:
            data = response.read(20 * 1024 * 1024 + 1)
    require(hashlib.sha256(data).hexdigest() == source["sha256"],
            f"AIME source checksum differs: {source['file']}")
    publish_bytes(path, data)
    return path


def from_rows(training, testing):
    """The reference shuffles all 90 examples once, then slices 45 / 45."""
    require(len(training) == 90 and len(testing) == 30, "AIME requires all 90+30 source rows")
    training = list(training)
    random.Random(0).shuffle(training)
    records = []
    seen_problems, seen_ids = set(), set()
    for split, rows in (("train", training[:45]), ("validation", training[45:]), ("test", testing)):
        for item in rows:
            source = SOURCES[1 if split == "test" else 0]
            raw_id = item["problem_idx" if split == "test" else "id"]
            require(type(raw_id) is int, "AIME source ID must be an integer")
            task_id = source["repository"] + ":" + str(raw_id)
            problem = item["problem"]
            require(type(problem) is str and bool(problem.strip()), "empty AIME problem")
            require(task_id not in seen_ids and problem not in seen_problems, "duplicate AIME problem/identity")
            seen_ids.add(task_id)
            seen_problems.add(problem)
            answer = item["answer"]
            require(type(answer) in (int, str) and str(answer).isdigit() and 0 <= int(answer) <= 999,
                    "invalid AIME reference answer")
            reference = {"answer": answer}
            if split != "test":
                require(type(item["solution"]) is str, "invalid AIME reference solution")
                reference["solution"] = item["solution"]
            records.append(TaskRecord(task=Task(id=task_id, version=source["revision"], split=split,
                input={"problem": problem}), reference=reference))
    return TaskDataset(version=VERSION, records=tuple(records))


def prepare(out, cache):
    import pyarrow
    import pyarrow.parquet as parquet
    require(pyarrow.__version__ == "21.0.0", "AIME preparation requires pyarrow==21.0.0")
    rows = [parquet.read_table(fetch(source, cache)).to_pylist() for source in SOURCES]
    dataset = from_rows(*rows)
    require(content_hash(dataset) == DATASET_HASH, "AIME prepared dataset differs from the frozen reference")
    out = Path(out).resolve()
    provenance = {"dataset_hash": content_hash(dataset), "dataset_version": VERSION,
        "sources": [{**source, "url": source_url(source),
                     "card": f"https://huggingface.co/datasets/{source['repository']}/tree/{source['revision']}"}
                    for source in SOURCES],
        "split": {"algorithm": "random.Random(0).shuffle; first 45 train, last 45 validation; test unchanged",
                  "task_ids": {split: [r.task.id for r in dataset.records if r.task.split == split]
                               for split in ("train", "validation", "test")}},
        "preparation": {"pyarrow": pyarrow.__version__},
        "notice": "Dataset licenses apply separately from the Proteus software license. "
                  "MathArena/aime_2025 is CC-BY-NC-SA-4.0; retain source attribution and license."}
    publish_bytes(out / "tasks.json", (dataset.to_json() + "\n").encode())
    publish_bytes(out / "provenance.json", (canonical_json(provenance) + "\n").encode())
    return out / "tasks.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.out, args.cache))


if __name__ == "__main__":
    main()
