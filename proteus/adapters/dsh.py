"""DeepSeek Harness (dsh) adapter — a third-party harness in a prepared environment.

`dsh` is DeepSeek's open-source agent harness (github.com/deepseek-ai/deepseek-harness,
MIT, Node >= 24). This adapter runs its **headless profile** — one fresh persisted session
per phase — inside the source-mode image from `environments/dsh-src/`. The pinned upstream
checkout stays untouched, while each run receives its own evolvable copy of the real
TypeScript source. The adapter exact-syncs and rebuilds that copy on boot, launches the
container, and reads dsh's native session logs back.

Layout under the run root:
    harness/            the workspace dsh mounts at /workspace (the evolving state)
      AGENTS.md         instructions surface — dsh reads it natively; the disposition
                        is installed here as a removable marked block
      notes/  tools/    persistent surfaces the seed instructions establish
      src/              real dsh monorepo source; rebuilt and booted after edits
    .dsh-state/         DSH_HOME (sessions land here; not part of the harness)
    traces/epNNN.json   episode -> {phase: session dir} mapping

Requirements: the image (build once from environments/dsh-src/), a DeepSeek key
in DEEPSEEK_API_KEY (or DEEPSEEK_KEY), and Python 3.14+ or the `zstandard` package to read
dsh's zstd-compressed session JSONL.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from proteus.core.adapter import ActionEvent, EpisodeResult, EpisodeSpec, Surface
from proteus.core.disposition import Disposition
from proteus.core.episode import PHASES

IMAGE = os.environ.get("PROTEUS_DSH_IMAGE", "proteus-env-dsh-src:0.1.0-rc.8")
PHASE_TIMEOUT_S = 600
#: The editable self is dsh's real TypeScript source — the deepseek-harness monorepo
#: checkout the image was built from (tracked files via `git archive`, so the seed's
#: src/ is exactly the source of the build it boots). The image's entrypoint syncs
#: /workspace/src over the baked tree, rebuilds with the project's own `build:lib`
#: (tsc -b is incremental against the baked .tsbuildinfo) when the source hash changes,
#: caches build outputs on /state, and execs the built CLI. See environments/dsh-src/.
SOURCE_TAR = "/opt/dsh-source.tar"
#: A full build:lib is ~330s; the gate's timeout must cover one on a changed source.
BOOT_TIMEOUT_S = 900
SEED_INSTRUCTIONS = """\
# Agent instructions

You maintain and improve your own harness — the files in this workspace, which persist
across sessions. Your surfaces:

- `AGENTS.md` — these instructions (you may refine them)
- `notes/` — markdown knowledge you want future sessions to have
- `tools/` — small python utilities you may want later
- `src/` — your own program: the real TypeScript source of the harness that runs you.
  Edit it and the next session is rebuilt from your edit and runs it. If your edit
  does not compile, the run ends.

