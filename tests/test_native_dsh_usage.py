"""DSH accounting excludes fork history and preserves missing auxiliary usage."""

import json

import pytest

from check_native_dsh_package import check, compress, events, raw
from proteus.core.native_dsh_usage import capture_dsh_sources
from proteus.core.native_pi_usage import fingerprint
from proteus.core.native_usage import dsh_usage, validate_usage
from proteus.core.serialization import ContractError


def evidence(root, values=None, *, header=None, complete=True):
    path = root / "session.jsonl.zstd"
    path.write_bytes(compress(raw(values, header=header)))
    return dsh_usage(capture_dsh_sources([path], {}, root=root), transport_complete=complete)


def test_actual_adapter_keeps_two_attempts_with_replaced_step_usage():
    check()


@pytest.mark.parametrize(
    "field", ["inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"]
)
@pytest.mark.parametrize("value", [None, -1, True, 1.5, "3"])
def test_invalid_components_keep_only_the_other_dimension_known(tmp_path, field, value):
    values = events()
    target = next(
        event["data"]["usage"] for event in values if event["type"] == "assistant/message"
    )
    target[field] = value
    result = evidence(tmp_path, values)
    unknown = "output_tokens" if field == "outputTokens" else "input_tokens"
    other = "input_tokens" if unknown == "output_tokens" else "output_tokens"
    assert result["reported"][unknown] is None and result["reported"][other] > 0
    assert result["fields"]["unknown_samples"][unknown] == 1


def test_reported_zero_and_omitted_optional_cache_fields_are_valid(tmp_path):
    values = events()
    for event in values:
        if "usage" in event["data"]:
            event["data"]["usage"] = {"inputTokens": 0, "outputTokens": 0}
    result = evidence(tmp_path, values)
    assert result["reported"]["input_tokens"] == result["reported"]["output_tokens"] == 0


def test_fork_seed_is_excluded_but_later_lifecycle_marker_does_not_erase_live_work(tmp_path):
    seed = events()
    live = events()
    live.insert(0, {"type": "session/end-seed", "data": {}})
    live.append({"type": "session/end-seed", "data": {}})
    result = evidence(
        tmp_path,
        [*seed, *live],
        header={"parentSession": "parent", "seedLength": len(seed), "origin": "subagent"},
    )
    assert result["reported"]["input_tokens"] == 24 and result["reported"]["output_tokens"] == 7
    assert result["fields"]["seed_events"] == len(seed)


@pytest.mark.parametrize(
    "tag", ["session/title-llm-request", "web/deepseek-search-llm-request", "llm/retry"]
)
def test_auxiliary_requests_and_failed_retries_are_not_silently_discarded(tmp_path, tag):
    values = events() + [{"type": tag, "data": {"retryId": "retry"}}]
    result = evidence(tmp_path, values)
    assert (
        result["reported"]["input_tokens"] is None and result["reported"]["output_tokens"] is None
    )
    assert result["fields"]["known_tokens"] == {"input_tokens": 24, "output_tokens": 7}
    assert result["fields"]["samples"]["unreported"] == 1


