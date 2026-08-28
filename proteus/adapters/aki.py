"""Aki adapter — the default, full-featured research harness.

The reference integration: the Aki harness (persistent memory + writable skills +
self-authored tools + an editable episode loop) plugged into Proteus. It is the harness the
paper's headline experiments run on.

Two paths, deliberately separated:

- **Measure path** (`read_trace`, `disposition_fingerprint`): pure JSONL/file parsing. Needs
  no Aki checkout at all, so Proteus's rulers read existing Aki run roots as-is.
- **Run path** (`seed`, `install_disposition`, `run_episode`): drives the source-built Aki
  image through framed Docker stdin/stdout. Native initialization executes inside the
  network-disabled image and receives only fixed container paths. Ordinary episodes require
  a host-owned model channel and are completed by the next controller-proxy layer.

The adapter never writes into the Aki checkout; all state lands in the Proteus run root.

Known limits, stated rather than papered over:
- Aki's phase prompts live inside the harness (`loop.py`), so `spec.phase_prompts` is not
  injected into the episode yet; no-goal runs — the paper's primary regime — are fully
  supported, while the framework default epistemic protocol and goal-text injection are
  not wired into Aki episodes. Other bundled adapters consume `spec.phase_prompts`.
- Aki couples seeding and disposition install in one `init_run`, so this adapter performs
  both inside `install_disposition` (the framework calls `seed` first; it only records state).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from proteus.adapters.aki_container import (
    AKI_CONTROLLER_BASE_URL,
    AkiContainerController,
    AkiContainerPlan,
)
from proteus.core.adapter import ActionEvent, EpisodeResult, EpisodeSpec, Surface
from proteus.core.disposition import Disposition
from proteus.core.episode import private_record_dir
from proteus.sandbox import DockerSandbox, SandboxConfig

if TYPE_CHECKING:
    from proteus.adapters.aki_safety import AkiPermissionPolicyAdapter, AkiSafetyRuntime

#: Aki phase names -> Proteus phase names.
_PHASE = {"observe": "observe", "propose": "propose",
          "select_and_act": "act", "reflect": "reflect"}

_NATIVE_CONFIG_VERSION = 1
_SUPERVISOR_CONFIG_KEYS = frozenset(
    {"condition", "seed", "episodes", "root", "model", "base_url", "persona", "max_turns"}
)
_EPISODE_CONFIG_KEYS = frozenset(
    {
        "root",
        "persona",
        "model",
        "base_url",
        "max_turns",
        "max_output_tokens",
        "snapshot_dir",
        "memory_dir",
        "skills_dir",
        "tools_dir",
        "trace_dir",
        "loop_path",
        "package_dir",
        "integrity_path",
        "aki_root",
        "persona_dir",
    }
)


class AkiHarness:
    """`HarnessAdapter` for the Aki research harness."""

    name = "aki"
    continuity_mode = "native"     # Aki's supervisor owns its internal phase state
    disposition_in_files = True    # the apparatus installs the persona through its carrier
    native_conditions = (
        "openness_high",
        "openness_low",
        "conscientiousness_high",
        "conscientiousness_low",
        "neutral",
        "neutral_matched",
    )

    SURFACES = (
        Surface("memory", "memory", unit="file", write_tools=frozenset({"memory_write"})),
        Surface("skills", "skills", unit="directory",
                write_tools=frozenset({"skill_write", "skill_update"})),
        Surface("tools", "tools", unit="file", write_tools=frozenset({"tool_write"}),
                is_code=True),
        Surface("loop", "loop.py", unit="top_level_def", is_code=True, free_named=False,
                write_tools=frozenset({"file_write", "file_edit"})),
        Surface(
            "permission_policy",
            "permission_policy.py",
            unit="file",
            is_code=True,
            free_named=False,
            write_tools=frozenset({"file_write"}),
        ),
        Surface(
            "permission_policy_control",
            "permission_policy_control.py",
            unit="file",
            is_code=True,
            free_named=False,
            write_tools=frozenset({"file_write"}),
        ),
    )

    def __init__(
        self,
        *,
        sandbox: DockerSandbox | None = None,
        init_timeout_s: float = 300,
        episode_timeout_s: float = 90 * 60,
        call_timeout_s: float = 180,
    ) -> None:
        if sandbox is None:
            manifest = Path(__file__).parents[2] / "environments/aki/environment.toml"
            sandbox = DockerSandbox(SandboxConfig.from_manifest(manifest))
        config = sandbox.config
        if config.network != "none" or config.env_passthrough:
            raise ValueError("Aki Docker runtime requires network none and no environment passthrough")
        if config.env:
            raise ValueError("Aki Docker runtime rejects literal environment values")
        if config.extra_mounts:
            raise ValueError("Aki Docker runtime rejects sandbox extra mounts")
        if config.extra_args:
            raise ValueError("Aki Docker runtime rejects extra Docker args")
        if config.entrypoint:
            raise ValueError("Aki Docker runtime rejects an alternate entrypoint")
        host_user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else ""
        self.sandbox = DockerSandbox(replace(config, user=host_user))
        self.container = AkiContainerController(self.sandbox)
        self.init_timeout_s = init_timeout_s
        self.episode_timeout_s = episode_timeout_s
        self.call_timeout_s = call_timeout_s
        self._pending_root: Optional[Path] = None
        self._run_configs: dict[Path, dict[str, dict[str, object]]] = {}

    def preflight(self) -> None:
        """Require the configured image to exist locally before any run-side capability."""
        image = self.sandbox.config.image
        try:
            inspected = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            raise ValueError(
                "Docker executable is not available for selected Aki preflight"
            ) from None
        if inspected.returncode != 0:
            raise ValueError(
                f"configured Aki Docker image {image!r} is not available locally; "
                "build it with environments/aki-src/build.sh"
            )

    # ---------------------------------------------------------------- contract: metadata

    def surfaces(self) -> Sequence[Surface]:
        return self.SURFACES

    def required_edit_tools(self) -> frozenset[str]:
        return frozenset({"memory_write", "skill_write", "tool_write", "file_write"})

    def safety_runtime(self) -> AkiSafetyRuntime:
        """Bind the universal safety scenarios to Aki's candidate-local runtime."""
        from proteus.adapters.aki_safety import AkiSafetyRuntime

        return AkiSafetyRuntime(self)

    def permission_policy_adapter(self) -> AkiPermissionPolicyAdapter:
        """Bind snapshot-owned native permission evidence for canonical Aki routes."""
        from proteus.adapters.aki_safety import AkiPermissionPolicyAdapter

        return AkiPermissionPolicyAdapter(self)

    # ---------------------------------------------------------------- run path (contained Aki)

    def _arm_label(self, disposition: Disposition) -> str:
        if "AKI_ARM" in disposition.config:
            condition = disposition.config["AKI_ARM"]
            if condition in self.native_conditions:
                return condition
            raise ValueError(f"no current native Aki condition {condition!r}")
        if disposition.is_empty:
            return "neutral"
        raise ValueError(
            f"disposition {disposition.label!r} has no current native Aki condition; "
            "pass an explicit current condition via Disposition.config['AKI_ARM']"
        )

    def seed(self, harness_root: Path, rng_seed: int = 0) -> None:
        # Aki's init_run seeds and installs in one step; remember state until then.
        harness_root.parent.mkdir(parents=True, exist_ok=True)
        self._pending_root = harness_root
        self._rng_seed = rng_seed

    def install_disposition(self, harness_root: Path, disposition: Disposition) -> None:
        arm = self._arm_label(disposition)
        run_root = harness_root.parent.resolve()
        result = self.container.run_once(
            run_root=run_root,
            plan=AkiContainerPlan(
                action="init",
                payload={
                    "condition": arm,
                    "seed": getattr(self, "_rng_seed", 0),
                    "episodes": 1,
                    "root": "/run",
                },
            ),
            mounts=((str(run_root), "/run"),),
            timeout_s=self.init_timeout_s,
        )
        native_config = result.get("native_config")
        if not isinstance(native_config, dict):
            raise RuntimeError("Aki native init returned no configuration")
        if native_config.get("root") != "/run":
            raise RuntimeError("Aki native init returned a non-container root")
        episode_config = result.get("episode_config")
        if not isinstance(episode_config, dict) or episode_config.get("root") != "/run":
            raise RuntimeError("Aki native init returned no container episode configuration")
        record = {
            "version": _NATIVE_CONFIG_VERSION,
            "run_root": str(run_root),
            "supervisor": native_config,
            "episode": episode_config,
        }
        config = self._validate_config_record(run_root, record)
        path = self._config_record_path(run_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
        self._run_configs[run_root] = config
        self._pending_root = None
        from proteus.adapters.aki_container_worker import install_snapshot_permission_policy

        snapshot_root = Path(harness_root).resolve()
        install_snapshot_permission_policy(snapshot_root)
        if not (snapshot_root / "permission_policy.py").is_file() or not (
            snapshot_root / "permission_policy_control.py"
        ).is_file():
            raise RuntimeError("Aki snapshot permission policy was not installed")
        from proteus.core import snapshot as proteus_snapshot

        git_dir = snapshot_root.parent / ".snapshot.git"
        if git_dir.exists():
            proteus_snapshot._git(snapshot_root, "add", "-A", "-f", "--", ".")
            proteus_snapshot._git(
                snapshot_root,
                "commit",
                "-q",
                "--amend",
                "--no-edit",
                "--allow-empty",
            )

    @staticmethod
    def _config_record_path(run_root: Path) -> Path:
        return private_record_dir(run_root) / "aki-native-config.json"

    @classmethod
    def _validate_config_record(
        cls, run_root: Path, value: object
    ) -> dict[str, dict[str, object]]:
        if not isinstance(value, dict):
            raise RuntimeError("Aki native config record is not a JSON object")
        if type(value.get("version")) is not int or value["version"] != _NATIVE_CONFIG_VERSION:
            raise RuntimeError("Aki native config record has an unsupported version")
        if value.get("run_root") != str(run_root.resolve()):
            raise RuntimeError("Aki native config record belongs to a different run root")
        supervisor = value.get("supervisor")
        episode = value.get("episode")
        if not isinstance(supervisor, dict) or set(supervisor) != _SUPERVISOR_CONFIG_KEYS:
            raise RuntimeError("Aki native supervisor config is malformed")
        if not isinstance(episode, dict) or set(episode) != _EPISODE_CONFIG_KEYS:
            raise RuntimeError("Aki native episode config is malformed")
        if (
            not isinstance(supervisor["condition"], str)
            or supervisor["condition"] not in cls.native_conditions
            or type(supervisor["seed"]) is not int
            or type(supervisor["episodes"]) is not int
            or supervisor["episodes"] <= 0
            or supervisor["root"] != "/run"
            or type(supervisor["max_turns"]) is not int
            or supervisor["max_turns"] <= 0
        ):
            raise RuntimeError("Aki native supervisor config is malformed")
        for field in ("model", "base_url", "persona"):
            if not isinstance(supervisor[field], str) or not supervisor[field]:
                raise RuntimeError("Aki native supervisor config is malformed")
        expected_paths = {
            "root": "/run",
            "snapshot_dir": "/run/harness",
            "memory_dir": "/run/harness/memory",
            "skills_dir": "/run/harness/skills",
            "tools_dir": "/run/harness/tools",
            "trace_dir": "/run/traces",
            "loop_path": "/run/harness/loop.py",
            "package_dir": "/run/harness/aki",
            "integrity_path": "/run/integrity.json",
            "aki_root": "/run/.aki",
            "persona_dir": "/run/.persona",
        }
        if any(episode.get(field) != expected for field, expected in expected_paths.items()):
            raise RuntimeError("Aki native episode config has invalid container paths")
        if type(episode["max_turns"]) is not int or episode["max_turns"] <= 0:
            raise RuntimeError("Aki native episode config is malformed")
        if type(episode["max_output_tokens"]) is not int or episode["max_output_tokens"] <= 0:
            raise RuntimeError("Aki native episode config is malformed")
        for field in ("model", "base_url", "persona", "max_turns"):
            if episode[field] != supervisor[field]:
                raise RuntimeError("Aki native supervisor and episode configs do not match")
        return {"supervisor": dict(supervisor), "episode": dict(episode)}

    @classmethod
    def _load_config_record(cls, run_root: Path) -> dict[str, dict[str, object]]:
        path = cls._config_record_path(run_root)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise RuntimeError(f"Aki native config record is missing at {path}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Aki native config record is unreadable: {exc}") from None
        return cls._validate_config_record(run_root, value)

    @staticmethod
    def _container_episode_config(
        config: dict[str, object], *, root: str, model: str, max_turns: int
    ) -> dict[str, object]:
        return {
            **config,
            "root": root,
            "model": model,
            "base_url": AKI_CONTROLLER_BASE_URL,
            "max_turns": max_turns,
            "snapshot_dir": f"{root}/harness",
            "memory_dir": f"{root}/harness/memory",
            "skills_dir": f"{root}/harness/skills",
            "tools_dir": f"{root}/harness/tools",
            "trace_dir": f"{root}/traces",
            "loop_path": f"{root}/harness/loop.py",
            "package_dir": f"{root}/harness/aki",
            "integrity_path": f"{root}/integrity.json",
            "aki_root": f"{root}/.aki",
            "persona_dir": f"{root}/.persona",
        }

    @staticmethod
    def _task_mount(run_root: Path) -> tuple[tuple[str, ...], ...]:
        """Expose the snapshot-external task workspace at the shared writable alias."""
        task_root = run_root / "task"
        return ((str(task_root.resolve()), "/workspace/task"),) if task_root.is_dir() else ()

    def run_episode(self, spec: EpisodeSpec) -> EpisodeResult:
        run_root = Path(spec.root).resolve()
        config = self._run_configs.get(run_root)
        if config is None:
            config = self._load_config_record(run_root)
            self._run_configs[run_root] = config
        episode_config = self._container_episode_config(
            config["episode"],
            root="/workspace/candidate",
            model=spec.model,
            max_turns=spec.max_turns or sys.maxsize,
        )
        self._run_configs[run_root] = {
            "supervisor": config["supervisor"],
            "episode": episode_config,
        }
        if spec.live_model_channel is None:
            raise RuntimeError(
                "Aki ordinary episodes require a host-owned LiveModelChannel; "
                "native provider fallback is disabled"
            )
        active_source = Path(spec.active_root or (run_root / "harness")).resolve()
        if not active_source.is_dir():
            raise RuntimeError(f"Aki active harness is missing at {active_source}")
        with tempfile.TemporaryDirectory(prefix="proteus-aki-active-") as active_root:
            materialized_active = Path(active_root)
            shutil.copytree(active_source, materialized_active / "harness")
            result = self.container.run_model_episode(
                run_root=run_root,
                plan=AkiContainerPlan(
                    action="ordinary_episode",
                    payload={
                        "condition": config["supervisor"]["condition"],
                        "seed": config["supervisor"]["seed"],
                        "episode": spec.episode,
                        "model": spec.model,
                        "base_url": episode_config["base_url"],
                        "persona": episode_config["persona"],
                        "max_turns": episode_config["max_turns"],
                        "max_output_tokens": episode_config["max_output_tokens"],
                    },
                ),
                channel=spec.live_model_channel,
                mounts=(
                    (str(materialized_active), "/workspace/active", "ro"),
                    (str(run_root), "/workspace/candidate"),
                )
                + self._task_mount(run_root),
                episode_timeout_s=self.episode_timeout_s,
                call_timeout_s=self.call_timeout_s,
            )
        evidence = asdict(result)
        evidence_path = (
            private_record_dir(run_root)
            / "aki-live-worker"
            / f"episode-{spec.episode:03d}"
            / "ordinary-episode.json"
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = evidence_path.with_name(evidence_path.name + ".tmp")
        temporary.write_text(
            json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(evidence_path)

        supervisor = result.supervisor_result
        subprocess_status = supervisor.get("subprocess_status")
        ok = result.terminal and not result.error and subprocess_status == "complete"
        counters = {
            name: supervisor[name]
            for name in (
                "tokens_in",
                "tokens_out",
                "memory_writes",
                "skill_writes",
                "tool_writes",
                "code_edits",
                "ask_human_events",
                "web_calls",
            )
            if type(supervisor.get(name)) is int
        }
        error = result.error
        if not ok and not error:
            error = str(supervisor.get("stderr_tail") or subprocess_status or "native episode failed")
        return EpisodeResult(
            episode=spec.episode,
            ok=ok,
            turns=int(supervisor.get("turns_used") or 0),
            error=error,
            counters=counters,
        )

    # ---------------------------------------------------------------- measure path (no Aki)

    @staticmethod
    def _trace_path(root: Path, episode: int) -> Path:
        traces = Path(root) / "traces"
        for candidate in (traces / f"ep{episode:03d}.jsonl", traces / f"ep{episode}.jsonl"):
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"no trace for episode {episode} under {traces}")

    def _surface_for_tool(self, tool: str) -> Optional[str]:
        for s in self.SURFACES:
            if tool in s.write_tools:
                return s.name
        return None

    def _episode_outcome(self, root: Path, episode: int) -> tuple[dict, dict]:
        status: dict = {}
        counters: dict = {}
        for event in self._events(root, episode):
            if event.get("event") == "episode_status":
                status = event.get("data", {})
            elif event.get("event") == "episode_end":
                counters = event.get("data", {}).get("counters", {})
        return status, counters

    def _events(self, root: Path, episode: int):
        path = self._trace_path(root, episode)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    def read_trace(self, root: Path, episode: int) -> Sequence[ActionEvent]:
        events: list[ActionEvent] = []
        phase = "observe"
        for e in self._events(root, episode):
            kind = e.get("event")
            data = e.get("data", {})
            if kind == "phase_start":
                phase = _PHASE.get(data.get("phase", ""), data.get("phase", ""))
            elif kind == "tool_call":
                tool = data.get("tool_name", "")
                events.append(ActionEvent(
                    turn=int(e.get("iteration", 0)), phase=phase, tool=tool,
                    surface=self._surface_for_tool(tool),
                    params=data.get("params", {}) or {}, text="",
                ))
            elif kind == "reply":
                events.append(ActionEvent(
                    turn=int(e.get("iteration", 0)), phase=phase, tool=None,
                    surface=None, params={}, text=str(data.get("content", "")),
                ))
        return events

    def disposition_fingerprint(self, harness_root: Path) -> str:
        """Hash the installed-disposition carriers so drift of F is detectable."""
        harness_root = Path(harness_root)
        h = hashlib.sha256()
        candidates = sorted(
            [harness_root / "loop.py",
             *(harness_root.parent / ".aki").glob("persona*"),
             *harness_root.glob("disposition_*.py")], key=str)
        for p in candidates:
            if p.is_file():
                h.update(p.name.encode())
                h.update(p.read_bytes())
        return h.hexdigest()[:16]
