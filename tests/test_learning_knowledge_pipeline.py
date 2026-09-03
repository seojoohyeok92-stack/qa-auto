"""승인된 Learning을 실제 운영 지식으로 쓰기 위한 불변 조건.

두 가지 결함을 함께 다룬다.

Trust. ``LEARNING_AUTHORITY`` 는 APPROVED_EDITED 8, APPROVED_UNEDITED 6 이었다.
직원이 초안을 고쳐야 했다는 사실을 "그 답변이 더 믿을 만하다"로 읽은 것인데,
두 행 모두 사람이 최종 답변을 읽고 승인한 같은 관문을 통과했다.  수정 여부는
초안이 어땠는지를 말할 뿐 승인된 답변이 맞는지를 말하지 않는다.  ``rating``
쪽은 더 나빴다 -- edit_ratio 가 클수록 rating 이 5에서 2까지 내려가서, 직원이
가장 많이 손본 답변이 가장 덜 신뢰받았다.

Negative memo.  서버 스냅샷의 Negative 47건 중 30건은 운영자가 "무엇이 틀렸고
어떻게 답해야 하는지"를 ``learning_feedback.correction_note`` 에 적어 두었다.
그 30건은 ``learning_signals`` 로 승격된 적이 없어 런타임에서 한 번도 읽히지
않았다.  원본은 그대로 두고 (수정/이관/재작성 없음) 검색 시점에 읽는다.
메모가 없는 Negative 는 여기 들어오지 않는다 -- 없는 이유를 지어내는 것이
이 경로에서 가장 위험한 실패다.
"""
from __future__ import annotations

import itertools
import json

import pytest

from answer.negative_correction import parse_operator_memo
from repositories.database import Database
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from services.learning_evidence_policy import (
    APPROVED_EDITED,
    APPROVED_UNEDITED,
    HUMAN_APPROVED_PROVENANCES,
    LEARNING_AUTHORITY,
    SELLER_ANSWER_VERIFIED,
    classify_provenance,
    human_approved_trust,
    order_identifier_request_reason,
)
from services.learning_quality_service import LearningQualityService
from services.learning_signal_service import LearningSignalService
from services.similar_answer_service import SimilarAnswerService


PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
PRODUCT_ID = "12139453925"
OTHER_PRODUCT = "삼성 107.9cm(43인치) 비즈니스TV LH43BEFHLGFXKR 스탠드형"
OTHER_PRODUCT_ID = "12021985151"

_key = itertools.count()


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "learning-knowledge.db")
    value.initialize()
    return value


def add_learning(
    database: Database,
    *,
    question: str,
    answer: str,
    learning_source: str = "APPROVED_UNEDITED",
    provenance: str = "PROGRAM_GENERATED",
    human_verified: bool = True,
    semantic_action: str | None = None,
    product_name: str = PRODUCT,
    validity_active: bool = True,
    active: bool = True,
    rating: int = 5,
) -> int:
    metadata: dict[str, object] = {"learning_signal_type": "POSITIVE"}
    if human_verified:
        metadata["human_verified"] = True
    metadata["answer_provenance"] = provenance
    if semantic_action:
        metadata["semantic"] = {"primary_action": semantic_action}
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
                f"lk-{next(_key)}", learning_source, question, question,
                "OJE_PLUS", "PRODUCT_INQUIRY", product_name, answer, answer,
                1, rating, 0.0, 1.0, 0, 1,
                json.dumps(metadata, ensure_ascii=False), int(active), 0,
                "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z",
                "PERMANENT", int(validity_active),
            ),
        )
        return int(cursor.lastrowid)


