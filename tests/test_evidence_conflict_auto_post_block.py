"""근거가 서로 달라 미해결인 atom 은 자동등록하지 않는다.

운영 DB 감사에서 나온 두 건이 근거다.

  10936  "무게가 얼마나 되나요" -- Product Fact 와 승인된 Learning 이 다른 무게를
         주장했고 검색은 그 atom 을 CONFLICT 로 기록했다. GPT ② 는
         unresolved=[] / can_auto_post=True 로 보고하고 "제품 본체 무게는
         28.4kg입니다" 라고 단정했다. Eligibility 는 사유 0건으로 SAFE 를
         돌려줬고 답변이 등록됐다.
  11162  같은 형태. 조건부 문장이었다는 점만 다르다.

어느 쪽이 맞는지는 답변을 쓴 모델이 스스로 정할 수 없다. 그래서 이 사유는
GPT ② 의 자기보고로 해제되지 않는다.

Validator 쪽도 함께 고친다. GPT ① 이해가 usable 하다는 이유로 모든
REVIEW_REQUIRED 가 PASS 로 바뀌면서 rule 과 signal 은 남아, 기록이 "PASS 인데
검토 신호 있음" 이 됐다 -- 운영 DB 1,123건 중 49건. rule 이 이름을 붙인 findings
는 판정을 유지하고, rule 이 뒷받침하지 않는 신호만 기존처럼 해제한다.
"""
from __future__ import annotations

import pytest

from answer.hybrid_models import ValidationResult, ValidationRuleResult
from services.auto_processing_eligibility_service import (
    EVIDENCE_CONFLICT,
    AutoProcessingEligibilityService,
    _evidence_conflict,
)

ANSWER = "안녕하세요. 제품 본체 무게는 28.4kg입니다.\n감사합니다."
SAFE_ANSWER = "안녕하세요. 벽걸이 설치도 가능합니다.\n감사합니다."

PLAN = {
    "requires_order_lookup": False,
    "requires_dps_lookup": False,
    "order_id_status": "MISSING",
    "order_lookup_status": "NOT_REQUIRED",
    "dps_lookup_status": "NOT_REQUIRED",
    "valid_dps_snapshot_available": False,
    "workflow_block_reasons": [],
    "analysis": {"requires_dps_lookup": False},
}


def metadata(evidence, *, questions=None, route="GPT_DIRECT"):
    return {
        "selected_answer_route": route,
        "processing_plan": dict(PLAN),
        "hybrid": {
            "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
            "subquestion_evidence": list(evidence),
            # GPT ② 가 스스로 해결했다고 보고한 상태. 이 조합이 10936 이다.
            "draft": {"unresolved": [], "can_auto_post": True},
        },
        "semantic_routing": {
            "understanding": {
                "usable": True,
                "questions": questions or [
                    {"text": "무게가 얼마나 되나요", "action": "PRODUCT_SPEC",
                     "requested_attribute": "SPEC_VALUE"},
                ],
            },
        },
    }


def verdict(evidence, *, answer=ANSWER, route="GPT_DIRECT",
            store="OJE_PLUS", questions=None):
    draft = {
        "original_answer": answer,
        "validation_status": "PASS",
        "validator_result_json": {"passed": True, "status": "PASS",
                                  "errors": [], "review_signals": []},
        "review_status": "NONE",
        "metadata_json": metadata(evidence, questions=questions, route=route),
    }
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "store_code": store, "source_answered": 0,
                 "post_status": "PENDING"},
        draft=draft, route=route)


def row(status, source, **extra):
    return {"subquestion": "무게가 얼마나 되나요", "status": status,
            "source": source, **extra}


# ---------------------------------------------------------------------------
# STEP A. 근거 충돌 자동등록 차단
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", [
    # 10936 / 11162 의 실제 source
    "PRODUCT_FACT_VS_LEARNING_CONFLICT",
    # Learning 끼리의 미해결 충돌 (winner 가 정해지지 않은 쌍)
    "CONFLICTING_ACTIVE_LEARNING_NO_RELIABLE_TIME",
    "CONFLICTING_VERIFIED_FEEDBACK_SIGNALS",
    "APPROVED_LEARNING_CONFLICT",
])
def test_01_an_unresolved_conflict_blocks_auto_post(source):
    """미해결 충돌은 출처가 어디든 자동등록을 막는다."""

    result = verdict([row("CONFLICT", source)])
    assert result.decision == "REVIEW_REQUIRED"
    assert EVIDENCE_CONFLICT in result.reasons


