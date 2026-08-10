from __future__ import annotations

import re
from typing import Any

from answer.providers.gpt_provider import GptProvider


def _split_questions(text: str) -> list[str]:
    parts = [
        part.strip(" \t\r\n?.!。！？")
        for part in re.split(r"(?:\r?\n+|[?？!！]+)", str(text or ""))
        if part.strip(" \t\r\n?.!。！？")
    ]
    return parts or ([str(text).strip()] if str(text).strip() else [])


def _emotion(text: str) -> str:
    value = str(text or "").lower()
    if any(word in value for word in ("화가", "짜증", "최악", "왜 안", "도대체")):
        return "ANGRY"
    if any(word in value for word in ("급해", "긴급", "오늘 꼭", "당장")):
        return "URGENT"
    if any(word in value for word in ("감사", "고맙")):
        return "THANKFUL"
    if any(word in value for word in ("다시", "전에", "추가로")):
        return "FOLLOW_UP"
    if any(word in value for word in ("모르겠", "헷갈", "무슨 뜻")):
        return "CONFUSED"
    return "NORMAL"


class FakeGptProvider(GptProvider):
    """네트워크 없이 실제 GPT JSON 계약을 재현하는 결정적 provider."""

    name = "fake_gpt"

    def __init__(
        self,
        *,
        responses: dict[str, dict[str, Any]] | None = None,
        fail_tasks: set[str] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.fail_tasks = set(fail_tasks or set())
        self.calls: list[dict[str, Any]] = []

    def generate_json(
        self,
        *,
        task: str,
        prompt: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_task = str(task).upper()
        self.calls.append(
            {"task": normalized_task, "prompt": prompt, "context": context}
        )
        if normalized_task in self.fail_tasks:
            raise RuntimeError(f"Fake GPT failure: {normalized_task}")
        if normalized_task in self.responses:
            return dict(self.responses[normalized_task])
        if normalized_task == "UNDERSTANDING":
            question = str(context.get("question") or "")
            rule = context.get("rule") or {}
            dps = context.get("dps") or {}
            emotion = _emotion(question)
            requires_review = bool(
                rule.get("needs_review")
                or dps.get("change_request")
                or dps.get("lookup_status")
                in {
                    "WAITING_FOR_ORDER_ID",
                    "NOT_FOUND",
                    "TIMEOUT",
                    "AGENT_OFFLINE",
                    "AUTOMATION_ERROR",
                    "PARSE_ERROR",
                }
            )
            return {
                "category": str(rule.get("category") or "기타/직원확인"),
                "questions": _split_questions(question),
                "emotion": emotion,
                "urgency": (
                    "HIGH" if emotion in {"URGENT", "ANGRY"} else "NORMAL"
                ),
                "confidence": 0.95 if question.strip() else 0.0,
                "requires_review": requires_review,
                "reason": (
                    "확인되지 않은 정보가 있어 직원 검토가 필요합니다."
                    if requires_review
                    else "제공된 Facts 범위에서 질문을 분류했습니다."
                ),
            }
        if normalized_task == "DRAFT":
            rule = context.get("rule") or {}
            allowed = context.get("allowed_facts") or {}
            answer = str(
                rule.get("answer")
                or allowed.get("rule.answer")
                or ""
            )
            used = ["rule.answer"] if answer else []
            dps = context.get("dps") or {}
            if allowed.get("installation.date") and not answer:
                date_text = str(allowed["installation.date"])
                year, month, day = (int(part) for part in date_text.split("-"))
                answer = (
                    "안녕하세요, 고객님.\n"
                    f"확인 결과 설치 예정일은 {year}년 {month}월 {day}일입니다.\n"
                    "최종 설치 일정은 설치 전날 카카오톡으로 안내드릴 예정입니다.\n"
                    "감사합니다."
                )
                used = ["installation.date"]
            for key in (
                "delivery_status",
                "installation_status",
                "installation_date",
            ):
                if dps.get(key) and str(dps.get(key)) in answer:
                    used.append(
                        "delivery.status"
                        if key == "delivery_status"
                        else f"installation.{key.removeprefix('installation_')}"
                    )
            return {
                "answer": answer,
                "confidence": 0.97 if answer else 0.25,
                "used_facts": used,
                "missing_information": [] if answer else ["rule.answer"],
                "requires_review": bool(
                    rule.get("needs_review") or not answer
                ),
                "warnings": [],
            }
        if normalized_task == "SELF_REVIEW":
            draft = context.get("draft") or {}
            answer = str(draft.get("answer") or "")
            questions = context.get("questions") or []
            speculation = bool(
                re.search(r"(아마|추측|것 같습니다|일 듯|예상해 봅니다)", answer)
            )
            passed = bool(answer) and not speculation
            return {
                "passed": passed,
                "answered_all_questions": bool(answer) and bool(questions),
                "has_speculation": speculation,
                "facts_consistent": True,
                "requires_review": bool(
                    draft.get("requires_review") or not passed
                ),
                "reason": (
                    "Facts와 질문을 기준으로 자체 검토했습니다."
                    if passed
                    else "답변 누락 또는 추측 표현을 확인했습니다."
                ),
                "warnings": [],
            }
        raise ValueError(f"Unsupported Fake GPT task: {task}")
