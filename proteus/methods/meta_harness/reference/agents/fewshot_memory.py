"""Few-Shot Memory - baseline using all training examples as demonstrations.

Accumulates (raw_question, ground_truth) pairs during training, uses them as
few-shot context at test time. Uses raw questions (not full preprocessed prompts)
to avoid repeating task context per example — matching MCE reproduce behavior.

Variant:
- FewShotAll: Up to 9999 examples
Uses a global max_chars=50000 cap (matching MCE reproduce).
"""

import hashlib
import json
import random
from typing import Any

from ..llm import LLMCallable
from ..memory_system import MemorySystem, extract_json_field

PROMPT_TEMPLATE = """Solve the problem below based on the examples provided.

{examples_section}

**Problem:**
{input}

**Instructions:**
- Follow the patterns shown in the examples above
- Respond in JSON format

{{"reasoning": "[your reasoning]", "final_answer": "[your answer]"}}"""

MAX_CHARS = 30000


def _seed_for_input(input: str) -> int:
    """Return a stable per-input seed across Python processes."""
    return int.from_bytes(hashlib.sha256(input.encode()).digest()[:8], "big")


class FewShotMemory(MemorySystem):
    """Few-shot baseline - accumulate all examples, use as demonstrations."""

    def __init__(
        self,
        llm: LLMCallable,
        max_examples: int = 50,
    ):
        super().__init__(llm)
        self.max_examples = max_examples
        self.examples: list[dict[str, str]] = []

    def _format_examples_section(self, seed: int | None = None) -> str:
        """Format examples as few-shot demonstrations, respecting global char limit.

        Uses raw_question (short Q/A) when available to avoid repeating task
        context per example. Falls back to full input for non-MCE datasets.

        Args:
            seed: If provided, randomly sample max_examples and shuffle order.
                  Each eval instance should pass a different seed for diversity.
        """
        if not self.examples:
            return ""

        if seed is not None and len(self.examples) > self.max_examples:
            rng = random.Random(seed)
            to_use = rng.sample(self.examples, self.max_examples)
        else:
            to_use = self.examples[-self.max_examples :]
            if seed is not None:
                rng = random.Random(seed)
                to_use = list(to_use)
                rng.shuffle(to_use)

        parts = []
        total_chars = 0
        for ex in to_use:
            # Use raw_question for demos (avoids repeating task context per example)
            question = ex.get("raw_question", ex["input"])
            part = f"Q: {question}\nA: {ex['target']}"
            if total_chars + len(part) > MAX_CHARS:
                break
            parts.append(part)
            total_chars += len(part) + 2  # \n\n separator

        return "\n\n".join(parts)

    def predict(self, input: str) -> tuple[str, dict[str, Any]]:
        """Generate prediction using accumulated few-shot examples."""
        seed = _seed_for_input(input)
        examples_section = self._format_examples_section(seed=seed)
        prompt = PROMPT_TEMPLATE.format(
            examples_section=examples_section,
            input=input,
        )

        response = self.call_llm(prompt)
        answer = extract_json_field(response, "final_answer")

        return answer, {
            "full_response": response,
            "num_examples": len(self.examples),
        }

    def learn_from_batch(self, batch_results: list[dict[str, Any]]) -> None:
        """Accumulate all examples with ground truth labels."""
        for r in batch_results:
            ex = {"input": r["input"], "target": r["ground_truth"]}
            if "raw_question" in r:
                ex["raw_question"] = r["raw_question"]
            self.examples.append(ex)

    def get_context_length(self) -> int:
        """Return length of the examples section actually injected per query."""
        return len(self._format_examples_section())

    def get_state(self) -> str:
        """Serialize memory state."""
        return json.dumps({"examples": self.examples}, indent=2)

    def set_state(self, state: str) -> None:
        """Restore memory state from JSON."""
        data = json.loads(state)
        self.examples = data.get("examples", [])
