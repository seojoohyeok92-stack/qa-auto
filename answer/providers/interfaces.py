from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class JsonGptProvider(Protocol):
    name: str

    def generate_json(
        self,
        *,
        task: str,
        prompt: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Return one JSON-compatible object without side effects."""
