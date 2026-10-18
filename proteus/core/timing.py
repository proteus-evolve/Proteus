"""Journaled elapsed measurements spanning managed operations.

Only a continuous invocation can measure an interval. A recovered end returns
its original committed measurement; a recovered start without a committed end
produces an explicitly unknown duration, never a replay or downtime estimate.
"""

from dataclasses import dataclass, field
import time
import uuid

from proteus.core.contracts import DataRef, NONNEGATIVE, TEXT, require
from proteus.core.serialization import Contract


@dataclass(frozen=True, kw_only=True)
class TimingStart(Contract):
    operation_id: str = field(metadata=TEXT)
    invocation: str = field(metadata=TEXT)
    monotonic_seconds: float = field(metadata=NONNEGATIVE)


@dataclass(frozen=True, kw_only=True)
class TimingMeasurement(Contract):
    start: DataRef
    wall_seconds: float | None = field(metadata=NONNEGATIVE)
    interrupted: bool

    def _validate(self):
        require(self.start.kind == "proteus.timing.start@1", "measurement requires a timing start")
        require(self.interrupted == (self.wall_seconds is None), "interrupted duration must stay unknown")


class TimingService:
    def __init__(self, services):
        self.services = services
        if not hasattr(services, "_timing_invocation"):
            services._timing_invocation = uuid.uuid4().hex

    def begin(self, operation_id):
        services = self.services

        def create():
            require(not any(ref.kind == "proteus.timing.start@1" and value.operation_id == operation_id
                            for ref, value in services.values._values.values()), "timing operation already started")
            start = TimingStart(operation_id=operation_id, invocation=services._timing_invocation,
                                monotonic_seconds=time.monotonic())
            return services.values.publish("proteus.timing.start@1", start)

        return services._record("timing.begin", {"operation_id": operation_id}, create, decode=DataRef.from_dict)

    def finish(self, start):
        services = self.services
        require(start.kind == "proteus.timing.start@1", "expected timing start reference")
        ticket = services.values.get(start)

        def complete():
            require(not any(ref.kind == "proteus.timing.measurement@1" and value.start == start
                            for ref, value in services.values._values.values()), "timing operation already finished")
            continuous = ticket.invocation == services._timing_invocation
            duration = time.monotonic() - ticket.monotonic_seconds if continuous else None
            measurement = TimingMeasurement(start=start, wall_seconds=duration, interrupted=not continuous)
            return services.values.publish("proteus.timing.measurement@1", measurement)

        return services._record("timing.finish", {"start": start}, complete, decode=DataRef.from_dict)
