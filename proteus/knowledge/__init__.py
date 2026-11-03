"""Versioned research knowledge shared by independent workers."""

from proteus.knowledge.records import KnowledgeIdentity, KnowledgeOutcome, KnowledgeRevision, KnowledgeSnapshot
from proteus.knowledge.store import KnowledgeConflict, SharedKnowledgeStore

__all__ = ["KnowledgeConflict", "KnowledgeIdentity", "KnowledgeOutcome", "KnowledgeRevision", "KnowledgeSnapshot", "SharedKnowledgeStore"]
