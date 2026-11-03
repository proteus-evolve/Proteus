"""Researcher entry points for checking, locking and running registered recipes."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from proteus.core.contracts import OperationStatus, require
from proteus.core.serialization import ContractError, content_hash, read_json
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import load_recipe
from proteus.recipes.manifest import DependencyLock, DependencyPin, SourcePin
from proteus.recipes.runner import run_recipe


def installed_packages():
    packages = {}
    for dist in importlib.metadata.distributions():
        name = re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()
        pin = DependencyPin(name=name, version=dist.version)
        require(name not in packages or packages[name] == pin,
                f"conflicting installed distributions for {name}")
        packages[name] = pin
    return tuple(packages[name] for name in sorted(packages))


def source_pin(name, path):
    root = Path(path).resolve()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)

    require(Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip()).resolve() == root,
            "source path must be a repository root")
    revision = git("rev-parse", "HEAD").decode().strip()
    repository = git("remote", "get-url", "origin").decode().strip()
    from urllib.parse import urlsplit
    require(not (repository.startswith(("http:", "https:")) and urlsplit(repository).username),
            "source remote must not embed credentials")
    changes = git("diff", "HEAD", "--binary")
    untracked = {}
    for raw in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if raw:
            relative = os.fsdecode(raw)
            file = root / relative
            data = os.fsencode(os.readlink(file)) if file.is_symlink() else file.read_bytes()
            untracked[relative] = hashlib.sha256(data).hexdigest()
    dirty = content_hash({"diff": hashlib.sha256(changes).hexdigest(), "untracked": untracked}) \
        if changes or untracked else None
    return SourcePin(name=name, repository=repository, revision=revision, dirty_patch_hash=dirty)


def command(args):
    try:
        return _command(args)
    except (ContractError, OSError, ImportError, subprocess.CalledProcessError) as exc:
        print(f"recipe error: {exc}", file=sys.stderr)
        return 2


def _command(args):
    if args.recipe_cmd == 'summarize':
        from proteus.recipes.study import summarize_study
        print(summarize_study(args.study, args.out, include_private=args.include_private))
        return 0
    if args.recipe_cmd == 'export':
        from proteus.recipes.export import export_run
        from proteus.measure.artifacts import MeasurementSpec
        measurement_spec = MeasurementSpec.from_json(Path(args.measurements).read_text()) if args.measurements else None
        print(export_run(args.run, args.out, include_private=args.include_private, measurement_spec=measurement_spec))
        return 0
    if args.recipe_cmd == 'catalog':
        from proteus.recipes.catalog import describe
        print(json.dumps(describe(args.name, family=args.family, kind=args.kind), indent=2))
        return 0
    if args.recipe_cmd == 'example':
        from proteus.recipes.catalog import prepare_example
        print(json.dumps(prepare_example(args.name, args.out), indent=2))
        return 0
    if args.recipe_cmd == 'scaffold':
        from proteus.recipes.scaffolding import scaffold
        print(scaffold(args.kind, args.name, args.out))
        return 0
    registry = builtin_registry()
    for entry in args.plugin:
        registry.load(entry)
    if args.recipe_cmd == "list":
        print(json.dumps(registry.describe(), indent=2))
        return 0
    recipe = registry.resolve(load_recipe(args.config))
    if args.recipe_cmd == "check":
        print(recipe.to_json())
        return 0
    if args.recipe_cmd == "lock":
        sources = []
        for value in args.source:
            name, separator, path = value.partition("=")
            require(bool(name and separator and path), "--source requires NAME=REPOSITORY_ROOT")
            sources.append(source_pin(name, path))
        lock = registry.make_lock(recipe, sources, packages=installed_packages())
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(lock.to_json() + "\n")
        print(path)
        return 0
    lock = DependencyLock.from_json(Path(args.lock).read_text(encoding="utf-8"))
    if args.recipe_cmd == "recovery":
        from proteus.core.recovery import recovery_status
        from proteus.recipes.manifest import RunManifest
        registry.check_lock(recipe, lock)
        manifest = RunManifest.from_json((Path(args.out) / "manifest.json").read_text())
        manifest.assert_resume_compatible(recipe, lock)
        print(json.dumps(recovery_status(args.out), indent=2))
        return 0
    prompts = read_json(Path(args.prompts).read_text(encoding="utf-8")) if args.prompts else {}
    result = run_recipe(recipe, registry=registry, lock=lock, root=args.out,
                        run_id=args.run_id, prompts=prompts, resume=args.recipe_cmd == "resume",
                        artifact_source=args.artifact_store)
    print(result.to_json())
    return 0 if result.status == OperationStatus.COMPLETED else 1


def add_parser(subparsers):
    parser = subparsers.add_parser("recipe", help="check and execute versioned 1.0 recipes")
    commands = parser.add_subparsers(dest="recipe_cmd", required=True)
    summarize = commands.add_parser('summarize', help='compare explicit study conditions and aggregate seeds')
    summarize.add_argument('study', help='study JSON with declared conditions, seeds and final evaluations')
    summarize.add_argument('--out', required=True, help='new comparison JSON outside all runs')
    summarize.add_argument('--include-private', action='store_true', help='include final hidden grades in owner-side comparisons')
    summarize.set_defaults(func=command)
    export = commands.add_parser('export', help='export saved recipe evidence and aggregate metrics')
    export.add_argument('run', help='existing settled run directory')
    export.add_argument('--out', required=True, help='new export directory outside the run')
    export.add_argument('--include-private', action='store_true', help='include hidden grades in the offline owner report')
    export.add_argument('--measurements', help='JSON measurement spec for codec-specific artifact and lineage changes')
    export.set_defaults(func=command)
    catalog = commands.add_parser('catalog', help='discover offline examples and benchmark preparation entry points')
    catalog.add_argument('name', nargs='?')
    catalog.add_argument('--family', choices=('framework', 'gepa', 'ace', 'meta-harness', 'combee', 'coral'))
    catalog.add_argument('--kind', choices=('example', 'benchmark'))
    catalog.set_defaults(func=command)
    example = commands.add_parser('example', help='prepare a bundled offline mechanism example')
    example.add_argument('name')
    example.add_argument('--out', required=True)
    example.set_defaults(func=command)
    create = commands.add_parser('scaffold', help='create an editable offline plugin project')
    create.add_argument('kind', choices=('method', 'stage', 'evaluator', 'subject', 'policy'))
    create.add_argument('--name', required=True, help='Python module and plugin namespace')
    create.add_argument('--out', required=True, help='new project directory')
    create.set_defaults(func=command)
    for name in ("list", "check", "lock", "run", "resume", "recovery"):
        item = commands.add_parser(name)
        item.add_argument("--plugin", action="append", default=[], metavar="MODULE:REGISTER",
                          help="explicitly import a trusted plugin registration hook")
        if name != "list":
            item.add_argument("config", help="recipe JSON or TOML")
        if name == "lock":
            item.add_argument("--source", action="append", required=True, metavar="NAME=ROOT")
            item.add_argument("--out", required=True)
        if name in ("run", "resume", "recovery"):
            item.add_argument("--lock", required=True, help="resolved dependency lock JSON")
            item.add_argument("--out", required=True)
            item.add_argument("--run-id")
            item.add_argument("--prompts", help="JSON mapping from content hash to role prompt text")
            item.add_argument("--artifact-store", help="import the exact initial artifact from this store")
        item.set_defaults(func=command)
