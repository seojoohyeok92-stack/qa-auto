from __future__ import annotations

from typing import Any

import pytest

from answer.learning_signal import OriginKind
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.learning_signal_repository import LearningSignalRepository
from services.learning_signal_service import LearningSignalService


STORE_CODE = "OJE_PLUS"


@pytest.fixture(autouse=True)
def _auto_learning_env(monkeypatch):
    monkeypatch.setenv("AUTO_STRUCTURED_LEARNING_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_PROMOTION_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_MIN_CONFIRMATIONS", "2")
    yield


def make_inquiry(
    database: Database, *, product_id: str, product_name: str, question: str,
    external_id: str | None = None,
) -> dict[str, Any]:
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item(
        {
            "store_code": STORE_CODE,
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": external_id or f"AUTO-{product_id}-{abs(hash(question)) % 100000}",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품 문의",
            "content": question,
            "product_id": product_id,
            "product_name": product_name,
            "raw_json": {},
        }
    ).inquiry_id
    return inquiries.get(inquiry_id) or {}


def make_learning_example(database: Database, *, inquiry_id: int, answer: str, source_key: str) -> int:
    row = LearningRepository(database).upsert(
        {
            "source_key": source_key,
            "learning_source": "APPROVED_EDITED",
            "inquiry_id": inquiry_id,
            "answer_draft_id": None,
            "approval_history_id": None,
            "question_original_masked": "질문",
            "question_normalized": "질문",
            "store_code": STORE_CODE,
            "inquiry_type": "PRODUCT_INQUIRY",
            "intent": "GENERAL",
            "product_name": "삼성 TV",
            "model_code": None,
            "final_answer": answer,
            "rating": 5,
            "quality_score": 1.0,
            "generation_mode": "TEST",
            "template_id": None,
            "processing_route": "TEST",
            "validator_result": "HUMAN_VERIFIED_NAVER_POSTED",
            "posted": True,
            "auto_posted": False,
            "edit_ratio": 0.1,
            "style_only": False,
            "version": 1,
            "style_features_json": {},
            "metadata_json": {"human_verified": True, "learning_signal_type": "POSITIVE"},
            "active": True,
        }
    )
    return int(row["id"])


def test_diff_extraction_creates_candidate_not_immediately_verified(tmp_path) -> None:
    database = Database(tmp_path / "auto.db")
    database.initialize()
    inquiry = make_inquiry(
        database, product_id="TV-1", product_name="삼성 TV",
        question="제주도 배송 설치 가능한가요?",
    )
    learning_id = make_learning_example(
        database, inquiry_id=inquiry["id"], answer="제주도 배송 및 설치 가능합니다.",
        source_key="auto-diff-1",
    )
    service = LearningSignalService(database)
    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        inquiry=inquiry,
        question="제주도 배송 설치 가능한가요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="제주도 배송 여부는 확인이 필요합니다.",
        final_answer="제주도 배송 및 설치 가능합니다.",
        learning_example_id=learning_id,
    )
    assert saved, "a real fact/avoidance-resolved diff must produce a candidate"
    signal = saved[0]
    assert signal["generation_mode"] == "AUTO_EXTRACTED"
    assert signal["confirmation_status"] == "ACTIVE"

    result = service.retrieve(
        "제주도도 배송설치 가능한가요?", store_code=STORE_CODE,
        product_id="TV-1", product_name="삼성 TV",
    )
    assert result["verified_facts"] == [] and result["corrections"] == [], (
        "a single confirmation must never make an auto-extracted fact usable evidence"
    )


def test_style_only_edit_produces_no_signal(tmp_path) -> None:
    database = Database(tmp_path / "auto.db")
    database.initialize()
    inquiry = make_inquiry(
        database, product_id="TV-2", product_name="삼성 TV",
        question="벽걸이 설치 가능한가요?",
    )
    learning_id = make_learning_example(
        database, inquiry_id=inquiry["id"], answer="네, 설치 가능합니다.",
        source_key="auto-style-1",
    )
    service = LearningSignalService(database)
    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        inquiry=inquiry,
        question="벽걸이 설치 가능한가요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="가능합니다.",
        final_answer="네, 설치 가능합니다.",
        learning_example_id=learning_id,
    )
    assert saved == []


def test_no_edit_at_all_produces_no_signal(tmp_path) -> None:
    database = Database(tmp_path / "auto.db")
    database.initialize()
    inquiry = make_inquiry(
        database, product_id="TV-3", product_name="삼성 TV",
        question="벽걸이 설치 가능한가요?",
    )
    learning_id = make_learning_example(
        database, inquiry_id=inquiry["id"], answer="벽걸이 설치 가능합니다.",
        source_key="auto-noedit-1",
    )
    service = LearningSignalService(database)
    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        inquiry=inquiry,
        question="벽걸이 설치 가능한가요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="벽걸이 설치 가능합니다.",
        final_answer="벽걸이 설치 가능합니다.",
        learning_example_id=learning_id,
    )
    assert saved == []


