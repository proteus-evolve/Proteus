"""Isolated current tools-permission-drift suite."""

from dataclasses import dataclass

from proteus.safety.phase1 import TOOLS_PERMISSION_DRIFT
from proteus.safety.taxonomy import SafetyCaseFamilyDefinition


@dataclass(frozen=True)
class ToolsPermissionDriftSuite:
    name: str = "proteus-tools-permission-drift"
    version: str = "3"

    def definitions(self) -> tuple[SafetyCaseFamilyDefinition, ...]:
        return (TOOLS_PERMISSION_DRIFT,)


SUITE = ToolsPermissionDriftSuite()
