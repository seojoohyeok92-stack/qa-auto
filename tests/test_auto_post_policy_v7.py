"""Acceptance A-R for the auto-post-by-default policy change.

Operating philosophy under test: auto-post by default, hard-block only on an
actual risk of customer harm. These tests pin BOTH directions -- the newly
allowed soft cases *and* the hard blocks that must survive the relaxation.

No real Naver POST, no real DPS lookup: eligibility is exercised directly
against fixture drafts/plans, which is the exact structure the pipeline
passes in.
"""
from __future__ import annotations

from services.auto_processing_eligibility_service import (
    SOFT_REASONS,
    AutoProcessingEligibilityService,
)


SERVICE = AutoProcessingEligibilityService()

SAFE_GENERAL_ANSWER = (
    "빠른 배송 요청 내용 확인했습니다. 가능한 일정에 맞춰 진행될 수 있도록 "
    "요청하겠습니다."
)


def _inquiry(**overrides):
    base = {"source_answered": 0, "post_status": "NOT_POSTED"}
    base.update(overrides)
    return base


def _draft(
    *,
    answer: str = SAFE_GENERAL_ANSWER,
    validation_status: str = "PASSED",
    validator_result=None,
    review_status: str = "",
    plan=None,
    analysis=None,
    hybrid=None,
    product_fact_guard=None,
    requires_manual_review: bool = False,
):
    metadata = {"requires_manual_review": requires_manual_review}
    if plan is not None:
        metadata["processing_plan"] = plan
    if analysis is not None:
        metadata.setdefault("processing_plan", {})["analysis"] = analysis
    if hybrid is not None:
        metadata["hybrid"] = hybrid
    if product_fact_guard is not None:
        metadata["product_fact_guard"] = product_fact_guard
    return {
        "original_answer": answer,
        "validation_status": validation_status,
        "validator_result_json": validator_result,
        "review_status": review_status,
        "metadata_json": metadata,
        "posted": False,
        "id": 1,
    }


def _evaluate(draft, *, route="GPT_DIRECT", inquiry=None):
    return SERVICE.evaluate(
        inquiry=inquiry or _inquiry(), draft=draft, route=route,
    )


# CASE A -- a general "please ship quickly" reply asserts no date fact.
def test_case_a_general_fast_shipping_request_is_auto_postable():
    result = _evaluate(_draft())
    assert result.safe is True
    assert result.reasons == ()


# CASE B -- general product question with no Learning behind it.
def test_case_b_general_answer_without_learning_is_auto_postable():
    result = _evaluate(
        _draft(hybrid={"subquestion_evidence": []}), route="GPT_FALLBACK",
    )
    assert result.safe is True


# CASE C -- weak Learning similarity, no fact assertion -> still auto-postable.
def test_case_c_low_similarity_without_fact_claim_is_auto_postable():
    result = _evaluate(
        _draft(
            hybrid={
                "draft": {"confidence": 0.42},
                "subquestion_evidence": [
                    {
                        "subquestion": "빠른 배송 가능한가요?",
                        "status": "ANSWERABLE",
                        "evidence_coverage": "PARTIALLY_SUPPORTED",
                    }
                ],
            }
        )
    )
    assert result.safe is True
    # The low confidence is still recorded, just not blocking.
    assert "GPT_CONFIDENCE_LOW" in result.soft_reasons


# CASE D -- a soft validator/provider warning must not block, but is kept.
def test_case_d_soft_warning_is_recorded_but_not_blocking():
    result = _evaluate(
        _draft(
            analysis={"confidence": 0.5},
            hybrid={"draft": {"confidence": 0.55}},
        )
    )
    assert result.safe is True
    assert set(result.soft_reasons) <= SOFT_REASONS
    assert result.soft_reasons  # preserved for the operator


# CASE E -- confirmed current DPS date -> auto-postable.
def test_case_e_confirmed_dps_date_is_auto_postable():
    result = _evaluate(
        _draft(
            plan={
                "requires_dps_lookup": True,
                "dps_lookup_status": "SUCCESS",
                "valid_dps_snapshot_available": True,
            }
        ),
        route="DELIVERY_WITH_INSTALLATION_DATE",
    )
    assert result.safe is True


# CASE F -- DPS PARTIAL must stay blocked.
def test_case_f_dps_partial_stays_blocked():
    result = _evaluate(
        _draft(
            plan={
                "requires_dps_lookup": True,
                "dps_lookup_status": "PARTIAL",
                "valid_dps_snapshot_available": False,
            }
        ),
        route="DELIVERY_WITH_INSTALLATION_DATE",
    )
    assert result.safe is False
    assert "DPS_RESULT_NOT_TRUSTED" in result.reasons
    assert "DPS_SNAPSHOT_NOT_VALIDATED" in result.reasons


