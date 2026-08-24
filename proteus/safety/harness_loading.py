"""Load module-first harness-safety suites through the extension convention."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import cast

from proteus.safety.live import LiveModelConfig, preflight_live_model
from proteus.safety.plugins import HarnessSafetyCaseSuite
from proteus.safety.taxonomy import EvidenceStratum, SafetyCaseFamilyDefinition


def _looks_like_suite(value: object) -> bool:
    return (
        isinstance(getattr(value, "name", None), str)
        and isinstance(getattr(value, "version", None), str)
        and callable(getattr(value, "definitions", None))
    )


def validate_harness_safety_suite(
    value: object,
) -> tuple[SafetyCaseFamilyDefinition, ...]:
    """Validate one definitions-only suite before any output path is created."""
    for name, predicate in (
        ("name", lambda item: isinstance(item, str) and bool(item.strip())),
        ("version", lambda item: isinstance(item, str) and bool(item.strip())),
        ("definitions", callable),
    ):
        if not predicate(getattr(value, name, None)):
            raise TypeError(f"harness safety suite needs valid {name}")
    if callable(getattr(value, "provider", None)):
        raise TypeError("harness safety suites must be definitions-only")
    definitions = tuple(value.definitions())  # type: ignore[attr-defined]
    if not definitions:
        raise ValueError("harness safety suite has no case families")
    if not all(isinstance(item, SafetyCaseFamilyDefinition) for item in definitions):
        raise TypeError("harness safety suite definitions must be case families")
    family_ids = [item.family_id for item in definitions]
    if len(family_ids) != len(set(family_ids)):
        raise ValueError("harness safety suite has duplicate family ID")
    return definitions


def suite_requires_fixed_live(
    definitions: tuple[SafetyCaseFamilyDefinition, ...],
) -> bool:
    return any(
        EvidenceStratum.FIXED_LIVE_BEHAVIOR in requirement.required_strata
        for definition in definitions
        for requirement in definition.indicator_requirements
    )


def preflight_harness_safety_suite(
    suite: object,
    *,
    model_config: LiveModelConfig | None,
    repository_root: Path,
) -> tuple[SafetyCaseFamilyDefinition, ...]:
    """Validate suite and fixed-live prerequisites without creating output."""
    definitions = validate_harness_safety_suite(suite)
    if suite_requires_fixed_live(definitions):
        if model_config is None:
            raise ValueError("fixed-live safety evidence requires an explicit model config")
        preflight_live_model(model_config, repository_root)
    return definitions


def load_harness_safety_suite(spec: str) -> HarnessSafetyCaseSuite:
    """Resolve a suite instance, class, or zero-argument factory."""
    module_name, separator, object_name = spec.partition(":")
    if not separator or not module_name or not object_name:
        raise ValueError("harness safety suite must use <module>:<object>")
    module = importlib.import_module(module_name)
    value = getattr(module, object_name)
    if isinstance(value, type) or (callable(value) and not _looks_like_suite(value)):
        value = value()
    validate_harness_safety_suite(value)
    return cast(HarnessSafetyCaseSuite, value)
