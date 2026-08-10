from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import requests

from answer.exceptions import AnswerProviderUnavailableError
from answer.governance_models import GptProviderSettings
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.gpt_chat_repository import GptChatRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.log_repository import LogRepository
from repositories.post_review_repository import PostReviewRepository
from repositories.project_knowledge_repository import ProjectKnowledgeRepository
from repositories.workflow_repository import WorkflowRepository
from services.prompt_privacy_service import PromptPrivacyService
from services.similar_answer_service import SimilarAnswerService
from services.historical_case_service import HistoricalCaseService


Transport = Callable[..., str]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED_PATH = PROJECT_ROOT / "answer_data" / "knowledge" / "qna_auto_project_knowledge.json"


class OpenAICopilotTransport:
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(
        self,
        settings: GptProviderSettings,
        *,
        session: requests.Session | None = None,
    ) -> None:
        issues = settings.validation_issues()
        if issues or not settings.is_real_provider:
            raise AnswerProviderUnavailableError(
                "GPT Copilot Provider 설정이 유효하지 않습니다."
            )
        api_key = os.getenv("QNA_GPT_API_KEY")
        if not api_key:
            raise AnswerProviderUnavailableError("QNA_GPT_API_KEY가 설정되지 않았습니다.")
        self.settings = settings
        self.api_key = api_key
        self.session = session or requests.Session()

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for output in payload.get("output") or []:
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content") or []:
                if (
                    isinstance(content, dict)
                    and content.get("type") == "output_text"
                    and isinstance(content.get("text"), str)
                    and content.get("text").strip()
                ):
                    return str(content["text"]).strip()
        raise ValueError("OpenAI 응답에서 텍스트를 찾지 못했습니다.")

    def __call__(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
        model: str,
        connect_timeout: float,
        read_timeout: float,
    ) -> str:
        body = {
            "model": model,
            "input": [
                {"role": "system", "content": system_prompt},
                *messages,
            ],
        }
        try:
            response = self.session.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.Timeout as error:
            raise TimeoutError("GPT Copilot 응답 시간이 초과되었습니다.") from error
        except requests.ConnectionError as error:
            raise ConnectionError("GPT Copilot 연결에 실패했습니다.") from error
        if response.status_code in {401, 403}:
            raise PermissionError("GPT Copilot 인증 또는 권한을 확인해 주세요.")
        if response.status_code == 429:
            raise RuntimeError("GPT Copilot 요청 한도에 도달했습니다.")
        if response.status_code >= 500:
            raise RuntimeError("GPT Copilot 서비스가 일시적으로 응답하지 않습니다.")
        if response.status_code >= 400:
            raise RuntimeError(f"GPT Copilot 요청 오류({response.status_code})")
        return self._output_text(response.json())