def add_negative(
    database: Database,
    *,
    question: str,
    memo: str | None,
    reason: str = "INTENT_NOT_REFLECTED",
    product_name: str = PRODUCT,
    product_id: str = PRODUCT_ID,
    signal_type: str = "NEGATIVE",
    active: bool = True,
) -> int:
    """A Negative exactly as the dashboard writes one, memo included."""

    with database.connection() as conn:
        inquiry = conn.execute(
            """
            INSERT INTO inquiries (
                store_code, source_type, source_question_id, inquiry_type,
                content, product_id, product_name, created_at, updated_at
            ) VALUES ('OJE_PLUS','NAVER',?,'PRODUCT_INQUIRY',?,?,?,
                      '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')
            """,
            (f"q-{next(_key)}", question, product_id, product_name),
        ).lastrowid
        cursor = conn.execute(
            """
            INSERT INTO learning_feedback (
                source_key, feedback_type, correction_reason, correction_note,
                learning_signal_type, source, inquiry_id, question_masked,
                metadata_json, active, created_at, updated_at
            ) VALUES (?, 'STAFF_CORRECTION', ?, ?, ?, 'DASHBOARD_NEGATIVE_REVIEW',
                      ?, ?, '{}', ?, '2026-08-01T00:00:00Z',
                      '2026-08-01T00:00:00Z')
            """,
            (
                f"fb-{next(_key)}", reason, memo, signal_type,
                int(inquiry), question, int(active),
            ),
        )
        return int(cursor.lastrowid)


def search(database: Database, question: str, **goal) -> list[dict]:
    service = SimilarAnswerService(LearningRepository(database))
    return service.search(
        question, store_code="OJE_PLUS", product_name=PRODUCT,
        product_id=PRODUCT_ID, inquiry_type="PRODUCT_INQUIRY", limit=3,
        semantic_goal=goal or None,
    )


def corrections(database: Database, question: str, **kwargs) -> dict:
    return LearningSignalService(database).negative_corrections(
        question, store_code="OJE_PLUS",
        product_name=kwargs.pop("product_name", PRODUCT),
        product_id=kwargs.pop("product_id", PRODUCT_ID),
        semantic_goal=kwargs.pop("semantic_goal", None),
        minimum_relevance=kwargs.pop("minimum_relevance", 0.0),
        **kwargs,
    )


# ==========================================================================
# A/B/C -- 사람이 승인했다는 사실만이 trust 이고, 수정 여부는 아니다
# ==========================================================================


def test_a_approved_without_editing_carries_human_approved_trust() -> None:
    item = {
        "learning_source": "APPROVED_UNEDITED",
        "metadata_json": {
            "human_verified": True, "answer_provenance": "PROGRAM_GENERATED",
        },
    }
    assert classify_provenance(item) == APPROVED_UNEDITED
    assert human_approved_trust(item) is True
    assert LEARNING_AUTHORITY[APPROVED_UNEDITED] == 8


def test_b_approved_after_editing_is_the_same_trust_tier() -> None:
    edited = {
        "learning_source": "APPROVED_EDITED",
        "metadata_json": {
            "human_verified": True, "answer_provenance": "STAFF_EDITED",
        },
    }
    assert classify_provenance(edited) == APPROVED_EDITED
    assert human_approved_trust(edited) is True
    assert (
        LEARNING_AUTHORITY[APPROVED_EDITED]
        == LEARNING_AUTHORITY[APPROVED_UNEDITED]
    ), "수정 여부는 trust 차등 기준이 아니다"
    assert HUMAN_APPROVED_PROVENANCES == {APPROVED_EDITED, APPROVED_UNEDITED}


def test_the_edit_ratio_no_longer_lowers_an_approved_answers_rating() -> None:
    quality = LearningQualityService()
    heavily_edited = quality.score("APPROVED_EDITED", "초안" * 40, "최종" * 40)
    untouched = quality.score("APPROVED_UNEDITED", "동일", "동일")

    assert heavily_edited.rating == untouched.rating == 5
    # provenance 로서의 edit_ratio 자체는 계속 측정되고 저장된다.
    assert heavily_edited.edit_ratio > 0
    assert untouched.edit_ratio == 0.0


def test_bulk_verified_seller_answers_stay_below_the_approved_tier() -> None:
    assert (
        LEARNING_AUTHORITY[SELLER_ANSWER_VERIFIED]
        < LEARNING_AUTHORITY[APPROVED_UNEDITED]
    )