@pytest.mark.parametrize(
    "mutation", ["missing_summary_usage", "missing_all_step_usage", "failed_summary"]
)
def test_missing_usage_never_shrinks_recorded_total(tmp_path, mutation):
    values = events()
    for event in values:
        if mutation == "missing_summary_usage" and event["type"] == "compaction/summary":
            event["data"].pop("usage")
        elif mutation == "missing_all_step_usage":
            if event["type"] == "assistant/message":
                event["data"].pop("usage")
            if event["type"] == "assistant/chunk" and event["data"]["chunk"]["type"] == "usage":
                event["data"]["chunk"].pop("usage")
        elif mutation == "failed_summary" and event["type"] == "compaction/end":
            event["data"]["error"] = "failed"
    result = evidence(tmp_path, values)
    assert (
        result["reported"]["input_tokens"] is None and result["reported"]["output_tokens"] is None
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "late_usage",
        "missing_finish",
        "missing_end",
        "failed_turn",
        "unbound_retry",
        "unknown_event",
        "fork_no_boundary",
    ],
)
def test_unfinished_or_unsupported_logs_cannot_claim_complete_totals(tmp_path, mutation):
    values = events()
    header = {}
    if mutation == "late_usage":
        values.append(values[2])
    elif mutation == "missing_finish":
        values = [
            event
            for event in values
            if event["type"] != "assistant/chunk" or event["data"]["chunk"]["type"] != "finish"
        ]
    elif mutation == "missing_end":
        values = [event for event in values if event["type"] != "turn/end"]
    elif mutation == "failed_turn":
        next(event["data"] for event in values if event["type"] == "turn/end")["reason"] = {
            "kind": "error"
        }
    elif mutation == "unbound_retry":
        values.append({"type": "llm/retry-started", "data": {"retryId": "missing"}})
    elif mutation == "unknown_event":
        values.append({"type": "future/usage", "data": {}})
    else:
        header = {"parentSession": "parent"}
    result = evidence(tmp_path, values, header=header)
    assert result["fields"]["stream_status"] in ("invalid", "incomplete")
    assert result["reported"]["input_tokens"] is None


def test_changed_prior_files_or_failed_transport_remain_unknown(tmp_path):
    path = tmp_path / "session.jsonl.zstd"
    path.write_bytes(compress(raw()))
    previous = {path: fingerprint(path)}
    path.write_bytes(path.read_bytes() + b"x")
    result = dsh_usage(capture_dsh_sources([], previous, root=tmp_path), transport_complete=True)
    assert result["reported"]["input_tokens"] is None
    assert evidence(tmp_path, complete=False)["reported"]["input_tokens"] is None


def test_symlinked_parent_is_not_read_into_a_private_bundle(tmp_path):
    external = tmp_path / "private"
    external.mkdir()
    (external / "session.jsonl.zstd").write_bytes(b"PRIVATE-FILE")
    root = tmp_path / "state"
    root.mkdir()
    (root / "link").symlink_to(external, target_is_directory=True)
    source = capture_dsh_sources([root / "link/session.jsonl.zstd"], {}, root=root)
    assert json.loads(source)["logs"][0]["data"] is None
    assert dsh_usage(source, transport_complete=True)["reported"]["input_tokens"] is None


@pytest.mark.parametrize('prefix', [
    ['--profile', 'headless'], ['--profile', 'headless', '--', '--'],
])
def test_source_accepts_historical_and_delimited_fresh_commands(tmp_path, prefix):
    usage = evidence(tmp_path)
    assert validate_usage(usage, {'scope': 'harness_cli', 'payload': [*prefix, '--- literal prompt']}) == usage


@pytest.mark.parametrize('prefix', [
    ['--profile', 'headless', '--resume'],
    ['--profile', 'headless', '--'],
    ['--profile', 'headless', '--resume', '--', '--'],
])
def test_source_requires_fresh_headless_command(tmp_path, prefix):
    with pytest.raises(ContractError, match="fresh headless"):
        validate_usage(
            evidence(tmp_path),
            {"scope": "harness_cli", "payload": [*prefix, "prompt"]},
        )


@pytest.mark.parametrize("failure", ["timeout", "exit"])
def test_failed_adapter_preserves_source_and_rejects_source_corruption(tmp_path, failure):
    import subprocess
    from check_native_dsh_package import Sandbox
    from proteus.adapters.dsh import DshHarness
    from proteus.core.adapter import EpisodeSpec
    from proteus.core.native_inputs import input_directory, read_native_inputs
    from proteus.core.native_usage import summarize_native_inputs

    class FailedSandbox(Sandbox):
        def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
            super().run(root, command, env, timeout_s, mounts, stop_check)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, timeout_s)
            return subprocess.CompletedProcess(command, 1, "", "")

    (tmp_path / "harness").mkdir()
    result = DshHarness(key="fixture", sandbox=FailedSandbox()).run_episode(
        EpisodeSpec(
            root=tmp_path,
            episode=1,
            model="fixture",
            phases=("act",),
            phase_prompts={"act": "fixture"},
        )
    )
    assert not result.ok
    records = read_native_inputs(tmp_path)
    assert records[0]["completion"] == ("raised" if failure == "timeout" else "returned")
    assert summarize_native_inputs(records)["reported"]["input_tokens"] is None
    path = next(input_directory(tmp_path).glob("*.usage-source"))
    path.write_bytes(b"{}")
    with pytest.raises(ContractError, match="source differs"):
        read_native_inputs(tmp_path)


