"""Read durable sweep evidence without importing or executing subject adapters.

Only explicit records, normalized action metadata, and immutable Git blobs are read.
Raw model text, tool parameters, native sessions, and evaluator detail are not exported.
Missing, skipped, and errored measurements remain missing, including legacy error zeros.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import threading

from proteus.core.adapter import Surface
from proteus.core.continuity import _redact
from proteus.measure import distance, stream

MAX_JSON = 16 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
SAFE_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,160}$")
SHA = re.compile(r"^[0-9a-f]{40,64}$")


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def confined(base: Path, *parts: str) -> Path:
    """All run-supplied paths must remain under the operator's explicit workspace."""
    root = base.resolve()
    candidate = base.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("record path escapes the workspace")
    return candidate


def read_json(path: Path, default=None):
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ValueError("record exceeds the viewer's 16 MiB limit")
        return json.loads(raw)
    except FileNotFoundError:
        return default


def read_lines(path: Path) -> list[dict]:
    """Ignore only an incomplete final append, never silently discard interior damage."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_JSON + 1)
    except FileNotFoundError:
        return []
    if len(raw) > MAX_JSON:
        raise ValueError("record exceeds the viewer's 16 MiB limit")
    output = []
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not raw.endswith(b"\n"):
                break
            raise ValueError("invalid complete JSONL record") from None
        if not isinstance(row, dict):
            raise ValueError("JSONL rows must be objects")
        output.append(row)
    return output


def clean(value):
    """Redact recognized credential forms; this is not a promise of public anonymity."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, dict):
        return {str(clean(k)): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if finite(value) or value is None or isinstance(value, bool):
        return value
    return None


def _git(repo: Path, *args: str, limit=MAX_JSON) -> bytes:
    # No work-tree mutation, checkout, adapter loading, hooks, or arbitrary git arguments.
    process = subprocess.Popen(
        ["git", "--git-dir", str(repo), *args], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    result = []
    errors = []

    def read():
        try:
            result.append(process.stdout.read(limit + 1))
        except OSError as exc:
            errors.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert process.stdout is not None
        reader.join(timeout=20)
        if reader.is_alive():
            raise ValueError("snapshot read timed out")
        if errors:
            raise ValueError("snapshot evidence could not be read")
        data = result[0]
        if len(data) > limit:
            raise ValueError("snapshot output exceeds viewer limit")
        if process.wait(timeout=20) != 0:
            raise ValueError("snapshot evidence could not be read")
        return data
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=1)


def snapshot_index(repo: Path) -> dict[int, str]:
    if not repo.is_dir():
        return {}
    result = {}
    for line in _git(repo, "log", "--format=%H %s").decode("utf8", "replace").splitlines():
        match = re.match(r"^([0-9a-f]{40,64}) episode (\d+):", line)
        if match:
            result.setdefault(int(match[2]), match[1])
    return result


@lru_cache(maxsize=32)
def snapshot_units(repo_name: str, sha: str, declarations: str):
    """Use the public measurement implementation over safe regular Git archive members.

    Never extract symlinks/gitlinks or trust archive paths. Do not execute subject code.
    Caching is by immutable snapshot identity and declared measurement schema.
    """
    if not SHA.fullmatch(sha):
        raise ValueError("invalid snapshot identity")
    surfaces = [Surface(**row) for row in json.loads(declarations)]
    raw = _git(Path(repo_name), "archive", sha, limit=MAX_ARCHIVE)
    with tempfile.TemporaryDirectory(prefix="proteus-viz-measure-") as temporary:
        base = Path(temporary)
        members = 0
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if not member.isfile() or name.is_absolute() or ".." in name.parts:
                    continue
                # Extract only declared editable surfaces; metadata and native logs stay out.
                if not any(name == PurePosixPath(s.subdir)
                           or PurePosixPath(s.subdir) in name.parents for s in surfaces):
                    continue
                if member.size > MAX_JSON:
                    raise ValueError("surface file exceeds viewer measurement size limit")
                members += 1
                if members > 10000:
                    raise ValueError("snapshot has more than 10,000 surface files")
                source = archive.extractfile(member)
                if source is None:
                    continue
                target = confined(base, *name.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())
        return distance.units(base, surfaces)


def _delta(before, after):
    deltas = [distance.delta(before[name], after[name], name) for name in before]
    union = sum(item.union for item in deltas)
    moved = sum(item.added + item.dropped + item.revised for item in deltas)
    return {
        "distance": moved / union if union else 0.0,
        "added": sum(item.added for item in deltas),
        "dropped": sum(item.dropped for item in deltas),
        "revised": sum(item.revised for item in deltas),
        "surfaces": {item.surface: {
            "added": item.added, "dropped": item.dropped, "revised": item.revised,
            "distance": item.distance,
        } for item in deltas},
    }


def _changes(repo: Path, before: str, after: str):
    if not before or not after:
        return []
    if not SHA.fullmatch(before) or not SHA.fullmatch(after):
        raise ValueError("invalid snapshot identity")
    rows = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z",
                "--no-renames", before, after, "--").decode("utf8", "replace").split("\0")
    output = []
    for row in rows:
        if not row:
            continue
        added, dropped, name = row.split("\t", 2)
        output.append({"path": name, "added": int(added) if added.isdigit() else None,
                       "dropped": int(dropped) if dropped.isdigit() else None})
    return output


