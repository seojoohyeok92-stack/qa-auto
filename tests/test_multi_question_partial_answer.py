"""여러 질문 중 근거가 없는 질문은 빼고, 답할 수 있는 질문만으로 자동등록한다.

26.11.12 서버 사본의 다중질문 초안 101건이 GPT② 의 unresolved 보고 하나로만
보류됐다. 그중 43건은 다른 질문에 답했고, 남은 질문은 모두 근거 없음
(NO_RELIABLE_SOURCE) 이었다. 답변은 그 질문을 "확인이 어렵습니다" 로 적었다.

이제 그 질문은 답변에서 완전히 빠지고(omitted_unsupported) 보류 사유가 되지
않는다. 단일 질문, 답한 질문이 없는 경우, CONFLICT·NEEDS_DPS·
DELIVERY_SCHEDULE_REVIEW, 답변에 보류·확인 문구가 남은 경우는 지금처럼 보류한다.
"""
from __future__ import annotations

import pytest

from services.auto_processing_eligibility_service import (
    GPT_REPORTED_UNRESOLVED,
    GPT_WITHHELD_AUTO_POST,
    _evidence_insufficient,
)
from services.draft_generation_service import DraftGenerationService
from test_auto_post_gate_server_cases import _draft, _evaluate

Q1, Q2, Q3 = "벽걸이 설치 되나요?", "리모컨 포함인가요?", "저장 용량은 어떻게 되나요?"
ANSWER = "벽걸이 설치가 가능하며, 리모컨은 기본 구성품에 포함되어 있습니다."


def result(text, *, answered):
    return {
        "subquestion": text,
        "answered": answered,
        "status": "ANSWERABLE" if answered else "NO_RELIABLE_SOURCE",
    }


def resolve(results, *, answer=ANSWER, retrieval=None):
    raw = {
        "answer": answer,
        "requires_review": False,
        "can_auto_post": True,
        "missing_information": [],
        "unresolved": [],
        "subquestion_results": results,
    }
    # GPT ① 이 분리한 질문마다 retrieval 이 한 번 돈다. 기본값은 GPT ② 가 보고한
    # 질문을 모두 GPT ① 도 분리한, 운영 경로의 정상 형태다.
    if retrieval is None:
        retrieval = {item["subquestion"]: "CANDIDATE" for item in results}
    context = {"subquestion_evidence": [
        {"subquestion": text, "status": status}
        for text, status in retrieval.items()
    ]}
    return DraftGenerationService._apply_reported_resolution(raw, context)


def verdict(generated):
    draft = _draft()
    draft["metadata_json"]["hybrid"]["draft"] = generated
    return _evaluate(draft=draft)


# ------------------------------------------------------------- 요청된 사례
@pytest.mark.parametrize("answered", [(True, True, False), (True, False, False)])
def test_some_answerable_questions_are_published_alone(answered):
    texts = (Q1, Q2, Q3)
    generated = resolve([result(t, answered=a) for t, a in zip(texts, answered)])

    left_out = sorted(t for t, a in zip(texts, answered) if not a)
    assert generated["omitted_unsupported"] == left_out
    assert generated["unresolved"] == []
    assert generated["can_auto_post"] is True
    assert verdict(generated).decision == "SAFE"


def test_every_question_answerable_is_unchanged():
    generated = resolve([result(Q1, answered=True), result(Q2, answered=True)])

    assert generated["omitted_unsupported"] == []
    assert verdict(generated).decision == "SAFE"


def test_no_answerable_question_is_still_held():
    generated = resolve(
        [result(Q1, answered=False), result(Q3, answered=False)], answer="")

    assert generated["omitted_unsupported"] == []
    assert set(generated["unresolved"]) == {Q1, Q3}
    held = verdict(generated)
    assert held.decision == "REVIEW_REQUIRED"
    assert {GPT_REPORTED_UNRESOLVED, GPT_WITHHELD_AUTO_POST} <= set(held.reasons)


