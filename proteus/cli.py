"""Proteus command line.

    proteus run     --harness minimal --arm review:notes --goal none \
                    --seeds 4 --episodes 10 --out runs/demo
    proteus measure --out runs/demo          # structural + behavioural distance, per arm

`--arm` is `neutral`, or `review:<surface>` / `record:<surface>` (repeatable). `--goal` is
freeform objective text (`none` for no-goal); repeatable `--evaluator` flags independently
choose what is measured and whether the agent sees it. The default harness is `minimal`,
which runs offline; `dsh` and `pi` are the source-evolving container adapters, and `aki`
plugs in the reference research harness.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from proteus.core.budget import make_budget_plan
from proteus.core.disposition import NEUTRAL, record, review
from proteus.core.goal import GoalConfig
from proteus.sweep import SweepConfig, run_sweep


def _collapse_episodes(args) -> frozenset[int]:
    from proteus.safety.collapse_filler import parse_collapse_episodes

    spec = getattr(args, "collapse_episodes", "every:5")
    episodes = getattr(args, "episodes", 1)
    try:
        return parse_collapse_episodes(spec, episodes)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _candidate_gate_factory(
    args,
    *,
    adapter_factory,
    controller_root: Path,
    channel_factory=None,
):
    """Load safety only for an explicitly safety-gated run.

    Normal Minimal/benchmark runs must remain usable while optional safety providers are
    absent or have unavailable live dependencies, so the import belongs behind the CLI flag.
    """
    if not args.safety_suite:
        if args.safety_model:
            raise ValueError("--safety-model requires --safety-suite")
        return None
    from proteus.safety.gate import build_candidate_gate_factory

    return build_candidate_gate_factory(
        adapter_factory=adapter_factory,
        suite_spec=args.safety_suite,
        safety_model=args.safety_model,
        controller_root=controller_root,
        channel_factory=channel_factory,
        collapse_episodes=_collapse_episodes(args),
    )


def _controller_live_channel_factory(args, controller_root: Path):
    """Create the trusted ordinary/safety model controller outside adapter objects."""
    if args.harness not in {"llm", "pi", "dsh", "aki"}:
        return None
    if args.harness == "aki" and not _pi_controller_model(getattr(args, "model", "")):
        raise ValueError(
            "Aki ordinary episodes require --model selecting a host-owned OpenAI controller"
        )
    if (
        args.harness in {"pi", "dsh", "aki"}
        and not getattr(args, "safety_suite", "")
        and not _pi_controller_model(getattr(args, "model", ""))
    ):
        return None
    from proteus.safety.live import (
        OpenAIResponsesChannelFactory,
        common_repository_root,
    )

    return OpenAIResponsesChannelFactory.from_repository(
        repository_root=common_repository_root(Path.cwd()),
        evidence_root=controller_root / "live-model-ledgers",
    )


def _pi_controller_model(model: str) -> bool:
    """Whether an explicit container-harness model uses the OpenAI controller."""
    value = model.strip().lower()
    return value.startswith(("gpt-", "o1", "o3", "o4"))


_BUILTIN_PERMISSION_SUPPORT = {
    "minimal": 4,
    "llm": 4,
    "pi": 6,
    "dsh": 6,
    "aki": 5,
}


def _local_image_exists(harness: str) -> bool:
    """Return whether the pinned local image exists. Never prints image or credential payloads."""
    name = harness.strip().lower()
    if name in {"minimal", "llm"}:
        return True
    tags = {
        "pi": os.environ.get("PROTEUS_PI_IMAGE", "proteus-env-pi-src:0.84.2"),
        "dsh": os.environ.get("PROTEUS_DSH_IMAGE", "proteus-env-dsh-src:0.1.0-rc.7"),
        "aki": os.environ.get("PROTEUS_AKI_IMAGE", "proteus-env-aki-src:0.1.0"),
    }
    image = tags.get(name)
    if not image:
        return False
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _repository_openai_key_is_present() -> bool:
    """Return whether repository-root .env has a non-empty OPENAI_API_KEY without printing it."""
    from proteus.safety.live import (
        LiveConfigurationError,
        common_repository_root,
        load_repository_openai_key,
    )

    try:
        load_repository_openai_key(common_repository_root(Path.cwd()))
    except LiveConfigurationError:
        return False
    return True


def _builtin_permission_supported_cases(harness: str) -> int:
    name = harness.strip().lower()
    if name in _BUILTIN_PERMISSION_SUPPORT:
        return _BUILTIN_PERMISSION_SUPPORT[name]
    return 0


def _call_plan_payload(args) -> dict[str, object]:
    from proteus.safety.live import derive_builtin_live_call_plan

    plan = derive_builtin_live_call_plan(
        harness=args.harness,
        episodes=args.episodes,
        ordinary_hard_limit=args.max_turns,
        permission_supported_cases=_builtin_permission_supported_cases(args.harness),
        include_memory_families="phase1" in getattr(args, "suite", ""),
        collapse_episode_count=(
            len(_collapse_episodes(args))
            if "phase1" in getattr(args, "suite", "")
            else None
        ),
    )
    return {
        "harness": plan.harness,
        "ordinary_cap": plan.ordinary_cap,
        "safety_cap": plan.safety_cap,
        "total_cap": plan.total_cap,
    }


def _ordinary_live_channel_factory(args, controller_factory):
    """Keep empty/default container models on their established provider paths."""
    if (
        args.harness in {"pi", "dsh", "aki"}
        and not _pi_controller_model(getattr(args, "model", ""))
    ):
        return None
    return controller_factory


def _validate_run_model_configuration(args) -> None:
    """Reject incomplete Aki model routing before adapters or credentials are opened."""
    if args.harness != "aki":
        return
    if not _pi_controller_model(getattr(args, "model", "")):
        raise ValueError(
            "Aki ordinary episodes require --model selecting a host-owned OpenAI controller"
        )
    safety_suite = getattr(args, "safety_suite", "")
    safety_model = getattr(args, "safety_model", "")
    if safety_suite and not safety_model.strip():
        raise ValueError("Aki safety episodes require --safety-model")
    if safety_model and not safety_suite:
        raise ValueError("--safety-model requires --safety-suite")


def _load_seed_records(root: Path) -> list[dict]:
    """CLI-facing seed reader with one clean error path and last-write-wins semantics."""
    path = root / "seeds.jsonl"
    if not path.exists():
        raise SystemExit(f"no seeds.jsonl at {path}")
    from proteus.sweep import read_seed_records
    try:
        records = read_seed_records(root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if not records:
        raise SystemExit(f"no seed records in {path}")
    return records


def _adapter_factory(name: str):
    if name == "minimal":
        from proteus.adapters.minimal import MinimalHarness
        return MinimalHarness
    if name == "llm":
        from proteus.adapters.llm import LLMHarness
        return LLMHarness
    if name == "dsh":
        from proteus.adapters.dsh import DshHarness
        return DshHarness
    if name == "pi":
        from proteus.adapters.pi import PiHarness
        return PiHarness
    if name == "aki":
        from proteus.adapters.aki import AkiHarness
        return AkiHarness
    if ":" in name:
        # your own adapter, no registration needed: --harness mypkg.mymodule:MyHarness
        import importlib
        mod_name, _, cls_name = name.partition(":")
        # A generated adapter is commonly an uninstalled module in the current project.
        # Python adds that directory for ``python -m ...`` but not for a console-script
        # entry point such as ``proteus``.  Put it first just for this import so the
        # scaffolder's printed next command also works from a regular wheel install.
        cwd = str(Path.cwd())
        sys.path.insert(0, cwd)
        try:
            cls = getattr(importlib.import_module(mod_name), cls_name)
        except (ImportError, AttributeError) as exc:
            raise SystemExit(f"cannot load harness {name!r}: {exc}") from exc
        finally:
            try:
                sys.path.remove(cwd)
            except ValueError:  # pragma: no cover - defensive against import hooks
                pass
        return cls
    raise SystemExit(f"unknown harness {name!r} "
                     "(built-in: minimal, llm, dsh, pi, aki; or use <module>:<Class>)")


def _sandbox_factory(args):
    """A callable returning a fresh sandbox per run, or None to leave the adapter's default.

    `--env` is what the user brings: an image reference, or a directory / TOML manifest
    describing one. The individual flags override whatever the manifest said.
    """
    if not (args.env or args.network or args.mem or args.cpus or args.docker_arg):
        return None
    from proteus.sandbox import DockerSandbox, SandboxConfig
    overrides = {"network": args.network, "mem_limit": args.mem, "cpus": args.cpus,
                 "extra_args": tuple(args.docker_arg or ())}
    try:
        cfg = (SandboxConfig.from_spec(args.env, **overrides) if args.env
               else SandboxConfig(**{k: v for k, v in overrides.items() if v}))
    except (OSError, KeyError, ValueError) as exc:
        raise SystemExit(f"bad --env {args.env!r}: {exc}") from None
    return lambda: DockerSandbox(cfg)


def _harness_factory(args):
    """The adapter factory the sweep calls per run, with the user's environment and
    per-episode caps applied to whichever adapter accepts them."""
    import inspect
    cls = _adapter_factory(args.harness)
    sandbox = _sandbox_factory(args)
    params = set(inspect.signature(cls).parameters)
    kw = {}
    if sandbox is not None and "sandbox" in params:
        kw["sandbox"] = None      # filled per call below
    if args.phase_timeout and "phase_timeout_s" in params:
        kw["phase_timeout_s"] = args.phase_timeout
    if sandbox is not None and "sandbox" not in params:
        raise SystemExit(
            f"harness {args.harness!r} does not take a sandbox, so --env/--network/--mem/"
            "--cpus/--docker-arg cannot apply to it (containerised built-ins: dsh, pi)")

    def make():
        call = dict(kw)
        if sandbox is not None:
            call["sandbox"] = sandbox()
        return cls(**call)
    return make


def _preflight_run_harness(factory) -> None:
    """Run an adapter's selected-run preflight before opening model capabilities."""
    adapter = factory()
    preflight = getattr(adapter, "preflight", None)
    if callable(preflight):
        preflight()


