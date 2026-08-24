"""What the dashboard tells the operator about one answer.

Production reproduction, inquiry 686125753 (inquiry_id 2610, draft 307). The
answer was fine -- validator PASS, no review signals -- and it was already
published on Naver, so the gate correctly refused to publish it a second time.
The screen nevertheless read:

    자동 답변: 가능      (validation.passed, which is True for REVIEW_REQUIRED too)
    직원 검토: 필요      (approval_status != APPROVED -- nothing to do with safety)
    경고: 2건            (two advisory notes, neither of them blocking)
    전략: MANUAL_REVIEW  (the InquiryAnalysis taken before the answer existed)

so "already answered, nothing to do" was indistinguishable from "unsafe answer
held back for review". None of those four fields consulted the auto-registration
gate at all.

These tests pin the separation: the validator's verdict, whether a person must
act, what the gate decides, and where an answer is already published are four
different questions with four different answers.

Fakes only -- no provider, no network, no posting, no database writes.
"""
from __future__ import annotations

import pytest

from services.auto_processing_eligibility_service import (
    AutoProcessingEligibility,
)
from ui.answer_status_presenter import (
    ALREADY_ANSWERED,
    BLOCKED,
    ELIGIBLE,
    HELD,
    UNKNOWN,
    build_answer_status,
    describe_reason,
    pipeline_route,
)


def _inquiry(**overrides):
    base = {
        "id": 2610,
        "source_answered": 0,
        "post_status": "NOT_POSTED",
        "approval_status": "PENDING",
    }
    base.update(overrides)
    return base


def _draft(
    *,
    status: str = "PASS",
    review_signals=(),
    warnings=(),
    errors=(),
    review_status: str = "PENDING",
    route: str = "GPT_DIRECT",
    analysis: dict | None = None,
):
    """A stored draft row shaped the way the repository writes it."""

    return {
        "id": 307,
        "original_answer": "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.",
        "review_status": review_status,
        "validation_status": "PASS" if status != "BLOCK" else "FAILED_INVALID_CONTENT",
        "validator_result_json": {
            "passed": status != "BLOCK",
            "status": status,
            "errors": list(errors),
            "warnings": list(warnings),
            "review_signals": list(review_signals),
        },
        "inquiry_analysis_json": analysis or {},
        "metadata_json": {
            "selected_answer_route": route,
            "processing_plan": {"analysis": analysis or {}},
            "hybrid": {},
        },
    }


def _status(inquiry, draft, *, eligibility=None):
    return build_answer_status(
        inquiry=inquiry,
        draft=draft,
        route=pipeline_route(draft),
        eligibility=eligibility,
    )


def _gate(decision: str, reasons=(), soft=()):
    return AutoProcessingEligibility(
        decision, "AUTO_POST_ELIGIBILITY", tuple(reasons), soft_reasons=tuple(soft)
    )


# ------------------------------------------------------------------ CASE A


def test_case_a_pass_with_advisory_is_eligible_and_not_staff_review() -> None:
    """A safe answer with advisory notes must not read as needing review."""

    view = _status(
        _inquiry(),
        _draft(warnings=["배송기한 미확정", "ANSWER_CONTAINS_UNREQUESTED_TOPIC"]),
        eligibility=_gate("SAFE"),
    )
    assert view.validation_status == "PASS"
    assert view.validation_label.startswith("PASS")
    assert view.registration == ELIGIBLE
    assert view.registration_label == "가능"
    assert view.staff_review_required is False
    assert view.staff_review_label == "불필요"
    assert view.advisory == (
        "배송기한 미확정",
        "ANSWER_CONTAINS_UNREQUESTED_TOPIC",
    )
    assert view.review_signals == ()
    assert view.blocking_reasons == ()


# ------------------------------------------------------------------ CASE B


def test_case_b_review_required_holds_and_asks_for_staff() -> None:
    view = _status(
        _inquiry(),
        _draft(status="REVIEW_REQUIRED", review_signals=["직원 확인이 필요합니다"]),
        eligibility=_gate("REVIEW_REQUIRED", ["VALIDATOR_REVIEW_REQUIRED"]),
    )
    assert view.validation_status == "REVIEW_REQUIRED"
    assert view.registration == HELD
    assert view.staff_review_required is True
    assert view.review_signals == ("직원 확인이 필요합니다",)
    assert view.blocking_reasons == (
        ("VALIDATOR_REVIEW_REQUIRED", "Validator가 직원 확인을 요청했습니다."),
    )