def test_02_gpt_self_report_cannot_lift_it():
    """can_auto_post=True / unresolved=[] 로는 해제되지 않는다.

    10936 의 전체 조합이다: 충돌 근거 + 모델이 해결했다는 보고 + 단정 답변.
    """

    draft = {
        "original_answer": ANSWER,
        "validation_status": "PASS",
        "validator_result_json": {"passed": True, "status": "PASS",
                                  "errors": [], "review_signals": []},
        "review_status": "NONE",
        "metadata_json": metadata(
            [row("CONFLICT", "PRODUCT_FACT_VS_LEARNING_CONFLICT",
                 learning_ids=[205, 243749], answer_required=False)]),
    }
    # GPT ② 가 답했다고 보고한 상태를 명시적으로 넣는다.
    draft["metadata_json"]["hybrid"]["draft"] = {
        "unresolved": [], "can_auto_post": True,
        "subquestion_results": [
            {"subquestion": "무게가 얼마나 되나요", "answered": True,
             "status": "ANSWERABLE"},
        ],
    }
    result = AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 10936, "store_code": "OJE_PLUS", "source_answered": 0,
                 "post_status": "PENDING"},
        draft=draft, route="GPT_FALLBACK")
    assert result.decision == "REVIEW_REQUIRED"
    assert EVIDENCE_CONFLICT in result.reasons


@pytest.mark.parametrize("status", ["ANSWERABLE", "CANDIDATE"])
def test_03_a_settled_conflict_keeps_its_safe_verdict(status):
    """권한·최신 승인으로 해소된 충돌은 미해결로 취급하지 않는다.

    해소된 쌍은 ``winner_learning_id`` 를 가지므로
    ``unresolved_learning_conflicts`` 에 들어가지 않고, atom 은
    ANSWERABLE/CANDIDATE 로 남는다. 그 기록이 여기 입력이다.
    """

    result = verdict(
        [row(status, "ACTIVE_POSITIVE_LEARNING", learning_ids=[1])],
        answer=SAFE_ANSWER)
    assert result.decision == "SAFE"
    assert EVIDENCE_CONFLICT not in result.reasons


def test_04_a_conflict_on_another_atom_still_blocks_the_inquiry():
    """복합문의에서 한 atom 이 충돌이면 그 답변은 등록되지 않는다."""

    result = verdict([
        row("ANSWERABLE", "ACTIVE_POSITIVE_LEARNING", learning_ids=[1]),
        {"subquestion": "스피커 내장인가요", "status": "CONFLICT",
         "source": "PRODUCT_FACT_VS_LEARNING_CONFLICT"},
    ])
    assert EVIDENCE_CONFLICT in result.reasons


def test_05_a_candidate_pool_conflict_alone_does_not_block():
    """검색 후보에 충돌 기록이 있어도 atom 판정이 아니면 막지 않는다."""

    result = verdict(
        [row("ANSWERABLE", "ACTIVE_POSITIVE_LEARNING", learning_ids=[1],
             learning_conflicts=[
                 {"left_learning_id": 1, "right_learning_id": 2,
                  "winner_learning_id": 2, "reason": "HIGHER_AUTHORITY"},
             ])],
        answer=SAFE_ANSWER)
    assert result.decision == "SAFE"
    assert EVIDENCE_CONFLICT not in result.reasons


@pytest.mark.parametrize("route", [
    "ORDER_ID_REQUEST", "DELIVERY_WITH_INSTALLATION_DATE",
    "SAFE_RULE", "PRODUCT_DB",
])
def test_06_routes_settled_before_retrieval_are_exempt(route):
    """검색 이전에 답이 정해진 route 는 기존 정책을 유지한다."""

    assert _evidence_conflict(
        metadata([row("CONFLICT", "PRODUCT_FACT_VS_LEARNING_CONFLICT")],
                 route=route),
        route=route,
    ) is False