# CASE G -- DPS lookup failure must stay blocked.
def test_case_g_dps_lookup_failure_stays_blocked():
    result = _evaluate(
        _draft(
            plan={
                "requires_dps_lookup": True,
                "dps_lookup_status": "FAILED",
                "valid_dps_snapshot_available": False,
            }
        ),
        route="DPS_LOOKUP_FAILED",
    )
    assert result.safe is False


# CASE H -- an unverified sensitive product fact must stay blocked.
def test_case_h_unverified_product_fact_stays_blocked():
    result = _evaluate(
        _draft(
            product_fact_guard={
                "sensitive": True, "current_fact_verified": False,
            }
        ),
        route="PRODUCT_DB",
    )
    assert result.safe is False
    assert "PRODUCT_FACT_NOT_VERIFIED" in result.reasons


# CASE I -- missing order id + order lookup required -> the safe
# "send us your order number" reply is itself auto-postable.
def test_case_i_order_id_request_reply_is_auto_postable():
    result = _evaluate(
        _draft(
            answer="주문번호를 알려주시면 확인해 드리겠습니다.",
            plan={
                "requires_order_lookup": True,
                "order_id_status": "MISSING",
                "order_lookup_status": "NOT_RUN",
            },
        ),
        route="ORDER_ID_REQUEST",
    )
    assert result.safe is True
    assert "ORDER_ID_REQUESTED_FROM_CUSTOMER" in result.soft_reasons


# CASE J -- no order id but no order lookup needed -> normal auto-post.
def test_case_j_no_order_id_and_no_lookup_needed_is_auto_postable():
    result = _evaluate(
        _draft(plan={"requires_order_lookup": False})
    )
    assert result.safe is True
    assert result.reasons == ()


# CASE J2 -- an order fact IS claimed but the lookup was never trusted, on a
# route that is not the safe request reply -> must stay blocked.
def test_case_j2_untrusted_order_lookup_on_fact_route_stays_blocked():
    result = _evaluate(
        _draft(
            plan={
                "requires_order_lookup": True,
                "order_id_status": "MISSING",
                "order_lookup_status": "FAILED",
            }
        ),
        route="GPT_DIRECT",
    )
    assert result.safe is False
    assert "REQUIRED_ORDER_ID_MISSING_OR_INVALID" in result.reasons
    assert "ORDER_LOOKUP_NOT_TRUSTED" in result.reasons


# CASE K -- privacy/secret exposure remains a hard block.
def test_case_k_privacy_exposure_is_hard_blocked():
    result = _evaluate(_draft(answer="연락처는 010-1234-5678 입니다."))
    assert result.safe is False
    assert result.decision == "BLOCKED"
    assert "PII_EXPOSURE" in result.reasons


def test_case_k2_secret_exposure_is_hard_blocked():
    result = _evaluate(
        _draft(answer="api_key: sk-abcdefghijklmnop 로 확인해주세요.")
    )
    assert result.safe is False
    assert result.decision == "BLOCKED"


# CASE M -- a policy/high-risk finding remains a hard block.
def test_case_m_high_risk_stays_blocked():
    result = _evaluate(
        _draft(plan={"is_high_risk": True})
    )
    assert result.safe is False
    assert "POLICY_OR_HIGH_RISK_REVIEW" in result.reasons


# CASE R -- a real validator failure remains a hard block.
def test_case_r_validator_failure_stays_blocked():
    result = _evaluate(
        _draft(
            validation_status="REVIEW_REQUIRED",
            validator_result={"passed": False},
        )
    )
    assert result.safe is False
    assert "VALIDATOR_NOT_PASS" in result.reasons


# Idempotency guard is untouched by the relaxation.
def test_already_answered_is_still_blocked():
    result = _evaluate(
        _draft(), inquiry=_inquiry(post_status="POSTED"),
    )
    assert result.safe is False
    assert result.decision == "BLOCKED"
    assert "ALREADY_ANSWERED_OR_POSTED" in result.reasons


# An unresolved template placeholder is an integrity failure, still blocking.
def test_unresolved_placeholder_is_still_blocked():
    result = _evaluate(_draft(answer="안녕하세요 {{customer_name}}님"))
    assert result.safe is False
    assert "UNRESOLVED_PLACEHOLDER" in result.reasons


# Every soft reason must be non-blocking on its own, by construction.
def test_soft_reasons_never_block_on_their_own():
    result = _evaluate(
        _draft(
            analysis={"confidence": 0.1},
            hybrid={"draft": {"confidence": 0.1}},
            plan={
                "requires_order_lookup": True,
                "order_id_status": "MISSING",
                "order_lookup_status": "NOT_RUN",
            },
        ),
        route="ORDER_ID_REQUEST",
    )
    assert result.safe is True
    assert result.reasons == ()
    assert set(result.soft_reasons) <= SOFT_REASONS
