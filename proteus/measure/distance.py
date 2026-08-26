"""Structural distance between harness states — the instrument no fixed harness-evolver ships.

Distance is defined over **units of structure**, not bytes (a byte count measures
verbosity), and separately per **surface**, iterating the harness's declared manifest so a
new harness with new surfaces is measured without changing this file. For each surface a
unit is added / dropped / revised / unchanged between two states; these are reported
separately (a harness that only adds is accumulating; one that revises and drops is
curating) and their sum over the union is the distance in [0, 1].

Travel is the **path length** summed episode to episode, not endpoint displacement, because
surfaces start empty and endpoint distance saturates after the first episode.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from proteus.core.adapter import Surface


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts)


def _defs(path: Path, rel: str) -> dict[str, str]:
    """Top-level defs and classes of a Python file, as {rel::name: hash of its source}.

    Falls back to the whole file when it does not parse — a harness mid-edit often has
    a syntactically broken file, and a measurement pass must not raise on it.
    """
    import ast
    src = _read(path)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {rel: _sha(src)}
    lines = src.splitlines(keepends=True)
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        seg = "".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
        out[f"{rel}::{node.name}"] = _sha(seg)
    # module-level code outside any def is itself a unit: an agent that rewrites the
    # dispatch table at the bottom of loop.py has changed the harness
    body = "".join(ln for i, ln in enumerate(lines, 1)
                   if not any(n.lineno <= i <= (n.end_lineno or n.lineno)
                              for n in tree.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                ast.ClassDef))))
    if body.strip():
        out[f"{rel}::<module>"] = _sha(body)
    return out


def units(harness_root: Path, surfaces: Sequence[Surface]) -> dict[str, dict[str, str]]:
    """Every unit of every declared surface, as {surface: {unit_id: content_hash}}.

    Unit ids are paths relative to the surface, never bare stems: two files that share a
    stem in different directories are two units, and a directory-unit surface hashes all
    of its members, so editing any one of them moves the unit.
    """
    out: dict[str, dict[str, str]] = {s.name: {} for s in surfaces}
    for s in surfaces:
        base = harness_root / s.subdir
        if base.is_file():
            # a surface whose subdir is a single file (a self-edited code file such as
            # loop.py, or an instructions file such as AGENTS.md). Skipping these was a
            # measurement bug: they are exactly the self-editing surfaces the study is
            # about, and they would have read distance 0 forever.
            if s.unit == "top_level_def":
                out[s.name].update(_defs(base, base.name))
            else:
                out[s.name][base.name] = _sha(_read(base))
            continue
        if not base.is_dir():
            continue
        if s.unit == "directory":
            # the unit is a directory (a skill is a folder); its hash covers every member,
            # so a change to any file inside moves the unit. Keying by the directory name
            # alone let the last file written overwrite its siblings' hashes, and a
            # two-file skill could be edited without the measurement changing at all.
            groups: dict[str, list[Path]] = {}
            for path in _files(base):
                rel = path.relative_to(base)
                # the unit is the TOP-LEVEL directory: alpha/scripts/run.py belongs to
                # unit "alpha", not to a separate unit "alpha/scripts"
                key = rel.parts[0] if len(rel.parts) > 1 else rel.as_posix()
                groups.setdefault(key, []).append(path)
            for key, members in groups.items():
                out[s.name][key] = _sha("\0".join(
                    f"{p.relative_to(base).as_posix()}\0{_read(p)}" for p in sorted(members)))
            continue
        for path in _files(base):
            rel = path.relative_to(base).as_posix()
            if s.unit == "top_level_def":
                out[s.name].update(_defs(path, rel))
            else:
                out[s.name][rel] = _sha(_read(path))
    return out


@dataclass(frozen=True)
class SurfaceDelta:
    surface: str
    added: int
    dropped: int
    revised: int
    unchanged: int

    @property
    def union(self) -> int:
        return self.added + self.dropped + self.revised + self.unchanged

    @property
    def distance(self) -> float:
        return 0.0 if not self.union else (self.added + self.dropped + self.revised) / self.union

    @property
    def churn(self) -> float:
        """Of what moved, the fraction that was revision/removal rather than accumulation."""
        moved = self.added + self.dropped + self.revised
        return 0.0 if not moved else (self.dropped + self.revised) / moved


def _sim(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9_]{3,}", a.lower()))
    tb = set(re.findall(r"[a-z0-9_]{3,}", b.lower()))
    if not ta or not tb:
        return 1.0 if ta == tb else 0.0
    return len(ta & tb) / len(ta | tb)


def delta(a: dict[str, str], b: dict[str, str], surface: str) -> SurfaceDelta:
    both = set(a) & set(b)
    revised = sum(1 for k in both if a[k] != b[k])
    return SurfaceDelta(surface, len(set(b) - set(a)), len(set(a) - set(b)),
                        revised, len(both) - revised)


def compare(root_a: Path, root_b: Path, surfaces: Sequence[Surface]) -> dict[str, SurfaceDelta]:
    ua, ub = units(root_a, surfaces), units(root_b, surfaces)
    return {s.name: delta(ua[s.name], ub[s.name], s.name) for s in surfaces}


def path_length(states: Sequence[Path], surfaces: Sequence[Surface]) -> dict[str, SurfaceDelta]:
    """Travel summed along consecutive states, per surface."""
    totals = {s.name: SurfaceDelta(s.name, 0, 0, 0, 0) for s in surfaces}
    for before, after in zip(states, states[1:]):
        step = compare(before, after, surfaces)
        for name, d in step.items():
            acc = totals[name]
            totals[name] = SurfaceDelta(name, acc.added + d.added, acc.dropped + d.dropped,
                                        acc.revised + d.revised, acc.unchanged + d.unchanged)
    return totals