class GptCopilotService:
    """Q&A Auto 운영용 읽기 전용 GPT Copilot.

    이 서비스는 문의/주문/DPS/Learning/프로젝트 지식을 설명용 Context로만
    사용한다. 네이버 POST나 운영 상태 변경 함수는 의도적으로 제공하지 않는다.
    """

    def __init__(
        self,
        database: Database,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.database = database
        self.settings = GptProviderSettings.from_environment()
        self.chats = GptChatRepository(database)
        self.knowledge = ProjectKnowledgeRepository(database)
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.dps = DpsRepository(database)
        self.workflow = WorkflowRepository(database)
        self.reviews = PostReviewRepository(database)
        self.learning = SimilarAnswerService(LearningRepository(database))
        self.historical = HistoricalCaseService(database)
        self.logs = LogRepository(database)
        self.privacy = PromptPrivacyService()
        self._transport = transport
        self.knowledge.seed_from_json(SEED_PATH)

    def status(self) -> dict[str, Any]:
        issues = list(self.settings.validation_issues())
        ready = (
            self.settings.is_real_provider
            and self.settings.provider_name.lower() == "openai"
            and self.settings.enabled
            and self.settings.mode.value not in {"DISABLED", "FAKE"}
            and not issues
        )
        return {
            "ready": ready,
            "provider": self.settings.provider_name,
            "mode": self.settings.mode.value,
            "model": self.settings.model,
            "issues": issues,
            "knowledge_count": self.knowledge.count(),
        }

    @staticmethod
    def _compact(value: Any, *, max_chars: int = 5000) -> Any:
        if isinstance(value, dict):
            return {
                str(key): GptCopilotService._compact(item, max_chars=max_chars)
                for key, item in value.items()
                if key not in {"raw_json", "source_payload", "source_metadata_json"}
            }
        if isinstance(value, list):
            return [GptCopilotService._compact(item, max_chars=max_chars) for item in value[:20]]
        if isinstance(value, str):
            return value[:max_chars]
        return value

    def _inquiry_context(self, inquiry_id: int) -> dict[str, Any]:
        inquiry = self.inquiries.get(int(inquiry_id)) or {}
        draft = self.answers.active_for_inquiry(int(inquiry_id)) or self.answers.latest_for_inquiry(int(inquiry_id)) or {}
        dps = self.dps.get_latest_by_inquiry_id(int(inquiry_id)) or {}
        steps = self.workflow.list_steps(int(inquiry_id))
        review = self.reviews.get(int(inquiry_id)) or {}
        current_version = (
            self.reviews.get_version(int(review["current_version_id"]))
            if review.get("current_version_id") is not None
            else None
        ) or {}
        draft_metadata = draft.get("metadata_json") if isinstance(draft.get("metadata_json"), dict) else {}
        activities = [
            {
                "event": row.get("event_code"),
                "summary": row.get("message"),
                "at": row.get("created_at"),
            }
            for row in self.logs.recent_for_inquiry(int(inquiry_id), limit=8)
        ]
        return self._compact(
            {
                "inquiry": {
                    "id": inquiry.get("id"),
                    "store_code": inquiry.get("store_code"),
                    "inquiry_type": inquiry.get("inquiry_type"),
                    "title": inquiry.get("title"),
                    "content": inquiry.get("content"),
                    "product_name": inquiry.get("product_name"),
                    "option_name": inquiry.get("option_name"),
                    "order_id": inquiry.get("order_id"),
                    "product_order_id": inquiry.get("product_order_id"),
                    "source_answered": inquiry.get("source_answered"),
                    "post_status": inquiry.get("post_status"),
                    "source_status": inquiry.get("source_status"),
                    "order_lookup_status": inquiry.get("order_lookup_status"),
                    "order_status": inquiry.get("order_status"),
                    "order_date": inquiry.get("order_date"),
                },
                "answer": {
                    "draft_id": draft.get("id"),
                    "source": draft.get("source"),
                    "review_status": draft.get("review_status"),
                    "validation_status": draft.get("validation_status"),
                    "program_answer": draft.get("original_answer"),
                    "staff_edit": draft.get("edited_answer"),
                    "final_answer": draft.get("final_answer"),
                    "processing_plan": draft_metadata.get("processing_plan"),
                    "route": draft_metadata.get("selected_answer_route")
                    or (draft_metadata.get("processing_plan") or {}).get("selected_answer_route")
                    if isinstance(draft_metadata.get("processing_plan"), dict)
                    else draft_metadata.get("selected_answer_route"),
                },
                "dps": dps,
                "workflow": steps,
                "post_review": review,
                "current_posted_version": current_version,
                "recent_activity": activities,
            }
        )

    def _learning_context(self, inquiry_id: int) -> list[dict[str, Any]]:
        inquiry = self.inquiries.get(int(inquiry_id)) or {}
        question = "\n".join(
            part
            for part in (
                str(inquiry.get("title") or "").strip(),
                str(inquiry.get("content") or "").strip(),
            )
            if part
        )
        if not question:
            return []
        results = self.learning.search(
            question,
            store_code=inquiry.get("store_code"),
            product_name=inquiry.get("product_name"),
            inquiry_type=inquiry.get("inquiry_type"),
            limit=3,
        )
        return [
            {
                "question": row.get("question_original_masked"),
                "answer": row.get("final_answer"),
                "source": row.get("learning_source"),
                "source_origin": (
                    (row.get("metadata_json") or {}).get("source_origin")
                    if isinstance(row.get("metadata_json"), dict) else None
                ),
                "historical_case_id": (
                    (row.get("metadata_json") or {}).get("historical_case_id")
                    if isinstance(row.get("metadata_json"), dict) else None
                ),
                "rating": row.get("rating"),
                "relevance": row.get("relevance"),
            }
            for row in results
        ]

    def _knowledge_context(self, query: str) -> list[dict[str, Any]]:
        return [
            {
                "category": row.get("category"),
                "title": row.get("title"),
                "content": str(row.get("content") or "")[:3500],
                "source": row.get("source"),
            }
            for row in self.knowledge.search(query, limit=6)
        ]

    def _historical_context(
        self, query: str, *, inquiry_id: int | None = None,
    ) -> list[dict[str, Any]]:
        inquiry = self.inquiries.get(int(inquiry_id)) if inquiry_id is not None else None
        question = query
        if inquiry:
            question = "\n".join(
                part for part in (
                    str(inquiry.get("title") or "").strip(),
                    str(inquiry.get("content") or "").strip(),
                ) if part
            ) or query
        return [
            {
                "historical_case_id": int(row["id"]),
                "question": row.get("question"),
                "past_answer": row.get("seller_answer"),
                "product": row.get("product_name"),
                "inquiry_type": row.get("inquiry_type"),
                "quality": row.get("quality_score"),
                "policy_risk": row.get("policy_risk"),
                "created_at": row.get("inquiry_created_at"),
                "relevance": row.get("relevance"),
                "usage_notice": row.get("usage_notice"),
                "source": row.get("reference_strength") or "HISTORICAL_REFERENCE",
            }
            for row in self.historical.search(
                question,
                store_code=(inquiry or {}).get("store_code"),
                product_name=(inquiry or {}).get("product_name"),
                inquiry_type=(inquiry or {}).get("inquiry_type"),
                limit=4,
            )
        ]

    def _past_chat_context(self, query: str, *, session_id: int) -> list[dict[str, Any]]:
        results = self.chats.search_messages(query, limit=6)
        return [
            {
                "session_title": row.get("session_title"),
                "user_name": row.get("user_name"),
                "content": str(row.get("content") or "")[:1800],
            }
            for row in results
            if int(row.get("session_id") or 0) != int(session_id)
        ][:5]

    @staticmethod
    def _system_prompt() -> str:
        return (
            "당신은 Q&A Auto 운영자를 돕는 읽기 전용 Copilot이다. 먼저 결론을 쉬운 한국어로 말한다. "
            "가능하면 '결론', '현재 상태', '왜 그런가요?', '어떻게 하면 되나요?' 순서로 답한다. "
            "현재 문의는 어디에서 멈췄는지, 왜 멈췄는지, 무엇을 확인하면 되는지를 중심으로 설명한다. "
            "로그를 그대로 나열하지 말고 확인된 Context만 요약하며, 없는 사실은 추측하지 말고 '확인 필요'라고 쓴다. "
            "PENDING은 '자동처리 대기 중', STAFF_REVIEW는 '직원 확인이 필요한 상태', "
            "source_answered=true는 '네이버에 이미 답변이 등록된 문의', lease/lock은 '중복 처리를 막는 내부 보호장치'로 번역한다. "
            "Event Queue, DB 필드명, 내부 Enum 같은 개발 용어는 일반 본문에 노출하지 않는다. 꼭 필요할 때만 마지막에 "
            "'## 기술 정보' 제목 아래에 내부 상태값을 짧게 적는다. "
            "사실 우선순위는 현재 Rule/안전정책 > 현재 주문 정보 > 현재 DPS > Product DB > 검증된 Template > "
            "승인된 직원 수정 Learning > Historical Case > GPT 표현 생성이다. "
            "Historical Case는 문체·설명 방식·유사 대응 참고일 뿐이며 배송일, 재고, 가격, 프로모션 같은 과거 사실을 현재 사실로 확정하지 않는다. "
            "네이버 등록·삭제·설정 변경을 직접 실행한다고 말하지 않는다. 등록 요청에는 기존 검증·승인·자동등록 경로를 이용해야 한다고 안내한다."
        )

    def _call_provider(self, messages: list[dict[str, str]]) -> str:
        status = self.status()
        if not status["ready"]:
            issues = ", ".join(status["issues"]) or "GPT Provider가 활성화되지 않았습니다."
            raise AnswerProviderUnavailableError(issues)
        transport = self._transport or OpenAICopilotTransport(self.settings)
        return transport(
            system_prompt=self._system_prompt(),
            messages=messages,
            model=self.settings.model,
            connect_timeout=self.settings.connect_timeout_seconds,
            read_timeout=self.settings.read_timeout_seconds,
        )

    def ask(
        self,
        *,
        session_id: int,
        message: str,
        inquiry_id: int | None = None,
        include_inquiry: bool = True,
        include_learning: bool = True,
        include_knowledge: bool = True,
        include_past_chats: bool = True,
        include_historical: bool = True,
    ) -> dict[str, Any]:
        clean_message = str(message or "").strip()
        if not clean_message:
            raise ValueError("질문을 입력해 주세요.")
        session = self.chats.get_session(int(session_id))
        if session is None:
            raise LookupError("GPT chat session not found")

        user_message = self.chats.add_message(
            session_id=int(session_id),
            role="user",
            content=clean_message,
            inquiry_id=inquiry_id,
        )
        if inquiry_id is not None:
            try:
                from services.copilot_correction_learning_service import (
                    CopilotCorrectionLearningService,
                )
                CopilotCorrectionLearningService(self.database).capture(
                    inquiry_id=int(inquiry_id),
                    message=clean_message,
                    chat_session_id=int(session_id),
                    chat_message_id=int(user_message["id"]),
                )
            except Exception as correction_error:
                self.logs.record_system(
                    "COPILOT_CORRECTION_LEARNING_FAILED",
                    "Copilot 교정 Learning 저장은 실패했지만 대화는 계속합니다.",
                    level="WARNING",
                    details={
                        "session_id": int(session_id),
                        "inquiry_id": int(inquiry_id),
                        "exception_type": correction_error.__class__.__name__,
                    },
                )
        if str(session.get("title") or "") == "새 대화":
            self.chats.update_title(int(session_id), clean_message.replace("\n", " ")[:42])

        context: dict[str, Any] = {}
        if inquiry_id is not None and include_inquiry:
            context["current_inquiry"] = self._inquiry_context(int(inquiry_id))
        if inquiry_id is not None and include_learning:
            context["learning_examples"] = self._learning_context(int(inquiry_id))
        if include_knowledge:
            context["project_knowledge"] = self._knowledge_context(clean_message)
        if include_past_chats:
            context["past_chat_references"] = self._past_chat_context(
                clean_message, session_id=int(session_id)
            )
        if include_historical:
            context["historical_cases"] = self._historical_context(
                clean_message, inquiry_id=inquiry_id
            )
        if context.get("learning_examples") and context.get("historical_cases"):
            promoted_case_ids = {
                int(item["historical_case_id"])
                for item in context["learning_examples"]
                if item.get("historical_case_id") is not None
            }
            context["historical_cases"] = [
                item for item in context["historical_cases"]
                if int(item["historical_case_id"]) not in promoted_case_ids
            ]

        privacy = self.privacy.sanitize(
            {"question": clean_message, "context": context}
        )
        if not privacy.safe_to_send:
            answer = (
                "GPT 전송이 개인정보/내부정보 보호 규칙에 의해 차단되었습니다. "
                "질문에서 인증정보, 내부 URL 또는 로컬 파일 경로를 제거한 뒤 다시 시도해 주세요."
            )
            saved = self.chats.add_message(
                session_id=int(session_id),
                role="assistant",
                content=answer,
                inquiry_id=inquiry_id,
                metadata={
                    "status": "PRIVACY_BLOCKED",
                    "blocking_issues": list(privacy.blocking_issues),
                },
            )
            return {"answer": answer, "message": saved, "status": "PRIVACY_BLOCKED"}

        sanitized = privacy.sanitized_payload
        recent = self.chats.messages(int(session_id), limit=12)
        history = [
            {"role": str(row["role"]), "content": str(row["content"])[:4000]}
            for row in recent
            if row["role"] in {"user", "assistant"}
        ][:-1]
        user_payload = (
            f"운영자 질문:\n{sanitized.get('question', clean_message)}\n\n"
            "Q&A Auto Context:\n"
            + json.dumps(
                sanitized.get("context") or {},
                ensure_ascii=False,
                default=str,
            )[:24000]
        )
        messages = [*history[-8:], {"role": "user", "content": user_payload}]
        try:
            answer = self._call_provider(messages)
            provider_status = "SUCCESS"
        except Exception as error:
            provider_status = "FAILED"
            answer = (
                "GPT Copilot 호출에 실패했습니다. 기존 Q&A Auto 자동답변/자동등록 기능에는 "
                "영향이 없습니다. 원인: " + str(error)[:500]
            )
        saved = self.chats.add_message(
            session_id=int(session_id),
            role="assistant",
            content=answer,
            inquiry_id=inquiry_id,
            metadata={
                "status": provider_status,
                "model": self.settings.model,
                "provider": self.settings.provider_name,
                "masked_patterns": list(privacy.masked_patterns),
                "context_flags": {
                    "inquiry": bool(inquiry_id is not None and include_inquiry),
                    "learning": bool(inquiry_id is not None and include_learning),
                    "knowledge": bool(include_knowledge),
                    "past_chats": bool(include_past_chats),
                    "historical": bool(include_historical),
                },
            },
        )
        self.logs.record_system(
            "GPT_COPILOT_MESSAGE",
            "GPT 운영 도우미 대화를 저장했습니다.",
            details={
                "session_id": int(session_id),
                "inquiry_id": inquiry_id,
                "status": provider_status,
                "provider": self.settings.provider_name,
                "model": self.settings.model,
            },
        )
        return {"answer": answer, "message": saved, "status": provider_status}