def _arm(spec: str):
    if spec == "neutral":
        return NEUTRAL
    kind, _, surface = spec.partition(":")
    if kind == "review" and surface:
        return review(surface)
    if kind == "record" and surface:
        return record(surface)
    raise SystemExit(f"bad --arm {spec!r} (use neutral | review:<surface> | record:<surface>)")


def _goal(spec: str, evaluators) -> GoalConfig:
    """`--goal` is freeform text ("none" for the no-goal condition); evaluators attach
    independently via --evaluator. `task:<text>` is accepted for compatibility."""
    text = "" if spec == "none" else spec.partition(":")[2] if spec.startswith("task:") else spec
    return GoalConfig.of(text=text, evaluators=evaluators)


def _phase_turns(spec: str) -> dict[str, int]:
    """Parse ``observe=40,propose=25,act=200,reflect=35``."""
    values: dict[str, int] = {}
    for item in spec.split(","):
        name, sep, raw = item.strip().partition("=")
        if not sep or not name or not raw:
            raise argparse.ArgumentTypeError(
                "use observe=N,propose=N,act=N,reflect=N"
            )
        if name in values:
            raise argparse.ArgumentTypeError(f"phase {name!r} appears more than once")
        try:
            values[name] = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"phase {name!r} needs an integer") from None
    return values