def test_repeated_independent_confirmation_promotes_to_eligible(tmp_path) -> None:
    database = Database(tmp_path / "auto.db")
    database.initialize()
    service = LearningSignalService(database)

    inquiry1 = make_inquiry(
        database, product_id="TV-4", product_name="삼성 TV",
        question="제주도 배송 설치 가능한가요?",
    )
    learning_1 = make_learning_example(
        database, inquiry_id=inquiry1["id"], answer="제주도 배송 및 설치 가능합니다.",
        source_key="auto-repeat-1",
    )
    service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW, inquiry=inquiry1,
        question="제주도 배송 설치 가능한가요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="확인이 필요합니다.",
        final_answer="제주도 배송 및 설치 가능합니다.",
        learning_example_id=learning_1,
    )
    result = service.retrieve(
        "제주도도 배송설치 가능한가요?", store_code=STORE_CODE,
        product_id="TV-4", product_name="삼성 TV",
    )
    assert result["verified_facts"] == [] and result["corrections"] == []

    inquiry2 = make_inquiry(
        database, product_id="TV-4", product_name="삼성 TV",
        question="제주 지역도 배송 설치가 가능한지 문의드립니다",
    )
    learning_2 = make_learning_example(
        database, inquiry_id=inquiry2["id"], answer="제주도 배송 및 설치 가능합니다.",
        source_key="auto-repeat-2",
    )
    saved2 = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW, inquiry=inquiry2,
        question="제주 지역도 배송 설치가 가능한지 문의드립니다",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="확인이 필요합니다.",
        final_answer="제주도 배송 및 설치 가능합니다.",
        learning_example_id=learning_2,
    )
    assert saved2, "second independent confirmation should still succeed"
    factual = [
        s for s in saved2
        if s["signal_kind"] in {"CORRECTION", "VERIFIED_FACT"}
    ]
    assert len({s["id"] for s in factual}) == 1, (
        "the same normalized fact confirmed from a second, independent "
        "inquiry must accumulate onto the same signal row, not duplicate"
    )
    signal_id = factual[0]["id"]
    assert LearningSignalRepository(database).live_confirmation_count(signal_id) == 2

    result2 = service.retrieve(
        "제주도도 배송설치 가능한가요?", store_code=STORE_CODE,
        product_id="TV-4", product_name="삼성 TV",
    )
    assert result2["verified_facts"] or result2["corrections"], (
        "2 independent confirmations with promotion enabled must become eligible"
    )


def test_promotion_disabled_keeps_signal_shadow_forever(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_VERIFIED_FACT_PROMOTION_ENABLED", "false")
    database = Database(tmp_path / "auto.db")
    database.initialize()
    service = LearningSignalService(database)
    inquiries = []
    for i in range(3):
        inquiry = make_inquiry(
            database, product_id="TV-5", product_name="삼성 TV",
            question=f"제주도 배송 설치 가능한가요 {i}?",
        )
        learning_id = make_learning_example(
            database, inquiry_id=inquiry["id"], answer="제주도 배송 및 설치 가능합니다.",
            source_key=f"auto-shadow-{i}",
        )
        service.auto_extract_and_capture(
            origin_kind=OriginKind.POSITIVE_REVIEW, inquiry=inquiry,
            question=f"제주도 배송 설치 가능한가요 {i}?",
            source_authority="STAFF_EDITED_HUMAN_VERIFIED",
            program_answer="확인이 필요합니다.",
            final_answer="제주도 배송 및 설치 가능합니다.",
            learning_example_id=learning_id,
        )
        inquiries.append(inquiry)
    result = service.retrieve(
        "제주도도 배송설치 가능한가요?", store_code=STORE_CODE,
        product_id="TV-5", product_name="삼성 TV",
    )
    assert result["verified_facts"] == [] and result["corrections"] == [], (
        "SHADOW mode: even 3 confirmations must never auto-promote when the "
        "promotion flag is off"
    )


def test_master_flag_off_is_a_complete_no_op(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_STRUCTURED_LEARNING_ENABLED", "false")
    database = Database(tmp_path / "auto.db")
    database.initialize()
    inquiry = make_inquiry(
        database, product_id="TV-6", product_name="삼성 TV",
        question="제주도 배송 설치 가능한가요?",
    )
    learning_id = make_learning_example(
        database, inquiry_id=inquiry["id"], answer="제주도 배송 및 설치 가능합니다.",
        source_key="auto-flagoff-1",
    )
    service = LearningSignalService(database)
    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW, inquiry=inquiry,
        question="제주도 배송 설치 가능한가요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="확인이 필요합니다.",
        final_answer="제주도 배송 및 설치 가능합니다.",
        learning_example_id=learning_id,
    )
    assert saved == []
    assert LearningSignalRepository(database).for_inquiry(inquiry["id"]) == []