def test_c_between_two_approved_candidates_relevance_decides(database) -> None:
    """수정 여부가 아니라 semantic relevance 가 높은 쪽이 선택된다."""

    edited_but_off_topic = add_learning(
        database,
        question="벽걸이 브라켓 설치 비용은 얼마인가요?",
        answer="상하좌우 벽걸이 옵션에는 기본 설치 공임이 포함되어 있습니다.",
        learning_source="APPROVED_EDITED", provenance="STAFF_EDITED",
    )
    unedited_on_topic = add_learning(
        database,
        question="국내 삼성 서비스센터에서 A/S 받을 수 있나요?",
        answer="구매하신 제품은 삼성전자서비스센터에서 A/S 접수가 가능합니다.",
        learning_source="APPROVED_UNEDITED", provenance="PROGRAM_GENERATED",
    )

    selected = search(
        database, "삼성전지센터에서 AS 받을 수 있나요?",
        customer_goal="REPAIR",
        requested_information="국내 삼성 서비스센터에서 A/S 가능 여부",
        atomic_question="국내 삼성 서비스센터에서 A/S 가능한지",
    )

    ids = [int(item["id"]) for item in selected]
    assert ids and ids[0] == unedited_on_topic, (
        "직원이 수정한 쪽이 아니라 질문에 직접 맞는 쪽이 선택되어야 한다"
    )
    if edited_but_off_topic in ids:
        assert ids.index(unedited_on_topic) < ids.index(edited_but_off_topic)


# ==========================================================================
# D/E -- Positive 는 claim 을 실제로 뒷받침할 때만 근거가 된다
# ==========================================================================


def test_d_approved_positive_that_supports_the_claim_is_usable_evidence(
    database,
) -> None:
    add_learning(
        database,
        question="포토리뷰 네이버페이 포인트는 언제 지급되나요?",
        answer=(
            "포토리뷰 네이버페이 포인트는 리뷰 등록 확인 후 익월 중 "
            "지급됩니다."
        ),
        semantic_action="BENEFIT",
    )

    selected = search(
        database, "포토리뷰 네이버페이 2만원은 언제 받는건가요?",
        customer_goal="BENEFIT",
        requested_information="리뷰 보상 지급 시점",
        atomic_question="포토리뷰 네이버페이 보상을 언제 받는지",
    )

    assert selected, "지급 시점을 직접 support 하는 Positive 가 있어야 한다"
    assert selected[0]["answer_support"] >= 0.5


def test_e_same_words_different_requested_information_is_not_evidence(
    database,
) -> None:
    """단어만 겹치는 Learning 은 근거가 되지 않는다."""

    add_learning(
        database,
        question="상품권은 어디서 신청하나요?",
        answer="상품권 신청은 행사 페이지에서 진행해 주세요.",
        semantic_action="FORM_FIELD_GUIDANCE",
    )

    selected = search(
        database, "상품권신청 얼마전에했는데 확인부탁드려요",
        customer_goal="OTHER",
        requested_information="이미 신청한 상품권 건의 확인/처리 상태",
        atomic_question="얼마 전 신청한 상품권 건이 처리되었는지 확인",
    )

    assert not selected, (
        "신청 방법 Learning 이 신청 후 확인 문의의 근거가 되면 안 된다"
    )


def test_delivery_status_rejects_legacy_ott_learning_by_topic(database) -> None:
    """A legacy row without semantic metadata must still obey topic scope.

    This is the generalized retrieval invariant behind the former inquiry 364
    failure: a customer reporting that an already-ordered TV was not delivered
    must never receive an OTT/Netflix answer merely because both rows mention
    a TV.  The test intentionally leaves ``semantic_action`` absent, matching
    the legacy corpus rather than relying on a newly populated metadata field.
    """

    ott = add_learning(
        database,
        question="넷플릭스 등 OTT 앱을 TV에서 사용할 수 있나요?",
        answer="이 제품은 TV 자체에서 넷플릭스 등 OTT 앱을 지원하지 않습니다.",
        learning_source="SELLER_ANSWER",
        provenance="NAVER_POSTED",
        human_verified=False,
    )
    delivery = add_learning(
        database,
        question="주문한 TV 배송이 아직 오지 않았습니다.",
        answer="주문번호를 확인한 뒤 현재 배송 상태를 안내드리겠습니다.",
        learning_source="SELLER_ANSWER",
        provenance="NAVER_POSTED",
        human_verified=False,
    )

    selected = search(
        database, "이사 때문에 TV를 주문했는데 배송이 안 됩니다. 급합니다.",
        customer_goal="DELIVERY_STATUS",
        requested_information="현재 주문의 배송 상태",
        atomic_question="주문한 TV의 현재 배송 상태",
        order_evidence_required=True,
        schedule_scoped=True,
    )

    selected_ids = [int(item["id"]) for item in selected]
    assert delivery in selected_ids
    assert ott not in selected_ids


