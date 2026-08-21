import json

from proteus.core.adapter import ActionEvent
from proteus.core.continuity import HandoffStore, PROTOCOL_VERSION


def test_agent_handoff_is_archived_redacted_and_carried_to_next_phase(tmp_path):
    store = HandoffStore(tmp_path)
    observe = store.begin(1, "observe")
    store.current.write_text(
        "# Findings\nAudio is rejected.\n\n# Next action\nInspect src/audio.ts.\n"
        "api_key=sk-abcdefghijklmnop\n",
        encoding="utf-8",
    )
    record = store.finish(observe)

    assert record["source"] == "agent"
    assert "Audio is rejected" in record["content"]
    assert "sk-abcdefghijklmnop" not in record["content"]
    assert (store.history / "ep001" / "observe.md").exists()
    archived = json.loads((store.history / "ep001" / "observe.json").read_text())
    assert archived["protocol_version"] == PROTOCOL_VERSION

    propose = store.begin(1, "propose")
    assert "Audio is rejected" in propose.previous
    assert "Audio is rejected" in store.current.read_text()


def test_fallback_uses_normalized_actions_not_reasoning_or_results(tmp_path):
    store = HandoffStore(tmp_path)
    start = store.begin(1, "observe")
    events = [
        ActionEvent(turn=1, phase="observe", tool=None,
                    text="private chain of thought and huge private output"),
        ActionEvent(turn=2, phase="observe", tool="read", surface="loop",
                    params={"file_path": "/workspace/src/audio.ts"}),
        ActionEvent(turn=3, phase="observe", tool="bash",
                    params={"command": "curl -H 'Bearer abcdefghijklmnop' /health"}),
    ]
    record = store.finish(start, events, interrupted=True)

    assert record["source"] == "framework-fallback"
    assert record["interrupted"] is True
    assert "/workspace/src/audio.ts" in record["content"]
    assert "private chain of thought" not in record["content"]
    assert "huge private output" not in record["content"]
    assert "abcdefghijklmnop" not in record["content"]
    assert "interrupted" in record["content"]


def test_reflect_handoff_crosses_the_episode_boundary_with_history(tmp_path):
    store = HandoffStore(tmp_path)
    reflect = store.begin(1, "reflect")
    store.current.write_text(
        "# Tests\nTypecheck passed.\n\n# Next action\nAdd the transcription provider.\n",
        encoding="utf-8",
    )
    store.finish(reflect)

    next_observe = store.begin(2, "observe")
    assert "Typecheck passed" in next_observe.previous
    assert (store.history / "ep001" / "reflect.json").exists()
    assert not (store.history / "ep002" / "observe.json").exists()


def test_retried_phase_preserves_the_first_attempt(tmp_path):
    store = HandoffStore(tmp_path)
    first = store.begin(1, "observe")
    store.finish(first, interrupted=True)
    original = (store.history / "ep001" / "observe.json").read_text()

    retry = store.begin(1, "observe")
    store.current.write_text("# Findings\nRetry completed.\n", encoding="utf-8")
    record = store.finish(retry)

    assert (store.history / "ep001" / "observe.json").read_text() == original
    assert (store.history / "ep001" / "observe-02.json").exists()
    assert record["attempt"] == 2


def test_dsh_normalization_excludes_reasoning_blocks(tmp_path, monkeypatch):
    import proteus.adapters.dsh as dsh

    native = "\n".join([
        json.dumps({"type": "assistant/message", "data": {"message": {"content": [
            {"type": "reasoning", "text": "hidden reasoning"},
            {"type": "text", "text": "visible summary"},
        ]}}}),
        json.dumps({"type": "tool/call", "data": {
            "name": "read", "arguments": '{"file_path":"/workspace/src/audio.ts"}',
        }}),
    ]).encode()
    session = tmp_path / "session"
    session.mkdir()
    (session / "session.jsonl.zstd").write_bytes(b"placeholder")
    monkeypatch.setattr(dsh, "_zstd_partial", lambda _: native)

    adapter = dsh.DshHarness(key="x", sandbox=object())
    events = adapter._session_trace(session, "observe", partial=True)
    combined = " ".join(event.text for event in events)
    assert "visible summary" in combined
    assert "hidden reasoning" not in combined
    assert any(event.tool == "read" for event in events)
