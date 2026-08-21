from __future__ import annotations

from typing import Any

import pytest

from answer.learning_signal import OriginKind, SignalKind
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.learning_signal_repository import LearningSignalRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services.learning_feedback_service import LearningFeedbackService
from services.learning_signal_service import LearningSignalService
from tests.test_learning_feedback import make_context


@pytest.fixture(autouse=True)
def _auto_learning_env(monkeypatch):
    monkeypatch.setenv("AUTO_STRUCTURED_LEARNING_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_PROMOTION_ENABLED", "true")
    monkeypatch.setenv("AUTO_VERIFIED_FACT_MIN_CONFIRMATIONS", "3")
    yield


def approve_with_edit(database, inquiry_id, draft_id, *, edited_answer, actor="직원"):
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id, draft_id=draft_id, edited_answer=edited_answer,
    )
    return ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft_id, actor=actor,
    )


def signals_for_inquiry(database, inquiry_id) -> list[dict[str, Any]]:
    return LearningSignalRepository(database).for_inquiry(inquiry_id)


# ---------------------------------------------------------------------------
# Case A -- plain Positive, no meaningful diff
# ---------------------------------------------------------------------------


def test_case_a_plain_positive_creates_no_verified_fact(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="직원",
    )
    positive = LearningRepository(database).candidates(store_code="OJE_PLUS")
    assert len(positive) == 1
    assert signals_for_inquiry(database, inquiry_id) == []


# ---------------------------------------------------------------------------
# Case B -- style-only edit
# ---------------------------------------------------------------------------


def test_case_b_style_only_edit_creates_no_verified_fact(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    approve_with_edit(
        database, inquiry_id, draft["id"],
        edited_answer="네, 상품 설명서를 확인해 주세요.",
    )
    signals = signals_for_inquiry(database, inquiry_id)
    factual = [s for s in signals if s["signal_kind"] in {"CORRECTION", "VERIFIED_FACT"}]
    assert factual == []


# ---------------------------------------------------------------------------
# Case C -- an obviously wrong pattern (unconditional order-number request)
# is removed for a general policy question
# ---------------------------------------------------------------------------


def test_case_c_removed_order_number_requirement_becomes_bad_pattern(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    inquiries = InquiryRepository(database)
    # This fixture's own Program Answer doesn't request an order number, so
    # the realistic Case C shape (Program requests one, Final removes it
    # for a general policy question) is exercised directly against the
    # extraction service rather than through the approval flow.
    service = LearningSignalService(database)
    inquiry = inquiries.get(inquiry_id)
    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        inquiry=inquiry,
        question="반품 정책이 어떻게 되나요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="정확한 확인을 위해 주문번호를 남겨주세요.",
        final_answer=(
            "반품은 상품 수령 후 7일 이내 가능하며 자세한 절차는 고객센터로 "
            "문의해 주세요."
        ),
        learning_example_id=None,
    )
    bad_patterns = [s for s in saved if s["signal_kind"] == "BAD_PATTERN"]
    assert bad_patterns, "removing an unconditional order-number request must surface guidance"
    for item in bad_patterns:
        assert "모든 문의" not in item["content_text"]
        assert item["product_scope"] != "GLOBAL" or "일반 문의" in item["content_text"]


# ---------------------------------------------------------------------------
# Case D -- Jeju: avoidance replaced by a concrete claim must be a CANDIDATE,
# never an immediately-eligible GLOBAL VERIFIED_FACT from one edit alone
# ---------------------------------------------------------------------------


def test_case_d_jeju_single_edit_stays_candidate_not_global_verified(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    inquiry = InquiryRepository(database).get(inquiry_id)
    service = LearningSignalService(database)
    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        inquiry=inquiry,
        question="제주도 배송 여부는 확인이 필요합니다.",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="제주도 배송 여부는 확인이 필요합니다.",
        final_answer="제주도 배송 및 설치 가능합니다.",
        learning_example_id=None,
    )
    factual = [s for s in saved if s["signal_kind"] in {"CORRECTION", "VERIFIED_FACT"}]
    assert factual, "an avoidance-to-concrete-claim edit must produce a candidate"
    for item in factual:
        assert item["confirmation_status"] == "ACTIVE"
        assert item["generation_mode"] == "AUTO_EXTRACTED"
        assert item["product_scope"] != "GLOBAL"

    result = service.retrieve(
        "제주도도 배송설치 가능한가요?", store_code="OJE_PLUS",
    )
    assert result["verified_facts"] == [] and result["corrections"] == [], (
        "one STAFF_EDITED confirmation alone must never make this usable evidence"
    )


# ---------------------------------------------------------------------------
# Case F -- CURRENT_DPS/order-dependent diffs never produce a candidate
# ---------------------------------------------------------------------------


def test_case_f_schedule_dependent_diff_produces_no_candidate(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    inquiry = InquiryRepository(database).get(inquiry_id)
    service = LearningSignalService(database)
    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        inquiry=inquiry,
        question="설치 예정일이 언제인가요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="설치 예정일을 확인할 수 없습니다.",
        final_answer="설치 예정일은 2026년 8월 30일입니다.",
        learning_example_id=None,
    )
    assert saved == []


# ---------------------------------------------------------------------------
# Case G -- Conflict: an existing manual VERIFIED_FACT must not be silently
# contradicted once an auto-extracted correction becomes eligible
# ---------------------------------------------------------------------------


def test_case_g_conflict_between_manual_fact_and_promoted_auto_correction(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    inquiry = InquiryRepository(database).get(inquiry_id)
    service = LearningSignalService(database)
    manual = service.capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        signal_kind=SignalKind.VERIFIED_FACT,
        content_text="운영 확인 결과 제주도 배송이 불가능합니다.",
        inquiry=inquiry,
        question="제주도 배송 가능한가요?",
        product_name=inquiry.get("product_name"),
    )
    assert manual is not None

    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        inquiry=inquiry,
        question="제주도 배송 가능한가요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="제주도 배송 여부는 확인이 필요합니다.",
        final_answer="제주도 배송이 가능합니다.",
        learning_example_id=None,
    )
    factual = [s for s in saved if s["signal_kind"] in {"CORRECTION", "VERIFIED_FACT"}]
    assert factual
    LearningSignalRepository(database).promote(factual[0]["id"], actor="관리자")

    result = service.retrieve("제주도 배송 가능한가요?", store_code="OJE_PLUS")
    assert result["verified_facts"] == [] and result["corrections"] == [], (
        "a manually-promoted auto-correction that conflicts with an active "
        "manual VERIFIED_FACT must never be handed to GPT as usable evidence"
    )
    assert result["conflicts"], "the conflict must be surfaced, not silently resolved"


