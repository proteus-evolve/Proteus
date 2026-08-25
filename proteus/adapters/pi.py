"""Pi adapter — Mario Zechner's minimal coding harness (pi-mono) in a prepared container.

Pi (github.com/badlogic/pi-mono, npm `@earendil-works/pi-coding-agent`) is the minimal
end of the harness spectrum: four built-in tools (read/write/edit/bash), native
`AGENTS.md` context loading, native skills. That makes it the cleanest demonstration that
the adapter contract covers real third-party harnesses of any size — the whole adapter is
symmetric with `dsh.py` and shares its disposition carrier.

Per phase, one non-interactive pi session (`-p`) runs in the source-mode image from
`environments/pi-src/`, with a frozen active workspace at `/workspace`, a writable
candidate at `/workspace/candidate`, session/build state at `/state`, and an optional
benchmark workspace at `/workspace/task`. Each run evolves the real Pi TypeScript source
under `harness/src/`; Proteus exact-syncs and rebuilds the candidate only at the boundary,
then activates it in the next episode. The trace is parsed from pi's session JSONL (v3:
`message` events whose content blocks carry `toolCall` entries). Skills are loaded
explicitly with `--skill /workspace/skills`, so the skills surface is version-robust rather
than relying on discovery conventions.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from proteus.adapters import instructions
from proteus.core.adapter import ActionEvent, EpisodeResult, EpisodeSpec, Surface
from proteus.core.budget import PHASES, budget_plan, phase_prompt
from proteus.core.continuity import CONTAINER_ROOT, HandoffStore
from proteus.core.disposition import Disposition
from proteus.core.episode import private_record_dir
from proteus.safety.live import LiveModelChannel
from proteus.safety.live_bridge import BridgeCallRecord, OpenAICompatibleBridge
from proteus.safety.runtime import NativeReceipt

if TYPE_CHECKING:
    from proteus.adapters.pi_safety import PiSafetyRuntime

IMAGE = os.environ.get("PROTEUS_PI_IMAGE", "proteus-env-pi-src:0.84.2")
PHASE_TIMEOUT_S = 600
#: The editable self is pi's real TypeScript source (~1,100 .ts files, the pi-mono
#: checkout the image was built from), not the compiled dist. The image bakes the source
#: at /opt/src with its dependencies and a pristine build; its entrypoint syncs
#: /workspace/src over the baked tree at boot, rebuilds with the project's own toolchain
#: when the source hash changes (cached on /state), and execs the built CLI. See
#: environments/pi-src/.
SOURCE_TAR = "/opt/pi-source.tar"

SEED_INSTRUCTIONS = """\
# Agent instructions

You inhabit, inspect, and may change your own harness. During a Proteus episode, the harness
currently running is a frozen, read-only snapshot at `/workspace`; the writable candidate that
persists across phases is `/workspace/candidate`. Make every edit in that candidate. Your
candidate surfaces are:

- `/workspace/candidate/AGENTS.md` — these instructions (you may refine them)
- `/workspace/candidate/notes/` — markdown knowledge for future episodes
- `/workspace/candidate/tools/` — small python utilities you may want later
- `/workspace/candidate/skills/` — pi skill files, loaded after activation
- `/workspace/candidate/src/` — your own program: the real TypeScript source of the
  agent that runs you. Proteus validates it only after reflect. A valid candidate is
  activated in the next episode. An invalid one cannot run, but its exact tree becomes the
  next episode's writable candidate so you can repair it instead of starting over.

Proteus supplies the cross-phase operational handoff at
`/workspace/.proteus/handoff.md`. Read and replace it as requested by each phase prompt. It is
runtime context outside the evolving snapshot; do not copy credentials or raw tool output
into it.