def _normalized_actions(root, run_root, ep, progress):
    recorded = progress.get("action_metadata")
    if isinstance(recorded, list):
        rows = recorded
    else:
        # Legacy stdlib/minimal/LLM normalized traces. Native adapter mapping files are
        # not normalized streams; do not import their adapters or read reasoning logs.
        rows = read_lines(confined(root, str(run_root.relative_to(root)), "traces",
                                   f"ep{ep:03d}.jsonl"))
        if not rows:
            return None
    output = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("phase"), str):
            raise ValueError("invalid normalized action metadata")
        output.append({"turn": row.get("turn") if finite(row.get("turn")) else None,
                       "phase": row["phase"],
                       "tool": row.get("tool") if isinstance(row.get("tool"), str) else None,
                       "surface": row.get("surface")
                       if isinstance(row.get("surface"), str) else None})
    return output


def _schema(manifest):
    declarations = manifest.get("condition", {}).get("adapter", {}).get("surfaces", [])
    if not declarations:
        return None
    output = []
    for row in declarations:
        if not isinstance(row, dict) or not isinstance(row.get("subdir"), str):
            raise ValueError("invalid editable surface declaration")
        name, subdir, unit = row["name"], row["subdir"], row.get("unit", "file")
        path = PurePosixPath(subdir)
        if (not isinstance(name, str) or path.is_absolute() or ".." in path.parts
                or subdir == "" or unit not in ("file", "directory", "top_level_def")):
            raise ValueError("invalid editable surface declaration")
        output.append({"name": name, "subdir": subdir, "unit": unit})
    return json.dumps(output, sort_keys=True)


def _scores(row, specs):
    results = row.get("results", [])
    values = {result["name"]: result for result in results
              if isinstance(result, dict) and isinstance(result.get("name"), str)}
    output = {}
    for spec in specs:
        name = spec["name"]
        result = values.get(name)
        if result is None and name in row.get("scores", {}):
            result = {"score": row["scores"][name],
                      "status": row.get("evaluation_status", {}).get(name, "ok")}
        status = result.get("status", "ok") if result else "missing"
        value = result.get("score") if result else None
        output[name] = {"score": value if status == "ok" and finite(value) else None,
                        "status": status, "visibility": spec.get("visibility", "hidden"),
                        "kind": spec.get("kind", "custom"),
                        "selection_eligible": spec.get("selection_eligible", True)}
    return output


def _metric(name, value, unit, source, reason="", **extra):
    return {"id": name, "value": value, "unit": unit, "source": source,
            "status": "ok" if finite(value) else "missing", "reason": reason, **extra}


