from __future__ import annotations

from copy import deepcopy

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from scripts.audit_historical_reuse import _near_duplicate, audit
from services.historical_reaudit_service import HistoricalReauditService


def assess(question: str, answer: str, **values):
    return HistoricalReauditService().assess({
        "question": question,
        "seller_answer": answer,
        "quality_score": 0.85,
        "policy_risk": "NONE",
        "active": True,
        "source_answered": True,
        **values,
    })


def test_stable_general_policy_is_safe_reusable() -> None:
    result = assess(
        "반품 절차는 어떻게 되나요?",
        "반품 접수 후 안내된 방법에 따라 상품을 포장해 회수 기사에게 전달해 주세요.",
    )
    assert result.disposition == "SAFE_REUSABLE"
    assert result.runtime_context_eligible is True


def test_question_answer_mismatch_is_unsafe() -> None:
    result = assess(
        "반품 박스가 없어 회수 방법을 알고 싶습니다.",
        "넷플릭스는 셋톱박스를 연결하면 시청할 수 있습니다.",
    )
    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason == "QUESTION_ANSWER_MISMATCH"


@pytest.mark.parametrize(
    ("question", "answer"),
    (
        ("설치일이 언제인가요?", "해당 주문은 2026-08-25 설치 예정입니다."),
        ("배송 상태가 궁금합니다.", "조회 결과 현재 배송중으로 확인됩니다."),
    ),
)
def test_current_order_dates_and_status_are_unsafe(question: str, answer: str) -> None:
    result = assess(question, answer)
    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason in {
        "CUSTOMER_OR_ORDER_SPECIFIC_FACT", "ORDER_SPECIFIC_FACT",
        "EXPIRED_OR_DATED_TEMPORARY",
    }


def test_expired_promotion_is_unsafe() -> None:
    result = assess(
        "온누리 환급 혜택은 언제까지인가요?",
        "온누리 환급은 7월 5일까지 구매한 주문에 한해 신청할 수 있습니다.",
    )
    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason == "EXPIRED_OR_DATED_TEMPORARY"


def test_unbounded_event_never_becomes_permanent_automatically() -> None:
    result = assess(
        "행사 배송 기간이 궁금합니다.",
        "삼성 감사제 주문량 증가로 배송이 평소보다 지연되고 있습니다.",
    )
    assert result.disposition == "REVIEW_REQUIRED"
    assert result.primary_reason == "UNBOUNDED_TEMPORARY_EVENT"
    assert result.runtime_context_eligible is False


def test_acknowledgement_only_is_unsafe() -> None:
    result = assess("취소해 주세요.", "처리되었습니다.")
    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason == "ACKNOWLEDGEMENT_ONLY"


@pytest.mark.parametrize(
    ("question", "answer", "expected"),
    (
        (
            "주문 취소가 제대로 됐나요?",
            "로지텍 주문건을 삭제해서 1대만 출고되도록 하겠습니다.",
            "UNSAFE_NOT_REUSABLE",
        ),
        (
            "운송장 번호를 알려주세요.",
            "로젠택배 운송장은 448-8743-1680입니다.",
            "UNSAFE_NOT_REUSABLE",
        ),
        (
            "현재 75인치 TV를 판매하나요?",
            "현재 삼성 75인치 TV 상품은 판매 중입니다.",
            "REVIEW_REQUIRED",
        ),
        (
            "배송 가능한 지역인가요?",
            "현재 확인이 어려워 담당자 검토가 필요합니다.",
            "REVIEW_REQUIRED",
        ),
        (
            "감사제 구매처가 어디인가요?",
            "안녕하세요 고객님 네이버입니다.",
            "REVIEW_REQUIRED",
        ),
    ),
)
def test_low_value_dynamic_and_customer_specific_answers_are_not_safe(
    question: str, answer: str, expected: str
) -> None:
    assert assess(question, answer).disposition == expected


def test_negative_or_excluded_source_cannot_be_safe() -> None:
    result = assess(
        "설치 방법을 알려주세요.",
        "기사 설치 상품은 배송 시 기본 설치를 진행합니다.",
        metadata_json={"learning_signal_type": "EXCLUDED"},
    )
    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason == "NEGATIVE_EXCLUDED_OR_REVOKED"


