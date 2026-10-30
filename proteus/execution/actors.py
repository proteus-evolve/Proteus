"""Generic chat calls and compatibility execution through existing harness adapters."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from proteus.core.adapter import EpisodeSpec
from proteus.core.contracts import TEXT, OperationStatus, RuntimeIdentity, Usage, require
from proteus.core.methods import ActorFailed, ActorResult
from proteus.core.native_usage import chat_usage
from proteus.core.serialization import Contract, json_value, read_json


@dataclass(frozen=True, kw_only=True)
class ChatOptions(Contract):
    base_url: str = field(metadata=TEXT)
    key_env: str = field(default="PROTEUS_LLM_KEY", metadata={"pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"})
    timeout_seconds: float = field(default=180, metadata={"exclusiveMinimum": 0})

    def _validate(self):
        url = urlsplit(self.base_url)
        require(url.scheme in ("http", "https") and url.hostname is not None,
                "chat base_url must be HTTP(S)")
        require(url.username is None and url.password is None and not url.query and not url.fragment,
                "chat base_url cannot contain credentials, query or fragment")


def validate_chat(spec, options):
    require(spec.model is not None, "chat actor requires a resolved model")
    require(not spec.tools, "chat actor does not implement a tool loop")
    require(spec.session.value == "fresh", "chat actor currently supports fresh requests")
    require(not (set(spec.model_settings) & {"model", "messages", "stream", "tools", "tool_choice"}),
            "model settings cannot replace model/messages or enable streaming/tools")


class ChatActor:
    model_call_bound = 1

    def __init__(self, options: ChatOptions):
        self.options = options

    def execute(self, spec, request, workspace: Path) -> ActorResult:
        validate_chat(spec, self.options)
        require(request.session_id is None and request.handoff is None, "chat actor needs fresh context")
        key = os.environ.get(self.options.key_env, "")
        require(bool(key), f"missing chat credential environment variable: {self.options.key_env}")
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        payload = {**json_value(spec.model_settings), "model": spec.model, "messages": messages}
        wire = urllib.request.Request(self.options.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload, allow_nan=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        started = time.monotonic()
        # A single request: retries, if requested, belong to the stage/attempt service.
        timeout = min(self.options.timeout_seconds, request.timeout_seconds or self.options.timeout_seconds)
        with urllib.request.urlopen(wire, timeout=timeout) as response:
            body = read_json(response.read().decode("utf-8"))
        # A response can report billable work even when its answer cannot be used.
        # Normalize each count independently before validating the answer envelope.
        usage = Usage.from_dict(chat_usage(body.get('usage') if isinstance(body, dict) else None)['reported'])
        runtime = RuntimeIdentity(runtime=spec.runtime.plugin,
            settings={"requested_model": spec.model, **spec.model_settings})
        try:
            require(isinstance(body, dict), "chat response must be an object")
            model = body.get('model')
            require(model is None or (isinstance(model, str) and bool(model)),
                    "chat response model must be a nonempty string")
            runtime = replace(runtime, model=model)
            choices = body.get('choices')
            require(isinstance(choices, list) and choices and isinstance(choices[0], dict),
                    "chat response needs an assistant choice")
            message = choices[0].get('message')
            require(isinstance(message, dict), "chat response needs an assistant message")
            require(not message.get("tool_calls"), "chat response requested unsupported tools")
            text = message.get('content')
            require(isinstance(text, str), "chat response content must be text")
            finish_reason = choices[0].get('finish_reason')
            require(finish_reason is None or (isinstance(finish_reason, str) and bool(finish_reason)),
                    "chat finish_reason must be a nonempty string")
            return ActorResult(text=text, runtime=runtime,
                usage=replace(usage, wall_seconds=time.monotonic() - started), finish_reason=finish_reason)
        except Exception as exc:
            raise ActorFailed(ActorResult(text='', runtime=runtime, status=OperationStatus.FAILED,
                usage=replace(usage, wall_seconds=time.monotonic() - started),
                diagnostic=f'{type(exc).__name__}: {exc}')) from exc


@dataclass(frozen=True, kw_only=True)
class AdapterOptions(Contract):
    image: str | None = field(default=None, metadata=TEXT)
    network: str = "host"
    phase_timeout_seconds: int | None = field(default=None, metadata={"minimum": 1})
    auth_file: str | None = field(default=None, metadata=TEXT)
    base_url: str | None = field(default=None, metadata=TEXT)
    key_env: str | None = field(default=None, metadata={"pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"})
    model_selection: Literal["legacy", "framework", "runtime"] = "legacy"


@dataclass(frozen=True, kw_only=True)
class HarnessOptions(AdapterOptions):
    max_turns: int = field(default=20, metadata={"minimum": 1})
    seed: int = 0


def harness_class(name):
    # Reuse the existing adapter mapping, including user-registered HarnessAdapters.
    from proteus.cli import _adapter_factory
    return _adapter_factory(name)


def validate_harness(name, spec, options):
    require(not spec.tools, "legacy harnesses cannot enforce a restricted actor tool allowlist")
    require(not spec.model_settings, "legacy harnesses do not accept generic model_settings")
    require(spec.budget.max_model_calls is None or spec.runtime.plugin.name in
            ('proteus.native.minimal', 'proteus.native.llm'),
            "legacy harness call accounting has no hard global bound")
    require(name == "dsh" or options.model_selection != "runtime",
            "runtime-owned model selection is currently supported only by DSH")
    if name == "dsh" and options.model_selection == "runtime":
        require(spec.model is None, "runtime-owned DSH model selection requires model=null")
    else:
        require(name == "minimal" or spec.model is not None, "live harness recipes require an explicit model")
    if name not in ("codex", "dsh", "pi"):
        require(options.image is None and options.network == "host"
                and options.phase_timeout_seconds is None,
                "this harness has no configurable container environment")
    require(name == "codex" or options.auth_file is None, "auth_file is a Codex harness option")
    require(name == "llm" or (options.base_url is None and options.key_env is None),
            "base_url/key_env apply to the LLM harness")


class HarnessActor:
    model_call_bound = None
    supports_workspace = False

    def __init__(self, name, options):
        self.name, self.options = name, options

    def adapter(self, spec):
        validate_harness(self.name, spec, self.options)
        options = self.options
        args = {}
        if self.name in ("codex", "dsh", "pi"):
            args = {"network": options.network}
            if options.phase_timeout_seconds is not None:
                args["phase_timeout_s"] = options.phase_timeout_seconds
            if options.image is not None:
                args["image"] = options.image
            if self.name == "codex" and options.auth_file is not None:
                args["auth_file"] = options.auth_file
            if self.name == "dsh" and self.model_selection(spec)["owner"] == "framework":
                args["model"] = spec.model
        elif self.name == "llm":
            if options.base_url is not None:
                args["base_url"] = options.base_url
            if options.key_env is not None:
                require(bool(os.environ.get(options.key_env)), "missing LLM harness credential")
                args["key"] = os.environ[options.key_env]
        return harness_class(self.name)(**args)

    def model_selection(self, spec):
        policy = self.options.model_selection
        owner = "framework"
        if self.name == "dsh" and (policy == "runtime" or (policy == "legacy" and not self.supports_workspace)):
            owner = "runtime"
        return {"policy": policy, "owner": owner,
                "requested_model": spec.model,
                "applied_model": spec.model if owner == "framework" else None,
                "resolved_model": None}

    def execute(self, spec, request, workspace: Path) -> ActorResult:
        return self._execute(spec, request, workspace, prepared_candidate=False)

    def execute_candidate(self, spec, request, workspace: Path) -> ActorResult:
        """Execute a materialized candidate; never seed over its editable state."""
        require(not self.supports_workspace, 'candidate execution is not a coding workspace call')
        return self._execute(spec, request, workspace, prepared_candidate=True)

    def _execute(self, spec, request, workspace: Path, *, prepared_candidate) -> ActorResult:
        require(spec.session.value == "fresh" and request.session_id is None and request.handoff is None,
                "generic harness actor calls require fresh workspaces; native method owns continuity")
        require((request.workspace is not None) == self.supports_workspace,
                'coding workspace requests require a workspace-capable actor')
        from proteus.core.native_inputs import read_native_inputs
        require(not read_native_inputs(workspace), 'generic harness actor requires fresh receipt identity')
        adapter = self.adapter(spec)
        require(getattr(adapter, "supports_custom_phases", False), "harness needs custom-phase support")
        workspace.mkdir(parents=True, exist_ok=True)
        harness = workspace / "harness"
        if prepared_candidate:
            require(harness.is_dir() and not harness.is_symlink(), 'candidate harness was not materialized')
            prepare = getattr(adapter, 'prepare_candidate', None)
            if prepare is not None:
                prepare(harness)
            validate = getattr(adapter, 'validate_candidate', None)
            if validate is not None:
                reason = validate(harness)
                if reason:
                    from proteus.evaluation.runners import InvalidCandidate
                    raise InvalidCandidate(reason)
        else:
            harness.mkdir(exist_ok=True)
            adapter.seed(harness, self.options.seed)
        if self.supports_workspace:
            require(request.workspace.episode is None, 'ordinary workspace actor cannot execute an evolution episode')
            require(all((workspace / 'task' / name).is_dir() for name in ('input', 'history', 'output', 'scratch')),
                    'coding workspace was not prepared by the service')
            # This actor optimizes a supplied system, not its own runtime. Do not
            # inherit the native self-evolution protocol from the adapter seed.
            (harness / 'AGENTS.md').write_text(WORKSPACE_INSTRUCTIONS, encoding='utf-8')
        active = None
        if getattr(adapter, "staged_activation", False):
            active = workspace / "active"
            shutil.copytree(harness, active)
        prompt = (request.system + "\n\n" if request.system else "") + request.prompt
        started = time.monotonic()
        result, trace, failure = None, (), None
        try:
            result = adapter.run_episode(EpisodeSpec(root=workspace, episode=1, model=spec.model or "",
                phase_prompts={"act": prompt}, phases=("act",), max_turns=self.options.max_turns,
                seed=self.options.seed, active_root=active,
                readonly_task_paths=('input', 'history') if self.supports_workspace else (),
                continuity_mode=getattr(adapter, "continuity_mode", "native")))
            trace = tuple(asdict(event) for event in adapter.read_trace(workspace, 1))
        except Exception as exc:
            from proteus.core.recovery import RecoveryRequired
            if isinstance(exc, RecoveryRequired):
                raise
            failure = exc
        from proteus.execution.harness_usage import collect
        usage, marker, _ = collect(workspace, self.name)
        if usage is None:
            counters = result.counters if result is not None else {}
            usage = Usage(input_tokens=counters.get('tokens_in'), output_tokens=counters.get('tokens_out'))
        settings = {"requested_model": spec.model, "harness": self.name,
                    "model_selection": self.model_selection(spec)}
        if marker is not None:
            settings['native_usage'] = marker
        text = "\n".join(event["text"] for event in trace if event.get("text"))
        from proteus.core.continuity import _redact
        outcome = ActorResult(text=text, trace=trace, runtime=RuntimeIdentity(runtime=spec.runtime.plugin, settings=settings),
            usage=replace(usage, wall_seconds=time.monotonic() - started),
            status=OperationStatus.COMPLETED if failure is None and result.ok else OperationStatus.FAILED,
            diagnostic=_redact(f'{type(failure).__name__}: {failure}') if failure is not None else result.error)
        if failure is not None:
            raise ActorFailed(outcome) from failure
        return outcome


WORKSPACE_INSTRUCTIONS = '''You are a coding research actor working on the supplied task.
Read the current request for the method, output format and experimental protocol.
Your task directory is /workspace/task:
- input/: the supplied immutable starting files.
- history/: the explicitly disclosed historical files and view.json provenance.
- output/: write every deliverable here; this tree is captured after you finish.
- scratch/: disposable work and local checks, excluded from the returned artifact.
Input and history are read-only. Inspect as much of the supplied history as useful.
The controlling harness is infrastructure. Keep its source and configuration unchanged.
Your session is fresh; only the declared input/history files carry previous work.
'''


def validate_workspace_harness(name, spec, options):
    from proteus.execution.builds import image_pin
    validate_harness(name, spec, options)
    require(name in ('dsh', 'pi', 'codex'), 'coding workspaces require a container coding harness')
    require(spec.session.value == 'fresh', 'coding workspaces use fresh sessions')
    require(options.image is not None, 'coding workspace runtime needs an explicit immutable image')
    # Locally built production images have a content-addressed image ID before
    # they have a registry manifest digest. Both identify immutable Docker input.
    if re.fullmatch(r'sha256:[0-9a-f]{64}', options.image) is None:
        image_pin(options.image)
    require(options.network in ('none', 'bridge'), 'coding workspace runtime uses none or bridge networking')
    require(options.phase_timeout_seconds is not None, 'coding workspace runtime needs an explicit phase timeout')


class WorkspaceHarnessActor(HarnessActor):
    supports_workspace = True

    def adapter(self, spec):
        validate_workspace_harness(self.name, spec, self.options)
        return super().adapter(spec)
