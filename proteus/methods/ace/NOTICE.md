# ACE playbook reference

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
This component does not implement the full ACE training method or reproduce its
published experimental results.
