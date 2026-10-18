"""Exercise installed Formula study HTTP transport and audit outside the checkout."""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from proteus.benchmarks.ace_formula_reference import FormulaReference, parse_context_and_question_formula
from proteus.evaluation.tasks import Task, TaskDataset, TaskRecord
from proteus.examples.ace_formula import Settings, execute, prepare
from proteus.examples.ace_playbook import response
from proteus.examples.candidate_evaluation import plugin
from proteus.recipes.config import ActorSpec
from proteus.recipes.manifest import SourcePin


def check():
    assert FormulaReference()._formula_answer_is_correct("1,000.0", "1000")
    assert parse_context_and_question_formula("plain question") == ("", "plain question")
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
    previous = os.environ.get("ACE_PACKAGE_FIXTURE_KEY")
    os.environ["ACE_PACKAGE_FIXTURE_KEY"] = "fixture-only"
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "study"
            dataset = TaskDataset(version="installed-formula-fixture", records=tuple(
                TaskRecord(task=Task(id=f"{split}-{i}", version="1", split=split,
                    input={"question": f"{split} fixture {i}", "context": ""}), reference="1.0")
                for split in ("train", "validation", "test") for i in range(2)))
            settings = Settings(seeds=(0,), eval_steps=1, online_eval_frequency=1,
                **{role: ActorSpec(runtime=plugin("proteus.chat", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    key_env="ACE_PACKAGE_FIXTURE_KEY"), model=role) for role in ("generator", "reflector", "curator")})
            prepare(root, dataset, settings, sources=[SourcePin(name="proteus", repository="https://example.invalid/fixture",
                                                               revision="0" * 40)])
            result = execute(root)
            assert [row["accuracy"] for row in result["runs"]] == [1, 0.5]
            assert len(calls) == 30
            assert execute(root, resume=True) == result and len(calls) == 30
    finally:
        if previous is None:
            os.environ.pop("ACE_PACKAGE_FIXTURE_KEY", None)
        else:
            os.environ["ACE_PACKAGE_FIXTURE_KEY"] = previous
        server.shutdown()
        server.server_close()
        thread.join()
    print("Installed ACE Formula: offline/online studies, HTTP roles, complete private audit and call-free resume passed")


if __name__ == "__main__":
    check()