def test_a_single_question_is_unchanged():
    generated = resolve([result(Q3, answered=False)], answer="")

    assert generated["omitted_unsupported"] == []
    assert generated["unresolved"] == [Q3]
    assert verdict(generated).decision == "REVIEW_REQUIRED"


# ------------------------------------------------------------ 지금처럼 보류
@pytest.mark.parametrize("answer", [
    ANSWER + " 저장 용량은 확인이 필요합니다.",
    ANSWER + " 저장 용량은 확인 후 안내드리겠습니다.",
    ANSWER + " 저장 용량은 확인된 상품 사양에 없어 정확한 안내가 어렵습니다.",   # 11392
    ANSWER + " 정식 제품인지는 제공된 정보만으로 확인되지 않습니다.",             # 11039
    ANSWER + " 거치대 종류는 현재 근거만으로 특정하기 어렵습니다.",               # 10896
])
def test_an_answer_that_still_writes_up_the_gap_is_held(answer):
    generated = resolve(
        [result(Q1, answered=True), result(Q3, answered=False)], answer=answer)

    assert generated["omitted_unsupported"] == []
    assert generated["unresolved"] == [Q3]
    assert verdict(generated).decision == "REVIEW_REQUIRED"


@pytest.mark.parametrize("status", ["CONFLICT", "NEEDS_DPS", "DELIVERY_SCHEDULE_REVIEW"])
def test_a_question_with_its_own_review_path_is_never_left_out(status):
    generated = resolve(
        [result(Q1, answered=True), result(Q3, answered=False)],
        retrieval={Q1: "CANDIDATE", Q3: status})

    assert generated["omitted_unsupported"] == []
    assert generated["can_auto_post"] is False


def test_gpt2_reporting_a_conflict_is_not_left_out():
    generated = resolve([
        result(Q1, answered=True),
        {"subquestion": Q3, "answered": False, "status": "CONFLICT"},
    ])

    assert generated["omitted_unsupported"] == []
    assert generated["unresolved"] == [Q3]


def test_the_operational_commitment_hold_is_unchanged():
    raw = {
        "answer": ANSWER, "requires_review": True, "can_auto_post": False,
        "missing_information": ["현재 상품의 삼성 전용 8% 혜택 대상 여부"],
        "unresolved": [],
        "subquestion_results": [result(Q1, answered=True), result(Q3, answered=False)],
    }
    generated = DraftGenerationService._apply_reported_resolution(
        raw, {"subquestion_evidence": [{"subquestion": Q1}, {"subquestion": Q3}]})

    assert generated["omitted_unsupported"] == [Q3]
    assert generated["can_auto_post"] is False


# -------------------------------------------------------- 파싱·리드타임 게이트
def test_the_parsed_draft_carries_the_omitted_questions():
    draft = DraftGenerationService.parse({
        **resolve([result(Q1, answered=True), result(Q3, answered=False)]),
        "confidence": 0.9,
    })

    assert draft.omitted_unsupported == (Q3,)
    assert draft.unresolved == ()
    assert draft.can_auto_post is True
    assert draft.to_dict()["omitted_unsupported"] == [Q3]


LEAD_TIME = "설치까지 보통 며칠 걸리나요?"