def test_human_verified_metadata_is_not_downgraded_by_read_only_assessment() -> None:
    row = {
        "question": "설치 방법을 알려주세요.",
        "seller_answer": "기사 설치 상품은 배송 시 기본 설치를 진행합니다.",
        "quality_score": 0.9,
        "policy_risk": "NONE",
        "active": True,
        "source_answered": True,
        "metadata_json": {
            "learning_signal_type": "POSITIVE", "human_verified": True,
        },
    }
    before = deepcopy(row)
    result = HistoricalReauditService().assess(row)
    assert result.disposition == "SAFE_REUSABLE"
    assert row == before
    assert row["metadata_json"]["human_verified"] is True


def test_uncertain_product_fact_scope_requires_review() -> None:
    result = assess(
        "이 제품 HDMI 단자는 몇 개인가요?",
        "이 제품에는 HDMI 단자가 3개 제공됩니다.",
        product_name="TV",
        product_id=None,
    )
    assert result.disposition == "REVIEW_REQUIRED"
    assert result.primary_reason == "PRODUCT_SCOPE_UNCERTAIN"


def test_masking_does_not_make_order_specific_fact_reusable() -> None:
    result = assess(
        "주문번호 2026081392706071 배송 상태를 알려주세요.",
        "해당 주문은 8월 25일 기사 방문 예정입니다.",
    )
    assert result.personal_information_detected is True
    assert result.disposition == "UNSAFE_NOT_REUSABLE"


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (
            "오늘 배송된다고 하셨는데 오늘 올까요?",
            "주문 시 확인하신 도착일자에 맞춰 배송됩니다.",
        ),
        (
            "온누리 신청에 넣을 주문번호를 확인해 주세요.",
            "확인되는 고객님의 주문번호는 2026****1251 입니다.",
        ),
        (
            "기존 주문의 배송일자인 8월 22일로 유지할 수 있을까요?",
            "일정 유지는 불가능합니다.",
        ),
        (
            "배송날짜가 17일인데 14일로 변경 요청합니다.",
            "해당 일정은 가장 빠른 일정으로 반영된 상태입니다.",
        ),
    ],
)
def test_current_order_question_or_masked_customer_identifier_is_unsafe(
    question: str, answer: str
) -> None:
    result = assess(question, answer)

    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason == "CUSTOMER_OR_ORDER_SPECIFIC_FACT"


def test_temporary_event_mentioned_only_in_question_is_not_safe() -> None:
    result = assess(
        "삼성 감사제 신청에 사용할 모델 코드는 무엇인가요?",
        "패키지 상품 코드는 상세 페이지에서 확인하실 수 있습니다.",
    )

    assert result.disposition == "REVIEW_REQUIRED"
    assert result.primary_reason == "UNBOUNDED_TEMPORARY_EVENT"


def test_customer_cancellation_request_is_not_reusable_policy() -> None:
    result = assess(
        "배송이 너무 늦어서 취소해주세요.",
        "판매자센터에서 취소 접수를 진행하겠습니다.",
    )

    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason == "CUSTOMER_OR_ORDER_SPECIFIC_FACT"


def test_event_only_in_question_and_customer_name_are_conservative() -> None:
    event = assess(
        "7월 27일 라이브 방송 포인트는 언제 지급되나요?",
        "당첨자에게 네이버 알림으로 지급을 안내합니다.",
    )
    named = assess(
        "화면에 검은 점이 보여요.",
        "김범철 고객님, 제조사 판정 후 교환할 수 있습니다.",
    )

    assert event.disposition != "SAFE_REUSABLE"
    assert named.disposition == "UNSAFE_NOT_REUSABLE"
    assert "김범철" not in named.masked_answer
    assert "<masked-name>" in named.masked_answer


def test_customer_specific_action_promise_is_unsafe() -> None:
    result = assess(
        "더 큰 사이즈로 변경할 수 있을까요?",
        "반품 요청 주시면 확인 후 승인 처리 도와드리겠습니다.",
    )

    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason == "CUSTOMER_OR_ORDER_SPECIFIC_FACT"


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        (
            "TV를 이곳에서 사고 시킨 건데 한 달째 안 와서 취소하려고요.",
            "저희 쪽에서 회수 접수를 진행해드릴 예정입니다.",
        ),
        (
            "배송문의 언제까지 보내주나요?",
            "현재 확인 시 08/10 예정으로 확인됩니다.",
        ),
    ],
)
def test_past_purchase_experience_or_answer_date_is_unsafe(
    question: str, answer: str
) -> None:
    result = assess(question, answer)

    assert result.disposition == "UNSAFE_NOT_REUSABLE"