@pytest.mark.parametrize("store", ["OJE_PLUS", "COUPANG_OJE_NS"])
def test_07_both_marketplaces_are_held(store):
    """네이버·쿠팡 모두 같은 규칙이다 (11179 는 쿠팡 건이었다)."""

    result = verdict(
        [row("CONFLICT", "PRODUCT_FACT_VS_LEARNING_CONFLICT")], store=store)
    assert EVIDENCE_CONFLICT in result.reasons


def test_08_no_evidence_record_is_not_a_conflict():
    """기록이 없는 과거 초안은 건드리지 않는다."""

    assert _evidence_conflict({}, route="GPT_DIRECT") is False
    assert _evidence_conflict(
        {"hybrid": {"answer_pipeline": "X"}}, route="GPT_DIRECT") is False


# ---------------------------------------------------------------------------
# STEP B. Validator 상태 정합성
# ---------------------------------------------------------------------------

def downgrade(validation, *, usable=True):
    """``HybridAnswerService`` 의 강등 조건을 그대로 적용한다."""

    rule_review = any(
        item.status == "REVIEW_REQUIRED" for item in validation.rules
    )
    if usable and validation.status == "REVIEW_REQUIRED" and not rule_review:
        return ValidationResult(
            passed=True, errors=validation.errors,
            warnings=validation.warnings,
            checked_facts=validation.checked_facts, status="PASS",
            rules=validation.rules, review_signals=(),
        )
    return validation


CONFLICT_SIGNAL = (
    "동일 범위/주제에서 서로 충돌하는 Verified Fact/Correction이 있어 "
    "직원 확인이 필요합니다: 무게가 얼마나 되나요"
)
LIFTED_SIGNAL = "Rule 정책의 직원 검토 요구를 해제했습니다."


def test_09_a_rule_backed_review_keeps_its_verdict():
    """VERIFIED_FACT_CONFLICT 는 PASS 로 바뀌지 않는다 (44건 유형)."""

    validation = ValidationResult(
        passed=True, warnings=(CONFLICT_SIGNAL,), status="REVIEW_REQUIRED",
        rules=(ValidationRuleResult(
            "VERIFIED_FACT_CONFLICT", "REVIEW_REQUIRED", CONFLICT_SIGNAL),),
        review_signals=(CONFLICT_SIGNAL,),
    )
    result = downgrade(validation)
    assert result.status == "REVIEW_REQUIRED"
    assert result.review_signals == (CONFLICT_SIGNAL,)
    assert CONFLICT_SIGNAL in result.warnings


def test_10_a_signal_no_rule_stands_behind_is_still_lifted():
    """정상 해제 5건은 그대로 PASS 가 된다."""

    validation = ValidationResult(
        passed=True, warnings=(LIFTED_SIGNAL,), status="REVIEW_REQUIRED",
        rules=(ValidationRuleResult("FACT_GROUNDING", "PASS", "ok"),),
        review_signals=(LIFTED_SIGNAL,),
    )
    result = downgrade(validation)
    assert result.status == "PASS"
    # 해제한 뒤에는 미해결 검토 요구가 남지 않는다 -- 정보는 warnings 에 있다.
    assert result.review_signals == ()
    assert LIFTED_SIGNAL in result.warnings


def test_11_the_record_is_never_pass_with_an_open_review_signal():
    """저장되는 status 와 review_signals 가 서로 모순되지 않는다."""

    for validation in (
        ValidationResult(
            passed=True, status="REVIEW_REQUIRED",
            warnings=(CONFLICT_SIGNAL,),
            rules=(ValidationRuleResult(
                "VERIFIED_FACT_CONFLICT", "REVIEW_REQUIRED",
                CONFLICT_SIGNAL),),
            review_signals=(CONFLICT_SIGNAL,)),
        ValidationResult(
            passed=True, status="REVIEW_REQUIRED",
            warnings=(LIFTED_SIGNAL,), rules=(),
            review_signals=(LIFTED_SIGNAL,)),
    ):
        result = downgrade(validation)
        assert not (result.status == "PASS" and result.review_signals)


def test_12_an_unusable_understanding_changes_nothing():
    """GPT ① 이 없으면 종전처럼 REVIEW_REQUIRED 가 유지된다."""

    validation = ValidationResult(
        passed=True, status="REVIEW_REQUIRED", rules=(),
        review_signals=(LIFTED_SIGNAL,))
    assert downgrade(validation, usable=False).status == "REVIEW_REQUIRED"
