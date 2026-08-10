from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from repositories.log_repository import mask_sensitive_data


@dataclass(frozen=True)
class OperationalError:
    user_message: str
    retryable: bool
    preserved_data: tuple[str, ...]
    action: str
    technical_details: dict[str, Any]


def create_operational_error(
    *,
    component: str,
    operation: str,
    error: Exception,
    user_message: str,
    preserved_data: tuple[str, ...],
    action: str,
    retryable: bool,
    elapsed_seconds: float | None = None,
    correlation_id: str | None = None,
) -> OperationalError:
    technical = mask_sensitive_data(
        {
            "error_code": error.__class__.__name__.upper(),
            "correlation_id": correlation_id or str(uuid.uuid4()),
            "component": component,
            "operation": operation,
            "exception_type": error.__class__.__name__,
            "elapsed_seconds": elapsed_seconds,
            "retryable": retryable,
        }
    )
    return OperationalError(
        user_message=user_message,
        retryable=retryable,
        preserved_data=preserved_data,
        action=action,
        technical_details=technical,
    )