def test_parent_and_child_logs_share_basenames_but_count_own_work_once(tmp_path):
    paths = []
    for identity in ("parent", "child"):
        path = tmp_path / identity / "session.jsonl.zstd"
        path.parent.mkdir()
        values = events()
        header = {}
        if identity == "child":
            header = {"parentSession": "parent", "seedLength": len(values), "origin": "subagent"}
            values = [*values, {"type": "session/end-seed", "data": {}}, *events()]
        path.write_bytes(compress(raw(values, identity=identity, header=header)))
        paths.append(path)
    result = dsh_usage(capture_dsh_sources(paths, {}, root=tmp_path), transport_complete=True)
    assert sorted(result["fields"]["session_ids"]) == ["child", "parent"]
    assert result["reported"]["input_tokens"] == 48 and result["reported"]["output_tokens"] == 14


def test_recipe_export_and_resume_preserve_single_dsh_attempt_privately(tmp_path):
    from dataclasses import replace
    from pathlib import Path
    from check_native_dsh_package import Sandbox
    from proteus.adapters.dsh import DshHarness
    from proteus.core.adapter import EpisodeSpec
    from proteus.core.contracts import OperationStatus, PluginRef
    from proteus.core.methods import MethodResult
    from proteus.core.native_inputs import read_native_inputs
    from proteus.recipes import load_recipe
    from proteus.recipes.builtins import builtin_registry
    from proteus.recipes.config import PluginSpec
    from proteus.recipes.export import report_run
    from proteus.recipes.runner import run_recipe
    from test_v1_plugins import plugin, SOURCE

    class Driver:
        def run(self, services, restored_state):
            root = services.root / "workspace"
            if not root.exists():
                (root / "harness").mkdir(parents=True)
                assert (
                    DshHarness(key="fixture", sandbox=Sandbox())
                    .run_episode(
                        EpisodeSpec(
                            root=root,
                            episode=1,
                            model="fixture",
                            phases=("act",),
                            phase_prompts={"act": "fixture"},
                        )
                    )
                    .ok
                )
            services.native_inputs = read_native_inputs(root)
            return MethodResult(status=OperationStatus.COMPLETED)

    registry = builtin_registry()
    registry.register(
        plugin("fixture.native_dsh", "method", lambda _: Driver(), capabilities=("run.resume",))
    )
    recipe = load_recipe(Path(__file__).resolve().parents[1] / "recipes/native/minimal.toml")
    recipe = replace(
        recipe,
        actors={},
        method=PluginSpec(plugin=PluginRef(name="fixture.native_dsh", version="1")),
    )
    lock = registry.make_lock(recipe, [SOURCE])
    root = tmp_path / "run"
    for resume in (False, True):
        assert (
            run_recipe(recipe, registry=registry, lock=lock, root=root, resume=resume).status
            == OperationStatus.COMPLETED
        )
    public, owner = report_run(root), report_run(root, include_private=True)
    assert public["native_accounting"]["boundary_count"] == 1
    assert public["native_accounting"]["reported"]["input_tokens"] == 24
    assert sum(path.endswith(".usage-source") for path in owner["evidence"]) == 1
    assert not any(path.endswith(".usage-source") for path in public["evidence"])
    assert "PRIVATE-SUMMARY" not in json.dumps(owner)
