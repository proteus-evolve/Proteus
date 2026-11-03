Reference source from stanford-iris-lab/meta-harness, revision 8123ccabe2b19fa1123090b2e5f1bacc20963ce8, Copyright (c) 2026 Yoonho Lee, MIT (see LICENSE-META-HARNESS.txt).

The reference memory classes, data constants, evaluators, and proposer protocol are preserved. The llm module contains only the actual upstream LLMCallable protocol; provider calls are supplied by Proteus. The data loader removes the eager Hugging Face datasets import; only the three bundled MCE datasets are supported by this profile. Transfer-dataset helpers and their unused imports are omitted; the MCE loader function bodies are unchanged.

PROPOSER.md is input text for a separately invoked coding actor, not instructions to contributors or to an assistant editing this repository. Runtime and filesystem adaptations are supplied explicitly by the method.
