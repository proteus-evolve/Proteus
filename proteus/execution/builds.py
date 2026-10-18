"""Candidate-bound Docker builds, immutable receipts and verified image reuse."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from proteus.artifacts.store import publish_bytes
from proteus.core.contracts import DIGEST, TEXT, ArtifactRef, Usage, require
from proteus.core.ledger import Amounts
from proteus.core.run_lock import run_lock
from proteus.core.serialization import Contract, canonical_json, content_hash
from proteus.evaluation.records import zero_usage
from proteus.evaluation.runners import InvalidCandidate
from proteus.execution.harbor_worker import docker


def image_pin(value):
    require(re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", value) is not None,
            "source builds require repository@sha256 image pins")


@dataclass(frozen=True, kw_only=True)
class SourceBuildSpec(Contract):
    commands: tuple[str, ...] = field(metadata={"minItems": 1})
    builder_image: str | None = field(default=None, metadata=TEXT)
    platform: str = field(default="linux/amd64", metadata={"pattern": r"^linux/(amd64|arm64)(/v[0-9]+)?$"})
    network: Literal["none", "default"] = "none"
    timeout_seconds: float = field(default=600, metadata={"exclusiveMinimum": 0})
    reuse: bool = True

    def _validate(self):
        require(all(command.strip() and "\0" not in command for command in self.commands), "empty/invalid build command")
        if self.builder_image is not None:
            image_pin(self.builder_image)


@dataclass(frozen=True, kw_only=True)
class BuildReceipt(Contract):
    id: str = field(metadata=TEXT)
    artifact_hash: str = field(metadata=DIGEST)
    build_hash: str = field(metadata=DIGEST)
    input_hash: str | None = field(default=None, metadata=DIGEST)
    base_image: str = field(metadata=TEXT)
    dockerfile_hash: str = field(metadata=DIGEST)
    builder: Mapping[str, Any] = field(default_factory=dict)
    image_id: str | None = field(default=None, metadata=DIGEST)
    cache_hit: bool = False
    cached_from: str | None = field(default=None, metadata=DIGEST)
    status: Literal["completed", "failed"]
    usage: Usage
    diagnostic: str = ""

    def _validate(self):
        require((self.status == "completed") == (self.image_id is not None), "build result/image mismatch")
        require(self.cache_hit == (self.cached_from is not None), "cache reuse must identify its receipt")


def dockerfile(spec: SourceBuildSpec, base_image: str) -> str:
    image_pin(base_image)
    # Only /opt/proteus/build crosses from the build stage into a fresh runtime.
    # Build scripts cannot replace the runtime's verifier/tools or source witness.
    lines = [f"FROM {spec.builder_image or base_image} AS candidate_build", "WORKDIR /opt/proteus/build",
             "COPY source/ /opt/proteus/build/"]
    lines.extend("RUN " + json.dumps(["/bin/sh", "-eu", "-c", command]) for command in spec.commands)
    lines.extend([f"FROM {base_image}", "COPY --from=candidate_build /opt/proteus/build/ /opt/proteus/build/",
                  "COPY source/ /opt/proteus/source/", "WORKDIR /opt/proteus/build"])
    return "\n".join(lines) + "\n"


def builder_identity():
    server = json.loads(docker("version", "--format", "{{json .Server}}"))
    # inspect has no JSON/--format interface. Keep only stable, unambiguous
    # fields; timestamps and host endpoint strings are not build inputs.
    report = docker("buildx", "inspect", "default")
    fields = {}
    for line in report.splitlines():
        key, separator, value = line.strip().partition(":")
        if key == "BuildKit version":
            key = "BuildKit"
        if separator and key in {"Driver", "BuildKit", "Platforms"}:
            require(key not in fields, "expected a single-node local Docker builder")
            fields[key] = value.strip()
    require(fields.get("Driver") == "docker", "source builds currently require the local Docker driver")
    require(bool(fields.get("BuildKit")) and bool(fields.get("Platforms")), "Docker builder identity is unavailable")
    return {"server": {key: server[key] for key in ("Version", "ApiVersion", "Os", "Arch")},
            "buildx": docker("buildx", "version"), "driver": fields["Driver"],
            "buildkit": fields["BuildKit"], "platforms": sorted(p.strip() for p in fields["Platforms"].split(","))}


def build_key(artifact, spec, base_image, builder):
    return content_hash({"artifact": artifact, "build": spec, "base_image": base_image,
                         "builder": builder, "dockerfile": dockerfile(spec, base_image)})


def verify_image(image_id, key, artifact, spec):
    row, = json.loads(docker("image", "inspect", image_id))
    require(row["Id"] == image_id, "built image ID changed")
    labels = row["Config"].get("Labels") or {}
    require(labels.get("org.proteus.build") == key and labels.get("org.proteus.source") == artifact.content_hash
            and labels.get("org.proteus.build-spec") == content_hash(spec), "built image provenance differs")
    parts = spec.platform.split("/")
    require(row["Os"] == parts[0] and row["Architecture"] == parts[1]
            and (len(parts) < 3 or row.get("Variant") == parts[2]), "built image platform differs")


def run_build(args, log, timeout):
    keys = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "DOCKER_HOST", "DOCKER_CONTEXT",
            "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG"}
    env = {key: value for key, value in os.environ.items() if key in keys}
    with log.open("wb") as stream:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except BaseException:
            # BuildKit cancels its solve when the client connection closes. A
            # killed/uncertain solve never publishes a reusable image receipt.
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
            finally:
                # An exited CLI can leave a child buildx process behind.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise
    if process.returncode:
        with log.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 65536))
            tail = stream.read().decode("utf-8", errors="replace")
        message = "source build failed; controller-private build.log contains diagnostics"
        if re.search(r'process ".+" did not complete successfully: exit code: [1-9][0-9]*', tail):
            raise InvalidCandidate(message)
        raise RuntimeError(message)  # Transport/daemon failures are not candidate failures.


class BuildService:
    def __init__(self, services):
        self.services = services
        self.root = services.root / "private/builds"
        self.records = []

    def execute(self, artifact: ArtifactRef, spec: SourceBuildSpec, base_image: str, *, operation_id, attempt_id, trial_id):
        services = self.services
        require(threading.get_ident() == services._dispatch_thread, "builds require serial service dispatch")
        require(type(spec) is SourceBuildSpec and content_hash(spec) == services.recipe.execution.environment.build_hash
                and base_image == services.recipe.execution.environment.image_digest, "build differs from the locked recipe")
        require(any(c.artifact == artifact for c in services.candidates), "build source must be a run candidate")
        require(artifact.codec.name == "proteus.file_tree", "source builds require a file-tree candidate")
        definition = dockerfile(spec, base_image)
        entry = "build-" + content_hash([operation_id, attempt_id, trial_id]).split(":")[1]
        services.ledger.reserve(entry, scopes={"run": services.recipe.budget}, reservation=Amounts(), slots=0,
            metadata={"kind": "source_build", "role": "builder", "operation_id": operation_id,
                      "attempt_id": attempt_id, "trial_id": trial_id, "worker_id": None},
            request={"artifact": artifact, "build": spec, "base_image": base_image}, reuse=False)
        services.ledger.start(entry)
        started = time.monotonic()
        builder, key, image_id, cached_from = {}, None, None, None
        dispatched = False
        error = None
        attempt = self.root / "attempts" / entry
        try:
            attempt.mkdir(parents=True, exist_ok=False)
            publish_bytes(attempt / "Dockerfile", definition.encode())
            builder = builder_identity()
            key = build_key(artifact, spec, base_image, builder)
            cache = self.root / "cache" / key.split(":")[1]
            with run_lock(cache):
                if spec.reuse:
                    for path in sorted(cache.glob("*.json")):
                        previous = BuildReceipt.from_json(path.read_text())
                        require(content_hash(previous) == "sha256:" + path.stem and previous.input_hash == key
                                and previous.artifact_hash == artifact.content_hash
                                and previous.build_hash == content_hash(spec) and previous.base_image == base_image
                                and previous.dockerfile_hash == content_hash(definition)
                                and previous.status == "completed",
                                "corrupt build cache receipt")
                        try:
                            verify_image(previous.image_id, key, artifact, spec)
                        except subprocess.CalledProcessError as exc:
                            if "No such image" in (exc.stderr or ""):
                                continue
                            raise
                        image_id, cached_from = previous.image_id, content_hash(previous)
                        break
                if image_id is None:
                    context = attempt / "context"
                    services.artifacts.materialize(artifact, context / "source")
                    publish_bytes(context / "Dockerfile", definition.encode())
                    args = ["docker", "buildx", "build", "--builder", "default", "--load", "--progress", "plain",
                            "--network", spec.network, "--platform", spec.platform,
                            "--iidfile", str(attempt / "image-id"),
                            "--label", "org.proteus.build=" + key,
                            "--label", "org.proteus.source=" + artifact.content_hash,
                            "--label", "org.proteus.build-spec=" + content_hash(spec)]
                    # Override Docker config's automatic proxy injection: build
                    # scripts receive no host proxy credentials or subject keys.
                    for name in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY"):
                        for spelling in (name, name.lower()):
                            args.extend(["--build-arg", spelling + "="])
                    if not spec.reuse:
                        args.append("--no-cache")
                    args.append(str(context))
                    publish_bytes(attempt / "command.json", canonical_json(args).encode())
                    dispatched = True
                    run_build(args, attempt / "build.log", spec.timeout_seconds)
                    image_id = (attempt / "image-id").read_text().strip()
                    verify_image(image_id, key, artifact, spec)
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            image_id, cached_from = None, None
            raise
        finally:
            elapsed = time.monotonic() - started
            usage = Usage(wall_seconds=elapsed) if dispatched else zero_usage(wall_seconds=elapsed)
            receipt = BuildReceipt(id=entry, artifact_hash=artifact.content_hash, build_hash=content_hash(spec),
                base_image=base_image, dockerfile_hash=content_hash(definition), input_hash=key, builder=builder,
                image_id=image_id, cache_hit=cached_from is not None, cached_from=cached_from,
                status="failed" if error else "completed", usage=usage, diagnostic=error or "")
            self.records.append(receipt)
            services.ledger.settle(entry, actual=Amounts.from_usage(usage), status=receipt.status,
                                   result={"receipt": receipt})
            publish_bytes(attempt / "receipt.json", canonical_json(receipt).encode())
        if not receipt.cache_hit:
            publish_bytes(self.root / "cache" / key.split(":")[1] / (content_hash(receipt).split(":")[1] + ".json"),
                          canonical_json(receipt).encode())
        return receipt
