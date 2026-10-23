"""What may contradict a verified product fact, and whose Learning it costs.

Found replaying a real Coupang inquiry -- "피벗 기능 가능한가요? 높낮이?각도?
스피커있나요?" on LS27HG400EKXKR.  Seventeen approved Learning answers were
retrieved across the four atoms and every one was discarded, with
``approved_learning_evidence.usable`` false and
``PRODUCT_FACT_VS_LEARNING_CONFLICT`` as the reason.  Two faults produced
that, and they are separate.

**A number that names something is not a measurement.**  The one disputed
answer was an A/S referral about screen noise whose only digits were the
service centre's phone number, 1588-3366.  ``quantities`` read every digit
run as a claim, so the two sets of numbers shared none and the answer was
reported as contradicting ``pivot_spec = -92 ~ +92`` -- an answer that says
nothing about pivot at all.  What separates the two is the company a number
keeps: 1588-3366 is one hyphen-joined identifier, -92 ~ +92 is two signed
measurements, LS27HG400 glues digits to letters, and 120mm glues them to a
unit.

**A conflict belongs to the sub-question it was found under.**  Each conflict
already records which, and the refusal ignored it: one disputed pivot answer
removed the Learning of the speaker, height and angle atoms with it.

Both halves of the original check still have to work, and the tests below
pin that too: the same attribute disagreeing in polarity or in number is
still a conflict, and an inquiry whose every atom is disputed is still
refused whole.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.learning_evidence_policy import (
    evaluate, fact_conflicts, quantities, quantities_conflict,
)

# The real A/S referral, from the replayed trace (learning 319151).
AS_REFERRAL = (
    "안녕하세요 고객님, 삼성공식파트너 오제앤에스입니다. 먼저 사용 중에 "
    "불편을 드려 정말 죄송합니다. 문의해주신 화면 증상(노이즈 및 점 현상)은 "
    "제품 자체 점검 및 점검 기사님의 정확한 진단이 필요한 상황입니다. "
    "제조사 서비스 규정에 따라 삼성전자 서비스 센터(1588-3366)로 접수해주시거나, "
    "기사님 방문 점검을 통해 증상을 확인받으신 후 결과에 따라 A/S가 진행됩니다."
)
PIVOT_SPEC = "-92 ~ +92"
HEIGHT_SPEC = "105.0 (±5.0)"


def fact(field_key: str, value: str, unit: str | None = None):
    # 수치 fact 는 단위를 자기 컬럼에 담는다. 운영 PK 의 height_adjust 17행은
    # value='105.0(±5.0)' / unit='mm' 형태이고, 측정값 비교는 그 단위를 읽는다.
    return SimpleNamespace(field_key=field_key, value=value, unit=unit)


def approved(learning_id: int, answer: str, subquestion: str):
    """One approved, exact-product, definite candidate."""

    return {
        "learning_example_id": learning_id,
        "answer": answer,
        "authority": "APPROVED",
        "matched_subquestion": subquestion,
        "compatibility": {"product_match": "EXACT_MODEL"},
    }


# --- 1-2. an identifier never contradicts a specification ----------------

def test_a_service_centre_number_is_not_a_conflicting_quantity() -> None:
    """The exact replayed pairing."""

    assert quantities(AS_REFERRAL) == frozenset()
    assert quantities_conflict(AS_REFERRAL, PIVOT_SPEC) is False
    assert quantities_conflict(AS_REFERRAL, HEIGHT_SPEC) is False

    conflicts = fact_conflicts(
        [approved(319151, AS_REFERRAL, "피벗 기능이 가능한가요?")],
        [fact("pivot_spec", PIVOT_SPEC), fact("pivot", "-92°(±2°) ~ 92°(±2°)")],
    )
    assert conflicts == ()


def test_an_unrelated_answer_does_not_oppose_a_negative_fact() -> None:
    """``speaker = 없음`` against an affirmative sentence about A/S.

    Polarity compared a whole sentence's mood with a stored absence, and the
    sentence was about a service centre. An answer can only contradict a
    fact it talks about.
    """

    conflicts = fact_conflicts(
        [approved(319151, AS_REFERRAL, "스피커가 있나요?")],
        [fact("speaker", "없음")],
    )
    assert conflicts == ()


@pytest.mark.parametrize("text,expected", [
    pytest.param("삼성전자 서비스 센터(1588-3366)로 접수", set(), id="phone"),
    pytest.param("주문번호 26102469841482 확인", set(), id="order-number"),
    pytest.param("LS27HG400EKXKR 모델입니다", set(), id="model-code"),
    pytest.param("주문번호 2026****0811", set(), id="masked"),
    pytest.param("2026-10-20 출고", set(), id="date"),
    pytest.param("높낮이는 120mm까지", {120.0}, id="unit-mm"),
    pytest.param("무게는 6.5kg", {6.5}, id="unit-kg"),
    pytest.param("HDMI 단자는 3개입니다", {3.0}, id="korean-counter"),
    pytest.param("출력 5W", {5.0}, id="unit-w"),
    pytest.param("-92 ~ +92", {92.0}, id="signed-range"),
])
def test_which_numbers_count_as_measurements(text, expected) -> None:
    assert quantities(text) == frozenset(expected)


# --- 3-4. the same attribute disagreeing is still a conflict -------------

def test_a_speaker_claim_still_contradicts_a_verified_absence() -> None:
    conflicts = fact_conflicts(
        [approved(1, "해당 제품은 내장 스피커를 지원합니다.", "스피커가 있나요?")],
        [fact("speaker", "없음")],
    )
    assert len(conflicts) == 1
    assert conflicts[0]["field_key"] == "speaker"
    assert conflicts[0]["learning_id"] == 1
    assert conflicts[0]["subquestion"] == "스피커가 있나요?"


def test_a_different_measurement_of_the_same_attribute_still_conflicts(
) -> None:
    conflicts = fact_conflicts(
        [approved(2, "높낮이는 120mm까지 조절됩니다.", "높낮이 조절이 가능한가요?")],
        [fact("height_adjust", HEIGHT_SPEC, "mm")],
    )
    assert len(conflicts) == 1
    assert conflicts[0]["field_key"] == "height_adjust"


def test_the_same_measurement_is_not_a_conflict() -> None:
    assert fact_conflicts(
        [approved(3, "높낮이는 105mm까지 조절됩니다.", "높낮이 조절이 가능한가요?")],
        [fact("height_adjust", HEIGHT_SPEC, "mm")],
    ) == ()


def test_a_port_count_disagreement_still_conflicts() -> None:
    """The case ``quantities`` was written for, unchanged."""

    assert quantities_conflict("HDMI 단자는 3개입니다.", "HDMI 단자는 2개입니다.")
    assert not quantities_conflict("HDMI 단자는 3개입니다.", "HDMI는 3개 있습니다.")
    assert not quantities_conflict("지원합니다.", "HDMI 단자는 3개입니다.")


# --- 5-6. the refusal is scoped to the disputed atoms -------------------

def _context(items):
    return {
        "similar_approved_answers": list(items),
        "subquestion_evidence": [
            {
                "subquestion": item["matched_subquestion"],
                "status": "CANDIDATE",
                "source": "ACTIVE_POSITIVE_LEARNING",
            }
            for item in items
        ],
    }


def test_one_disputed_atom_does_not_discard_the_others() -> None:
    """The replayed shape: four atoms, one disputed."""

    items = [
        approved(10, "해당 제품은 내장 스피커를 지원합니다.", "스피커가 있나요?"),
        approved(11, "높낮이는 105mm까지 조절됩니다.", "높낮이 조절이 가능한가요?"),
        approved(12, "틸트는 -3 ~ 25도까지 가능합니다.", "각도 조절이 가능한가요?"),
    ]
    decision = evaluate(
        learning_context=_context(items),
        safe_facts=[fact("speaker", "없음")],
    )

    # The speaker answer contradicts the verified absence and is refused.
    assert decision.conflict is True
    assert {c["subquestion"] for c in decision.conflicts} == {"스피커가 있나요?"}
    # The height and angle answers are independent and survive.
    assert decision.usable is True
    assert set(decision.learning_ids) == {11, 12}
    assert 10 not in decision.learning_ids
    assert set(decision.subquestions) == {
        "높낮이 조절이 가능한가요?", "각도 조절이 가능한가요?"}


def test_every_atom_disputed_is_still_refused_whole() -> None:
    items = [
        approved(20, "해당 제품은 내장 스피커를 지원합니다.", "스피커가 있나요?"),
        approved(21, "높낮이는 120mm까지 조절됩니다.", "높낮이 조절이 가능한가요?"),
    ]
    decision = evaluate(
        learning_context=_context(items),
        safe_facts=[fact("speaker", "없음"), fact("height_adjust", HEIGHT_SPEC, "mm")],
    )

    assert decision.usable is False
    assert decision.reason == "PRODUCT_FACT_VS_LEARNING_CONFLICT"
    assert decision.conflict is True
    assert decision.learning_ids == ()


def test_a_conflict_still_travels_when_other_atoms_are_usable() -> None:
    """The caller marks the disputed sub-questions CONFLICT from this.

    ``HybridAnswerService`` reads ``decision.conflicts`` and only touches the
    sub-questions named there, so the conflict has to keep travelling even
    when the decision is usable -- otherwise a disputed atom would quietly
    look supported.
    """

    items = [
        approved(30, "해당 제품은 내장 스피커를 지원합니다.", "스피커가 있나요?"),
        approved(31, "피벗은 -92 ~ +92도까지 가능합니다.", "피벗 기능이 가능한가요?"),
    ]
    decision = evaluate(
        learning_context=_context(items),
        safe_facts=[fact("speaker", "없음")],
    )
    assert decision.usable is True
    assert decision.conflict is True
    assert decision.conflicts, "the conflict record was dropped"
    assert all(
        c["subquestion"] == "스피커가 있나요?" for c in decision.conflicts)


def test_the_disputed_answer_itself_is_never_usable() -> None:
    """Scoping must not let the conflicting row through on another atom."""

    disputed = approved(40, "해당 제품은 내장 스피커를 지원합니다.", "스피커가 있나요?")
    also = dict(disputed)
    also["matched_subquestion"] = "각도 조절이 가능한가요?"
    decision = evaluate(
        learning_context=_context([disputed, also]),
        safe_facts=[fact("speaker", "없음")],
    )
    assert 40 not in decision.learning_ids


# --- 7. one policy, both markets ----------------------------------------

def test_the_policy_is_market_independent() -> None:
    """Nothing in the decision reads a store, a market or an applicability.

    The Coupang and Naver replays of this inquiry produced the same verdict
    for the same reason; that is a property of this module having no market
    input at all, which is what this pins.
    """

    import inspect

    import services.learning_evidence_policy as policy

    source = inspect.getsource(policy)
    # No market, store or applicability reaches the decision: ``evaluate``
    # takes a learning context, the safe facts and a scope callable, and
    # nothing else.
    signature = inspect.signature(policy.evaluate)
    # 아래 세 개는 시장·스토어·applicability 가 아니다. ``answered_at`` 은 승인
    # 후 3개월 창을 재는 기준 시각, ``asked_product_id`` 는 고객이 보고 있는
    # 리스팅, ``exact_model_for`` 는 그 리스팅의 모델을 증명하는 카탈로그
    # 리졸버다. 상품 ID 와 모델은 마켓 양쪽이 똑같이 가지는 값이고, 이 테스트가
    # 막으려는 입력은 아래 ``for term in`` 이 이름으로 직접 확인한다.
    assert set(signature.parameters) == {
        "learning_context", "safe_facts", "scope_for", "answered_at",
        "asked_product_id", "exact_model_for"}
    for term in ("store_code", "market_applicability", "origin_market",
                 "is_market_applicable"):
        assert term not in source, term

    items = [approved(50, "높낮이는 105mm까지 조절됩니다.", "높낮이 조절이 가능한가요?")]
    first = evaluate(learning_context=_context(items),
                     safe_facts=[fact("height_adjust", HEIGHT_SPEC, "mm")])
    second = evaluate(learning_context=_context(items),
                      safe_facts=[fact("height_adjust", HEIGHT_SPEC, "mm")])
    assert first.to_dict() == second.to_dict()
    assert first.usable is True
