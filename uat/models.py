from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class UatStatus(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    FAILED = "FAILED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"

    @property
    def label(self) -> str:
        return {
            self.NORMAL: "정상",
            self.WARNING: "주의",
            self.FAILED: "실패",
            self.NOT_CONFIGURED: "미설정",
            self.NOT_RUN: "미실행",
            self.BLOCKED: "차단됨",
        }[self]


class EnvironmentRequirement(str, Enum):
    REQUIRED = "REQUIRED"
    CONDITIONAL = "CONDITIONAL"
    OPTIONAL = "OPTIONAL"
    DEPRECATED = "DEPRECATED"
    UNKNOWN = "UNKNOWN"


class UserRole(str, Enum):
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    AGENT = "AGENT"


@dataclass(frozen=True)
class EnvironmentVariableCheck:
    name: str
    requirement: EnvironmentRequirement
    scope: str
    present: bool
    valid: bool
    description: str
    resolution: str
    status: UatStatus

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["requirement"] = self.requirement.value
        result["status"] = self.status.value
        return result


@dataclass(frozen=True)
class EnvironmentValidationResult:
    checks: tuple[EnvironmentVariableCheck, ...]
    status: UatStatus
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [item.to_dict() for item in self.checks],
            "status": self.status.value,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True)
class UatDiagnosticItem:
    code: str
    label: str
    status: UatStatus
    message: str
    checked_at: str
    failure_type: str | None = None
    action: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass(frozen=True)
class UatDiagnosticReport:
    items: tuple[UatDiagnosticItem, ...]
    status: UatStatus
    correlation_id: str
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "status": self.status.value,
            "correlation_id": self.correlation_id,
            "checked_at": self.checked_at,
        }

