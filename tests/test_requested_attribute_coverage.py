"""The property that was asked, not just the subject that was mentioned.

Every case here is a measured one. The two harmful auto-posts come from the
cached corpus run; the answers that must keep working come from the thirteen
correct auto-answers in the same run, eight of which rest on a single piece of
evidence. A gate that blocks the first two by refusing single-evidence answers
would take those eight with it, so both directions are asserted.
"""
from __future__ import annotations

import pytest

from services.requested_attribute_coverage import (
    ACTION_NOT_PERFORMED,
    ATTRIBUTE_MISMATCH,
    ATTRIBUTE_UNKNOWN,
    COVERED,
    all_covered,
    evaluate,
    evaluate_atoms,
    uncovered_reasons,
)
from services.semantic_analysis import (
    REQUESTED_ATTRIBUTES,
    UNKNOWN_ATTRIBUTE,
    AtomicQuestion,
    parse,
)


def _semantic(**overrides):
    payload = {
        "primary_action": "OTHER",
        "secondary_actions": [],
        "request_type": "QUESTION",
        "objects": [],
        "atomic_questions": [],
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": False,
        "requires_delivery_schedule": False,
        "purchase_state": "UNKNOWN",
        "asks_delivery_schedule": False,
        "asks_delivery_outcome": False,
        "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------- 계약 (additive)
def test_atomic_question_defaults_to_unknown_attribute():
    """An older model response has no such field and must still parse."""
    question = AtomicQuestion(text="질문", action="OTHER")
    assert question.requested_attribute == UNKNOWN_ATTRIBUTE
    assert question.to_dict()["requested_attribute"] == UNKNOWN_ATTRIBUTE


def test_parse_accepts_response_without_the_new_field():
    analysis = parse(_semantic(atomic_questions=[
        {"text": "오토 피벗이 되나요?", "action": "OTHER",
         "requested_information": "오토 피벗 지원 여부"}]))
    assert analysis.atomic_questions[0].requested_attribute == UNKNOWN_ATTRIBUTE


def test_parse_reads_the_attribute_when_present():
    analysis = parse(_semantic(atomic_questions=[
        {"text": "비용은 누가 내나요?", "action": "OTHER",
         "requested_information": "비용 부담 주체",
         "requested_attribute": "ACTOR"}]))
    assert analysis.atomic_questions[0].requested_attribute == "ACTOR"


def test_parse_downgrades_an_unrecognised_attribute_instead_of_raising():
    """An invented label must not become a licence, and must not break parsing."""
    analysis = parse(_semantic(atomic_questions=[
        {"text": "질문", "action": "OTHER", "requested_attribute": "WHO_PAYS"}]))
    assert analysis.atomic_questions[0].requested_attribute == UNKNOWN_ATTRIBUTE


def test_unknown_is_part_of_the_closed_vocabulary():
    assert UNKNOWN_ATTRIBUTE in REQUESTED_ATTRIBUTES


# --------------------------------------------------------------- 실측 harmful 2건
def test_cost_existence_does_not_answer_who_bears_the_cost():
    """CM18: "사다리차는 유상" answered "비용은 누가 내나요"."""
    coverage = evaluate("ACTOR", [
        {"id": 317203, "establishes_attribute": "EXISTENCE_OR_CAPABILITY"}])
    assert coverage.covered is False
    assert coverage.reason == ATTRIBUTE_MISMATCH
    assert coverage.mismatched_evidence == (317203,)


def test_installation_method_does_not_answer_whether_it_may_be_declined():
    """CM01: "기사님이 배송 후 설치합니다" answered "안 부르고 받아만 볼 수 있나요"."""
    coverage = evaluate("PERMISSION_OR_OPTION", [
        {"id": 154681, "establishes_attribute": "METHOD_OR_PROCEDURE"}])
    assert coverage.covered is False
    assert coverage.reason == ATTRIBUTE_MISMATCH


# --------------------------------------------------------------- 정상 답변 보존
@pytest.mark.parametrize("attribute, evidence_id", [
    ("EXISTENCE_OR_CAPABILITY", 158169),   # 오토 피벗이 되는 모델인가요
    ("DIFFERENCE", 291312),                # BEF랑 BEH가 어떻게 다른가요
    ("LOCATION_OR_CONTACT", 154704),       # 핀번호 어디에 물어보나요
    ("METHOD_OR_PROCEDURE", 62543),        # 무엇을 연결하면 유튜브를 보나요
    ("INCLUSION", 154567),                 # 스탠드 같이 배송 오나요
])
def test_a_single_matching_evidence_still_answers(attribute, evidence_id):
    """Eight of the thirteen correct auto-answers rest on one piece of evidence."""
    coverage = evaluate(attribute, [
        {"id": evidence_id, "establishes_attribute": attribute}])
    assert coverage.covered is True
    assert coverage.reason == COVERED
    assert coverage.covering_evidence == (evidence_id,)


def test_one_matching_evidence_among_several_mismatches_is_enough():
    coverage = evaluate("ACTOR", [
        {"id": 1, "establishes_attribute": "TIMING"},
        {"id": 2, "establishes_attribute": "ACTOR"},
        {"id": 3, "establishes_attribute": "EXISTENCE_OR_CAPABILITY"}])
    assert coverage.covered is True
    assert coverage.covering_evidence == (2,)
    assert coverage.mismatched_evidence == (1, 3)


# --------------------------------------------------------------- 행위 대행
def test_a_request_to_act_is_never_settled_by_stored_procedure():
    """CM26 / V4Q126: quoting how it is done is not doing it."""
    coverage = evaluate("ACTION_EXECUTION", [
        {"id": 167845, "establishes_attribute": "METHOD_OR_PROCEDURE"},
        {"id": 154563, "establishes_attribute": "METHOD_OR_PROCEDURE"}])
    assert coverage.covered is False
    assert coverage.reason == ACTION_NOT_PERFORMED


# --------------------------------------------------------------- UNKNOWN 안전측
def test_an_unknown_asked_attribute_never_licenses_an_answer():
    coverage = evaluate(UNKNOWN_ATTRIBUTE, [
        {"id": 1, "establishes_attribute": "EXISTENCE_OR_CAPABILITY"}])
    assert coverage.covered is False
    assert coverage.reason == ATTRIBUTE_UNKNOWN


def test_evidence_with_an_unknown_attribute_does_not_cover():
    coverage = evaluate("ACTOR", [{"id": 1, "establishes_attribute": ""}])
    assert coverage.covered is False


def test_no_evidence_at_all_is_not_coverage():
    assert evaluate("ACTOR", []).covered is False


# --------------------------------------------------------------- compound
def test_a_compound_inquiry_is_not_answered_when_one_atom_is_unsettled():
    coverage = evaluate_atoms([
        {"atom_id": "a1", "requested_attribute": "EXISTENCE_OR_CAPABILITY",
         "evidence": [{"id": 1, "establishes_attribute": "EXISTENCE_OR_CAPABILITY"}]},
        {"atom_id": "a2", "requested_attribute": "ACTOR",
         "evidence": [{"id": 2, "establishes_attribute": "EXISTENCE_OR_CAPABILITY"}]}])
    assert coverage["a1"].covered is True
    assert coverage["a2"].covered is False
    assert all_covered(coverage) is False
    assert uncovered_reasons(coverage) == {"a2": ATTRIBUTE_MISMATCH}


def test_separate_evidence_may_settle_separate_atoms():
    coverage = evaluate_atoms([
        {"atom_id": "a1", "requested_attribute": "AMOUNT_OR_COST",
         "evidence": [{"id": 1, "establishes_attribute": "AMOUNT_OR_COST"}]},
        {"atom_id": "a2", "requested_attribute": "ACTOR",
         "evidence": [{"id": 2, "establishes_attribute": "ACTOR"}]}])
    assert all_covered(coverage) is True


def test_a_template_resolved_atom_keeps_its_precedence():
    """Exact Template and RULE decide first and do not need Learning evidence."""
    coverage = evaluate_atoms([
        {"atom_id": "a1", "resolved_by_template": True,
         "requested_attribute": "METHOD_OR_PROCEDURE"},
        {"atom_id": "a2", "requested_attribute": "ACTOR",
         "evidence": [{"id": 2, "establishes_attribute": "ACTOR"}]}])
    assert coverage["a1"].covered is True
    assert coverage["a1"].reason == "RESOLVED_BY_TEMPLATE"
    assert all_covered(coverage) is True


def test_an_empty_inquiry_is_not_covered():
    assert all_covered(evaluate_atoms([])) is False


# --------------------------------------------------------------- 과분해 방지
def test_one_subject_stays_one_atom():
    """The fix is a property on the question, not more questions.

    "사다리차가 필요하면 비용은 누가 내나요" is one atom asking one property.
    Splitting it into cost-exists and cost-payer would send inquiries to review
    that a single stored answer settles.
    """
    analysis = parse(_semantic(atomic_questions=[
        {"text": "사다리차가 필요하면 비용은 누가 내나요?", "action": "OTHER",
         "requested_information": "사다리차 비용 부담 주체",
         "requested_attribute": "ACTOR"}]))
    assert len(analysis.atomic_questions) == 1
    assert analysis.atomic_questions[0].requested_attribute == "ACTOR"
