from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from answer.inquiry_analysis import AnswerStrategy, InquiryType, OrderIdStatus
from answer.models import AnswerRequest
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from services.draft_generation_service import DraftGenerationService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.learning_context_service import LearningContextService


ORDER_ID = "2026070563746121"


def analyze(question: str, *, order_id: str = ORDER_ID):
    return InquiryAnalysisService().analyze(
        AnswerRequest(
            inquiry_id=1,
            question_id="REGRESSION",
            store_code="OJE_PLUS",
            inquiry_type="CUSTOMER_INQUIRY",
            question=question,
            product_name="삼성 TV",
            order_id=order_id,
        )
    )


def test_order_number_is_preserved_for_installation_method_without_dps() -> None:
    result = analyze("배송기사님이 설치해주시나요? 자가설치인가요?")

    assert result.inquiry_type is InquiryType.INSTALLATION_GENERAL
    assert result.inquiry_subtype == "INSTALLATION_METHOD"
    assert result.order_id_status is OrderIdStatus.VALIDATED
    assert result.order_id_present is True
    assert result.requires_order_lookup is False
    assert result.requires_dps_lookup is False
    assert result.answer_strategy is AnswerStrategy.GENERAL_GUIDANCE


def test_promised_date_deadline_with_order_is_dps_eligible() -> None:
    result = analyze(
        "예정일이 8/25일이라던 것 같은데 말일까지 가능할까요? "
        "잊어먹고 있으면 오겠지 했는데 기다리다 지쳐가네요."
    )

    assert result.inquiry_type is InquiryType.DELIVERY_INSTALLATION_STATUS
    assert result.detected_intent == "DELIVERY_DATE"
    assert result.order_id_status is OrderIdStatus.VALIDATED
    assert result.requires_order_lookup is True
    assert result.requires_dps_lookup is True
    assert result.can_execute_dps_lookup is True
    assert result.answer_strategy is AnswerStrategy.DIRECT_FACT_ANSWER


def test_installation_date_without_order_keeps_safe_order_request() -> None:
    result = analyze("언제 설치되나요?", order_id="")

    assert result.inquiry_type is InquiryType.ORDER_INFO_REQUIRED
    assert result.requires_dps_lookup is True
    assert result.can_execute_dps_lookup is False
    assert result.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID


def test_order_number_does_not_force_dps_for_general_product_question() -> None:
    result = analyze("이 제품의 HDMI 단자는 몇 개인가요?")

    assert result.inquiry_type is InquiryType.PRODUCT_GENERAL
    assert result.order_id_status is OrderIdStatus.VALIDATED
    assert result.requires_order_lookup is False
    assert result.requires_dps_lookup is False


def make_database(tmp_path) -> tuple[Database, int, int]:
    database = Database(tmp_path / "pipeline-learning.db")
    database.initialize()
    inquiries = InquiryRepository(database)
    source_id = inquiries.upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "LEARNING-SOURCE",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "설치 문의",
            "content": "배송기사님이 설치해주시나요?",
            "product_id": "TV-FAMILY",
            "product_name": "삼성 TV",
            "source_answered": True,
            "raw_json": {},
        }
    ).inquiry_id
    target_id = inquiries.upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "LEARNING-TARGET",
            "inquiry_type": "CUSTOMER_INQUIRY",
            "title": "복합 문의",
            "content": "기사님 설치와 A/S 정책이 궁금합니다.",
            "product_id": "TV-FAMILY",
            "product_name": "삼성 TV",
            "order_id": ORDER_ID,
            "raw_json": {},
        }
    ).inquiry_id
    return database, source_id, target_id


