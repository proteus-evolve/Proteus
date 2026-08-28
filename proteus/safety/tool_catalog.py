"""Native callable-tool catalog observations for settled-snapshot audits.

The controller records the schemas that a harness actually offered to a model.
This module intentionally does not inspect authored files: a file is not a
callable tool unless it appears in that native catalog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from proteus.core.snapshot import SnapshotRef
from proteus.safety.evidence import validate_evidence_refs
from proteus.safety.taxonomy import SafetyStatus

PAIRED_PERMISSION_PROBE = "paired_permission"
DISPATCH_PROBE = "dispatch"


def _canonical_schema(schema: Mapping[str, object]) -> str:
    """Return one stable, complete JSON representation of a native tool schema."""
    if not isinstance(schema, Mapping):
        raise TypeError("native tool schema must be a mapping")
    try:
        return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("native tool schema must be JSON serializable") from exc


def _require_canonical_schema(value: str) -> None:
    if not value:
        raise ValueError("canonical tool schema must not be empty")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("canonical tool schema must be JSON") from exc
    if not isinstance(decoded, dict) or _canonical_schema(decoded) != value:
        raise ValueError("tool schema must use canonical JSON object encoding")


@dataclass(frozen=True)
class NativeToolSchema:
    """One tool definition observed in a controller-owned native request."""

    name: str
    canonical_schema: str
    raw_schema_ref: str

    @classmethod
    def from_schema(
        cls, *, name: str, schema: Mapping[str, object], raw_schema_ref: str
    ) -> NativeToolSchema:
        return cls(
            name=name,
            canonical_schema=_canonical_schema(schema),
            raw_schema_ref=raw_schema_ref,
        )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("native tool name must not be blank")
        _require_canonical_schema(self.canonical_schema)
        validate_evidence_refs((self.raw_schema_ref,))


@dataclass(frozen=True)
class NativeToolCatalog:
    """The ordinary callable-tool inventory for one fresh snapshot boot."""

    snapshot: SnapshotRef
    loader_id: str
    tools: tuple[NativeToolSchema, ...]
    raw_catalog_ref: str

    def __post_init__(self) -> None:
        if not self.loader_id.strip():
            raise ValueError("native tool catalog requires a loader ID")
        validate_evidence_refs((self.raw_catalog_ref,))
        names = tuple(tool.name for tool in self.tools)
        if len(set(names)) != len(names):
            raise ValueError("native tool catalog has duplicate tool names")
        if names != tuple(sorted(names)):
            raise ValueError("native tool catalog tools must be sorted by name")

    def by_name(self) -> dict[str, NativeToolSchema]:
        return {tool.name: tool for tool in self.tools}


def native_schema_accepts_exact_empty_object(tool: NativeToolSchema) -> bool:
    """Return whether the observed function schema unambiguously accepts ``{}``.

    Built-in harnesses expose either a direct function definition or the OpenAI
    ``{"type": "function", "function": ...}`` envelope.  This deliberately
    recognizes only the common, explicit object form.  Composition, references,
    and value constraints need a harness-owned argument vector rather than a
    guessed generic request.
    """
    try:
        schema = json.loads(tool.canonical_schema)
    except json.JSONDecodeError:
        return False
    if not isinstance(schema, Mapping):
        return False
    nested = schema.get("function")
    definition = nested if isinstance(nested, Mapping) else schema
    if definition.get("name") != tool.name:
        return False
    parameters = definition.get("parameters")
    if not isinstance(parameters, Mapping) or parameters.get("type") != "object":
        return False
    required = parameters.get("required", [])
    if (
        not isinstance(required, list)
        or required
        or not all(isinstance(item, str) for item in required)
    ):
        return False
    min_properties = parameters.get("minProperties", 0)
    if type(min_properties) is not int or min_properties != 0:
        return False
    ambiguous_constraints = {
        "$ref",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "const",
        "enum",
    }
    return not any(key in parameters for key in ambiguous_constraints)


def native_tool_catalog_evidence_is_local(
    catalog: NativeToolCatalog,
    *,
    artifact_root: Path,
    evidence_dir: Path,
) -> bool:
    """Return whether every catalog receipt belongs to this collection context.

    Native-catalog cache keys deliberately use stable snapshot identity, while
    artifact references are relative to one particular safety staging root.
    Reusing a catalog is sound only if all of its receipts still resolve under
    the *requested* context's evidence directory.
    """
    try:
        root = artifact_root.resolve(strict=True)
        local = evidence_dir.resolve(strict=True)
        local.relative_to(root)
    except (OSError, ValueError):
        return False
    refs = (catalog.raw_catalog_ref, *(tool.raw_schema_ref for tool in catalog.tools))
    for ref in refs:
        try:
            path = (root / ref).resolve(strict=True)
            path.relative_to(local)
        except (OSError, ValueError):
            return False
        if not path.is_file():
            return False
    return True


def tool_coverage_evidence_is_local(
    coverage: AdapterOwnedToolCoverage,
    *,
    artifact_root: Path,
) -> bool:
    """Return whether every coverage receipt resolves inside this artifact root."""
    try:
        root = artifact_root.resolve(strict=True)
    except OSError:
        return False
    refs = (coverage.raw_coverage_ref, *coverage.probe_evidence_refs)
    for ref in refs:
        # Evidence references may select one record inside a JSON artifact.
        file_ref = ref.partition("#")[0]
        try:
            path = (root / file_ref).resolve(strict=True)
            path.relative_to(root)
        except (OSError, ValueError):
            return False
        if not path.is_file():
            return False
    return True


@dataclass(frozen=True)
class ChangedNativeToolSchema:
    """One same-name tool whose full native schema changed across snapshots."""

    baseline: NativeToolSchema
    current: NativeToolSchema

    def __post_init__(self) -> None:
        if self.baseline.name != self.current.name:
            raise ValueError("changed tool schemas must have the same name")
        if self.baseline.canonical_schema == self.current.canonical_schema:
            raise ValueError("changed tool schemas must differ")

    @property
    def name(self) -> str:
        return self.current.name


@dataclass(frozen=True)
class NativeToolCatalogDelta:
    """Full-schema difference between a baseline and evolved callable catalog."""

    baseline: NativeToolCatalog
    current: NativeToolCatalog
    added: tuple[NativeToolSchema, ...]
    removed: tuple[NativeToolSchema, ...]
    changed: tuple[ChangedNativeToolSchema, ...]

    @property
    def introduced_or_changed(self) -> tuple[NativeToolSchema, ...]:
        return self.added + tuple(change.current for change in self.changed)


def compare_native_tool_catalogs(
    baseline: NativeToolCatalog, current: NativeToolCatalog
) -> NativeToolCatalogDelta:
    """Compare the complete canonical schemas from two settled-snapshot boots."""
    before = baseline.by_name()
    after = current.by_name()
    added = tuple(after[name] for name in sorted(after.keys() - before.keys()))
    removed = tuple(before[name] for name in sorted(before.keys() - after.keys()))
    changed = tuple(
        ChangedNativeToolSchema(before[name], after[name])
        for name in sorted(before.keys() & after.keys())
        if before[name].canonical_schema != after[name].canonical_schema
    )
    return NativeToolCatalogDelta(
        baseline=baseline,
        current=current,
        added=added,
        removed=removed,
        changed=changed,
    )


@dataclass(frozen=True)
class AdapterOwnedToolCoverage:
    """Exact adapter-owned binding plus its controller-observed probe outcome.

    A dispatch probe establishes registration and route viability only.  A
    paired-permission probe additionally owns protected/allowed requests and an
    independent canary, and is the only scope that can establish permission
    safety for a newly callable schema.
    """

    name: str
    canonical_schema: str
    adapter_name: str
    native_mechanism: str
    raw_coverage_ref: str
    probe_status: SafetyStatus = SafetyStatus.NOT_EVALUATED
    probe_evidence_refs: tuple[str, ...] = ()
    probe_scope: str = PAIRED_PERMISSION_PROBE
    probe_reason: str = ""

    @classmethod
    def from_schema(
        cls,
        *,
        name: str,
        schema: Mapping[str, object],
        adapter_name: str,
        native_mechanism: str,
        raw_coverage_ref: str,
        probe_status: SafetyStatus = SafetyStatus.NOT_EVALUATED,
        probe_evidence_refs: tuple[str, ...] = (),
        probe_scope: str = PAIRED_PERMISSION_PROBE,
        probe_reason: str = "",
    ) -> AdapterOwnedToolCoverage:
        return cls(
            name=name,
            canonical_schema=_canonical_schema(schema),
            adapter_name=adapter_name,
            native_mechanism=native_mechanism,
            raw_coverage_ref=raw_coverage_ref,
            probe_status=probe_status,
            probe_evidence_refs=probe_evidence_refs,
            probe_scope=probe_scope,
            probe_reason=probe_reason,
        )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("covered tool name must not be blank")
        _require_canonical_schema(self.canonical_schema)
        if not self.adapter_name.strip():
            raise ValueError("adapter-owned coverage requires an adapter name")
        if not self.native_mechanism.strip():
            raise ValueError("adapter-owned coverage requires a native mechanism")
        if self.probe_status not in {
            SafetyStatus.PASS,
            SafetyStatus.FAIL,
            SafetyStatus.NOT_EVALUATED,
            SafetyStatus.INVALID,
            SafetyStatus.ERROR,
        }:
            raise ValueError("callable coverage has an unsupported probe status")
        if self.probe_scope not in {PAIRED_PERMISSION_PROBE, DISPATCH_PROBE}:
            raise ValueError("callable coverage has an unsupported probe scope")
        if not isinstance(self.probe_reason, str):
            raise TypeError("callable coverage probe reason must be a string")
        validate_evidence_refs((self.raw_coverage_ref, *self.probe_evidence_refs))


@dataclass(frozen=True)
class CallableToolCatalogAudit:
    """Exact binding and probe outcome for newly callable or changed tools."""

    status: SafetyStatus
    delta: NativeToolCatalogDelta
    unbound_names: tuple[str, ...]
    coverage: tuple[AdapterOwnedToolCoverage, ...]
    evidence_refs: tuple[str, ...]
    unprobed_names: tuple[str, ...] = ()
    failed_names: tuple[str, ...] = ()
    invalid_names: tuple[str, ...] = ()
    error_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, names in (
            ("unbound", self.unbound_names),
            ("unprobed", self.unprobed_names),
            ("failed", self.failed_names),
            ("invalid", self.invalid_names),
            ("error", self.error_names),
        ):
            if tuple(sorted(set(names))) != names:
                raise ValueError(f"{label} callable names must be sorted and unique")
        expected = (
            SafetyStatus.ERROR
            if self.error_names
            else SafetyStatus.INVALID
            if self.invalid_names
            else SafetyStatus.FAIL
            if self.failed_names
            else SafetyStatus.NOT_EVALUATED
            if self.unbound_names or self.unprobed_names
            else SafetyStatus.PASS
        )
        if self.status is not expected:
            raise ValueError("catalog audit status does not match callable probe outcomes")
        validate_evidence_refs(self.evidence_refs)


def audit_callable_tool_catalog(
    baseline: NativeToolCatalog,
    current: NativeToolCatalog,
    coverage: tuple[AdapterOwnedToolCoverage, ...] = (),
) -> CallableToolCatalogAudit:
    """Audit source-evolved callable definitions against exact adapter-owned coverage.

    Removing a callable tool requires no new coverage.  An added tool or a
    same-name schema change requires an exact coverage record and an actual
    adapter-owned probe outcome; catalog presence by itself is not evaluable.
    """
    delta = compare_native_tool_catalogs(baseline, current)
    coverage_by_schema: dict[tuple[str, str], list[AdapterOwnedToolCoverage]] = {}
    for item in coverage:
        coverage_by_schema.setdefault((item.name, item.canonical_schema), []).append(item)
    unbound_names = tuple(
        sorted(
            tool.name
            for tool in delta.introduced_or_changed
            if (tool.name, tool.canonical_schema) not in coverage_by_schema
        )
    )
    outcomes: dict[str, SafetyStatus] = {}
    priority = {
        SafetyStatus.PASS: 0,
        SafetyStatus.NOT_EVALUATED: 1,
        SafetyStatus.FAIL: 2,
        SafetyStatus.INVALID: 3,
        SafetyStatus.ERROR: 4,
    }
    for tool in delta.introduced_or_changed:
        matching = coverage_by_schema.get((tool.name, tool.canonical_schema), ())
        if matching:
            permission = tuple(
                item for item in matching if item.probe_scope == PAIRED_PERMISSION_PROBE
            )
            dispatch = tuple(item for item in matching if item.probe_scope == DISPATCH_PROBE)
            if permission:
                outcomes[tool.name] = max(
                    (item.probe_status for item in permission), key=priority.__getitem__
                )
            else:
                dispatch_status = max(
                    (item.probe_status for item in dispatch), key=priority.__getitem__
                )
                # Route failures are real failures even before permission semantics
                # are known.  A successful dispatch alone cannot establish that the
                # new callable preserved protected-vs-allowed behavior.
                outcomes[tool.name] = (
                    dispatch_status
                    if dispatch_status
                    in {SafetyStatus.FAIL, SafetyStatus.INVALID, SafetyStatus.ERROR}
                    else SafetyStatus.NOT_EVALUATED
                )
    unprobed_names = tuple(
        name for name in sorted(outcomes) if outcomes[name] is SafetyStatus.NOT_EVALUATED
    )
    failed_names = tuple(
        name for name in sorted(outcomes) if outcomes[name] is SafetyStatus.FAIL
    )
    invalid_names = tuple(
        name for name in sorted(outcomes) if outcomes[name] is SafetyStatus.INVALID
    )
    error_names = tuple(
        name for name in sorted(outcomes) if outcomes[name] is SafetyStatus.ERROR
    )
    evidence_refs = tuple(
        dict.fromkeys(
            (
                baseline.raw_catalog_ref,
                current.raw_catalog_ref,
                *(tool.raw_schema_ref for tool in delta.introduced_or_changed),
                *(item.raw_coverage_ref for item in coverage),
                *(ref for item in coverage for ref in item.probe_evidence_refs),
            )
        )
    )
    return CallableToolCatalogAudit(
        status=(
            SafetyStatus.ERROR
            if error_names
            else SafetyStatus.INVALID
            if invalid_names
            else SafetyStatus.FAIL
            if failed_names
            else SafetyStatus.NOT_EVALUATED
            if unbound_names or unprobed_names
            else SafetyStatus.PASS
        ),
        delta=delta,
        unbound_names=unbound_names,
        coverage=coverage,
        evidence_refs=evidence_refs,
        unprobed_names=unprobed_names,
        failed_names=failed_names,
        invalid_names=invalid_names,
        error_names=error_names,
    )
