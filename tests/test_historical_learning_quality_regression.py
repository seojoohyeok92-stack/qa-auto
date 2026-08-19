from __future__ import annotations

from datetime import UTC, datetime

from repositories.database import Database
from services.historical_case_service import HistoricalCaseService
from services.historical_learning_quality_service import (
    HistoricalLearningQualityService,
)
from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from services.draft_generation_service import DraftGenerationService


def test_return_pickup_question_rejects_unrelated_ott_answer() -> None:
    result = HistoricalLearningQualityService().assess(
        question=(
            "반품 박스를 설치 기사님이 회수해 갔습니다. 일반 택배 반품을 "
            "할 수 없는데 직접 반품하는 방법을 알려주세요."
        ),
        answer=(
            "OTT 기능은 셋탑박스를 연결해 이용할 수 있으며 상품페이지에 "
            "고지되어 단순변심 교환 반품은 어렵습니다."
        ),
        stored_quality=0.74,
    )
    assert result.status == "QUESTION_ANSWER_MISMATCH"
    assert result.context_eligible is False


def test_unbounded_vacation_notice_blocks_whole_mixed_historical_answer() -> None:
    result = HistoricalLearningQualityService().assess(
        question="TV는 미도착이고 스탠드만 반품하려는데 어디로 보내나요?",
        answer=(
            "[8/3~8/4] 하계 휴가 기간에는 일부 고객센터만 운영합니다. "
            "스탠드 개봉 여부를 확인해 주세요."
        ),
        stored_quality=0.70,
    )
    assert result.status == "TEMPORARY_OR_EXPIRED"
    assert result.temporary is True


def test_past_order_facts_cannot_support_a_new_current_order() -> None:
    result = HistoricalLearningQualityService().assess(
        question="배송 완료라고 표시되는데 아직 못 받았습니다. 언제 오나요?",
        answer="TV 발송 과정에서 스탠드가 누락된 것으로 보여 익일 출고하겠습니다.",
        stored_quality=0.70,
    )
    assert result.status == "ORDER_SPECIFIC"
    assert result.order_specific is True


def test_aligned_stable_policy_and_product_guidance_remain_reusable() -> None:
    policy = HistoricalLearningQualityService().assess(
        question="배송 기사님이 설치까지 해주시나요?",
        answer="기사 설치 상품은 배송 시 기본 설치를 함께 진행합니다.",
        stored_quality=0.82,
    )
    product = HistoricalLearningQualityService().assess(
        question="이 TV 패널은 QLED인가요?",
        answer="해당 제품은 QLED 패널을 사용하는 상품입니다.",
        stored_quality=0.84,
    )
    assert policy.status == "SAFE_REUSABLE"
    assert product.status == "SAFE_REUSABLE"


def test_explicitly_conflicting_reusable_policies_are_detected() -> None:
    policy = HistoricalLearningQualityService()
    assert policy.contradicts(
        "벽걸이 설치비는 상품 가격에 포함됩니다.",
        "벽걸이 설치비는 별도 비용입니다.",
    )


def test_historical_list_uses_timestamp_sort_with_stable_id_tiebreaker(tmp_path) -> None:
    database = Database(tmp_path / "historical-sort.db")
    database.initialize()
    service = HistoricalCaseService(database)
    for external_id, created_at in (
        ("older", "2026-08-01T09:00:00+09:00"),
        ("newer-a", "2026-08-02T09:00:00+09:00"),
        ("newer-b", "2026-08-02T09:00:00+09:00"),
    ):
        row = service.prepare_case(
            {
                "store_code": "OJE_PLUS",
                "source_type": "PRODUCT_INQUIRY",
                "external_inquiry_id": external_id,
                "content": "설치 방법을 알려주세요.",
                "seller_answer": "기사 설치 상품은 배송 시 설치를 진행합니다.",
                "answered": True,
                "source_created_at": created_at,
            },
            source_reference="TEST:SORT",
        )
        service.repository.upsert(row)
    rows = service.repository.list_cases(limit=10)
    assert [row["external_inquiry_id"] for row in rows] == [
        "newer-b", "newer-a", "older"
    ]


def test_runtime_audit_preserves_rows_and_reports_eligibility(tmp_path) -> None:
    database = Database(tmp_path / "historical-audit.db")
    database.initialize()
    service = HistoricalCaseService(database)
    examples = (
        ("배송 기사님이 설치하나요?", "기사 설치 상품은 배송 시 설치합니다."),
        ("반품 택배 수거가 안 됩니다.", "OTT는 셋탑박스로 이용합니다."),
        ("배송이 언제 오나요?", "해당 주문은 익일 출고하겠습니다."),
    )
    for index, (question, answer) in enumerate(examples):
        row = service.prepare_case(
            {
                "store_code": "OJE_PLUS",
                "source_type": "CUSTOMER_INQUIRY",
                "external_inquiry_id": f"audit-{index}",
                "content": question,
                "seller_answer": answer,
                "answered": True,
                "source_created_at": datetime.now(UTC).isoformat(),
            },
            source_reference="TEST:AUDIT",
        )
        service.repository.upsert(row)
    before = service.repository.summary()["total"]
    audit = service.audit_corpus()
    after = service.repository.summary()["total"]
    assert before == after == 3
    assert audit["counts"]["SAFE_REUSABLE"] == 1
    assert audit["counts"]["QUESTION_ANSWER_MISMATCH"] == 1
    assert audit["counts"]["ORDER_SPECIFIC"] == 1


def test_attached_historical_and_actual_answer_use_are_distinct() -> None:
    class Provider:
        def generate_json(self, **_: object) -> dict[str, object]:
            return {
                "answer": "기사 설치 상품은 배송 시 기본 설치를 진행합니다.",
                "confidence": 0.9,
                "used_facts": [],
                "missing_information": [],
                "requires_review": False,
                "warnings": [],
                "historical_usage": [{
                    "historical_case_id": 17,
                    "matched_subquestion": "기사님이 설치하나요?",
                    "answer_supported": True,
                    "reason": "SAFE_REUSABLE_POLICY",
                }],
            }

    context = {
        "historical_cases": [{
            "historical_case_id": 17,
            "matched_subquestion": "기사님이 설치하나요?",
            "answer_reference": "기사 설치 상품은 배송 시 기본 설치를 진행합니다.",
            "eligibility": {"context_eligible": True},
        }],
        "subquestion_evidence": [{
            "subquestion": "기사님이 설치하나요?",
            "status": "ANSWERABLE",
            "learning_ids": [],
            "historical_case_ids": [17],
        }],
    }
    result = DraftGenerationService(
        Provider(), learning_context_provider=lambda *_: context
    ).generate(
        AnswerFacts(inquiry={"question": "기사님이 설치하나요?"}),
        IntentResult(
            "INSTALL_METHOD", ("기사님이 설치하나요?",),
            Emotion.NORMAL, "NORMAL", 0.9, False, "",
        ),
    )
    assert result.historical_usage == ({
        "historical_case_id": 17,
        "matched_subquestion": "기사님이 설치하나요?",
        "answer_supported": True,
        "reason": "SAFE_REUSABLE_POLICY",
    },)
