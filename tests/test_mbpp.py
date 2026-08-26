"""MBPP adapter tests against a fabricated official-format dataset, without network."""

import hashlib
import json
import os
from pathlib import Path


def _mini_dataset(tmp_path: Path) -> tuple[Path, dict]:
    record = {
        "source_file": "fixture.json",
        "task_id": 2,
        "prompt": "Write a function clamp(value) that limits integers to the range 0..5.",
        "code": "def clamp(value):\n    return min(5, max(0, value))\n",
        "test_imports": [],
        "test_list": [
            "assert clamp(-1) == 0",
            "assert clamp(2) == 2",
            "assert clamp(10) == 5",
        ],
    }
    path = tmp_path / "sanitized-mbpp.json"
    path.write_text(json.dumps([record]), encoding="utf-8")
    return path, record


def _seed_task(tmp_path: Path):
    from proteus.bench.mbpp import mbpp_task

    dataset, record = _mini_dataset(tmp_path)
    task = mbpp_task(2, dataset_file=dataset)
    ws = tmp_path / "task"
    ws.mkdir()
    task.setup(ws)
    return task, ws, record


def test_lists_and_seeds_only_public_task_material(tmp_path):
    from proteus.bench.mbpp import list_tasks, mbpp_task

    dataset, record = _mini_dataset(tmp_path)
    assert list_tasks(dataset) == ["2"]

    task = mbpp_task(2, dataset_file=dataset)
    ws = tmp_path / "task"
    ws.mkdir()
    task.setup(ws)

    assert task.id == "mbpp:2"
    assert sorted(path.name for path in ws.iterdir()) == ["README.md", "solution.py"]
    seeded = "\n".join(path.read_text(encoding="utf-8") for path in ws.iterdir())
    assert record["prompt"] in seeded
    assert record["code"].strip() not in seeded
    assert not any(test in seeded for test in record["test_list"])


def test_default_dataset_downloads_to_cache_once(tmp_path):
    import io
    from unittest.mock import patch

    from proteus.bench.mbpp import dataset_path, list_tasks

    fixture, _ = _mini_dataset(tmp_path)
    payload = fixture.read_bytes()
    old_home = os.environ.get("HOME")
    old_dataset = os.environ.pop("PROTEUS_MBPP_PATH", None)
    os.environ["HOME"] = str(tmp_path / "home")
    try:
        digest = hashlib.sha256(payload).hexdigest()
        with patch("proteus.bench.mbpp.DATA_SHA256", digest), patch(
            "proteus.bench._datasets.request.urlopen", return_value=io.BytesIO(payload)
        ) as get:
            first = dataset_path()
            second = dataset_path()
        assert first == second and first.is_file()
        assert list_tasks(first) == ["2"]
        assert get.call_count == 1
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_dataset is not None:
            os.environ["PROTEUS_MBPP_PATH"] = old_dataset


def test_user_supplied_dataset_paths_bypass_official_verification(tmp_path):
    from unittest.mock import patch

    from proteus.bench import mbpp

    dataset, _ = _mini_dataset(tmp_path)
    with patch.object(
        mbpp, "download_verified", side_effect=AssertionError("official download called")
    ):
        assert mbpp.dataset_path(dataset) == dataset

        old = os.environ.get("PROTEUS_MBPP_PATH")
        os.environ["PROTEUS_MBPP_PATH"] = str(dataset)
        try:
            assert mbpp.dataset_path() == dataset
        finally:
            if old is None:
                os.environ.pop("PROTEUS_MBPP_PATH", None)
            else:
                os.environ["PROTEUS_MBPP_PATH"] = old


def test_seeded_stub_fails_all_held_out_tests(tmp_path, trusted_grader):
    task, ws, _ = _seed_task(tmp_path)
    result = task.grade(ws, sandbox=trusted_grader)
    assert result.score == 0.0 and not result.passed
    assert "0/3" in result.detail


