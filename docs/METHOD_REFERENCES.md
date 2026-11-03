# Upstream methods, papers and code attribution

The Proteus 1.0 integrations and the [frozen experiment recipes](../recipes/README.md)
build on the methods and software below. Cite the relevant original papers when
describing these methods or reporting their adapted results, in addition to
[citing Proteus](../CITATION.cff). Repository links identify software provenance;
they do not replace academic citations. Source licenses and notices remain applicable.

The paper versions and source links below were checked on **2026-10-28**.
Paper versions identify the reference text; code commits identify the upstream
implementation used by the integration. They are separate identities and do not
imply that the experiment matches that paper's complete setup.

## Papers and official repositories

Full-author BibTeX entries are available in [references.bib](../references.bib).
The five paper entries use the official arXiv BibTeX exports, including their
exported years; GEPA and ACE first appeared in 2025 and were revised in 2026.

| Method | Original paper and checked version | Official code | BibTeX key |
| --- | --- | --- | --- |
| GEPA | Agrawal et al., [GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning](https://arxiv.org/abs/2507.19457v2), v2 | [gepa-ai/gepa](https://github.com/gepa-ai/gepa) | `agrawal2026gepareflectivepromptevolution` |
| ACE | Zhang et al., [Agentic Context Engineering: Evolving Contexts for Self-Improving Language Models](https://arxiv.org/abs/2510.04618v3), v3 | [ace-agent/ace](https://github.com/ace-agent/ace) | `zhang2026agenticcontextengineeringevolving` |
| Combee | Li et al., [Combee: Scaling Prompt Learning for Self-Improving Language Model Agents](https://arxiv.org/abs/2604.04247v2), v2 | [gepa-ai/gepa](https://github.com/gepa-ai/gepa), including its [Combee guide](https://gepa-ai.github.io/gepa/guides/combee/); our Formula integration also uses [ace-agent/ace](https://github.com/ace-agent/ace) | `li2026combeescalingpromptlearning`; also cite ACE for Combee/Formula |
| Meta-Harness | Lee et al., [Meta-Harness: End-to-End Optimization of Model Harnesses](https://arxiv.org/abs/2603.28052v1), v1 | [stanford-iris-lab/meta-harness](https://github.com/stanford-iris-lab/meta-harness) | `lee2026metaharnessendtoendoptimizationmodel` |
| CORAL | Qu et al., [CORAL: Towards Autonomous Multi-Agent Evolution for Open-Ended Discovery](https://arxiv.org/abs/2604.01658v3), v3 | [Human-Agent-Society/CORAL](https://github.com/Human-Agent-Society/CORAL) | `qu2026coralautonomousmultiagentevolution` |

## Exact code sources and adaptation boundaries

These are upstream method/software pins, distinct from the Proteus core revisions
reconstructed by the [frozen source archive](../recipes/experiments/single-seed-20261020/README.md#reconstruct-the-source-that-ran).

| Integration | Pinned upstream source | Reuse, attribution and experiment boundary |
| --- | --- | --- |
| GEPA / AIME | [GEPA `8b0ce6cd99a234f6b74daf37558a2ac0ce18f975`](https://github.com/gepa-ai/gepa/tree/8b0ce6cd99a234f6b74daf37558a2ac0ce18f975) | Pinned optimizer runtime; [source verification](../proteus/methods/gepa/reference.py). MIT upstream. Our profile uses single mutation and an instance frontier without merge/cache, a different model/transport and a 500 metric-call budget. The seed-0 result is an adapted study, not matched paper reproduction. |
| ACE / Formula | [ACE `82709de050e1db6e6ef2f07bcb0393560b94992a`](https://github.com/ace-agent/ace/tree/82709de050e1db6e6ef2f07bcb0393560b94992a) | Reuses playbook functions, role prompts, parsers and Formula scoring/data provenance. [Source notice](../proteus/methods/ace/NOTICE.md), [Apache-2.0 license](../proteus/methods/ace/LICENSE-ACE.txt). Our one-epoch, three-refinement-round runs use managed transport and a different model; offline selected and online stream scores have different meanings. |
| Combee / Formula | [GEPA aggregation `f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819`](https://github.com/gepa-ai/gepa/blob/f1cfdee2856e9dc4b5b4dc5cc2e391d0aa20d819/src/gepa/proposer/reflective_mutation/combee.py), [dynamic-sampler proposal `f9e2d430815e5a0b36d58d1dab2f36f8082759e4`](https://github.com/gepa-ai/gepa/blob/f9e2d430815e5a0b36d58d1dab2f36f8082759e4/src/gepa/strategies/batch_sampler.py), and ACE `82709de…` above | Combee aggregation is sourced from GEPA, with ACE prompts/reducer for the Formula adaptation. The dynamic controller uses a separate proposal revision, not a released GEPA dependency upgrade. [Source notice](../proteus/operators/combee/NOTICE.md), [MIT license](../proteus/operators/combee/LICENSE-GEPA.txt), plus ACE's Apache-2.0 notice. Eight conditions use eight workers in parallel arms; shared resources, continuations and observed batch sizes prevent a claim of reproducing the paper's speedup. |
| Meta-Harness / MCE | [Meta-Harness `8123ccabe2b19fa1123090b2e5f1bacc20963ce8`](https://github.com/stanford-iris-lab/meta-harness/tree/8123ccabe2b19fa1123090b2e5f1bacc20963ce8) | Reuses reference memory classes, three MCE datasets, evaluators and proposer protocol. [Source notice](../proteus/methods/meta_harness/NOTICE.md), [MIT license](../proteus/methods/meta_harness/LICENSE-META-HARNESS.txt). The frozen run uses one JSON proposal call producing three programs, instead of the paper's long coding-agent search. It is a Meta-Harness-inspired MCE study, not a Terminal-Bench paper replication. |
| CORAL / circle packing | [CORAL `0123dfb939b35228cf2c1fde224cd0561e727408`](https://github.com/Human-Agent-Society/CORAL/tree/0123dfb939b35228cf2c1fde224cd0561e727408) | Reuses heartbeat policy/prompts and the 26-circle task/seed/grader. [Method notice](../proteus/methods/coral/NOTICE.md), [task notice](../proteus/benchmarks/coral_circle_packing/NOTICE.md), [Apache-2.0 license](../proteus/methods/coral/LICENSE-CORAL.txt). Frozen arms use bounded JSON workers with 20 proposals each and one evaluation worker. They do not reproduce the original persistent native agents; confirmation uses the same public task rather than a held-out task. |

See each [family's recipe guide](../recipes/README.md) for the actual settings,
launch scripts, selected results and limitations. An upstream paper result must
be labeled as such when shown beside a Proteus result; citation alone does not
establish matched models, data splits, budgets, selection or runtime.

## Native Pi software credit

The frozen Native Pi integration uses **Pi Agent Harness v0.85.1**, by Mario
Zechner and contributors: [official repository](https://github.com/earendil-works/pi),
[exact revision `d981de1229ef899957bbe968bc8dcda02a21f477`](https://github.com/earendil-works/pi/tree/d981de1229ef899957bbe968bc8dcda02a21f477),
and [upstream MIT license](https://github.com/earendil-works/pi/blob/d981de1229ef899957bbe968bc8dcda02a21f477/LICENSE).
The BibTeX software entry is `zechner_pi_0851`.

This credits the software used in our [source-activation experiment](../recipes/native/pi/README.md).
It is not an independent academic method citation or a claim of quality improvement.
The older Pi v0.84.2 adapter example is a separate configuration.

## Reporting checklist

When adding a method to a report, website, slide deck or recipe:

1. Link its original paper and official code repository at first introduction.
2. Cite the paper using the corresponding entry in `references.bib`; for
   Combee/Formula, include the underlying ACE paper too.
3. Record the exact code commit, actual recipe and any reused components with
   their source notices. Credit Pi as software for the Native integration.
4. Describe setup deviations before comparing results; distinguish an adapted
   experiment from a matched reproduction and distinguish selected from last candidates.

Proteus's own citation remains in [CITATION.cff](../CITATION.cff). No public
Proteus paper is asserted by this upstream reference list.
