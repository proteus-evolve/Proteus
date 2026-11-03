"""Composition bridge copied into each candidate artifact as candidate.py.

It preserves the upstream instance's internal method dispatch and captures the
prompt metadata in the same process as prediction. No provider SDK is imported.
"""

import importlib
import inspect
import json
from pathlib import Path


class Candidate:
    def __init__(self, model):
        from text_classification.memory_system import MemorySystem
        self.name = json.loads(Path('selected.json').read_text())['system']
        module = importlib.import_module('text_classification.agents.' + self.name)
        factory = next((value for _, value in inspect.getmembers(module, inspect.isclass)
                        if issubclass(value, MemorySystem) and value is not MemorySystem), None)
        if factory is None:
            raise ValueError('no MemorySystem subclass in candidate module')
        self.memory = factory(llm=model)

    def inspect(self):
        return {'system': self.name}

    def predict(self, value):
        answer, metadata = self.memory.predict(value)
        return answer, {'upstream_metadata': metadata, 'prompt_info': self.memory.get_last_prompt_info()}

    def learn_from_batch(self, rows):
        return self.memory.learn_from_batch(rows)

    def get_state(self):
        return self.memory.get_state()

    def set_state(self, state):
        return self.memory.set_state(state)
