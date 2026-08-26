"""Behavioural distance over action traces — the manipulation-check ruler.

Reads the ordered tool stream from an action trace and compares two streams at three
pre-committed levels: unigram Jensen-Shannon over tool names (frequency), bigram JS
(order), and normalized compression distance over the raw sequence (procedure). The
between/within ratio `R` with a label-permutation test is the action-preference statistic:
does *who is acting* explain more of the spread than chance relabelling does.

This is what makes the paper's Step-1 (does the knob turn?) and δ₀ (dose) computable, and
it is read from the same normalized `ActionEvent` trace any harness emits.
"""

from __future__ import annotations

import bz2
import itertools
import math
import random
from collections import Counter
from typing import Sequence

from proteus.core.adapter import ActionEvent

TEXT_TOKEN = "<text>"


def tool_stream(trace: Sequence[ActionEvent]) -> list[str]:
    return [e.tool or TEXT_TOKEN for e in trace]


def _js(p: Counter, q: Counter) -> float:
    keys = set(p) | set(q)
    tp, tq = sum(p.values()) or 1, sum(q.values()) or 1
    m = {k: 0.5 * (p[k] / tp + q[k] / tq) for k in keys}

    def kl(a: Counter, ta: int) -> float:
        s = 0.0
        for k in keys:
            pa = a[k] / ta
            if pa > 0:
                s += pa * math.log2(pa / m[k])
        return s
    return 0.5 * kl(p, tp) + 0.5 * kl(q, tq)


def freq_distance(a: Sequence[str], b: Sequence[str]) -> float:
    return _js(Counter(a), Counter(b))


def order_distance(a: Sequence[str], b: Sequence[str]) -> float:
    ba = Counter(zip(a, a[1:]))
    bb = Counter(zip(b, b[1:]))
    return _js(ba, bb)


def ncd(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = (" ".join(a)).encode(), (" ".join(b)).encode()
    ca, cb = len(bz2.compress(sa)), len(bz2.compress(sb))
    cab = len(bz2.compress(sa + b" " + sb))
    return (cab - min(ca, cb)) / max(ca, cb) if max(ca, cb) else 0.0


def _resample(stream: Sequence[str], pool: Sequence[str], rng: random.Random) -> list[str]:
    """A stream of the same length drawn from `pool`: same composition, no procedure."""
    return [rng.choice(pool) for _ in stream]


def reliability(streams: Sequence[Sequence[str]], level: str = "freq",
                draws: int = 200, seed: int = 0) -> dict:
    """Does one condition reproduce itself? The prerequisite for any group comparison.

    `between_within` divides between-condition distance by within-condition distance, so
    if a condition does not reproduce itself the denominator is noise and R means nothing
    whichever way it comes out. This is therefore reported *before* R, and a failure here
    voids the comparison rather than qualifying it.

    The reference is a **composition-matched null**: streams resampled token by token from
    the condition's own pooled marginal, so a null stream has the same length and the same
    tool mix but no shared procedure. `within` far below `null` means runs of one condition
    share structure that random draws from the same tool budget do not produce.

    Returns `within`, `null`, their ratio (near 0 = highly reproducible, near 1 = no better
    than chance) and `reliable`, the pre-registered `ratio < 0.5`.
    """
    dist = {"freq": freq_distance, "order": order_distance, "ncd": ncd}[level]
    runs = [list(s) for s in streams]
    if len(runs) < 2:
        raise ValueError("reliability needs at least two runs of the condition")
    pairs = list(itertools.combinations(range(len(runs)), 2))
    within = sum(dist(runs[i], runs[j]) for i, j in pairs) / len(pairs)

    pool = [tok for s in runs for tok in s]
    if not pool:
        raise ValueError("reliability needs at least one action across the runs")
    rng = random.Random(seed)
    nulls = []
    for _ in range(draws):
        fake = [_resample(s, pool, rng) for s in runs]
        nulls.append(sum(dist(fake[i], fake[j]) for i, j in pairs) / len(pairs))
    null = sum(nulls) / len(nulls)
    ratio = within / null if null > 0 else float("nan")
    return {"within": within, "null": null, "ratio": ratio, "level": level,
            "n_runs": len(runs), "reliable": null > 0 and ratio < 0.5}


def between_within(streams: dict[str, list[list[str]]], level: str = "freq",
                   permutations: int = 10000, seed: int = 0) -> dict:
    """R = mean between-label distance / mean within-label distance, with a permutation p.

    `streams` maps a label (condition/seed) to its list of tool streams. The distance
    matrix is computed once; only the labels are shuffled, so the null tested is exactly
    'the labels are exchangeable'.
    """
    dist = {"freq": freq_distance, "order": order_distance, "ncd": ncd}[level]
    items = [(lbl, s) for lbl, ss in streams.items() for s in ss]
    n = len(items)
    D = [[0.0] * n for _ in range(n)]
    for i, j in itertools.combinations(range(n), 2):
        D[i][j] = D[j][i] = dist(items[i][1], items[j][1])
    labels = [lbl for lbl, _ in items]

    def ratio(labs: list[str]) -> tuple[float, float, float]:
        wi = [D[i][j] for i, j in itertools.combinations(range(n), 2) if labs[i] == labs[j]]
        bw = [D[i][j] for i, j in itertools.combinations(range(n), 2) if labs[i] != labs[j]]
        mw = sum(wi) / len(wi) if wi else 0.0
        mb = sum(bw) / len(bw) if bw else 0.0
        # zero within-label distance is the *strongest* separation, not the absence of an
        # effect: deterministic runs make identical within-label streams (JS = 0), and
        # `(mb/mw) if mw else 0` reported that maximal case as R=0, inverting the signal.
        # A tiny floor keeps the ratio large-but-finite and monotone with separation.
        r = mb / max(mw, 1e-9)
        return r, mw, mb

    if len(streams) < 2:
        raise ValueError(
            "between_within needs at least two labels: with one label there are no "
            "between-label pairs and R is undefined")
    if all(len(ss) < 2 for ss in streams.values()):
        raise ValueError(
            "between_within needs at least one label with 2+ streams: with a single stream "
            "per label there are no within-label pairs and R is undefined"
        )
    r_obs, mw, mb = ratio(labels)
    rng = random.Random(seed)
    hits = 0
    for _ in range(permutations):
        shuf = labels[:]
        rng.shuffle(shuf)
        r, _, _ = ratio(shuf)
        if r >= r_obs:
            hits += 1
    return {"R": r_obs, "within": mw, "between": mb,
            "p": (hits + 1) / (permutations + 1), "level": level}