class Workspace:
    """One sweep or a directory of sweeps, selected explicitly by the operator."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def catalog(self):
        entries, errors = [], []
        roots = [self.root] if (self.root / "manifest.json").is_file() else []
        if not roots and self.root.is_dir():
            for item in sorted(self.root.iterdir()):
                if item.is_dir() and (item / "manifest.json").is_file():
                    try:
                        roots.append(confined(self.root, item.name))
                    except ValueError as exc:
                        errors.append({"sweep": item.name, "error": str(exc)})
        for sweep in roots:
            try:
                manifest = read_json(confined(self.root, str(sweep.relative_to(self.root)),
                                               "manifest.json"))
                if not isinstance(manifest, dict):
                    raise ValueError("manifest must be an object")
                condition = manifest.get("condition", {})
                if not isinstance(condition, dict) or not isinstance(condition.get("adapter", {}), dict):
                    raise ValueError("invalid adapter condition")
                target = manifest.get("episodes")
                if not isinstance(target, int) or isinstance(target, bool) or not 0 <= target <= 10000:
                    raise ValueError("invalid episode target")
                records = manifest.get("runs", [])
                if not isinstance(records, list) or len(records) > 10000:
                    raise ValueError("invalid run catalog")
                for row in records:
                    if not isinstance(row, dict):
                        raise ValueError("invalid run catalog row")
                    rid = row.get("id", "")
                    if not isinstance(rid, str) or not SAFE_ID.fullmatch(rid) or rid in (".", ".."):
                        raise ValueError("invalid run identity")
                    confined(self.root, str(sweep.relative_to(self.root)), "runs", rid)
                    public_id = hashlib.sha256(
                        f"{sweep.relative_to(self.root)}/{rid}".encode()).hexdigest()[:24]
                    entries.append({"id": public_id, "name": f"{sweep.name} · {row.get('arm', '')}"
                                    f" · seed {row.get('seed', '')}", "sweep": sweep,
                                    "record": row, "manifest": manifest})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({"sweep": sweep.name, "error": str(exc)})
        return entries, clean(errors)

    def summary(self):
        entries, errors = self.catalog()
        return clean({"runs": [{"id": item["id"], "name": item["name"],
                                 "harness": item["manifest"].get("condition", {}).get(
                                     "adapter", {}).get("name", "unknown"),
                                 "episodes_target": item["manifest"]["episodes"]}
                                for item in entries], "errors": errors})

    def load(self, identity):
        entries, _ = self.catalog()
        item = next((row for row in entries if row["id"] == identity), None)
        if item is None:
            raise KeyError("unknown run")
        sweep, manifest, record = item["sweep"], item["manifest"], item["record"]
        relative = str(sweep.relative_to(self.root))
        run_root = confined(self.root, relative, "runs", record["id"])
        private = confined(self.root, relative, "runs", ".proteus-records", record["id"])
        history = read_json(confined(private, "eval_history.json"), [])
        if not isinstance(history, list):
            raise ValueError("evaluation history must be a list")
        progress = read_lines(confined(self.root, relative, "progress", record["id"] + ".jsonl"))
        rows = {row["episode"]: row for row in progress if isinstance(row.get("episode"), int)}
        histories = {row["episode"]: row for row in history
                     if isinstance(row, dict) and isinstance(row.get("episode"), int)}
        specs = manifest.get("evaluators", [])
        if not isinstance(specs, list) or any(not isinstance(s, dict)
                or not isinstance(s.get("name"), str) for s in specs):
            raise ValueError("invalid evaluator declarations")
        repo = confined(self.root, str(run_root.relative_to(self.root)), ".snapshot.git")
        snapshots = snapshot_index(repo)
        schema = _schema(manifest)
        condition = manifest.get("condition", {})
        phases = condition.get("phases", ["observe", "propose", "act", "reflect"])
        from proteus.core.budget import validate_phases
        phases = list(validate_phases(phases))
        primary = specs[0]["name"] if specs else None
        units_cache = {}
        warnings = []

        def measured(sha):
            if not sha or not schema:
                return None
            if sha not in units_cache:
                try:
                    units_cache[sha] = snapshot_units(str(repo), sha, schema)
                except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
                    warnings.append(f"Structural measurement unavailable: {exc}")
                    units_cache[sha] = None
            return units_cache[sha]

        baseline = measured(snapshots.get(0))
        previous = baseline
        previous_sha = snapshots.get(0, "")
        initial = read_json(confined(private, "initial_evaluation.json"), {}) or {}
        initial_values = _scores(initial, specs)
        episodes = [{"index": 0, "title": "The initial harness.",
                     "summary": "Seeded snapshot before evolution; no evolution actions yet.",
                     "accepted": True, "gate": "Initial snapshot", "sha": previous_sha,
                     "candidateSha": None, "evaluations": initial_values,
                     "score": initial_values.get(primary, {}).get("score"),
                     "selfCheck": None, "distance": 0.0 if baseline is not None else None,
                     "step": 0.0 if baseline is not None else None,
                     "pathLength": 0.0 if baseline is not None else None,
                     "behavior": None, "order": None, "procedure": None,
                     "calls": 0, "cost": None, "phaseCalls": [0 for _ in phases],
                     "phaseToolCalls": [0 for _ in phases], "files": [], "changes": 0,
                     "added": 0, "dropped": 0, "revised": 0, "surface": None,
                     "actions": [], "phases": {}, "duration": None,
                     "provenance": ["manifest.json", "initial_evaluation.json", "snapshot git"]}]
        cumulative_calls = 0
        cumulative_cost = 0
        travel = 0.0
        totals = Counter()
        reference = None
        reference_episode = None
        completion = 0
        for ep in range(1, manifest["episodes"] + 1):
            if ep not in snapshots:
                break  # A trace alone does not prove a completed episode boundary.
            completion = ep
            row = rows.get(ep, {})
            hist = histories.get(ep, {})
            counters = hist.get("counters", row.get("counters", {})) or {}
            recorded_cost = counters.get("cost_usd")
            recorded_cost = recorded_cost if finite(recorded_cost) and recorded_cost >= 0 else None
            cumulative_cost = (cumulative_cost + recorded_cost
                               if cumulative_cost is not None and recorded_cost is not None else None)
            actions = _normalized_actions(self.root, run_root, ep, row)
            action_stream = [a["tool"] or stream.TEXT_TOKEN for a in actions] if actions else None
            if action_stream and reference is None:
                reference, reference_episode = action_stream, ep
            calls = row.get("tool_calls")
            if not finite(calls):
                calls = sum(bool(a["tool"]) for a in actions) if actions is not None else None
            cumulative_calls = (cumulative_calls + calls
                                if cumulative_calls is not None and calls is not None else None)
            endpoint = measured(snapshots[ep])
            candidate = hist.get("candidate_commit") or snapshots[ep]
            candidate_units = measured(candidate)
            change = _delta(previous, candidate_units) if previous is not None and candidate_units is not None else None
            movement = _delta(previous, endpoint) if previous is not None and endpoint is not None else None
            displacement = _delta(baseline, endpoint) if baseline is not None and endpoint is not None else None
            if movement is not None and travel is not None:
                travel += movement["distance"]
            else:
                travel = None
            if change:
                totals.update({name: change[name] for name in ("added", "dropped", "revised")})
            try:
                files = _changes(repo, previous_sha, candidate)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                files = []
                warnings.append(f"Candidate diff unavailable: {exc}")
            surface = None
            if change and change["surfaces"]:
                surface = max(change["surfaces"], key=lambda name: sum(
                    change["surfaces"][name][k] for k in ("added", "dropped", "revised")))
                if not any(change["surfaces"][surface][k] for k in ("added", "dropped", "revised")):
                    surface = None
            accepted = hist.get("accepted", row.get("accepted"))
            failure = hist.get("failure_kind", "")
            gate = ("Failed viability gate" if failure == "viability" else
                    "Rejected: evaluation unavailable" if hist.get("selection_unavailable") else
                    "Accepted" if accepted is True else
                    "Rejected by selection" if accepted is False else "Not recorded")
            values = _scores(hist or row, specs)
            phase_turns = [counters.get(f"phase_{phase}_turns") for phase in phases]
            phase_turns = [v if finite(v) else None for v in phase_turns]
            phase_tools = ([sum(bool(a["tool"]) and a["phase"] == phase for a in actions)
                            for phase in phases] if actions is not None else [None for _ in phases])
            phase_info = {}
            for phase in phases:
                counts = Counter(a["tool"] or stream.TEXT_TOKEN for a in actions or []
                                 if a["phase"] == phase)
                phase_info[phase] = {"summary": "Recorded normalized tool metadata only; model reasoning is not displayed.",
                                     "actions": [f"{tool}: {count} action(s)" for tool, count in counts.items()]}
            summary = f"{gate}; {len(files)} changed file(s)"
            if calls is not None:
                summary += f"; {calls} normalized tool action(s)."
            if surface:
                summary += f" Most structural edits targeted {surface}."
            episodes.append({
                "index": ep, "title": f"Episode {ep} · {gate}", "summary": summary,
                "accepted": accepted, "gate": gate, "sha": snapshots[ep],
                "candidateSha": candidate, "score": values.get(primary, {}).get("score"),
                "selfCheck": None, "evaluations": values,
                "distance": displacement["distance"] if displacement else None,
                "step": movement["distance"] if movement else None, "pathLength": travel,
                "behavior": stream.freq_distance(reference, action_stream)
                if reference and action_stream else None,
                "order": stream.order_distance(reference, action_stream)
                if reference and action_stream else None,
                "procedure": stream.ncd(reference, action_stream)
                if reference and action_stream else None,
                "calls": cumulative_calls, "toolActions": calls, "cost": cumulative_cost,
                "costSource": "adapter cost_usd counter, evolution only" if cumulative_cost is not None else None,
                "phaseCalls": phase_turns, "phaseToolCalls": phase_tools,
                "counters": {key: value for key, value in counters.items()
                             if finite(value) or isinstance(value, bool)},
                "files": files, "changes": len(files), "surface": surface,
                "added": change["added"] if change else None,
                "dropped": change["dropped"] if change else None,
                "revised": change["revised"] if change else None,
                "surfaceChanges": change["surfaces"] if change else None,
                "actions": (actions or [])[:200], "actionsAvailable": actions is not None,
                "actionsTruncated": actions is not None and len(actions) > 200,
                "phases": phase_info, "duration": counters.get("duration_s")
                if finite(counters.get("duration_s")) and counters["duration_s"] >= 0 else None,
                "error": hist.get("error") or row.get("error") or None,
                "evaluationUnavailable": hist.get("selection_unavailable", False),
                "provenance": [f"progress/{record['id']}.jsonl", "eval_history.json", "snapshot git"],
            })
            previous, previous_sha = endpoint, snapshots[ep]
        tail = episodes[-1]
        total_moved = totals["added"] + totals["dropped"] + totals["revised"]
        structural_reason = "Requires declared surfaces and readable committed snapshots."
        trace_reason = "Requires normalized action metadata, not native mapping or reasoning logs."
        metrics = [
            _metric("endpoint", tail["distance"], "distance from H₀", "proteus.measure.distance", structural_reason),
            _metric("path", tail["pathLength"], "sum of consecutive valid-snapshot distances", "proteus.measure.distance", structural_reason),
            _metric("churn", (totals["dropped"] + totals["revised"]) / total_moved
                    if total_moved else None, "revision/removal fraction of candidate changes",
                    "proteus.measure.distance", "No measured movement; denominator is zero."),
            _metric("frequency", tail["behavior"], "tool-frequency JS divergence", "proteus.measure.stream.freq_distance", trace_reason),
            _metric("order", tail["order"], "tool-bigram JS divergence", "proteus.measure.stream.order_distance", trace_reason),
            _metric("procedure", tail["procedure"], "normalized compression distance", "proteus.measure.stream.ncd", trace_reason),
            _metric("reliability", None, "within / composition-matched null", "proteus.measure.stream.reliability", "Use Measure matched seeds; requires two comparable seed endpoints.", status="not_computed"),
            _metric("separation", None, "between / within arms", "proteus.measure.stream.between_within", "Use Measure matched seeds; requires reliable within-arm replicates.", status="not_computed"),
            _metric("crystallization", None, "neutral-probe fidelity", "proteus.measure.crystallize", "Requires explicitly recorded neutral probe episodes. Viewing never launches probes."),
        ]
        terminal = {}
        for seed in read_lines(confined(self.root, relative, "seeds.jsonl")):
            if seed.get("arm") == record.get("arm") and seed.get("seed") == record.get("seed"):
                terminal = seed
        status = "completed" if completion >= manifest["episodes"] else "incomplete"
        if terminal.get("error"):
            status = "failed"
        adapter = condition.get("adapter", {})
        budget = manifest.get("budget", {})
        payload = {
            "schema": "proteus-visualize-run-v1", "synthetic": False,
            "id": identity, "name": item["name"], "harness": adapter.get("name", "unknown"),
            "model": manifest.get("model", ""), "goal": manifest.get("goal", ""),
            "episodesTarget": manifest["episodes"], "episodesComplete": completion,
            "status": status, "liveProcessConfirmed": False,
            "statusNote": "Progress is durable evidence, not proof that a controller is currently running.",
            "error": terminal.get("error"), "phases": phases, "episodes": episodes,
            "evaluators": specs, "surfaces": json.loads(schema) if schema else [],
            "behaviorReferenceEpisode": reference_episode,
            "measurements": metrics, "warnings": list(dict.fromkeys(warnings)),
            "config": {"harness": adapter.get("name", "unknown"), "model": manifest.get("model", ""),
                       "goalMode": "general" if manifest.get("goal") else "none",
                       "goal": manifest.get("goal", ""), "episodes": manifest["episodes"],
                       "seeds": manifest.get("seeds", 1), "arms": manifest.get("arms", []),
                       "normal": condition.get("max_turns", 100),
                       "hard": budget.get("hard_limit", condition.get("max_turns", 100)),
                       "checkpoint": budget.get("checkpoint_turns", 0),
                       "phases": budget.get("phase_turns", {}),
                       "evaluators": specs, "selection": condition.get("goal", {}).get("selection", "none"),
                       "runtime": adapter.get("runtime", {}),
                       "continuity": manifest.get("continuity", {})},
        }
        return clean(payload)

    def population(self, identity):
        """Compare matched endpoints from this sweep, without launching probe runs.

        Arms are labels, not assumed equivalent conditions. The owner must still judge
        comparability. All replicates must have a committed endpoint at the selected
        run's episode; partial seeds are excluded and their omission is explicit.
        """
        entries, _ = self.catalog()
        selected = next((row for row in entries if row["id"] == identity), None)
        if selected is None:
            raise KeyError("unknown run")
        payload = self.load(identity)
        ep = payload["episodesComplete"]
        groups = defaultdict(list)
        excluded = 0
        for item in entries:
            if item["sweep"] != selected["sweep"]:
                continue
            sweep, record = item["sweep"], item["record"]
            relative = str(sweep.relative_to(self.root))
            root = confined(self.root, relative, "runs", record["id"])
            if not ep or ep not in snapshot_index(confined(root, ".snapshot.git")):
                excluded += 1
                continue
            rows = read_lines(confined(self.root, relative, "progress", record["id"] + ".jsonl"))
            progress = next((r for r in reversed(rows) if r.get("episode") == ep), {})
            actions = _normalized_actions(self.root, root, ep, progress)
            if not actions:
                excluded += 1
                continue
            groups[str(record.get("arm", "unknown"))].append(
                [a["tool"] or stream.TEXT_TOKEN for a in actions])
        arm = str(selected["record"].get("arm", "unknown"))
        evidence = {"episode": ep, "excluded_runs": excluded, "level": "order",
                    "comparability": "Same sweep and episode; operator must judge condition comparability.",
                    "draws": 200, "permutations": 1000, "seed": 0}
        if len(groups[arm]) >= 2:
            stats = stream.reliability(groups[arm], level="order")
            reliability = _metric("reliability", stats["ratio"], "within / composition-matched null",
                "proteus.measure.stream.reliability", "Null variation is zero; ratio is undefined.",
                evidence=evidence, statistics=clean(stats))
        else:
            reliability = _metric("reliability", None, "within / composition-matched null",
                "proteus.measure.stream.reliability", "Requires two matched seed endpoints.", evidence=evidence)
        if len(groups) >= 2 and all(len(ss) >= 2 for ss in groups.values()):
            reliable = all(stream.reliability(ss, level="order")["reliable"] for ss in groups.values())
            if reliable:
                stats = stream.between_within(dict(groups), level="order", permutations=1000)
                separation = _metric("separation", stats["R"], "between / within arms",
                    "proteus.measure.stream.between_within", evidence=evidence, statistics=stats)
            else:
                separation = _metric("separation", None, "between / within arms",
                    "proteus.measure.stream.between_within", "Within-arm reliability prerequisite failed.", evidence=evidence)
        else:
            separation = _metric("separation", None, "between / within arms",
                "proteus.measure.stream.between_within", "Requires two or more arms with two matched seeds each.", evidence=evidence)
        return clean({"measurements": [reliability, separation]})

    def export(self, identity, *, include_hidden=False):
        """Shared exports exclude hidden evaluation scores unless explicitly requested."""
        payload = self.load(identity)
        if not include_hidden:
            allowed = {s["name"] for s in payload["evaluators"] if s.get("visibility") == "observe"}
            primary = payload["evaluators"][0]["name"] if payload["evaluators"] else None
            payload["evaluators"] = [s for s in payload["evaluators"] if s["name"] in allowed]
            payload["config"]["evaluators"] = payload["evaluators"]
            for ep in payload["episodes"]:
                ep["evaluations"] = {name: row for name, row in ep["evaluations"].items() if name in allowed}
                if primary not in allowed:
                    ep["score"] = None
                # Errors can mention hidden evaluator names or private verifier outputs.
                ep.pop("error", None)
            payload.pop("error", None)
            payload["warnings"] = []
        payload["exportPolicy"] = {"hiddenIncluded": include_hidden, "rawReasoning": False,
                                   "toolParameters": False, "evaluatorDetails": False,
                                   "publicAnonymityGuaranteed": False}
        return payload
