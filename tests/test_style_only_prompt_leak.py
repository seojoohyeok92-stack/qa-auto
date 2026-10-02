"""``style_only`` is provenance, and must not reach the model as a verdict.

It is written once, as ``source == "SELLER_ANSWER"``: an answer a person at
the store wrote and posted to the customer directly.  It says nothing about
product identity, factual eligibility or attribute relevance -- those come
from ``compatibility``, the identity gates, and GPT ②'s reading of the text.

Carried into the prompt the name was read as a ruling.  On inquiry 690731999
("25년형, 26년형 다른점이 뭔가요?") the store's own answer to the same question
on the same listing reached GPT ② with EXACT_MODEL, ``eligible``, the highest
relevance of five candidates and ``attached_to_prompt``, and came back as
"질문에는 직접 부합하지만 스타일 참고용으로 분류되어 사실 근거로 사용하지
않았습니다".

The shape of that case is reproduced here synthetically.  No production
export is used: the real dump carries customer inquiry text and order
identifiers, and the contract does not need them.

The tests on either side of the fix matter equally.  Dropping the field must
not let a wrong-model fact, an unrelated attribute, an expired promotion or
another customer's order through -- none of which was ever decided by
``style_only``, which is exactly what the second half asserts.
"""

from __future__ import annotations

import json

from services.learning_context_service import (
    _LEARNING_PROMPT_KEYS,
    prompt_context,
)

#: The same listing on both sides, as in the production specimen.
LISTING = {
    "brand": "SAMSUNG",
    "category": "TV",
    "family": None,
    "model_code": "43BEF",
    "normalized_name": "삼성 43인치 비즈니스TV LH43BEFH 스탠드형",
    "option": None,
    "product_id": "12021985151",
    "product_name": "삼성 43인치 비즈니스TV LH43BEFH 스탠드형",
    "size_inches": 43.0,
}


def _same_product_compatibility(**overrides: object) -> dict:
    value = {
        "candidate_product": dict(LISTING),
        "current_product": dict(LISTING),
        "eligible": True,
        "hard_reject": False,
        "product_match": "EXACT_MODEL",
        "product_match_reason": "EXPLICIT_MODEL_CODE_MATCH",
        "reject_reason": None,
        "topic_match": "UNCERTAIN",
        "topic_match_reason": "TOPIC_UNCLEAR",
    }
    value.update(overrides)
    return value


def _seller_posted_learning(**overrides: object) -> dict:
    """A seller-written answer to this product's own question.

    ``style_only`` true and ``authority`` AUTO is what the write path produces
    for ``SELLER_ANSWER``: there is no ``human_verified`` flag on these rows.
    """

    value = {
        "learning_example_id": 194680,
        "question": "2025,2026 차이가 무엇인지 알 수 있나요?",
        "answer": "모델명의 변경 이외의 큰 차이점은 없습니다.",
        "relevance": 1.003,
        "authority": "AUTO",
        "evidence_authority": "SELLER_POSTED",
        "style_only": True,
        "hedge_reason": None,
        "matched_subquestion": "25년형과 26년형 제품의 차이점이 무엇인가요?",
        "compatibility": _same_product_compatibility(),
    }
    value.update(overrides)
    return value


def _context(*items: dict, style_examples: list[dict] | None = None) -> dict:
    return {
        "similar_approved_answers": list(items),
        "seller_style_examples": list(style_examples or []),
        "oje_style_rules": {},
    }


# ----------------------------------------------------------------------
# The leak itself
# ----------------------------------------------------------------------


def test_style_only_is_not_serialised_into_the_prompt() -> None:
    """CONTRACT B: the field does not exist in what the model is handed."""

    projected = prompt_context(_context(_seller_posted_learning()))
    rows = projected["similar_approved_answers"]
    assert len(rows) == 1
    assert "style_only" not in rows[0]
    # Not merely absent from the mapping -- absent from the serialised prompt,
    # which is the form GPT ② actually reads.
    assert "style_only" not in json.dumps(projected, ensure_ascii=False)


def test_style_only_is_dropped_from_the_style_channel_too() -> None:
    """Both Learning lists share one projection, so both must be clean."""

    assert "seller_style_examples" in _LEARNING_PROMPT_KEYS
    projected = prompt_context(
        _context(style_examples=[_seller_posted_learning()])
    )
    assert "style_only" not in projected["seller_style_examples"][0]


# ----------------------------------------------------------------------
# What must survive the drop
# ----------------------------------------------------------------------


def test_seller_posted_learning_keeps_its_evidence_candidacy() -> None:
    """CONTRACT C/D/E: the row is still evidence, not a demoted reference."""

    context = _context(_seller_posted_learning())
    projected = prompt_context(context)
    rows = projected["similar_approved_answers"]

    # C: still in the evidence list, not moved to the style channel.
    assert [row["learning_example_id"] for row in rows] == [194680]
    assert projected["seller_style_examples"] == []
    # D/E: the content and the question it answers travel with it, so GPT ②
    # judges the answer rather than a label.
    assert rows[0]["answer"] == "모델명의 변경 이외의 큰 차이점은 없습니다."
    assert rows[0]["relevance"] == 1.003
    assert rows[0]["matched_subquestion"] == (
        "25년형과 26년형 제품의 차이점이 무엇인가요?"
    )