# ------------------------------------------------------------------ CASE C


def test_case_c_block_is_reported_as_block() -> None:
    view = _status(
        _inquiry(),
        _draft(status="BLOCK", errors=["근거 없는 수치·기간을 확정했습니다: 2주"]),
        eligibility=_gate("REVIEW_REQUIRED", ["VALIDATOR_NOT_PASS"]),
    )
    assert view.validation_status == "BLOCK"
    assert "BLOCK" in view.validation_label
    assert view.staff_review_required is True
    assert view.registration != ELIGIBLE
    assert view.errors == ("근거 없는 수치·기간을 확정했습니다: 2주",)


def test_privacy_block_is_reported_as_blocked_not_merely_held() -> None:
    view = _status(
        _inquiry(),
        _draft(),
        eligibility=AutoProcessingEligibility(
            "BLOCKED", "PRIVACY", ("PII_EXPOSURE",)
        ),
    )
    assert view.registration == BLOCKED
    assert view.registration_label == "차단"
    assert view.staff_review_required is True
    assert view.blocking_reasons == (
        ("PII_EXPOSURE", "개인정보가 노출될 수 있어 자동 등록을 차단했습니다."),
    )


# ------------------------------------------------------------------ CASE D


def test_case_d_already_answered_reads_as_already_answered() -> None:
    """Idempotency is a normal outcome, not a failure to explain away."""

    view = _status(
        _inquiry(source_answered=1),
        _draft(),
        eligibility=AutoProcessingEligibility(
            "BLOCKED", "IDEMPOTENCY", ("ALREADY_ANSWERED_OR_POSTED",)
        ),
    )
    assert view.registration == ALREADY_ANSWERED
    assert "이미 답변됨" in view.registration_label
    assert "중복등록 방지" in view.registration_label
    # Nothing for a person to decide: the customer already has an answer.
    assert view.staff_review_required is False
    assert view.blocking_reasons == (
        (
            "ALREADY_ANSWERED_OR_POSTED",
            "이미 답변이 등록되어 있어 중복 등록하지 않습니다.",
        ),
    )


# ------------------------------------------------------------------ CASE E


def test_case_e_pending_approval_is_not_staff_review() -> None:
    """Awaiting approval and needing judgement are different states."""

    view = _status(
        _inquiry(approval_status="PENDING"),
        _draft(),
        eligibility=_gate("SAFE"),
    )
    assert view.approval_label == "대기"
    assert view.staff_review_required is False
    assert view.registration == ELIGIBLE


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("PENDING", "대기"), ("APPROVED", "승인 완료"), ("POSTED", "등록 완료")],
)
def test_approval_status_is_reported_on_its_own_axis(
    stored: str, expected: str
) -> None:
    view = _status(
        _inquiry(approval_status=stored), _draft(), eligibility=_gate("SAFE")
    )
    assert view.approval_label == expected


# ------------------------------------------------------------------ CASE F


def test_case_f_stale_manual_review_analysis_does_not_drive_the_verdict() -> None:
    """The pre-answer analysis must not be shown as the final judgement."""

    analysis = {
        "answer_strategy": "MANUAL_REVIEW",
        "manual_review_required": True,
        "inquiry_subtype": "UNCLASSIFIED",
        "question_category": "INFORMATION_INSUFFICIENT",
        "confidence": 0.45,
    }
    view = _status(
        _inquiry(),
        _draft(analysis=analysis),
        eligibility=_gate(
            "SAFE", soft=["INTENT_UNCLASSIFIED_VALIDATOR_CLEAR"]
        ),
    )
    assert view.validation_status == "PASS"
    assert view.registration == ELIGIBLE
    assert view.staff_review_required is False
    # The classifier gap is still surfaced, as a recorded note.
    assert view.soft_reasons == (
        (
            "INTENT_UNCLASSIFIED_VALIDATOR_CLEAR",
            "문의 유형을 분류하지 못했지만 Validator는 통과했습니다.",
        ),
    )


