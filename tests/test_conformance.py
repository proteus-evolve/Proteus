"""The public adapter/benchmark conformance gate.

Every built-in adapter that runs without Docker or a model is checked here against the
whole `HarnessAdapter` contract (`proteus.testing.check_adapter`), so a change that
breaks the contract fails CI. The committed templates and the scaffolder are checked the
same way, so `proteus/examples/adapter_template.py` and `python -m proteus.scaffold` can
never silently rot.

Contributors: this is the suite to point at your own adapter. Either register it in
`proteus/cli.py::_adapter_factory` and add its name to `PURE_ADAPTERS` (if it runs
offline), or, for a containerized / model-backed harness, run the opt-in hook:

    PROTEUS_CHECK_ADAPTER=mypkg.mod:MyHarness pytest tests/test_conformance.py
    PROTEUS_CHECK_ADAPTER=pi PROTEUS_CHECK_EPISODE=1 pytest tests/test_conformance.py

The same hook validates the built-in heavy adapters (dsh, pi, aki) on a machine that has
Docker / their checkouts; they are not in the default gate because CI has neither.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path

import pytest

from proteus.testing import check_adapter


def _load(name: str):
    """Resolve an adapter class the same way the CLI does (`--harness`)."""
    from proteus.cli import _adapter_factory
    return _adapter_factory(name)


# Adapters that provision offline (no Docker, no model): the always-on CI gate. Names
# resolve through the same factory `proteus check --harness` uses.
PURE_ADAPTERS = ["minimal", "llm", "proteus.examples.adapter_template:TemplateHarness"]

# Of those, the ones whose loop also runs a full episode offline (so `--episode` works
# with no API key). `llm` needs a model, so it is provisioning-only above.
OFFLINE_EPISODE_ADAPTERS = ["minimal", "proteus.examples.adapter_template:TemplateHarness"]


@pytest.mark.parametrize("name", PURE_ADAPTERS)
def test_pure_adapter_conformance(name):
    failures = check_adapter(_load(name)(), episode=False, verbose=False)
    assert failures == [], f"{name} fails the contract: {failures}"


@pytest.mark.parametrize("name", OFFLINE_EPISODE_ADAPTERS)
def test_offline_episode_conformance(name):
    failures = check_adapter(_load(name)(), episode=True, verbose=False)
    assert failures == [], f"{name} fails the live-episode contract: {failures}"


# --- benchmark contract -----------------------------------------------------------------

def _bench_template():
    return importlib.import_module("proteus.examples.benchmark_template")


def test_benchmark_template_grades():
    bt = _bench_template()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        bt.TASK.setup(ws)
        empty = bt.TASK.grade(ws)
        assert empty.score == 0.0 and not empty.passed
        (ws / "solution.py").write_text("def add(a, b):\n    return a + b\n")
        solved = bt.TASK.grade(ws)
        assert solved.score == 1.0 and solved.passed


def test_benchmark_wiring():
    """A BenchTask plugs into both the evaluator and the goal paths."""
    from proteus.bench.task import as_evaluator, as_goal, seed_task
    from proteus.core.goal import GoalConfig, GoalContext
    bt = _bench_template()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        harness = Path(d) / "harness"
        harness.mkdir(parents=True)
        seed_task(harness, bt.TASK)                       # materialises <run>/task/
        ev = as_evaluator(bt.TASK)
        res = ev([], GoalContext(harness_root=str(harness), episode=1))
        assert res.name == bt.TASK.id and 0.0 <= res.score <= 1.0
        assert isinstance(as_goal(bt.TASK), GoalConfig)


# --- scaffolder round-trips (the templates and the generator cannot rot) -----------------

def _import_file(path: Path, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scaffold_adapter_roundtrip(tmp_path):
    from proteus.scaffold import scaffold_adapter
    dest = tmp_path / "myharness.py"
    scaffold_adapter("MyHarness", dest)
    mod = _import_file(dest, "scaffolded_adapter")
    cls = mod.MyHarness
    assert cls.name == "my"
    assert check_adapter(cls(), episode=True, verbose=False) == []


def test_adapter_factory_loads_scaffold_from_console_script_cwd(tmp_path, monkeypatch):
    """Installed ``proteus`` scripts do not automatically put their cwd on sys.path."""
    import sys

    from proteus.cli import _adapter_factory
    from proteus.scaffold import scaffold_adapter

    scaffold_adapter("CwdHarness", tmp_path / "cwd.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in ("", str(tmp_path))])
    sys.modules.pop("cwd", None)
    try:
        cls = _adapter_factory("cwd:CwdHarness")
        assert cls.name == "cwd"
        assert check_adapter(cls(), episode=True, verbose=False) == []
    finally:
        sys.modules.pop("cwd", None)


def test_scaffold_benchmark_roundtrip(tmp_path):
    from proteus.bench.task import BenchTask
    from proteus.scaffold import scaffold_benchmark
    dest = tmp_path / "my_task.py"
    scaffold_benchmark("my_task", dest)
    mod = _import_file(dest, "scaffolded_bench")
    assert isinstance(mod.TASK, BenchTask)
    assert mod.TASK.id == "my_task"


# --- opt-in hook for containerized / third-party adapters -------------------------------

@pytest.mark.skipif(not os.environ.get("PROTEUS_CHECK_ADAPTER"),
                    reason="set PROTEUS_CHECK_ADAPTER=<name|module:Class> to check one")
def test_registered_adapter_conformance():
    name = os.environ["PROTEUS_CHECK_ADAPTER"]
    episode = bool(os.environ.get("PROTEUS_CHECK_EPISODE"))
    failures = check_adapter(_load(name)(), episode=episode, verbose=False)
    assert failures == [], f"{name} fails the contract: {failures}"
