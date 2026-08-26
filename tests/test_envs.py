"""Prepared-environment scaffolding and build metadata."""

import subprocess
from pathlib import Path

import pytest

from proteus import envs


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    _git(repo, "add", "Dockerfile")
    _git(
        repo,
        "-c",
        "user.name=Proteus Test",
        "-c",
        "user.email=test@proteus",
        "commit",
        "-qm",
        "seed",
    )
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_scaffold_writes_manifest_and_local_dockerfile(tmp_path):
    root = tmp_path / "environments"
    manifest = envs.scaffold(
        "https://example.com/acme/harness.git",
        "acme",
        ref="v1.2.3",
        env_root=root,
        use_local_dockerfile=True,
    )

    assert manifest == root / "acme" / "environment.toml"
    data = envs._toml(manifest)
    assert data["environment"] == {"name": "acme", "image": "", "network": "none"}
    assert data["source"] == {
        "repo": "https://example.com/acme/harness.git",
        "ref": "v1.2.3",
        "use_local_dockerfile": True,
    }
    assert data["harness"] == {"adapter": "", "workspace_mount": "/workspace"}
    assert (manifest.parent / "Dockerfile").read_text(encoding="utf-8").startswith(
        "# Wrapper Dockerfile"
    )


def test_scaffold_refuses_to_replace_an_existing_manifest(tmp_path):
    root = tmp_path / "environments"
    manifest = envs.scaffold("https://example.com/harness.git", "acme", env_root=root)

    with pytest.raises(FileExistsError, match="environment.toml already exists"):
        envs.scaffold("https://example.com/other.git", "acme", env_root=root)

    assert envs._toml(manifest)["source"]["repo"] == "https://example.com/harness.git"


def test_build_records_resolved_sha_and_image_tag(tmp_path, monkeypatch):
    source, sha = _source_repo(tmp_path)
    root = tmp_path / "environments"
    manifest = envs.scaffold(str(source), "acme", env_root=root)
    real_run = subprocess.run
    docker_call = {}

    def fake_run(argv, **kwargs):
        if argv[:2] == ["docker", "build"]:
            docker_call["argv"] = argv
            docker_call["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, stdout=b"image-id\n", stderr=b"")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(envs.subprocess, "run", fake_run)

    tag = envs.build("acme", env_root=root)

    assert tag == f"proteus-env-acme:{sha[:12]}"
    data = envs._toml(manifest)
    assert data["environment"]["image"] == tag
    assert data["source"]["resolved_sha"] == sha
    argv = docker_call["argv"]
    assert argv[:3] == ["docker", "build", "-q"]
    assert argv[argv.index("-t") + 1] == tag
    assert Path(argv[argv.index("-f") + 1]).name == "Dockerfile"
    assert Path(argv[-1]).name == "src"
    assert docker_call["kwargs"]["check"] is True
    assert docker_call["kwargs"]["capture_output"] is True


def test_build_reports_a_missing_dockerfile(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("no container recipe\n", encoding="utf-8")
    root = tmp_path / "environments"
    envs.scaffold(str(source), "acme", env_root=root)

    with pytest.raises(FileNotFoundError, match="no Dockerfile"):
        envs.build("acme", env_root=root)