# ---------------------------------------------------------------------------
# Case H -- temporary/event-scoped content never becomes a PERMANENT fact
# ---------------------------------------------------------------------------


def test_case_h_temporary_promotion_content_produces_no_factual_candidate(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    inquiry = InquiryRepository(database).get(inquiry_id)
    service = LearningSignalService(database)
    saved = service.auto_extract_and_capture(
        origin_kind=OriginKind.POSITIVE_REVIEW,
        inquiry=inquiry,
        question="지금 사은품 행사하나요?",
        source_authority="STAFF_EDITED_HUMAN_VERIFIED",
        program_answer="확인이 필요합니다.",
        final_answer="현재 삼성 감사제 이벤트 기간으로 사은품이 제공됩니다.",
        learning_example_id=None,
    )
    factual = [s for s in saved if s["signal_kind"] in {"CORRECTION", "VERIFIED_FACT"}]
    assert factual == []


# ---------------------------------------------------------------------------
# Case I -- AI self-loop: an unreviewed auto-post-accepted answer must never
# feed the extraction pipeline
# ---------------------------------------------------------------------------


def test_case_i_auto_unchanged_acceptance_never_calls_extraction(tmp_path, monkeypatch) -> None:
    from services.learning_service import LearningService

    database, inquiry_id, draft = make_context(tmp_path)
    called = {"count": 0}
    original = LearningSignalService.auto_extract_and_capture

    def spy(self, *args, **kwargs):
        called["count"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(LearningSignalService, "auto_extract_and_capture", spy)

    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO answer_versions(
                inquiry_id, answer_draft_id, version_number, version_kind,
                answer_body, actor, author_type, finalization_source,
                approval_status, naver_status, answer_hash
            ) VALUES (?, ?, 1, 'AUTO_POST_INITIAL', ?, 'SYSTEM_AUTO_POST',
                'SYSTEM_AUTO_POST', 'AUTO_POST', 'PENDING', 'POSTED', 'hash1')
            """,
            (inquiry_id, draft["id"], "상품 설명서를 확인해 주세요."),
        )
        version_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    LearningService(database).capture_auto_unchanged_accepted(
        inquiry_id=inquiry_id, version_id=version_id, post_attempt_id=1,
        observed_answer="상품 설명서를 확인해 주세요.",
        observed_at="2026-08-21T00:00:00Z", observation_days=7,
    )
    assert called["count"] == 0
    factual = [
        s for s in signals_for_inquiry(database, inquiry_id)
        if s["signal_kind"] in {"CORRECTION", "VERIFIED_FACT"}
    ]
    assert factual == []


# ---------------------------------------------------------------------------
# Case K -- regression: feature flag off means byte-for-byte unchanged
# ---------------------------------------------------------------------------


def test_case_k_flag_off_matches_pre_feature_behavior(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_STRUCTURED_LEARNING_ENABLED", "false")
    database, inquiry_id, draft = make_context(tmp_path)
    approve_with_edit(
        database, inquiry_id, draft["id"],
        edited_answer="제주도 배송 및 설치 가능합니다.",
    )
    assert signals_for_inquiry(database, inquiry_id) == []
    positive = LearningRepository(database).candidates(store_code="OJE_PLUS")
    assert len(positive) == 1
