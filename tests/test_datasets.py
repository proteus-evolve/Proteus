"""Verified official benchmark download behavior, fully offline."""

import hashlib
import io
from unittest.mock import patch


def test_verified_download_caches_only_a_matching_valid_payload(tmp_path):
    from proteus.bench._datasets import download_verified

    payload = b'[{"task_id": 1}]'
    cache = tmp_path / "dataset.json"
    validated = []

    with patch("proteus.bench._datasets.request.urlopen", return_value=io.BytesIO(payload)) as get:
        first = download_verified(
            name="fixture",
            url="https://example.invalid/pinned.json",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            cache=cache,
            validate=lambda path: validated.append(path.read_bytes()),
        )
        second = download_verified(
            name="fixture",
            url="https://example.invalid/pinned.json",
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            cache=cache,
            validate=lambda path: validated.append(path.read_bytes()),
        )

    assert first == second == cache
    assert cache.read_bytes() == payload
    assert validated == [payload]
    assert get.call_count == 1


def test_verified_download_rejects_a_checksum_mismatch_without_publishing_cache(tmp_path):
    from proteus.bench._datasets import download_verified

    cache = tmp_path / "dataset.json"
    with patch("proteus.bench._datasets.request.urlopen", return_value=io.BytesIO(b"changed")):
        try:
            download_verified(
                name="fixture",
                url="https://example.invalid/pinned.json",
                expected_sha256="0" * 64,
                cache=cache,
                validate=lambda path: None,
            )
        except ValueError as exc:
            assert "fixture checksum mismatch" in str(exc)
            assert "expected" in str(exc) and "got" in str(exc)
        else:
            raise AssertionError("mismatched download was accepted")

    assert not cache.exists()
    assert list(tmp_path.iterdir()) == []


def test_verified_download_removes_valid_payload_when_validation_raises(tmp_path):
    from proteus.bench._datasets import download_verified

    payload = b'[{"task_id": 1}]'
    cache = tmp_path / "dataset.json"
    temporary = []

    def reject(path):
        temporary.append(path)
        assert path.read_bytes() == payload
        raise ValueError("invalid dataset format")

    with patch("proteus.bench._datasets.request.urlopen", return_value=io.BytesIO(payload)):
        try:
            download_verified(
                name="fixture",
                url="https://example.invalid/pinned.json",
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                cache=cache,
                validate=reject,
            )
        except ValueError as exc:
            assert str(exc) == "invalid dataset format"
        else:
            raise AssertionError("invalid dataset was accepted")

    assert len(temporary) == 1 and not temporary[0].exists()
    assert not cache.exists()
    assert list(tmp_path.iterdir()) == []


def test_official_dataset_urls_and_hashes_are_immutable():
    from proteus.bench import humaneval, mbpp

    assert "6d43fb980f9fee3c892a914eda09951f772ad10d" in humaneval.DATA_URL
    assert humaneval.DATA_SHA256 == "b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef"
    assert "e20eb00d074cdb569ee27318f112ea1e85bbb98f" in mbpp.DATA_URL
    assert mbpp.DATA_SHA256 == "ca95deaa9a01ef0a6f439f88bcf0dd3db3563d22f22aad6cae04ebb9a8d8c8e9"
    assert "/master/" not in humaneval.DATA_URL
    assert "/master/" not in mbpp.DATA_URL
