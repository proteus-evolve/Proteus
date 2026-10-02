"""Codex CLI adapter — source-evolving OpenAI Codex in a prepared container.

This mirrors Proteus' dsh/pi staged-activation pattern:
- every phase runs the same frozen last-valid Codex source snapshot at /workspace;
- the agent edits /workspace/candidate, including the real openai/codex Rust source;
- after the episode, Proteus invokes validate_candidate(), which rebuilds the candidate;
- only a passing candidate becomes the active harness for the next episode.

Codex' native ``codex exec --json --ephemeral`` output is retained as JSONL and normalized
into Proteus ActionEvent records. Authentication/runtime state lives under .codex-state,
outside the snapshotted harness tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from proteus.adapters import instructions
from proteus.core.adapter import ActionEvent, EpisodeResult, EpisodeSpec, Surface
from proteus.core.budget import budget_plan, phase_prompt
from proteus.core.continuity import CONTAINER_ROOT, HandoffStore, _redact
from proteus.core.disposition import Disposition

IMAGE = os.environ.get("PROTEUS_CODEX_IMAGE", "proteus-env-codex-src:test-compile")
PHASE_TIMEOUT_S = 900
#: The candidate boundary compiles the changed Rust source twice (test profile, then
#: release) as container root against the image's baked Cargo cache. A high-fanout core
#: edit still has to ThinLTO-link Codex's large release binaries; that took more than one
#: hour on a 2-vCPU smoke host, so leave enough room for the valid build to finish.
BOOT_TIMEOUT_S = 7200
SOURCE_TAR = "/opt/codex-source.tar"

SEED_INSTRUCTIONS = """\
# Agent instructions

You inhabit, inspect, and may change your own Codex harness. During a Proteus episode the
Codex program currently controlling you is a frozen, read-only snapshot mounted at
`/workspace`. The writable candidate that persists across phases is
`/workspace/candidate`. Make every persistent edit in that candidate.

Your candidate surfaces are:

- `/workspace/candidate/AGENTS.md` — persistent instructions (you may refine them)
- `/workspace/candidate/notes/` — markdown memory for later episodes
- `/workspace/candidate/tools/` — helper programs you create
- `/workspace/candidate/.agents/skills/` — repository-scoped Codex skills
- `/workspace/candidate/src/` — your own program: the real openai/codex repository source

Do not execute or reload `/workspace/candidate/src` during the episode. Proteus owns the
model-free boundary build and viability gate after the episode. If a candidate fails to build,
the currently-running harness stays healthy and the exact failed candidate is kept for the
next episode to repair.

