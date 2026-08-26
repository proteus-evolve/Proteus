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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--harness", required=True)
    ap.add_argument("--max-turns", type=int, default=10)
    ap.add_argument("--require-self-edit", action="store_true",
                    help="fail unless the agent edited its own src/ during the run AND "
                         "the boot used the rebuilt copy (dist cache keyed by the "
                         "current source hash)")
    args = ap.parse_args()

    from proteus.cli import _adapter_factory
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
            ep0 = subprocess.run(
                ["git", "--git-dir", str(git_dir), "ls-tree", "-r", "--name-only",
                 f"HEAD~{episodes}" if episodes else "HEAD"],
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
            if args.require_self_edit:
                # 1) the agent edited its own source before the final episode, so at
                # least one later boot loaded the edit
                diff = subprocess.run(
                    ["git", "--git-dir", str(git_dir), "diff", "--name-only",
                     f"HEAD~{episodes}", "HEAD~1", "--", "src/"],
                    capture_output=True, text=True, errors="replace", check=False).stdout
                edited = [line for line in diff.splitlines()
                          if line.strip() and "node_modules" not in line]
                check(bool(edited), "the agent edited its own source before the last episode",
                      "no src/ change between episode 0 and the second-to-last snapshot")
                # 2) machine evidence the edited source was rebuilt and booted: the boot
                # wrapper writes a dist cache tar only when it builds a non-pristine
                # source, and a boot right now must hit that cache rather than rebuild.
                # Since v0.2.0 framework continuity adds `.proteus-state` beside the
                # adapter's own `*-state` dir, and iterdir() order is arbitrary — union
                # the glob over every adapter state dir instead of trusting `next()`.
                state_dirs = [d for d in run_root.iterdir()
                              if d.is_dir() and d.name.endswith("-state")
                              and d.name != ".proteus-state"]

                def _dist_tars() -> set:
                    return {t for d in state_dirs for t in d.glob("dist-*.tar")}

                tars_before = _dist_tars()
                check(bool(tars_before),
                      "a rebuild happened during the run (dist cache exists)",
                      f"no dist-*.tar under {[d.name for d in state_dirs]}")
                boot_msg = adapter.check_boot(run_root / "harness")
                tars_after = _dist_tars()
                check(boot_msg == "" and bool(tars_before) and tars_after == tars_before,
                      "booting the final source hits the cache (it was built and "
                      "loaded during the run)",
                      boot_msg or f"new tars: {[t.name for t in tars_after - tars_before]}")
            victims = [v for pat in patterns for v in sorted(src.glob(pat))]
            if victims:
                victim = victims[0]
                original = victim.read_text(encoding="utf-8", errors="replace")
                victim.write_text("const proteusSmokeBroken: string = 42;\n" + original)
                msg = adapter.check_boot(run_root / "harness")
                check("does not boot" in msg, "planted error refused by the boot gate")
                victim.write_text(original)
                check(adapter.check_boot(run_root / "harness") == "",
                      "restore clears the gate")

    print(f"\n{args.harness}: {FAILS} failed checks")
    return FAILS


if __name__ == "__main__":
    raise SystemExit(main())
