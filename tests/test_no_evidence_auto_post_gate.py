"""근거가 없는 답변은, 그 사실을 잘 쓴 문장이어도 게시되지 않는다.

측정된 경로는 이랬다. 네 가지를 묻는 문의에 검색이 아무것도 찾지 못했고,
generation 은 "현재 확인 가능한 정보가 없어 각각 확인 후 안내가 필요합니다"
라고 네 주제를 모두 이름 붙여 답했다. coverage 는 물어본 주제가 답변에 전부
등장하므로 PASS 를 냈고, 다른 gate 를 뺀 상태에서 eligibility 는 이유 없이
SAFE 를 돌려주었다 -- 즉 자동등록 가능이었다.

실제 파이프라인에서는 validator 와 processing plan 이 함께 걸려 Review 가
되었지만, 그것은 원칙이 아니라 우연이다. 안전이 다른 gate 의 동시 발동에
의존하고 있었다.

coverage 는 "답변이 질문을 다뤘는가" 를 묻고, 이 gate 는 "그 답을 받쳐주는
근거가 있는가" 를 묻는다. 두 질문은 다르고, 갈라진 지점이 위 경로였다.
"""
from __future__ import annotations

import pytest

from services.auto_processing_eligibility_service import (
    EVIDENCE_NOT_SUFFICIENT,
    AutoProcessingEligibilityService,
    _evidence_insufficient,
)


DEFERRAL_ANSWER = (
    "문의하신 25년형·26년형의 차이, 해당 상품의 제조일, KT 이용 환경에서 "
    "넷플릭스·유튜브 시청 가능 여부, 설치 후 반품 가능 여부는 현재 확인 "
    "가능한 정보가 없어 각각 확인 후 안내가 필요합니다."
)
GROUNDED_ANSWER = "기사님이 방문하여 설치해드리며 폐가전 수거도 함께 진행됩니다."


def evidence(*statuses: str) -> list[dict]:
    return [
        {"subquestion": "질문 %d" % index, "status": status,
         "source": None if status == "NO_RELIABLE_SOURCE" else "ACTIVE_POSITIVE_LEARNING",
         "learning_ids": [] if status == "NO_RELIABLE_SOURCE" else [100 + index],
         "historical_case_ids": []}
        for index, status in enumerate(statuses, start=1)
    ]


def verdict_for(*, route: str, entries, answer: str = DEFERRAL_ANSWER,
                coverage: str = "PASS", extra: dict | None = None):
    metadata = {
        "selected_answer_route": route,
        "semantic_coverage": {"status": coverage},
        "hybrid": {"subquestion_evidence": entries,
                   "draft": {"learning_usage": [], "requires_review": False,
                             "missing_information": []},
                   "self_review": {"requires_review": False}},
    }
    metadata.update(extra or {})
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={"metadata_json": metadata, "original_answer": answer,
               "validation_status": "PASS"},
        route=route,
    )


# ================================================== A. 근거 0 + deferral 문장
def test_a_factual_question_with_no_source_cannot_auto_post():
    """측정된 경로 그 자체. 네 질문 모두 근거가 없었다."""
    result = verdict_for(
        route="GPT_FALLBACK",
        entries=evidence(*(["NO_RELIABLE_SOURCE"] * 4)))

    assert EVIDENCE_NOT_SUFFICIENT in result.reasons
    assert result.decision != "SAFE"


def test_naming_the_gap_fluently_does_not_substitute_for_evidence():
    """답변이 주제를 전부 이름 붙였다는 사실은 근거가 아니다."""
    held = verdict_for(route="GPT_FALLBACK",
                       entries=evidence("NO_RELIABLE_SOURCE"))
    assert held.decision != "SAFE"


# ============================================ B~D. 근거가 있는 경로는 그대로
def test_a_verified_learning_answer_still_publishes():
    result = verdict_for(route="GPT_HYBRID", entries=evidence("ANSWERABLE"),
                         answer=GROUNDED_ANSWER)
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


@pytest.mark.parametrize("route", ["TEMPLATE", "SAFE_RULE", "PRODUCT_DB"])
def test_a_deterministic_route_is_never_held_by_this_gate(route):
    """Template/RULE/Catalog 의 답은 검색보다 먼저 정해진다.

    그 경로에서 비어 있는 ``subquestion_evidence`` 는 근거의 부재가 아니라
    검색이 관여하지 않았다는 뜻이다.
    """
    result = verdict_for(route=route,
                         entries=evidence(*(["NO_RELIABLE_SOURCE"] * 2)))
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


# =========================================== E. compound 부분 근거는 차단
def test_two_supported_and_one_unsupported_still_blocks():
    """근거 있는 둘이 근거 없는 하나를 데리고 나가지 못한다."""
    result = verdict_for(
        route="GPT_HYBRID",
        entries=evidence("ANSWERABLE", "ANSWERABLE", "NO_RELIABLE_SOURCE"))

    assert EVIDENCE_NOT_SUFFICIENT in result.reasons
    assert result.decision != "SAFE"


def test_every_atom_supported_is_not_held():
    result = verdict_for(
        route="GPT_HYBRID",
        entries=evidence("ANSWERABLE", "ANSWERABLE", "ANSWERABLE"),
        answer=GROUNDED_ANSWER)
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


# ======================================== F~G. 기존에 허용된 경로는 보존
def test_the_safe_order_number_request_is_untouched():
    """주문번호를 물어보는 답변은 어떤 사실도 주장하지 않는다."""
    result = verdict_for(route="ORDER_ID_REQUEST",
                         entries=evidence("NO_RELIABLE_SOURCE"))
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons


@pytest.mark.parametrize("status", ["NEEDS_DPS", "DELIVERY_SCHEDULE_REVIEW",
                                    "CONFLICT"])
def test_statuses_with_their_own_review_path_are_not_re_judged(status):
    """이미 자기 검토 경로를 가진 상태를 여기서 다시 판정하지 않는다."""
    assert _evidence_insufficient(
        {"hybrid": {"subquestion_evidence": evidence(status)}},
        route="GPT_HYBRID") is False


# ==================================================== 기록 부재는 무영향
def test_a_draft_with_no_evidence_record_is_untouched():
    """이 단계가 없던 시절의 draft 는 그대로 게시된다."""
    assert _evidence_insufficient({}, route="GPT_HYBRID") is False
    assert _evidence_insufficient(
        {"hybrid": {"subquestion_evidence": []}}, route="GPT_HYBRID") is False


def test_an_unreadable_record_is_untouched():
    assert _evidence_insufficient(
        {"hybrid": "not a mapping"}, route="GPT_HYBRID") is False


# ============================================ H~I. 배송 정책은 기존 그대로
def test_the_gate_adds_nothing_to_an_already_reviewed_draft():
    """PRE_PURCHASE/AMBIGUOUS 는 이미 Review 이므로 이 gate 가 바꾸는 것이 없다."""
    result = verdict_for(
        route="GPT_HYBRID",
        entries=evidence("DELIVERY_SCHEDULE_REVIEW"),
        extra={"requires_manual_review": True})
    assert result.decision != "SAFE"
    assert EVIDENCE_NOT_SUFFICIENT not in result.reasons
