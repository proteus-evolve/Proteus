"""Explicit configuration drafts translated to the existing CLI, never a new runner."""
from __future__ import annotations

import re
import shlex
import sys

from proteus.core.budget import PHASES, make_budget_plan, validate_phases
from proteus.core.continuity import _redact
from proteus.core.goal import EvaluatorSpec, GoalConfig, Visibility

BUILTINS = ("minimal", "llm", "dsh", "pi", "codex", "aki")
DEFAULT = {
    "harness": "minimal", "model": "", "goal": "", "episodes": 10, "seeds": 1,
    "arms": ["neutral"], "normal": 300, "hard": 500, "checkpoint": 0,
    "phase_names": list(PHASES),
    "phase_turns": {"observe": 40, "propose": 25, "act": 200, "reflect": 35},
    "phase_prompts": {}, "announce_budget": True, "selection": "none",
    "evaluators": [], "env": "", "network": "", "mem": "", "cpus": "",
    "phase_timeout": 0,
}


def _integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 1000000:
        raise ValueError(f"{name} must be an integer between {minimum} and 1000000")
    return value


def _text(value, name, maximum=32000):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} must be text of at most {maximum} characters")
    if _redact(value) != value:
        raise ValueError(f"{name} contains a credential-like value; use server-side environment variables")
    return value


def validate(config, *, allowed_adapters=()):
    """Validate shape and core protocols without importing adapters or downloading data.

    This is configuration validation, not environment/provider preflight. Custom adapters
    are operator-allowlisted. Arbitrary evaluator Python imports are not accepted here;
    existing Python API runs can still be opened and measured without such restrictions.
    """
    if not isinstance(config, dict) or set(config) - set(DEFAULT):
        raise ValueError("configuration contains unknown fields")
    c = {**DEFAULT, **config}
    for key in ("harness", "model", "goal", "env", "network", "mem", "cpus", "selection"):
        c[key] = _text(c[key], key)
    if c["harness"] not in (*BUILTINS, *allowed_adapters):
        raise ValueError("custom harness requires the server's explicit --allow-adapter allowlist")
    if c["network"] not in ("", "none", "bridge", "host"):
        raise ValueError("network must be default, none, bridge, or host")
    if c["selection"] not in ("none", "accept_reject"):
        raise ValueError("invalid selection policy")
    for key in ("episodes", "seeds", "normal", "hard", "checkpoint", "phase_timeout"):
        c[key] = _integer(c[key], key, 1 if key in ("episodes", "seeds") else 0)
    if c["episodes"] > 10000 or c["seeds"] > 100:
        raise ValueError("viewer launch limit is 10000 episodes and 100 seeds")
    if type(c["announce_budget"]) is not bool:
        raise ValueError("announce_budget must be boolean")
    c["phase_names"] = list(validate_phases(c["phase_names"]))
    if not isinstance(c["phase_turns"], dict) or not isinstance(c["phase_prompts"], dict):
        raise ValueError("phase_turns and phase_prompts must be objects")
    make_budget_plan(max_turns=c["normal"], hard_max_turns=c["hard"],
                     phase_turns=c["phase_turns"], checkpoint_turns=c["checkpoint"],
                     phases=tuple(c["phase_names"]))
    if c["checkpoint"] and not c["announce_budget"]:
        raise ValueError("checkpoint reserve requires announce_budget")
    if c["checkpoint"] and c["harness"] in ("minimal", "llm"):
        raise ValueError("checkpoint reserve requires a harness with native or framework continuity")
    if set(c["phase_prompts"]) - set(c["phase_names"]):
        raise ValueError("prompt names must belong to the configured phases")
    for name in c["phase_names"]:
        if name not in PHASES and not c["phase_prompts"].get(name):
            raise ValueError(f"custom phase {name} requires a prompt")
    for name, prompt in c["phase_prompts"].items():
        _text(prompt, f"phase prompt {name}")
    if (not isinstance(c["arms"], list) or not 1 <= len(c["arms"]) <= 30
            or any(not isinstance(a, str) or not re.fullmatch(
                r"neutral|(?:review|record):[a-zA-Z0-9_.-]+", a) for a in c["arms"])
            or len(set(c["arms"])) != len(c["arms"])):
        raise ValueError("arms must be unique neutral / review:SURFACE / record:SURFACE values")
    if not isinstance(c["evaluators"], list) or len(c["evaluators"]) > 50:
        raise ValueError("evaluators must be a list of at most 50 instruments")
    specs, normalized, tasks = [], [], 0
    for row in c["evaluators"]:
        if not isinstance(row, dict) or set(row) - {
            "spec", "visibility", "selection_eligible", "every", "at", "initial", "error_policy"
        }:
            raise ValueError("invalid evaluator fields")
        body = _text(row.get("spec", ""), "evaluator spec", 4000)
        kind, _, arg = body.partition(":")
        if "@" in body or not body or kind not in (
            "tool-calls", "step", "units", "contains", "local", "polyglot", "mbpp", "humaneval"
        ) or (kind not in ("tool-calls", "step") and not arg):
            raise ValueError("use a CLI evaluator spec and a separate visibility field")
        if kind in ("tool-calls", "step") and arg:
            raise ValueError("tool-calls and step take no arguments")
        if kind == "contains":
            path, separator, needle = arg.partition(":")
            if not path or not separator or not needle or path.startswith("/") or ".." in path.split("/"):
                raise ValueError("contains requires a relative file path and a nonempty needle")
            name = f"contains:{path}"
        else:
            name = "structural-step" if kind == "step" else body
        benchmark = kind in ("local", "polyglot", "mbpp", "humaneval")
        tasks += benchmark
        visibility = row.get("visibility", "observe")
        try:
            visibility = Visibility(visibility)
        except ValueError:
            raise ValueError("visibility must be observe or hidden") from None
        spec = EvaluatorSpec(name=name, run=lambda trace, context: None,
            kind="benchmark" if benchmark else "custom" if kind == "contains" else "measurement",
            visibility=visibility, selection_eligible=row.get("selection_eligible", True),
            every_n_episodes=row.get("every", 1),
            episodes=tuple(row["at"]) if isinstance(row.get("at"), list) else None,
            include_initial=row.get("initial", False), error_policy=row.get("error_policy", "missing"))
        if row.get("at") is not None and not isinstance(row["at"], list):
            raise ValueError("explicit evaluator episodes must be a list")
        if row.get("at") == []:
            raise ValueError("explicit evaluator episodes must not be empty")
        if any(s.name == name for s in specs):
            raise ValueError("evaluator identities must be unique")
        specs.append(spec)
        normalized.append({"spec": body, "name": name, "visibility": visibility.value,
                           "selection_eligible": spec.selection_eligible, "every": spec.every_n_episodes,
                           "at": list(spec.episodes) if spec.episodes is not None else None,
                           "initial": spec.include_initial, "error_policy": spec.error_policy})
    if tasks > 1:
        raise ValueError("the CLI supports one benchmark task workspace per run")
    GoalConfig.of(text=c["goal"], evaluators=specs, selection=c["selection"])
    c["evaluators"] = normalized
    return c


