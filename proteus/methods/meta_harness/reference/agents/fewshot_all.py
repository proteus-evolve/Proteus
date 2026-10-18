"""Few-Shot Memory using ALL training examples (no cap)."""

from ..llm import LLMCallable
from .fewshot_memory import FewShotMemory


class FewShotAll(FewShotMemory):
    """Few-shot baseline using all training examples, 50k token limit."""

    def __init__(self, llm: LLMCallable):
        super().__init__(llm, max_examples=9999)
