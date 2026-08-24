"""Adapter compliance check: does a `HarnessAdapter` implementation hold the contract?

    proteus check --harness mypkg.my_adapter:MyHarness            # static + provisioning
    proteus check --harness mypkg.my_adapter:MyHarness --episode  # also run one episode

Static checks cost nothing and run anywhere; `--episode` actually executes one neutral
episode through the adapter (which may launch containers / call a model, i.e. cost money)
and then verifies the trace contract. Each check prints PASS/FAIL with the reason; exit
code is the number of failures.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from proteus.core import NEUTRAL, GoalConfig, review, snapshot
from proteus.core.adapter import ActionEvent, HarnessAdapter
from proteus.core.episode import RunConfig, run


def check_adapter(adapter, *, episode: bool = False, verbose: bool = True) -> list[str]:
    """Run the compliance checks; returns the list of failure messages."""
    failures: list[str] = []

    def ok(cond: bool, label: str, why: str = ""):
        if verbose:
            print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f" — {why}"))
        if not cond:
            failures.append(label)

    # --- static -------------------------------------------------------------------------
    ok(isinstance(adapter, HarnessAdapter), "implements the HarnessAdapter protocol",
       "missing one or more required methods/attributes")
    surfaces = list(adapter.surfaces())
    ok(len(surfaces) > 0, "declares at least one surface")
    ok(all(s.name and s.subdir for s in surfaces), "surfaces have name and subdir")
    ok(len({s.name for s in surfaces}) == len(surfaces), "surface names are unique")

    # --- provisioning (filesystem only) --------------------------------------------------
    with tempfile.TemporaryDirectory(prefix="proteus-check-") as tmp:
        harness = Path(tmp) / "run" / "harness"
        try:
            adapter.seed(harness, 0)
        except Exception as exc:  # noqa: BLE001
            ok(False, "seed runs", repr(exc))
            return failures

        try:
            adapter.install_disposition(harness, NEUTRAL)
            # existence is checked after the NEUTRAL install: adapters wrapping harnesses
            # that couple seeding and installing (see aki) materialise here
            ok(harness.exists(), "seed + install materialise the harness root")
            f_neutral = adapter.disposition_fingerprint(harness)
            disp = review(surfaces[0].name)
            adapter.install_disposition(harness, disp)
            f_installed = adapter.disposition_fingerprint(harness)
            ok(f_installed != f_neutral,
               "disposition install changes the fingerprint",
               "fingerprint identical under NEUTRAL and an installed disposition")
            adapter.install_disposition(harness, NEUTRAL)
            ok(adapter.disposition_fingerprint(harness) == f_neutral,
               "disposition is removable (reinstalling NEUTRAL restores the fingerprint)",
               "residue left after removal — crystallization cannot mount F0")
        except Exception as exc:  # noqa: BLE001
            ok(False, "disposition install/remove", repr(exc))

        try:
            snapshot.init(harness)
            ok(bool(snapshot.head(harness)), "harness state is snapshot-able")
        except Exception as exc:  # noqa: BLE001
            ok(False, "harness state is snapshot-able", repr(exc))

    # --- one live episode (optional: may cost money) --------------------------------------
    if episode:
        with tempfile.TemporaryDirectory(prefix="proteus-check-ep-") as tmp:
            cfg = RunConfig(name="check", run_id="run-check", adapter=adapter, disposition=NEUTRAL,
                            goal=GoalConfig.no_goal(), root=Path(tmp) / "run",
                            model="", episodes=1, seed=0)
            res = run(cfg)
            ok(res.episodes_complete == 1, "one neutral episode completes",
               res.error or "episode did not complete")
            if res.episodes_complete:
                trace = adapter.read_trace(Path(res.root), 1)
                ok(len(trace) > 0, "read_trace returns events")
                ok(all(isinstance(e, ActionEvent) for e in trace),
                   "trace events are ActionEvent")
                phases = {e.phase for e in trace}
                ok(phases <= {"observe", "propose", "act", "reflect"},
                   "trace phases are the canonical four", f"got {sorted(phases)}")
                ok(snapshot.commit_for_episode(Path(res.root) / "harness", 1) is not None,
                   "episode 1 has a snapshot commit")

    if verbose:
        print(f"\n{len(failures)} failure(s)")
    return failures
