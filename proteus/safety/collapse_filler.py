"""Unrelated-word filler used to occupy memory on a collapse probe copy."""

from __future__ import annotations

import random

MIN_TOKENS = 200
MAX_TOKENS = 500
FLOOD_PREFIX = "flood-ep"

# Concrete nouns with no task, tool, or instruction force. Repeated by a seeded RNG.
_WORDS = (
    "amber", "anvil", "aspen", "barley", "basalt", "beetle", "birch", "boulder",
    "bramble", "bronze", "canyon", "cedar", "chalk", "cinder", "clover", "cobalt",
    "copper", "coral", "cotton", "creek", "crystal", "cypress", "daisy", "delta",
    "drift", "dune", "ember", "fern", "flint", "fog", "frost", "granite",
    "gravel", "harbor", "hazel", "heather", "horizon", "iris", "ivory", "jasper",
    "kelp", "lagoon", "lichen", "lotus", "maple", "marble", "meadow", "mist",
    "moss", "nickel", "oak", "obsidian", "olive", "onyx", "orchid", "pebble",
    "pine", "prairie", "quartz", "rain", "reed", "ridge", "river", "sable",
    "sage", "sand", "silt", "slate", "snow", "spruce", "stone", "storm",
    "stream", "thistle", "tide", "timber", "valley", "violet", "willow", "zinc",
)


def flood_state_id(episode: int, index: int = 0) -> str:
    if episode < 1 or index < 0:
        raise ValueError("flood state identity requires a positive episode")
    return f"{FLOOD_PREFIX}{episode}-{index}"


def is_flood_state_id(state_id: str) -> bool:
    return state_id.startswith(FLOOD_PREFIX)


def generate_unrelated_document(
    rng: random.Random,
    *,
    min_tokens: int = MIN_TOKENS,
    max_tokens: int = MAX_TOKENS,
) -> str:
    if min_tokens < 1 or max_tokens < min_tokens:
        raise ValueError("unrelated-word filler requires a positive token range")
    count = rng.randint(min_tokens, max_tokens)
    words = [rng.choice(_WORDS) for _ in range(count)]
    return " ".join(words) + "\n"


def parse_collapse_episodes(spec: str, episodes: int) -> frozenset[int]:
    if episodes < 1:
        raise ValueError("collapse episode selection requires a positive episode count")
    selected: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token == "last":
            selected.add(episodes)
            continue
        if token.startswith("every:"):
            try:
                step = int(token.split(":", 1)[1])
            except ValueError as exc:
                raise ValueError(
                    f"collapse episode {raw!r} must be every:<positive integer>"
                ) from exc
            if step < 1:
                raise ValueError("collapse every-N step must be a positive integer")
            selected.add(1)
            selected.update(range(step, episodes + 1, step))
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise ValueError(
                f"collapse episode {raw!r} must be an integer, 'last', or 'every:N'"
            ) from exc
        if value < 1 or value > episodes:
            raise ValueError(
                f"collapse episode {value} is outside 1..{episodes}"
            )
        selected.add(value)
    if not selected:
        raise ValueError("collapse episode selection is empty")
    return frozenset(selected)
