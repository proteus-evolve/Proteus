# CORAL heartbeat reference

`_reference_heartbeat.py` and the three `*.reference.md` prompts are unmodified
copies from CORAL commit `0123dfb939b35228cf2c1fde224cd0561e727408`.
They are distributed under the accompanying Apache-2.0 `LICENSE-CORAL.txt`.
The source files are `coral/agent/heartbeat.py` and
`coral/hub/prompts/{reflect,consolidate,pivot}.md`.

The `coral.heartbeat` policy calls this implementation. Its `reference_manager`
profile constructs a new runner at every formal observation, matching the pinned
manager's `_get_heartbeat_runner` lifecycle. `persistent_cooldown` retains the
runner's cooldown state and is an explicit extension. Neither profile changes the
reference function's treatment of leading missing scores or its strict epsilon
comparison.

Proteus observes every processed GatewayResult in controller order. The upstream
manager coalesces simultaneous scored files to the latest attempt for each check;
that scheduling behavior is not reproduced by this adapter. Failed gateway
operations count as evaluations but do not enter the score history; completed
cohorts with missing rows enter it as `None`. This is an explicit mapping, not a
claim of equality to all CORAL budget classes or grader failure modes.

Prompt text must be supplied by the recipe. The preserved reference prompts use
CORAL CLI commands and filesystem layout and are reference material, not directly
executable instructions for Proteus workers. Adapted prompts and the complete
single-island CORAL recipe require their own validation.
