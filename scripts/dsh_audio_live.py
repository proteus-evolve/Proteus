#!/usr/bin/env python3
"""Export an episode-level DSH evolution feed and optionally publish it to GitHub.

The public site is static, so its live card polls one small JSON document.  This process
runs beside a Proteus sweep, rebuilds that document after every finished episode, and can
update the deployment repository through GitHub's Contents API.  It publishes normalized
tool names and touched paths only; assistant prose and tool arguments stay private.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proteus.adapters.dsh import DshHarness
from proteus.core import snapshot

BASELINE = {
    "release": "dsh-v0.1.0-rc.8",
    "commit": "141eb6fef83422698aef7a981029e843e8161534",
    "released": "2026-08-19",
}
DEFAULT_GOAL = "Evolve DeepSeek Harness rc.8 from image-only input into a safe, first-class audio input path."
LIVE_STATE = "live-state.json"
HEARTBEAT_STALE_S = 90


def _json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _runtime_state(sweep_root: Path) -> dict[str, Any]:
    """Read the launcher's heartbeat without ever exporting an error message or command."""
    path = sweep_root / LIVE_STATE
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    status = str(state.get("status") or "")
    heartbeat = float(state.get("heartbeat_at") or 0)
    if status in {"starting", "running"} and heartbeat:
        if time.time() - heartbeat > HEARTBEAT_STALE_S:
            status = "paused"
    return {"status": status, "proteus_version": state.get("proteus_version")}


