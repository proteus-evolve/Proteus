#!/usr/bin/env python3
"""Reconstruct a historical scientific core from the public base and exact patches."""

import argparse
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def git(*args, cwd):
    return subprocess.check_output(["git", *map(str, args)], cwd=cwd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=json.loads((ROOT / "sources.json").read_text())["profiles"])
    parser.add_argument("--repository", type=Path, default=ROOT.parents[2])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source = json.loads((ROOT / "sources.json").read_text())
    spec = source["profiles"][args.profile]
    out = args.out.resolve()
    if out.exists():
        parser.error("--out must not exist; source reconstruction never overwrites a checkout")
    archive = git("archive", source["base_commit"], cwd=args.repository)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            target = out / member.name
            if not target.resolve().is_relative_to(out) or not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported archive member: {member.name}")
        out.mkdir(parents=True)
        tar.extractall(out, filter="data")
    git("init", "--quiet", cwd=out)
    git("add", "--force", "--all", cwd=out)
    for patch in spec["patches"]:
        git("apply", "--index", ROOT / "source-patches" / patch, cwd=out)
    tree = git("write-tree", cwd=out).decode().strip()
    if tree != spec["tree"]:
        raise ValueError(f"reconstructed tree differs: {tree} != {spec['tree']}")
    git("remote", "add", "origin", out.as_uri(), cwd=out)
    git("-c", "user.name=Proteus recipe reconstruction", "-c", "user.email=recipes@example.invalid",
        "commit", "--quiet", "-m", f"Reconstruct exact source tree of {spec['revision']}", cwd=out)
    print(json.dumps({"source": str(out), "historical_revision": spec["revision"],
                      "verified_tree": tree,
                      "local_commit": git("rev-parse", "HEAD", cwd=out).decode().strip(),
                      "scope": "Exact source tree; new local commit, not historical commit identity.",
                      "provider_calls": 0}, indent=2))


if __name__ == "__main__":
    main()
