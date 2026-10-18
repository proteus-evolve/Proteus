# Combee aggregation reference sources

The `_gepa` package contains three MIT-licensed source files from
[gepa-ai/gepa](https://github.com/gepa-ai/gepa), revision
`f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819`. Copyright (c) 2025 Lakshya A
Agrawal and the GEPA contributors. The full license is `LICENSE-GEPA.txt`.
`reference.json` records original paths, byte hashes and the three import
relocations. No executable algorithm or prompt bodies were changed.

These files supply ComBEE, its instruction renderer/parser and signature types.
They use the separately verified optional GEPA runtime already pinned by Proteus
at `8b0ce6cd99a234f6b74daf37558a2ac0ce18f975`. The base GEPA optimizer is not
upgraded. `tests/combee_reference.py` verifies byte equivalence after import
relocation and runs the actual newer upstream package independently to compare
prompts, raw outputs, parsed proposals, wave order and RNG state.

The ACE operator uses the Apache-2.0 sources and license under
`proteus/methods/ace`, revision `82709de050e1db6e6ef2f07bcb0393560b94992a`.
The exact `CURATOR_OPERATIONS_AGGREGATION_PROMPT` is added to that package's
`prompts.py`. The reference checker executes the actual `ACEBatch._train_batch`
with fixture generation and actual curator methods to compare the complete
aggregation phase. See `docs/V1_COMBEE_AGGREGATION.md` for adaptations and scope.