def lead_time_metadata(omitted):
    return {
        "semantic_routing": {"understanding": {"questions": [
            {"text": Q1, "action": "PRODUCT_SPEC", "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
            {"text": LEAD_TIME, "action": "INSTALLATION_SCHEDULE", "requested_attribute": "TIMING"},
        ]}},
        "hybrid": {
            "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
            "subquestion_evidence": [
                {"subquestion": Q1, "status": "CANDIDATE"},
                {"subquestion": LEAD_TIME, "status": "NO_RELIABLE_SOURCE"},
            ],
            "draft": {"omitted_unsupported": omitted},
        },
    }


def test_a_lead_time_left_out_of_the_answer_is_not_insufficient_evidence():
    assert _evidence_insufficient(lead_time_metadata([LEAD_TIME]), route="GPT_FALLBACK") is False
    # 답변에 남아 있는 리드타임은 지금처럼 근거 부족으로 보류한다.
    assert _evidence_insufficient(lead_time_metadata([]), route="GPT_FALLBACK") is True


def test_gpt2_is_told_to_leave_unsupported_questions_out():
    from answer.prompt_builder import PromptBuilder

    rules = " ".join(PromptBuilder.EVIDENCE_JUDGEMENT_RULES)
    assert "answer 에서 완전히 뺀다" in rules
    assert "단일 질문이거나" in rules


# ------------------------------------- 실GPT 재생 11667·11496 (26.11.12 사본)
def test_a_question_gpt1_dropped_keeps_the_answer_held():
    """11496: GPT ① 이 '80cm 서랍장 위 43인치' 질문을 빠뜨렸다. GPT ② 가 원문
    그대로 unresolved 에 올리면 생략이 아니라 보류다."""

    dropped = "80cm 서랍장 위에 올려도 되나요?"
    raw = {
        "answer": "스탠드 다리 간격은 약 75cm가 맞습니다.",
        "requires_review": False, "can_auto_post": True,
        "missing_information": [], "unresolved": [dropped],
        "subquestion_results": [result("스탠드 다리 간격 75cm 맞나요?", answered=True)],
    }
    generated = DraftGenerationService._apply_reported_resolution(raw, {})

    assert generated["omitted_unsupported"] == []
    assert generated["unresolved"] == [dropped]
    assert verdict(generated).decision == "REVIEW_REQUIRED"


def test_a_dropped_question_beside_an_omitted_one_is_still_held():
    raw = {
        "answer": ANSWER, "requires_review": False, "can_auto_post": True,
        "missing_information": [], "unresolved": ["원문에만 있는 질문"],
        "subquestion_results": [result(Q1, answered=True), result(Q3, answered=False)],
    }
    generated = DraftGenerationService._apply_reported_resolution(raw, {})

    assert generated["omitted_unsupported"] == []
    assert set(generated["unresolved"]) == {"원문에만 있는 질문", Q3}


def test_gpt2_is_told_not_to_guess_and_to_report_dropped_questions():
    from answer.prompt_builder import PromptBuilder

    rules = " ".join(PromptBuilder.EVIDENCE_JUDGEMENT_RULES)
    # 11496 서랍장, 11667 셋톱박스: 추측 단정 대신 생략.
    assert "추측해서 답하는 것의 대안이 아니다" in rules
    assert "기사가 해 주는 작업과 해 주지 않는 작업" in rules
    # 11496: GPT ① 이 빠뜨린 원문 질문.
    assert "customer_inquiry 원문에 있는 질문이 atomic_questions 에 없거나" in rules
    # 11667: 다른 상품 Learning 으로 새 상품 단정.
    assert "정품·새 상품·유통 경로" in rules


# ------------------------------- 실GPT 재생 11691 (26.11.13): GPT ① 누락 질문
INSTALL_FEE = "기사님 설치가 11만원이 추가되는 것 같아서요"


def test_a_question_gpt1_dropped_but_gpt2_called_unsupported_is_held():
    """11691: GPT ① 은 설치비 질문을 분리하지 않았고, GPT ② 는 그것을 원문에서
    찾아 unresolved 가 아니라 NO_RELIABLE_SOURCE 로 보고했다. 생략이 아니라 보류다."""

    generated = resolve(
        [result(Q1, answered=True), result(Q3, answered=False),
         result(INSTALL_FEE, answered=False)],
        retrieval={Q1: "CANDIDATE", Q3: "CANDIDATE"})

    assert generated["omitted_unsupported"] == []
    assert set(generated["unresolved"]) == {Q3, INSTALL_FEE}
    assert generated["can_auto_post"] is False
    assert verdict(generated).decision == "REVIEW_REQUIRED"


def test_without_gpt1s_split_on_record_nothing_is_left_out():
    generated = resolve(
        [result(Q1, answered=True), result(Q3, answered=False)], retrieval={})

    assert generated["omitted_unsupported"] == []
    assert generated["unresolved"] == [Q3]
    assert verdict(generated).decision == "REVIEW_REQUIRED"