def test_k_expired_positive_is_not_used_even_when_approved(database) -> None:
    add_learning(
        database,
        question="포토리뷰 네이버페이 포인트는 언제 지급되나요?",
        answer="포토리뷰 포인트는 리뷰 확인 후 익월 중 지급됩니다.",
        validity_active=False,
    )

    assert not search(
        database, "포토리뷰 네이버페이 2만원은 언제 받는건가요?",
        customer_goal="BENEFIT",
        requested_information="리뷰 보상 지급 시점",
        atomic_question="포토리뷰 보상 지급 시점",
    )


# ==========================================================================
# F/G -- 메모가 있는 Negative 만 교정 지식이 된다
# ==========================================================================


def test_f_negative_with_a_memo_is_retrieved_as_correction_knowledge(
    database,
) -> None:
    feedback_id = add_negative(
        database,
        question="폐가전 수거도 해주시나요?",
        memo=(
            "폐가전 무료수거 안내는 맞음. 고객센터 별도 신청 안내는 잘못됨. "
            "설치기사 방문 시 수거 요청하도록 안내."
        ),
    )

    result = corrections(
        database, "폐가전 무료수거 가능한가요?",
        semantic_goal={
            "customer_goal": "COLLECTION",
            "requested_information": "폐가전 무료수거 가능 여부",
            "atomic_question": "폐가전 무료수거가 가능한지",
        },
    )

    selected = result["selected"]
    assert [item["feedback_id"] for item in selected] == [feedback_id]
    assert any("고객센터" in text for text in selected[0]["bad_patterns"])
    assert any("설치기사" in text for text in selected[0]["corrections"])
    assert any("맞음" in text for text in selected[0]["good_patterns"])
    assert selected[0]["structured"] is True


def test_f_the_original_memo_row_is_never_modified(database) -> None:
    memo = (
        "폐가전 무료수거 안내는 맞음. 고객센터 별도 신청 안내는 잘못됨. "
        "설치기사 방문 시 수거 요청하도록 안내."
    )
    feedback_id = add_negative(
        database, question="폐가전 수거도 해주시나요?", memo=memo,
    )
    def row() -> tuple:
        with database.connection() as conn:
            return tuple(conn.execute(
                "SELECT * FROM learning_feedback WHERE id=?", (feedback_id,)
            ).fetchone())

    before = row()

    corrections(
        database, "폐가전 무료수거 가능한가요?",
        semantic_goal={"customer_goal": "COLLECTION"},
    )

    after = row()
    assert after == before
    with database.connection() as conn:
        stored = conn.execute(
            "SELECT correction_note FROM learning_feedback WHERE id=?",
            (feedback_id,),
        ).fetchone()
    after = {"correction_note": stored[0]}
    assert after["correction_note"] == memo, "원본 메모는 source-of-truth 다"


def test_g_negative_without_a_memo_produces_no_correction(database) -> None:
    """메모가 없으면 교정 지식도 없다 -- 이유를 지어내지 않는다."""

    add_negative(database, question="폐가전 수거도 해주시나요?", memo=None)
    add_negative(database, question="폐가전 수거 문의", memo="   ")

    result = corrections(
        database, "폐가전 무료수거 가능한가요?",
        semantic_goal={"customer_goal": "COLLECTION"},
    )

    assert result["selected"] == []
    assert parse_operator_memo(None) is None
    assert parse_operator_memo("   ") is None


def test_g_an_excluded_negative_keeps_its_exclusion_behaviour(database) -> None:
    """EXCLUDED 는 배제 신호일 뿐, 교정 지식으로 승격되지 않는다."""

    add_negative(
        database, question="폐가전 수거도 해주시나요?",
        memo="재사용 불가한 고객별 답변", reason="CUSTOMER_SPECIFIC",
        signal_type="EXCLUDED",
    )

    assert corrections(
        database, "폐가전 무료수거 가능한가요?",
        semantic_goal={"customer_goal": "COLLECTION"},
    )["selected"] == []


