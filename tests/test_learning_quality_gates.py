"""What may be shown to the model as an approved answer, and what may not.

Two corpora were being confused. The prompt's factual slot admitted anything
that was not ``style_only``; the validator's grounding corpus applied
``usable_as_factual_evidence`` on top. Replayed over a 22-question corpus
against the live store, 15 of the 24 items handed to the model as
``similar_approved_answers`` carried an explicit hedge -- and 10 rows in that
store combine one with rating 5 and HUMAN_VERIFIED_NAVER_POSTED. Somebody
approved a guess; the approval is real and the certainty is not.

The store also holds 47 answers carrying a redaction token and 11 more with a
partially blanked number ("주문번호 2026****2541"), which is the same thing
under a different shape, and 179 rows whose text is a repeat of another row's
-- so one fact could occupy several evidence slots and look independently
corroborated.

None of these tests remove an answer from the corpus. A hedged or duplicated
sentence is still written in the seller's voice and stays available as a tone
reference; what it loses is the claim to prove something.
"""
from __future__ import annotations

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.learning_evidence_policy import (
    contamination_reason,
    estimation_reason,
    usable_as_factual_evidence,
)
from services.similar_answer_service import SimilarAnswerService


PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
OTHER_PRODUCT = "삼성 80cm(32인치) 스마트 모니터 M5 LS32DM501EKXKR"


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "learning.db")
    value.initialize()
    return value


def add(
    database: Database,
    *,
    question: str,
    answer: str,
    style_only: bool = False,
    rating: int = 5,
    human_verified: bool = True,
    signal: str = "POSITIVE",
    product_name: str = PRODUCT,
    validity_type: str = "PERMANENT",
    valid_from: str | None = None,
    valid_until: str | None = None,
    source_key: str | None = None,
) -> int:
    metadata = {
        "human_verified": human_verified,
        "learning_signal_type": signal,
        "facts_authority": "APPROVED" if human_verified else "AUTO",
    }
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                final_answer, seller_answer, posted, rating, edit_ratio,
                quality_score, style_only, version, metadata_json, active,
                usage_count, created_at, updated_at, validity_type,
                valid_from, valid_until, validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_key or f"k-{abs(hash((question, answer)))}",
                "SELLER_ANSWER", question, question, "OJE_PLUS",
                "PRODUCT_INQUIRY", product_name, answer, answer, 1, rating,
                0.0, 1.0, int(style_only), 1,
                __import__("json").dumps(metadata, ensure_ascii=False), 1, 0,
                "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z",
                validity_type, valid_from, valid_until, 1,
            ),
        )
        return int(cursor.lastrowid)


def retrieve(database: Database, question: str, **filters):
    service = SimilarAnswerService(LearningRepository(database))
    return service.context(
        question,
        store_code="OJE_PLUS",
        product_name=filters.pop("product_name", PRODUCT),
        inquiry_type="PRODUCT_INQUIRY",
        **filters,
    )


def factual_answers(context) -> list[str]:
    return [
        str(item.get("answer") or "")
        for item in context.get("similar_approved_answers") or []
    ]


def style_answers(context) -> list[str]:
    return [
        str(item.get("answer") or "")
        for item in context.get("seller_style_examples") or []
    ]


# ==========================================================================
# 1-2. STYLE_ONLY
# ==========================================================================


def test_style_only_spec_never_grounds_a_fact(database) -> None:
    """The headline invariant: a tone reference cannot prove a number."""

    add(
        database,
        question="HDMI 단자가 몇 개인가요?",
        answer="HDMI 단자는 4개입니다.",
        style_only=True,
    )
    context = retrieve(database, "HDMI 단자가 몇 개인가요?")

    assert factual_answers(context) == []
    assert not usable_as_factual_evidence(
        {"style_only": True, "answer": "HDMI 단자는 4개입니다."}
    )


def test_style_only_remains_available_as_a_tone_reference(database) -> None:
    add(
        database,
        question="HDMI 단자가 몇 개인가요?",
        answer="HDMI 단자는 4개입니다.",
        style_only=True,
    )
    context = retrieve(database, "HDMI 단자가 몇 개인가요?")

    assert any("HDMI" in answer for answer in style_answers(context))
    assert context["oje_style_rules"]["seller_examples_are_style_only"] is True


# ==========================================================================
# 3. Placeholder contamination
# ==========================================================================


