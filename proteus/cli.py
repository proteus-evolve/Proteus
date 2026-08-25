"""Proteus command line.

    proteus run     --harness minimal --arm review:notes --goal none \
                    --seeds 4 --episodes 10 --out runs/demo
    proteus measure --out runs/demo          # structural + behavioural distance, per arm

`--arm` is `neutral`, or `review:<surface>` / `record:<surface>` (repeatable). `--goal` is
`none` (no-goal) or `task:<text>` (a stated objective). The default harness is `minimal`,
which runs offline; `aki` plugs in the reference research harness.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from proteus.core.disposition import NEUTRAL, record, review
from proteus.core.goal import Goal, GoalConfig
from proteus.sweep import SweepConfig, run_sweep


def _repository_root(start: Path | None = None) -> Path:
    """Repository root whose explicit ``.env`` is trusted by safety preflight."""
    root = Path(start or Path(__file__).resolve().parents[1]).resolve()
    marker = root / ".git"
    if not marker.is_file():
        return root
    try:
        label, separator, raw_git_dir = marker.read_text(encoding="utf-8").strip().partition(":")
        if label != "gitdir" or not separator:
            return root
        git_dir = Path(raw_git_dir.strip())
        if not git_dir.is_absolute():
            git_dir = marker.parent / git_dir
        common = git_dir / "commondir"
        if not common.is_file():
            return root
        common_dir = (git_dir / common.read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        return root
    return common_dir.parent if common_dir.name == ".git" else root


@dataclass(frozen=True)
class _SelectedSafetySuite:
    name: str
    version: str
    families: tuple[object, ...]

    def definitions(self):
        return self.families


def _candidate_gate_factory(args, *, adapter_factory, controller_root: Path):
    """Preflight an optional online gate before the sweep root can be created."""
    if not args.safety_suite:
        if args.safety_family:
            raise ValueError("--safety-family requires --safety-suite")
        return None

    from proteus.safety.gate import GateRunner
    from proteus.safety.harness_loading import (
        load_harness_safety_suite,
        preflight_harness_safety_suite,
        suite_requires_fixed_live,
        validate_harness_safety_suite,
    )
    from proteus.safety.live import LiveModelBroker, LiveModelConfig
    from proteus.safety.plugins import CandidateSafetyAdapter, CandidateSafetyExecutor

    for label, value in (
        ("seeds", args.seeds),
        ("episodes", args.episodes),
        ("max turns", args.max_turns),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"safety-gated run {label} must be a positive integer")

    suite = load_harness_safety_suite(args.safety_suite)
    definitions = validate_harness_safety_suite(suite)
    selected_ids = tuple(args.safety_family or ())
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("duplicate safety family selection")
    declared = {item.family_id: item for item in definitions}
    unknown = [family_id for family_id in selected_ids if family_id not in declared]
    if unknown:
        raise ValueError(f"unknown safety family: {', '.join(unknown)}")
    selected = (
        tuple(declared[family_id] for family_id in selected_ids)
        if selected_ids
        else definitions
    )
    configured_suite = _SelectedSafetySuite(suite.name, suite.version, selected)

    adapter = adapter_factory()
    if not isinstance(adapter, CandidateSafetyAdapter):
        raise TypeError(f"adapter {adapter.name!r} does not implement candidate safety")
    executor = adapter.candidate_safety_executor()
    if not isinstance(executor, CandidateSafetyExecutor):
        raise TypeError(f"adapter {adapter.name!r} returned an invalid candidate safety executor")
    profile = adapter.harness_safety_profile()
    profile.validate_surfaces(adapter.surfaces())
    # The predeclared primary module must exist. A missing supporting module is part of the
    # adapter's exposure result (and therefore a fail-closed gate fact), not a reason to skip
    # publishing candidate evidence altogether.
    required_modules = {definition.primary_module for definition in selected}
    missing_modules = sorted(
        module.value for module in required_modules if profile.binding_for(module) is None
    )
    if missing_modules:
        raise ValueError(
            f"adapter {adapter.name!r} does not bind required safety modules: "
            f"{', '.join(missing_modules)}"
        )

    model_config = None
    broker = None
    if suite_requires_fixed_live(selected):
        if not isinstance(args.model, str) or not args.model.strip():
            raise ValueError("fixed-live safety evidence requires an explicit --model")
        model_config = LiveModelConfig(model=args.model)
        preflight_harness_safety_suite(
            configured_suite,
            model_config=model_config,
            repository_root=_repository_root(),
        )
        broker = LiveModelBroker.from_repository(model_config, _repository_root())
    else:
        preflight_harness_safety_suite(
            configured_suite,
            model_config=None,
            repository_root=_repository_root(),
        )

    def factory(_run_id: str):
        return GateRunner(
            adapter=adapter_factory(),
            suite=configured_suite,
            controller_root=controller_root,
            model_config=model_config,
            broker=broker,
        )

    return factory


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


def _arm(spec: str):
    if spec == "neutral":
        return NEUTRAL
    kind, _, surface = spec.partition(":")
    if kind == "review" and surface:
        return review(surface)
    if kind == "record" and surface:
        return record(surface)
    raise SystemExit(f"bad --arm {spec!r} (use neutral | review:<surface> | record:<surface>)")


def _goal(spec: str) -> GoalConfig:
    if spec == "none":
        return GoalConfig.no_goal()
    kind, _, text = spec.partition(":")
    if kind == "task":
        return GoalConfig.single(Goal(name="task", text=text))
    raise SystemExit(f"bad --goal {spec!r} (use none | task:<text>)")


def cmd_run(args) -> int:
    try:
        adapter_factory = _adapter_factory(args.harness)
        root = Path(args.out).expanduser()
        candidate_gate_factory = _candidate_gate_factory(
            args,
            adapter_factory=adapter_factory,
            controller_root=root,
        )
        cfg = SweepConfig(
            name=args.out,
            adapter_factory=adapter_factory,
            arms=[_arm(a) for a in args.arm],
            seeds=args.seeds,
            goal=_goal(args.goal),
            root=root,
            model=args.model,
            episodes=args.episodes,
            max_turns=args.max_turns,
            candidate_gate_factory=candidate_gate_factory,
        )
        records = run_sweep(cfg)
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 2
    done = sum(r["episodes_complete"] for r in records)
    print(f"ran {len(records)} seeds, {done} episodes -> {args.out}")
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
    records = [json.loads(l) for l in (root / "seeds.jsonl").read_text().splitlines()]

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


def cmd_audit(args) -> int:
    """Run an independent post-run audit over a completed sweep."""
    from proteus.safety.loading import load_suite
    from proteus.safety.runner import run_audit

    try:
        adapter = _adapter_factory(args.harness)()
    except SystemExit as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    try:
        suite = load_suite(args.suite)
        result = run_audit(
            Path(args.out).expanduser(),
            adapter,
            suite,
            audit_id=args.audit_id,
        )
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    print(f"audit results: {result.total_results} -> {result.audit_root}")
    return 0


def cmd_check(args) -> int:
    from proteus.testing import check_adapter
    adapter = _adapter_factory(args.harness)()
    return len(check_adapter(adapter, episode=args.episode, model=args.model))


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
    r.add_argument("--goal", default="none", help="none | task:<text>")
    r.add_argument("--seeds", type=int, default=4)
    r.add_argument("--episodes", type=int, default=10)
    r.add_argument("--max-turns", type=int, default=100)
    r.add_argument("--model", default="",
                   help="model name; empty uses the adapter's default")
    r.add_argument("--safety-suite", default="",
                   help="online candidate-safety suite as <module>:<object>")
    r.add_argument("--safety-family", action="append", default=None,
                   help="family ID to run (repeatable; default: every declared family)")
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("measure", help="measure a finished sweep")
    m.add_argument("--harness", default="minimal")
    m.add_argument("--out", required=True)
    m.add_argument("--travel", action="store_true",
                   help="also compute per-surface path length over episode snapshots")
    m.set_defaults(func=cmd_measure)

    a = sub.add_parser(
        "audit",
        help="audit a completed evolution sweep without changing it",
        description="Audit a completed evolution sweep without changing it.",
    )
    a.add_argument("--harness", default="minimal")
    a.add_argument("--out", required=True)
    a.add_argument("--suite", default="proteus.safety.integrity:SUITE",
                   help="audit suite as <module>:<object>")
    a.add_argument("--audit-id", default="",
                   help="output id under <sweep>/audits (default: suite name)")
    a.set_defaults(func=cmd_audit)

    c = sub.add_parser("check", help="compliance-check a HarnessAdapter implementation")
    c.add_argument("--harness", required=True, help="built-in name or <module>:<Class>")
    c.add_argument("--episode", action="store_true",
                   help="also run one neutral episode (may launch containers / cost money)")
    c.add_argument("--model", default="",
                   help="explicit model for the optional live episode")
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
