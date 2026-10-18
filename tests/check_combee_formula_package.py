"""Installed four-arm Formula study, CLI reports and call-free resume via HTTP."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.ace_playbook import response
from proteus.examples.candidate_evaluation import plugin
from proteus.examples.combee_formula import Settings, prepare, report
from proteus.operators.combee.batching import DynamicBatchOptions
from proteus.recipes.config import ActorSpec
from proteus.recipes.manifest import SourcePin


def check():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(payload)
            text = response(payload["model"], payload["messages"][0]["content"])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    prior = os.environ.get("COMBEE_PACKAGE_FIXTURE_KEY")
    os.environ["COMBEE_PACKAGE_FIXTURE_KEY"] = "fixture-only"
    try:
        with TemporaryDirectory(prefix="proteus-combee-formula-package-") as directory:
            root = Path(directory) / "study"
            dataset = TaskDataset(version="installed-combee-formula-fixture-1", records=tuple(
                TaskRecord(task=Task(id=f"{split}-{i}", version="1", split=split,
                    input={"question": f"{split} fixture {i}", "context": ""}), reference="1.0")
                for split, count in (("train", 3), ("validation", 2), ("test", 2)) for i in range(count)))
            settings = Settings(seeds=(0,), eval_steps=3, online_eval_frequency=2, batch_size=2, max_workers=2,
                dynamic_batch=DynamicBatchOptions(max_batch_size=2),
                **{role: ActorSpec(runtime=plugin("proteus.chat", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    key_env="COMBEE_PACKAGE_FIXTURE_KEY"), model=role) for role in ("generator", "reflector", "curator")})
            prepare(root, dataset, settings, sources=[SourcePin(name="proteus", repository="https://example.invalid/package",
                                                               revision="0" * 40)])
            outputs = []
            first_calls = None
            for action in ("run", "report", "resume"):
                completed = subprocess.run([sys.executable, "-m", "proteus.examples.combee_formula", action,
                    "--out", str(root)], capture_output=True, text=True, timeout=180)
                assert completed.returncode == 0, completed.stdout + completed.stderr
                outputs.append(json.loads(completed.stdout))
                if first_calls is None:
                    first_calls = len(calls)
                assert len(calls) == first_calls
            assert outputs[0] == outputs[1] == outputs[2] == report(root)
            result = outputs[0]
            assert len(result["runs"]) == 8
            assert sum(row["ledger"]["charged"]["model_calls"] for row in result["runs"]) == len(calls)
            assert all(row["elapsed"]["wall_seconds"] > 0 and row["ledger"]["charged"]["cost_usd"] is None
                       for row in result["runs"])
    finally:
        if prior is None:
            os.environ.pop("COMBEE_PACKAGE_FIXTURE_KEY", None)
        else:
            os.environ["COMBEE_PACKAGE_FIXTURE_KEY"] = prior
        server.shutdown()
        server.server_close()
        thread.join()
    print("Installed Combee Formula: four arms, offline/online, HTTP accounting, private audit and CLI resume passed; no inference.")


if __name__ == "__main__":
    check()