def add_learning(
    repository: LearningRepository,
    *,
    source_id: int,
    source_key: str,
    question: str,
    answer: str,
    validity_type: str = "PERMANENT",
    valid_from=None,
    valid_until=None,
) -> int:
    row = repository.upsert(
            {
                "source_key": source_key,
                "learning_source": "APPROVED_EDITED",
                "inquiry_id": source_id,
                "answer_draft_id": None,
                "approval_history_id": None,
                "question_original_masked": question,
                "question_normalized": question.lower(),
                "store_code": "OJE_PLUS",
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
                "metadata_json": {
                    "human_verified": True,
                    "learning_signal_type": "POSITIVE",
                },
                "active": True,
                "validity_type": validity_type,
                "validity_active": True,
                "valid_from": valid_from,
                "valid_until": valid_until,
            }
        )
    learning_id = int(row["id"])
    if validity_type == "TEMPORARY":
        repository.update_validity(
            learning_id,
            validity_type="TEMPORARY",
            event_name="회귀 테스트 행사",
            valid_from=valid_from,
            valid_until=valid_until,
            validity_active=True,
            validity_note="기간성 후보 필터 검증",
        )
    return learning_id


def facts(target_id: int, question: str) -> AnswerFacts:
    return AnswerFacts(
        inquiry={
            "inquiry_id": target_id,
            "question": question,
            "type": "CUSTOMER_INQUIRY",
        },
        product={"product_id": "TV-FAMILY", "name": "삼성 TV"},
        order={"order_id": ORDER_ID},
    )


def test_known_active_learning_is_selected_attached_and_sent_to_provider(tmp_path) -> None:
    database, source_id, target_id = make_database(tmp_path)
    repository = LearningRepository(database)
    learning_id = add_learning(
        repository,
        source_id=source_id,
        source_key="install-method",
        question="배송기사님이 설치해주시나요?",
        answer="검증된 설치 방식 안내입니다.",
    )
    customer_question = "기사님이 오시면 설치까지 해주시나요? 자가설치 제품인가요?"
    intent = IntentResult(
        "INSTALLATION_METHOD",
        (customer_question,),
        Emotion.NORMAL,
        "NORMAL",
        0.95,
        False,
        "installation method",
    )
    context = LearningContextService(database).build(
        facts(target_id, customer_question), intent
    )

    trace = context["learning_retrieval"]
    assert trace["candidate_count"] == 1
    assert trace["active_candidates"] == 1
    assert trace["selected_count"] == 1
    assert trace["selected_learning_ids"] == [learning_id]
    assert context["similar_approved_answers"][0]["answer"] == "검증된 설치 방식 안내입니다."

    captured = {}

    class Provider:
        def generate_json(self, *, task, prompt, context):
            captured["context"] = context
            return {
                "answer": "검증된 설치 방식 안내입니다.",
                "confidence": 0.95,
                "used_facts": [],
                "missing_information": [],
                "requires_review": False,
                "warnings": [],
            }

    result = DraftGenerationService(
        Provider(), learning_context_provider=lambda *_: context
    ).generate(facts(target_id, customer_question), intent)
    assert captured["context"]["similar_approved_answers"][0]["learning_example_id"] == learning_id
    assert result.answer == "검증된 설치 방식 안내입니다."


def test_compound_questions_retrieve_learning_per_subquestion(tmp_path) -> None:
    database, source_id, target_id = make_database(tmp_path)
    repository = LearningRepository(database)
    install_id = add_learning(
        repository,
        source_id=source_id,
        source_key="compound-install",
        question="기사님이 설치해주시나요?",
        answer="설치 정책 답변",
    )
    as_id = add_learning(
        repository,
        source_id=source_id,
        source_key="compound-as",
        question="7일 이후 A/S는 어디에서 받나요?",
        answer="A/S 정책 답변",
    )
    question = "기사님 설치 여부와 7일 이후 A/S 접수처가 궁금합니다."
    intent = IntentResult(
        "COMPOUND",
        ("기사님이 설치까지 해주시나요?", "7일 이후에는 삼성에 A/S 해야 하나요?"),
        Emotion.NORMAL,
        "NORMAL",
        0.95,
        False,
        "two questions",
    )

    context = LearningContextService(database).build(facts(target_id, question), intent)
    trace = context["learning_retrieval"]

    assert trace["subquestion_count"] == 2
    assert len(trace["subquestions"]) == 2
    assert {install_id, as_id} <= set(trace["selected_learning_ids"])
    assert trace["selected_count"] >= 2
    assert all(item.get("matched_subquestion") for item in context["similar_approved_answers"])


def test_expired_temporary_learning_is_excluded_from_candidates(tmp_path) -> None:
    database, source_id, target_id = make_database(tmp_path)
    repository = LearningRepository(database)
    now = datetime.now(UTC)
    add_learning(
        repository,
        source_id=source_id,
        source_key="expired-install",
        question="기사님이 설치해주시나요?",
        answer="종료된 행사 답변",
        validity_type="TEMPORARY",
        valid_from=now - timedelta(days=2),
        valid_until=now - timedelta(days=1),
    )
    question = "기사님이 설치까지 해주시나요?"
    intent = IntentResult(
        "INSTALLATION_METHOD", (question,), Emotion.NORMAL, "NORMAL", 0.9, False, ""
    )

    context = LearningContextService(database).build(facts(target_id, question), intent)
    trace = context["learning_retrieval"]

    assert trace["candidate_count"] == 0
    assert trace["active_candidates"] == 0
    assert trace["rejection_counts"]["FILTERED_BY_VALIDITY"] == 1
    assert trace["selected_count"] == 0
    assert context["similar_approved_answers"] == []


@pytest.mark.parametrize(
    ("name", "learned_question", "new_question", "category"),
    (
        (
            "wall-install",
            "벽걸이 설치가 포함된 가격인가요?",
            "벽걸이로 주문하면 기사님 설치비도 같이 포함되어 있나요?",
            "INSTALLATION_POLICY",
        ),
        (
            "delivery-general",
            "구매 후 배송까지 며칠 정도 걸리나요?",
            "지금 주문하면 받아보기까지 얼마나 걸릴까요?",
            "DELIVERY_POLICY",
        ),
        (
            "after-service",
            "7일 이후 A/S는 어디에 신청하나요?",
            "일주일이 지나면 에이에스는 삼성에 접수해야 하나요?",
            "AFTER_SERVICE_POLICY",
        ),
        (
            "panel",
            "이 TV 패널 종류는 무엇인가요?",
            "LED인지 QLED인지 패널 방식이 궁금합니다.",
            "PRODUCT_SPEC",
        ),
        (
            "point",
            "네이버 포인트 프로모션 적용되나요?",
            "지금 올려도 네이버포인트 혜택을 받을 수 있나요?",
            "PROMOTION_POLICY",
        ),
    ),
)
def test_representative_active_learning_categories_support_paraphrases(
    tmp_path, name, learned_question, new_question, category
) -> None:
    database, source_id, target_id = make_database(tmp_path)
    repository = LearningRepository(database)
    learning_id = add_learning(
        repository,
        source_id=source_id,
        source_key=name,
        question=learned_question,
        answer=f"{name} 검증 답변",
    )
    intent = IntentResult(
        category, (new_question,), Emotion.NORMAL, "NORMAL", 0.9, False, ""
    )

    context = LearningContextService(database).build(
        facts(target_id, new_question), intent
    )
    trace = context["learning_retrieval"]

    assert trace["candidate_count"] == 1
    assert trace["active_candidates"] == 1
    assert trace["selected_count"] == 1
    assert trace["selected_learning_ids"] == [learning_id]
    assert context["similar_approved_answers"]
    assert context["product_fact_guard"]["approved_learning_must_not_supply"] == [
        "CURRENT_ORDER_STATUS",
        "CURRENT_DELIVERY_STATUS",
        "CURRENT_INSTALLATION_DATE",
    ]
