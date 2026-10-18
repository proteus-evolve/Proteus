"""Pinned bundled MCE data, with exact upstream prompt rendering and split order."""

import hashlib
import json
from pathlib import Path
import random
from types import FunctionType

from proteus.core.contracts import require
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.methods.meta_harness.records import REVISION
from proteus.methods.meta_harness.reference.data import loaders

DIRECTORIES = {
    "USPTO": "uspto",
    "Symptom2Disease": "symptom_diagnosis",
    "LawBench": "crime_prediction",
}
SIZES = {"USPTO": (50, 30, 100), "Symptom2Disease": (200, 50, 212), "LawBench": (200, 50, 100)}
HASHES = {
    "uspto/train.jsonl": "5049890cd908a5bbed4f635a2a68c51a8756d50fc09e7648ad4ebc36a295f6d6",
    "uspto/val.jsonl": "824579221c59d1e71895620f539a05f978d8832e067a7956562612eebcdef2ed",
    "uspto/test.jsonl": "e2730f05f215363687ec0c6a1f75ce808ed697749b181becdae62cd7991c67e4",
    "symptom_diagnosis/train.jsonl": "b41e494b40bf971a1eadd79ed4dda527640916abe0a2dc94e047e13ef293ee4c",
    "symptom_diagnosis/val.jsonl": "f6ad74bea9fb59f4a3abf930668042c33fbb7519bc9e2568a9758c2a0b1277e8",
    "symptom_diagnosis/test.jsonl": "6a6b224665715f40d42a46eed82498698d39fa7689241eaae2cc2276e444671d",
    "crime_prediction/train.jsonl": "36ead48b5e88e02f6bf5944d1c00c561f6592e5573f9ecf05c23affc111c386c",
    "crime_prediction/val.jsonl": "c5d900d64a7be5359579e4c2fc0b499f18ebbe9a96fee1ff43879d511bc992ea",
    "crime_prediction/test.jsonl": "a08a9aba4da6814fc0efdfc713814a2562f4ce1d174981d8d5823a126b99f6dc",
}
DATASET_HASHES = {
    "USPTO": "sha256:3ef04c43b7b8e1cb36ea656217decfb209d719860e64a6005d16571fd3c8461c",
    "Symptom2Disease": "sha256:68585c7c609c2650ded0d790ccb3d3a525ad456911006b8ff51bac9bd351d49c",
    "LawBench": "sha256:8423c368f75ea01268fc69afac8443b97afd1c50202e70c7555db857ffe53ae8",
}


def dataset(source, name, seed):
    require(name in DIRECTORIES, "unsupported Meta-Harness dataset")
    data = Path(source) / "reference_examples/text_classification/data"
    # Clone only module globals to select a data root without process-global
    # mutation. Function bodies remain the pinned upstream MCE implementations.
    namespace = {**vars(loaders), "MCE_DATA_PATH": str(data)}
    for key, value in tuple(namespace.items()):
        if isinstance(value, FunctionType) and value.__module__ == loaders.__name__:
            namespace[key] = FunctionType(
                value.__code__, namespace, value.__name__, value.__defaults__, value.__closure__
            )
    records, orders, provenance = [], {}, {}
    for split, expected in zip(("train", "val", "test"), SIZES[name]):
        relative = DIRECTORIES[name] + "/" + split + ".jsonl"
        raw = (data / relative).read_bytes()
        require(
            hashlib.sha256(raw).hexdigest() == HASHES[relative],
            "reference data bytes differ: " + relative,
        )
        examples = namespace["load_mce_dataset"](name, split)
        require(len(examples) == expected, "reference split size differs")
        ids = []
        for index, example in enumerate(examples):
            key = name + "-" + split + "-" + str(index)
            ids.append(key)
            text = (example["context"] + "\n\n" if example.get("context") else "") + example[
                "input"
            ]
            records.append(
                TaskRecord(
                    task=Task(id=key, version=REVISION, split=split, input=text),
                    reference={
                        "target": example["target"],
                        "extras": {
                            k: v for k, v in example.items() if k not in ("input", "target")
                        },
                    },
                )
            )
        random.Random(seed).shuffle(ids)
        orders[split] = tuple(ids)
        provenance[relative] = {"sha256": HASHES[relative], "records": expected}
    return TaskDataset(version=REVISION, records=tuple(records)), orders, provenance


def save_provenance(path, records):
    Path(path).write_text(json.dumps({"revision": REVISION, "files": records}, indent=2) + "\n")