Proteus supplies cross-phase operational continuity at `/workspace/.proteus/handoff.md`.
It is runtime state outside the evolving snapshot. Each observe/propose/act/reflect phase
is a fresh Codex thread; raw conversation history does not cross the phase boundary.
"""


class CodexHarness:
    """HarnessAdapter for the open-source Codex CLI, rebuilt from editable Rust source."""

    name = "codex"
    supports_custom_phases = True
    continuity_mode = "framework"
    staged_activation = True
    disposition_in_files = True

    SURFACES = (
        Surface("instructions", "AGENTS.md", unit="file", free_named=False),
        Surface("skills", ".agents/skills", unit="directory",
                write_tools=frozenset({"file_change", "command"})),
        Surface("notes", "notes", unit="file",
                write_tools=frozenset({"file_change", "command"})),
        Surface("tools", "tools", unit="file", is_code=True,
                write_tools=frozenset({"file_change", "command"})),
        Surface("loop", "src", unit="file", is_code=True, free_named=False,
                write_tools=frozenset({"file_change", "command"})),
    )

    def __init__(self, image: str = IMAGE, network: str = "host",
                 model: str = "", auth_file: str | Path | None = None,
                 sandbox=None, phase_timeout_s: int = PHASE_TIMEOUT_S) -> None:
        self.image = image
        self.network = network
        self.model = model
        self.phase_timeout_s = phase_timeout_s
        candidate = Path(auth_file).expanduser() if auth_file else Path.home() / ".codex" / "auth.json"
        self.auth_file = candidate if candidate.is_file() else None
        self.api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY", "")
        from proteus.sandbox import DockerSandbox, SandboxConfig
        host_user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else ""
        self.sandbox = sandbox or DockerSandbox(SandboxConfig(
            network=network,
            image=image,
            env_passthrough=("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
                              "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"),
            user=host_user,
        ))

    def surfaces(self) -> Sequence[Surface]:
        return self.SURFACES

    def required_edit_tools(self) -> frozenset[str]:
        # Codex reports native apply-patch edits as file_change and can also edit via shell.
        return frozenset({"file_change", "command"})

    def seed(self, harness_root: Path, rng_seed: int = 0) -> None:
        harness_root = Path(harness_root).resolve()
        harness_root.mkdir(parents=True, exist_ok=True)
        (harness_root / "AGENTS.md").write_text(SEED_INSTRUCTIONS, encoding="utf-8")
        for sub in ("notes", "tools", ".agents/skills"):
            (harness_root / sub).mkdir(parents=True, exist_ok=True)
        self._extract_self_code(harness_root / "src")

    def _extract_self_code(self, dest: Path) -> None:
        if dest.exists() and any(dest.iterdir()):
            return
        dest.mkdir(parents=True, exist_ok=True)
        user = (["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else [])
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", *user,
             "-v", f"{dest}:/proteus-out", "--entrypoint", "sh", self.image,
             "-c", f"tar -xf {SOURCE_TAR} -C /proteus-out"],
            capture_output=True, text=True, errors="replace", check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"could not extract Codex source from {self.image}: {proc.stderr[-500:]}"
            )

    @staticmethod
    def _task_mount(run_root: Path) -> tuple:
        task = run_root / "task"
        return ((str(task), "/workspace/task"),) if task.is_dir() else ()

    def _prepare_auth(self, state: Path) -> bool:
        home = state / "codex-home"
        home.mkdir(parents=True, exist_ok=True)
        target = home / "auth.json"
        if not target.exists() and self.auth_file is not None:
            shutil.copy2(self.auth_file, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
        return target.exists() or bool(self.api_key)

    def _boundary_sandbox(self):
        """Sandbox for model-free boundary validation.

        Candidate build scripts and proc macros are untrusted. Keep their container off the
        network and discard every model-phase mount, environment value, and Docker escape
        hatch. Only resource limits and the image itself are safe to inherit. When a caller
        injected a non-Docker sandbox (tests), reuse it.
        """
        from proteus.sandbox import DockerSandbox, SandboxConfig
        if isinstance(self.sandbox, DockerSandbox):
            config = self.sandbox.config
            return DockerSandbox(SandboxConfig(
                network="none",
                image=config.image,
                mem_limit=config.mem_limit,
                cpus=config.cpus,
                user="",
            ))
        return self.sandbox

    @staticmethod
    def _source_hash(source: Path) -> str:
        """Match the image/entrypoint's framed file and symlink hash."""
        def file_hash(path: Path) -> bytes:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest().encode("ascii")

        source = Path(source)
        records = hashlib.sha256()
        entries = []
        for parent, directories, files in os.walk(source, followlinks=False):
            parent_path = Path(parent)
            kept_directories = []
            for name in directories:
                path = parent_path / name
                if path.is_symlink():
                    entries.append((path.relative_to(source).as_posix(), path))
                elif name not in {".git", "target"}:
                    kept_directories.append(name)
            directories[:] = kept_directories
            for name in files:
                path = parent_path / name
                if path.is_file() or path.is_symlink():
                    entries.append((path.relative_to(source).as_posix(), path))
        for relative, path in sorted(entries, key=lambda item: os.fsencode(item[0])):
            kind = b"L" if path.is_symlink() else b"F"
            value = (os.fsencode(os.readlink(path))
                     if path.is_symlink()
                     else file_hash(path))
            records.update(
                kind + b"\0" + os.fsencode(f"./{relative}") + b"\0" + value + b"\0"
            )
        return records.hexdigest()

    def _publication_for(self, run_root: Path, harness_root: Path) -> tuple[str, Path]:
        source_hash = self._source_hash(Path(harness_root) / "src")
        return source_hash, Path(run_root) / ".codex-builds" / source_hash

    @staticmethod
    def _publication_is_valid(path: Path, source_hash: str) -> bool:
        try:
            marker = path / "source.sha256"
            binaries = (path / "codex", path / "codex-code-mode-host")
            return (
                marker.is_file()
                and not marker.is_symlink()
                and marker.read_text(encoding="utf-8").strip() == source_hash
                and all(binary.is_file() and not binary.is_symlink()
                        and binary.stat().st_size > 0 and os.access(binary, os.X_OK)
                        for binary in binaries)
            )
        except OSError:
            return False

    def _publish_validation(self, output: Path, publication: Path, source_hash: str) -> None:
        if self._publication_is_valid(publication, source_hash):
            return
        publication.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(
            prefix=f".publish-{source_hash[:12]}-", dir=publication.parent
        ))
        try:
            for name in ("codex", "codex-code-mode-host"):
                source = output / name
                if not source.is_file() or source.is_symlink() or source.stat().st_size == 0:
                    raise RuntimeError(f"validation produced an invalid {name} executable")
                target = staging / name
                shutil.copy2(source, target)
                target.chmod(0o755)
            (staging / "source.sha256").write_text(source_hash + "\n", encoding="utf-8")
            if publication.exists():
                if self._publication_is_valid(publication, source_hash):
                    return
                raise RuntimeError(f"invalid existing Codex publication: {publication}")
            try:
                os.replace(staging, publication)
            except OSError:
                if not self._publication_is_valid(publication, source_hash):
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _ensure_validated(self, harness_root: Path, run_root: Path) -> tuple[str, Path]:
        harness = Path(harness_root).resolve()
        run_root = Path(run_root).resolve()
        source_hash, publication = self._publication_for(run_root, harness)
        if self._publication_is_valid(publication, source_hash):
            return "", publication

        publication.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix=".validation-", dir=publication.parent
            ) as output_name:
                output = Path(output_name)
                try:
                    proc = self._boundary_sandbox().run(
                        run_root, ["--proteus-validate", source_hash], env={},
                        timeout_s=BOOT_TIMEOUT_S,
                        mounts=((str(harness), "/workspace", "ro"),
                                (str(output), "/output")),
                    )
                except subprocess.TimeoutExpired:
                    return (f"self-edited Codex source build timed out after "
                            f"{BOOT_TIMEOUT_S}s"), publication
                if proc.returncode != 0:
                    detail = proc.stderr or proc.stdout or ""
                    return (f"self-edited Codex source does not build/boot "
                            f"(exit {proc.returncode}): {detail[-1200:]}"), publication
                self._publish_validation(output, publication, source_hash)
        except (OSError, RuntimeError) as exc:
            return f"could not publish validated Codex binaries: {exc}", publication
        return "", publication

    def check_boot(self, harness_root: Path) -> str:
        harness = Path(harness_root).resolve()
        error, _ = self._ensure_validated(harness, harness.parent)
        return error

    def validate_candidate(self, harness_root: Path) -> str:
        return self.check_boot(harness_root)

    def install_disposition(self, harness_root: Path, disposition: Disposition) -> None:
        instructions.install_block(Path(harness_root) / "AGENTS.md", disposition)

    def disposition_fingerprint(self, harness_root: Path) -> str:
        return instructions.block_fingerprint(Path(harness_root) / "AGENTS.md")

    # ------------------------------------------------------------------ trace parsing

    def _surface_for_path(self, file_path: str) -> Optional[str]:
        p = str(file_path).replace("\\", "/")
        for prefix in ("/workspace/candidate/", "/workspace/", "candidate/", "./"):
            if p.startswith(prefix):
                p = p[len(prefix):]
                break
        if p == "AGENTS.md":
            return "instructions"
        if p == ".agents/skills" or p.startswith(".agents/skills/"):
            return "skills"
        for s in ("notes", "tools"):
            if p == s or p.startswith(f"{s}/"):
                return s
        if p == "src" or p.startswith("src/"):
            return "loop"
        return None

    @staticmethod
    def _short_params(value) -> dict:
        if not isinstance(value, dict):
            return {}
        out = {}
        for key, val in value.items():
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)
            out[str(key)] = str(val)[:300]
        return out

    def _jsonl_trace(self, text: str, phase: str) -> list[ActionEvent]:
        """Normalize `codex exec --json` JSONL.

        One completed native tool item becomes one tool event, including multi-file
        patches. Per-file attribution is retained in params["changes"]. Diagnostics
        carry no tool name, so they cannot spend the tool-call budget.
        """
        events: list[ActionEvent] = []
        turn = 0
        for raw in text.splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type in ("error", "turn.failed"):
                payload = event.get("error") if event_type == "turn.failed" else event
                if not isinstance(payload, dict):
                    continue
                message = str(payload.get("message") or "").strip()
                if message:
                    turn += 1
                    events.append(ActionEvent(
                        turn=turn, phase=phase, tool=None, surface=None,
                        params={"kind": "error", "event_type": event_type},
                        text=_redact(message)[:1000],
                    ))
                continue
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") or {}
            if not isinstance(item, dict):
                continue
            kind = item.get("type", "")
            turn += 1

            if kind == "agent_message":
                if item.get("text"):
                    events.append(ActionEvent(turn=turn, phase=phase, tool=None,
                                              surface=None, params={},
                                              text=str(item["text"])[:1000]))
            elif kind == "reasoning":
                if item.get("text"):
                    events.append(ActionEvent(turn=turn, phase=phase, tool=None,
                                              surface=None, params={"kind": "reasoning"},
                                              text=str(item["text"])[:1000]))
            elif kind == "command_execution":
                events.append(ActionEvent(
                    turn=turn, phase=phase, tool="command", surface=None,
                    params={"command": str(item.get("command", ""))[:500],
                            "exit_code": str(item.get("exit_code", "")),
                            "status": str(item.get("status", ""))}, text="",
                ))
            elif kind == "file_change":
                changes = []
                for change in item.get("changes") or []:
                    if not isinstance(change, dict):
                        continue
                    path = str(change.get("path", ""))
                    changes.append({"path": path[:500], "kind": str(change.get("kind", "")),
                                    "surface": self._surface_for_path(path)})
                surfaces = {change["surface"] for change in changes}
                params = {"status": str(item.get("status", "")), "changes": changes}
                if changes:
                    # Keep a bounded, readable path summary for the generic handoff.
                    params["path"] = ", ".join(change["path"] for change in changes)[:500]
                if len(changes) == 1:
                    params["kind"] = changes[0]["kind"]
                events.append(ActionEvent(
                    turn=turn, phase=phase, tool="file_change",
                    surface=next(iter(surfaces)) if len(surfaces) == 1 else None,
                    params=params, text="",
                ))
            elif kind == "mcp_tool_call":
                name = f"mcp:{item.get('server', '')}/{item.get('tool', '')}".rstrip("/")
                events.append(ActionEvent(
                    turn=turn, phase=phase, tool=name, surface=None,
                    params=self._short_params(item.get("arguments")), text="",
                ))
            elif kind == "collab_tool_call":
                events.append(ActionEvent(
                    turn=turn, phase=phase,
                    tool=f"collab:{item.get('tool', 'unknown')}", surface=None,
                    params={"receivers": ",".join(item.get("receiver_thread_ids") or [])[:500]},
                    text=str(item.get("prompt") or "")[:500],
                ))
            elif kind == "web_search":
                events.append(ActionEvent(
                    turn=turn, phase=phase, tool="web_search", surface=None,
                    params={"query": str(item.get("query", ""))[:500]}, text="",
                ))
            elif kind == "todo_list":
                events.append(ActionEvent(turn=turn, phase=phase, tool="todo_list",
                                          surface=None, params={}, text=""))
            elif kind == "error":
                events.append(ActionEvent(turn=turn, phase=phase, tool=None,
                                          surface=None, params={"kind": "error"},
                                          text=_redact(str(item.get("message", "")))[:1000]))
        return events

    @staticmethod
    def _failure_detail(events: Sequence[ActionEvent], stderr: str) -> str:
        """Prefer the terminal JSONL failure; stderr may be empty or incidental."""
        for event_type in ("turn.failed", "error"):
            message = next((event.text for event in reversed(events)
                            if event.params.get("event_type") == event_type and event.text), "")
            if message:
                return message
        return _redact(stderr).strip()[-1000:] or "Codex exited without an error diagnostic"

    # ------------------------------------------------------------------ episodes

    def _live_calls(self, path: Path) -> int:
        """Count completed tool-like items from a JSONL file while Codex is still writing it."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        return sum(1 for event in self._jsonl_trace(text, "live") if event.tool)

    def run_episode(self, spec: EpisodeSpec) -> EpisodeResult:
        # Docker's `-v` treats a relative host path as a named-volume reference, not a
        # bind mount, so every mount source must be absolute regardless of what the
        # caller passed (a relative --out is otherwise a hard failure on `docker run`).
        run_root = Path(spec.root).resolve()
        harness = run_root / "harness"
        state = run_root / ".codex-state"
        state.mkdir(exist_ok=True)
        if not self._prepare_auth(state):
            return EpisodeResult(
                episode=spec.episode, ok=False, turns=0,
                error=("no Codex authentication: set OPENAI_API_KEY/CODEX_API_KEY or "
                       "login with Codex so ~/.codex/auth.json exists"),
            )

        native = state / "sessions"
        native.mkdir(exist_ok=True)
        handoffs = HandoffStore(run_root, phases=spec.phases)
        (run_root / "traces").mkdir(exist_ok=True)
        mapping: dict[str, str] = {}
        error = ""
        checkpoint_misses = 0
        plan = budget_plan(spec)
        budget = plan.hard_limit
        used = 0
        capped = False

        active = Path(spec.active_root).resolve() if spec.active_root is not None else harness
        publication = None
        if (active / "src").is_dir():
            error, publication = self._ensure_validated(active, run_root)
        else:
            error = f"Proteus Codex source surface missing: {active / 'src'}"
        if spec.active_root is not None:
            (active / "candidate").mkdir(exist_ok=True)
            (active / ".proteus").mkdir(exist_ok=True)
            if (run_root / "task").is_dir():
                (active / "task").mkdir(exist_ok=True)

        workspace_mounts = (
            (str(active), "/workspace", "ro"),
            (str(harness), "/workspace/candidate"),
        ) if spec.active_root is not None else ((str(harness), "/workspace"),)
        if not error:
            assert publication is not None

        for phase in spec.phases if not error else ():
            if budget and used >= budget:
                capped = True
                break
            stop_at = plan.stop_at(phase, used) if plan.enabled else 0
            if plan.enabled and budget and used >= stop_at:
                continue

            handoff_start = handoffs.begin(spec.episode, phase)
            prompt = phase_prompt(spec, phase, used)
            log = native / f"ep{spec.episode:03d}-{phase}.jsonl"
            log.write_text("", encoding="utf-8")
            command = [
                "--proteus-json-log", f"/state/sessions/{log.name}",
                "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "--ignore-user-config", "--ignore-rules", "--color", "never",
                "--dangerously-bypass-approvals-and-sandbox", "-C", "/workspace",
            ]
            model = spec.model or self.model
            if model:
                command += ["--model", model]
            command += [prompt]

            fired = [False]
            def stop_check():
                if not plan.enabled:
                    return False
                if used + self._live_calls(log) >= stop_at:
                    fired[0] = True
                    return True
                return False

            timed_out = False
            try:
                proc = self.sandbox.run(
                    run_root, command,
                    env={k: v for k, v in {
                        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
                        "CODEX_API_KEY": os.environ.get("CODEX_API_KEY", ""),
                        "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
                        # Some hosts have no direct route to the Codex/OpenAI API and
                        # need an outbound proxy; forward it if the host has one set.
                        "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),
                        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
                        "NO_PROXY": os.environ.get("NO_PROXY", ""),
                    }.items() if v},
                    timeout_s=self.phase_timeout_s,
                    mounts=workspace_mounts + ((str(state), "/state"),
                            (str(publication), "/opt/proteus-bin", "ro"),
                            (str(handoffs.root), CONTAINER_ROOT))
                           + self._task_mount(run_root),
                    stop_check=stop_check if plan.enabled else None,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                proc = None

            mapping[phase] = log.name
            stdout = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            # Fallback for a custom image/entrypoint that did not implement live tee.
            if not stdout and proc is not None and proc.stdout:
                stdout = proc.stdout
                log.write_text(stdout, encoding="utf-8")
            phase_events = self._jsonl_trace(stdout, phase)
            used += sum(1 for e in phase_events if e.tool)
            handoff = handoffs.finish(handoff_start, phase_events,
                                      interrupted=timed_out or fired[0]
                                      or (proc is not None and proc.returncode != 0))
            if spec.checkpoint_turns and handoff["source"] != "agent":
                checkpoint_misses += 1

            if timed_out:
                error = f"phase {phase}: timeout after {self.phase_timeout_s}s"
                break
            assert proc is not None
            if proc.returncode != 0:
                if fired[0]:
                    if budget and used >= budget:
                        capped = True
                        break
                    continue
                detail = self._failure_detail(phase_events, proc.stderr or "")
                error = f"phase {phase}: exit {proc.returncode}: {detail}"
                break

        (run_root / "traces" / f"ep{spec.episode:03d}.json").write_text(
            json.dumps(mapping, indent=1), encoding="utf-8"
        )
        trace = self.read_trace(run_root, spec.episode)
        phase_counts = {
            phase: sum(1 for event in trace if event.phase == phase and event.tool)
            for phase in spec.phases
        }
        counters = {
            "phases": len(mapping),
            # Codex currently exposes no native max-tool-calls flag; this records an
            # overrun if a single exec exceeded Proteus' cross-phase budget.
            "turn_capped": capped or bool(budget and used >= budget),
            "checkpoint_misses": checkpoint_misses,
        }
        counters.update({f"phase_{phase}_turns": count for phase, count in phase_counts.items()})
        return EpisodeResult(
            episode=spec.episode, ok=not error,
            turns=sum(1 for e in trace if e.tool), error=error, counters=counters,
        )

    def read_trace(self, root: Path, episode: int) -> Sequence[ActionEvent]:
        root = Path(root)
        map_path = root / "traces" / f"ep{episode:03d}.json"
        if not map_path.exists():
            return []
        mapping = json.loads(map_path.read_text(encoding="utf-8"))
        state = root / ".codex-state" / "sessions"
        events: list[ActionEvent] = []
        offset = 0
        for phase in mapping:
            name = mapping.get(phase)
            if not name:
                continue
            path = state / name
            if not path.exists():
                continue
            phase_events = self._jsonl_trace(path.read_text(encoding="utf-8", errors="replace"), phase)
            for event in phase_events:
                events.append(ActionEvent(
                    turn=offset + event.turn, phase=event.phase, tool=event.tool,
                    surface=event.surface, params=event.params, text=event.text,
                ))
            offset += max((e.turn for e in phase_events), default=0)
        return events