# ------------------------------------------------------------- CASE G and H


def test_case_g_advisory_only_is_not_presented_as_a_block() -> None:
    view = _status(
        _inquiry(), _draft(warnings=["참고 사항"]), eligibility=_gate("SAFE")
    )
    assert view.advisory == ("참고 사항",)
    assert view.review_signals == ()
    assert view.errors == ()
    assert view.validation_status == "PASS"
    assert view.registration == ELIGIBLE


def test_case_h_review_signals_are_separated_from_advisory() -> None:
    """warnings carries both; a signal must never be counted as advisory."""

    view = _status(
        _inquiry(),
        _draft(
            status="REVIEW_REQUIRED",
            review_signals=["직원 확인이 필요합니다"],
            warnings=["참고 사항", "직원 확인이 필요합니다"],
        ),
        eligibility=_gate("REVIEW_REQUIRED", ["VALIDATOR_REVIEW_REQUIRED"]),
    )
    assert view.advisory == ("참고 사항",)
    assert view.review_signals == ("직원 확인이 필요합니다",)
    assert view.warning_count == 2


# ------------------------------------------------------------------- reasons


def test_every_reason_the_gate_can_emit_has_a_human_sentence() -> None:
    """No operator-facing screen may show a bare enum for a known reason."""

    import re
    from pathlib import Path

    source = (
        Path(__file__).parents[1]
        / "services"
        / "auto_processing_eligibility_service.py"
    ).read_text(encoding="utf-8")
    emitted = set(re.findall(r'reasons\.append\(\s*"([A-Z_]+)"', source))
    emitted |= set(
        re.findall(r'^\s{16,}"([A-Z_]+)"$', source, flags=re.MULTILINE)
    )
    # Codes that are not gate reasons at all (stages, routes, statuses).
    emitted -= {
        "AUTO_POST_ELIGIBILITY", "IDEMPOTENCY", "PRIVACY", "VALIDATOR",
        "BLOCKED", "REVIEW_REQUIRED", "NEEDS_REVIEW", "IN_REVIEW", "POSTED",
        "SUCCESS", "VALID", "ANSWERABLE", "SUPPORTED", "UNCLASSIFIED",
        "INFORMATION_INSUFFICIENT", "MANUAL", "FAILED", "REVIEW",
        "UNCONFIRMED", "PRODUCT_COMPATIBILITY",
    }
    assert emitted, "no reason codes were discovered -- the scan is wrong"
    for code in sorted(emitted):
        assert describe_reason(code) != code, f"{code} has no Korean sentence"


def test_a_route_reason_is_explained_without_being_hardcoded() -> None:
    text = describe_reason("ROUTE_REVIEW_REQUIRED_SAFE_DRAFT")
    assert "REVIEW_REQUIRED_SAFE_DRAFT" in text
    assert text != "ROUTE_REVIEW_REQUIRED_SAFE_DRAFT"


def test_an_unknown_reason_is_shown_verbatim_not_guessed() -> None:
    assert describe_reason("SOME_FUTURE_REASON") == "SOME_FUTURE_REASON"


# ------------------------------------------- Naver answer vs. our own post


@pytest.mark.parametrize(
    ("answered", "post_status", "naver", "program"),
    [
        (1, "NOT_POSTED", "답변 완료", "미등록"),
        (0, "NOT_POSTED", "미답변", "미등록"),
        (1, "POSTED", "답변 완료", "등록 완료"),
        (0, "POST_FAILED", "미답변", "등록 실패"),
        (0, "POSTING", "미답변", "등록 중"),
    ],
)
def test_naver_answer_and_program_post_are_reported_separately(
    answered: int, post_status: str, naver: str, program: str
) -> None:
    """source_answered is Naver's truth; post_status is only our own posting."""

    view = _status(
        _inquiry(source_answered=answered, post_status=post_status),
        _draft(),
        eligibility=_gate("SAFE"),
    )
    assert view.naver_answer_label == naver
    assert view.program_post_label == program


