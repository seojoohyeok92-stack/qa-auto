from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.log_repository import LogRepository
from services.learning_privacy_service import LearningPrivacyService
from services.learning_quality_service import LearningQualityService
from services.similar_answer_service import normalize_learning_question


STALE_POLICY = re.compile(r"(?:\d{1,3}(?:,\d{3})*\s*원|\d{4}[./-]\d{1,2}[./-]\d{1,2}|이벤트\s*(?:기간|마감))")
UNSAFE_DELIVERY = re.compile(r"(?:확실히|반드시|무조건).{0,15}(?:배송|도착|설치)")
SELLER_KEYS = {"selleranswer", "seller_answer", "answercontent", "commentcontent", "replycontent"}


def _first_text(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("-", "").replace("_", "").lower()
            if normalized in SELLER_KEYS and isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = _first_text(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_text(item)
            if found:
                return found
    return ""


class LearningService:
    """승인/등록 트랜잭션의 결과만 복제하는 격리된 Learning Layer."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.repository = LearningRepository(database)
        self.logs = LogRepository(database)
        self.privacy = LearningPrivacyService()
        self.quality = LearningQualityService()

    @staticmethod
    def _metadata(draft: dict[str, Any]) -> dict[str, Any]:
        value = draft.get("metadata_json")
        return value if isinstance(value, dict) else {}

    def _build(
        self, *, inquiry: dict[str, Any], draft: dict[str, Any] | None,
        learning_source: str, answer: str, history_id: int | None = None,
        seller_answer: str = "",
    ) -> dict[str, Any] | None:
        clean_answer = str(answer or "").strip()
        question = "\n".join(
            part for part in (str(inquiry.get("title") or "").strip(), str(inquiry.get("content") or "").strip()) if part
        ).strip()
        if not question or not clean_answer:
            return None
        if learning_source == "SELLER_ANSWER" and (
            STALE_POLICY.search(clean_answer) or UNSAFE_DELIVERY.search(clean_answer)
        ):
            return None
        names = [inquiry.get("customer_display"), inquiry.get("masked_writer_id")]
        masked_question = self.privacy.mask(question, customer_names=names)
        masked_answer = self.privacy.mask(clean_answer, customer_names=names)
        if not masked_question or not masked_answer:
            return None
        metadata = self._metadata(draft or {})
        plan = metadata.get("processing_plan") if isinstance(metadata.get("processing_plan"), dict) else {}
        phase9 = metadata.get("phase9") if isinstance(metadata.get("phase9"), dict) else {}
        analysis = phase9.get("analysis") if isinstance(phase9.get("analysis"), dict) else {}
        original = self.privacy.mask((draft or {}).get("original_answer"), customer_names=names)
        edited = self.privacy.mask((draft or {}).get("edited_answer"), customer_names=names)
        source = learning_source
        quality = self.quality.score(source, original, masked_answer)
        digest = hashlib.sha256(
            f"{source}|{inquiry.get('id')}|{masked_question}|{masked_answer}".encode("utf-8")
        ).hexdigest()
        validator = str((draft or {}).get("validation_status") or "")
        return {
            "source_key": digest,
            "inquiry_id": inquiry.get("id"),
            "answer_draft_id": (draft or {}).get("id"),
            "approval_history_id": history_id,
            "learning_source": source,
            "question_original_masked": masked_question,
            "question_normalized": normalize_learning_question(masked_question),
            "store_code": inquiry.get("store_code"),
            "inquiry_type": inquiry.get("inquiry_type"),
            "intent": plan.get("detected_intent") or analysis.get("detected_intent") or analysis.get("primary_intent"),
            "product_name": self.privacy.mask(inquiry.get("product_name")),
            "model_code": self.privacy.mask(metadata.get("model_code")),
            "generation_mode": metadata.get("generation_mode") or (draft or {}).get("source"),
            "template_id": metadata.get("template_id"),
            "processing_route": plan.get("selected_answer_route") or metadata.get("selected_answer_route"),
            "validator_result": validator or ("SOURCE_ANSWERED" if source == "SELLER_ANSWER" else "PASSED"),
            "seller_answer": masked_answer if source == "SELLER_ANSWER" else None,
            "gpt_draft": original if (draft or {}).get("source") == "GPT" else None,
            "edited_answer": edited or None,
            "final_answer": masked_answer,
            "posted": bool((draft or {}).get("posted") or inquiry.get("post_status") == "POSTED"),
            "posted_at": (draft or {}).get("posted_at") or inquiry.get("posted_at"),
            "auto_posted": False,
            "rating": quality.rating,
            "edit_ratio": quality.edit_ratio,
            "quality_score": quality.quality_score,
            "style_only": source == "SELLER_ANSWER",
            "version": 1,
            "style_features_json": self.quality.style_features(masked_answer),
            "metadata_json": {"facts_authority": "STYLE_ONLY" if source == "SELLER_ANSWER" else "APPROVED_REFERENCE"},
            "active": True,
        }

    def capture_approved(self, *, inquiry_id: int, draft_id: int, history_id: int | None = None) -> dict[str, Any] | None:
        inquiry, draft = self.inquiries.get(inquiry_id), self.answers.get(draft_id)
        if not inquiry or not draft or str(draft.get("review_status")).upper() != "APPROVED":
            return None
        if str(draft.get("validation_status") or "").upper().startswith("FAILED"):
            return None
        final = str(draft.get("final_answer") or "").strip()
        if not final:
            return None
        source = "APPROVED_EDITED" if str(draft.get("edited_answer") or "").strip() else "APPROVED_UNEDITED"
        example = self._build(inquiry=inquiry, draft=draft, learning_source=source, answer=final, history_id=history_id)
        if example is None:
            return None
        existing = self.repository.get_by_source_key(example["source_key"])
        if existing is not None:
            self.logs.record_inquiry(
                inquiry_id,
                "LEARNING_RECORD_SKIPPED",
                "동일한 최종 답변이 이미 저장되어 Learning 중복 저장을 건너뛰었습니다.",
                details={
                    "learning_example_id": existing["id"],
                    "learning_source": source,
                    "reason": "DUPLICATE_FINAL_ANSWER",
                },
            )
            return existing
        saved = self.repository.upsert(example)
        self.logs.record_inquiry(
            inquiry_id,
            "LEARNING_RECORD_CREATED",
            "승인된 최종 답변을 Learning Repository에 저장했습니다.",
            details={
                "learning_example_id": saved["id"],
                "learning_source": source,
                "rating": saved["rating"],
            },
        )
        self.logs.record_inquiry(inquiry_id, "LEARNING_EXAMPLE_SAVED", "승인된 최종 답변을 Learning Repository에 저장했습니다.", details={"learning_example_id": saved["id"], "learning_source": source, "rating": saved["rating"]})
        return saved

    def import_existing_seller_answers(self, *, limit: int | None = None) -> dict[str, int]:
        sql = "SELECT id FROM inquiries WHERE source_answered=1 ORDER BY id"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"; params = (max(0, int(limit)),)
        result = {"scanned": 0, "saved": 0, "excluded": 0, "unavailable": 0}
        with self.database.connection() as connection:
            ids = [int(row[0]) for row in connection.execute(sql, params).fetchall()]
        for inquiry_id in ids:
            result["scanned"] += 1
            inquiry = self.inquiries.get(inquiry_id) or {}
            answer = _first_text(inquiry.get("raw_json"))
            if not answer:
                result["unavailable"] += 1; continue
            example = self._build(inquiry=inquiry, draft=None, learning_source="SELLER_ANSWER", answer=answer, seller_answer=answer)
            if example is None:
                result["excluded"] += 1; continue
            self.repository.upsert(example); result["saved"] += 1
        return result

    def capture_seller_answer(self, *, inquiry_id: int, answer: str) -> dict[str, Any] | None:
        inquiry = self.inquiries.get(inquiry_id)
        if not inquiry:
            return None
        example = self._build(
            inquiry=inquiry, draft=None, learning_source="SELLER_ANSWER",
            answer=answer, seller_answer=answer,
        )
        return self.repository.upsert(example) if example is not None else None

    def mark_posted(self, inquiry_id: int, *, posted_at: str | None, auto_posted: bool = False) -> int:
        return self.repository.mark_posted(inquiry_id, posted_at=posted_at, auto_posted=auto_posted)

    def deactivate_draft(self, draft_id: int) -> int:
        return self.repository.deactivate_draft(draft_id)

    def capture_auto_post_version(
        self, *, inquiry_id: int, version_id: int, source: str,
    ) -> dict[str, Any] | None:
        normalized = str(source or "").upper()
        if normalized not in {
            "AUTO_POST_CORRECTED", "AUTO_POST_REVIEWED_NO_CHANGE",
        }:
            raise ValueError(f"Unsupported auto-post learning source: {source}")
        inquiry = self.inquiries.get(int(inquiry_id))
        with self.database.connection() as connection:
            version = connection.execute(
                "SELECT * FROM answer_versions WHERE id=? AND inquiry_id=?",
                (int(version_id), int(inquiry_id)),
            ).fetchone()
        if inquiry is None or version is None:
            return None
        value = dict(version)
        if str(value.get("naver_status") or "").upper() != "POSTED":
            return None
        allowed_kind = (
            "NAVER_CORRECTION_APPLIED"
            if normalized == "AUTO_POST_CORRECTED"
            else "REVIEWED_NO_CHANGE"
        )
        if str(value.get("version_kind") or "").upper() != allowed_kind:
            return None
        draft = (
            self.answers.get(int(value["answer_draft_id"]))
            if value.get("answer_draft_id") is not None else None
        )
        previous = None
        attempt = None
        with self.database.connection() as connection:
            if value.get("previous_version_id") is not None:
                previous_row = connection.execute(
                    "SELECT * FROM answer_versions WHERE id=?",
                    (int(value["previous_version_id"]),),
                ).fetchone()
                previous = dict(previous_row) if previous_row is not None else None
            attempt_row = connection.execute(
                """
                SELECT * FROM naver_post_attempts
                WHERE inquiry_id=? AND status='SUCCEEDED'
                ORDER BY id DESC LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
            attempt = dict(attempt_row) if attempt_row is not None else None
        example = self._build(
            inquiry=inquiry,
            draft=draft,
            learning_source=normalized,
            answer=str(value.get("answer_body") or ""),
        )
        if example is None:
            return None
        example["posted"] = True
        example["auto_posted"] = True
        example["posted_at"] = value.get("posted_at")
        example["edited_answer"] = self.privacy.mask(value.get("answer_body"))
        example["metadata_json"] = {
            **(example.get("metadata_json") or {}),
            "facts_authority": "STAFF_VERIFIED_NAVER_FINAL",
            "answer_version_id": int(version_id),
            "previous_answer_version_id": value.get("previous_version_id"),
            "naver_post_attempt_id": (attempt or {}).get("id"),
            "original_auto_post_answer": self.privacy.mask(
                (previous or {}).get("answer_body")
                or (draft or {}).get("final_answer")
            ),
            "staff_edited_final_answer": self.privacy.mask(value.get("answer_body")),
            "edit_detected_at": value.get("modified_at"),
            "source_priority": normalized,
        }
        existing = self.repository.get_by_source_key(example["source_key"])
        if existing is not None:
            if normalized == "AUTO_POST_CORRECTED":
                self.repository.deactivate_automatic_positive(
                    int(inquiry_id), superseded_by_learning_id=int(existing["id"])
                )
            return existing
        saved = self.repository.upsert(example)
        if normalized == "AUTO_POST_CORRECTED":
            self.repository.deactivate_automatic_positive(
                int(inquiry_id), superseded_by_learning_id=int(saved["id"])
            )
        self.logs.record_inquiry(
            int(inquiry_id), "LEARNING_RECORD_CREATED",
            "네이버 반영이 확인된 사후검토 답변을 Learning Repository에 저장했습니다.",
            details={
                "learning_example_id": saved["id"],
                "learning_source": normalized,
                "answer_version_id": int(version_id),
            },
        )
        return saved

    def capture_auto_unchanged_accepted(
        self, *, inquiry_id: int, version_id: int, post_attempt_id: int,
        observed_answer: str, observed_at: str, observation_days: int,
    ) -> dict[str, Any] | None:
        inquiry = self.inquiries.get(int(inquiry_id))
        with self.database.connection() as connection:
            version = connection.execute(
                "SELECT * FROM answer_versions WHERE id=? AND inquiry_id=?",
                (int(version_id), int(inquiry_id)),
            ).fetchone()
        if inquiry is None or version is None:
            return None
        value = dict(version)
        draft = self.answers.get(int(value["answer_draft_id"]))
        example = self._build(
            inquiry=inquiry, draft=draft,
            learning_source="AUTO_POST_REVIEWED_NO_CHANGE",
            answer=str(observed_answer or ""),
        )
        if example is None:
            return None
        example.update({
            "posted": True,
            "auto_posted": True,
            "posted_at": value.get("posted_at"),
            "edited_answer": self.privacy.mask(observed_answer),
            # Observation without an explicit staff action is intentionally weak.
            "rating": min(int(example.get("rating") or 3), 3),
            "quality_score": min(float(example.get("quality_score") or 0.6), 0.6),
            "metadata_json": {
                **(example.get("metadata_json") or {}),
                "facts_authority": "OBSERVED_UNCHANGED_REFERENCE",
                "acceptance_mode": "AUTO_OBSERVATION",
                "answer_version_id": int(version_id),
                "naver_post_attempt_id": int(post_attempt_id),
                "observed_at": observed_at,
                "observation_days": int(observation_days),
                "source_priority": "AUTO_POST_UNCHANGED_ACCEPTED",
            },
        })
        existing = self.repository.get_by_source_key(example["source_key"])
        if existing is not None:
            return existing
        saved = self.repository.upsert(example)
        self.logs.record_inquiry(
            int(inquiry_id), "AUTO_POST_UNCHANGED_LEARNING_SAVED",
            "관찰기간 동안 수정되지 않은 자동등록 답변을 약한 Positive Learning으로 저장했습니다.",
            details={
                "learning_example_id": int(saved["id"]),
                "observation_days": int(observation_days),
                "answer_version_id": int(version_id),
            },
        )
        return saved

    def capture_historical_promotion(
        self, *, case: dict[str, Any], actor: str,
    ) -> dict[str, Any]:
        """Promote one admin-reviewed case through the existing Learning store."""

        question = str(case.get("question") or "").strip()
        answer = str(case.get("seller_answer") or "").strip()
        if not question or not answer:
            raise ValueError("문의와 판매자 답변이 모두 있어야 승격할 수 있습니다.")
        source_key = hashlib.sha256(
            f"HISTORICAL_PROMOTED|{case.get('fingerprint')}".encode("utf-8")
        ).hexdigest()
        existing = self.repository.get_by_source_key(source_key)
        if existing is not None:
            return existing
        quality_score = max(0.0, min(float(case.get("quality_score") or 0), 1.0))
        rating = max(1, min(5, int(round(quality_score * 5))))
        example = {
            "source_key": source_key,
            "inquiry_id": case.get("inquiry_id"),
            "answer_draft_id": None,
            "approval_history_id": None,
            # Existing schema source is retained; metadata identifies this as
            # an explicitly approved Historical promotion.
            "learning_source": "APPROVED_EDITED",
            "question_original_masked": self.privacy.mask(question),
            "question_normalized": normalize_learning_question(question),
            "store_code": case.get("store_code"),
            "inquiry_type": case.get("inquiry_type"),
            "intent": case.get("classification"),
            "product_name": self.privacy.mask(case.get("product_name")) or None,
            "model_code": None,
            "generation_mode": "HISTORICAL_ADMIN_PROMOTION",
            "template_id": None,
            "processing_route": None,
            "validator_result": "HISTORICAL_ADMIN_APPROVED",
            "seller_answer": None,
            "gpt_draft": None,
            "edited_answer": self.privacy.mask(answer),
            "final_answer": self.privacy.mask(answer),
            "posted": True,
            "posted_at": case.get("answer_updated_at"),
            "auto_posted": False,
            "rating": rating,
            "edit_ratio": 0.0,
            "quality_score": quality_score,
            "style_only": False,
            "version": 1,
            "style_features_json": self.quality.style_features(answer),
            "metadata_json": {
                "facts_authority": "ADMIN_APPROVED_REFERENCE",
                "source_origin": "HISTORICAL_PROMOTED",
                "historical_case_id": case.get("id"),
                "historical_fingerprint": case.get("fingerprint"),
                "promoted_by": str(actor or "관리자"),
                "policy_risk": case.get("policy_risk"),
            },
            "active": True,
        }
        saved = self.repository.upsert(example)
        if case.get("inquiry_id") is not None:
            self.logs.record_inquiry(
                int(case["inquiry_id"]),
                "HISTORICAL_CASE_PROMOTED",
                "관리자가 검토한 과거 사례를 Learning Repository로 승격했습니다.",
                details={
                    "historical_case_id": case.get("id"),
                    "learning_example_id": saved["id"],
                    "actor": str(actor or "관리자"),
                },
            )
        return saved

    def import_existing_approved(self) -> dict[str, int]:
        result = {"scanned": 0, "saved": 0, "excluded": 0}
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT d.inquiry_id, d.id AS draft_id,
                       (SELECT h.id FROM approval_history h
                        WHERE h.answer_draft_id=d.id AND h.action='APPROVED'
                        ORDER BY h.id DESC LIMIT 1) AS history_id
                FROM answer_drafts d
                WHERE d.review_status='APPROVED' AND trim(COALESCE(d.final_answer,''))<>''
                """
            ).fetchall()
        for row in rows:
            result["scanned"] += 1
            saved = self.capture_approved(
                inquiry_id=int(row["inquiry_id"]), draft_id=int(row["draft_id"]),
                history_id=int(row["history_id"]) if row["history_id"] is not None else None,
            )
            result["saved" if saved else "excluded"] += 1
        return result
