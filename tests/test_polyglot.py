"""The polyglot tier, exercised against a fabricated dataset (no network, no clone).

The fabricated layout mirrors github.com/Aider-AI/polyglot-benchmark exactly — these
tests double as the shape documentation a contributor copies.
"""

import json
import textwrap
from pathlib import Path

from proteus.bench.polyglot import list_tasks, polyglot_task


def _mini_dataset(tmp_path: Path) -> Path:
    ex = tmp_path / "repo" / "python" / "exercises" / "practice" / "two-fer"
    (ex / ".docs").mkdir(parents=True)
    (ex / ".meta").mkdir()
    (ex / ".docs" / "instructions.md").write_text(
        "Given a name, return `One for <name>, one for me.` "
        "Without a name, use `you`.\n")
    (ex / ".meta" / "config.json").write_text(json.dumps(
        {"files": {"solution": ["two_fer.py"], "test": ["two_fer_test.py"]}}))
    (ex / "two_fer.py").write_text("def two_fer(name=None):\n    pass\n")
    (ex / "two_fer_test.py").write_text(textwrap.dedent("""\
        import unittest
        from two_fer import two_fer

        class TwoFerTest(unittest.TestCase):
            def test_no_name(self):
                self.assertEqual(two_fer(), "One for you, one for me.")

            def test_a_name(self):
                self.assertEqual(two_fer("Alice"), "One for Alice, one for me.")
        """))
    return tmp_path / "repo"


def test_lists_and_seeds_a_failing_exercise(tmp_path, trusted_grader):
    repo = _mini_dataset(tmp_path)
    assert list_tasks(repo) == ["two-fer"]
    task = polyglot_task("two-fer", repo_dir=repo)
    ws = tmp_path / "ws"
    ws.mkdir()
    task.setup(ws)
    assert (ws / "instructions.md").exists() and (ws / "two_fer.py").exists()
    r = task.grade(ws, sandbox=trusted_grader)
    assert not r.passed and r.score == 0.0, "a stub that already passes carries no signal"
    assert "0/2" in r.detail


def test_solving_the_stub_scores_full(tmp_path, trusted_grader):
    repo = _mini_dataset(tmp_path)
    task = polyglot_task("two-fer", repo_dir=repo)
    ws = tmp_path / "ws"
    ws.mkdir()
    task.setup(ws)
    (ws / "two_fer.py").write_text(
        'def two_fer(name=None):\n    return f"One for {name or \'you\'}, one for me."\n')
    r = task.grade(ws, sandbox=trusted_grader)
    assert r.passed and r.score == 1.0


def test_editing_the_tests_gains_nothing(tmp_path, trusted_grader):
    repo = _mini_dataset(tmp_path)
    task = polyglot_task("two-fer", repo_dir=repo)
    ws = tmp_path / "ws"
    ws.mkdir()
    task.setup(ws)
    (ws / "two_fer_test.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n"
        "    def test_free(self):\n        pass\n")
    r = task.grade(ws, sandbox=trusted_grader)
    assert not r.passed and r.score == 0.0, "held-out tests must be restored before grading"


def test_unknown_exercise_says_how_to_list(tmp_path):
    repo = _mini_dataset(tmp_path)
    try:
        polyglot_task("no-such", repo_dir=repo)
    except KeyError as exc:
        assert "list_tasks" in str(exc)
    else:
        raise AssertionError("an unknown exercise must raise")
