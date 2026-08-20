from pathlib import Path

from proteus.bench.dsh_audio import capability_gates


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_empty_tree_scores_no_audio_gates(tmp_path):
    assert not any(capability_gates(tmp_path).values())


def test_cross_layer_audio_shape_scores_every_gate(tmp_path):
    _write(tmp_path, "packages/llm/llm/src/types.ts", """
        interface AudioBlock { type: 'audio' }
        interface ContentBlockMap { 'audio': AudioBlock }
    """)
    _write(tmp_path, "packages/attachment/audio.ts", """
        type AudioMediaType = 'audio/wav'; interface AudioAttachmentRef {}
        function saveAudio() {} function readAudio() {}
    """)
    _write(tmp_path, "packages/acp/acp/src/content.ts", """
        switch (block.type) { case 'audio': return persistAudio(block) }
    """)
    _write(tmp_path, "packages/client/ui-attachment/audio.tsx", """
        // composer draft audio attachment drop target: audio/wav
        export const AudioPlayer = () => <audio controls />
    """)
    _write(tmp_path, "packages/transcription/service.ts", """
        interface TranscriptionProvider { transcribeAudio(): Promise<string> }
    """)
    for area in ("attachment", "acp", "client", "llm"):
        _write(tmp_path, f"packages/{area}/tests/audio.spec.ts",
               "test('AudioAttachment transcription', () => {})")
    assert all(capability_gates(tmp_path).values())


def test_acp_rejection_does_not_count_as_admission(tmp_path):
    _write(tmp_path, "packages/acp/acp/src/content.ts", """
        case 'audio': throw new Error('audio prompt content is not supported')
        function persistAudio() {}
    """)
    assert not capability_gates(tmp_path)["ACP admission"]
