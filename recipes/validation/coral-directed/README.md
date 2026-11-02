# CORAL directed qualification

A bounded integration recipe for Proteus's current CORAL worker, knowledge,
intervention, evaluation, and recovery paths. It uses a small model-free
integer-multiplier subject; model responses are structured JSON and never
executed as code.

## Frozen reference

- Source: `cebc1a9e808a25e8c7ee0144f525138325008253`
- Production `proteus/` tree: `0e73363387123ad067df3cfeff684af7d913fed2`
- Live plan SHA256: `bded2fa9e62422e5d224d610b6e252264d847a5585bb7922bfc42c744e611dca`
- [Linux fixture run](https://github.com/proteus-evolve/Proteus/actions/runs/41394652342)
- [Single live run](https://github.com/proteus-evolve/Proteus/actions/runs/41394999162)

These are dedicated qualification jobs. The normal CI and release-smoke matrix
were not executed by these runs.

## Sequence and bounds

Two command-worker processes overlap in time; B waits while A publishes.
Exactly three model slots are available:

1. `a:0`: A makes an initial proposal and publishes its note.
2. `a:1`: A's next generation receives the native controller intervention and
   evaluation feedback in its actual model request.
3. `b:0`: B's actual request includes A's published revision and content bytes.

One evaluator runs at a time. After B's evaluation is durably recorded, the
controller intentionally calls `os._exit(86)`. A distinct controller process
resumes the same workspace. The auditor verifies unchanged model dispatches,
preserved settled evaluation identities, and no duplicate settlement.

The durable broker limits physical dispatch attempts to **3**, with at most
**4,096 output tokens per request**. Provider retries and resending uncertain
attempts are disabled. This cap is not attributed to the native command worker's
otherwise unknown model-call budget.

## Run without a provider

Use Python 3.12 from the frozen checkout and choose fresh directories:

```sh
git clone https://github.com/proteus-evolve/Proteus.git /tmp/proteus-coral-repro
cd /tmp/proteus-coral-repro
git checkout --detach cebc1a9e808a25e8c7ee0144f525138325008253
python3.12 -m venv /tmp/proteus-coral-venv
. /tmp/proteus-coral-venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q tests/test_v1_coral_directed_qualification.py
python tools/validation/coral_directed.py run --mode fixture --out /tmp/coral-example-new
python tools/validation/coral_directed.py audit --out /tmp/coral-example-new
```

The registered `ci.yml` manual entry defaults to `coral_mode=fixture`; it forwards
no provider secret. A separately approved live dispatch requires the exact
reviewed commit and live plan hash. Only the live wrapper passes the named
`DEEPSEEK_API_KEY` secret; it does not inherit other secrets.

## Observed live result

| Slot | Input tokens | Output tokens | HTTP |
| --- | ---: | ---: | ---: |
| `a:0` | 185 | 33 | 200 |
| `a:1` | 458 | 36 | 200 |
| `b:0` | 498 | 44 | 200 |
| Total | 1,141 | 113 | |

Both requested and provider-reported model names were `deepseek-flash`.
All three slots completed; there were no unknown model dispatches. The native
ledger recorded four evaluations, zero remaining active reservations, and three
unknown USD cost entries. The provider reported tokens but no USD charge, so
monetary cost remains **unknown**.

## Evidence and scope

The artifact retains exact request bodies, raw HTTP responses, transport receipts,
source/plan witnesses, controller exit/resume observations, and native records.
Exported gateway credentials are removed; audit copies are explicitly
non-resumable. The saved-record auditor makes no provider calls, but native
saved-run reads can create empty local advisory lock files. A derived native
report's hash includes its `source_run` path, so relocating an audit copy changes
that report hash while leaving the request-level checks unchanged.

This verifies the directed mechanism, not CORAL benchmark performance, historical
v5.1's missing request records, worker-crash recovery, native model-session
continuation, simultaneous provider requests, or a causal improvement claim.
The controlled subject's submission scores of 0 and 1 are integration checks.

## Archived readback

The [original live artifact](../../../docs/validation/v1-20261101/coral-directed/live-41394999162.zip)
is retained unchanged (SHA256 `13f28f94be0918f1e49604a3ce9536fed55bc8c1abaf88c293f923c812100eb4`).
It contains the live and no-model fixture records from the same workflow execution.
The [evidence index](../../../docs/validation/v1-20261101/coral-directed/README.md)
links the original failure, review, and acceptance records.

From the preparation checkout with the frozen commit available, read the archive
without starting workers or contacting a provider:

```sh
unzip docs/validation/v1-20261101/coral-directed/live-41394999162.zip -d /tmp/coral-records-readback
python tools/validation/coral_directed.py audit --out /tmp/coral-records-readback/live
```