def test_no_draft_reports_no_draft_rather_than_a_verdict() -> None:
    view = build_answer_status(inquiry=_inquiry(), draft=None, route="")
    assert view.registration == UNKNOWN
    assert view.validation_status == ""
    assert view.staff_review_required is False


# ------------------------------------------------- the real inquiry, 686125753


def test_inquiry_686125753_reads_as_already_answered_not_as_a_failure() -> None:
    """The exact stored state the server reported, end to end."""

    inquiry = _inquiry(
        source_answered=1, post_status="NOT_POSTED", approval_status="PENDING"
    )
    draft = _draft(
        status="PASS",
        review_signals=(),
        warnings=[
            "현재 배송기한과 설치 예정일은 확인되지 않아 임의로 안내하지 않았습니다.",
            "ANSWER_CONTAINS_UNREQUESTED_TOPIC",
        ],
        review_status="NEEDS_REVIEW",
        analysis={
            "answer_strategy": "MANUAL_REVIEW",
            "manual_review_required": True,
            "inquiry_subtype": "UNCLASSIFIED",
            "question_category": "INFORMATION_INSUFFICIENT",
            "confidence": 0.45,
        },
    )
    # The gate short-circuits on idempotency before anything else is read.
    view = _status(
        inquiry,
        draft,
        eligibility=AutoProcessingEligibility(
            "BLOCKED", "IDEMPOTENCY", ("ALREADY_ANSWERED_OR_POSTED",)
        ),
    )

    assert view.validation_status == "PASS"
    assert view.warning_count == 2
    assert view.review_signals == ()
    assert len(view.advisory) == 2
    assert view.naver_answer_label == "답변 완료"
    assert view.program_post_label == "미등록"
    assert view.registration == ALREADY_ANSWERED
    assert view.approval_label == "대기"
    # The whole point: this must not read as "unsafe answer, staff must fix it".
    assert view.staff_review_required is False
    assert view.validation_label.startswith("PASS")


def test_inquiry_686125753_uses_the_real_gate_when_none_is_supplied() -> None:
    """Computed, not asserted: the presenter calls the gate the pipeline calls."""

    view = _status(
        _inquiry(source_answered=1, post_status="NOT_POSTED"),
        _draft(review_status="NEEDS_REVIEW"),
    )
    assert view.registration == ALREADY_ANSWERED
    assert view.staff_review_required is False


# ------------------------------------------------------------- the invariant


@pytest.mark.parametrize(
    ("decision", "stage", "reasons"),
    [
        ("SAFE", "AUTO_POST_ELIGIBILITY", ()),
        ("REVIEW_REQUIRED", "AUTO_POST_ELIGIBILITY", ("DRAFT_REVIEW_REQUIRED",)),
        ("REVIEW_REQUIRED", "VALIDATOR", ("FINAL_ANSWER_REQUIRED",)),
        ("BLOCKED", "PRIVACY", ("PII_EXPOSURE",)),
        ("BLOCKED", "IDEMPOTENCY", ("ALREADY_ANSWERED_OR_POSTED",)),
    ],
)
def test_the_screen_says_eligible_only_when_the_gate_does(
    decision: str, stage: str, reasons: tuple[str, ...]
) -> None:
    """The UI must never claim publishable when the gate refuses, or vice versa.

    This is the property the old screen broke: it reported readiness from
    fields the gate never consulted.
    """

    eligibility = AutoProcessingEligibility(decision, stage, reasons)
    view = _status(_inquiry(), _draft(), eligibility=eligibility)
    assert (view.registration == ELIGIBLE) is eligibility.safe


def test_every_blocking_reason_reaches_the_screen() -> None:
    """A refusal must never be shown with its reasons dropped."""

    reasons = (
        "DRAFT_REVIEW_REQUIRED",
        "PRODUCT_FACT_NOT_VERIFIED",
        "ROUTE_REVIEW_REQUIRED_SAFE_DRAFT",
    )
    view = _status(
        _inquiry(), _draft(), eligibility=_gate("REVIEW_REQUIRED", reasons)
    )
    assert tuple(code for code, _ in view.blocking_reasons) == reasons
    assert all(text for _, text in view.blocking_reasons)
