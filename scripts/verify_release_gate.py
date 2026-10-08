"""Require successful live smoke jobs for the exact release tag and commit before upload."""
from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REQUIRED_JOBS = {"offline", "minimal", "llm", "container (pi)", "container (dsh)", "codex"}


def matching_runs(runs, *, tag, sha):
    return [run for run in runs if run.get("head_sha") == sha
            and run.get("head_branch") == tag and run.get("event") == "push"
            and run.get("status") == "completed" and run.get("conclusion") == "success"]


def verify_jobs(jobs):
    successful = {job.get("name") for job in jobs if job.get("conclusion") == "success"}
    missing = REQUIRED_JOBS - successful
    if missing:
        raise ValueError("release smoke jobs missing or unsuccessful: " + ", ".join(sorted(missing)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        parser.exit(1, "GH_TOKEN is required to verify the release smoke gate\n")

    def fetch(path):
        request = Request(f"https://api.github.com/repos/{args.repository}/{path}", headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "proteus-release-gate",
        })
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    try:
        query = urlencode({"branch": args.tag, "event": "push", "status": "success", "per_page": 100})
        runs = fetch(f"actions/workflows/release-smoke.yml/runs?{query}")["workflow_runs"]
        candidates = matching_runs(runs, tag=args.tag, sha=args.sha)
        for run in candidates:
            jobs = fetch(f"actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
            try:
                verify_jobs(jobs)
            except ValueError:
                continue
            print(f"Release smoke verified for {args.tag} at {args.sha}: {run['html_url']}")
            return 0
        parser.exit(1, "No successful release-smoke with every required job for this exact tag/commit. "
                       "Wait for its smoke run to pass before publishing.\n")
    except HTTPError as exc:
        parser.exit(1, f"GitHub release gate lookup failed (HTTP {exc.code}); upload is blocked.\n")


if __name__ == "__main__":
    raise SystemExit(main())
