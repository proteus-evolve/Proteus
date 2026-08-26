"""The Proteus hosted playground — submit a run, watch it evolve, bounded concurrency.

Stdlib only. One process serves the static site and the API, owns a FIFO queue, and runs
at most `--max-concurrent` sweeps at once; everything else waits in line and the queue
position is visible to the submitter.

Trust model (v1):
- **Harness allowlist.** Hosted runs execute only the prepared, pinned harnesses
  (`minimal`, `llm`, `dsh`, `pi`). Arbitrary repos stay a local-mode feature — a hosted
  "run any repo" is arbitrary code execution as a service.
- **BYO key, memory only.** The user's API key lives in the job object, is injected into
  the adapter instance for that run only (never process env, so concurrent tenants cannot
  cross-contaminate), and is dropped when the run ends.
- **Hard caps** on arms/seeds/episodes/text lengths; run artifacts expire after a TTL.
- Run ids are unguessable (uuid4); knowing the id grants read access to that run's
  progress — the sharable-link model.

**Deployment posture (v0.2.0): localhost / trusted network only.** This server is not
hardened for untrusted public exposure — there is no key redaction in logs, no auth on
cancellation, and a request can name an arbitrary LLM `base_url`. Bind it to 127.0.0.1
(the default) or put it behind an authenticating proxy; do not expose it raw to the
internet until these are addressed.

Run:  python web/server.py --port 8400 --max-concurrent 2
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proteus.core import evaluators
from proteus.core.disposition import NEUTRAL, Disposition, record, review
from proteus.core.goal import Goal, GoalConfig, Visibility
from proteus.sweep import SweepConfig, run_sweep

STATIC = Path(__file__).parent / "static"

# ------------------------------------------------------------------ caps & config
MAX_ARMS = 3
MAX_SEEDS = 2
MAX_EPISODES = 8
MAX_TEXT = 500
MAX_QUEUE = 10
RUN_TTL_S = 24 * 3600
HARNESSES = ("minimal", "llm", "dsh", "pi")   # hosted allowlist; local mode has more


@dataclass
class Job:
    id: str
    token: str
    spec: dict
    key: str | None
    root: Path
    status: str = "queued"          # queued | running | done | error
    error: str = ""
    created: float = field(default_factory=time.time)
    finished: float = 0.0


JOBS: dict[str, Job] = {}
LOCK = threading.Lock()
PENDING: "queue.Queue[str]" = queue.Queue()
EP_DELAY = 0.0  # test hook: --test-delay adds per-episode sleep to make queueing visible


# ------------------------------------------------------------------ spec -> sweep
def _arm(a: dict, i: int) -> Disposition:
    kind = a.get("kind", "neutral")
    if kind == "neutral":
        return NEUTRAL
    if kind == "review":
        return review(str(a.get("surface", "notes"))[:40])
    if kind == "record":
        return record(str(a.get("surface", "notes"))[:40])
    if kind == "custom":
        text = str(a.get("text", ""))[:MAX_TEXT]
        if not text.strip():
            raise ValueError("custom arm needs non-empty text")
        return Disposition(label=f"custom_{i}", prompt_suffix=text)
    raise ValueError(f"unknown arm kind {kind!r}")


def _evaluator(e: dict):
    etype = e.get("type", "")
    if etype == "surface_units":
        return evaluators.surface_units(str(e.get("surface", "notes"))[:40],
                                        name=str(e.get("name", ""))[:40] or "units")
    if etype == "tool_calls":
        return evaluators.tool_calls(name=str(e.get("name", ""))[:40] or "activity",
                                     ceiling=int(e.get("ceiling", 0)))
    if etype == "file_contains":
        return evaluators.file_contains(str(e.get("path", ""))[:120],
                                        str(e.get("needle", ""))[:120],
                                        name=str(e.get("name", ""))[:40])
    if etype in ("", "none"):
        return None
    raise ValueError(f"unknown evaluator type {etype!r}")


def _goal_config(g: dict) -> GoalConfig:
    mode = g.get("mode", "none")
    if mode == "none":
        return GoalConfig.no_goal()
    goals = []
    for spec in (g.get("goals") or [])[:3]:
        vis = Visibility.OBSERVE if spec.get("visibility") == "observe" else Visibility.HIDDEN
        goals.append(Goal(
            name=str(spec.get("name", "goal"))[:40],
            text=str(spec.get("text", ""))[:MAX_TEXT],
            evaluator=_evaluator(spec.get("evaluator") or {}),
            visibility=vis,
        ))
    if not goals:
        raise ValueError("goal mode set but no goals given")
    selection = "accept_reject" if g.get("selection") == "accept_reject" else "none"
    if mode == "single":
        return GoalConfig.single(goals[0], selection=selection)
    return GoalConfig.multi(goals, selection=selection)


def _adapter_factory(spec: dict, key: str | None):
    harness = spec["harness"]
    model = str(spec.get("model", ""))[:80]
    base_url = str(spec.get("base_url", ""))[:200]

    def factory():
        if harness == "minimal":
            from proteus.adapters.minimal import MinimalHarness
            adapter = MinimalHarness()
        elif harness == "llm":
            from proteus.adapters.llm import LLMHarness
            adapter = LLMHarness(model=model or None, base_url=base_url or None, key=key)
        elif harness == "dsh":
            from proteus.adapters.dsh import DshHarness
            adapter = DshHarness(key=key)
        else:
            from proteus.adapters.pi import PiHarness
            adapter = PiHarness(model=model or "deepseek-v4-flash", key=key)
        if EP_DELAY:
            adapter = _SlowWrapper(adapter, EP_DELAY)
        return adapter
    return factory


class _SlowWrapper:
    """Test-only: makes queue behaviour observable with the instant minimal harness."""

    def __init__(self, inner, delay: float):
        self._inner, self._delay = inner, delay

    def run_episode(self, spec):
        time.sleep(self._delay)
        return self._inner.run_episode(spec)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def validate(spec: dict) -> str:
    if spec.get("harness") not in HARNESSES:
        return f"harness must be one of {HARNESSES}"
    arms = spec.get("arms") or []
    if not 1 <= len(arms) <= MAX_ARMS:
        return f"1..{MAX_ARMS} arms"
    try:
        for i, a in enumerate(arms):
            _arm(a, i)
        _goal_config(spec.get("goal") or {"mode": "none"})
    except ValueError as exc:
        return str(exc)
    if not 1 <= int(spec.get("seeds", 1)) <= MAX_SEEDS:
        return f"1..{MAX_SEEDS} seeds"
    if not 1 <= int(spec.get("episodes", 3)) <= MAX_EPISODES:
        return f"1..{MAX_EPISODES} episodes"
    if spec.get("harness") != "minimal" and not (spec.get("key") or "").strip():
        return "this harness needs an API key"
    return ""


# ------------------------------------------------------------------ workers
def worker(runs_dir: Path):
    while True:
        job_id = PENDING.get()
        with LOCK:
            job = JOBS[job_id]
            job.status = "running"
        try:
            spec, key = job.spec, job.key
            arms = [_arm(a, i) for i, a in enumerate(spec["arms"])]
            cfg = SweepConfig(
                name=job.id,
                adapter_factory=_adapter_factory(spec, key),
                arms=arms,
                seeds=int(spec.get("seeds", 1)),
                goal=_goal_config(spec.get("goal") or {"mode": "none"}),
                root=job.root,
                model=str(spec.get("model", ""))[:80],
                episodes=int(spec.get("episodes", 3)),
            )
            records = run_sweep(cfg)
            errs = [r["error"] for r in records if r.get("error")]
            with LOCK:
                job.status = "error" if errs else "done"
                job.error = errs[0][:300] if errs else ""
        except Exception as exc:  # noqa: BLE001
            with LOCK:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            with LOCK:
                job.key = None          # drop the key the moment the run ends
                job.finished = time.time()


def reaper(runs_dir: Path):
    while True:
        time.sleep(600)
        now = time.time()
        with LOCK:
            expired = [j for j in JOBS.values()
                       if j.finished and now - j.finished > RUN_TTL_S]
            for j in expired:
                JOBS.pop(j.id, None)
        for j in expired:
            shutil.rmtree(j.root, ignore_errors=True)


def queue_position(job_id: str) -> int:
    with LOCK:
        waiting = sorted((j.created, j.id) for j in JOBS.values() if j.status == "queued")
    for pos, (_, jid) in enumerate(waiting, 1):
        if jid == job_id:
            return pos
    return 0


# ------------------------------------------------------------------ http
class Handler(BaseHTTPRequestHandler):
    runs_dir: Path = Path("runs-hosted")

    def log_message(self, *a):  # no request logging: URLs must never leak into logs
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str) -> None:
        if not path.is_file():
            return self._json(404, {"error": "not found"})
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------- GET
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._file(STATIC / "index.html", "text/html; charset=utf-8")
        if p in ("/playground", "/playground.html"):
            return self._file(STATIC / "playground.html", "text/html; charset=utf-8")
        if p in ("/run", "/run.html"):
            return self._file(STATIC / "run.html", "text/html; charset=utf-8")
        if p in ("/demo", "/demo.html"):
            return self._file(STATIC / "demo.html", "text/html; charset=utf-8")
        if re.fullmatch(r"/assets/[\w.-]+", p):
            ext = p.rsplit(".", 1)[-1]
            ctype = {"png": "image/png", "svg": "image/svg+xml", "css": "text/css",
                     "js": "text/javascript", "json": "application/json"}.get(
                ext, "application/octet-stream")
            return self._file(STATIC / "assets" / p.split("/")[-1], ctype)
        if p == "/api/config":
            return self._json(200, {
                "harnesses": HARNESSES, "max_arms": MAX_ARMS, "max_seeds": MAX_SEEDS,
                "max_episodes": MAX_EPISODES,
                "surfaces": {"minimal": ["notes", "tools"],
                             "llm": ["notes", "tools"],
                             "dsh": ["notes", "tools", "instructions"],
                             "pi": ["notes", "tools", "skills", "instructions"]},
            })
        m = re.fullmatch(r"/api/runs/([0-9a-f-]{36})", p)
        if m:
            with LOCK:
                job = JOBS.get(m.group(1))
                if not job:
                    return self._json(404, {"error": "unknown run"})
                out = {"id": job.id, "status": job.status, "error": job.error}
            if job.status == "queued":
                out["queue_position"] = queue_position(job.id)
            return self._json(200, out)
        m = re.fullmatch(r"/api/runs/([0-9a-f-]{36})/manifest\.json", p)
        if m:
            return self._file(self.runs_dir / m.group(1) / "manifest.json",
                              "application/json")
        m = re.fullmatch(r"/api/runs/([0-9a-f-]{36})/progress/(run-[0-9a-f]{12})\.jsonl", p)
        if m:
            return self._file(self.runs_dir / m.group(1) / "progress" / f"{m.group(2)}.jsonl",
                              "application/jsonl")
        return self._json(404, {"error": "not found"})

    # ---------------- POST
    def do_POST(self):
        if self.path != "/api/runs":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            spec = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        err = validate(spec)
        if err:
            return self._json(400, {"error": err})
        with LOCK:
            waiting = sum(1 for j in JOBS.values() if j.status in ("queued", "running"))
        if waiting >= MAX_QUEUE:
            return self._json(429, {"error": "queue full, try again later"})
        key = (spec.pop("key", "") or "").strip() or None
        job = Job(id=str(uuid.uuid4()), token=secrets.token_urlsafe(16),
                  spec=spec, key=key, root=self.runs_dir / "placeholder")
        job.root = self.runs_dir / job.id
        with LOCK:
            JOBS[job.id] = job
        PENDING.put(job.id)
        return self._json(201, {"id": job.id, "token": job.token,
                                "queue_position": queue_position(job.id)})


def main() -> int:
    global EP_DELAY
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8400)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--max-concurrent", type=int, default=2)
    ap.add_argument("--runs-dir", default="runs-hosted")
    ap.add_argument("--test-delay", type=float, default=0.0,
                    help="test hook: seconds of sleep per episode")
    args = ap.parse_args()
    EP_DELAY = args.test_delay

    runs_dir = Path(args.runs_dir).resolve()
    runs_dir.mkdir(parents=True, exist_ok=True)
    Handler.runs_dir = runs_dir
    for _ in range(max(1, args.max_concurrent)):
        threading.Thread(target=worker, args=(runs_dir,), daemon=True).start()
    threading.Thread(target=reaper, args=(runs_dir,), daemon=True).start()

    print(f"proteus playground on http://{args.host}:{args.port} "
          f"(max {args.max_concurrent} concurrent sweeps, queue {MAX_QUEUE})")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
