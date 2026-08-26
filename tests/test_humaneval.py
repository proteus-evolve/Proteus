"""HumanEval adapter tests against a fabricated official-format dataset, offline."""

import gzip
import hashlib
import json
import os
from pathlib import Path


def _mini_dataset(tmp_path: Path) -> tuple[Path, dict]:
    record = {
        "task_id": "HumanEval/0",
        "prompt": (
            "def expected_clamp(value: int) -> int:\n"
            "    return sorted((0, value, 5))[1]\n\n\n"
            "def clamp(value: int) -> int:\n"
            "    \"\"\"Limit an integer to the inclusive range 0..5.\"\"\"\n"
        ),
        "canonical_solution": "    return min(5, max(0, value))\n",
        "test": (
            "def check(candidate):\n"
            "    assert candidate(-1) == expected_clamp(-1)\n"
            "    assert candidate(2) == expected_clamp(2)\n"
            "    assert candidate(10) == expected_clamp(10)\n"
        ),
        "entry_point": "clamp",
    }
    path = tmp_path / "HumanEval.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    return path, record


def _seed_task(tmp_path: Path):
    from proteus.bench.humaneval import humaneval_task

    dataset, record = _mini_dataset(tmp_path)
    task = humaneval_task("HumanEval/0", dataset_file=dataset)
    ws = tmp_path / "task"
    ws.mkdir()
    task.setup(ws)
    return task, ws, record


def test_lists_and_seeds_only_public_task_material(tmp_path):
    from proteus.bench.humaneval import humaneval_task, list_tasks

    dataset, record = _mini_dataset(tmp_path)
    assert list_tasks(dataset) == ["HumanEval/0"]

    task = humaneval_task("HumanEval/0", dataset_file=dataset)
    ws = tmp_path / "task"
    ws.mkdir()
    task.setup(ws)

    assert task.id == "humaneval:HumanEval/0"
    assert sorted(path.name for path in ws.iterdir()) == ["README.md", "solution.py"]
    seeded = "\n".join(path.read_text(encoding="utf-8") for path in ws.iterdir())
    assert record["prompt"] in seeded
    assert record["canonical_solution"].strip() not in seeded
    assert record["test"].strip() not in seeded


def test_default_dataset_downloads_to_cache_once(tmp_path):
    import io
    from unittest.mock import patch

    from proteus.bench.humaneval import dataset_path, list_tasks

    fixture, _ = _mini_dataset(tmp_path)
    payload = fixture.read_bytes()
    old_home = os.environ.get("HOME")
    old_dataset = os.environ.pop("PROTEUS_HUMANEVAL_PATH", None)
    os.environ["HOME"] = str(tmp_path / "home")
    try:
        digest = hashlib.sha256(payload).hexdigest()
        with patch("proteus.bench.humaneval.DATA_SHA256", digest), patch(
            "proteus.bench._datasets.request.urlopen", return_value=io.BytesIO(payload)
        ) as get:
            first = dataset_path()
            second = dataset_path()
        assert first == second and first.is_file()
        assert list_tasks(first) == ["HumanEval/0"]
        assert get.call_count == 1
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_dataset is not None:
            os.environ["PROTEUS_HUMANEVAL_PATH"] = old_dataset


def test_user_supplied_dataset_paths_bypass_official_verification(tmp_path):
    from unittest.mock import patch

    from proteus.bench import humaneval

    dataset, _ = _mini_dataset(tmp_path)
    with patch.object(
        humaneval, "download_verified", side_effect=AssertionError("official download called")
    ):
        assert humaneval.dataset_path(dataset) == dataset

        old = os.environ.get("PROTEUS_HUMANEVAL_PATH")
        os.environ["PROTEUS_HUMANEVAL_PATH"] = str(dataset)
        try:
            assert humaneval.dataset_path() == dataset
        finally:
            if old is None:
                os.environ.pop("PROTEUS_HUMANEVAL_PATH", None)
            else:
                os.environ["PROTEUS_HUMANEVAL_PATH"] = old


def test_seeded_stub_fails_held_out_check(tmp_path, trusted_grader):
    task, ws, _ = _seed_task(tmp_path)
    result = task.grade(ws, sandbox=trusted_grader)
    assert result.score == 0.0 and not result.passed
    assert "official check failed" in result.detail


def test_canonical_solution_passes_official_check(tmp_path, trusted_grader):
    task, ws, record = _seed_task(tmp_path)
    (ws / "solution.py").write_text(
        record["prompt"] + record["canonical_solution"], encoding="utf-8"
    )
    result = task.grade(ws, sandbox=trusted_grader)
    assert result.score == 1.0 and result.passed
    assert result.detail == "official check passed"


def test_cli_resolves_humaneval_task(tmp_path):
    from proteus.cli import _evaluator

    dataset, _ = _mini_dataset(tmp_path)
    old_dataset = os.environ.get("PROTEUS_HUMANEVAL_PATH")
    os.environ["PROTEUS_HUMANEVAL_PATH"] = str(dataset)
    try:
        spec, task = _evaluator("humaneval:HumanEval/0@observe", lambda: None)
    finally:
        if old_dataset is None:
            os.environ.pop("PROTEUS_HUMANEVAL_PATH", None)
        else:
            os.environ["PROTEUS_HUMANEVAL_PATH"] = old_dataset

    assert spec.kind == "benchmark" and spec.visibility.value == "observe"
    assert task is not None and task.id == "humaneval:HumanEval/0"
    assert spec.name == task.id
