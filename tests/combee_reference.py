"""Execute actual pinned reference operators, substituting only model transport."""

import __future__
import ast
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from types import SimpleNamespace

from ace_reference_method import ROLE_HASHES
from check_ace_reference import reference
from proteus.methods.ace import prompts
from proteus.methods.ace.playbook import EMPTY_PLAYBOOK
from proteus.operators import combee


def ace(source, samples, responder, *, factor=2, num_groups=None, seed=42):
    """Real ACEBatch phase 2; phase-1 records and post-generation are fixtures."""
    source = Path(source)
    namespace = reference(source)
    batch = (source / "ace/ace_batch.py").read_bytes()
    assert (
        hashlib.sha256(batch).hexdigest()
        == "18e4bf974ff17383997ba28b715d0a99e83b5ea2e5d49a2fb91b4b000c0874fd"
    )
    for name, digest in ROLE_HASHES.items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest
    nodes = []
    tree = ast.parse((source / "playbook_utils.py").read_text())
    nodes.extend(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "extract_json_from_text"
    )
    tree = ast.parse((source / "ace/prompts/curator.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            assert ast.literal_eval(node.value) == getattr(prompts, name)
            nodes.append(node)
    tree = ast.parse((source / "ace/core/curator.py").read_text())
    curator = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Curator"
    )
    names = {
        "propose_operations",
        "aggregate_operations",
        "_add_operations_only",
        "_extract_and_validate_operations",
    }
    curator.body = [
        node for node in curator.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert len(curator.body) == len(names)
    nodes.append(curator)
    cls = next(
        node
        for node in ast.parse(batch).body
        if isinstance(node, ast.ClassDef) and node.name == "ACEBatch"
    )
    cls.body = [
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_train_batch"
    ]
    assert len(cls.body) == 1
    nodes.append(cls)
    calls = []

    def transport(client, provider, model, prompt, **kwargs):
        calls.append((kwargs["call_id"], prompt))
        return responder(prompt), {}

    rng = random.Random(seed)
    namespace.update(
        json=json,
        Path=Path,
        random=rng,
        count_tokens=len,
        timed_llm_call=transport,
        ThreadPoolExecutor=ThreadPoolExecutor,
        as_completed=as_completed,
        INCORRECT_DUE_TO_API_ERROR="api-error",
        is_api_error=lambda exc: False,
        extract_answer=lambda text: "1",
        log_curator_failure=lambda *a, **k: None,
        log_curator_operation_diff=lambda *a, **k: None,
    )
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]),
            str(source / "<actual-combee-ace>"),
            "exec",
            flags=__future__.annotations.compiler_flag,
        ),
        namespace,
    )
    engine = namespace["ACEBatch"]()
    engine.playbook, engine.next_global_id, engine.use_bulletpoint_analyzer = (
        EMPTY_PLAYBOOK,
        1,
        False,
    )
    engine.curator = namespace["Curator"]()
    engine.curator.api_client, engine.curator.api_provider = None, "fixture"
    engine.curator.model, engine.curator.max_tokens = "fixture", 1000
    engine.generator = SimpleNamespace(generate=lambda **kwargs: ("answer", None, None))
    engine._generate_and_reflect_single_sample = lambda task_dict, **kwargs: {
        "is_correct": False,
        "pre_train_answer": "0",
        "all_bullet_tags": [],
        "reflection_content": f"reflection {task_dict['i']}",
        "context": f"context {task_dict['i']}",
        "question": "q",
        "target": "1",
        "step_id": str(task_dict["i"]),
        "tracking_dict": {},
    }
    engine._train_batch(
        [{"i": i} for i in range(samples)],
        SimpleNamespace(answer_is_correct=lambda a, b: a == b),
        1,
        1,
        None,
        None,
        {
            "token_budget": 80000,
            "use_json_mode": False,
            "no_ground_truth": False,
            "augmented_shuffling_factor": factor,
            "curator_num_groups": num_groups,
        },
        samples,
    )
    ordered = sorted(
        (call for call in calls if "_group_" in call[0]),
        key=lambda call: int(call[0].rsplit("_", 1)[1]),
    )
    ordered.extend(call for call in calls if call[0].endswith("_reducer"))
    return [prompt for _, prompt in ordered], engine.playbook, engine.next_global_id, rng.getstate()


GEPA_WORKER = r"""
import hashlib,json,random,sys
from gepa.proposer.reflective_mutation.combee import ComBEEReflectionLM
from gepa.lm import LMOutput
data=json.loads(sys.stdin.read())
calls=[]
class LM:
    def __call__(self,prompt):
        calls.append([prompt])
        return self.answer(prompt)
    def answer(self,prompt):
        digest=hashlib.sha256(json.dumps(prompt,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        value=data['responses'].get('sha256:'+digest,data.get('default_response'))
        return LMOutput(value if value is not None else '```instruction '+digest[:12]+'```',finish_reason=data.get('finish_reason'))
    def batch_complete(self,messages):
        prompts=[message[0]['content'] for message in messages]
        calls.append(prompts)
        return [self.answer(prompt) for prompt in prompts]
rng=random.Random(data['seed'])
strategy=ComBEEReflectionLM(LM(),rng=rng,duplication_factor=data['factor'],batch_reflection=data['batch'])
outputs=[]
for _ in range(data['repeat']):
    candidate={name:'base '+name for name in data['components']}
    records=[{'trace':'reflection '+str(i),'answer':str(i)} for i in range(data['samples'])]
    proposal,_=strategy.reflect(candidate,{name:records for name in candidate},data['components'])
    outputs.append(vars(proposal))
print(json.dumps({'outputs':outputs,'calls':calls,'rng':rng.getstate()}))
"""


def gepa(source, **options):
    source = Path(source)
    root = Path(combee.__file__).parent
    manifest = json.loads((root / "reference.json").read_text())
    for name, entry in manifest["files"].items():
        raw = (source / entry["source"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        expected = raw.decode()
        for before, after in entry["relocations"].items():
            expected = expected.replace(before, after)
        assert (root / "_gepa" / name).read_text() == expected
    assert (root / "LICENSE-GEPA.txt").read_bytes() == (source / "LICENSE").read_bytes()
    data = dict(
        samples=9,
        components=["instruction"],
        factor=2,
        seed=42,
        batch=True,
        repeat=1,
        responses={},
        default_response=None,
    )
    data.update(options)
    result = subprocess.run(
        [sys.executable, "-c", GEPA_WORKER],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(source / "src")},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)