def arguments(c, destination):
    """No shell interpolation; new runs always refuse an existing destination."""
    argv = [sys.executable, "-m", "proteus.cli", "run", "--out", str(destination),
            "--on-existing", "refuse", "--harness", c["harness"], "--model", c["model"],
            "--goal", c["goal"] or "none", "--episodes", str(c["episodes"]),
            "--seeds", str(c["seeds"]), "--max-turns", str(c["normal"]),
            "--selection", c["selection"], "--phases", ",".join(c["phase_names"])]
    for arm in c["arms"]:
        argv += ["--arm", arm]
    if c["phase_turns"]:
        argv += ["--phase-turns", ",".join(f"{k}={v}" for k, v in c["phase_turns"].items()),
                 "--hard-max-turns", str(c["hard"]), "--checkpoint-turns", str(c["checkpoint"])]
    if c["announce_budget"]:
        argv += ["--announce-budget"]
    for name, prompt in c["phase_prompts"].items():
        argv += ["--phase-prompt", f"{name}={prompt}"]
    for key in ("env", "network", "mem", "cpus", "phase_timeout"):
        if c[key]:
            argv += ["--" + key.replace("_", "-"), str(c[key])]
    for row in c["evaluators"]:
        name = row["name"]
        argv += ["--evaluator", f"{row['spec']}@{row['visibility']}"]
        if row["at"] is not None:
            argv += ["--eval-at", f"{name}=" + ",".join(map(str, row["at"]))]
        else:
            argv += ["--eval-every", f"{name}={row['every']}"]
            if row["initial"]:
                argv += ["--eval-initial", name]
        if not row["selection_eligible"]:
            argv += ["--selection-exclude", name]
        if row["error_policy"] == "missing":
            argv += ["--eval-missing-on-error", name]
    return argv


def preview(c):
    return shlex.join(arguments(c, "NEW_SWEEP_DIRECTORY"))