def test_redaction_token_answer_is_not_retrieved_at_all(database) -> None:
    add(
        database,
        question="A/S 문의 전화번호 알려주세요",
        answer="A/S 문의는 <masked-phone>로 연락해 주세요.",
    )
    context = retrieve(database, "A/S 문의 전화번호 알려주세요")

    assert factual_answers(context) == []
    assert style_answers(context) == []


def test_partially_masked_number_is_treated_as_a_redaction(database) -> None:
    """"주문번호 2026****2541" is a record of a removal, not an order number."""

    assert contamination_reason("주문번호는 2026****2541 입니다.") is not None

    add(
        database,
        question="주문번호 어디서 확인하나요?",
        answer="구매하신 제품의 주문번호는 2026****2541 입니다.",
    )
    context = retrieve(database, "주문번호 어디서 확인하나요?")

    assert factual_answers(context) == []


@pytest.mark.parametrize(
    "text", ["★★★ 안내 ★★★", "평점 ***** 감사합니다", "전화 02-706-2678로 문의"]
)
def test_decorative_asterisks_are_not_redactions(text: str) -> None:
    """The rule needs digits on both sides, or it eats ordinary punctuation."""

    assert contamination_reason(text) is None


def test_no_placeholder_reaches_the_prompt(database) -> None:
    add(
        database,
        question="설치 문의",
        answer="설치 기사님이 <masked-phone>로 연락드립니다.",
    )
    add(database, question="설치 문의", answer="설치는 삼성 기사님이 방문합니다.")
    context = retrieve(database, "설치 문의")

    for answer in factual_answers(context) + style_answers(context):
        assert "<masked-" not in answer


# ==========================================================================
# 4. Hedged answers with high authority
# ==========================================================================


def test_hedged_answer_with_rating_five_is_not_factual_evidence(
    database,
) -> None:
    """Approving a guess approves the guess, not the certainty."""

    add(
        database,
        question="기사님이 언제 연락주시나요?",
        answer="설치 기사님이 전날 연락드릴 것으로 보입니다.",
        rating=5,
        human_verified=True,
    )
    context = retrieve(database, "기사님이 언제 연락주시나요?")

    assert factual_answers(context) == []
    assert estimation_reason("설치 기사님이 전날 연락드릴 것으로 보입니다.") is not None


def test_hedged_answer_is_demoted_not_discarded(database) -> None:
    add(
        database,
        question="기사님이 언제 연락주시나요?",
        answer="설치 기사님이 전날 연락드릴 것으로 보입니다.",
    )
    context = retrieve(database, "기사님이 언제 연락주시나요?")

    assert any("전날" in answer for answer in style_answers(context))
    assert context["learning_retrieval"]["HEDGED_FACTUAL_DEMOTED"] >= 1


def test_a_definite_answer_is_still_factual_evidence(database) -> None:
    """The gate must not swallow ordinary declaratives.

    ``is_hedged`` answers the validator's question -- does this commit to a
    polarity or a quantity -- and says no for "이 제품은 LED 패널을 사용합니
    다". Using it here would have rejected most of the usable corpus.
    """

    add(
        database,
        question="패널 종류가 뭔가요?",
        answer="이 제품은 LED 패널을 사용합니다.",
    )
    context = retrieve(database, "패널 종류가 뭔가요?")

    assert any("LED" in answer for answer in factual_answers(context))


# ==========================================================================
# 5-7. Temporary validity
# ==========================================================================


def test_expired_temporary_learning_is_not_retrieved(database) -> None:
    add(
        database,
        question="배송 얼마나 걸리나요?",
        answer="행사 기간에는 배송이 3주 소요됩니다.",
        validity_type="TEMPORARY",
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2026-02-01T00:00:00Z",
    )
    context = retrieve(database, "배송 얼마나 걸리나요?")

    assert factual_answers(context) == []
    assert style_answers(context) == []


def test_future_temporary_learning_is_not_retrieved(database) -> None:
    add(
        database,
        question="배송 얼마나 걸리나요?",
        answer="다음 행사 기간에는 배송이 3주 소요됩니다.",
        validity_type="TEMPORARY",
        valid_from="2099-01-01T00:00:00Z",
        valid_until="2099-02-01T00:00:00Z",
    )
    context = retrieve(database, "배송 얼마나 걸리나요?")

    assert factual_answers(context) == []


