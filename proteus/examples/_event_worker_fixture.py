"""Offline external process: submit once, wait for intervention, confirm continuation."""

import base64
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.error
import urllib.request


def main():
    request = json.loads(sys.stdin.readline())
    interrupted = False

    def pause(*_):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, pause)
    with Path("launches.jsonl").open("a") as stream:
        stream.write(json.dumps(request) + "\n")
    session = "session-" + request["identity"]["worker_id"]
    if request["session_id"] is not None:
        assert Path("session.txt").read_text() == request["session_id"] == session
    Path("session.txt").write_text(session)
    if request["continuity"] == "fresh":
        worker = request["identity"]["worker_id"]
        factor = 1 if worker == "a" else 2
        code = ("import json, sys\nr=json.load(sys.stdin)\n"
                f"print(json.dumps({{'output':r['task']['input']*{factor},'state':r['state']}}))\n")
        body = {"schema_version": 1, "operation_id": "candidate", "evaluation_id": "validation", "files": [
            {"schema_version": 1, "path": "main.py", "base64": base64.b64encode(code.encode()).decode(), "executable": False}]}
        url, token = os.environ["PROTEUS_EVALUATION_URL"], os.environ["PROTEUS_EVALUATION_TOKEN"]

        def call(path, data=None):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                req = urllib.request.Request(url + path, data=json.dumps(data).encode() if data is not None else None,
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=1) as response:
                        return json.load(response)
                except urllib.error.HTTPError as exc:
                    if exc.code != 503:
                        raise
                except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead):
                    pass
                time.sleep(0.025)
            raise TimeoutError("gateway did not recover")

        accepted = call("/v1/submissions", body)
        while True:
            response = call("/v1/submissions/" + accepted["ticket_id"])
            if response["status"] in ("completed", "failed"):
                assert response["status"] == "completed"
                Path("feedback.json").write_text(json.dumps(response))
                break
            time.sleep(0.01)
        Path("ready").touch()
        while not interrupted:
            time.sleep(0.01)
    else:
        Path("intervention.txt").write_text(request["prompt"])
    print(json.dumps({"event": "result", "session_id": session, "resumed_from": request["session_id"],
                      "usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}}), flush=True)


if __name__ == "__main__":
    main()
