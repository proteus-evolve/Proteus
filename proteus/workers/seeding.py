"""One-time editable seed copies, with a durable intent before any workspace writes."""

import os
import shutil

from proteus.artifacts.store import _sync_directory
from proteus.core.contracts import DataRef, require
from proteus.core.recovery import _load, _write
from proteus.core.serialization import content_hash
from proteus.workers.records import WorkerSeed


KIND = "proteus.worker.seed@1"


def locations(controller, directory):
    key = content_hash(directory).split(":")[1]
    return (controller.private / "seeds" / key,
            controller.workspace / directory, controller.workspace / (".proteus-seed-" + key))


def seed(worker, operation_id, artifact, *, directory="candidate"):
    services, controller = worker.services, worker.controller
    request = WorkerSeed(identity=controller.identity, operation_id=operation_id, artifact=artifact, directory=directory)
    receipts, destination, staging = locations(controller, directory)

    def create():
        services.artifacts.validate(artifact)
        if (receipts / "seed.json").exists():
            require(WorkerSeed.from_dict(_load(receipts / "seed.json")) == request,
                    "worker seed identity changed")
        else:
            require(controller.head() is None and not any(controller.private.glob("*/request.json")),
                    "worker seeding must precede every launch")
            prior_intent = (receipts / "request.json").exists()
            require(prior_intent or (not destination.exists() and not staging.exists()),
                    "worker seed destination already exists without a saved intent")
            _write(receipts / "request.json", request)
            require(not destination.is_symlink() and not staging.is_symlink(), "worker seed path cannot be a symlink")
            if destination.exists():
                # A crash after rename but before publication is recoverable only
                # when the unpublished destination still equals the pinned seed.
                services.artifacts.verify_tree(artifact, destination)
            else:
                if staging.exists():
                    # This staging path is bound by the immutable intent, no
                    # worker has launched, and it is never exposed as a candidate.
                    shutil.rmtree(staging)
                services.artifacts.materialize(artifact, staging)
                for path in staging.rglob("*"):
                    if path.is_file():
                        with path.open("rb") as stream:
                            os.fsync(stream.fileno())
                for path in sorted((item for item in staging.rglob("*") if item.is_dir()), reverse=True):
                    _sync_directory(path)
                _sync_directory(staging)
                staging.rename(destination)
                _sync_directory(destination.parent)
            _write(receipts / "seed.json", request)
        return services.values.publish(KIND, request)

    return services._record("worker.seed", request, create, decode=DataRef.from_dict)


def validate_seed(services, value):
    controller = services.worker(value.identity.worker_id).controller
    receipts, destination, _ = locations(controller, value.directory)
    require(value.identity == controller.identity
            and WorkerSeed.from_dict(_load(receipts / "request.json")) == value
            and WorkerSeed.from_dict(_load(receipts / "seed.json")) == value,
            "restored worker seed differs from its receipts")
    services.artifacts.validate(value.artifact)
    if controller.head() is None:
        require(not any(controller.private.glob("*/request.json")), "worker head is missing after a launch")
        services.artifacts.verify_tree(value.artifact, destination)
    # Once launched, edits in the working copy are expected and never reverted.