def _snapshot_changes(run_root: Path, episode: int) -> dict[str, list[str]]:
    harness = run_root / "harness"
    before = snapshot.commit_for_episode(harness, episode - 1)
    after = snapshot.commit_for_episode(harness, episode)
    out = {"added": [], "changed": [], "deleted": []}
    if not before or not after:
        return out
    git_dir = run_root / ".snapshot.git"
    proc = subprocess.run(
        ["git", "--git-dir", str(git_dir), "diff", "--name-status", before, after],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    for line in proc.stdout.splitlines():
        status, _, path = line.partition("\t")
        if not path:
            continue
        key = "added" if status.startswith("A") else "deleted" if status.startswith("D") else "changed"
        out[key].append(path)
    return out


def _trace_steps(adapter: DshHarness, run_root: Path, episode: int) -> list[list[str]]:
    steps = []
    for event in adapter.read_trace(run_root, episode):
        if not event.tool:
            continue
        # Paths are useful public evidence. Other arguments may contain source, prompts,
        # shell commands, or credentials and are intentionally not exported.
        target = str(event.params.get("file_path") or event.params.get("path") or "")
        if not target and event.surface:
            target = event.surface
        target = target.replace("\\", "/")
        for prefix in ("/workspace/", "/opt/src/"):
            if prefix in target:
                target = target.split(prefix, 1)[1]
                break
        if target.startswith("/"):
            # Do not leak a local home or temporary-directory prefix. The basename is
            # enough evidence when the adapter did not report a workspace-relative path.
            target = Path(target).name
        steps.append([event.phase, event.tool, target[:180]])
    return steps


def _summary(episode: int, changes: dict[str, list[str]], scores: dict[str, float]) -> str:
    touched = changes["added"] + changes["changed"] + changes["deleted"]
    score = scores.get("dsh-audio-capability")
    score_text = f" The audio capability rubric reached {score:.0%}." if score is not None else ""
    if touched:
        lead = ", ".join(touched[:2])
        return f"Episode {episode} carried changes in {lead}{' and more' if len(touched) > 2 else ''}.{score_text}"
    return f"Episode {episode} kept the current structure and tested what already existed.{score_text}"


def build_payload(
    sweep_root: Path,
    *,
    editorial: dict[str, str] | None = None,
    adapter: DshHarness | None = None,
) -> dict[str, Any]:
    """Build the public, privacy-reduced feed from one single-run sweep."""
    sweep_root = Path(sweep_root)
    runtime = _runtime_state(sweep_root)
    manifest_path = sweep_root / "manifest.json"
    if not manifest_path.is_file():
        status = runtime.get("status") or "scheduled"
        return {
            "schema": 1,
            "status": status,
            "updated_at": None,
            "title": "Can DSH learn to hear?",
            "baseline": BASELINE,
            "proteus_version": runtime.get("proteus_version") or "0.1.0",
            "goal": DEFAULT_GOAL,
            "episodes_target": 12,
            "episodes": [],
        }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runs = manifest.get("runs") or []
    if len(runs) != 1:
        raise ValueError("the public feed requires exactly one run (one arm × one seed)")
    run = runs[0]
    progress = _json_lines(sweep_root / "progress" / f"{run['id']}.jsonl")
    run_root = sweep_root / "runs" / run["id"]
    adapter = adapter or DshHarness()
    editorial = editorial or {}
    episodes = []
    for row in progress:
        ep = int(row["episode"])
        changes = _snapshot_changes(run_root, ep)
        scores = {str(k): float(v) for k, v in (row.get("scores") or {}).items()}
        episodes.append({
            "episode": ep,
            "ok": bool(row.get("ok")),
            "accepted": bool(row.get("accepted", True)),
            "turns": int(row.get("turns") or 0),
            "tool_calls": int(row.get("tool_calls") or 0),
            "units": row.get("units") or {},
            "scores": scores,
            "changes": changes,
            "steps": _trace_steps(adapter, run_root, ep),
            "summary": editorial.get(str(ep)) or _summary(ep, changes, scores),
            "summary_kind": "editorial" if str(ep) in editorial else "automatic",
        })

    seed_rows = _json_lines(sweep_root / "seeds.jsonl")
    target = int(manifest.get("episodes") or 0)
    if any(row.get("error") for row in seed_rows):
        status = "error"
    elif episodes and episodes[-1]["episode"] >= target:
        status = "complete"
    elif runtime.get("status") in {"starting", "running", "paused", "error", "complete"}:
        status = runtime["status"]
    elif episodes or run_root.exists():
        status = "running"
    else:
        status = "scheduled"
    latest_ts = max((float(row.get("ts") or 0) for row in progress), default=0)
    updated_at = (datetime.fromtimestamp(latest_ts, timezone.utc).isoformat()
                  if latest_ts else None)
    return {
        "schema": 1,
        "status": status,
        "updated_at": updated_at,
        "title": "Can DSH learn to hear?",
        "baseline": BASELINE,
        "proteus_version": runtime.get("proteus_version") or "0.1.0",
        "model": manifest.get("model") or "deepseek-v4-flash",
        "goal": manifest.get("goal") or DEFAULT_GOAL,
        "episodes_target": target,
        "run": {"id": run["id"], "arm": run.get("arm"), "seed": run.get("seed")},
        "active_episode": min(len(episodes) + 1, target) if status in {"starting", "running"} else None,
        "episodes": episodes,
        "disclosure": "Real Proteus run · normalized DSH traces · summaries labelled automatic or editorial",
    }


def _write_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()
    if path.is_file() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return True


def _github_request(url: str, token: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "proteus-live-trace-publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"GitHub API {method} failed ({exc.code}): {detail}") from exc


def publish(repo: str, remote_path: str, branch: str, token: str, content: bytes) -> None:
    """Create or replace the public feed with one auditable GitHub commit."""
    url = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
    sha = None
    try:
        current = _github_request(f"{url}?ref={branch}", token)
        sha = current.get("sha")
        encoded = str(current.get("content") or "").replace("\n", "")
        if encoded and base64.b64decode(encoded) == content:
            return
    except RuntimeError as exc:
        if "(404)" not in str(exc):
            raise
    body = {
        "message": "live: update DSH audio evolution trace",
        "content": base64.b64encode(content).decode(),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    _github_request(url, token, method="PUT", body=body)


def _publish_token(env_name: str) -> str:
    """Use an explicit token first, then the user's existing GitHub CLI login."""
    token = os.environ.get(env_name, "")
    if token:
        return token
    if shutil.which("gh"):
        proc = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    raise SystemExit(
        f"set {env_name}, or run 'gh auth login', to publish the live feed"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", default="runs/dsh-audio-live")
    parser.add_argument("--out", default="web/static/assets/dsh-audio-live.json")
    parser.add_argument("--editorial", help="optional JSON object mapping episode number to summary")
    parser.add_argument("--watch", type=float, default=0, metavar="SECONDS")
    parser.add_argument("--repo", help="optional deployment repo, e.g. proteus-evolve/proteus-evolve.github.io")
    parser.add_argument("--remote-path", default="assets/dsh-audio-live.json")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--token-env", default="GH_TOKEN")
    args = parser.parse_args()

    editorial = (json.loads(Path(args.editorial).read_text(encoding="utf-8"))
                 if args.editorial else {})
    destination = Path(args.out)
    previous_digest = ""
    while True:
        payload = build_payload(Path(args.sweep), editorial=editorial)
        changed = _write_if_changed(destination, payload)
        content = destination.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if args.repo and digest != previous_digest:
            token = _publish_token(args.token_env)
            publish(args.repo, args.remote_path, args.branch, token, content)
        if changed:
            print(f"{payload['status']}: {len(payload['episodes'])} episodes -> {destination}", flush=True)
        previous_digest = digest
        if not args.watch or payload["status"] in {"complete", "error", "paused"}:
            return
        time.sleep(max(args.watch, 5))


if __name__ == "__main__":
    main()