def test_active_temporary_learning_is_retrieved(database) -> None:
    add(
        database,
        question="배송 얼마나 걸리나요?",
        answer="현재 행사 기간에는 배송이 3주 소요됩니다.",
        validity_type="TEMPORARY",
        valid_from="2026-01-01T00:00:00Z",
        valid_until="2099-01-01T00:00:00Z",
    )
    context = retrieve(database, "배송 얼마나 걸리나요?")

    assert any("3주" in answer for answer in factual_answers(context))


# ==========================================================================
# 8. Negative signals
# ==========================================================================


def test_negative_learning_is_not_offered_as_evidence(database) -> None:
    add(
        database,
        question="벽걸이 설치 되나요?",
        answer="벽걸이 설치는 불가능합니다.",
        signal="NEGATIVE",
    )
    context = retrieve(database, "벽걸이 설치 되나요?")

    assert factual_answers(context) == []
    assert style_answers(context) == []


def test_a_negative_example_does_not_suppress_its_whole_topic(
    database,
) -> None:
    """Suppress the bad answer, not the subject it was about."""

    add(
        database,
        question="벽걸이 설치 되나요?",
        answer="벽걸이 설치는 불가능합니다.",
        signal="NEGATIVE",
    )
    add(
        database,
        question="벽걸이 설치 되나요?",
        answer="벽걸이 설치가 가능하며 기사님이 방문합니다.",
    )
    context = retrieve(database, "벽걸이 설치 되나요?")

    answers = factual_answers(context)
    assert any("가능하며" in answer for answer in answers)
    assert not any("불가능합니다" in answer for answer in answers)


# ==========================================================================
# 9-10. Product scope
# ==========================================================================


def test_another_models_spec_does_not_ground_this_one(database) -> None:
    add(
        database,
        question="HDMI 단자가 몇 개인가요?",
        answer="HDMI 단자는 3개입니다.",
        product_name=OTHER_PRODUCT,
    )
    context = retrieve(
        database, "HDMI 단자가 몇 개인가요?", product_name=PRODUCT
    )

    assert not any("3개" in answer for answer in factual_answers(context))


def test_a_generic_delivery_policy_is_reusable(database) -> None:
    """Scope restriction applies to specs, not to store-wide policy."""

    add(
        database,
        question="배송은 보통 며칠 걸리나요?",
        answer="결제 확인 후 설치 기사님 일정에 맞춰 배송이 진행됩니다.",
        product_name=OTHER_PRODUCT,
    )
    context = retrieve(
        database, "배송은 보통 며칠 걸리나요?", product_name=PRODUCT
    )

    assert factual_answers(context)


# ==========================================================================
# 14. Duplicate evidence
# ==========================================================================


def test_the_same_answer_stored_twice_fills_one_evidence_slot(
    database,
) -> None:
    """One fact repeated is not two independent confirmations of it."""

    for index in range(3):
        add(
            database,
            question="설치는 누가 하나요?",
            answer="설치는 삼성 기사님이 방문하여 진행합니다.",
            source_key=f"dup-{index}",
        )
    context = retrieve(database, "설치는 누가 하나요?")

    answers = factual_answers(context)
    assert len(answers) == 1
    assert context["learning_retrieval"]["DUPLICATE_EVIDENCE"] >= 1


def test_answers_that_differ_are_kept_apart(database) -> None:
    add(
        database,
        question="설치는 누가 하나요?",
        answer="설치는 삼성 기사님이 방문하여 진행합니다.",
        source_key="a",
    )
    add(
        database,
        question="설치는 누가 하나요?",
        answer="설치 기사님 방문 일정은 알림톡으로 안내됩니다.",
        source_key="b",
    )
    context = retrieve(database, "설치는 누가 하나요?")

    assert len(factual_answers(context)) == 2


# ==========================================================================
# 16. No regression for ordinary Learning
# ==========================================================================


def test_an_ordinary_approved_answer_is_still_selected(database) -> None:
    add(
        database,
        question="A/S는 어디서 받나요?",
        answer="A/S는 삼성전자 서비스센터에서 접수하시면 됩니다.",
    )
    context = retrieve(database, "A/S는 어디서 받나요?")

    assert any("서비스센터" in answer for answer in factual_answers(context))