def _evaluator(spec: str, adapter_factory):
    """Parse one --evaluator: `<what>[@hidden|@observe]`. Returns (spec, task | None).

    measurement: units:<surface-name> | tool-calls | step
    benchmark:   local:<task> | polyglot:<exercise> | mbpp:<task-id> |
                 humaneval:<task-id>
    custom:      contains:<relpath>:<needle>
    """
    from proteus.bench.task import as_evaluator
    from proteus.core import EvaluatorSpec, Visibility
    from proteus.core import evaluators as ev
    body, _, vis = spec.partition("@")
    if vis and vis not in ("hidden", "observe"):
        raise SystemExit(f"bad --evaluator {spec!r}: visibility is @hidden or @observe")
    visibility = Visibility(vis) if vis else Visibility.HIDDEN
    kind, _, arg = body.partition(":")
    if kind in ("local", "polyglot", "mbpp", "humaneval") and arg:
        try:
            if kind == "local":
                from proteus.bench.local import local_task
                task = local_task(f"local:{arg}")
            elif kind == "polyglot":
                from proteus.bench.polyglot import polyglot_task
                task = polyglot_task(arg)
            elif kind == "mbpp":
                from proteus.bench.mbpp import mbpp_task
                task = mbpp_task(arg)
            else:
                from proteus.bench.humaneval import humaneval_task
                task = humaneval_task(arg)
        except KeyError as exc:
            raise SystemExit(str(exc)) from None
        return EvaluatorSpec(name=task.id, run=as_evaluator(task), kind="benchmark",
                             visibility=visibility), task
    if kind == "units" and arg:
        surfaces = list(adapter_factory().surfaces())
        surface = next((s for s in surfaces if s.name == arg), None)
        # Accept the declared subdir too for compatibility with early v0.1 examples, but
        # canonicalize the evaluator identity to the surface name in all records.
        if surface is None:
            surface = next((s for s in surfaces if s.subdir == arg), None)
        if surface is None:
            choices = ", ".join(s.name for s in surfaces) or "(none declared)"
            raise SystemExit(
                f"unknown surface {arg!r} for this harness; choose one of: {choices}")
        return EvaluatorSpec(name=f"units:{surface.name}", run=ev.surface_units(surface),
                             kind="measurement", visibility=visibility), None
    if kind == "tool-calls":
        return EvaluatorSpec(name="tool-calls", run=ev.tool_calls(),
                             kind="measurement", visibility=visibility), None
    if kind == "step":
        surfaces = adapter_factory().surfaces()
        return EvaluatorSpec(name="structural-step", run=ev.structural_step(surfaces),
                             kind="measurement", visibility=visibility), None
    if kind == "contains":
        relpath, _, needle = arg.partition(":")
        if relpath and needle:
            return EvaluatorSpec(name=f"contains:{relpath}", kind="custom",
                                 run=ev.file_contains(relpath, needle),
                                 visibility=visibility), None
    raise SystemExit(
        f"bad --evaluator {spec!r} "
        "(use units:<surface-name> | tool-calls | step | local:<task> | "
        "polyglot:<exercise> | mbpp:<task-id> | humaneval:<task-id> "
        "| contains:<relpath>:<needle>, with optional @hidden/@observe)")


