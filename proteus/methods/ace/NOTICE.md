# ACE reference sources

`upstream.py` contains selected functions from [ace-agent/ace](https://github.com/ace-agent/ace),
revision `82709de050e1db6e6ef2f07bcb0393560b94992a`, distributed under Apache-2.0.
The complete upstream license is included as `LICENSE-ACE.txt`.

Sources: `playbook_utils.py` and `utils.py:get_section_slug`. Executable function
bodies are unchanged; trailing whitespace (including in docstrings) is removed.
Imports are limited to `re`, and the new module-local `print` suppresses
diagnostic output without changing global stdout. No model clients or embedding
dependencies are imported. `playbook.py` adds Proteus contracts, validation and
artifact encoding around these functions; its empty template follows
`ace/ace.py:ACE._initialize_empty_playbook` at the same revision.

`tests/check_ace_reference.py` verifies the upstream source hashes, selected
function ASTs, initial template and behavioral conformance against that checkout.
`parsing.py` also contains the selected `utils.py` answer parsers,
`playbook_utils.py:extract_json_from_text`, Generator bullet-ID parsers,
Reflector tag parser and Curator operation parser from `ace/core/`. Their
executable bodies are unchanged, with the same whitespace and local diagnostic
normalization. `prompts.py` contains the exact five Generator, Reflector and
Curator prompt strings from `ace/prompts/`, including their internal whitespace.

`roles.py` and `method.py` implement managed roles and offline/online control flow.
`tests/ace_reference_method.py` verifies the additional source hashes, parser ASTs
and prompt strings and executes the pinned upstream control flow for independent
conformance tests. Managed transport, stricter artifact validation and serial
execution are declared adaptations; this does not reproduce published model
performance. See `docs/V1_ACE_METHOD.md` for the supported profile and limits.

`proteus/benchmarks/ace_formula_reference.py` contains the Formula question parser
and exact correctness predicate from `eval/finance/data_processor.py` at the same
revision, with only trailing whitespace removed. The complete license above also
applies to these selected functions. `tests/check_ace_formula_reference.py`
verifies their ASTs against the upstream file and checks all 1000 source records.
The Formula dataset is fetched separately by hash; its bytes are not redistributed
in the Proteus package. See `docs/V1_ACE_FORMULA.md` for provenance and scope.
