"""Offline trusted bridge for lifecycle examples; no provider/model calls."""

import json
from pathlib import Path
import signal
import sys
import time


def main():
    request = json.loads(sys.stdin.readline())
    session = Path("session.json")
    if request["session_id"] is not None:
        state = json.loads(session.read_text())
        if state["id"] != request["session_id"]:
            raise ValueError("requested native session is unavailable")
    else:
        state = {"id": request["identity"]["worker_id"] + ":" + request["operation_id"], "turns": 0}
    state["turns"] += 1
    session.write_text(json.dumps(state))
    with Path("launches.jsonl").open("a") as stream:
        stream.write(json.dumps(request) + "\n")
    stopped = False

    def interrupt(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, interrupt)
    Path("ready-" + request["operation_id"]).touch()
    if request["prompt"] == "wait for intervention":
        while not stopped:
            time.sleep(0.01)
    print(json.dumps({"event": "result", "session_id": state["id"], "resumed_from": request["session_id"],
                      "usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}}), flush=True)


if __name__ == "__main__":
    main()
