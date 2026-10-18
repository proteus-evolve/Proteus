"""Abstract interface for memory systems."""

import hashlib
import json
import re
import threading
from abc import ABC, abstractmethod
from typing import Any

from .llm import LLMCallable


def extract_json_field(text: str, field: str, default: str = "") -> str:
    """Helper function to extract a field from JSON in LLM response."""
    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return str(data.get(field, default))
    except json.JSONDecodeError:
        pass

    # Try code blocks: ```json ... ``` or ``` ... ```
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                return str(data.get(field, default))
        except json.JSONDecodeError:
            pass

    # Find balanced braces and try parsing
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth, pos, in_str = 1, start + 1, False
        while pos < len(text) and depth > 0:
            c = text[pos]
            if c == '"' and (pos == 0 or text[pos - 1] != "\\"):
                in_str = not in_str
            elif not in_str:
                depth += 1 if c == "{" else (-1 if c == "}" else 0)
            pos += 1
        if depth == 0:
            candidate = text[start:pos]
            candidate = re.sub(
                r",\s*([\]}])", r"\1", candidate
            )  # Remove trailing commas
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return str(data.get(field, default))
            except json.JSONDecodeError:
                pass

    # Regex fallback
    match = re.findall(rf'"{field}"\s*:\s*"([^"]*)"', text)
    return match[-1] if match else default


class MemorySystem(ABC):
    """Memory system interface for online and offline learning.

    Args:
        llm: Callable that takes a prompt string and returns a response string.
    """

    def __init__(self, llm: LLMCallable):
        self._llm = llm
        self._prompt_local = threading.local()

    def call_llm(self, prompt: str) -> str:
        """Call the LLM with a prompt. Tracks last prompt length/hash per thread."""
        self._prompt_local.last_prompt_len = len(prompt)
        self._prompt_local.last_prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[
            :8
        ]
        self._prompt_local.last_prompt_text = prompt
        return self._llm(prompt)

    def get_last_prompt_info(self) -> dict[str, Any]:
        """Return length, hash, and full text of the last prompt sent via call_llm (thread-local)."""
        return {
            "prompt_len": getattr(self._prompt_local, "last_prompt_len", None),
            "prompt_hash": getattr(self._prompt_local, "last_prompt_hash", None),
            "prompt_text": getattr(self._prompt_local, "last_prompt_text", None),
        }

    @abstractmethod
    def predict(self, input: str) -> tuple[str, dict[str, Any]]:
        """Generate prediction BEFORE seeing ground truth. Returns (answer, metadata)."""
        pass

    @abstractmethod
    def learn_from_batch(self, batch_results: list[dict[str, Any]]) -> None:
        """Learn from a batch of evaluation results.

        Args:
            batch_results: List of dicts, each containing:
                - input: str
                - prediction: str
                - ground_truth: str
                - was_correct: bool
                - metadata: dict (optional)

        This is called AFTER all predictions in the batch are complete.
        The memory system can analyze patterns across the batch.
        """
        pass

    def get_context_length(self) -> int:
        """Return the character length of context actually injected per query.

        Override in subclasses where the injected context differs from stored state
        (e.g., fewshot memories that store all examples but only inject N).
        """
        return len(self.get_state())

    @abstractmethod
    def get_state(self) -> str:
        """Return serializable state for checkpointing."""
        pass

    @abstractmethod
    def set_state(self, state: str) -> None:
        """Restore state from serialized representation."""
        pass
