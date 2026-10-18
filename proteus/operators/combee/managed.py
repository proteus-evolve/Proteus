"""All aggregation completions use the existing stage, budget and recovery APIs."""

from dataclasses import dataclass

from proteus.core.contracts import DataRef, OperationStatus, require
from proteus.core.parallel import StageJob
from proteus.core.serialization import Contract
from proteus.recipes.builtins import Text


@dataclass(frozen=True, kw_only=True)
class Wave(Contract):
    operation_id: str
    prompts: tuple[DataRef, ...]
    responses: tuple[DataRef, ...]
    map_receipt: DataRef | None = None

    def _validate(self):
        require(len(self.prompts) == len(self.responses), "aggregation wave is not aligned")


class CompletionText(str):
    """Ephemeral parser input; the durable ActorResult owns provider metadata."""

    def __new__(cls, value, *, finish_reason=None):
        instance = super().__new__(cls, value)
        instance.finish_reason = finish_reason
        return instance

    def strip(self, chars=None):
        return type(self)(super().strip(chars), finish_reason=self.finish_reason)


class ManagedLM:
    def __init__(self, services, stage, *, operation_id, max_workers):
        require(services._journal is not None, "aggregation requires a journal-enabled method")
        require(
            type(operation_id) is str and bool(operation_id.strip()),
            "aggregation needs an operation ID",
        )
        require(type(max_workers) is int and max_workers >= 1, "invalid aggregation worker count")
        spec = next((item for item in services.recipe.stages if item.id == stage), None)
        require(spec is not None, "unknown aggregation stage")
        require(
            dict(spec.input_types) == {"prompt": "proteus.text@1"}
            and dict(spec.output_types) == {"response": "proteus.actor_result@1"},
            "aggregation stage requires managed text prompt/response ports",
        )
        self.services, self.stage = services, stage
        self.operation_id, self.max_workers = operation_id, max_workers
        self.waves = []

    def complete(self, prompts, *, parallel=True):
        services = self.services
        require(all(type(prompt) is str for prompt in prompts), "aggregation requires text prompts")
        if not prompts:
            return []
        operation = f"{self.operation_id}/wave-{len(self.waves)}"
        refs = tuple(services.values.publish("proteus.text@1", Text(text=text)) for text in prompts)
        receipt = None
        if parallel:
            mapped = services.map_stages(
                tuple(
                    StageJob(key=str(i), stage=self.stage, inputs={"prompt": ref})
                    for i, ref in enumerate(refs)
                ),
                operation_id=operation,
                max_workers=self.max_workers,
            )
            receipt = services.values.publish("proteus.stage_map@1", mapped)
            require(
                mapped.succeeded, "aggregation wave failed; incomplete proposals cannot be reduced"
            )
            results = mapped.results
        else:
            require(len(refs) == 1, "serial completion accepts one prompt")
            results = (services.stage(self.stage, {"prompt": refs[0]}, operation_id=operation),)
        require(
            all(result.status == OperationStatus.COMPLETED for result in results),
            "aggregation stage failed",
        )
        responses = tuple(result.outputs["response"] for result in results)
        values = [services.values.get(ref) for ref in responses]
        require(
            all(value.status == OperationStatus.COMPLETED for value in values),
            "aggregation actor failed",
        )
        wave = Wave(operation_id=operation, prompts=refs, responses=responses, map_receipt=receipt)
        self.waves.append(services.values.publish("proteus.combee.wave@1", wave))
        return [CompletionText(value.text, finish_reason=value.finish_reason) for value in values]

    def __call__(self, prompt):
        return self.complete([prompt], parallel=False)[0]

    def batch_complete(self, messages_list):
        for messages in messages_list:
            require(
                len(messages) == 1
                and set(messages[0]) == {"role", "content"}
                and messages[0]["role"] == "user"
                and type(messages[0]["content"]) is str,
                "managed Combee currently supports one text user message per completion",
            )
        return self.complete([messages[0]["content"] for messages in messages_list])
