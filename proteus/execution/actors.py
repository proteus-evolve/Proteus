"""Generic chat calls and compatibility execution through existing harness adapters."""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from proteus.core.adapter import EpisodeSpec
from proteus.core.contracts import TEXT, OperationStatus, RuntimeIdentity, Usage, require
from proteus.core.methods import ActorResult
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
        with urllib.request.urlopen(wire, timeout=self.options.timeout_seconds) as response:
            body = read_json(response.read().decode("utf-8"))
        message = body["choices"][0]["message"]
        require(not message.get("tool_calls"), "chat response requested unsupported tools")
        text = message["content"]
        require(isinstance(text, str), "chat response content must be text")
        usage = body.get("usage") or {}
        return ActorResult(text=text, runtime=RuntimeIdentity(runtime=spec.runtime.plugin,
            model=body.get("model"), settings={"requested_model": spec.model, **spec.model_settings}),
            usage=Usage(model_calls=1, input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                        wall_seconds=time.monotonic() - started))


@dataclass(frozen=True, kw_only=True)
class AdapterOptions(Contract):
    image: str | None = field(default=None, metadata=TEXT)
    network: str = "host"
    phase_timeout_seconds: int | None = field(default=None, metadata={"minimum": 1})
    auth_file: str | None = field(default=None, metadata=TEXT)
    base_url: str | None = field(default=None, metadata=TEXT)
    key_env: str | None = field(default=None, metadata={"pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"})


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
    require(spec.budget.max_model_calls is None, "legacy harness call accounting has no hard global bound")
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
        elif self.name == "llm":
            if options.base_url is not None:
                args["base_url"] = options.base_url
            if options.key_env is not None:
                require(bool(os.environ.get(options.key_env)), "missing LLM harness credential")
                args["key"] = os.environ[options.key_env]
        return harness_class(self.name)(**args)

    def execute(self, spec, request, workspace: Path) -> ActorResult:
        require(spec.session.value == "fresh" and request.session_id is None and request.handoff is None,
                "generic harness actor calls require fresh workspaces; native method owns continuity")
        adapter = self.adapter(spec)
        require(getattr(adapter, "supports_custom_phases", False), "harness needs custom-phase support")
        workspace.mkdir(parents=True, exist_ok=True)
        harness = workspace / "harness"
        harness.mkdir(exist_ok=True)
        adapter.seed(harness, self.options.seed)
        active = None
        if getattr(adapter, "staged_activation", False):
            active = workspace / "active"
            shutil.copytree(harness, active)
        prompt = (request.system + "\n\n" if request.system else "") + request.prompt
        started = time.monotonic()
        result = adapter.run_episode(EpisodeSpec(root=workspace, episode=1, model=spec.model or "",
            phase_prompts={"act": prompt}, phases=("act",), max_turns=self.options.max_turns,
            seed=self.options.seed, active_root=active,
            continuity_mode=getattr(adapter, "continuity_mode", "native")))
        trace = tuple(asdict(event) for event in adapter.read_trace(workspace, 1))
        text = "\n".join(event["text"] for event in trace if event.get("text"))
        return ActorResult(text=text, trace=trace,
            runtime=RuntimeIdentity(runtime=spec.runtime.plugin,
                                    settings={"requested_model": spec.model, "harness": self.name}),
            usage=Usage(input_tokens=result.counters.get("tokens_in"),
                        output_tokens=result.counters.get("tokens_out"),
                        wall_seconds=time.monotonic() - started),
            status=OperationStatus.COMPLETED if result.ok else OperationStatus.FAILED,
            diagnostic=result.error)