def test_provenance_survives_in_the_context_for_telemetry() -> None:
    """CONTRACT A: the caller's context keeps the field the prompt lost.

    The dashboard, ``answer_learning_provenance`` and the candidate
    diagnostics all read it, and ``prompt_context`` is documented as taking a
    copy rather than editing in place.
    """

    context = _context(_seller_posted_learning())
    prompt_context(context)
    assert context["similar_approved_answers"][0]["style_only"] is True
    assert context["similar_approved_answers"][0]["authority"] == "AUTO"


def test_origin_labels_other_than_style_only_still_travel() -> None:
    """``evidence_authority`` stays: its legend says it is origin, not permission."""

    rows = prompt_context(_context(_seller_posted_learning()))[
        "similar_approved_answers"
    ]
    assert rows[0]["evidence_authority"] == "SELLER_POSTED"


def test_compatibility_verdict_still_leaves_the_prompt() -> None:
    """The pre-existing drop is unchanged, and the context still has it."""

    context = _context(_seller_posted_learning())
    rows = prompt_context(context)["similar_approved_answers"]
    assert "compatibility" not in rows[0]
    assert (
        context["similar_approved_answers"][0]["compatibility"]["product_match"]
        == "EXACT_MODEL"
    )


# ----------------------------------------------------------------------
# Reverse regression: the drop decides nothing about eligibility
# ----------------------------------------------------------------------


def test_projection_does_not_judge_any_candidate() -> None:
    """§4: the same five shapes project identically whatever style_only says.

    ``prompt_context`` is a field filter, not a gate.  Spelling that out is
    the point: if dropping ``style_only`` had quietly changed which rows are
    carried, a wrong-model fact or an expired promotion could ride along.  The
    gates that reject those live in ``similar_answer_service`` compatibility
    and in the identity/validity SQL, and none of them reads this field.
    """

    wrong_model = _seller_posted_learning(
        learning_example_id=11720,
        answer="화면 반사가 적은 편입니다.",
        compatibility=_same_product_compatibility(
            candidate_product={**LISTING, "model_code": "G5", "category": "MONITOR"},
            product_match="MODEL_MISMATCH",
            eligible=False,
            hard_reject=True,
            reject_reason="MODEL_MISMATCH",
        ),
    )
    unrelated_attribute = _seller_posted_learning(
        learning_example_id=32,
        answer="자동 전원 기능은 지원하지 않습니다.",
        compatibility=_same_product_compatibility(topic_match="MISMATCH"),
    )
    expired_promotion = _seller_posted_learning(
        learning_example_id=901,
        answer="9월 한정 사은품을 드립니다.",
        validity_type="TEMPORARY",
        valid_to="2026-09-30",
    )
    other_customer_order = _seller_posted_learning(
        learning_example_id=902,
        answer="고객님 주문은 9월 12일에 설치 완료되었습니다.",
    )
    not_style_only = _seller_posted_learning(
        learning_example_id=318225,
        style_only=False,
        evidence_authority="APPROVED",
        authority="APPROVED",
        answer="연식별로 배송 일정이 다를 수 있습니다.",
    )

    candidates = [
        wrong_model, unrelated_attribute, expired_promotion,
        other_customer_order, not_style_only,
    ]
    projected = prompt_context(_context(*candidates))
    rows = projected["similar_approved_answers"]

    # Every candidate is carried through unchanged in number and order: the
    # projection removed fields, never rows.
    assert [row["learning_example_id"] for row in rows] == [
        11720, 32, 901, 902, 318225
    ]
    # And every one of them lost exactly the two dropped fields and nothing
    # else -- including the row that was never style_only.
    for row, candidate in zip(rows, candidates):
        assert set(candidate) - set(row) == {"compatibility", "style_only"}
        assert "style_only" not in row

    # The verdicts the real gates produced are still readable on the context,
    # which is what ``_qualifying`` and the dashboard consult.
    context_rows = _context(*candidates)["similar_approved_answers"]
    assert context_rows[0]["compatibility"]["hard_reject"] is True
    assert context_rows[0]["compatibility"]["reject_reason"] == "MODEL_MISMATCH"
    assert context_rows[1]["compatibility"]["topic_match"] == "MISMATCH"
    assert context_rows[2]["validity_type"] == "TEMPORARY"


def test_style_only_costs_the_prompt_nothing() -> None:
    """§8: this change removes a key, so the payload must not grow.

    Measured against the same row with the field already absent rather than
    against the unprojected context -- otherwise the pre-existing
    ``compatibility`` drop supplies the difference and the assertion passes
    whatever happens to ``style_only``.
    """

    with_flag = _seller_posted_learning()
    without_flag = {
        key: value for key, value in with_flag.items() if key != "style_only"
    }

    projected_with = prompt_context(_context(with_flag))
    projected_without = prompt_context(_context(without_flag))

    assert projected_with["similar_approved_answers"] == (
        projected_without["similar_approved_answers"]
    )
    assert len(json.dumps(projected_with, ensure_ascii=False)) == len(
        json.dumps(projected_without, ensure_ascii=False)
    )
