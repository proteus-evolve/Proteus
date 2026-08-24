from __future__ import annotations

import sys
import types

import pytest

from proteus.safety.harness_loading import load_harness_safety_suite
from proteus.safety.phase1 import SUITE


class FixtureSuite:
    name = "fixture-suite"
    version = "1"

    def definitions(self):
        return SUITE.definitions()


def test_loads_family_suite_instance_class_and_factory(monkeypatch) -> None:
    module = types.ModuleType("fixture_harness_safety_suites")
    module.INSTANCE = FixtureSuite()
    module.SUITE_CLASS = FixtureSuite
    module.factory = lambda: FixtureSuite()
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert load_harness_safety_suite(f"{module.__name__}:INSTANCE").name == "fixture-suite"
    assert load_harness_safety_suite(f"{module.__name__}:SUITE_CLASS").name == "fixture-suite"
    assert load_harness_safety_suite(f"{module.__name__}:factory").name == "fixture-suite"


@pytest.mark.parametrize("spec", ("", "fixture", ":suite", "fixture:"))
def test_suite_spec_requires_module_and_object(spec: str) -> None:
    with pytest.raises(ValueError, match="<module>:<object>"):
        load_harness_safety_suite(spec)


def test_loader_rejects_an_instrument_integrity_suite(monkeypatch) -> None:
    from proteus.safety.integrity import SUITE

    module = types.ModuleType("instrument_suite")
    module.SUITE = SUITE
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(TypeError, match="definitions"):
        load_harness_safety_suite(f"{module.__name__}:SUITE")


def test_loader_rejects_a_suite_with_an_arbitrary_provider(monkeypatch) -> None:
    class ProviderSuite(FixtureSuite):
        def provider(self):
            return object()

    module = types.ModuleType("provider_harness_safety_suite")
    module.SUITE = ProviderSuite()
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(TypeError, match="definitions-only"):
        load_harness_safety_suite(f"{module.__name__}:SUITE")