# ==========================================================================
# H -- 교정은 지적된 claim 에만 적용된다
# ==========================================================================


def test_h_a_correction_does_not_invalidate_the_correct_positive_claim(
    database,
) -> None:
    positive = add_learning(
        database,
        question="폐가전 무료수거 되나요?",
        answer="폐가전은 무료수거가 가능합니다.",
        semantic_action="COLLECTION",
    )
    add_negative(
        database,
        question="폐가전 수거도 해주시나요?",
        memo=(
            "폐가전 무료수거 안내는 맞음. 고객센터 별도 신청 안내는 잘못됨. "
            "설치기사 방문 시 수거 요청하도록 안내."
        ),
    )

    goal = {
        "customer_goal": "COLLECTION",
        "requested_information": "폐가전 무료수거 가능 여부",
        "atomic_question": "폐가전 무료수거가 가능한지",
    }
    selected = search(database, "폐가전 무료수거 가능한가요?", **goal)
    correction = corrections(
        database, "폐가전 무료수거 가능한가요?", semantic_goal=goal,
    )["selected"]

    assert positive in [int(item["id"]) for item in selected], (
        "Negative 가 저장됐다고 해서 맞는 Positive claim 까지 사라지면 안 된다"
    )
    assert correction, "동시에 교정 constraint 도 같이 검색되어야 한다"
    assert not any(
        "무료수거" in text and "잘못" in text
        for text in correction[0]["bad_patterns"]
    )


# ==========================================================================
# I/J -- 관련 없는 Negative 와 다른 모델의 Negative 는 들어오지 않는다
# ==========================================================================


def test_i_an_unrelated_negative_is_not_retrieved(database) -> None:
    add_negative(
        database,
        question="A/S 보증기간이 얼마나 되나요?",
        memo="A/S 보증은 1년, 패널은 2년으로 안내해야함.",
    )

    assert corrections(
        database, "폐가전 무료수거 가능한가요?",
        semantic_goal={
            "customer_goal": "COLLECTION",
            "requested_information": "폐가전 무료수거 가능 여부",
            "atomic_question": "폐가전 무료수거가 가능한지",
        },
        minimum_relevance=0.24,
    )["selected"] == []


def test_j_a_negative_about_another_model_does_not_leak(database) -> None:
    add_negative(
        database,
        question="LH43BEFHLGFXKR 스탠드에 듀얼 모니터 장착되나요?",
        memo="이 스탠드에는 모니터 1개만 결합 가능하다고 안내해야함.",
        product_name=OTHER_PRODUCT, product_id=OTHER_PRODUCT_ID,
    )

    result = corrections(
        database, "이 스탠드에 듀얼 모니터 장착되나요?",
        semantic_goal={
            "customer_goal": "PRODUCT_SPEC",
            "requested_information": "스탠드 듀얼 모니터 장착 가능 여부",
            "atomic_question": "이 스탠드에 듀얼 모니터를 장착할 수 있는지",
        },
        minimum_relevance=0.24,
    )

    assert result["selected"] == [], (
        "다른 제품에 대해 쓰인 Negative 가 현재 제품 문의로 새면 안 된다"
    )


# ==========================================================================
# 주문 전 문의 -- 주문번호를 요구하는 근거는 scope 밖이다
# ==========================================================================


def test_order_identifier_request_is_read_as_scope_not_as_a_banned_phrase() -> None:
    assert order_identifier_request_reason(
        "정확한 확인을 위해 주문번호를 비밀글로 남겨주세요."
    )
    assert order_identifier_request_reason(
        "주문번호는 네이버 주문내역에서 확인하실 수 있습니다."
    ) is None
    assert order_identifier_request_reason(
        "결제 완료 후 평균 2주 정도 소요됩니다."
    ) is None


