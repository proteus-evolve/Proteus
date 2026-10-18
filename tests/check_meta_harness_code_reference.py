"""Pinned memory classes: exact source, managed callback, prompts/state comparison.

Only the SDK-bearing llm module is reduced to its actual LLMCallable protocol.
This exercises real upstream memory classes, not a live benchmark or full method.
"""

import ast
import hashlib
import importlib
import sys
from pathlib import Path

from proteus.artifacts.codecs import FileContent
from proteus.core.contracts import OperationStatus, Outcome, PluginRef, RuntimeIdentity, Usage
from proteus.core.methods import ActorResult
from proteus.core.serialization import canonical_json, content_hash
from proteus.execution.code_records import CodeCall, CodeOptions, CodeState
from proteus.execution.code_rpc import invoke

REVISION = '8123ccabe2b19fa1123090b2e5f1bacc20963ce8'
HASHES = {
    'llm.py': '7d2d4e46771b5b7f6d99a124a38658815bd589ae8be56df7a4daedd995212c2f',
    'memory_system.py': '0128a4f1eb20352bcdb932f93e8c541dc16c6dd8e0de08e4abf489f44f21a0b7',
    'agents/no_memory.py': '720c8b09691541102549749e17decc85ce779fcf190915c07a511e5958e8e562',
    'agents/fewshot_memory.py': 'daa346dbae5a83ffd58c6507eca99cb9e718f1e406be80d95b2fa72d90576240',
    'agents/fewshot_all.py': '3369cf4571f8719b0748b9740688f8b050d96d68c10048432bb54249e6051cb3',
}


def reference_files(source):
    root = Path(source) / 'reference_examples/text_classification'
    files = {}
    for name, digest in HASHES.items():
        raw = (root/name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest, name
        files['meta_reference_fixture/'+name] = FileContent(raw)
    # Extract the real Protocol declaration without importing provider SDKs.
    raw = files['meta_reference_fixture/llm.py'].data.decode()
    protocol = next(node for node in ast.parse(raw).body if isinstance(node, ast.ClassDef) and node.name == 'LLMCallable')
    files['meta_reference_fixture/llm.py'] = FileContent(('from typing import Protocol\n'+ast.unparse(protocol)+'\n').encode())
    for name in ('__init__.py', 'agents/__init__.py'):
        files['meta_reference_fixture/'+name] = FileContent(b'')
    return files


def check(source, root, baseline):
    root = Path(root)
    files = reference_files(source)
    root.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.data)
    sys.path.insert(0, str(root))
    prompts = []
    response = canonical_json({'reasoning': 'fixture', 'final_answer': 'label'})
    def direct(prompt):
        prompts.append(prompt)
        return response
    try:
        module = importlib.import_module('meta_reference_fixture.agents.'+baseline)
        factory = module.NoMemory if baseline == 'no_memory' else module.FewShotAll
        original = factory(direct)
        rows = [{'input': 'context '+str(i), 'raw_question': 'question '+str(i),
                 'prediction': str(i), 'ground_truth': str(i), 'was_correct': True} for i in range(5)]
        original.learn_from_batch(rows)
        expected_state = original.get_state()
        expected = original.predict('query')
        options = CodeOptions(module='meta_reference_fixture.agents.'+baseline)
        state = CodeState(artifact_hash=content_hash(baseline))
        def actor(prompt, *, timeout_seconds):
            return ActorResult(text=response, runtime=RuntimeIdentity(runtime=PluginRef(name='fixture.actor', version='1')),
                usage=Usage(model_calls=1), status=OperationStatus.COMPLETED)
        learned = invoke(root, options, CodeCall(method='learn_from_batch', state=state, args=(rows,)), actor,
                         timeout_seconds=5)
        assert learned.status == Outcome.SUCCEEDED and learned.state == expected_state, learned.diagnostic
        assert not any(event['kind']=='model' for event in learned.trace.events)
        state = CodeState(artifact_hash=state.artifact_hash, initialized=True, value=learned.state)
        predicted = invoke(root, options, CodeCall(method='predict', state=state, args=('query',)), actor, timeout_seconds=5)
        assert predicted.status == Outcome.SUCCEEDED, predicted.diagnostic
        assert canonical_json(predicted.output) == canonical_json(expected)
        assert predicted.state == original.get_state()
        calls = [event for event in predicted.trace.events if event['kind']=='model']
        assert [event['prompt'] for event in calls] == prompts and len(calls)==1
        return expected_state
    finally:
        sys.path.remove(str(root))
        for name in tuple(sys.modules):
            if name == 'meta_reference_fixture' or name.startswith('meta_reference_fixture.'):
                del sys.modules[name]
