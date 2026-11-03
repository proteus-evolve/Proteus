"""GEPA's pinned instance-frontier coverage pruning and weighted parent selection.

Call the actual upstream helpers; do not substitute generic Pareto dominance.
The supplied ordered population represents accepted program indices, independently
of artifact content and the archive's rejected proposals. Full comparable validation
is required: sparse/partial evaluation and objective/hybrid frontiers are not covered.
"""

from __future__ import annotations

import math
from pathlib import Path

from proteus.core.contracts import PluginRef, require
from proteus.core.plugins import EmptyOptions, Plugin, implementation_hash
from proteus.core.search import PolicyResult
from proteus.core.serialization import content_hash
from proteus.methods.gepa.reference import REVISION, SOURCE_HASH, load_reference


class InstanceFrontier:
    def __init__(self):
        load_reference()
        from gepa.gepa_utils import remove_dominated_programs, select_program_candidate_from_pareto_front
        self.prune = remove_dominated_programs
        self.sample = select_program_candidate_from_pareto_front

    def inputs(self, view):
        require(not view.objectives, "GEPA instance frontier requires per-task scores, not aggregate objectives")
        require(bool(view.vectors) and bool(view.dimensions), "GEPA needs a nonempty scored population")
        require(view.candidate_ids is not None, "GEPA requires an explicit accepted population in program-index order")
        require(view.candidate_ids == tuple(item.candidate_id for item in view.vectors),
                "GEPA accepted population has missing/invalid validation evidence; cannot renumber program indices")
        # Use the reference's sum/len, including its floating-point and tie behavior.
        scores = [sum(item.scores) / len(item.scores) for item in view.vectors]
        require(all(math.isfinite(score) for score in scores), "GEPA aggregate score overflow")
        fronts = {}
        # Build sets incrementally, like GEPAState, preserving integer program IDs.
        for dimension in range(len(view.dimensions)):
            best = max(item.scores[dimension] for item in view.vectors)
            fronts[dimension] = set()
            for index, item in enumerate(view.vectors):
                if item.scores[dimension] == best:
                    fronts[dimension].add(index)
        return fronts, scores

    def _result(self, view, fronts, pruned, chosen):
        reasons = {}
        for index, item in enumerate(view.vectors):
            wins = sum(index in front for front in fronts.values())
            weight = sum(index in front for front in pruned.values())
            if not wins:
                reason = "no instance maximum; parent weight=0"
            elif not weight:
                reason = f"coverage redundant under reference pruning; instance wins={wins}; parent weight=0"
            else:
                reason = f"coverage retained; instance wins={wins}; parent weight={weight}"
            reasons[item.candidate_id] = reason + ("; selected" if index in chosen else "; not selected")
        return PolicyResult(selected=tuple(view.vectors[index].candidate_id for index in chosen), reasons=reasons)

    def retain(self, view, rng):
        fronts, scores = self.inputs(view)
        pruned = self.prune(fronts, scores)
        # Reporting order is accepted-program order; the upstream sampler below
        # retains its own iteration order and RNG semantics.
        chosen = tuple(index for index in range(len(view.vectors))
                       if any(index in front for front in pruned.values()))
        return self._result(view, fronts, pruned, chosen)

    def select_parent(self, view, rng):
        fronts, scores = self.inputs(view)
        pruned = self.prune(fronts, scores)
        chosen = self.sample(fronts, scores, rng)
        return self._result(view, fronts, pruned, (chosen,))

    def select_final(self, view, rng):
        _, scores = self.inputs(view)
        from gepa.gepa_utils import idxmax
        chosen = idxmax(scores)
        return PolicyResult(selected=(view.vectors[chosen].candidate_id,), reasons={item.candidate_id:
            "maximum reference mean; first program breaks ties" if index == chosen else "lower mean or later tie"
            for index, item in enumerate(view.vectors)})


def register(registry):
    policy = InstanceFrontier()  # Fail before a run if the optional reference differs.
    root = Path(__file__).resolve().parent
    digest = content_hash({"bridge": implementation_hash({path.name: path for path in root.glob("*.py")}),
                           "upstream_revision": REVISION, "upstream_source": SOURCE_HASH})
    for name, role in (("gepa_parent", "parent"), ("gepa_frontier", "archive"), ("gepa_best", "final")):
        registry.register(Plugin(ref=PluginRef(name="proteus." + name, version="1"), kind="policy",
            options=EmptyOptions, factory=lambda options: policy, implementation_hash=digest,
            source="proteus", capabilities=("search." + role,)))
