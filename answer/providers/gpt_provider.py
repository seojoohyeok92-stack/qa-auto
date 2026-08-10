from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class GptProvider(ABC):
    name = "gpt_base"

    @abstractmethod
    def generate_json(
        self,
        *,
        task: str,
        prompt: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError
