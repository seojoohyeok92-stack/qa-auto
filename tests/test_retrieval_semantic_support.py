"""맞는 Learning 을 실제로 근거로 쓰기 위한 불변 조건.

실데이터 40건 평가에서 적합 Learning 이 존재하는 31건 중 정상 선택은 10건
뿐이었다. 16건의 MISS 를 단계별로 추적한 결과 가장 큰 덩어리는 검색 실패가
아니었다 -- 7건은 모든 gate 를 통과하고도 근거로 승격되지 못했다.

원인은 answer-support 의 측정 방식이다. 질문의 단어가 답변에 얼마나 다시
나오는지를 재는데, 좋은 답변일수록 질문과 다른 말로 사실을 진술한다. 실측:
독립 evaluator 가 정답이라고 판정한 Learning 15건 중 support 0.5 를 넘긴 것은
**0건**이었다. semantic atomic question 이 있으면 evidence 승격에 SUPPORTED 를
요구하므로, 과거 Learning 은 아무리 잘 맞아도 근거가 될 수 없었다.

    inq 2127 "후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어서요!"
      L16658 질문이 글자 그대로 같음  rel=0.78  support=0.00 → 미사용

버려지고 있던 신호는 질문-질문 유사도다. 저장된 Learning 이 같은 질문을 하고
있으면 그 답변은 이 질문의 답이다. 기준은 높게 잡았다(0.60 vs 검색 임계값
0.24). 비-일정 문의에서 정답 8건 통과 / 오답 0건이었고 0.55~0.70 구간에서
결과가 같아 평평한 구간의 중간값을 썼다.

일정 문의는 이 floor 에서 제외한다. 기준을 넘긴 오답은 전부 배송 일정 문의였고,
거기서는 거의 같은 과거 질문이 *다른 고객의* 날짜를 담고 있다. 그 문의들은
정책 보류 또는 DPS 로 가므로 이 floor 를 만나지 않는다.

두 번째 결함은 historical 경로다. inquiry 116 "재입고 가능한가요?" 가 천정형
VESA 설치 사례를 근거로 승격해 무관한 답변을 냈고, 아무것도 막지 않았다.
"""
from __future__ import annotations

import itertools
import json

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.similar_answer_service import (
    SEMANTIC_QUESTION_MATCH_RELEVANCE,
    SimilarAnswerService,
)


_key = itertools.count()
PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "retrieval-support.db")
    value.initialize()
    return value


def add_learning(
    database: Database, *, question: str, answer: str,
    human_verified: bool = False, action: str | None = None,
) -> int:
    metadata: dict = {"learning_signal_type": "POSITIVE"}
    if human_verified:
        metadata["human_verified"] = True
        metadata["answer_provenance"] = "NAVER_POSTED"
    if action:
        metadata["semantic"] = {"primary_action": action}
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                final_answer, seller_answer, posted, rating, edit_ratio,
                quality_score, style_only, version, metadata_json, active,
                usage_count, created_at, updated_at, validity_type,
                validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"rs-{next(_key)}", "SELLER_ANSWER", question, question,
                "OJE_PLUS", "PRODUCT_INQUIRY", PRODUCT, answer, answer,
                1, 5, 0.0, 1.0, 0, 1,
                json.dumps(metadata, ensure_ascii=False), 1, 0,
                "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z", "PERMANENT", 1,
            ),
        )
        return int(cursor.lastrowid)


def search(database: Database, question: str, **goal):
    return SimilarAnswerService(LearningRepository(database)).search(
        question, store_code="OJE_PLUS", product_name=PRODUCT,
        inquiry_type="PRODUCT_INQUIRY", limit=3, semantic_goal=goal or None,
    )


# ==========================================================================
# ROOT_CAUSE_C -- 어휘 겹침이 의미 일치를 뒤집던 문제
# ==========================================================================


def test_a_same_question_learning_becomes_evidence(database) -> None:
    """실측 사례 그대로: 질문이 사실상 같은데 support 0 이던 건."""

    add_learning(
        database,
        question="상품 문의 후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어서요!",
        answer="네이버페이 포인트는 리뷰 확인 후 익월 중 순차 지급됩니다.",
    )

    selected = search(
        database, "후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어요",
        customer_goal="BENEFIT",
        requested_information="리뷰 보상 지급 여부",
        atomic_question="리뷰 보상이 지급되었는지",
        schedule_scoped=False,
    )

    assert selected, "질문이 같은 Learning 이 검색되어야 한다"
    assert selected[0]["answer_support"] >= 0.5
    assert selected[0]["answer_support_reason"] == "SEMANTIC_QUESTION_MATCH"


def test_the_floor_needs_no_human_verification(database) -> None:
    """과거 Learning 대부분은 human_verified 표시가 없다.

    기존 두 floor 는 둘 다 human_verified 를 요구해서, 정확히 그 대다수에
    적용되지 않았다.
    """

    learning_id = add_learning(
        database,
        question="상품 문의 후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어서요!",
        answer="네이버페이 포인트는 리뷰 확인 후 익월 중 순차 지급됩니다.",
        human_verified=False,
    )

    selected = search(
        database, "후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어요",
        customer_goal="BENEFIT", schedule_scoped=False,
    )

    assert [int(item["id"]) for item in selected] == [learning_id]
    assert selected[0]["answer_support"] >= 0.5


def test_a_merely_related_question_does_not_reach_the_floor(database) -> None:
    """같은 주제라도 질문이 다르면 근거가 되지 않는다."""

    add_learning(
        database,
        question="상품권은 어디서 신청하나요?",
        answer="상품권 신청은 상품 상세페이지의 신청 링크에서 진행해 주세요.",
    )

    selected = search(
        database, "얼마 전에 신청한 상품권 처리됐는지 확인 부탁드려요",
        customer_goal="OTHER",
        requested_information="이미 신청한 건의 처리 상태",
        schedule_scoped=False,
    )

    for item in selected:
        assert item["answer_support_reason"] != "SEMANTIC_QUESTION_MATCH"


def test_a_hedged_answer_never_reaches_the_floor(database) -> None:
    """확답하지 않는 답변은 아무것도 뒷받침하지 못한다."""

    add_learning(
        database,
        question="상품 문의 후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어서요!",
        answer="지급 여부는 담당자 확인이 필요할 것으로 보입니다.",
    )

    selected = search(
        database, "후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어요",
        customer_goal="BENEFIT", schedule_scoped=False,
    )

    for item in selected:
        assert item["answer_support_reason"] != "SEMANTIC_QUESTION_MATCH"


def test_a_schedule_question_is_excluded_from_the_floor(database) -> None:
    """거의 같은 과거 배송 질문의 답변에는 *다른 고객의* 날짜가 들어 있다."""

    add_learning(
        database,
        question="언제배송되나요? 목요일에 배송일 지정했는데 아직 발송전이네요.",
        answer="확인되는 설치예정일은 8월 27일입니다.",
    )

    selected = search(
        database, "언제배송되나요 대략적인일자라도알려주세요.",
        customer_goal="DELIVERY_STATUS",
        atomic_question="배송이 언제 되는지",
        schedule_scoped=True,
    )

    for item in selected:
        assert item["answer_support_reason"] != "SEMANTIC_QUESTION_MATCH", (
            "다른 주문의 배송일이 이 고객의 근거가 되면 안 된다"
        )


def test_no_understanding_leaves_the_floor_off(database) -> None:
    """semantic 이 없으면 이전과 동일하게 동작한다."""

    add_learning(
        database,
        question="상품 문의 후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어서요!",
        answer="네이버페이 포인트는 리뷰 확인 후 익월 중 순차 지급됩니다.",
    )

    selected = search(database, "후기는 남겼고, 받은건지 아직 지급전인지 알고 싶어요")

    for item in selected:
        assert item["answer_support_reason"] != "SEMANTIC_QUESTION_MATCH"


def test_the_bar_is_far_above_the_retrieval_threshold() -> None:
    assert SEMANTIC_QUESTION_MATCH_RELEVANCE >= 0.5


# ==========================================================================
# ROOT_CAUSE_D -- historical 경로에 의미 검증이 없던 문제
# ==========================================================================


def _historical_case(database: Database, *, question: str, answer: str) -> int:
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO historical_cases (
                source, store_code, external_inquiry_id, inquiry_type, question,
                question_normalized, seller_answer, product_name,
                source_answered, quality_score, confidence, active, case_key,
                fingerprint, created_at, updated_at
            ) VALUES ('NAVER','OJE_PLUS',?,'PRODUCT_INQUIRY',?,?,?,?,1,1.0,1.0,1,
                      ?,?, '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')
            """,
            (f"ext-{next(_key)}", question, question, answer, PRODUCT,
             f"hc-{next(_key)}", f"fp-{next(_key)}"),
        )
        return int(cursor.lastrowid)


def _context(database, question, understanding):
    from answer.facts import build_answer_facts
    from answer.hybrid_models import Emotion, IntentResult
    from answer.models import AnswerRequest, AnswerResult, AnswerStatus
    from repositories.inquiry_repository import InquiryRepository
    from services.learning_context_service import LearningContextService

    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "NAVER",
        "source_question_id": f"rs-i-{next(_key)}",
        "inquiry_type": "PRODUCT_INQUIRY", "title": "문의",
        "content": question, "product_name": PRODUCT, "product_id": "p1",
        "raw_json": {},
    }).inquiry_id
    facts = build_answer_facts(
        AnswerRequest(inquiry_type="PRODUCT_INQUIRY", question=question,
                      product_name=PRODUCT),
        AnswerResult(status=AnswerStatus.NOT_SUPPORTED, category="기타",
                     reason="템플릿 없음", answer="", provider="test",
                     auto_answerable=False, needs_review=True),
    )
    facts.inquiry["inquiry_id"] = inquiry_id
    return LearningContextService(database).build(
        facts,
        IntentResult("GENERAL", (question,), Emotion.NORMAL, "NORMAL",
                     0.9, False, "t"),
        semantic_analysis=understanding,
    )


def _semantic(action: str, question: str):
    from services.semantic_analysis import parse

    return parse({
        "primary_action": action, "secondary_actions": [],
        "request_type": "QUESTION", "objects": [],
        "atomic_questions": [{"text": question, "action": action}],
        "deadline": None, "constraints": [], "negation": False,
        "conditional": False, "requires_order_context": False,
        "requires_delivery_schedule": False, "purchase_state": "UNKNOWN",
        "asks_delivery_schedule": False, "confidence": 0.95,
    })


def test_an_off_topic_historical_case_is_not_promoted(database) -> None:
    """inquiry 116 그대로: 재입고 문의에 천정형 설치 사례가 승격되던 경로."""

    _historical_case(
        database,
        question="천정에 설치할 수 있나요? 벽걸이 브라켓으로 가능한가요?",
        answer=(
            "VESA홀이 있어 설치 자체는 가능하나 삼성전자로지텍 기사님은 "
            "천정형 설치를 하지 않으므로 사설업체를 통해 별도 설치해 주셔야 합니다."
        ),
    )
    question = "재입고 가능한가요?"

    context = _context(database, question, _semantic("OTHER", question))

    sources = {
        item.get("source") for item in context["subquestion_evidence"]
    }
    assert "SAFE_HISTORICAL_LEARNING" not in sources, (
        "주제가 다른 historical 사례가 근거로 승격되면 안 된다"
    )
    assert context["learning_retrieval"]["rejection_counts"].get(
        "HISTORICAL_TOPIC_SCOPE_MISMATCH", 0
    ) >= 1


def test_an_on_topic_historical_case_is_still_usable(database) -> None:
    """의미 검증을 붙였다고 맞는 historical 까지 버리지 않는다."""

    _historical_case(
        database,
        question="벽걸이 브라켓은 따로 구매해야 하나요?",
        answer="상하좌우 벽걸이 옵션에는 브라켓과 기본 설치 공임이 포함되어 있습니다.",
    )
    question = "벽걸이 브라켓 별도로 사야 하나요?"

    context = _context(
        database, question, _semantic("PACKAGE_CONTENTS", question),
    )

    assert context["historical_cases"], (
        "같은 주제의 historical 사례는 계속 검색되어야 한다"
    )


# ==========================================================================
# ROOT_CAUSE_E -- 재사용 가능함과 이 질문에 답함을 한 판정으로 쓴 문제
# ==========================================================================


def _evidence(context, question: str) -> dict:
    for item in context["subquestion_evidence"]:
        if item.get("subquestion") == question:
            return item
    raise AssertionError(f"no evidence row for {question!r}")


def test_reusable_does_not_mean_it_answers_this_question(database) -> None:
    """SAFE_REUSABLE 은 "다시 써도 되는가"에 대한 답이지 "이 질문에 답하는가"가 아니다.

    COHORT_1 에서 두 건이 같은 자리에서 새어 나왔다. "무타공설치비용
    문의합니다" 는 다른 상품의 택배배송 안내로 ANSWERABLE 이 되었고, "쿠폰
    1만원 보냈는지 확인해주세요" 는 온누리 상품권 신청 안내를 그대로 답변으로
    받았다. 두 과거 답변 모두 재사용 가능한 일반 안내가 맞다 -- 물어본 것에
    답하지 않을 뿐이고, 승격과 함께 기록된 coverage 가 이미 UNSUPPORTED 라고
    말하고 있었다. Positive Learning 이 통과해야 하는 바로 그 시험을 historical
    에도 적용한다.
    """

    _historical_case(
        database,
        question="천정에 설치 가능한가요?",
        answer=(
            "VESA홀이 있어 설치 자체는 가능하나 삼성전자로지텍 기사님은 천정형 "
            "설치를 하지 않으므로 사설업체를 통해 별도 설치해 주셔야 합니다."
        ),
    )
    question = "무타공설치비용 문의합니다"

    context = _context(
        database, question, _semantic("INSTALLATION_METHOD", question),
    )
    evidence = _evidence(context, question)

    assert evidence["status"] == "NO_RELIABLE_SOURCE", evidence
    assert evidence["source"] == "SAFE_HISTORICAL_LEARNING_INSUFFICIENT_EVIDENCE"
    assert evidence["historical_case_ids"] == []


def test_a_covering_historical_case_is_still_evidence(database) -> None:
    """대조군 -- 답이 실제로 질문을 덮으면 예전처럼 근거가 된다."""

    _historical_case(
        database,
        question="벽걸이 브라켓은 따로 구매해야 하나요?",
        answer=(
            "벽걸이 브라켓은 따로 구매하지 않으셔도 되며, 상하좌우 벽걸이 "
            "옵션에 브라켓과 기본 설치 공임이 포함되어 있습니다."
        ),
    )
    question = "벽걸이 브라켓은 따로 구매해야 하나요?"

    context = _context(
        database, question, _semantic("PACKAGE_CONTENTS", question),
    )
    evidence = _evidence(context, question)

    assert evidence["status"] == "ANSWERABLE", evidence
    assert evidence["source"] == "SAFE_HISTORICAL_LEARNING"
    assert evidence["evidence_coverage"] == "SUPPORTED"
    assert evidence["historical_case_ids"]


def test_without_understanding_the_historical_path_is_unchanged(database) -> None:
    """semantic 이 없으면 이전 동작 그대로 -- 이번 판정은 의미 기반일 때만 붙는다."""

    _historical_case(
        database,
        question="천정에 설치 가능한가요?",
        answer=(
            "VESA홀이 있어 설치 자체는 가능하나 삼성전자로지텍 기사님은 천정형 "
            "설치를 하지 않으므로 사설업체를 통해 별도 설치해 주셔야 합니다."
        ),
    )
    question = "무타공설치비용 문의합니다"

    context = _context(database, question, None)
    evidence = _evidence(context, question)

    assert evidence["source"] != "SAFE_HISTORICAL_LEARNING_INSUFFICIENT_EVIDENCE"
