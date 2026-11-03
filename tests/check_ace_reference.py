"""Check selected ACE code against an actual hash-pinned upstream checkout.

Only verified pure function ASTs execute; upstream SDK imports never run. The
checker also exercises the Proteus wrapper against those reference functions.
"""

import argparse
import ast
import copy
import hashlib
import inspect
import json
import random
import re
from pathlib import Path

from proteus.methods.ace import upstream
from proteus.methods.ace.playbook import EMPTY_PLAYBOOK, REVISION, Playbook, PlaybookCodec, PlaybookDelta


SOURCE_HASHES = {
    "utils.py": "7824aa262de2cd030b6914d1f7de698ec620dbec9c9e5f14ed1fe6322079570c",
    "playbook_utils.py": "c67943f06cb0ee747be04d0599216e40d6c00e0cdf96ddc6af38fc26143e3a07",
    "ace/ace.py": "70f7af3ff9088498afa8d6939c5e142cd8b0046179516ee336c57586b92ec914",
    "LICENSE.txt": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
}
FUNCTIONS = {
    "utils.py": ("get_section_slug",),
    "playbook_utils.py": ("parse_playbook_line", "get_next_global_id", "format_playbook_line",
        "update_bullet_counts", "apply_curator_operations", "get_playbook_stats", "extract_playbook_bullets"),
}


def function_ast(node):
    node = copy.deepcopy(node)
    # The vendored file removes trailing whitespace, including in docstrings.
    # All executable AST nodes and every other string remain exact.
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        first.value.value = "\n".join(line.rstrip() for line in first.value.value.split("\n"))
    return ast.dump(node)


def reference(source):
    source = Path(source)
    trees = {}
    for name, expected in SOURCE_HASHES.items():
        raw = (source / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected, f"ACE reference changed: {name}"
        if name.endswith(".py"):
            trees[name] = ast.parse(raw.decode("utf-8"))
    nodes = []
    for name, functions in FUNCTIONS.items():
        by_name = {node.name: node for node in trees[name].body if isinstance(node, ast.FunctionDef)}
        for function in functions:
            node = by_name[function]
            local = ast.parse(inspect.getsource(getattr(upstream, function))).body[0]
            assert function_ast(node) == function_ast(local), f"vendored ACE function changed: {function}"
            nodes.append(node)
    namespace = {"re": re, "print": lambda *args, **kwargs: None}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source / "<selected-functions>"), "exec"), namespace)
    ace = next(node for node in trees["ace/ace.py"].body if isinstance(node, ast.ClassDef) and node.name == "ACE")
    initialize = next(node for node in ace.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_initialize_empty_playbook")
    template = next(node.value for node in initialize.body if isinstance(node, ast.Return))
    assert ast.literal_eval(template) == EMPTY_PLAYBOOK
    assert (Path(upstream.__file__).parent / "LICENSE-ACE.txt").read_bytes() == (source / "LICENSE.txt").read_bytes()
    return namespace


def check(source):
    ref = reference(source)
    codec, updates, reads = PlaybookCodec(), 0, 0
    templates = (EMPTY_PLAYBOOK, " \n## OTHERS\n\n", "## A\nlegacy text\n\n## A\n## OTHERS",
                 "## formulas & calculations\n## others\n", "## ONLY\n", "## general\n\n## OTHERS\n")
    sections = ("STRATEGIES & INSIGHTS", "FORMULAS & CALCULATIONS", "PROBLEM-SOLVING HEURISTICS",
                "common_mistakes_to_avoid", "A", "unknown", "general", "OTHERS", " ONLY ")
    for seed in range(48):
        rng = random.Random(seed)
        text, next_id = templates[seed % len(templates)], 1
        value = Playbook.from_text(text)
        for step in range(16):
            if step % 2 == 0:
                records = [{"type": "ADD", "section": rng.choice(sections),
                            "content": rng.choice(("first", "Unicode: 经验 α", "line one\n  continuation", ""))}
                           for _ in range(rng.randrange(4))]
                records.insert(rng.randrange(len(records) + 1), {"type": rng.choice(("UPDATE", "MERGE", "DELETE", "CREATE_META", "UNKNOWN")),
                    "bullet_id": "misc-00001", "content": "must not replace an entry"})
                kind = "curate"
                expected_text, expected_next = ref["apply_curator_operations"](text, records, next_id)
            else:
                ids = [bullet.id for bullet in value.bullets] or ["unknown"]
                chosen = rng.choice(ids)
                records = [{"id": chosen, "tag": "helpful"}, {"bullet": chosen, "tag": "harmful"},
                           {"id": rng.choice(ids), "tag": rng.choice(("neutral", "helpful", "other"))},
                           {"id": "missing", "tag": "helpful"}, None, "ignored", {}]
                kind = "counters"
                expected_text, expected_next = ref["update_bullet_counts"](text, records), next_id
            delta = PlaybookDelta(base_hash="sha256:" + "0" * 64, kind=kind, records=records)
            value = codec.apply(value, PlaybookDelta.from_json(delta.to_json()))
            assert (value.text, value.next_id) == (expected_text, expected_next), (seed, step)
            value = codec.decode(codec.encode(value))
            assert value.stats() == ref["get_playbook_stats"](value.text)
            assert ref["get_next_global_id"](value.text) <= value.next_id
            for selection in ([], ["unknown"], [bullet.id for bullet in value.bullets][::-2]):
                assert value.extract(selection) == ref["extract_playbook_bullets"](value.text, selection)
                reads += 1
            text, next_id = expected_text, expected_next
            updates += 1
    return {"revision": REVISION, "source_hashes": SOURCE_HASHES,
            "functions": sum(map(len, FUNCTIONS.values())), "updates": updates, "extractions": reads}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    print(json.dumps(check(parser.parse_args().source), indent=2))