def test_pre_order_delivery_question_does_not_reuse_an_order_scoped_answer(
    database,
) -> None:
    order_scoped = add_learning(
        database,
        question="제 주문 배송 언제 되나요?",
        answer=(
            "확인을 위해 네이버 주문내역의 주문번호를 비밀글로 남겨주시면 "
            "배송예정일을 안내드리겠습니다."
        ),
        semantic_action="DELIVERY_STATUS",
    )
    policy = add_learning(
        database,
        question="주문하면 배송까지 며칠 걸리나요?",
        answer="결제 완료 후 평균 2주 정도 배송 기간이 소요됩니다.",
        semantic_action="DELIVERY_POLICY",
    )

    selected = search(
        database, "아직 주문 안 했는데 며칠 내로 배송 가능한지 알려주세요.",
        customer_goal="DELIVERY_POLICY",
        requested_information="주문 전 일반적인 배송 소요 기간",
        atomic_question="주문하면 며칠 안에 배송이 가능한지",
        order_evidence_required=False,
    )

    ids = [int(item["id"]) for item in selected]
    assert order_scoped not in ids, (
        "주문이 없는 고객에게 주문번호를 요구하는 답변이 근거가 되면 안 된다"
    )
    assert policy in ids


def test_order_scope_gate_is_off_when_there_is_no_semantic_understanding(
    database,
) -> None:
    """semantic 이 없으면 기존 동작 그대로 -- 없는 이해를 근거로 거르지 않는다."""

    order_scoped = add_learning(
        database,
        question="제 주문 배송 언제 되나요?",
        answer=(
            "확인을 위해 네이버 주문내역의 주문번호를 비밀글로 남겨주시면 "
            "배송예정일을 안내드리겠습니다."
        ),
    )

    selected = search(database, "제 주문 배송 언제 되나요?")

    assert order_scoped in [int(item["id"]) for item in selected]


def test_a_correction_demanding_an_order_number_is_scoped_out_before_order(
    database,
) -> None:
    add_negative(
        database,
        question="배송 언제 되나요?",
        memo="주문번호가 없는고객이라서 주문번호 안내를 해야함.",
        reason="DELIVERY_INSTALLATION_ERROR",
    )

    result = corrections(
        database, "아직 주문 안 했는데 며칠 내로 배송 가능한가요?",
        semantic_goal={
            "customer_goal": "DELIVERY_POLICY",
            "requested_information": "주문 전 일반적인 배송 소요 기간",
            "atomic_question": "주문하면 며칠 안에 배송이 가능한지",
            "order_evidence_required": False,
        },
    )

    assert result["selected"] == []
    assert result["trace"]["rejection_counts"].get("ORDER_SCOPE_MISMATCH") == 1


# ==========================================================================
# backward compatibility -- metadata 가 없는 과거 Learning 은 계속 검색된다
# ==========================================================================


def test_legacy_learning_without_semantic_metadata_stays_retrievable(
    database,
) -> None:
    legacy = add_learning(
        database,
        question="폐가전 무료수거 되나요?",
        answer="폐가전은 무료수거가 가능합니다.",
        learning_source="SELLER_ANSWER", provenance="NAVER_POSTED",
        semantic_action=None,
    )

    selected = search(
        database, "폐가전 무료수거 가능한가요?",
        customer_goal="COLLECTION",
        requested_information="폐가전 무료수거 가능 여부",
        atomic_question="폐가전 무료수거가 가능한지",
    )

    assert legacy in [int(item["id"]) for item in selected], (
        "semantic metadata 가 없다는 이유로 과거 Learning 을 버리지 않는다"
    )


def test_legacy_memo_candidates_ignore_rows_already_promoted_to_signals(
    database,
) -> None:
    feedback_id = add_negative(
        database,
        question="폐가전 수거도 해주시나요?",
        memo="설치기사 방문 시 수거 요청하도록 안내.",
    )
    with database.connection() as conn:
        conn.execute(
            """
            INSERT INTO learning_signals (
                source_key, signal_kind, origin_kind, learning_feedback_id,
                content_text, active
            ) VALUES ('sig-1','CORRECTION','NEGATIVE_REVIEW', ?, '이미 승격됨', 1)
            """,
            (feedback_id,),
        )
        conn.commit()

    assert LearningFeedbackRepository(database).legacy_memo_candidates(
        store_code="OJE_PLUS"
    ) == []
