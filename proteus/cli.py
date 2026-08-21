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
import sys
from pathlib import Path

from proteus.core.disposition import NEUTRAL, record, review
from proteus.core.goal import GoalConfig
from proteus.sweep import SweepConfig, run_sweep


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
        try:
            cls = getattr(importlib.import_module(mod_name), cls_name)
        except (ImportError, AttributeError) as exc:
            raise SystemExit(f"cannot load harness {name!r}: {exc}") from exc
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


def _evaluator(spec: str, adapter_factory):
    """Parse one --evaluator: `<what>[@hidden|@observe]`. Returns (spec, task | None).

    measurement: units:<surface-name> | tool-calls | step
    benchmark:   local:<task> | polyglot:<exercise> | dsh-audio
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
    if kind == "dsh-audio" and not arg:
        adapter = adapter_factory()
        if getattr(adapter, "name", "") != "dsh":
            raise SystemExit("dsh-audio is a DeepSeek Harness capability benchmark; "
                             "use --harness dsh")
        from proteus.bench.dsh_audio import NAME, evaluate_audio_capability
        return EvaluatorSpec(name=NAME, run=evaluate_audio_capability, kind="benchmark",
                             visibility=visibility), None
    if kind in ("local", "polyglot") and arg:
        try:
            if kind == "local":
                from proteus.bench.local import local_task
                task = local_task(f"local:{arg}")
            else:
                from proteus.bench.polyglot import polyglot_task
                task = polyglot_task(arg)
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
        "(use units:<surface-name> | tool-calls | step | local:<task> | dsh-audio | "
        "polyglot:<exercise> "
        "| contains:<relpath>:<needle>, with optional @hidden/@observe)")


def cmd_run(args) -> int:
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
    factory = _harness_factory(args)
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
        root=Path(args.out).expanduser(),
        model=args.model,
        episodes=args.episodes,
        max_turns=args.max_turns,
        task=(tasks[0] if tasks else None),
        min_turns_per_phase=args.min_turns_per_phase,
        announce_budget=args.announce_budget,
        on_existing=args.on_existing,
    )
    try:
        records = run_sweep(cfg)
    except FileExistsError as exc:
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
                        "tool-calls | step | local:<task> | polyglot:<exercise> | dsh-audio | "
                        "contains:<relpath>:<needle>, each with optional @hidden "
                        "(default) or @observe; at most one benchmark task per run")
    r.add_argument("--seeds", type=int, default=4)
    r.add_argument("--episodes", type=int, default=10)
    r.add_argument("--max-turns", type=int, default=100)
    r.add_argument("--min-turns-per-phase", type=int, default=0,
                   help="reserve at least this many turns for every phase: a phase that "
                        "would starve the later ones is ended early (max-turns must be "
                        ">= 4x this)")
    r.add_argument("--announce-budget", action="store_true",
                   help="tell the agent its per-episode tool-call budget in every phase "
                        "prompt (recorded in the manifest; announcing changes behaviour)")
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
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("measure", help="measure a finished sweep")
    m.add_argument("--harness", default="minimal")
    m.add_argument("--out", required=True)
    m.add_argument("--travel", action="store_true",
                   help="also compute per-surface path length over episode snapshots")
    m.set_defaults(func=cmd_measure)

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
