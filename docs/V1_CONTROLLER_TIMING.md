# Controller elapsed time

Every new `run_recipe` invocation writes an immutable start before journal
recovery and a finish after method execution, cleanup, final journal commit and
execution/result snapshot publication. The caller holds the run lease throughout.
Monotonic time measures each interval inside one process; wall-clock timestamps
are diagnostic fields and are never subtracted across processes.

The boundary excludes waiting for the lease, resolving the registry/recipe,
constructing the method/services and writing the manifest. It includes artifact
import, evaluations and final confirmation. It ends before publishing the timing
finish itself. It is a controller measurement, not complete CLI startup latency.

`proteus recipe export` and CORAL reports expose `timing`; exports also write
`controller-intervals.csv`. The common StudySpec aggregates both comparable numeric
fields without replacing missing data with zero:

| Field | Meaning |
| --- | --- |
| `continuous_run_seconds` | Duration of one uninterrupted initial invocation that completed successfully. A resumed interrupted run has no such measurement. |
| `controller_seconds` | Sum of all non-replay controller intervals, only if all intervals and the original prefix were measured. |
| `known_controller_seconds` | Subtotal of the available intervals, explicitly not a complete run duration. This diagnostic subtotal is not a study metric. |
| `unknown_intervals`, `unmeasured_prefix` | A hard exit leaves a start without a finish. Legacy runs first measured on resume also lack their original prefix. |
| `replayed_invocations` | Calls that loaded an already committed journal result. Their interval remains inspectable, but is excluded from experiment totals. |

An exception with a published finish can retain a measured interval; it does not
establish a successful continuous run. Publication loss after a completed result
still leaves timing unknown. Resume validates existing records and appends a new
interval; it never closes an earlier interval using the new process's clock.
Legacy runs remain readable with unavailable timing. Controller timing history
is cumulative even for non-journal methods; this does not extend their task or
model evidence beyond the latest invocation described by `history_scope`.

Starts form an ordered chain bound to the manifest. Outcomes bind their start
and exact immutable execution snapshot; snapshots reference their timing start.
Report readers check these bindings, finite nonnegative durations and missing or
reordered records without running plugins. Hashes detect inconsistent evidence;
they do not authenticate against coordinated rewriting of an entire run.

Controller intervals exclude downtime. In CORAL, native workers may continue
running during downtime, so controller time cannot establish their total active
time. The ledger's summed operation time may overlap, and its calendar event span
may include gaps. Those fields retain their separate names and semantics. A
Combee Formula study's existing outer timer also retains its wider boundary;
compare runs using the same timing definition.

Tests cover continuous/no-op resume, hard exits before and after committed work,
missing timing publication, failed non-journal methods, legacy runs, corrupt
bindings, multi-seed reports and installed wheel/sdist execution. These are
measurement and recovery checks; real-model speed or collaboration gains require
the corresponding controlled experiments.
