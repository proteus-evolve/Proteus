"""Standalone stdlib supervisor; survives an orchestration-process hard exit.

Only this process signals its own child process group. No resumed controller
signals a PID recovered from a file. A supervisor loss without a final receipt
is uncertain and does not authorize launching the generation again.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def write(path, value):
    temporary = path.with_suffix(".pending")
    with temporary.open("w") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def send(child, sig):
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass


def protocol_result(path):
    result, session = None, None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # ordinary progress text is retained in the log
            if not isinstance(record, dict):
                continue
            if record.get("event") == "protocol_error":
                raise ValueError("runtime rejected its provider protocol")
            if record.get("event") == "session":
                if set(record) != {"event", "session_id", "resumed_from"}:
                    raise ValueError("invalid worker session event")
                if not isinstance(record["session_id"], str) or not record["session_id"].strip():
                    raise ValueError("invalid worker session identity")
                previous = record["resumed_from"]
                if previous is not None and (not isinstance(previous, str) or not previous.strip()):
                    raise ValueError("invalid worker preceding session")
                if session is not None and session != record:
                    raise ValueError("runtime changed its confirmed session")
                if result is not None:
                    raise ValueError("session confirmation follows a final result")
                session = record
            if record.get("event") == "result":
                if result is not None:
                    raise ValueError("runtime produced multiple final result events")
                result = record
    if result is None:
        return None, session
    if set(result) - {"event", "session_id", "resumed_from", "usage", "session_usage"}:
        raise ValueError("unknown worker result fields")
    for key in ("session_id", "resumed_from"):
        value = result.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("invalid worker session identity")
    usage = result.get("usage", {})
    if not isinstance(usage, dict) or set(usage) - {"model_calls", "input_tokens", "output_tokens", "cost_usd"}:
        raise ValueError("invalid worker usage fields")
    for key, value in usage.items():
        if value is None:
            continue
        expected = (int, float) if key == "cost_usd" else (int,)
        if type(value) not in expected or not math.isfinite(value) or value < 0:
            raise ValueError("invalid worker usage value")
    if session is not None and any(result.get(key) != session[key] for key in ("session_id", "resumed_from")):
        raise ValueError("final result differs from the confirmed worker session")
    totals = result.get("session_usage")
    if totals is not None:
        if not result.get("session_id") or not isinstance(totals, dict) or set(totals) != {"input_tokens", "output_tokens"}:
            raise ValueError("invalid cumulative session usage")
        if any(value is not None and (type(value) is not int or value < 0) for value in totals.values()):
            raise ValueError("invalid cumulative session token count")
    return result, session


def main():
    request_path, descriptor = Path(sys.argv[1]), int(sys.argv[2])
    # Inherited flock holds continuously from before Popen until final commit.
    os.fstat(descriptor)
    root = request_path.parent
    request = json.loads(request_path.read_text())
    request_hash = digest(request)
    if (root / "dispatch.json").exists():
        raise ValueError("worker generation was already dispatched")
    write(root / "dispatch.json", {"request_hash": request_hash})
    execution = json.loads((root / "execution.json").read_text())
    if execution["request_hash"] != request_hash:
        raise ValueError("worker executor belongs to another launch")
    options = execution["options"]
    started, child, action, stopped_at, escalation = time.monotonic(), None, None, None, 0
    diagnostic, return_code, timed_out = None, None, False
    usage = {"schema_version": 1, "model_calls": None, "input_tokens": None,
             "output_tokens": None, "cost_usd": None, "wall_seconds": None}
    stdout, stderr = root / "stdout.log", root / "stderr.log"
    with stdout.open("wb") as output, stderr.open("wb") as error:
        try:
            if any("sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest() != value
                   for path, value in options["command_files"].items()):
                raise ValueError("worker command file changed before dispatch")
            payload = {key: request[key] for key in ("identity", "operation_id", "prompt", "session_id",
                                                    "continuity", "fallback_reason", "session_usage")}
            # A regular input file cannot block this supervisor if a bridge
            # stops reading a prompt larger than a pipe buffer. The deadline
            # still applies while that bridge is unresponsive.
            write(root / "input.json", payload)
            with (root / "input.json").open("rb") as input_stream:
                child = subprocess.Popen(options["command"], cwd=request["workspace"],
                    stdin=input_stream, stdout=output, stderr=error, start_new_session=True)
            while child.poll() is None:
                now = time.monotonic()
                control = root / "control.json"
                if control.exists():
                    desired = json.loads(control.read_text())
                    if desired.get("request_hash") != request_hash or desired.get("action") not in ("pause", "stop"):
                        raise ValueError("invalid worker control identity")
                    action = desired["action"]
                if now - started >= options["timeout_seconds"]:
                    timed_out = True
                if (action is not None or timed_out) and stopped_at is None:
                    send(child, signal.SIGINT)
                    stopped_at = now
                if stopped_at is not None:
                    elapsed, grace = now - stopped_at, options["shutdown_grace_seconds"]
                    if elapsed >= grace and escalation == 0:
                        send(child, signal.SIGTERM)
                        escalation = 1
                    if elapsed >= 2 * grace and escalation == 1:
                        send(child, signal.SIGKILL)
                        escalation = 2
                time.sleep(0.025)
            return_code = child.wait()
        except BaseException as exc:
            diagnostic = "worker launch/supervision failed: " + type(exc).__name__
            if child is None:
                usage.update(model_calls=0, input_tokens=0, output_tokens=0, cost_usd=0)
        finally:
            if child is not None:
                # Drain descendants belonging to this generation before sealing
                # output. The private controller database is never passed in stdin.
                send(child, signal.SIGKILL)
                return_code = child.wait()
                if child.stdin is not None and not child.stdin.closed:
                    child.stdin.close()
            output.flush()
            error.flush()
            os.fsync(output.fileno())
            os.fsync(error.fileno())
    final, session = None, None
    try:
        final, session = protocol_result(stdout)
    except ValueError as exc:
        diagnostic = str(exc)
    if final is not None:
        usage.update(final.get("usage", {}))
    usage["wall_seconds"] = time.monotonic() - started
    if timed_out:
        diagnostic = "worker generation exceeded its timeout"
    confirmation = final or session or {}
    if request["continuity"] == "native" and confirmation.get("resumed_from") != request["session_id"]:
        diagnostic = "runtime did not confirm the requested native session"
    if diagnostic is not None:
        status = "failed"
    elif action is not None:
        status = "paused" if action == "pause" else "stopped"
    elif return_code == 0 and final is not None:
        status = "completed"
    else:
        status = "failed"
        diagnostic = "worker exited without a successful final result"
    receipt = {"schema_version": 1, "request_hash": request_hash, "status": status,
        "return_code": return_code, "session_id": confirmation.get("session_id"),
        "resumed_from": confirmation.get("resumed_from"), "usage": usage,
        "diagnostic": diagnostic, "control": action,
        "session_usage": {"schema_version": 1, **final["session_usage"]}
            if final is not None and final.get("session_usage") is not None else None,
        "stdout_hash": "sha256:" + hashlib.sha256(stdout.read_bytes()).hexdigest(),
        "stderr_hash": "sha256:" + hashlib.sha256(stderr.read_bytes()).hexdigest()}
    write(root / "completion.json", receipt)
    os.close(descriptor)


if __name__ == "__main__":
    main()
