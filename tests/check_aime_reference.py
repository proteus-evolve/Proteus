"""Run in an isolated DSPy environment, never beside Proteus's pinned GEPA.

python tests/check_aime_reference.py --gepa-source /path/to/pinned/gepa
No models are called. This checks the real DSPy formatter/parser and exact GEPA
metric/template source, rather than comparing two locally copied constants.
"""

import argparse
import ast
import importlib.metadata
import random
import subprocess
from pathlib import Path
from types import SimpleNamespace

import dspy

from proteus.benchmarks import aime_format
from proteus.methods.gepa.reference import REVISION


def check(source):
    assert importlib.metadata.version("dspy") == aime_format.DSPY_VERSION
    assert subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() == REVISION
    class MathSolverSignature(dspy.Signature):
        input = dspy.InputField(desc="The math problem to solve.")
        answer = dspy.OutputField(desc="The final numerical answer.")
    signature = dspy.ChainOfThought(MathSolverSignature).predict.signature
    adapter = dspy.ChatAdapter()
    samples = ["", "Solve carefully.", "  indented\n    more\n", "\n\n", "推理后回答。", "{input}\n<INS>"]
    randomizer = random.Random(17)
    samples += ["".join(randomizer.choices(" a\nb\tλ{}[]", k=50)) for _ in range(30)]
    message_checks = 0
    for instructions in samples:
        signature.instructions = instructions
        for problem in samples[:6]:
            expected = adapter.format(signature, demos=[], inputs={"input": problem})
            assert aime_format.messages(instructions, problem) == expected, (instructions, problem)
            message_checks += 1
    completions = [
        "[[ ## reasoning ## ]]\nwork\n[[ ## answer ## ]]\n42\n[[ ## completed ## ]]",
        "[[ ## answer ## ]] 0042\n[[ ## reasoning ## ]] work",
        "[[ ## reasoning ## ]]\n[[ ## answer ## ]]",
        "preamble\n[[ ## reasoning ## ]]first\n[[ ## reasoning ## ]]second\n[[ ## answer ## ]]42",
        "  [[ ## reasoning ## ]]work\n  [[ ## answer ## ]]42",
        "[[ ## unknown ## ]]extra\n[[ ## answer ## ]]42\n[[ ## reasoning ## ]]work",
        "", "42", "[[ ## answer ## ]]42", "[[ ## reasoning ## ]]work",
    ]
    for completion in completions:
        try:
            expected = adapter.parse(signature, completion)
        except Exception:
            try:
                aime_format.parse(completion)
            except ValueError:
                continue
            raise AssertionError("copied parser accepted a rejected completion")
        assert aime_format.parse(completion) == expected
    utils = ast.parse((source / "examples/aime_math/utils.py").read_text())
    metric = next(item for item in utils.body if isinstance(item, ast.FunctionDef) and item.name == "math_metric")
    namespace = {}
    exec(compile(ast.Module(body=[metric], type_ignores=[]), "upstream-metric", "exec"), namespace)
    for answer in (None, "", "42", "0042", " 42 ", "42.0", "\\boxed{42}", "forty two", "-1", "999", 42, 42.9):
        for solution in ("", "The reference solution."):
            example, prediction = SimpleNamespace(answer="42", solution=solution), SimpleNamespace(answer=answer)
            assert aime_format.math_metric(example, prediction) == namespace["math_metric"](example, prediction)
    upstream = ast.parse((source / "src/gepa/optimize_anything.py").read_text())
    template = next(ast.literal_eval(item.value) for item in upstream.body if isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name) and item.target.id == "optimize_anything_reflection_prompt_template")
    assert aime_format.REFLECTION_TEMPLATE == template
    print(f"AIME reference conformance: {message_checks} messages, {len(completions)} parses, 24 metrics, exact template")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gepa-source", type=Path, required=True)
    check(parser.parse_args().gepa_source)
