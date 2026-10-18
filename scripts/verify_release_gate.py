"""Require live, recipe/package and Harbor gates for the exact release tag and commit."""
from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REQUIRED_JOBS = frozenset({"offline", "minimal", "llm", "container (pi)", "container (dsh)", "codex"})
REQUIRED_WORKFLOWS = {
    "release-smoke.yml": REQUIRED_JOBS,
    "ci.yml": frozenset({"Visualize presentation helpers", "Pinned AIME data and DSPy contract",
                         "Installed wheel and sdist", *(f"test ({version})" for version in
                         ("3.10", "3.11", "3.12", "3.13", "3.14"))}),
    "harbor.yml": frozenset({"Pinned Harbor and real Docker trials"}),
}


def matching_runs(runs, *, tag, sha):
    return [run for run in runs if run.get("head_sha") == sha
            and run.get("head_branch") == tag and run.get("event") == "push"
            and run.get("status") == "completed" and run.get("conclusion") == "success"]


def verify_jobs(jobs, *, required=REQUIRED_JOBS, sha=None, run_id=None):
    successful = {job.get("name") for job in jobs
                  if job.get("status") == "completed" and job.get("conclusion") == "success"
                  and (sha is None or job.get("head_sha") == sha)
                  and (run_id is None or job.get("run_id") == run_id)}
    missing = required - successful
    if missing:
        raise ValueError("release jobs missing or unsuccessful: " + ", ".join(sorted(missing)))


def verify_workflows(fetch, *, tag, sha):
    """Require all gates independently; never combine jobs from different runs."""
    verified = {}
    query = urlencode({"branch": tag, "event": "push", "status": "success", "per_page": 100})
    for workflow, required in REQUIRED_WORKFLOWS.items():
        runs = fetch(f"actions/workflows/{workflow}/runs?{query}")["workflow_runs"]
        for run in matching_runs(runs, tag=tag, sha=sha):
            jobs = fetch(f"actions/runs/{run['id']}/jobs?per_page=100&filter=latest")["jobs"]
            try:
                verify_jobs(jobs, required=required, sha=sha, run_id=run['id'])
            except ValueError:
                continue
            verified[workflow] = run["html_url"]
            break
        else:
            raise ValueError(f"No successful {workflow} with every required job for {tag} at {sha}. "
                             "Wait for this exact tag/commit's checks before publishing.")
    return verified


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
        verified = verify_workflows(fetch, tag=args.tag, sha=args.sha)
    except HTTPError as exc:
        parser.exit(1, f"GitHub release gate lookup failed (HTTP {exc.code}); upload is blocked.\n")
    except (URLError, TimeoutError, OSError, KeyError, TypeError):
        parser.exit(1, "GitHub release gate evidence is unavailable or invalid; upload is blocked.\n")
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    for workflow, url in verified.items():
        print(f"Release gate {workflow} verified for {args.tag} at {args.sha}: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
