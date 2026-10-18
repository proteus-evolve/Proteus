"""Offline stdlib worker: submit exact code bytes, then await released feedback."""

import base64
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request


def request(path, body=None):
    url = os.environ["PROTEUS_EVALUATION_URL"] + path
    headers = {"Authorization": "Bearer " + os.environ["PROTEUS_EVALUATION_TOKEN"], "Content-Type": "application/json"}
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            wire = urllib.request.Request(url, method="GET" if body is None else "POST", headers=headers,
                                          data=None if body is None else json.dumps(body).encode())
            with urllib.request.urlopen(wire, timeout=1) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 503:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead):
            pass
        time.sleep(0.05)
    raise TimeoutError("gateway did not become available")


def main():
    launch = json.loads(sys.stdin.readline())
    worker = launch["identity"]["worker_id"]
    factor = 1 if worker == "a" else 2
    code = ("import json, sys\nr=json.load(sys.stdin)\n"
            f"print(json.dumps({{'output':r['task']['input']*{factor},'state':r['state']}}))\n").encode()
    Path("main.py").write_bytes(code)
    body = {"schema_version": 1, "operation_id": "proposal-" + launch["operation_id"], "evaluation_id": "validation",
            "files": [{"schema_version": 1, "path": "main.py", "base64": base64.b64encode(code).decode(), "executable": False}]}
    accepted = request("/v1/submissions", body)
    # Changes after submission must not change what the formal evaluator runs.
    Path("main.py").write_text("raise RuntimeError('later workspace edit must not be evaluated')\n")
    Path("ticket.json").write_text(json.dumps(accepted))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        response = request("/v1/submissions/" + accepted["ticket_id"])
        if response["status"] in ("completed", "failed"):
            Path("response.json").write_text(json.dumps(response))
            if response["status"] != "completed":
                raise RuntimeError("formal evaluation failed")
            print(json.dumps({"event": "result", "session_id": "gateway-" + worker,
                "resumed_from": launch["session_id"], "usage": {"model_calls": 0, "input_tokens": 0,
                "output_tokens": 0, "cost_usd": 0}}), flush=True)
            return
        time.sleep(0.025)
    raise TimeoutError("formal evaluation remains pending")


if __name__ == "__main__":
    main()
