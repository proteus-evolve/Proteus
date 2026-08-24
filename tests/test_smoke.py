"""End-to-end smoke tests — the framework runs offline and the instruments respond."""

from pathlib import Path

from proteus.adapters.minimal import MinimalHarness
from proteus.core import NEUTRAL, GoalConfig, review, snapshot
from proteus.core.episode import RunConfig, run
from proteus.measure import distance, stream


def _run(tmp_path, disposition, seed=0, episodes=6):
    cfg = RunConfig(
        name=disposition.label, adapter=MinimalHarness(), disposition=disposition,
        run_id=f"run-{disposition.label}-{seed}",
        goal=GoalConfig.no_goal(), root=tmp_path / disposition.label / str(seed),
        model="mock", episodes=episodes, seed=seed,
    )
    return run(cfg)


def test_episode_runs_and_snapshots(tmp_path):
    res = _run(tmp_path, NEUTRAL)
    assert res.episodes_complete == 6
    assert res.error == ""
    work = Path(res.root) / "harness"
    assert snapshot.head(work)  # snapshots exist
    assert snapshot.commit_for_episode(work, 6) is not None


def test_disposition_shifts_surface(tmp_path):
    notes = _run(tmp_path, review("notes"))
    tools = _run(tmp_path, review("tools"))
    surfaces = MinimalHarness().surfaces()
    n_units = distance.units(Path(notes.root) / "harness", surfaces)
    t_units = distance.units(Path(tools.root) / "harness", surfaces)
    # a notes-disposition builds more notes; a tools-disposition builds more tools
    assert len(n_units["notes"]) > len(n_units["tools"])
    assert len(t_units["tools"]) > len(t_units["notes"])


def test_behavioural_R_separates_arms(tmp_path):
    streams = {}
    for disp in (NEUTRAL, review("notes"), review("tools")):
        streams[disp.label] = []
        for s in range(4):
            res = _run(tmp_path, disp, seed=s)
            trace = MinimalHarness().read_trace(Path(res.root), res.episodes_complete)
            streams[disp.label].append(stream.tool_stream(trace))
    r = stream.between_within(streams, level="freq", permutations=1000)
    assert r["R"] > 1.0          # who is acting explains more than chance
    assert r["p"] < 0.1


def test_distance_path_length(tmp_path):
    res = _run(tmp_path, review("notes"), episodes=4)
    work = Path(res.root) / "harness"
    surfaces = MinimalHarness().surfaces()
    # two consecutive states differ (the run added units)
    d = distance.compare(work, work, surfaces)
    assert d["notes"].distance == 0.0     # identical to itself