def cmd_run(args) -> int:
    import hashlib

    if args.max_turns < 0:
        raise SystemExit("--max-turns must be 0 (unlimited) or a positive integer")
    if args.min_turns_per_phase < 0:
        raise SystemExit("--min-turns-per-phase must be 0 or a positive integer")
    required = args.min_turns_per_phase * 4
    if required and (not args.max_turns or required > args.max_turns):
        raise SystemExit(
            f"--max-turns={args.max_turns} cannot reserve "
            f"--min-turns-per-phase={args.min_turns_per_phase} across 4 phases; "
            f"use --max-turns >= {required}"
        )
    try:
        make_budget_plan(
            max_turns=args.max_turns,
            min_turns_per_phase=args.min_turns_per_phase,
            phase_turns=args.phase_turns,
            hard_max_turns=args.hard_max_turns,
            checkpoint_turns=args.checkpoint_turns,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if args.checkpoint_turns and not args.announce_budget:
        raise SystemExit("--checkpoint-turns requires --announce-budget")
    try:
        _validate_run_model_configuration(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    root = Path(args.out).expanduser()
    factory = _harness_factory(args)
    try:
        _preflight_run_harness(factory)
        controller_channel_factory = _controller_live_channel_factory(args, root)
        if (
            controller_channel_factory is not None
            and args.safety_suite
            and args.harness in _BUILTIN_PERMISSION_SUPPORT
        ):
            from proteus.safety.live import (
                ControllerLiveCallBudget,
                LiveCallCategory,
                derive_builtin_live_call_plan,
            )

            plan = derive_builtin_live_call_plan(
                harness=args.harness,
                episodes=args.episodes,
                ordinary_hard_limit=args.max_turns,
                permission_supported_cases=(
                    _builtin_permission_supported_cases(args.harness)
                    if args.safety_suite
                    else 0
                ),
                include_memory_families="phase1" in args.safety_suite,
                collapse_episode_count=(
                    len(_collapse_episodes(args))
                    if "phase1" in args.safety_suite
                    else None
                ),
            )
            inner_factory = controller_channel_factory
            budget_holder: list = []

            def controller_channel_factory(model: str, cell_id: str):
                if not budget_holder:
                    budget_holder.append(
                        ControllerLiveCallBudget(plan, root / "call-budget.json")
                    )
                category = (
                    LiveCallCategory.SAFETY
                    if any(
                        name in cell_id
                        for name in (
                            "tools_permission_drift",
                            "memory_bad_admission",
                            "memory_collapse",
                        )
                    )
                    else LiveCallCategory.ORDINARY
                )
                channel_cap = (
                    max(plan.safety_cap, 1)
                    if category is LiveCallCategory.SAFETY
                    else max(plan.ordinary_cap, 1)
                )
                return budget_holder[0].wrap(
                    inner_factory(model, cell_id),
                    category=category,
                    cell_id=cell_id,
                    channel_cap=channel_cap,
                )

        candidate_gate_factory = _candidate_gate_factory(
            args,
            adapter_factory=factory,
            controller_root=root,
            channel_factory=controller_channel_factory,
        )
        ordinary_channel_factory = _ordinary_live_channel_factory(
            args, controller_channel_factory
        )
    except (ImportError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    parsed = [_evaluator(e, factory) for e in (args.evaluator or ())]
    evaluators = tuple(spec for spec, _ in parsed)
    tasks = [task for _, task in parsed if task is not None]
    if len(tasks) > 1:
        raise SystemExit("only one benchmark evaluator per run: each run has one task "
                         f"workspace, and {len(tasks)} were attached")
    cfg = SweepConfig(
        name=args.out,
        adapter_factory=factory,
        arms=[_arm(a) for a in args.arm],
        seeds=args.seeds,
        goal=_goal(args.goal, evaluators),
        root=root,
        model=args.model,
        episodes=args.episodes,
        max_turns=args.max_turns,
        task=(tasks[0] if tasks else None),
        condition_metadata={
            # A contains:<path>:<needle> evaluator may embed sensitive literal text.
            # The normal manifest rows already expose its public identity; digests keep
            # resume sensitive to every raw CLI parameter without publishing the needle.
            "evaluator_specs": [
                hashlib.sha256(spec.encode()).hexdigest()
                for spec in (args.evaluator or ())
            ],
        },
        min_turns_per_phase=args.min_turns_per_phase,
        phase_turns=args.phase_turns,
        hard_max_turns=args.hard_max_turns,
        checkpoint_turns=args.checkpoint_turns,
        announce_budget=args.announce_budget,
        on_existing=args.on_existing,
        live_channel_factory=ordinary_channel_factory,
        candidate_gate_factory=candidate_gate_factory,
        candidate_gate_config=(
            {
                "suite": args.safety_suite,
                "model": args.safety_model,
                "collapse_episodes": sorted(_collapse_episodes(args)),
            }
            if args.safety_suite else {}
        ),
    )
    try:
        records = run_sweep(cfg)
    except (FileExistsError, ImportError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    done = sum(r["episodes_complete"] for r in records)
    print(f"ran {len(records)} seeds, {done} episodes -> {args.out}")
    failed = [r for r in records
              if r.get("error") or r.get("episodes_complete", 0) < cfg.episodes]
    if failed:
        print(f"{len(failed)} seed(s) failed or stopped before {cfg.episodes} episodes",
              file=sys.stderr)
        return 1
    return 0


def cmd_audit(args) -> int:
    """Which seeds left their harness, and which worked out they are an instrument."""
    from proteus.measure import audit
    root = Path(args.out).expanduser()
    adapter = _adapter_factory(args.harness)()
    records = _load_seed_records(root)
    # the study's own directories are what a subject must not be naming back at us
    outside = tuple(args.outside) or (str(root / "runs"), str(root))
    flagged = 0
    for rec in records:
        run_root = Path(rec["root"])
        trace = adapter.read_trace(run_root, rec.get("episodes_complete", 0))
        result = audit.audit_run(run_root, trace, outside=outside)
        if result.clean:
            continue
        flagged += 1
        print(f"\n{rec['arm']}/{rec['seed']}  {result}")
        for f in result.findings[:args.max_findings]:
            print(f"    [{f.kind}] {f.where}: {f.detail}")
            if f.quote:
                print(f"        {f.quote[:160]}")
    print(f"\n{flagged} of {len(records)} seeds flagged. "
          "Findings are evidence to read, not a verdict — a tool may import subprocess "
          "and never leave.")
    return 0


def cmd_reliability(args) -> int:
    """Does each arm reproduce itself? Report before reading any between-arm ratio."""
    from collections import defaultdict

    from proteus.measure import stream
    root = Path(args.out).expanduser()
    adapter = _adapter_factory(args.harness)()
    records = _load_seed_records(root)
    by_arm = defaultdict(list)
    for rec in records:
        trace = adapter.read_trace(Path(rec["root"]), rec.get("episodes_complete", 0))
        by_arm[rec["arm"]].append(stream.tool_stream(trace))
    print(f"{'arm':<18}{'n':>4}{'within':>10}{'null':>10}{'ratio':>8}  reliable")
    ok = True
    for arm, runs in sorted(by_arm.items()):
        if len(runs) < 2:
            print(f"{arm:<18}{len(runs):>4}   (needs 2+ runs)")
            continue
        r = stream.reliability(runs, level=args.level, draws=args.draws)
        ok &= r["reliable"]
        print(f"{arm:<18}{r['n_runs']:>4}{r['within']:>10.4f}{r['null']:>10.4f}"
              f"{r['ratio']:>8.2f}  {'yes' if r['reliable'] else 'NO'}")
    if not ok:
        print("\nAn arm that does not reproduce itself voids the between-arm comparison: "
              "R divides by that spread.")
    return 0


def _travel(run_root: Path, episodes: int, surfaces) -> dict:
    """Materialise every episode state from the snapshot chain and sum path length."""
    import tempfile

    from proteus.core import snapshot
    from proteus.measure import distance
    work = run_root / "harness"
    states = []
    with tempfile.TemporaryDirectory() as tmp:
        for ep in range(episodes + 1):
            sha = snapshot.commit_for_episode(work, ep)
            if sha is None:
                continue
            dest = Path(tmp) / f"s{ep}"
            snapshot.materialize(work, sha, dest)
            states.append(dest)
        return distance.path_length(states, surfaces)


def cmd_measure(args) -> int:
    import statistics as st
    from collections import defaultdict

    from proteus.measure import distance, stream
    root = Path(args.out).expanduser()
    adapter = _adapter_factory(args.harness)()
    surfaces = adapter.surfaces()
    records = _load_seed_records(root)

    # structural: per-arm, per-surface final unit counts (what got built)
    arm_surface = defaultdict(lambda: defaultdict(list))
    arm_streams = defaultdict(list)
    arm_travel = defaultdict(lambda: defaultdict(list))
    for rec in records:
        work = Path(rec["root"]) / "harness"
        final = distance.units(work, surfaces)
        for sname, u in final.items():
            arm_surface[rec["arm"]][sname].append(len(u))
        last = adapter.read_trace(Path(rec["root"]), rec["episodes_complete"])
        arm_streams[rec["arm"]].append(stream.tool_stream(last))
        if args.travel:
            pl = _travel(Path(rec["root"]), rec["episodes_complete"], surfaces)
            for sname, d in pl.items():
                arm_travel[rec["arm"]][sname].append(d.added + d.dropped + d.revised)

    names = [s.name for s in surfaces]
    print(f"{'arm':<16}{'seeds':>6}" + "".join(f"{n:>12}" for n in names) + "   (mean units built)")
    for arm in arm_surface:
        row = "".join(f"{st.mean(arm_surface[arm][n]):>12.1f}" for n in names)
        print(f"{arm:<16}{len(arm_streams[arm]):>6}{row}")

    if args.travel and arm_travel:
        print(f"\n{'arm':<16}{'':>6}" + "".join(f"{n:>12}" for n in names)
              + "   (mean travel: units added+dropped+revised along the path)")
        for arm in arm_travel:
            row = "".join(f"{st.mean(arm_travel[arm][n]):>12.1f}" for n in names)
            print(f"{arm:<16}{'':>6}{row}")

    if len(arm_streams) > 1:
        if any(len(v) >= 2 for v in arm_streams.values()):
            r = stream.between_within(dict(arm_streams), level="freq", permutations=2000)
            print(f"\nbehavioural R (between/within arms, last episode): "
                  f"{r['R']:.3f}  p={r['p']:.4f}")
        else:
            print("\nbehavioural R: not computed (needs 2+ seeds per arm)")
    return 0


def cmd_check(args) -> int:
    from proteus.testing import check_adapter
    adapter = _adapter_factory(args.harness)()
    return len(check_adapter(adapter, episode=args.episode))


def cmd_env_scaffold(args) -> int:
    from proteus.envs import scaffold
    manifest = scaffold(args.source, args.name, ref=args.ref,
                        use_local_dockerfile=args.local_dockerfile)
    print(f"scaffolded {manifest}")
    print(f"next: proteus env build {args.name}")
    return 0


def cmd_env_build(args) -> int:
    from proteus.envs import build
    tag = build(args.name)
    print(f"built {tag} (recorded in the manifest)")
    return 0


def cmd_report(args) -> int:
    from proteus.report import write_report
    out = write_report(Path(args.out).expanduser())
    print(f"wrote {out} (serve with: proteus watch --out {args.out})")
    return 0


def cmd_watch(args) -> int:
    from proteus.report import serve, write_report
    root = Path(args.out).expanduser()
    write_report(root)
    serve(root, port=args.port)
    return 0


def cmd_safety_call_plan(args) -> int:
    """Print the exact ordinary/safety call plan without credentials or output."""
    print(json.dumps(_call_plan_payload(args), sort_keys=True))
    return 0


def cmd_safety_preflight_permission(args) -> int:
    """Check permission-run inputs without creating channels, containers, or output."""
    from proteus.safety.gate import _load_suite
    from proteus.safety.phase1 import TOOLS_PERMISSION_DRIFT
    from proteus.safety.tools_permission_drift import SUITE as ISOLATED_SUITE

    output = Path(args.out).expanduser()
    if output.exists():
        raise SystemExit("permission preflight output path already exists")
    if args.model != "gpt-5.6-luna" or args.safety_model != "gpt-5.6-luna":
        raise SystemExit("permission preflight requires gpt-5.6-luna")
    allowed_suites = {
        "proteus.safety.tools_permission_drift:SUITE",
        "proteus.safety.phase1:SUITE",
    }
    if args.suite not in allowed_suites:
        raise SystemExit("permission preflight requires the Phase 1 or isolated version-2 suite")
    _, definitions = _load_suite(args.suite)
    family = next(
        item for item in definitions if item.family_id == TOOLS_PERMISSION_DRIFT.family_id
    )
    if family is not TOOLS_PERMISSION_DRIFT or ISOLATED_SUITE.version != "2":
        raise SystemExit("permission preflight requires tools_permission_drift version 2")
    if args.harness in {"pi", "dsh", "aki"} and not _local_image_exists(args.harness):
        raise SystemExit("pinned local image is not available")
    if args.harness in {"llm", "pi", "dsh", "aki"} and not _repository_openai_key_is_present():
        raise SystemExit("repository OpenAI key is not present")
    print(json.dumps(_call_plan_payload(args), sort_keys=True))
    return 0


def cmd_safety_audit_permission(args) -> int:
    from proteus.safety.publication import json_value
    from proteus.safety.reporting import audit_permission_artifact

    audit = audit_permission_artifact(Path(args.root).expanduser())
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(json_value(audit), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0 if audit.complete else 1


def cmd_safety_harness_report(args) -> int:
    from proteus.safety.reporting import write_harness_safety_report

    json_path, markdown_path = write_harness_safety_report(
        artifact_roots=tuple(Path(root).expanduser() for root in args.artifact),
        output_root=Path(args.out).expanduser(),
    )
    print(f"wrote {json_path} and {markdown_path}")
    return 0


def cmd_safety_retrospective(args) -> int:
    """Replay preserved checkpoints without turning historical evidence into activation."""
    from proteus.safety.retrospective import LiveModelConfig, run_retrospective_phase1
    from proteus.safety.runtime import RuntimeKind

    adapter = _adapter_factory(args.harness)()
    runtime = adapter.safety_runtime() if callable(getattr(adapter, "safety_runtime", None)) else None
    if runtime is None:
        raise SystemExit(f"harness {args.harness!r} does not implement safety_runtime()")
    model_config = None
    if runtime.kind is RuntimeKind.MODEL_MEDIATED:
        if not args.model:
            raise SystemExit("model-mediated retrospective replay requires --model")
        from proteus.safety.live import OpenAIResponsesChannelFactory, common_repository_root

        model_config = LiveModelConfig(
            model=args.model,
            build_channel_factory=lambda artifact_root: (
                OpenAIResponsesChannelFactory.from_repository(
                    repository_root=common_repository_root(Path.cwd()),
                    evidence_root=artifact_root / "live-model-ledgers",
                )
            ),
        )
    elif args.model:
        raise SystemExit("deterministic retrospective replay does not use --model")
    try:
        summary = run_retrospective_phase1(
            sweep_root=Path(args.sweep).expanduser(),
            adapter=adapter,
            output_root=Path(args.out).expanduser(),
            model_config=model_config,
            run_id=args.run_id,
            active_episode=args.active_episode,
        )
    except (FileExistsError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(
        f"replayed {summary.transitions_administered}/{summary.transitions_attempted} "
        f"preserved transitions -> {args.out}"
    )
    return 0 if summary.complete else 1


def cmd_repo(args) -> int:
    from proteus.report import export_repo, push_repo
    if args.repo_cmd == "export":
        dest = export_repo(Path(args.run_root), Path(args.dest), branch=args.branch)
        print(f"exported evolution history to {dest} (one commit per episode)")
    else:
        push_repo(Path(args.run_root), args.remote, branch=args.branch)
        print(f"pushed {args.run_root} -> {args.remote} ({args.branch})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="proteus", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a self-evolution sweep")
    r.add_argument("--harness", default="minimal")
    r.add_argument("--arm", action="append", default=None,
                   help="neutral | review:<surface> | record:<surface> (repeatable)")
    r.add_argument("--goal", default="none",
                   help="freeform objective text shown to the agent, or 'none'. A goal "
                        "names an aim; --evaluator decides what is measured. If the goal "
                        "is specific (e.g. a benchmark), attach that benchmark as an "
                        "evaluator and set it @observe, or the agent cannot pursue it.")
    r.add_argument("--evaluator", action="append", metavar="SPEC",
                   help="attach an evaluator, repeatable: units:<surface-name> | "
                        "tool-calls | step | local:<task> | polyglot:<exercise> | "
                        "mbpp:<task-id> | humaneval:<task-id> | "
                        "contains:<relpath>:<needle>, each with optional @hidden "
                        "(default) or @observe; at most one benchmark task per run")
    r.add_argument("--seeds", type=int, default=4)
    r.add_argument("--episodes", type=int, default=10)
    r.add_argument("--max-turns", type=int, default=100)
    r.add_argument("--min-turns-per-phase", type=int, default=0,
                   help="reserve at least this many turns for every phase: a phase that "
                        "would starve the later ones is ended early (max-turns must be "
                        ">= 4x this)")
    r.add_argument("--phase-turns", type=_phase_turns, default={}, metavar="PLAN",
                   help="explicit normal allocation, e.g. "
                        "observe=40,propose=25,act=200,reflect=35; replaces "
                        "--min-turns-per-phase and must sum to --max-turns")
    r.add_argument("--hard-max-turns", type=int, default=0, metavar="N",
                   help="burst ceiling for --phase-turns (default: --max-turns); unused "
                        "early quota and burst capacity are prioritised for act")
    r.add_argument("--checkpoint-turns", type=int, default=0, metavar="N",
                   help="reserve the final N calls of each planned phase for a persistent "
                        "handoff; requires --phase-turns and --announce-budget")
    r.add_argument("--announce-budget", action="store_true",
                   help="tell the agent its live used/remaining calls and phase allowance "
                        "(recorded in the manifest; announcing changes behaviour)")
    r.add_argument("--phase-timeout", type=int, default=0, metavar="S",
                   help="wall-clock limit per phase for containerised harnesses "
                        "(default: the adapter's own, 600s)")
    r.add_argument("--env", default="", metavar="SPEC",
                   help="the container to evolve in: an image reference, or a directory / "
                        "environment.toml describing one")
    r.add_argument("--network", default="", choices=("", "none", "host", "bridge"),
                   help="container network (default: the environment's, else none)")
    r.add_argument("--mem", default="", metavar="LIMIT", help="container memory, e.g. 4g")
    r.add_argument("--cpus", default="", metavar="N", help="container cpu limit, e.g. 2")
    r.add_argument("--docker-arg", action="append", metavar="FLAG",
                   help="extra `docker run` flag, repeatable (e.g. --docker-arg --gpus "
                        "--docker-arg all)")
    r.add_argument("--on-existing", choices=("refuse", "resume", "overwrite"),
                   default="refuse",
                   help="what to do when --out already holds runs: refuse (default), "
                        "resume unfinished seeds, or overwrite them")
    r.add_argument("--model", default="",
                   help="model name; empty uses the adapter's default")
    r.add_argument("--safety-suite", default="", metavar="SPEC",
                   help="optional candidate safety suite; loads safety runtime lazily")
    r.add_argument("--safety-model", default="", metavar="MODEL",
                   help="fixed model for the optional safety suite (requires --safety-suite)")
    r.add_argument(
        "--collapse-episodes",
        default="every:5",
        metavar="LIST",
        help="episodes for memory_collapse (integers, last, or every:N; "
             "default every:5 → 1,5,10,...). Admission and permission still run every episode",
    )
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("measure", help="measure a finished sweep")
    m.add_argument("--harness", default="minimal")
    m.add_argument("--out", required=True)
    m.add_argument("--travel", action="store_true",
                   help="also compute per-surface path length over episode snapshots")
    m.set_defaults(func=cmd_measure)

    safety = sub.add_parser("safety", help="controller-owned safety operations")
    safety_sub = safety.add_subparsers(dest="safety_cmd", required=True)
    retrospective = safety_sub.add_parser(
        "retrospective", help="replay retained transitions without activation"
    )
    retrospective.add_argument("--harness", required=True)
    retrospective.add_argument("--sweep", required=True, help="existing sweep root")
    retrospective.add_argument("--out", required=True, help="new retrospective artifact root")
    retrospective.add_argument("--model", default="", help="required for model-mediated runtimes")
    retrospective.add_argument("--run-id", default=None, help="logical run ID for one pair")
    retrospective.add_argument(
        "--active-episode", type=int, default=None, help="active episode for one pair"
    )
    retrospective.set_defaults(func=cmd_safety_retrospective)
    call_plan = safety_sub.add_parser("call-plan", help="print ordinary/safety live-call caps")
    call_plan.add_argument("--harness", required=True)
    call_plan.add_argument("--episodes", type=int, required=True)
    call_plan.add_argument("--max-turns", type=int, required=True)
    call_plan.add_argument("--suite", default="proteus.safety.tools_permission_drift:SUITE")
    call_plan.add_argument(
        "--collapse-episodes",
        default="1,last",
        metavar="LIST",
        help="episodes for memory_collapse occupancy probes (integers or last, default 1,last)",
    )
    call_plan.set_defaults(func=cmd_safety_call_plan)
    preflight = safety_sub.add_parser(
        "preflight-permission", help="check permission-run inputs without opening a channel"
    )
    preflight.add_argument("--harness", required=True)
    preflight.add_argument("--model", required=True)
    preflight.add_argument("--safety-model", required=True)
    preflight.add_argument("--suite", required=True)
    preflight.add_argument("--episodes", type=int, required=True)
    preflight.add_argument("--max-turns", type=int, required=True)
    preflight.add_argument("--out", required=True)
    preflight.set_defaults(func=cmd_safety_preflight_permission)
    audit_permission = safety_sub.add_parser(
        "audit-permission", help="audit a version-2 permission artifact root"
    )
    audit_permission.add_argument("--root", required=True)
    audit_permission.add_argument("--out", required=True)
    audit_permission.set_defaults(func=cmd_safety_audit_permission)
    harness_report = safety_sub.add_parser(
        "harness-report", help="write the three-family harness safety report"
    )
    harness_report.add_argument("--artifact", action="append", required=True)
    harness_report.add_argument("--out", required=True)
    harness_report.set_defaults(func=cmd_safety_harness_report)

    a = sub.add_parser("audit", help="escape and awareness evidence for a finished sweep")
    a.add_argument("--out", required=True)
    a.add_argument("--harness", default="minimal")
    a.add_argument("--outside", action="append", default=[],
                   help="path fragment the subject must not name (repeatable); "
                        "defaults to the sweep root")
    a.add_argument("--max-findings", type=int, default=6)
    a.set_defaults(func=cmd_audit)

    rel = sub.add_parser("reliability",
                         help="does each arm reproduce itself? (run before `measure`)")
    rel.add_argument("--out", required=True)
    rel.add_argument("--harness", default="minimal")
    rel.add_argument("--level", default="freq", choices=("freq", "order", "ncd"))
    rel.add_argument("--draws", type=int, default=200)
    rel.set_defaults(func=cmd_reliability)

    c = sub.add_parser("check", help="compliance-check a HarnessAdapter implementation")
    c.add_argument("--harness", required=True, help="built-in name or <module>:<Class>")
    c.add_argument("--episode", action="store_true",
                   help="also run one neutral episode (may launch containers / cost money)")
    c.set_defaults(func=cmd_check)

    e = sub.add_parser("env", help="prepared environments: scaffold/build from a harness repo")
    esub = e.add_subparsers(dest="env_cmd", required=True)
    es = esub.add_parser("scaffold", help="write environments/<name>/ for a harness repo")
    es.add_argument("--from", dest="source", required=True,
                    help="git URL or local path of the harness repo")
    es.add_argument("--name", required=True)
    es.add_argument("--ref", default="", help="branch/tag/sha to pin")
    es.add_argument("--local-dockerfile", action="store_true",
                    help="use a wrapper Dockerfile in environments/<name>/ (stub written)")
    es.set_defaults(func=cmd_env_scaffold)
    eb = esub.add_parser("build", help="build the environment image from its manifest")
    eb.add_argument("name", help="environment name or a path to environment.toml")
    eb.set_defaults(func=cmd_env_build)

    rp = sub.add_parser("report", help="write the tracking page into a sweep root")
    rp.add_argument("--out", required=True)
    rp.set_defaults(func=cmd_report)

    w = sub.add_parser("watch", help="serve a sweep's live tracking page")
    w.add_argument("--out", required=True)
    w.add_argument("--port", type=int, default=8300)
    w.set_defaults(func=cmd_watch)

    g = sub.add_parser("repo", help="export or push a run's evolution history (git)")
    gsub = g.add_subparsers(dest="repo_cmd", required=True)
    ge = gsub.add_parser("export", help="clone the snapshot chain into a normal repo")
    ge.add_argument("run_root")
    ge.add_argument("dest")
    ge.add_argument("--branch", default="main")
    ge.set_defaults(func=cmd_repo)
    gp = gsub.add_parser("push", help="push the snapshot chain to a remote you provide")
    gp.add_argument("run_root")
    gp.add_argument("remote")
    gp.add_argument("--branch", default="main")
    gp.set_defaults(func=cmd_repo)

    args = ap.parse_args(argv)
    if args.cmd == "run" and not args.arm:
        args.arm = ["neutral", "review:notes", "review:tools"]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
