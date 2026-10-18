"""Worker-side submission client. Credentials are read from the worker environment."""

import argparse
import json
import os
from pathlib import Path
import stat
import time
import urllib.error
import urllib.request

from proteus.artifacts.codecs import FileWrite
from proteus.core.contracts import require
from proteus.core.serialization import canonical_json, read_json
from proteus.gateway.records import GatewaySubmission


class GatewayClient:
    def __init__(self, url=None, token=None):
        self.url = (url or os.environ["PROTEUS_EVALUATION_URL"]).rstrip("/")
        self.token = token or os.environ["PROTEUS_EVALUATION_TOKEN"]

    def _request(self, path, body=None):
        request = urllib.request.Request(self.url + path, method="GET" if body is None else "POST",
            data=None if body is None else canonical_json(body).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return read_json(response.read().decode())

    def submit(self, submission):
        return self._request("/v1/submissions", submission.to_dict())

    def poll(self, ticket_id):
        return self._request("/v1/submissions/" + ticket_id)

    def wait(self, ticket_id, *, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self.poll(ticket_id)
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError):
                # A controller restart can temporarily close the fixed endpoint.
                time.sleep(0.1)
                continue
            if result["status"] in ("completed", "failed"):
                return result
            time.sleep(0.1)
        raise TimeoutError("evaluation remains pending; retain its ticket and poll again")


def snapshot(directory):
    root = Path(directory).resolve()
    require(root.is_dir(), "candidate directory does not exist")

    def capture():
        records = []
        for path in sorted(root.rglob("*")):
            mode = path.lstat().st_mode
            require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "candidate snapshot rejects symlinks and special files")
            if stat.S_ISREG(mode):
                records.append(FileWrite.from_bytes(path.relative_to(root).as_posix(), path.read_bytes(),
                                                    executable=bool(mode & 0o111)))
        return tuple(records)
    before = capture()
    require(capture() == before, "candidate changed while preparing its submission")
    return before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    submit = subparsers.add_parser("submit")
    submit.add_argument("directory", type=Path)
    submit.add_argument("--operation", required=True)
    submit.add_argument("--evaluation", required=True)
    poll = subparsers.add_parser("poll")
    poll.add_argument("ticket")
    wait_parser = subparsers.add_parser("wait")
    wait_parser.add_argument("ticket")
    wait_parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    client = GatewayClient()
    if args.action == "submit":
        result = client.submit(GatewaySubmission(operation_id=args.operation,
            evaluation_id=args.evaluation, files=snapshot(args.directory)))
    elif args.action == "poll":
        result = client.poll(args.ticket)
    else:
        result = client.wait(args.ticket, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
