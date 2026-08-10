from __future__ import annotations

import hashlib
import re
from typing import Any

from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from services.learning_service import LearningService


AMBIGUOUS = re.compile(r"(?:아닌가|일까요|같(?:아|은데)|맞나요|혹시|\?)")
NON_QNA_OPERATION = re.compile(
    r"(?:화면|글씨|어두|밝기|포트|8502|README|통계창|위치|레이아웃|코드|마이그레이션)", re.I
)
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "INQUIRY_CLASSIFICATION_CORRECTION",
        re.compile(r"(?:이건|이 문의는|이 문의가).{0,18}(?:배송|설치|교환|반품|환불)\s*문의(?:야|입니다|인데|건데|건이야|건입니다)"),
    ),
    (
        "REQUIRED_ACTION_CORRECTION",
        re.compile(r"(?:DPS|주문\s*조회).{0,18}(?:해야|필요|했어야|안\s*했)"),
    ),
    (
        "ANSWER_POLICY_CORRECTION",
        re.compile(r"(?:물어보면|안내하면|확정하면).{0,10}(?:안\s*돼|안됩니다|안\s*되는)"),
    ),
    (
        "RESPONSE_CORRECTION",
        re.compile(r"(?:이 경우|이 문의).{0,30}(?:안내해야|답변해야|말해야|알려줘야)"),
    ),
)


class CopilotCorrectionLearningService:
    """Capture only explicit inquiry-handling corrections as reference Learning."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.learning = LearningService(database)
        self.answers = AnswerRepository(database)

    @staticmethod
    def classify(message: str) -> tuple[str, float] | None:
        clean = str(message or "").strip()
        if not clean or AMBIGUOUS.search(clean) or NON_QNA_OPERATION.search(clean):
            return None
        for correction_type, pattern in PATTERNS:
            if pattern.search(clean):
                return correction_type, 0.9
        return None

    def capture(
        self, *, inquiry_id: int | None, message: str,
        chat_session_id: int, chat_message_id: int,
    ) -> dict[str, Any] | None:
        classification = self.classify(message)
        if inquiry_id is None or classification is None:
            return None
        correction_type, confidence = classification
        inquiry = self.learning.inquiries.get(int(inquiry_id))
        if inquiry is None:
            return None
        draft = self.answers.latest_for_inquiry(int(inquiry_id))
        example = self.learning._build(
            inquiry=inquiry,
            draft=draft,
            learning_source="APPROVED_EDITED",
            answer=str(message),
        )
        if example is None:
            return None
        with self.database.connection() as connection:
            dps_invoked = connection.execute(
                "SELECT 1 FROM dps_lookup_results WHERE inquiry_id=? LIMIT 1",
                (int(inquiry_id),),
            ).fetchone() is not None
        metadata = (draft or {}).get("metadata_json")
        metadata = metadata if isinstance(metadata, dict) else {}
        plan = metadata.get("processing_plan") if isinstance(metadata.get("processing_plan"), dict) else {}
        masked_correction = self.learning.privacy.mask(message)
        example.update({
            "source_key": hashlib.sha256(
                f"COPILOT_CORRECTION|{inquiry_id}|{correction_type}|{masked_correction}".encode("utf-8")
            ).hexdigest(),
            "generation_mode": "COPILOT_CORRECTION",
            "validator_result": "COPILOT_EXPLICIT_CORRECTION_REFERENCE",
            "rating": min(int(example.get("rating") or 3), 3),
            "quality_score": min(float(example.get("quality_score") or 0.65), 0.65),
            "metadata_json": {
                "facts_authority": "REFERENCE_ONLY_RULES_STILL_WIN",
                "source_origin": "COPILOT_CORRECTION",
                "correction_type": correction_type,
                "correction_text": masked_correction,
                "original_classification": inquiry.get("inquiry_type"),
                "original_route": plan.get("selected_answer_route") or metadata.get("selected_answer_route"),
                "original_processing_plan": plan,
                "order_reference_present": bool(inquiry.get("order_id")),
                "dps_invoked": bool(dps_invoked),
                "chat_session_id": int(chat_session_id),
                "chat_message_id": int(chat_message_id),
                "confidence": confidence,
                "does_not_change_rules": True,
            },
        })
        existing = self.learning.repository.get_by_source_key(example["source_key"])
        if existing is not None:
            return existing
        saved = self.learning.repository.upsert(example)
        self.learning.logs.record_inquiry(
            int(inquiry_id), "COPILOT_CORRECTION_LEARNING_SAVED",
            "운영자가 명확히 교정한 문의 처리 판단을 참고 Learning으로 저장했습니다.",
            details={
                "learning_example_id": int(saved["id"]),
                "correction_type": correction_type,
                "chat_session_id": int(chat_session_id),
                "chat_message_id": int(chat_message_id),
            },
        )
        return saved
