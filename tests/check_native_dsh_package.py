"""Installed DSH adapter accounting using deterministic compressed session logs."""

import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from proteus.adapters.dsh import DshHarness
from proteus.core.adapter import EpisodeSpec
from proteus.core.native_inputs import input_directory, read_native_inputs
from proteus.core.native_usage import summarize_native_inputs


def compress(data):
    try:
        from compression import zstd

        return zstd.compress(data)
    except ImportError:
        import zstandard

        return zstandard.ZstdCompressor(write_content_size=False).compress(data)


def usage(incoming, outgoing, read=0, write=0):
    return {
        "inputTokens": incoming,
        "outputTokens": outgoing,
        "cacheReadTokens": read,
        "cacheWriteTokens": write,
        "reasoningTokens": 1,
    }


def events():
    result = [{"type": "turn/start", "data": {"turn": 1}}]
    for step, incoming, outgoing, read, write in ((1, 11, 4, 2, 0), (2, 5, 2, 1, 1)):
        key = {"turn": 1, "step": step}
        result.extend(
            [
                {"type": "step/start", "data": key},
                {
                    "type": "assistant/chunk",
                    "data": {
                        **key,
                        "chunk": {
                            "type": "usage",
                            "usage": usage(incoming - 1, outgoing, read, write),
                        },
                    },
                },
                {
                    "type": "assistant/chunk",
                    "data": {
                        **key,
                        "chunk": {
                            "type": "finish",
                            "reason": {"kind": "tool-calls" if step == 1 else "stop"},
                        },
                    },
                },
                {
                    "type": "assistant/message",
                    "data": {
                        **key,
                        "usage": usage(incoming, outgoing, read, write),
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "PRIVATE-RESPONSE"}],
                        },
                    },
                },
                {"type": "step/end", "data": key},
            ]
        )
    result.extend(
        [
            {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
            {"type": "compaction/start", "data": {"compactionId": "summary", "turn": None}},
            {
                "type": "compaction/summary",
                "data": {
                    "compactionId": "summary",
                    "usage": usage(3, 1, 1),
                    "llmStreamCall": True,
                    "summary": [{"type": "text", "text": "PRIVATE-SUMMARY"}],
                },
            },
            {"type": "compaction/end", "data": {"compactionId": "summary", "turn": None}},
        ]
    )
    return result


def raw(values=None, *, identity=None, header=None):
    metadata = {
        "type": "session",
        "version": 0,
        "id": identity or str(uuid.uuid4()),
        "createdAt": 100,
        "delegationDepth": 0,
        **(header or {}),
    }
    rows = [
        metadata,
        *[
            {**event, "seq": index, "time": 100 + index}
            for index, event in enumerate(events() if values is None else values)
        ],
    ]
    return b"".join((json.dumps(row) + "\n").encode() for row in rows)


class Sandbox:
    def run(self, root, command, env, timeout_s, mounts=(), stop_check=None):
        identity = str(uuid.uuid4())
        log = root / ".dsh-state/sessions/project" / identity / "session.jsonl.zstd"
        log.parent.mkdir(parents=True)
        log.write_bytes(compress(raw(identity=identity)))
        return subprocess.CompletedProcess(command, 0, "", "")


def check():
    with tempfile.TemporaryDirectory(prefix="proteus-native-dsh-") as directory:
        root = Path(directory)
        (root / "harness").mkdir()
        adapter = DshHarness(key="fixture", sandbox=Sandbox())
        request = EpisodeSpec(
            root=root, episode=1, model="fixture", phases=("act",), phase_prompts={"act": "fixture"}
        )
        assert adapter.run_episode(request).ok and adapter.run_episode(request).ok
        records = read_native_inputs(root)
        assert len(records) == 2
        totals = summarize_native_inputs(records)
        assert (
            totals["reported"]["input_tokens"] == 48 and totals["reported"]["output_tokens"] == 14
        )
        assert totals["reported"]["model_calls"] is None and totals["reported"]["cost_usd"] is None
        assert all(
            record["outcome"]["usage"]["fields"]["samples"]
            == {"steps": 2, "summaries": 1, "unreported": 0}
            for record in records
        )
        assert len(list(input_directory(root).glob("*.usage-source"))) == 2
        assert "PRIVATE-RESPONSE" not in json.dumps(
            records
        ) and "PRIVATE-SUMMARY" not in json.dumps(totals)
        print(
            "Installed DSH adapter: fresh compressed sessions, latest step usage, summary/cache tokens and private durable sources passed"
        )


if __name__ == "__main__":
    check()