def test_contact_only_answer_is_not_reusable_knowledge() -> None:
    result = assess(
        "모니터 화면이 파손되어 있습니다.",
        "안녕하세요 고객님. 불편드려 죄송합니다. 유선 안내되었습니다.",
    )

    assert result.disposition == "UNSAFE_NOT_REUSABLE"
    assert result.primary_reason == "ANSWER_TOO_GENERIC"


def test_135_166_183_failure_shapes_are_generalized_without_ids() -> None:
    mismatch = assess(
        "반품 박스를 기사님이 가져가 직접 반품 방법을 알고 싶습니다.",
        "OTT는 셋톱박스로 시청할 수 있고 단순변심 반품은 어렵습니다.",
    )
    temporary = assess(
        "스탠드만 먼저 와서 반품 절차를 알고 싶습니다.",
        "[8/3~8/4] 하계 휴가 후 스탠드 개봉 여부를 확인하겠습니다.",
    )
    order = assess(
        "배송완료인데 상품을 받지 못했습니다.",
        "스탠드가 누락된 것으로 보여 익일 출고하겠습니다.",
    )
    assert mismatch.primary_reason == "QUESTION_ANSWER_MISMATCH"
    assert temporary.primary_reason == "EXPIRED_OR_DATED_TEMPORARY"
    assert order.primary_reason in {
        "CUSTOMER_OR_ORDER_SPECIFIC_FACT", "ORDER_SPECIFIC_FACT"
    }
    assert all(
        item.disposition == "UNSAFE_NOT_REUSABLE"
        for item in (mismatch, temporary, order)
    )


def test_semantic_near_duplicate_is_detected() -> None:
    matched, score, reason = _near_duplicate(
        {
            "question_normalized": "반품 접수 절차가 궁금합니다",
            "answer_normalized": "반품 접수 후 상품을 포장해 회수 기사에게 전달합니다",
        },
        {
            "question_normalized": "반품 접수 절차를 알려주세요",
            "answer_normalized": "반품 접수 뒤 상품을 포장하여 회수 기사에게 전달합니다",
        },
    )
    assert matched is True
    assert score >= 0.70
    assert "jaccard" in reason


def test_read_only_audit_excludes_exact_existing_positive_from_net_new(tmp_path) -> None:
    database = Database(tmp_path / "historical-reaudit.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "REAUDIT-EXACT",
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "반품 문의",
        "content": "반품 절차는 어떻게 되나요?",
        "product_name": "테스트 TV",
        "source_answered": True,
        "raw_json": {},
    }).inquiry_id
    answer = "반품 접수 후 상품을 포장해 회수 기사에게 전달해 주세요."
    LearningRepository(database).upsert({
        "source_key": "reaudit-existing-positive",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "반품 문의\n반품 절차는 어떻게 되나요?",
        "question_normalized": "반품 문의 반품 절차는 어떻게 되나요",
        "store_code": "OJE_PLUS",
        "inquiry_type": "PRODUCT_INQUIRY",
        "intent": "RETURN",
        "product_name": "테스트 TV",
        "seller_answer": answer,
        "final_answer": answer,
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 1.0,
        "style_only": False,
        "version": 1,
        "metadata_json": {
            "learning_signal_type": "POSITIVE", "human_verified": True,
        },
        "active": True,
    })

    result = audit(database.path)

    assert result["funnel"]["safe_reusable"] == 1
    assert result["funnel"]["duplicate_with_existing_learning"] == 1
    assert result["funnel"]["net_new_safe_candidate"] == 0


def test_safe_reusable_is_always_runtime_eligible() -> None:
    examples = (
        assess("A/S 접수 방법은 무엇인가요?", "제품 고장 시 제조사 서비스센터로 A/S를 접수해 주세요."),
        assess("기사님이 설치해 주시나요?", "기사 설치 상품은 배송 시 기본 설치를 진행합니다."),
        assess("반품 절차가 궁금합니다.", "반품 접수 후 안내에 따라 포장하고 회수 기사에게 전달해 주세요."),
    )
    assert all(
        item.runtime_context_eligible
        for item in examples if item.disposition == "SAFE_REUSABLE"
    )
