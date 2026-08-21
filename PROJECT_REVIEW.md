# Proteus Release Review

Reviewed remote `main` commit: [`20042d17f2c745f8027e19b86b7f2d3d9c4995d0`](https://github.com/proteus-evolve/Proteus/commit/20042d17f2c745f8027e19b86b7f2d3d9c4995d0) (55 commits, fetched 2026-08-19 PDT).

## Verdict

**No-Go for the public v0.1 release yet.** The ordinary CI and package build are healthy, and the new DSH/Pi source-mode design is the right direction. However, the current snapshot/boot behavior can still make the recorded harness differ from the harness that actually runs, and mid-seed resume does not preserve evaluator state. Those are measurement-integrity failures, not cosmetic defects.

## Release blockers

### Source-mode boot does not faithfully materialize deletions or renames

Both source-mode boot wrappers copy `/workspace/src` over the pristine image tree with `cp -R`; they never remove files deleted or renamed by the evolving agent. The source hash concatenates file contents without file paths, so some renames and empty-file changes also retain the pristine hash and skip the rebuild entirely.

- [`environments/dsh-src/boot.sh`](https://github.com/proteus-evolve/Proteus/blob/20042d17f2c745f8027e19b86b7f2d3d9c4995d0/environments/dsh-src/boot.sh#L11-L28)
- [`environments/pi-src/boot.sh`](https://github.com/proteus-evolve/Proteus/blob/20042d17f2c745f8027e19b86b7f2d3d9c4995d0/environments/pi-src/boot.sh#L13-L29)

The snapshot may therefore say a source file was removed while the next episode still runs the baked copy. Fix by materializing an exact clean tree before overlaying the snapshot and hash `relative_path + NUL + content`, not content alone.

### Mid-seed resume resets the experiment's evaluator state

`run(..., start=N)` initializes `eval_history`, visible feedback, `best_score`, and counters from scratch, then overwrites `eval_history.json`. A resumed `accept_reject` run therefore accepts the first post-resume score even when it is below the pre-interruption best.

- [`proteus/core/episode.py`](https://github.com/proteus-evolve/Proteus/blob/20042d17f2c745f8027e19b86b7f2d3d9c4995d0/proteus/core/episode.py#L148-L183)

Reproduction: episode 1 scored `1.0`; after resuming at episode 2, a `0.0` candidate was accepted and `eval_history.json` contained only episode 2. Resume must reload history, last visible feedback, selection baseline, and cumulative counters.

### Nested benchmark repositories remain outside snapshot/rejection semantics

SWE-bench clones a Git repository under `harness/task`. The outer snapshot records it as a gitlink, not its working files. Restoring episode 0 leaves edits inside the nested repository untouched.

- [`proteus/bench/task.py`](https://github.com/proteus-evolve/Proteus/blob/20042d17f2c745f8027e19b86b7f2d3d9c4995d0/proteus/bench/task.py#L31-L64)
- [`proteus/core/snapshot.py`](https://github.com/proteus-evolve/Proteus/blob/20042d17f2c745f8027e19b86b7f2d3d9c4995d0/proteus/core/snapshot.py#L46-L96)

Reproduction: after modifying `task/work.txt` and restoring the episode-0 snapshot, the file still contained the evolved content. The task workspace should live outside the harness snapshot, or be flattened/materialized as ordinary files with its own explicit reset contract.

### SWE-bench cache identity is still constant across episodes

The grader closes over the `episode_tag` passed once to `swe_task()`. `BenchTask.grade` receives only a path, so the caller cannot supply the current `GoalContext.episode`; every episode can reuse the same official `(run_id, instance_id)` cache key.

- [`proteus/bench/swe.py`](https://github.com/proteus-evolve/Proteus/blob/20042d17f2c745f8027e19b86b7f2d3d9c4995d0/proteus/bench/swe.py#L59-L121)

Make the grader episode-aware and include run/seed/episode plus a patch digest in the cache identity.

## Important pre-release fixes

- `proteus check --harness minimal` fails its own protocol check because `MinimalHarness`, `LLMHarness`, and `AkiHarness` do not expose `disposition_in_files`. This makes the advertised adapter compliance command reject three built-ins.
- `overwrite` deletes the run directory but appends to old `seeds.jsonl` and progress files. Reproduction after one overwrite: two seed rows and two episode-1 rows.
- `tests/run_offline.py` still fails at its first `tests.*` import, and neither ordinary CI nor the new release-smoke workflow invokes it.
- `between_within()` still accepts a one-label dataset and returns a meaningless `R=0, p=1` result.
- A `directory` surface counts nested directories as independent units (`alpha` and `alpha/scripts`) instead of hashing the full top-level unit.
- Python 3.10–3.13 users need `zstandard` for DSH traces, but the package declares no DSH/sandbox dependency; release-smoke installs it manually, masking the install gap.
- The new `release-smoke` and `upstream-canary` workflows have never run on GitHub. The ordinary five-version CI is green, but there is no live DSH/Pi source-edit proof yet.

## What passed

- Latest remote `main` fetched and reviewed at the SHA above.
- GitHub ordinary CI passed on Python 3.10–3.14.
- Local pytest: **57 passed**.
- Ruff 0.14.14: **passed**.
- Python, JavaScript, shell, JSON, and HTML syntax checks: **passed**.
- Exact minimal release-smoke cell: **0 failed checks** across two arms and four episodes.
- Clean sdist and wheel build: **passed**; wheel installed into a fresh virtual environment and the CLI/import worked outside the source checkout.
- DSH `dsh-v0.1.0-rc.7` and `dsh-v0.1.0-rc.8` tags and Pi `v0.84.2` tag exist upstream.

## DeepSeek campaign finding

The proposed comparison has a valid exact boundary:

- old system: `dsh-v0.1.0-rc.7` (`99f6f02`, 2026-08-17)
- official new system: `dsh-v0.1.0-rc.8` (`141eb6f`, 2026-08-19)

The rc.8 feature is narrower and more defensible than “DSH became multimodal”: it adds direct image input to a configured `deepseek-official` model route. Durable image references are converted to OpenAI-compatible `image_url` data URLs; PNG/JPEG/WebP/GIF and a 20 MiB request-level payload bound are covered. The shipped catalog still does not advertise the experimental vision model automatically.

For a fair live experiment, evolve rc.7 with a black-box user goal such as “Users should be able to attach images and have the selected DeepSeek model reason over them reliably.” Compare baseline rc.7, Proteus-evolved rc.7, and official rc.8 on identical hidden tests. Before running it, add network egress control: the current DSH container uses host networking and includes web access, so it could read rc.8 source or release notes during evolution.