def test_retrieval_trace_names_why_an_item_was_demoted(database) -> None:
    add(
        database,
        question="배송 언제 되나요?",
        answer="8월 둘째 주 이후로 예상됩니다.",
    )
    context = retrieve(database, "배송 언제 되나요?")
    trace = context["learning_retrieval"]

    assert "HEDGED_FACTUAL_DEMOTED" in trace
    assert "DUPLICATE_EVIDENCE" in trace


# ==========================================================================
# 28. Same subject, different property -- the 온누리 seller-name case
# ==========================================================================

ONNURI_PERIOD_ANSWER = (
    "디지털 온누리 상품권 환급은 8월 1일~8월 31일 주문 건에 한해 신청 가능하며, "
    "자세한 내용은 삼성전자 행사 페이지 https://event.samsung.com/onnuri 에서 "
    "확인하실 수 있습니다."
)
ONNURI_SELLER_QUESTION = (
    "삼성닷컴 온누리 신청을 하려고 하는데 판매처를 뭐라고 검색해야 하나요? "
    "오제앤에서 검색하면 (오제앤에스) 이것만 떠서 여기로 했더니 판매처가 다르다고 "
    "수정하라는 연락을 받았습니다."
)


def with_onnuri_period(database):
    add(
        database,
        question="온누리 환급 신청 기간이 언제인가요?",
        answer=ONNURI_PERIOD_ANSWER,
    )


def test_the_period_answer_still_answers_a_period_question(database) -> None:
    """C: the existing Learning is not deleted and stays usable for its own question."""

    with_onnuri_period(database)
    context = retrieve(database, "온누리 환급 신청 기간이 언제인가요?")

    assert any("8월 1일" in answer for answer in factual_answers(context))


def test_the_period_answer_does_not_answer_a_seller_question(database) -> None:
    """The operational failure: 온누리+기간 offered as grounds for 온누리+판매처."""

    with_onnuri_period(database)
    context = retrieve(database, ONNURI_SELLER_QUESTION)

    assert factual_answers(context) == []


def test_the_seller_correction_report_gets_no_period_evidence(
    database,
) -> None:
    with_onnuri_period(database)
    context = retrieve(database, "판매처가 다르다고 수정하라고 연락받았어요.")

    assert factual_answers(context) == []


def test_no_seller_name_and_no_event_url_can_be_taken_from_it(
    database,
) -> None:
    """B: with no grounded seller answer, nothing is borrowed to fill the gap."""

    with_onnuri_period(database)
    context = retrieve(database, ONNURI_SELLER_QUESTION)

    for answer in factual_answers(context):
        assert "event.samsung.com" not in answer
        assert "8월 1일" not in answer


def test_a_verified_seller_answer_is_used_when_one_exists(database) -> None:
    """A: when the seller-name fact is on record, it answers the question."""

    with_onnuri_period(database)
    add(
        database,
        question="온누리 신청 시 판매처는 무엇으로 입력하나요?",
        answer="온누리 신청 시 판매처는 오제앤에스로 입력해 주세요.",
    )
    context = retrieve(database, ONNURI_SELLER_QUESTION)

    answers = factual_answers(context)
    assert any("오제앤에스" in answer for answer in answers)
    assert not any("8월 1일" in answer for answer in answers)


def test_wrapped_festival_seller_question_retrieves_verified_learning(
    database,
) -> None:
    """Prose wrapping must not break the seller-name retrieval query."""

    from answer.text_utils import split_subquestions

    add(
        database,
        question="삼성 페스티벌 신청 시 구매처명은 무엇으로 입력하나요?",
        answer="구매처명은 오제앤에스로 입력해 주세요.",
    )
    wrapped = (
        "구매 후 삼성 페스티벌 신청했는데\n"
        "구매처명을 잘못 입력했다고 보완 요청이 왔습니다.\n"
        "어떤 이름으로 입력해야 하나요?"
    )
    parts = split_subquestions(wrapped)
    assert len(parts) == 1

    answers = factual_answers(retrieve(database, parts[0]))
    assert any("오제앤에스" in answer for answer in answers)