def test_canonical_solution_scores_full(tmp_path, trusted_grader):
    task, ws, record = _seed_task(tmp_path)
    (ws / "solution.py").write_text(record["code"], encoding="utf-8")
    result = task.grade(ws, sandbox=trusted_grader)
    assert result.score == 1.0 and result.passed
    assert "3/3" in result.detail


def test_reference_imports_needed_by_assertions_stay_in_trusted_parent(
    tmp_path, trusted_grader
):
    from proteus.bench.mbpp import mbpp_task

    record = {
        "source_file": "fixture.json",
        "task_id": 596,
        "prompt": "Write a function that returns the byte size of a tuple.",
        "code": "import sys\ndef tuple_size(value):\n    return sys.getsizeof(value)\n",
        "test_imports": [],
        "test_list": [
            "assert tuple_size(('A', 1)) == sys.getsizeof(('A', 1))",
        ],
    }
    dataset = tmp_path / "imports.json"
    dataset.write_text(json.dumps([record]), encoding="utf-8")
    task = mbpp_task(596, dataset_file=dataset)
    ws = tmp_path / "imports-task"
    ws.mkdir()
    task.setup(ws)
    (ws / "solution.py").write_text(record["code"], encoding="utf-8")
    result = task.grade(ws, sandbox=trusted_grader)
    assert result.score == 1.0 and result.passed


def test_opaque_return_values_preserve_truthiness(tmp_path, trusted_grader):
    from proteus.bench.mbpp import mbpp_task

    record = {
        "source_file": "fixture.json",
        "task_id": 737,
        "prompt": "Write a function that checks whether a string starts with a vowel.",
        "code": "import re\ndef starts_vowel(text):\n    return re.match(r'^[aeiou]', text)\n",
        "test_imports": [],
        "test_list": ["assert starts_vowel('annie')", "assert not starts_vowel('bob')"],
    }
    dataset = tmp_path / "opaque.json"
    dataset.write_text(json.dumps([record]), encoding="utf-8")
    task = mbpp_task(737, dataset_file=dataset)
    ws = tmp_path / "opaque-task"
    ws.mkdir()
    task.setup(ws)
    (ws / "solution.py").write_text(record["code"], encoding="utf-8")
    result = task.grade(ws, sandbox=trusted_grader)
    assert result.score == 1.0 and result.passed


def test_incomplete_solution_gets_dense_partial_score(tmp_path, trusted_grader):
    task, ws, _ = _seed_task(tmp_path)
    (ws / "solution.py").write_text(
        "def clamp(value):\n    return max(0, value)\n", encoding="utf-8"
    )
    result = task.grade(ws, sandbox=trusted_grader)
    assert result.score == 2 / 3 and not result.passed
    assert "2/3" in result.detail


def test_unavailable_sandbox_returns_legible_zero(tmp_path):
    class UnavailableSandbox:
        def run(self, *args, **kwargs):
            raise FileNotFoundError("docker unavailable")

    task, ws, _ = _seed_task(tmp_path)
    result = task.grade(ws, sandbox=UnavailableSandbox())
    assert result.score == 0.0 and not result.passed
    assert "secure grader unavailable" in result.detail


def test_cli_resolves_mbpp_task(tmp_path):
    from proteus.cli import _evaluator

    dataset, _ = _mini_dataset(tmp_path)
    old_dataset = os.environ.get("PROTEUS_MBPP_PATH")
    os.environ["PROTEUS_MBPP_PATH"] = str(dataset)
    try:
        spec, task = _evaluator("mbpp:2@observe", lambda: None)
    finally:
        if old_dataset is None:
            os.environ.pop("PROTEUS_MBPP_PATH", None)
        else:
            os.environ["PROTEUS_MBPP_PATH"] = old_dataset

    assert spec.kind == "benchmark" and spec.visibility.value == "observe"
    assert task is not None and task.id == "mbpp:2"
    assert spec.name == task.id
