from __future__ import annotations

import pytest

from proteus.core.snapshot import SnapshotRef, SnapshotRole
from proteus.safety.taxonomy import SafetyStatus
from proteus.safety.tool_catalog import (
    DISPATCH_PROBE,
    AdapterOwnedToolCoverage,
    NativeToolCatalog,
    NativeToolSchema,
    audit_callable_tool_catalog,
    compare_native_tool_catalogs,
    native_schema_accepts_exact_empty_object,
)


def _snapshot(episode: int, role: SnapshotRole) -> SnapshotRef:
    return SnapshotRef(run_id="catalog-run", episode=episode, role=role)


def _tool(name: str, schema: dict[str, object], ref: str) -> NativeToolSchema:
    return NativeToolSchema.from_schema(name=name, schema=schema, raw_schema_ref=ref)


def _catalog(
    snapshot: SnapshotRef, *tools: NativeToolSchema, ref: str
) -> NativeToolCatalog:
    return NativeToolCatalog(
        snapshot=snapshot,
        loader_id="ordinary-agent-tool-registry",
        tools=tuple(sorted(tools, key=lambda tool: tool.name)),
        raw_catalog_ref=ref,
    )


def test_compare_native_tool_catalogs_uses_full_canonical_schema() -> None:
    baseline = _catalog(
        _snapshot(3, SnapshotRole.ACTIVE),
        _tool("read", {"name": "read", "parameters": {"path": {"type": "string"}}}, "raw/base.json"),
        _tool("removed", {"name": "removed"}, "raw/base.json"),
        _tool("write", {"name": "write", "parameters": {"path": {"type": "string"}}}, "raw/base.json"),
        ref="raw/base.json",
    )
    current = _catalog(
        _snapshot(4, SnapshotRole.CANDIDATE),
        _tool("added", {"name": "added", "parameters": {"body": {"type": "string"}}}, "raw/current.json"),
        _tool("read", {"parameters": {"path": {"type": "string"}}, "name": "read"}, "raw/current.json"),
        _tool("write", {"name": "write", "parameters": {"path": {"type": "string"}, "mode": {"enum": ["safe"]}}}, "raw/current.json"),
        ref="raw/current.json",
    )

    delta = compare_native_tool_catalogs(baseline, current)

    assert [tool.name for tool in delta.added] == ["added"]
    assert [tool.name for tool in delta.removed] == ["removed"]
    assert [change.name for change in delta.changed] == ["write"]
    assert delta.changed[0].baseline.canonical_schema != delta.changed[0].current.canonical_schema


def test_audit_marks_exact_uncovered_source_evolved_callables_not_evaluated() -> None:
    baseline = _catalog(
        _snapshot(3, SnapshotRole.ACTIVE),
        _tool("write", {"name": "write", "parameters": {}}, "raw/base.json"),
        ref="raw/base.json",
    )
    current = _catalog(
        _snapshot(4, SnapshotRole.CANDIDATE),
        _tool("deploy", {"name": "deploy", "parameters": {"target": {"type": "string"}}}, "raw/current.json"),
        _tool("write", {"name": "write", "parameters": {"force": {"type": "boolean"}}}, "raw/current.json"),
        ref="raw/current.json",
    )

    audit = audit_callable_tool_catalog(baseline, current)

    assert audit.status is SafetyStatus.NOT_EVALUATED
    assert audit.unbound_names == ("deploy", "write")
    assert audit.evidence_refs == ("raw/base.json", "raw/current.json")


def test_audit_passes_only_with_adapter_coverage_for_exact_new_and_changed_schemas() -> None:
    baseline = _catalog(
        _snapshot(3, SnapshotRole.ACTIVE),
        _tool("write", {"name": "write", "parameters": {}}, "raw/base.json"),
        ref="raw/base.json",
    )
    deploy = {"name": "deploy", "parameters": {"target": {"type": "string"}}}
    write = {"name": "write", "parameters": {"force": {"type": "boolean"}}}
    current = _catalog(
        _snapshot(4, SnapshotRole.CANDIDATE),
        _tool("deploy", deploy, "raw/current.json"),
        _tool("write", write, "raw/current.json"),
        ref="raw/current.json",
    )
    coverage = (
        AdapterOwnedToolCoverage.from_schema(
            name="deploy",
            schema=deploy,
            adapter_name="dsh",
            native_mechanism="profile tool registry",
            raw_coverage_ref="raw/dsh-dispatch.json",
            probe_status=SafetyStatus.PASS,
        ),
        AdapterOwnedToolCoverage.from_schema(
            name="write",
            schema=write,
            adapter_name="dsh",
            native_mechanism="profile tool registry",
            raw_coverage_ref="raw/dsh-dispatch.json",
            probe_status=SafetyStatus.PASS,
        ),
    )

    audit = audit_callable_tool_catalog(baseline, current, coverage)

    assert audit.status is SafetyStatus.PASS
    assert audit.unbound_names == ()
    assert audit.evidence_refs == (
        "raw/base.json",
        "raw/current.json",
        "raw/dsh-dispatch.json",
    )


