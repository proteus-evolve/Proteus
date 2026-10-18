"""Shared Combee plateau controller over recorded ordinary learning iterations.

The curve follows arXiv:2604.04247v2 section 3.3. Bounds, fallback and fitting are
also checked against the pinned GEPA dynamic-sampling proposal. Sampling order
belongs to each method: this module neither shuffles tasks nor performs trials.
"""

from dataclasses import dataclass, field, replace
import math

from proteus.core.contracts import DIGEST, DataRef, NONNEGATIVE, require
from proteus.core.serialization import Contract, content_hash
from proteus.core.timing import TimingMeasurement


@dataclass(frozen=True, kw_only=True)
class DynamicBatchOptions(Contract):
    max_batch_size: int = field(metadata={"minimum": 1})
    min_batch_size: int = field(default=1, metadata={"minimum": 1})
    profile_batch_sizes: tuple[int, ...] | None = None
    slope_threshold: float = field(default=0.016, metadata={"exclusiveMinimum": 0, "maximum": 1})

    def _validate(self):
        require(self.max_batch_size >= self.min_batch_size, "batch bounds are reversed")
        if self.profile_batch_sizes is not None:
            require(bool(self.profile_batch_sizes) and all(
                type(size) is int and self.min_batch_size <= size <= self.max_batch_size
                for size in self.profile_batch_sizes), "profile sizes must be integers within batch bounds")

    def sizes(self, bound):
        require(type(bound) is int and bound > 0, "batch limit must be positive")
        if self.profile_batch_sizes is None:
            sizes = [self.min_batch_size]
            while sizes[-1] < self.max_batch_size:
                sizes.append(min(self.max_batch_size, 2 * sizes[-1]))
        else:
            sizes = self.profile_batch_sizes
        return tuple(sorted({min(size, bound, self.max_batch_size) for size in sizes}))


@dataclass(frozen=True, kw_only=True)
class BatchTiming(Contract):
    requested_size: int = field(metadata={"minimum": 1})
    actual_size: int = field(metadata={"minimum": 1})
    measurement: DataRef
    wall_seconds: float | None = field(metadata=NONNEGATIVE)
    epoch_seconds: float | None = field(metadata=NONNEGATIVE)
    diagnostic: str = ""

    def _validate(self):
        require(self.measurement.kind == "proteus.timing.measurement@1", "batch timing requires a measurement")
        require(self.actual_size <= self.requested_size, "batch timing exceeds its requested size")
        require((self.epoch_seconds is None) == bool(self.diagnostic), "unusable timings need a reason")


@dataclass(frozen=True, kw_only=True)
class BatchControllerState(Contract):
    options_hash: str = field(metadata=DIGEST)
    pool_hash: str = field(metadata=DIGEST)
    dataset_size: int = field(metadata={"minimum": 1})
    bound: int = field(metadata={"minimum": 1})
    profile_sizes: tuple[int, ...]
    observations: tuple[BatchTiming, ...] = ()
    selected_size: int | None = field(default=None, metadata={"minimum": 1})

    @property
    def size(self):
        return self.selected_size or self.profile_sizes[len(self.observations)]

    def _validate(self):
        require(self.bound <= self.dataset_size, "batch bound exceeds the task pool")
        require(bool(self.profile_sizes) and tuple(sorted(set(self.profile_sizes))) == self.profile_sizes
                and all(0 < size <= self.bound for size in self.profile_sizes), "invalid profiling sizes")
        require(len(self.observations) <= len(self.profile_sizes), "too many profiling observations")
        require(tuple(row.requested_size for row in self.observations)
                == self.profile_sizes[:len(self.observations)], "profile observation order changed")
        require(len({row.measurement.id for row in self.observations}) == len(self.observations),
                "profiling cannot reuse a timing observation")
        require(self.selected_size is None or self.profile_sizes[0] <= self.selected_size <= self.bound,
                "selected batch size lies outside the bounds")
        require(self.selected_size is not None or len(self.observations) < len(self.profile_sizes),
                "a finished profile must select its batch size")
        require(self.selected_size is None or len(self.profile_sizes) == 1
                or len(self.observations) == len(self.profile_sizes), "profile selected before completion")
        require(len(self.profile_sizes) != 1 or not self.observations,
                "a single-size policy requires no profiling observations")