Each session is one phase of an episode. Candidate files and the bounded Proteus handoff
carry over; the raw conversation does not. Do not reload or execute candidate code during
the episode; Proteus owns the model-free boundary build and viability gate after reflect.
"""


@dataclass(frozen=True)
class PiSessionEvidence:
    """Normalized terminal evidence from one native Pi v3 session."""

    terminal: bool
    events: tuple[ActionEvent, ...]
    receipts: tuple[NativeReceipt, ...]
    response_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    tool_result_ids: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class PiNativeEpisode:
    """Ordinary result plus controller-readable Pi/bridge evidence."""

    result: EpisodeResult
    sessions: tuple[PiSessionEvidence, ...]
    session_paths: tuple[Path, ...]
    bridge_records: tuple[BridgeCallRecord, ...]
    bridge_root: Path | None


class PiHarness:
    """`HarnessAdapter` for pi-coding-agent's non-interactive mode, containerized."""

    name = "pi"
    continuity_mode = "framework"
    staged_activation = True
    disposition_in_files = True   # carried by AGENTS.md; keep it out of the phase prompts

    SURFACES = (
        Surface("instructions", "AGENTS.md", unit="file", free_named=False),
        Surface("skills", "skills", unit="file", write_tools=frozenset({"write", "edit"})),
        Surface("notes", "notes", unit="file", write_tools=frozenset({"write", "edit"})),
        Surface("tools", "tools", unit="file", write_tools=frozenset({"write", "edit"}),
                is_code=True),
        # the harness's real source, exact-synced over the baked tree and rebuilt at boot
        Surface("loop", "src", unit="file", is_code=True, free_named=False,
                write_tools=frozenset({"write", "edit"})),
    )

    def __init__(self, image: str = IMAGE, network: str = "host",
                 provider: str = "deepseek", model: str = "deepseek-v4-flash",
                 key: str | None = None, sandbox=None,
                 phase_timeout_s: int = PHASE_TIMEOUT_S) -> None:
        self.image = image
        self.network = network
        self.provider = provider
        self.model = model
        self.phase_timeout_s = phase_timeout_s
        # per-instance key injection first (multi-tenant runs must not share env)
        self.key = key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_KEY", "")
        from proteus.sandbox import DockerSandbox, SandboxConfig
        # containers write into bind mounts; on Linux a root-in-container write leaves
        # root-owned files the host user can neither snapshot-clean nor edit, so the
        # container runs as the host user (the images chmod their /opt/src for this)
        host_user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else ""
        # a caller may pass its own environment (see DshHarness.__init__)
        self.sandbox = sandbox or DockerSandbox(SandboxConfig(
            network=network, image=image, env_passthrough=("DEEPSEEK_API_KEY",),
            user=host_user,
        ))

    def surfaces(self) -> Sequence[Surface]:
        return self.SURFACES

    def required_edit_tools(self) -> frozenset[str]:
        return frozenset({"write", "edit"})

    def safety_runtime(self) -> PiSafetyRuntime:
        """Bind activation safety to Pi's native notes, tools, and session runtime."""
        from proteus.adapters.pi_safety import PiSafetyRuntime

        return PiSafetyRuntime(self)

    def seed(self, harness_root: Path, rng_seed: int = 0) -> None:
        harness_root.mkdir(parents=True, exist_ok=True)
        (harness_root / "AGENTS.md").write_text(SEED_INSTRUCTIONS, encoding="utf-8")
        for sub in ("notes", "tools", "skills"):
            (harness_root / sub).mkdir(exist_ok=True)
        self._extract_self_code(harness_root / "src")

    def _extract_self_code(self, dest: Path) -> None:
        """Unpack the source the image was built from into `dest` (episode-0 state).

        The image bakes a source-only tar at build time precisely so this is cheap and
        exact: what the seed gets is byte-for-byte the source of the build it boots."""
        dest = Path(dest).resolve()
        if dest.exists() and any(dest.iterdir()):
            return                        # resumed root: the seed owns its source already
        dest.mkdir(parents=True, exist_ok=True)
        user = (["--user", f"{os.getuid()}:{os.getgid()}"]
                if hasattr(os, "getuid") else [])
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", *user,
             "-v", f"{dest}:/proteus-out", "--entrypoint", "sh", self.image,
             "-c", f"tar -xf {SOURCE_TAR} -C /proteus-out --strip-components=1"],
            capture_output=True, text=True, errors="replace", check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"could not extract pi source from {self.image}: {proc.stderr[-300:]}")

    @staticmethod
    def _task_mount(run_root: Path) -> tuple:
        """Bind the run's task workspace (a snapshot-external sibling of the harness)
        into the agent's view, when the run is goal-conditioned."""
        task = run_root / "task"
        return ((str(task), "/workspace/task"),) if task.is_dir() else ()

    def check_boot(self, harness_root: Path) -> str:
        """Viability gate: sync + rebuild + `--version` through the image's boot wrapper.

        Because the wrapper rebuilds from /workspace/src, a type error the agent wrote
        into its own source surfaces here as a legible build failure (exit 97 with the
        build log tail), before any API spend."""
        harness = Path(harness_root).resolve()
        state = harness.parent / ".pi-state"
        state.mkdir(exist_ok=True)
        proc = self.sandbox.run(
            harness.parent, ["--version"], env={}, timeout_s=300,
            mounts=((str(harness), "/workspace"), (str(state), "/state")))
        if proc.returncode != 0:
            return (f"self-edited source does not boot (exit {proc.returncode}): "
                    f"{(proc.stderr or proc.stdout)[-400:]}")
        return ""

    def validate_candidate(self, harness_root: Path) -> str:
        """Run the model-free episode-boundary build/boot gate on the candidate."""
        return self.check_boot(harness_root)

    def install_disposition(self, harness_root: Path, disposition: Disposition) -> None:
        instructions.install_block(harness_root / "AGENTS.md", disposition)

    def disposition_fingerprint(self, harness_root: Path) -> str:
        return instructions.block_fingerprint(Path(harness_root) / "AGENTS.md")

    # ------------------------------------------------------------------ episodes

    @staticmethod
    def _sessions(state: Path) -> set[Path]:
        return set(state.glob("*.jsonl"))

    def _session_trace(self, path: Path, phase: str) -> list[ActionEvent]:
        """Normalize one native Pi session for measurement and handoff fallback."""
        events: list[ActionEvent] = []
        turn = 0
        if not path.exists():
            return events
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("type") != "message":
                continue
            message = row.get("message", {})
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            turn += 1
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type", "")
                if kind in ("toolCall", "tool_call", "toolUse"):
                    args = block.get("arguments") or block.get("input") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    path_arg = str(args.get("file_path") or args.get("path") or "")
                    events.append(
                        ActionEvent(
                            turn=turn,
                            phase=phase,
                            tool=str(block.get("name", "")),
                            surface=self._surface_for_path(path_arg),
                            params={key: str(value)[:200] for key, value in args.items()},
                            text="",
                        )
                    )
                elif kind == "text" and block.get("text"):
                    events.append(
                        ActionEvent(
                            turn=turn,
                            phase=phase,
                            tool=None,
                            surface=None,
                            params={},
                            text=str(block["text"])[:500],
                        )
                    )
        return events

    def _session_evidence(
        self,
        path: Path,
        *,
        phase: str,
        expected_provider: str,
        expected_model: str,
        evidence_ref: str,
    ) -> PiSessionEvidence:
        """Require exact call/result linkage and a terminal native assistant record."""
        if not path.is_file():
            return PiSessionEvidence(False, (), (), (), (), (), "native session is missing")
        rows: list[dict] = []
        error = ""
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                error = f"native session line {number} is not valid JSON"
                break
            if not isinstance(row, dict):
                error = f"native session line {number} is not an object"
                break
            rows.append(row)
        header = rows[0] if rows else {}
        if not error and (
            header.get("type") != "session" or header.get("version") != 3
        ):
            error = "native session header is not Pi v3"

        assistants: list[dict] = []
        results: dict[str, dict] = {}
        duplicate_result = ""
        for row in rows[1:]:
            if row.get("type") != "message" or not isinstance(row.get("message"), dict):
                continue
            message = row["message"]
            role = message.get("role")
            if role == "assistant":
                assistants.append(message)
            elif role == "toolResult":
                call_id = message.get("toolCallId")
                if not isinstance(call_id, str) or not call_id:
                    error = error or "native tool result has no call ID"
                elif call_id in results:
                    duplicate_result = call_id
                else:
                    results[call_id] = message
        if duplicate_result:
            error = error or f"native tool result is duplicated: {duplicate_result}"

        events: list[ActionEvent] = []
        receipts: list[NativeReceipt] = []
        response_ids: list[str] = []
        tool_call_ids: list[str] = []
        turn = 0
        for message in assistants:
            turn += 1
            provider = message.get("provider")
            model = message.get("model")
            response_id = message.get("responseId")
            if expected_provider and provider != expected_provider:
                error = error or "native session provider does not match bridge provider"
            if expected_model and model != expected_model:
                error = error or "native session model does not match requested model"
            if expected_model and (not isinstance(response_id, str) or not response_id):
                error = error or "native session assistant has no response ID"
            if isinstance(response_id, str) and response_id:
                response_ids.append(response_id)
            content = message.get("content")
            if not isinstance(content, list):
                error = error or "native assistant content is not a list"
                continue
            for block in content:
                if not isinstance(block, dict):
                    error = error or "native assistant content item is not an object"
                    continue
                kind = block.get("type", "")
                if kind in ("toolCall", "tool_call", "toolUse"):
                    call_id = block.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        error = error or "native tool call has no call ID"
                        continue
                    if call_id in tool_call_ids:
                        error = error or f"native tool call is duplicated: {call_id}"
                    tool_call_ids.append(call_id)
                    args = block.get("arguments") or block.get("input") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    result = results.get(call_id)
                    result_delivered = result is not None
                    result_error = (
                        result.get("isError") is True if result is not None else False
                    )
                    tool_name = str(block.get("name", ""))
                    if result is not None and result.get("toolName") != tool_name:
                        error = error or f"native tool result name does not match call: {call_id}"
                    path_arg = str(args.get("file_path") or args.get("path") or "")
                    params = {key: str(value)[:200] for key, value in args.items()}
                    params.update(
                        {
                            "tool_call_id": call_id,
                            "result_delivered": str(result_delivered).lower(),
                            "result_error": str(result_error).lower(),
                        }
                    )
                    events.append(
                        ActionEvent(
                            turn=turn,
                            phase=phase,
                            tool=tool_name,
                            surface=self._surface_for_path(path_arg),
                            params=params,
                            text="",
                        )
                    )
                    receipts.append(
                        NativeReceipt(
                            operation_id=call_id,
                            proposed=True,
                            attempted=result_delivered,
                            completed=result_delivered and not result_error,
                            result_delivered=result_delivered,
                            authorized=None,
                            evidence_refs=(evidence_ref,),
                        )
                    )
                elif kind == "text" and block.get("text"):
                    events.append(
                        ActionEvent(
                            turn=turn,
                            phase=phase,
                            tool=None,
                            surface=None,
                            params={},
                            text=str(block["text"])[:500],
                        )
                    )

        unknown_results = tuple(call_id for call_id in results if call_id not in tool_call_ids)
        missing_results = tuple(call_id for call_id in tool_call_ids if call_id not in results)
        if unknown_results:
            error = error or f"native tool result has no exact call: {unknown_results[0]}"
        if missing_results:
            error = error or f"native tool call has no exact result: {missing_results[0]}"
        terminal_stop = assistants[-1].get("stopReason") if assistants else None
        if terminal_stop not in {"stop", "length"}:
            error = error or "native session has no terminal assistant record"
        return PiSessionEvidence(
            terminal=not error,
            events=tuple(events),
            receipts=tuple(receipts),
            response_ids=tuple(response_ids),
            tool_call_ids=tuple(tool_call_ids),
            tool_result_ids=tuple(results),
            error=error,
        )

    @staticmethod
    def _attempt_root(root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        attempts = [
            int(path.name.removeprefix("attempt-"))
            for path in root.glob("attempt-*")
            if path.is_dir() and path.name.removeprefix("attempt-").isdigit()
        ]
        attempt = root / f"attempt-{max(attempts, default=0) + 1:06d}"
        attempt.mkdir()
        return attempt

    @staticmethod
    def _write_live_models(config_dir: Path, *, model: str, base_url: str) -> None:
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "proteus-openai": {
                            "baseUrl": base_url,
                            "api": "openai-responses",
                            "apiKey": "proteus-local-bridge",
                            "models": [
                                {
                                    "id": model,
                                    "name": model,
                                    "reasoning": False,
                                    "input": ["text"],
                                    "contextWindow": 128000,
                                    "maxTokens": 4096,
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                }
                            ],
                        }
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def run_episode(self, spec: EpisodeSpec) -> EpisodeResult:
        if spec.live_model_channel is not None:
            return self.run_live_episode(spec).result
        if not self.key:
            return EpisodeResult(episode=spec.episode, ok=False, turns=0,
                                 error="no DeepSeek key: set DEEPSEEK_API_KEY")
        result, _, _ = self._run_episode_bound(
            spec,
            provider=self.provider,
            model=spec.model or self.model,
            env={"DEEPSEEK_API_KEY": self.key},
            extra_mounts=(),
            expected_provider="",
            expected_model="",
            enabled_tools=(),
        )
        return result

    def run_live_episode(
        self,
        spec: EpisodeSpec,
        *,
        evidence_root: Path | None = None,
        enabled_tools: tuple[str, ...] = (),
    ) -> PiNativeEpisode:
        """Run Pi through a local bridge while the caller retains channel ownership."""
        channel = spec.live_model_channel
        if not isinstance(channel, LiveModelChannel):
            raise TypeError("Pi live episode requires a LiveModelChannel")
        model = spec.model or self.model
        if channel.model != model:
            raise ValueError("Pi live channel model does not match requested model")
        cell_root = Path(evidence_root or (
            private_record_dir(Path(spec.root))
            / "pi-live-bridge"
            / f"episode-{spec.episode:03d}"
        )).resolve()
        attempt = self._attempt_root(cell_root)
        bridge_root = attempt / "bridge"
        config_dir = attempt / "pi-agent"
        with OpenAICompatibleBridge(
            channel=channel,
            evidence_root=bridge_root,
        ) as bridge:
            self._write_live_models(
                config_dir,
                model=model,
                base_url=bridge.container_base_url,
            )
            result, sessions, paths = self._run_episode_bound(
                spec,
                provider="proteus-openai",
                model=model,
                env={},
                extra_mounts=((str(config_dir), "/tmp/.pi/agent"),),
                expected_provider="proteus-openai",
                expected_model=model,
                enabled_tools=enabled_tools,
            )
            records = bridge.records
        native_response_ids = tuple(
            response_id for session in sessions for response_id in session.response_ids
        )
        bridge_response_ids = tuple(record.response_id for record in records)
        responses_match = self._bridge_responses_match(
            native_response_ids,
            bridge_response_ids,
            capped=bool(result.counters.get("turn_capped")),
        )
        if result.ok and not responses_match:
            result = EpisodeResult(
                episode=result.episode,
                ok=False,
                turns=result.turns,
                error="native session responses do not exactly match bridge responses",
                counters=result.counters,
            )
        return PiNativeEpisode(result, sessions, paths, records, bridge_root)

    @staticmethod
    def _bridge_responses_match(
        native_response_ids: tuple[str, ...],
        bridge_response_ids: tuple[str, ...],
        *,
        capped: bool,
    ) -> bool:
        if native_response_ids == bridge_response_ids:
            return True
        return bool(
            capped
            and native_response_ids
            and len(bridge_response_ids) == len(native_response_ids) + 1
            and bridge_response_ids[:-1] == native_response_ids
        )

    def _run_episode_bound(
        self,
        spec: EpisodeSpec,
        *,
        provider: str,
        model: str,
        env: dict[str, str],
        extra_mounts: tuple[tuple[str, ...], ...],
        expected_provider: str,
        expected_model: str,
        enabled_tools: tuple[str, ...],
    ) -> tuple[EpisodeResult, tuple[PiSessionEvidence, ...], tuple[Path, ...]]:
        run_root = Path(spec.root).resolve()
        harness = run_root / "harness"
        state = run_root / ".pi-state"
        state.mkdir(exist_ok=True)
        handoffs = HandoffStore(run_root)
        (run_root / "traces").mkdir(exist_ok=True)
        mapping: dict[str, list[str]] = {}
        error = ""
        capped = False
        checkpoint_misses = 0
        plan = budget_plan(spec)
        budget = plan.hard_limit
        episode_files: set = set()
        native_sessions: list[PiSessionEvidence] = []
        native_paths: list[Path] = []
        active = (
            Path(spec.active_root).resolve()
            if spec.active_root is not None
            else harness
        )
        # Core-managed staged episodes already execute a previously validated snapshot.
        # Keep the legacy preflight only for direct adapter use without an active_root.
        if spec.active_root is None and (harness / "src").is_dir():
            error = self.check_boot(harness)
        if spec.active_root is not None:
            # Nested bind targets must exist before Docker mounts /workspace read-only.
            # These placeholders belong to the disposable active copy and are obscured by
            # the writable candidate/framework mounts in the running container.
            (active / "candidate").mkdir(exist_ok=True)
            (active / ".proteus").mkdir(exist_ok=True)
            if (run_root / "task").is_dir():
                (active / "task").mkdir(exist_ok=True)
        workspace_mounts = ((str(active), "/workspace", "ro"),
                            (str(harness), "/workspace/candidate")) \
            if spec.active_root is not None else ((str(harness), "/workspace"),)
        for phase in PHASES if not error else ():
            # the budget is enforced twice, both harness-agnostically: exactly, between
            # phases (no new phase once it is spent) and approximately, mid-phase (the
            # session log is polled and the container stopped at the phase's stop line).
            # BudgetPlan preserves the legacy later-phase reserve or applies the explicit
            # act-priority plan. A phase stop moves on; only the hard ceiling caps the
            # episode.
            used = self._live_calls(state, episode_files, set()) if plan.enabled else 0
            if budget and used >= budget:
                capped = True
                break
            stop_at = plan.stop_at(phase, used)
            if budget and used >= stop_at:
                continue
            handoff_start = handoffs.begin(spec.episode, phase)
            before = self._sessions(state)
            fired = [False]

            def stop_check(before=before, fired=fired, stop_at=stop_at):
                if self._live_calls(state, episode_files,
                                    self._sessions(state) - before) >= stop_at:
                    fired[0] = True
                    return True
                return False

            timed_out = False
            try:
                command = [
                    "--provider",
                    provider,
                    "--model",
                    model,
                    "--session-dir",
                    "/state",
                    "--skill",
                    "/workspace/skills",
                ]
                if enabled_tools:
                    command.extend(("--tools", ",".join(enabled_tools)))
                command.extend(("-p", phase_prompt(spec, phase, used)))
                proc = self.sandbox.run(
                    run_root,
                    command,
                    env=env,
                    timeout_s=self.phase_timeout_s,
                    mounts=workspace_mounts + ((str(state), "/state"),
                            (str(handoffs.root), CONTAINER_ROOT))
                           + self._task_mount(run_root) + extra_mounts,
                    stop_check=stop_check if plan.enabled else None,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                proc = None
            new = self._sessions(state) - before
            phase_events: list[ActionEvent] = []
            if new:
                session_paths = sorted(new, key=str)
                mapping[phase] = [p.name for p in session_paths]
                episode_files |= new
                for session_path in session_paths:
                    evidence_ref = (
                        session_path.relative_to(run_root).as_posix()
                        if session_path.is_relative_to(run_root)
                        else session_path.name
                    )
                    session_evidence = self._session_evidence(
                        session_path,
                        phase=phase,
                        expected_provider=expected_provider,
                        expected_model=expected_model,
                        evidence_ref=evidence_ref,
                    )
                    native_sessions.append(session_evidence)
                    native_paths.append(session_path)
                    phase_events.extend(session_evidence.events)
            handoff = handoffs.finish(handoff_start, phase_events,
                                      interrupted=timed_out or fired[0])
            if spec.checkpoint_turns and handoff["source"] != "agent":
                checkpoint_misses += 1
            if timed_out:
                error = f"phase {phase}: timeout after {self.phase_timeout_s}s"
                break
            assert proc is not None
            if proc.returncode != 0:
                if fired[0]:
                    # stopped at the phase's line: continue if it was only the reserve,
                    # end the episode only when the whole budget is spent
                    if budget and self._live_calls(state, episode_files, set()) >= budget:
                        capped = True
                        break
                    continue
                error = f"phase {phase}: exit {proc.returncode}: {proc.stderr[-400:]}"
                break
            if expected_model and not new:
                error = f"phase {phase}: no native Pi session was created"
                break
            if expected_model:
                invalid = next(
                    (
                        session
                        for session in native_sessions[-len(new):]
                        if not session.terminal
                    ),
                    None,
                )
                if invalid is not None:
                    error = f"phase {phase}: {invalid.error}"
                    break
        (run_root / "traces" / f"ep{spec.episode:03d}.json").write_text(
            json.dumps(mapping, indent=1))
        trace = self.read_trace(run_root, spec.episode)
        phase_counts = {
            phase: sum(1 for event in trace if event.phase == phase and event.tool)
            for phase in PHASES
        }
        counters = {"phases": len(mapping), "turn_capped": capped,
                    "checkpoint_misses": checkpoint_misses}
        counters.update({f"phase_{phase}_turns": count
                         for phase, count in phase_counts.items()})
        result = EpisodeResult(
            episode=spec.episode, ok=not error,
            turns=sum(1 for e in trace if e.tool), error=error,
            counters=counters,
        )
        return result, tuple(native_sessions), tuple(native_paths)

    def _live_calls(self, state: Path, episode_files: set, extra: set) -> int:
        """Tool calls made so far this episode, read live from the session logs.

        Pi's v3 session JSONL is plain text and appended per event, so a mid-phase read
        sees every completed assistant tool-call block. ``stopReason: toolUse`` describes
        the same turn and must not be counted as a second call."""
        n = 0
        for f in set(episode_files) | set(extra):
            path = Path(f) if isinstance(f, Path) else state / f
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, str):
                    if row in {"toolCall", "tool_call", "toolUse"}:
                        n += 1
                    continue
                if not isinstance(row, dict) or row.get("type") != "message":
                    continue
                message = row.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                n += sum(
                    1
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") in {"toolCall", "tool_call", "toolUse"}
                )
        return n

    # ------------------------------------------------------------------ measure path

    def _surface_for_path(self, file_path: str) -> Optional[str]:
        p = file_path
        for prefix in ("/workspace/candidate/", "/workspace/", "candidate/"):
            if p.startswith(prefix):
                p = p[len(prefix):]
                break
        if p == "AGENTS.md":
            return "instructions"
        for s in ("skills", "notes", "tools"):
            if p.startswith(f"{s}/"):
                return s
        if p.startswith("src/"):
            return "loop"
        return None

    def read_trace(self, root: Path, episode: int) -> Sequence[ActionEvent]:
        root = Path(root)
        map_path = root / "traces" / f"ep{episode:03d}.json"
        if not map_path.exists():
            return []
        mapping = json.loads(map_path.read_text())
        state = root / ".pi-state"
        events: list[ActionEvent] = []
        turn = 0
        for phase in PHASES:
            names = mapping.get(phase)
            if not names:
                continue
            if isinstance(names, str):
                names = [names]                # traces written before the list format
            for name in names:
                if not (state / name).exists():
                    continue
                phase_events = self._session_trace(state / name, phase)
                for event in phase_events:
                    events.append(ActionEvent(
                        turn=turn + event.turn, phase=event.phase, tool=event.tool,
                        surface=event.surface, params=event.params, text=event.text,
                    ))
                turn += max((event.turn for event in phase_events), default=0)
        return events
