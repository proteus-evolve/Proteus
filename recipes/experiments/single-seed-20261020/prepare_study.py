#!/usr/bin/env python3
"""Prepare a NEW AIME/Formula run with frozen scientific settings and explicit transport."""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
MODULES = {"gepa": "proteus.examples.gepa_aime", "ace": "proteus.examples.ace_formula",
           "combee": "proteus.examples.combee_formula"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile")
    parser.add_argument("--source", type=Path, required=True, help="clean reconstructed source checkout")
    parser.add_argument("--data", type=Path, required=True, help="pinned tasks.json/provenance.json directory")
    parser.add_argument("--out", type=Path, required=True, help="new study directory")
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible URL; no credentials in this value")
    parser.add_argument("--key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--model", help="provider model name; omit only if endpoint supports deepseek-flash alias")
    args = parser.parse_args()
    index = json.loads((ROOT / "index.json").read_text())
    profile = next((p for p in index["profiles"] if p["profile"] == args.profile), None)
    if profile is None or profile["family"] not in MODULES:
        parser.error("this preparer supports AIME/Formula only; see the family README for container profiles")
    source = args.source.resolve()
    if not source.is_dir():
        parser.error("--source does not exist; reconstruct the pinned source first")
    if args.out.exists() or args.out.with_suffix(".settings.json").exists():
        parser.error("output/settings already exist; choose a fresh output path")
    base_url = args.base_url
    from urllib.parse import urlsplit
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("--base-url must be an HTTP(S) URL without credentials, query or fragment")
    settings = copy.deepcopy(json.loads((ROOT / profile["directory"] / "settings.json").read_text()))
    for name in ("solver", "reflection", "generator", "reflector", "curator"):
        if name not in settings:
            continue
        actor = settings[name]
        options = actor["runtime"]["options"]
        options.pop("dispatch_guard_path", None)
        options.update(base_url=base_url, key_env=args.key_env)
        if args.model:
            actor["model"] = args.model
    settings_path = args.out.resolve().with_suffix(".settings.json")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("x") as output:
        json.dump(settings, output, indent=2)
        output.write("\n")
    env = dict(os.environ, PYTHONPATH=str(source))
    module = MODULES[profile["family"]]
    command = [sys.executable, "-m", module, "prepare", "--out", str(args.out.resolve()),
               "--source", str(source), "--data", str(args.data.resolve()), "--settings", str(settings_path)]
    subprocess.run(command, cwd=source, env=env, check=True)
    print(json.dumps({"new_run": str(args.out.resolve()), "settings": str(settings_path),
        "run_command": [sys.executable, "-m", module, "run", "--out", str(args.out.resolve())],
        "runtime_environment": {"PYTHONPATH": str(source)},
        "notice": "Preparation only. Transport/source identities differ from the original locks. "
                  "Executing run makes new paid calls; historical results are not replayed."}, indent=2))


if __name__ == "__main__":
    main()
