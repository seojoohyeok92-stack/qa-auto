from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Mapping

import requests

from answer.exceptions import AnswerProviderUnavailableError
from answer.governance_models import GptProviderSettings
from answer.provider_errors import (
    GptProviderAuthenticationError,
    GptProviderRetryableError,
    GptProviderTimeoutError,
)
from answer.providers.gpt_provider import GptProvider
from answer.providers.task_profiles import task_request_profile


Transport = Callable[..., dict[str, Any]]


class OpenAIResponsesTransport:
    """승인 Gate 뒤에서만 생성되는 OpenAI Responses API transport."""

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        settings: GptProviderSettings,
        *,
        session: requests.Session | None = None,
        clock=time.monotonic,
    ) -> None:
        issues = settings.validation_issues()
        if issues or not settings.is_real_provider:
            raise AnswerProviderUnavailableError(
                "실제 Provider 설정이 유효하지 않습니다."
            )
        api_key = os.getenv("QNA_GPT_API_KEY")
        if not api_key:
            raise AnswerProviderUnavailableError(
                "Provider API key가 설정되지 않았습니다."
            )
        self.settings = settings
        self._api_key = api_key
        self.session = session or requests.Session()
        self.clock = clock

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        for output in payload.get("output") or []:
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content") or []:
                if (
                    isinstance(content, dict)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)
                ):
                    return str(content["text"])
        raise ValueError("OpenAI 응답에서 JSON 텍스트를 찾지 못했습니다.")

    def __call__(
        self,
        *,
        task: str,
        prompt: str,
        context: dict[str, Any],
        model: str,
        connect_timeout: float,
        read_timeout: float,
        total_timeout: float,
        request_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = self.clock()
        body = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Return only one JSON object that follows the supplied "
                        f"{task} contract. Use supplied facts only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "text": {"format": {"type": "json_object"}},
        }
        # Per-task tuning, empty for every task that does not ask for it. The
        # answer path sends exactly the body it always sent.
        body.update(dict(request_options or {}))
        try:
            response = self.session.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.ConnectTimeout as error:
            # Never reaching the server is a transient fault: the same request
            # is worth sending again. Distinguished from a read timeout so the
            # retry policy can treat the two differently.
            raise GptProviderRetryableError(
                "OpenAI Provider 연결 시간이 초과되었습니다."
            ) from error
        except requests.Timeout as error:
            # A read timeout means the request was accepted and generation
            # simply took longer than the budget. Repeating an identical
            # request rarely makes the model faster, so this is not retried.
            raise GptProviderTimeoutError(
                "OpenAI Provider 응답 시간이 초과되었습니다."
            ) from error
        except requests.ConnectionError as error:
            raise ConnectionError("OpenAI Provider 연결에 실패했습니다.") from error
        if self.clock() - started > total_timeout:
            raise GptProviderTimeoutError(
                "OpenAI Provider 전체 제한 시간을 초과했습니다."
            )
        if response.status_code in {401, 403}:
            raise GptProviderAuthenticationError(
                "OpenAI Provider 인증 또는 권한을 확인해 주세요."
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise GptProviderRetryableError(
                "OpenAI Provider가 일시적으로 요청을 처리하지 못했습니다.",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise ValueError(
                f"OpenAI Provider 요청 계약 오류({response.status_code})"
            )
        payload = response.json()
        parsed = json.loads(self._output_text(payload))
        if not isinstance(parsed, dict):
            raise ValueError("OpenAI Provider JSON 응답은 객체여야 합니다.")
        usage = payload.get("usage")
        if isinstance(usage, dict):
            parsed["_usage"] = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        return parsed


class OpenAIJsonProvider(GptProvider):
    """승인된 transport를 주입할 때만 사용할 수 있는 실제 Provider 경계."""

    name = "openai"

    def __init__(
        self,
        settings: GptProviderSettings,
        *,
        transport: Transport | None = None,
    ) -> None:
        issues = settings.validation_issues()
        if issues:
            raise AnswerProviderUnavailableError(" ".join(issues))
        if not settings.is_real_provider:
            raise AnswerProviderUnavailableError(
                "OpenAI adapter에는 실제 Provider 설정이 필요합니다."
            )
        self.settings = settings
        self._transport = transport or OpenAIResponsesTransport(settings)
        self._attempt_budget: float | None = None

    def set_attempt_budget(self, seconds: float | None) -> None:
        """Cap the next request by the time left in the generation budget.

        Without this the configured total timeout could not restrain an
        in-flight request: it was only ever compared *after* a read timeout
        had already elapsed. The retry wrapper calls this before each attempt
        so the socket read itself is bounded by the remaining budget.
        """

        self._attempt_budget = None if seconds is None else max(0.0, seconds)

    def _read_timeout(self) -> float:
        configured = self.settings.read_timeout_seconds
        if self._attempt_budget is None:
            return configured
        # Leave room for connecting, and never fall to zero -- a request with
        # no read budget at all would fail before it could be sent.
        usable = self._attempt_budget - self.settings.connect_timeout_seconds
        return max(1.0, min(configured, usable))

    def generate_json(
        self,
        *,
        task: str,
        prompt: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if self._transport is None:
            raise AnswerProviderUnavailableError(
                "실제 OpenAI transport는 아직 연결되지 않았습니다."
            )
        model, options = task_request_profile(
            task, self.settings.model,
            allowed_models=tuple(self.settings.allowed_models or ()),
        )
        return self._transport(
            task=task,
            prompt=prompt,
            context=context,
            model=model,
            connect_timeout=self.settings.connect_timeout_seconds,
            read_timeout=self._read_timeout(),
            total_timeout=self.settings.total_timeout_seconds,
            request_options=options,
        )