@pytest.mark.parametrize(
    ("question", "stored_question", "stored_answer"),
    [
        (
            "배송일을 변경할 수 있나요?",
            "배송 기간이 얼마나 되나요?",
            "배송 기간은 결제 후 2~3일 소요됩니다.",
        ),
        (
            "설치 비용이 얼마인가요?",
            "설치 방법이 어떻게 되나요?",
            "설치는 삼성 기사님이 방문하여 진행합니다.",
        ),
        (
            "브라켓이 구성품에 포함되나요?",
            "이 브라켓과 호환되나요?",
            "브라켓 호환은 베사 규격이 맞으면 사용 가능합니다.",
        ),
    ],
)
def test_the_rule_generalises_beyond_the_promotion(
    database, question: str, stored_question: str, stored_answer: str
) -> None:
    """One dimension, not one rule per promotion.

    배송+기간 vs 배송+변경, 설치+방법 vs 설치+비용, 브라켓+호환 vs 브라켓+구성품 --
    the same subject asked about a different property each time.
    """

    add(database, question=stored_question, answer=stored_answer)
    context = retrieve(database, question)

    assert factual_answers(context) == []


def test_matching_attributes_are_still_compatible(database) -> None:
    """The other direction: the gate must not reject a real match."""

    add(
        database,
        question="배송 기간이 얼마나 되나요?",
        answer="배송 기간은 결제 후 2~3일 소요됩니다.",
    )
    context = retrieve(database, "배송 기간이 얼마나 걸리나요?")

    assert any("2~3일" in answer for answer in factual_answers(context))


def test_a_candidate_with_no_stated_attribute_is_left_alone(database) -> None:
    """Generic guidance stays reusable; only two specific, differing sides clash."""

    add(
        database,
        question="설치는 누가 하나요?",
        answer="설치는 삼성 기사님이 방문하여 진행합니다.",
    )
    context = retrieve(database, "설치는 누가 하나요?")

    assert factual_answers(context)


# ==========================================================================
# 28b. The event template path -- where the operational failure actually was
# ==========================================================================

from answer.engine import AnswerEngine  # noqa: E402
from answer.text_utils import is_seller_identity_question  # noqa: E402

ONNURI_OPERATIONAL = (
    "삼성닷컴 온누리 신청하려고 하는데요 판매처를 뭐라고 검색해야 하나요? "
    "오제앤에스 검색하면 '테크한다(오제앤에스)' 이것만 떠서 여기로 했더니 "
    "판매처가 다르다고 수정하라고 연락을 받았어요"
)


def rule_answer(question: str) -> str:
    return AnswerEngine().answer(PRODUCT, question, "").answer or ""


def test_the_event_template_does_not_answer_a_seller_question() -> None:
    """The Learning gate was never the whole story.

    Replaying the operational inquiry through the full pipeline showed the
    rebate period and event URL still coming back -- and auto-postable --
    because they came from the rule engine's 온누리 branch, not from Learning.
    Its seller guard knew only "구매처", and the customer wrote "판매처".
    """

    answer = rule_answer(ONNURI_OPERATIONAL)

    assert "7월 5일" not in answer
    assert "samsung.com/sec/event" not in answer
    assert "환급 신청 대상 기준" not in answer


@pytest.mark.parametrize(
    "question",
    [
        "온누리 신청할 때 판매처를 뭐라고 입력하나요?",
        "감사페스티벌 구매처를 어떻게 입력하나요?",
        "온누리 신청 시 스토어명을 뭐라고 써야 하나요?",
        "환급 신청에 판매자명을 뭘로 넣나요?",
    ],
)
def test_every_name_for_the_seller_is_recognised(question: str) -> None:
    """One predicate, shared with the compatibility gate, not one literal."""

    assert is_seller_identity_question(question)
    answer = rule_answer(question)
    assert "samsung.com/sec/event" not in answer


def test_no_seller_name_is_invented_when_there_is_no_basis() -> None:
    answer = rule_answer(ONNURI_OPERATIONAL)

    for invented in ("테크한다", "오제앤에스로 입력", "판매처는 오제"):
        assert invented not in answer


def test_the_rebate_period_question_still_gets_its_answer() -> None:
    """Control case: 온누리 itself is not blocked, only the substitution is."""

    answer = rule_answer("온누리 환급 신청 기간이 언제인가요?")

    assert "온누리" in answer
    assert "7월 5일" in answer


def test_seller_identity_is_one_predicate_across_both_layers() -> None:
    """The engine guard and the evidence gate must not drift apart."""

    from services.learning_compatibility_service import attributes_of

    for question in ("판매처를 뭐라고 검색해야 하나요?", "구매처 입력 어떻게 하나요?"):
        assert is_seller_identity_question(question)
        assert "SELLER_IDENTITY" in attributes_of(question)
