"""Recorded occurrence-level grouping; ACE and GEPA retain their own RNG rules."""

from __future__ import annotations

from dataclasses import dataclass, field
import random
from typing import Any, Literal

from proteus.core.contracts import DataRef, TEXT, require
from proteus.core.serialization import Contract


@dataclass(frozen=True, kw_only=True)
class Grouping(Contract):
    profile: Literal["ace", "gepa"]
    duplication_factor: int = field(default=2, metadata={"minimum": 1})
    num_groups: int | None = field(default=None, metadata={"minimum": 1})

    def _validate(self):
        require(
            self.profile == "ace" or self.num_groups is None,
            "GEPA reference grouping uses floor(sqrt(n))",
        )


@dataclass(frozen=True, kw_only=True)
class Occurrence(Contract):
    source_index: int = field(metadata={"minimum": 0})
    copy_index: int = field(metadata={"minimum": 0})


def split_occurrences(count, options, rng):
    """Pure index plan: copies never become additional evaluated samples."""
    if count == 0:
        return ()
    if options.profile == "gepa" and count < 4:
        return (tuple(Occurrence(source_index=i, copy_index=0) for i in range(count)),)
    factor = options.duplication_factor
    pairs = (
        ((i, copy) for i in range(count) for copy in range(factor))
        if options.profile == "ace"
        else ((i, copy) for copy in range(factor) for i in range(count))
    )
    occurrences = [Occurrence(source_index=i, copy_index=copy) for i, copy in pairs]
    if options.profile == "gepa" or factor > 1:
        rng.shuffle(occurrences)
    groups = min(options.num_groups or max(1, int(count**0.5)), len(occurrences))
    size, remainder = divmod(len(occurrences), groups)
    result, offset = [], 0
    for index in range(groups):
        end = offset + size + (index < remainder)
        result.append(tuple(occurrences[offset:end]))
        offset = end
    return tuple(result)


@dataclass(frozen=True, kw_only=True)
class GroupPlan(Contract):
    operation_id: str = field(metadata=TEXT)
    options: Grouping
    records: tuple[DataRef, ...]
    groups: tuple[tuple[Occurrence, ...], ...]
    rng_before: Any
    rng_after: Any

    def _validate(self):
        rng = random.Random()
        rng.setstate(self.rng_before)
        require(
            self.groups == split_occurrences(len(self.records), self.options, rng),
            "aggregation groups differ from their source occurrences and RNG",
        )
        require(rng.getstate() == self.rng_after, "aggregation final RNG differs")


def plan_groups(services, records, options, *, operation_id):
    require(services._journal is not None, "aggregation requires a journal-enabled method")
    records = tuple(records)
    for ref in records:
        services.values.get(ref)

    def build():
        before = services.rng.getstate()
        groups = split_occurrences(len(records), options, services.rng)
        return GroupPlan(
            operation_id=operation_id,
            options=options,
            records=records,
            groups=groups,
            rng_before=before,
            rng_after=services.rng.getstate(),
        )

    plan = services._record(
        "aggregation.plan",
        {
            "operation_id": operation_id,
            "records": [ref.to_dict() for ref in records],
            "options": options.to_dict(),
        },
        build,
        decode=GroupPlan.from_dict,
    )
    return services.values.publish("proteus.combee.plan@1", plan)
