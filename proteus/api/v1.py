"""Version 1 of the researcher API, proposed for the Proteus 1.0 release.

These are aliases to the execution contracts, not a second implementation. A
versioned import surface does not permit resuming an experiment with changed
source, plugin implementations, configuration or dependency locks.
"""

from proteus.artifacts.codecs import FileContent, FileDelta, FileWrite, RecordsDelta, StructuredRecords, TextComponents, TextDelta
from proteus.artifacts.store import ArtifactStore
from proteus.core.contracts import (
    ArtifactComponent, ArtifactRef, BudgetLimits, Candidate, DataRef, EvaluationBatch, EvaluationRow,
    FeedbackVisibility, MethodState, OperationStatus, Outcome, PluginRef, PluginState, Rollout,
    RuntimeIdentity, SessionPolicy, StageRequest, StageResult, StatePolicy, Usage, ValidationStatus, require,
)
from proteus.core.methods import ActorRequest, ActorResult, ActorRuntime, Method, MethodResult, RunServices, Stage, StageContext
from proteus.core.plugins import EmptyOptions, Plugin, Registry, implementation_hash
from proteus.core.search import ArchivePolicy, ArchiveSnapshot, FinalSelector, Objective, ParentSelector, PolicyResult, SearchDecision, SelectionView
from proteus.core.serialization import Contract, ContractError, canonical_json, content_hash
from proteus.evaluation.records import EvaluationReceipt, Feedback, Grade, SubjectResult, zero_usage
from proteus.evaluation.runners import Evaluator, SubjectContext, SubjectRunner
from proteus.evaluation.tasks import StateValue, Task, TaskBatch, TaskDataset, TaskRecord, TaskSelection, TaskSuite
from proteus.execution.harnesses import HarnessIntegration, harness_support_matrix, register_harness
from proteus.measure.artifacts import MeasurementSpec, MeasurementSurface
from proteus.recipes.builtins import builtin_registry
from proteus.recipes.config import (
    ActorSpec, EnvironmentSpec, EvaluationSpec, ExecutionSpec, PluginSpec, RecipeConfig, ReferenceSpec,
    StageSpec, SubjectSpec, TaskSuiteSpec, load_recipe,
)
from proteus.recipes.manifest import DependencyLock, PluginLock, RunManifest, SourcePin
from proteus.recipes.runner import run_recipe

API_VERSION = 1

__all__ = (
    'API_VERSION', 'ActorRequest', 'ActorResult', 'ActorRuntime', 'ActorSpec', 'ArchivePolicy', 'ArchiveSnapshot',
    'ArtifactComponent', 'ArtifactRef', 'ArtifactStore', 'BudgetLimits', 'Candidate', 'Contract', 'ContractError',
    'DataRef', 'DependencyLock', 'EmptyOptions', 'EnvironmentSpec', 'EvaluationBatch', 'EvaluationReceipt',
    'EvaluationRow', 'EvaluationSpec', 'Evaluator', 'ExecutionSpec', 'Feedback', 'FeedbackVisibility',
    'FileContent', 'FileDelta', 'FileWrite', 'FinalSelector', 'Grade', 'HarnessIntegration', 'MeasurementSpec', 'MeasurementSurface', 'Method', 'MethodResult', 'MethodState',
    'Objective', 'OperationStatus', 'Outcome', 'ParentSelector', 'Plugin', 'PluginLock', 'PluginRef', 'PluginSpec',
    'PluginState', 'PolicyResult', 'RecipeConfig', 'RecordsDelta', 'ReferenceSpec', 'Registry', 'Rollout',
    'RunManifest', 'RunServices', 'RuntimeIdentity', 'SearchDecision', 'SelectionView', 'SessionPolicy', 'SourcePin',
    'Stage', 'StageContext', 'StageRequest', 'StageResult', 'StageSpec', 'StatePolicy', 'StateValue', 'StructuredRecords',
    'SubjectContext', 'SubjectResult', 'SubjectRunner', 'SubjectSpec', 'Task', 'TaskBatch', 'TaskDataset', 'TaskRecord',
    'TaskSelection', 'TaskSuite', 'TaskSuiteSpec', 'TextComponents', 'TextDelta', 'Usage', 'ValidationStatus', 'builtin_registry',
    'canonical_json', 'content_hash', 'harness_support_matrix', 'implementation_hash', 'load_recipe', 'register_harness', 'require', 'run_recipe', 'zero_usage',
)
