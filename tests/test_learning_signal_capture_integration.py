from __future__ import annotations

from answer.learning_feedback import CorrectionReason
from repositories.learning_signal_repository import LearningSignalRepository
from services.approval_service import ApprovalService
from services.learning_feedback_service import LearningFeedbackService
from tests.test_learning_feedback import make_context, make_historical_case


def test_approve_with_good_pattern_creates_positive_learning_signal(tmp_path) -> None:
    """Operator-tagged Positive review notes must actually reach the
    Structured Learning Signal store, not just sit in metadata_json."""

    database, inquiry_id, draft = make_context(tmp_path)
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        positive_signal_kind="GOOD_PATTERN",
        positive_signal_content="안내 순서와 표현이 명확하여 향후 유사 문의의 모범 답안으로 참고할 것.",
    )
    signals = LearningSignalRepository(database).for_inquiry(inquiry_id)
    assert len(signals) == 1
    assert signals[0]["signal_kind"] == "GOOD_PATTERN"
    assert signals[0]["origin_kind"] == "POSITIVE_REVIEW"
    assert signals[0]["learning_example_id"] is not None


def test_approve_with_reason_only_creates_no_signal(tmp_path) -> None:
    """The default path (categorical reason only) must remain a no-op for
    the new Structured Signal store -- existing behavior unchanged."""

    database, inquiry_id, draft = make_context(tmp_path)
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        positive_reason="CONTENT_ACCURATE",
        positive_note="그냥 좋음",
    )
    assert LearningSignalRepository(database).for_inquiry(inquiry_id) == []


def test_save_edited_answer_with_correction_signal_creates_bad_pattern(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        edited_answer="주문정보 확인 후 안내드리겠습니다.",
        correction_reason=CorrectionReason.FACT_ERROR.value,
        correction_note="사실 확인 없이 답변함",
        correction_signal_kind="BAD_PATTERN",
        correction_signal_content="확인 없이 배송 완료를 단정하는 표현을 피할 것.",
    )
    signals = LearningSignalRepository(database).for_inquiry(inquiry_id)
    assert len(signals) == 1
    assert signals[0]["signal_kind"] == "BAD_PATTERN"
    assert signals[0]["origin_kind"] == "NEGATIVE_REVIEW"
    assert signals[0]["learning_feedback_id"] is not None


def test_capture_dashboard_negative_with_correction_signal(tmp_path) -> None:
    database, inquiry_id, draft = make_context(tmp_path)
    LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="PROGRAM_GENERATED",
        original_answer_reference_id=int(draft["id"]),
        correction_reason=CorrectionReason.FACT_ERROR.value,
        correction_note="잘못된 안내",
        signal_kind="CORRECTION",
        signal_content="운영 확인 결과 실제로는 자가설치만 가능합니다.",
    )
    signals = LearningSignalRepository(database).for_inquiry(inquiry_id)
    assert len(signals) == 1
    assert signals[0]["signal_kind"] == "CORRECTION"


def test_capture_historical_review_with_bad_pattern_links_historical_case(
    tmp_path,
) -> None:
    database, _inquiry_id, _draft = make_context(tmp_path)
    case = make_historical_case(database)
    LearningFeedbackService(database).capture_historical_review(
        case_id=int(case["id"]),
        correction_reason=CorrectionReason.OTHER.value,
        correction_note="사례 부적절",
        signal_kind="BAD_PATTERN",
        signal_content="문의와 무관한 설명을 포함하지 말 것.",
    )
    signals = LearningSignalRepository(database).for_historical_case(int(case["id"]))
    assert len(signals) == 1
    assert signals[0]["signal_kind"] == "BAD_PATTERN"
    assert signals[0]["origin_kind"] == "HISTORICAL_REVIEW"
