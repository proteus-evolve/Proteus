"""NoMemory baseline - no learning, direct prompting."""

from typing import Any

from ..llm import LLMCallable
from ..memory_system import MemorySystem, extract_json_field

PROMPT = """Answer the following question.

{input}

**Answer in this exact JSON format:**
{{
  "reasoning": "[Your chain of thought / reasoning process]",
  "final_answer": "[Your concise final answer here]"
}}
"""


class NoMemory(MemorySystem):
    """Baseline that does not learn - just prompts the LLM directly."""

    def __init__(self, llm: LLMCallable):
        super().__init__(llm)
        self._state = "{}"

    def predict(self, input: str) -> tuple[str, dict[str, Any]]:
        response = self.call_llm(PROMPT.format(input=input))
        answer = extract_json_field(response, "final_answer")
        return answer, {"full_response": response}

    def learn_from_batch(self, batch_results: list[dict[str, Any]]) -> None:
        """No learning - this baseline ignores all feedback."""
        pass

    def get_state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        self._state = state