def test_changed_tool_requires_coverage_for_the_new_schema_not_just_its_name() -> None:
    baseline = _catalog(
        _snapshot(3, SnapshotRole.ACTIVE),
        _tool("write", {"name": "write", "parameters": {}}, "raw/base.json"),
        ref="raw/base.json",
    )
    current = _catalog(
        _snapshot(4, SnapshotRole.CANDIDATE),
        _tool("write", {"name": "write", "parameters": {"force": {"type": "boolean"}}}, "raw/current.json"),
        ref="raw/current.json",
    )
    stale_coverage = AdapterOwnedToolCoverage.from_schema(
        name="write",
        schema={"name": "write", "parameters": {}},
        adapter_name="dsh",
        native_mechanism="profile tool registry",
        raw_coverage_ref="raw/old-dispatch.json",
    )

    audit = audit_callable_tool_catalog(baseline, current, (stale_coverage,))

    assert audit.status is SafetyStatus.NOT_EVALUATED
    assert audit.unbound_names == ("write",)


def test_catalog_rejects_absolute_controller_evidence_reference() -> None:
    with pytest.raises(ValueError, match="relative"):
        NativeToolSchema.from_schema(
            name="write", schema={"name": "write"}, raw_schema_ref="/tmp/request.json"
        )


@pytest.mark.parametrize(
    "schema",
    (
        {"name": "ping", "parameters": {"type": "object"}},
        {
            "type": "function",
            "function": {
                "name": "ping",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ),
)
def test_empty_argument_probe_accepts_only_explicit_unconstrained_objects(
    schema: dict[str, object],
) -> None:
    tool = _tool("ping", schema, "raw/ping.json")

    assert native_schema_accepts_exact_empty_object(tool)


@pytest.mark.parametrize(
    "constraint",
    (
        {"required": ["path"]},
        {"minProperties": 1},
        {"allOf": [{"required": ["path"]}]},
        {"oneOf": [{"type": "object"}]},
        {"not": {}},
        {"$ref": "#/$defs/arguments"},
    ),
)
def test_empty_argument_probe_rejects_required_or_ambiguous_constraints(
    constraint: dict[str, object],
) -> None:
    tool = _tool(
        "ping",
        {"name": "ping", "parameters": {"type": "object", **constraint}},
        "raw/ping.json",
    )

    assert not native_schema_accepts_exact_empty_object(tool)


def test_dispatch_only_success_does_not_claim_permission_safety() -> None:
    baseline = _catalog(_snapshot(0, SnapshotRole.ACTIVE), ref="raw/base.json")
    schema = {"name": "ping", "parameters": {"type": "object"}}
    current = _catalog(
        _snapshot(1, SnapshotRole.ACTIVE),
        _tool("ping", schema, "raw/current.json"),
        ref="raw/current.json",
    )
    coverage = AdapterOwnedToolCoverage.from_schema(
        name="ping",
        schema=schema,
        adapter_name="aki",
        native_mechanism="native dispatch",
        raw_coverage_ref="raw/dispatch.json",
        probe_status=SafetyStatus.PASS,
        probe_scope=DISPATCH_PROBE,
    )

    audit = audit_callable_tool_catalog(baseline, current, (coverage,))

    assert audit.status is SafetyStatus.NOT_EVALUATED
    assert audit.unprobed_names == ("ping",)


def test_dispatch_failure_remains_a_real_callable_route_failure() -> None:
    baseline = _catalog(_snapshot(0, SnapshotRole.ACTIVE), ref="raw/base.json")
    schema = {"name": "ping", "parameters": {"type": "object"}}
    current = _catalog(
        _snapshot(1, SnapshotRole.ACTIVE),
        _tool("ping", schema, "raw/current.json"),
        ref="raw/current.json",
    )
    coverage = AdapterOwnedToolCoverage.from_schema(
        name="ping",
        schema=schema,
        adapter_name="aki",
        native_mechanism="native dispatch",
        raw_coverage_ref="raw/dispatch.json",
        probe_status=SafetyStatus.FAIL,
        probe_scope=DISPATCH_PROBE,
        probe_reason="native handler returned an error",
    )

    audit = audit_callable_tool_catalog(baseline, current, (coverage,))

    assert audit.status is SafetyStatus.FAIL
    assert audit.failed_names == ("ping",)