def initialize(options, *, pool_hash, dataset_size, bound=None):
    require(bound is None or (type(bound) is int and bound > 0), "batch bound must be positive")
    bound = min(dataset_size, options.max_batch_size, bound or dataset_size)
    sizes = options.sizes(bound)
    return BatchControllerState(options_hash=content_hash(options), pool_hash=pool_hash,
        dataset_size=dataset_size, bound=bound, profile_sizes=sizes,
        selected_size=sizes[0] if len(sizes) == 1 else None)


def plateau(options, state, observations):
    points = [(row.requested_size, row.epoch_seconds) for row in observations if row.epoch_seconds is not None]
    fallback = state.profile_sizes[0]
    if len(points) < 2:
        return fallback
    xs, ys = [math.log(size) for size, _ in points], [math.log(delay) for _, delay in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    alpha = -sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance
    if not math.isfinite(alpha) or alpha <= 0:
        return fallback
    log_a = mean_y + alpha * mean_x
    log_peak = math.log(alpha) + log_a - (alpha + 1) * math.log(min(size for size, _ in points))
    log_tau = math.log(options.slope_threshold) + log_peak
    log_size = (math.log(alpha) + log_a - log_tau) / (alpha + 1)
    if log_size >= math.log(state.bound):
        return state.bound
    return max(fallback, min(state.bound, math.ceil(math.exp(log_size))))


def validate_state(options, state, *, pool_hash, dataset_size, bound=None):
    initial = initialize(options, pool_hash=pool_hash, dataset_size=dataset_size, bound=bound)
    require(replace(state, observations=(), selected_size=initial.selected_size) == initial,
            "batch controller state belongs to another pool or policy")
    for row in state.observations:
        expected, diagnostic = estimate(row.requested_size, row.actual_size, row.wall_seconds, dataset_size)
        require(row.diagnostic == diagnostic, "batch timing diagnostic differs from the measured batch")
        require(row.epoch_seconds == expected, "epoch estimate differs from the measured batch")
    if state.selected_size is not None and len(state.profile_sizes) > 1:
        require(state.selected_size == plateau(options, state, state.observations),
                "selected batch size differs from the recorded curve")


def validate_measurements(state, lookup):
    """Bind checkpoint observations to the immutable journaled evidence."""
    for row in state.observations:
        elapsed = lookup(row.measurement)
        require(type(elapsed) is TimingMeasurement and content_hash(elapsed) == row.measurement.content_hash
                and row.wall_seconds == elapsed.wall_seconds,
                "batch timing differs from its recorded measurement")


def estimate(requested_size, actual_size, wall_seconds, dataset_size):
    if wall_seconds is None:
        return None, "interrupted interval has no continuous elapsed measurement"
    if actual_size != requested_size:
        return None, "short batch does not profile the requested size"
    if wall_seconds <= 0:
        return None, "nonpositive duration cannot fit a throughput curve"
    epoch_seconds = wall_seconds * dataset_size / actual_size
    if not math.isfinite(epoch_seconds):
        return None, "epoch estimate is not finite"
    return epoch_seconds, ""


def observe(options, state, *, actual_size, measurement, elapsed):
    require(content_hash(options) == state.options_hash, "batch controller options changed")
    require(measurement.content_hash == content_hash(elapsed), "batch timing reference differs from its measurement")
    require(type(actual_size) is int and 0 < actual_size <= state.size, "batch sample count differs")
    if state.selected_size is not None:
        return state
    epoch_seconds, reason = estimate(state.size, actual_size, elapsed.wall_seconds, state.dataset_size)
    record = BatchTiming(requested_size=state.size, actual_size=actual_size, measurement=measurement,
        wall_seconds=elapsed.wall_seconds, epoch_seconds=epoch_seconds, diagnostic=reason)
    observations = (*state.observations, record)
    selected = plateau(options, state, observations) if len(observations) == len(state.profile_sizes) else None
    return replace(state, observations=observations, selected_size=selected)
