"""The upstream callable protocol; provider transport is managed by Proteus."""
from typing import Protocol

class LLMCallable(Protocol):
    """Protocol for LLM call functions."""

    def __call__(self, prompt: str) -> str:
        ...
