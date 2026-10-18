"""Execute the actual pinned ACE algorithm/roles without importing its SDKs.

Only transport, token-count telemetry and logging are substituted. The run,
offline/online loops, per-sample algorithm, evaluation and role formatting/parsing
execute unchanged ASTs from verified upstream files, independently of Proteus.
"""

import __future__
import ast
import copy
import hashlib
import inspect
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from check_ace_reference import function_ast, reference
from proteus.methods.ace import parsing, prompts
from proteus.methods.ace.playbook import EMPTY_PLAYBOOK


ROLE_HASHES = {
    "ace/core/generator.py": "4b750a59752bd5bdb24b2f27b80938844d8a7b5d89e86d7d6bb726f3685c25f5",
    "ace/core/reflector.py": "fe5ae8fb006fd16fdef7b8e7ad38dc746195a4bf0a366a82e69c473c9982b811",
    "ace/core/curator.py": "a22f7f233761acfb00852914d78112a776274c55bc022738af7defca95897617",
    "ace/prompts/generator.py": "b8b12d4a6281ea4c422a2ee0bd919b50580d388027917e8740f82f97eb24eebb",
    "ace/prompts/reflector.py": "72b343ce2781531dac2fd765543410b6abfc4817c39078dd4f83187c4d217e9c",
    "ace/prompts/curator.py": "eb03d59b3f3593832f730a798199b581ad797acd503c47d1737616c68cf26107",
}


def load_engine(source, responder):
    source = Path(source)
    namespace = reference(source)
    trees = {}
    for name, expected in ROLE_HASHES.items():
        raw = (source / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected, f"ACE source differs: {name}"
        trees[name] = ast.parse(raw.decode())
    nodes = []
    for name in ("utils.py", "playbook_utils.py"):
        tree = ast.parse((source / name).read_text())
        wanted = ("extract_answer", "extract_boxed_content", "evaluate_test_set", "evaluate_single_test_sample") \
            if name == "utils.py" else ("extract_json_from_text",)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in wanted:
                if hasattr(parsing, node.name):
                    local = ast.parse(inspect.getsource(getattr(parsing, node.name))).body[0]
                    assert function_ast(node) == function_ast(local)
                nodes.append(node)
    for role, methods in (
        ("Generator", ("generate", "_extract_bullet_ids", "_extract_bullet_ids_regex")),
        ("Reflector", ("reflect", "_extract_bullet_tags")),
        ("Curator", ("curate", "_extract_and_validate_operations")),
    ):
        cls = copy.deepcopy(next(node for node in trees[f"ace/core/{role.lower()}.py"].body
                                 if isinstance(node, ast.ClassDef) and node.name == role))
        cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
        assert len(cls.body) == len(methods)
        for node in cls.body:
            if node.name.startswith("_"):
                local_cls = next(item for item in ast.parse(Path(parsing.__file__).read_text()).body
                                 if isinstance(item, ast.ClassDef) and item.name == role)
                local = next(item for item in local_cls.body if isinstance(item, ast.FunctionDef) and item.name == node.name)
                assert function_ast(node) == function_ast(local), node.name
        nodes.append(cls)
        for node in trees[f"ace/prompts/{role.lower()}.py"].body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if hasattr(prompts, name):
                    assert ast.literal_eval(node.value) == getattr(prompts, name), name
                    nodes.append(node)
    tree = ast.parse((source / "ace/ace.py").read_text())
    cls = copy.deepcopy(next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ACE"))
    methods = ("run", "_extract_config_params", "_setup_paths", "_run_test", "_train_single_sample",
               "_offline_train", "_online_train_and_test")
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    assert len(cls.body) == len(methods)
    nodes.append(cls)
    calls = []

    def transport(client, provider, model, prompt, role, **kwargs):
        calls.append((role, prompt))
        return responder(role, prompt), {}

    namespace.update(json=json, re=re, os=os, Path=Path, datetime=datetime, ThreadPoolExecutor=ThreadPoolExecutor,
        as_completed=as_completed, count_tokens=len, timed_llm_call=transport)
    for name in ("log_bullet_usage", "log_curator_operation_diff", "log_curator_failure"):
        namespace[name] = lambda *args, **kwargs: None
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source / "<verified-ace-method>"), "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    engine = namespace["ACE"]()
    engine.playbook, engine.best_playbook, engine.next_global_id = EMPTY_PLAYBOOK, EMPTY_PLAYBOOK, 1
    engine.max_tokens, engine.use_bulletpoint_analyzer, engine.bulletpoint_analyzer = 4096, False, None
    for role in ("Generator", "Reflector", "Curator"):
        actor = namespace[role]()
        actor.api_client, actor.api_provider, actor.model, actor.max_tokens = None, "openai", "fixture", 4096
        setattr(engine, role.lower(), actor)
    return engine, calls


class Processor:
    def answer_is_correct(self, answer, target):
        return answer == target

    def evaluate_accuracy(self, answers, targets):
        return sum(answer == target for answer, target in zip(answers, targets)) / len(answers)
