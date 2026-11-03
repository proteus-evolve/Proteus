"""Release-smoke assertions: did a 2-episode sweep actually exercise the instrument?

    python scripts/smoke_check.py <sweep_root> --harness <name> [--max-turns 10]

Checks, per run in the sweep:
  1. every seed completed all its episodes with no error;
  2. the trace parses into a non-empty ActionEvent stream (the harness's log format
     still matches the adapter);
  3. the snapshot chain is gapless: episode 0..N commits all present;
  4. every episode's progress line carries a score for every evaluator in the manifest;
  5. the turn budget held: in-process harnesses stay <= max_turns; containerized ones
     either stay under it or carry the turn_capped flag;
  6. a non-neutral arm has a non-empty disposition fingerprint;
  7. harnesses with a self-code surface: src/ is in the episode-0 snapshot, the evolved
     copy passes its complete viability contract (including DSH's fresh headless cold
     start), a planted error is refused by the gate, and restoring clears it.

Exit code = number of failed checks. Prints one line per check per run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILS = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global FAILS
    mark = "ok  " if ok else "FAIL"
    print(f"  {mark} {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS += 1


def episode_commits(run_root: Path, episodes: int) -> list[int]:
    git_dir = run_root / ".snapshot.git"
    if not git_dir.exists():
        return []
    log = subprocess.run(
        ["git", "--git-dir", str(git_dir), "log", "--format=%s"],
        capture_output=True, text=True, errors="replace", check=False).stdout
    found = []
    for ep in range(episodes + 1):
        if any(line.startswith(f"episode {ep}:") for line in log.splitlines()):
            found.append(ep)
    return found


def codex_runtime_matches_checkpoint(adapter, run_root: Path, episode: int) -> bool:
    """Prove the final episode activated source changed from the initial checkpoint."""
    from proteus.core import snapshot

    harness = run_root / "harness"
    checkpoint = snapshot.commit_for_episode(harness, episode - 1)
    initial = snapshot.commit_for_episode(harness, 0)
    if checkpoint is None or initial is None:
        return False
    try:
        record = json.loads((run_root / "traces" / f"ep{episode:03d}-runtime.json").read_text())
        with tempfile.TemporaryDirectory(prefix="proteus-codex-smoke-") as temporary:
            active = Path(temporary) / "active"
            snapshot.materialize(harness, checkpoint, active)
            source_hash = adapter._source_hash(active / "src")
            seeded = Path(temporary) / "seeded"
            snapshot.materialize(harness, initial, seeded)
            if source_hash == adapter._source_hash(seeded / "src"):
                return False
        relative = f".codex-builds/{source_hash}"
        return (record == {"version": 1, "episode": episode,
                           "active_source_sha256": source_hash, "publication": relative}
                and adapter._publication_is_valid(run_root / relative, source_hash))
    except (OSError, ValueError, RuntimeError):
        return False


def source_runtime_matches_checkpoint(adapter, run_root: Path, episode: int) -> bool:
    """Require startup receipts for the edited checkpoint, not post-run build caches."""
    from proteus.core import snapshot
    from proteus.core.native_activation import source_tree_hash, validate_activation
    from proteus.core.native_inputs import read_native_inputs

    if adapter.name not in ("pi", "dsh"):
        return False
    harness = run_root / "harness"
    checkpoint = snapshot.commit_for_episode(harness, episode - 1)
    initial = snapshot.commit_for_episode(harness, 0)
    if checkpoint is None or initial is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="proteus-source-smoke-") as temporary:
            active, seeded = Path(temporary) / "active", Path(temporary) / "seeded"
            snapshot.materialize(harness, checkpoint, active)
            snapshot.materialize(harness, initial, seeded)
            expected = source_tree_hash(active / "src")
            if expected == source_tree_hash(seeded / "src"):
                return False
        records = [row for row in read_native_inputs(run_root)
                   if row["input"]["episode"] == episode
                   and not (row["input"]["backend"] == "evaluator"
                            and row["input"]["scope"] == "native_evaluator")]
        return bool(records) and all(
            row["input"]["backend"] == adapter.name
            and row["completion"] == "returned"
            and type(row["outcome"].get("returncode")) is int
            and validate_activation(row["outcome"].get("activation"), row["input"])["source_sha256"] == expected
            for row in records)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False


def native_inputs_cover_run(run_root: Path, harness: str, episodes: int) -> bool:
    """Check the live adapter boundary, without equating it with provider messages."""
    from proteus.core.native_inputs import read_native_inputs
    scopes = {"minimal": "policy_prompt", "llm": "chat_request",
              "pi": "harness_cli", "dsh": "harness_cli", "codex": "harness_cli"}
    if harness not in scopes:
        return False
    try:
        # Evaluator receipts share the journal but are not harness phase dispatches.
        records = [row for row in read_native_inputs(run_root)
                   if not (row["input"]["backend"] == "evaluator"
                           and row["input"]["scope"] == "native_evaluator")]
        return (bool(records)
                and {row["input"]["episode"] for row in records} == set(range(1, episodes+1))
                and all(row["completion"] == "returned" and row["input"]["backend"] == harness
                        and row["input"]["scope"] == scopes[harness]
                        and row["input"]["phase"] in ("observe", "propose", "act", "reflect")
                        for row in records))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--harness", required=True)
    ap.add_argument("--max-turns", type=int, default=10)
    ap.add_argument("--require-self-edit", action="store_true",
                    help="fail unless the agent edited its own src/ during the run AND "
                         "the final episode recorded activation of the edited checkpoint")
    args = ap.parse_args()

    from proteus.cli import _adapter_factory
    from proteus.core import snapshot
    adapter = _adapter_factory(args.harness)()

    root = Path(args.root).expanduser()
    manifest = json.loads((root / "manifest.json").read_text())
    episodes = int(manifest["episodes"])
    evaluator_names = [e["name"] for e in manifest.get("evaluators", [])]
    records = [json.loads(line)
               for line in (root / "seeds.jsonl").read_text().splitlines() if line.strip()]

    print(f"{args.harness}: {len(records)} runs x {episodes} episodes, "
          f"evaluators={evaluator_names}, announce={manifest.get('announce_budget')}")
    check(bool(evaluator_names), "manifest lists the evaluators")
    check(bool(manifest.get("goal")), "manifest records the goal text")

    for rec in records:
        run_root = Path(rec["root"])
        rid = run_root.name
        print(f"[{rec['arm']}/{rec['seed']}] {rid}")
        check(rec.get("episodes_complete") == episodes and not rec.get("error"),
              f"completed {episodes}/{episodes} with no error",
              str(rec.get("error"))[:120])

        check(native_inputs_cover_run(run_root, args.harness, episodes),
              "hashed final adapter inputs cover every episode with terminal outcomes")

        # 2. trace parses and is non-empty
        try:
            trace = adapter.read_trace(run_root, episodes)
            check(len(trace) > 0, f"trace parses ({len(trace)} events in ep{episodes})")
        except Exception as exc:  # noqa: BLE001
            check(False, "trace parses", f"{type(exc).__name__}: {exc}")
            trace = []

        # 3. gapless snapshot chain
        found = episode_commits(run_root, episodes)
        check(found == list(range(episodes + 1)),
              f"snapshot chain gapless (episodes {found})")

        # 4. every evaluator scored every episode
        prog = root / "progress" / f"{rid}.jsonl"
        rows = ([json.loads(line) for line in prog.read_text().splitlines() if line.strip()]
                if prog.exists() else [])
        scored = all(set(evaluator_names) <= set(r.get("scores", {})) for r in rows)
        check(len(rows) == episodes and scored,
              "every evaluator scored every episode",
              f"{len(rows)} progress rows")

        # 5. the budget held
        for r in rows:
            turns = int(r.get("turns", 0))
            capped = bool(r.get("counters", {}).get("turn_capped"))
            if args.harness in ("minimal", "llm"):
                check(turns <= args.max_turns,
                      f"ep{r['episode']}: turns {turns} <= {args.max_turns}")
            else:
                check(turns <= args.max_turns or capped,
                      f"ep{r['episode']}: turns {turns} within budget or capped",
                      f"turns={turns} capped={capped}")

        # 6. disposition fingerprint on the non-neutral arm
        if rec["arm"] != "neutral":
            fp = adapter.disposition_fingerprint(run_root / "harness")
            check(bool(fp), "disposition fingerprint present")

        # 7. the self-code surface, where the harness has one
        src = run_root / "harness" / "src"
        if src.is_dir() and hasattr(adapter, "check_boot"):
            git_dir = run_root / ".snapshot.git"
            initial = snapshot.commit_for_episode(run_root / "harness", 0)
            preceding = snapshot.commit_for_episode(run_root / "harness", episodes - 1)
            ep0 = subprocess.run(
                ["git", "--git-dir", str(git_dir), "ls-tree", "-r", "--name-only",
                 initial or "HEAD"],
                capture_output=True, text=True, errors="replace", check=False).stdout
            check("src/" in ep0, "episode-0 snapshot contains src/")
            check(adapter.check_boot(run_root / "harness") == "",
                  "evolved self-code still boots")
            # plant into a file the build definitely compiles: an alphabetical-first
            # pick can land on a doc or example outside the build's include set, whose
            # type errors legitimately do not fail the build
            patterns = ("packages/*/src/cli.ts", "apps/*/src/bin.ts",
                        "packages/*/src/*.ts", "packages/*/*/src/*.ts",
                        "apps/*/src/*.ts", "lib/*.js")
            if args.harness == "codex":
                patterns = ("codex-rs/cli/src/main.rs",)
            if args.require_self_edit:
                # 1) the agent edited its own source before the final episode, so at
                # least one later boot loaded the edit
                diff = subprocess.run(
                    ["git", "--git-dir", str(git_dir), "diff", "--name-only",
                     initial or "HEAD", preceding or "HEAD", "--", "src/"],
                    capture_output=True, text=True, errors="replace", check=False).stdout
                edited = [line for line in diff.splitlines()
                          if line.strip() and "node_modules" not in line]
                check(bool(edited), "the agent edited its own source before the last episode",
                      "no src/ change between episode 0 and the second-to-last snapshot")
                # 2) runtime evidence is tied to the immutable preceding checkpoint.
                # A later viability probe or a leftover cache cannot establish this.
                if args.harness == "codex":
                    check(codex_runtime_matches_checkpoint(adapter, run_root, episodes),
                          "the final episode loaded the validated binary pair for the edited checkpoint")
                else:
                    check(source_runtime_matches_checkpoint(adapter, run_root, episodes),
                          "the final episode recorded startup from the edited checkpoint")
            victims = [v for pat in patterns for v in sorted(src.glob(pat))]
            check(bool(victims), "a compiled source file is available for the rejection probe")
            if victims:
                victim = victims[0]
                original = victim.read_text(encoding="utf-8", errors="replace")
                planted = ('compile_error!("Proteus smoke planted error");\n'
                           if args.harness == "codex" else "const proteusSmokeBroken: string = 42;\n")
                try:
                    victim.write_text(planted + original)
                    msg = adapter.check_boot(run_root / "harness")
                    expected = "does not build/boot" if args.harness == "codex" else "does not boot"
                    check(expected in msg, "planted error refused by the boot gate", msg)
                finally:
                    victim.write_text(original)
                check(adapter.check_boot(run_root / "harness") == "",
                      "restore clears the gate")

    print(f"\n{args.harness}: {FAILS} failed checks")
    return FAILS


if __name__ == "__main__":
    raise SystemExit(main())
