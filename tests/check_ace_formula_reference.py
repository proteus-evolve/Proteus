"""Execute Formula-only ASTs from the verified upstream processor; no SDKs."""

import __future__
import argparse
import ast
import copy
import hashlib
import inspect
import json
from pathlib import Path

from check_ace_reference import function_ast
from proteus.benchmarks import ace_formula, ace_formula_data, ace_formula_reference
from proteus.core.serialization import content_hash
from proteus.evaluation.tasks import TaskDataset


def load_processor(source):
    source = Path(source) / "eval/finance/data_processor.py"
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ace_formula.PROCESSOR_HASH
    tree = ast.parse(raw.decode())
    parse = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "parse_context_and_question_formula")
    assert function_ast(parse) == function_ast(ast.parse(inspect.getsource(
        ace_formula_reference.parse_context_and_question_formula)).body[0])
    cls = copy.deepcopy(next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DataProcessor"))
    methods = ("__init__", "process_task_data", "_formula_answer_is_correct", "answer_is_correct",
               "_evaluate_formula_accuracy", "evaluate_accuracy")
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    assert len(cls.body) == len(methods)
    metric = next(node for node in cls.body if node.name == "_formula_answer_is_correct")
    local = ast.parse(inspect.getsource(ace_formula_reference.FormulaReference)).body[0].body[0]
    assert function_ast(metric) == function_ast(local)
    namespace = {}
    exec(compile(ast.Module(body=[parse, cls], type_ignores=[]), str(source), "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace["DataProcessor"]("formula"), namespace[parse.name]


def check(source, out):
    processor, parse = load_processor(source)
    path = ace_formula_data.prepare(out, Path(out) / "cache", checkout=source)
    dataset = TaskDataset.from_json(path.read_text())
    assert content_hash(dataset) == ace_formula_data.DATASET_HASH
    expected = []
    for item in ace_formula_data.SOURCES:
        raw = (Path(source) / "eval/finance/data" / item["file"]).read_text()
        rows = processor.process_task_data([json.loads(line) for line in raw.splitlines() if line.strip()])
        assert len(rows) == item["rows"]
        expected.extend(rows)
    assert len(dataset.records) == len(expected) == 1000
    for record, row in zip(dataset.records, expected):
        assert dict(record.task.input) == {"question": row["question"], "context": row["context"]}
        assert record.reference == row["target"] and processor.answer_is_correct(record.reference, record.reference)
    cases = [("1,000.00", "1000"), ("1.001", "1"), ("NaN", "NaN"), ("inf", "Infinity"),
             ("$1", "$1"), (" 1 ", "1.0"), ("answer: 1", "1"), ("", ""), ("a,b", "ab")]
    metric = ace_formula_reference.FormulaReference()
    for prediction, target in cases:
        assert metric._formula_answer_is_correct(prediction, target) == processor.answer_is_correct(prediction, target)
    assert processor.evaluate_accuracy([p for p, _ in cases], [t for _, t in cases]) == sum(
        metric._formula_answer_is_correct(p, t) for p, t in cases) / len(cases)
    for text in ("plain question", 'prefix Question: "quoted question". Answer: ignored', "Question: unfinished"):
        assert ace_formula_reference.parse_context_and_question_formula(text) == parse(text)
    return dataset


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    check(args.source, args.out)
    print("ACE Formula: 1000 source rows, parser/metric ASTs, split identity and exact correctness passed")
