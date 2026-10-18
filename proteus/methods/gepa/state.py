"""Typed bridge state and sealed, data-only upstream checkpoints.

Only the fresh upstream engine's own temporary pickle is read. Persisted checkpoints
use an explicit JSON tree codec; restoration never unpickles checkpoint bytes.
"""

from __future__ import annotations

import pickle
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from proteus.artifacts.store import _read, publish_bytes
from proteus.core.contracts import DIGEST, TEXT, DataRef, require
from proteus.core.serialization import Contract, canonical_json, content_hash, read_json
from proteus.evaluation.tasks import TaskBatch
from proteus.methods.gepa.reference import REVISION, SOURCE_HASH
from proteus.methods.gepa.sampling import SamplerState


@dataclass(frozen=True, kw_only=True)
class BatchData(Contract):
    receipt: DataRef
    outputs: tuple[Any, ...]
    scores: tuple[float, ...]
    trajectories: tuple[Any, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class ProposalDecision(Contract):
    candidate_id: str = field(metadata=TEXT)
    parent_id: str = field(metadata=TEXT)
    iteration: int
    disposition: Literal["accepted", "rejected"]
    reason: str = field(metadata=TEXT)


@dataclass(frozen=True, kw_only=True)
class BridgeState(Contract):
    accepted_ids: tuple[str, ...] = field(metadata={"minItems": 1, "uniqueItems": True})
    seed: BatchData
    validation_protocol: str = field(metadata=DIGEST)
    sampler: SamplerState
    decisions: tuple[ProposalDecision, ...] = ()
    evaluations: int = field(default=0, metadata={"minimum": 0})
    reflection_calls: int = field(default=0, metadata={"minimum": 0})


@dataclass(frozen=True, kw_only=True)
class GepaState(Contract):
    train: TaskBatch
    validation: TaskBatch
    bridge: BridgeState
    phase: Literal["engine", "finished"] = "engine"
    engine_hash: str | None = field(default=None, metadata=DIGEST)
    outputs: Mapping[str, DataRef] = field(default_factory=dict)

    def _validate(self):
        require((self.phase == "engine") == (self.engine_hash is not None), "GEPA checkpoint phase/hash differ")


@dataclass(frozen=True, kw_only=True)
class GepaSummary(Contract):
    revision: str
    accepted_ids: tuple[str, ...]
    upstream_best_id: str
    selected_ids: tuple[str, ...]
    decisions: tuple[ProposalDecision, ...]
    metric_calls: int
    reflection_calls: int
    iterations: int
    stop_threshold: int
    evaluation_upper_bound: int


def pack(value):
    """Preserve mapping order and non-string keys; explicitly tag all containers."""
    if value is None or type(value) in (str, int, float, bool):
        return ["atom", value]
    if type(value) is dict:
        return ["dict", [[pack(key), pack(item)] for key, item in value.items()]]
    if type(value) in (list, tuple, set):
        items = [pack(item) for item in value]
        if type(value) is set:
            items.sort(key=canonical_json)
        return [type(value).__name__, items]
    raise ValueError(f"unsupported GEPA checkpoint value: {type(value).__name__}")


def unpack(value):
    require(type(value) is list and len(value) == 2, "invalid GEPA checkpoint node")
    kind, data = value
    if kind == "atom":
        require(data is None or type(data) in (str, int, float, bool), "invalid checkpoint atom")
        return data
    require(type(data) is list, "invalid checkpoint container")
    if kind == "dict":
        result = {}
        for entry in data:
            require(type(entry) is list and len(entry) == 2, "invalid checkpoint mapping entry")
            key = unpack(entry[0])
            require(key not in result, "duplicate checkpoint mapping key")
            result[key] = unpack(entry[1])
        return result
    require(kind in ("list", "tuple", "set"), "unknown checkpoint container")
    items = [unpack(item) for item in data]
    if kind == "tuple":
        return tuple(items)
    if kind == "set":
        # GEPA's stored program-index frontiers accumulate in ascending order.
        require(all(type(item) is int and item >= 0 for item in items), "unsupported GEPA frontier set")
        result = set()
        for item in sorted(items):
            result.add(item)
        require(len(result) == len(items), "duplicate checkpoint set member")
        return result
    return items


def identity(services):
    return {"run_id": services.run_id, "config_hash": services.recipe.config_hash,
            "lock_hash": services.lock_hash, "revision": REVISION, "source_hash": SOURCE_HASH}


def seal(services, work):
    data = pickle.loads(_read(work / "gepa_state.bin"))
    require(type(data) is dict, "invalid freshly generated GEPA engine state")
    envelope = {"identity": identity(services), "state": pack(data)}
    digest = content_hash(envelope)
    path = services.root / "private/gepa/checkpoints" / (digest.split(":")[1] + ".json")
    publish_bytes(path, canonical_json(envelope).encode())
    return digest


def restore_engine(services, work, digest):
    path = services.root / "private/gepa/checkpoints" / (digest.split(":")[1] + ".json")
    envelope = read_json(_read(path).decode())
    require(content_hash(envelope) == digest and envelope["identity"] == identity(services),
            "GEPA engine checkpoint integrity/identity mismatch")
    data = unpack(envelope["state"])
    from gepa.core.state import GEPAState
    require(type(data) is dict and data.get("validation_schema_version") == GEPAState._VALIDATION_SCHEMA_VERSION,
            "GEPA engine state requires an unsupported migration")
    # GEPA consumes a pickle, regenerated solely from verified primitive data.
    (work / "gepa_state.bin").write_bytes(pickle.dumps(data))
