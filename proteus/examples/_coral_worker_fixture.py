"""Long-lived, model-free fixture for controller cooperation and native continuation."""

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


def call(path, body=None):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        request = urllib.request.Request(os.environ["PROTEUS_EVALUATION_URL"] + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + os.environ["PROTEUS_EVALUATION_TOKEN"],
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 503:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead):
            pass
        time.sleep(.025)
    raise TimeoutError("gateway did not recover")


def file(path, data):
    return {"path": path, "base64": base64.b64encode(data).decode(), "executable": False}


def publish(kind, key, files, sources=()):
    value = call("/v1/knowledge/writes", {"action": "publish", "operation_id": key, "kind": kind,
        "key": key, "files": files, "sources": list(sources)})
    assert value["outcome"]["status"] == "published"
    return value["outcome"]["revision"]


def main():
    launch = json.loads(sys.stdin.readline())
    worker = launch["identity"]["worker_id"]
    interrupted = False

    def stop(*_):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, stop)
    with Path("launches.jsonl").open("a") as stream:
        stream.write(json.dumps(launch) + "\n")
    session = "coral-fixture-" + worker
    if launch["session_id"] is not None:
        assert Path("session.txt").read_text() == launch["session_id"] == session
    Path("session.txt").write_text(session)
    candidate = Path("candidate/main.py")
    assert candidate.is_file(), "controller must seed the editable workspace"
    if not Path("initial-source.txt").exists():
        Path("initial-source.txt").write_text(candidate.read_text())

    done = len(list(Path(".").glob("result-*.json")))
    if worker == "b" and done == 1 and launch["continuity"] != "fresh":
        publish("notes", "b-resumed", [file("ready.txt", b"Native intervention generation is running.\n")])
    if done < (2 if worker == "a" else 1) and "--idle" not in sys.argv:
        sources = []
        if worker == "a":
            publish("notes", "strategy", [file("factor.txt", b"2")])
            if done == 1 and "--cooperate" in sys.argv:
                # Make the fixture's second proposal causally follow B's native
                # continuation; no assumptions about OS scheduling or FIFO arrival.
                deadline, index, ready = time.monotonic() + 35, 0, False
                while not ready and time.monotonic() < deadline:
                    snapshot = call("/v1/knowledge/snapshots", {"operation_id": "await-b-" + str(index)})
                    index += 1
                    for ref in snapshot["snapshot"]["revisions"]:
                        record = call("/v1/knowledge/revisions/" + ref["content_hash"].split(":")[1])
                        write = record["revision"]["write"]
                        if (write["kind"], write["key"]) == ("notes", "b-resumed"):
                            ready = True
                            break
                    if not ready:
                        time.sleep(.025)
                assert ready, "cooperating worker did not resume"
            factor = 1 if done == 0 else 2
        else:
            index, factor = 0, None
            deadline = time.monotonic() + 35
            while factor is None and time.monotonic() < deadline:
                snapshot = call("/v1/knowledge/snapshots", {"operation_id": "observe-" + str(index)})
                index += 1
                for ref in snapshot["snapshot"]["revisions"]:
                    record = call("/v1/knowledge/revisions/" + ref["content_hash"].split(":")[1])
                    write = record["revision"]["write"]
                    if (write["kind"], write["key"]) == ("notes", "strategy"):
                        factor = int(base64.b64decode(record["files"][0]["base64"]))
                        sources.append(ref)
                        Path("learned.json").write_text(json.dumps(record))
                        break
                if factor is None:
                    time.sleep(.025)
            assert factor == 2
        source = ("import json, sys\nr=json.load(sys.stdin)\n"
                  f"print(json.dumps({{'output':r['task']['input']*{factor},'state':r['state']}}))\n")
        candidate.write_text(source)
        ticket = call("/v1/submissions", {"operation_id": "proposal-" + str(done),
            "evaluation_id": "validation", "files": [file("main.py", candidate.read_bytes())]})
        Path(f"ticket-{done}.json").write_text(json.dumps(ticket))
        while True:
            result = call("/v1/submissions/" + ticket["ticket_id"])
            if result["status"] in ("completed", "failed"):
                Path(f"result-{done}.json").write_text(json.dumps(result))
                if result["status"] == "completed" and worker == "b":
                    sources.append(result["result"]["shared_attempt"])
                    publish("skills", "double-input", [file("SKILL.md", b"Multiply by the shared factor.\n")], sources)
                break
            time.sleep(.025)

    if "--exit-first" not in sys.argv or launch["continuity"] != "fresh":
        while not interrupted:
            time.sleep(.025)
    print(json.dumps({"event": "result", "session_id": session, "resumed_from": launch["session_id"],
        "usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}}), flush=True)


if __name__ == "__main__":
    main()
