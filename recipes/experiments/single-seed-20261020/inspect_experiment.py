#!/usr/bin/env python3
"""Inspect frozen experiment controls without importing a runtime or shadowing inspect."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text())


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def verify():
    index = read(ROOT / "index.json")
    profiles = index["profiles"]
    assert len(profiles) == index["profile_count"] == 15
    assert len({p["profile"] for p in profiles}) == 15
    assert index["retired_profile_not_used_as_success"] not in {p["profile"] for p in profiles}
    recipes = 0
    for profile in profiles:
        folder = ROOT / profile["directory"]
        provenance = read(folder / "provenance.json")
        assert provenance["profile"] == profile["profile"]
        assert provenance["bindings"] == profile["recipes"]
        for binding in profile["recipes"]:
            recipe = read(folder / binding["recipe"])
            lock = read(folder / binding["lock"])
            assert digest(recipe) == binding["config_hash"], (profile["profile"], "config hash")
            assert digest(lock) == binding["lock_hash"], (profile["profile"], "lock hash")
            assert recipe["method"]["plugin"]["name"]
            recipes += 1
        assert (folder / profile["historical_launcher"]).is_file()
    assert recipes == index["recipe_count"] == 16
    files = set()
    for line in (ROOT / "SHA256SUMS").read_text().splitlines():
        expected, relative = line.split("  ", 1)
        path = ROOT / relative
        assert path.resolve().is_relative_to(ROOT)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, relative
        files.add(relative)
    actual = {str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()
              and p.name != "SHA256SUMS" and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    assert files == actual, {"unlisted": sorted(actual - files), "missing": sorted(files - actual)}
    return {"profiles": 15, "recipes": recipes, "files_verified": len(files),
            "provider_calls": 0, "runtime_imports": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", nargs="?", help="exact profile label; omit to list all profiles")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(), indent=2))
        return
    index = read(ROOT / "index.json")
    if args.profile is None:
        for profile in index["profiles"]:
            print(f'{profile["family"]:12} {profile["profile"]}')
        return
    profile = next((p for p in index["profiles"] if p["profile"] == args.profile), None)
    if profile is None:
        parser.error("unknown profile; run without a profile to list valid labels")
    folder = ROOT / profile["directory"]
    print(json.dumps({**profile, "launch": read(folder / "launch.json"),
                      "provenance": read(folder / "provenance.json")}, indent=2))


if __name__ == "__main__":
    main()