Each session is one phase of an episode; only these files carry over.
"""


def _zstd_partial(data: bytes) -> bytes:
    """Decode the complete leading frames of a possibly-truncated zstd stream.

    dsh flushes its session log one frame per event, so a file read mid-write ends in a
    partial frame; everything before it decodes cleanly. This is what makes a live turn
    count possible while a phase is still running. Returns what could be decoded; any
    failure (including no zstd support on this interpreter) returns what it has."""
    out = bytearray()
    try:
        try:
            from compression import zstd as _z  # Python 3.14+
            rest = data
            while rest:
                d = _z.ZstdDecompressor()
                out += d.decompress(rest)
                rest = d.unused_data
        except ImportError:
            import io

            import zstandard
            reader = zstandard.ZstdDecompressor().stream_reader(
                io.BytesIO(data), read_across_frames=True)
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                out += chunk
    except Exception:  # noqa: BLE001 - a partial tail frame is expected, not an error
        pass
    return bytes(out)


def _zstd_decompress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        try:
            import zstandard
        except ImportError as exc:
            raise RuntimeError(
                "reading dsh session logs needs Python 3.14+ (compression.zstd) or "
                "`pip install zstandard`"
            ) from exc
        # dsh streams its log one frame per event with no content size in the frame
        # header; the zstandard package's one-shot decompress() refuses exactly that.
        # A cross-frame stream reader handles it on every interpreter.
        import io
        reader = zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(data), read_across_frames=True)
        out = bytearray()
        while True:
            chunk = reader.read(65536)
            if not chunk:
                return bytes(out)
            out += chunk


class DshHarness:
    """`HarnessAdapter` for DeepSeek Harness's headless profile, containerized."""

    name = "dsh"
    disposition_in_files = True   # carried by AGENTS.md; keep it out of the phase prompts

    SURFACES = (
        Surface("instructions", "AGENTS.md", unit="file", free_named=False),
        Surface("notes", "notes", unit="file", write_tools=frozenset({"write"})),
        Surface("tools", "tools", unit="file", write_tools=frozenset({"write"}),
                is_code=True),
        # the harness's own program: the dsh monorepo source, extracted from the image at
        # seed time and rebuilt-on-boot by the image's entrypoint, so every phase runs the
        # seed's copy. The Aki loop.py arrangement, containerized (docs/ADAPTERS.md).
        Surface("loop", "src", unit="file", is_code=True, free_named=False,
                write_tools=frozenset({"write"})),
    )

    def __init__(self, image: str = IMAGE, network: str = "host",
                 key: str | None = None, sandbox=None,
                 phase_timeout_s: int = PHASE_TIMEOUT_S) -> None:
        self.image = image
        self.network = network
        self.phase_timeout_s = phase_timeout_s
        # per-instance key injection first (multi-tenant runs must not share env)
        self.key = key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_KEY", "")
        from proteus.sandbox import DockerSandbox, SandboxConfig
        # containers write into bind mounts; on Linux a root-in-container write leaves
        # root-owned files the host user can neither snapshot-clean nor edit, so the
        # container runs as the host user (the images chmod their /opt/src for this)
        host_user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else ""
        # `sandbox` lets a caller supply its own environment — a different image, extra
        # mounts, a GPU flag — without subclassing the adapter. The default keeps the
        # prepared image and the passthrough dsh needs.
        self.sandbox = sandbox or DockerSandbox(SandboxConfig(
            network=network, image=image,
            env_passthrough=("DEEPSEEK_API_KEY", "DSH_PERMISSION_MODE"),
            user=host_user,
        ))

    def surfaces(self) -> Sequence[Surface]:
        return self.SURFACES

    def required_edit_tools(self) -> frozenset[str]:
        return frozenset({"write"})

    def seed(self, harness_root: Path, rng_seed: int = 0) -> None:
        harness_root.mkdir(parents=True, exist_ok=True)
        (harness_root / "AGENTS.md").write_text(SEED_INSTRUCTIONS, encoding="utf-8")
        for sub in ("notes", "tools"):
            (harness_root / sub).mkdir(exist_ok=True)
        self._extract_self_code(harness_root / "src")

    def _extract_self_code(self, dest: Path) -> None:
        """Unpack the source the image was built from into `dest` (episode-0 state).

        The image bakes a `git archive` tar of the pinned checkout, so the seed's src/
        is exactly the tracked source of the build it boots. Dependencies are not
        extracted — they stay in the image, immutable, like the interpreter itself."""
        if dest.exists() and any(dest.iterdir()):
            return                        # resumed root: the seed owns its source already
        dest.mkdir(parents=True, exist_ok=True)
        user = (["--user", f"{os.getuid()}:{os.getgid()}"]
                if hasattr(os, "getuid") else [])
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", *user,
             "-v", f"{dest}:/proteus-out", "--entrypoint", "sh", self.image,
             "-c", f"tar -xf {SOURCE_TAR} -C /proteus-out"],
            capture_output=True, text=True, errors="replace", check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"could not extract dsh source from {self.image}: {proc.stderr[-300:]}")

    @staticmethod
    def _task_mount(run_root: Path) -> tuple:
        """Bind the run's task workspace (a snapshot-external sibling of the harness)
        into the agent's view, when the run is goal-conditioned."""
        task = run_root / "task"
        return ((str(task), "/workspace/task"),) if task.is_dir() else ()

    def check_boot(self, harness_root: Path) -> str:
        """Viability gate: sync + rebuild + `--version` through the image's boot wrapper.

        The wrapper rebuilds from /workspace/src, so a type error the agent wrote into
        its own source surfaces here as a legible build failure (exit 97 with the build
        log tail), before any API spend. The timeout covers one full build:lib."""
        harness = Path(harness_root)
        state = harness.parent / ".dsh-state"
        state.mkdir(exist_ok=True)
        proc = self.sandbox.run(
            harness.parent, ["--version"], env={}, timeout_s=BOOT_TIMEOUT_S,
            mounts=((str(harness), "/workspace"), (str(state), "/state")))
        if proc.returncode != 0:
            return (f"self-edited source does not boot (exit {proc.returncode}): "
                    f"{(proc.stderr or proc.stdout)[-400:]}")
        return ""

    def install_disposition(self, harness_root: Path, disposition: Disposition) -> None:
        from proteus.adapters import instructions
        instructions.install_block(harness_root / "AGENTS.md", disposition)

    # ------------------------------------------------------------------ episodes

    def _session_dirs(self, state: Path) -> set[Path]:
        root = state / "sessions"
        return {p.parent for p in root.rglob("session.jsonl.zstd")} if root.exists() else set()

    def run_episode(self, spec: EpisodeSpec) -> EpisodeResult:
        if not self.key:
            return EpisodeResult(episode=spec.episode, ok=False, turns=0,
                                 error="no DeepSeek key: set DEEPSEEK_API_KEY")
        run_root = Path(spec.root)
        harness = run_root / "harness"
        state = run_root / ".dsh-state"
        state.mkdir(exist_ok=True)
        (run_root / "traces").mkdir(exist_ok=True)
        mapping: dict[str, str] = {}
        error = ""
        capped = False
        budget = int(spec.max_turns or 0)
        episode_dirs: set = set()
        if (harness / "src").is_dir():
            error = self.check_boot(harness)
        min_pp = int(getattr(spec, "min_turns_per_phase", 0) or 0)
        for idx, phase in enumerate(PHASES if not error else ()):
            # the budget is enforced twice, both harness-agnostically: exactly, between
            # phases (no new phase once it is spent) and approximately, mid-phase (the
            # session log is polled and the container stopped at the phase's stop line).
            # The stop line reserves min_turns_per_phase for every later phase, so a
            # reservation stop moves to the next phase; only a spent budget ends the
            # episode.
            if budget and self._live_calls(state, episode_dirs, set()) >= budget:
                capped = True
                break
            stop_at = budget - min_pp * (len(PHASES) - idx - 1) if budget else 0
            before = self._session_dirs(state)
            fired = [False]

            def stop_check(before=before, fired=fired, stop_at=stop_at):
                if self._live_calls(state, episode_dirs,
                                    self._session_dirs(state) - before) >= stop_at:
                    fired[0] = True
                    return True
                return False

            try:
                proc = self.sandbox.run(
                    run_root,
                    ["--profile", "headless", spec.phase_prompts.get(phase, phase)],
                    env={"DEEPSEEK_API_KEY": self.key,
                         "DSH_PERMISSION_MODE": "workspace-write"},
                    timeout_s=self.phase_timeout_s,
                    mounts=((str(harness), "/workspace"), (str(state), "/state"))
                           + self._task_mount(run_root),
                    stop_check=stop_check if budget else None,
                )
            except subprocess.TimeoutExpired:
                error = f"phase {phase}: timeout after {self.phase_timeout_s}s"
                break
            new = self._session_dirs(state) - before
            if new:
                mapping[phase] = str(min(new).relative_to(state))
                episode_dirs |= new
            if proc.returncode != 0:
                if fired[0]:
                    # stopped at the phase's line: continue if it was only the reserve,
                    # end the episode only when the whole budget is spent
                    if budget and self._live_calls(state, episode_dirs, set()) >= budget:
                        capped = True
                        break
                    continue
                error = f"phase {phase}: exit {proc.returncode}: {proc.stderr[-400:]}"
                break
        (run_root / "traces" / f"ep{spec.episode:03d}.json").write_text(
            json.dumps(mapping, indent=1))
        trace = self.read_trace(run_root, spec.episode)
        return EpisodeResult(
            episode=spec.episode, ok=not error,
            turns=sum(1 for e in trace if e.tool), error=error,
            counters={"phases": len(mapping), "turn_capped": capped},
        )

    def _live_calls(self, state: Path, episode_dirs: set, extra: set) -> int:
        """Tool calls made so far this episode, read live from the session logs."""
        n = 0
        for d in set(episode_dirs) | set(extra):
            log = Path(d) if isinstance(d, Path) else state / d
            f = log / "session.jsonl.zstd"
            if f.exists():
                try:
                    n += _zstd_partial(f.read_bytes()).count(b'"tool/call"')
                except OSError:
                    continue
        return n

    # ------------------------------------------------------------------ measure path

    def _surface_for_path(self, file_path: str) -> Optional[str]:
        p = file_path.replace("/workspace/", "")
        if p == "AGENTS.md":
            return "instructions"
        if p.startswith("notes/"):
            return "notes"
        if p.startswith("tools/"):
            return "tools"
        if p.startswith("src/"):
            return "loop"
        return None

    def read_trace(self, root: Path, episode: int) -> Sequence[ActionEvent]:
        root = Path(root)
        map_path = root / "traces" / f"ep{episode:03d}.json"
        if not map_path.exists():
            return []
        mapping = json.loads(map_path.read_text())
        state = root / ".dsh-state"
        events: list[ActionEvent] = []
        turn_base = 0
        for phase in PHASES:
            rel = mapping.get(phase)
            if not rel:
                continue
            log = state / rel / "session.jsonl.zstd"
            if not log.exists():
                continue
            last_turn = 0
            for line in _zstd_decompress(log.read_bytes()).decode().splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = e.get("data", {})
                if e.get("type") == "tool/call":
                    try:
                        args = json.loads(data.get("arguments", "") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    last_turn = int(data.get("turn", last_turn))
                    events.append(ActionEvent(
                        turn=turn_base + last_turn, phase=phase,
                        tool=data.get("name", ""),
                        surface=self._surface_for_path(str(args.get("file_path", ""))),
                        params={k: str(v)[:200] for k, v in args.items()}, text="",
                    ))
                elif e.get("type") == "assistant/message":
                    parts = data.get("message", {}).get("content", [])
                    text = " ".join(c.get("text", "") for c in parts
                                    if c.get("type") == "text")
                    if text:
                        events.append(ActionEvent(
                            turn=turn_base + int(data.get("turn", last_turn)),
                            phase=phase, tool=None, surface=None, params={},
                            text=text[:500],
                        ))
            turn_base += last_turn
        return events

    def disposition_fingerprint(self, harness_root: Path) -> str:
        from proteus.adapters import instructions
        return instructions.block_fingerprint(Path(harness_root) / "AGENTS.md")
