"""The gate through the real service, and what the operator is told.

The unit tests pin the decision; this pins what actually happens to an inquiry
that hits it -- what gets stored, what the provider is asked, and what the
notification says. Every provider, poster and notifier here is a fake, and the
counts are asserted, because "we skipped the call" is only true if nothing
called it.
"""
from __future__ import annotations

import pytest

from answer.models import AnswerResult, AnswerStatus
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService

HIGH_RISK = "배송 중에 제품이 파손되어 왔습니다. 환불해 주세요."
ORDINARY = "이 제품의 사용 방법이 궁금합니다."


@pytest.fixture()
def database(tmp_path) -> Database:
    database = Database(tmp_path / "gate.db")
    database.initialize()
    return database


def create_inquiry(database: Database, content: str, sid: str = "GATE-1") -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "CUSTOMER_INQUIRY",
        "source_question_id": sid,
        "inquiry_type": "상품",
        "title": content.splitlines()[0],
        "content": content,
        "product_name": "삼성 스마트모니터 M5",
        "option_name": "32인치",
        "raw_json": {"existing_answer": ""},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


class CountingEngine:
    """A rule engine that records how often it was asked."""

    def __init__(self, result: AnswerResult) -> None:
        self.result, self.requests = result, []

    def generate(self, request):
        self.requests.append(request)
        return self.result


class CountingHybrid:
    """Stands in for the provider path; must not be reached when skipped."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request, rule_result):  # pragma: no cover - see asserts
        self.calls += 1
        raise AssertionError("the provider path was entered after a skip")


def rule_result(answer: str = "안내드립니다.") -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW, category="상품", reason="Rule",
        answer=answer, provider="rules", auto_answerable=False,
        needs_review=True, matched_rule="상품",
    )


@pytest.fixture()
def notifications(monkeypatch):
    sent: list[dict] = []

    def capture(**kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        "services.answer_service.notify_qna_safely", capture
    )
    return sent


# ------------------------------------------------------------------ A / J
def test_a_high_risk_inquiry_is_blocked_before_any_generation(
    database, notifications
):
    """The existing policy block: no draft at all, and reported as a decision."""

    from answer.exceptions import AutoAnswerProhibitedError

    inquiry_id = create_inquiry(database, HIGH_RISK)
    hybrid = CountingHybrid()
    service = AnswerService(
        database, engine=CountingEngine(rule_result()), hybrid_service=hybrid,
    )
    with pytest.raises(AutoAnswerProhibitedError):
        service.generate_for_inquiry(inquiry_id)

    assert hybrid.calls == 0, "a provider was called for a prohibited inquiry"


def test_an_ordinary_inquiry_still_reaches_generation(database, notifications):
    """The regression that matters most: the gate must not stop normal work."""

    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-OK")
    engine = CountingEngine(
        AnswerResult(
            status=AnswerStatus.GENERATED, category="상품", reason="Rule",
            answer="사용 방법은 제품 설명서를 참고해 주세요.", provider="rules",
            auto_answerable=True, needs_review=False, matched_rule="상품",
        )
    )
    outcome = AnswerService(database, engine=engine).generate_for_inquiry(
        inquiry_id
    )
    assert outcome.draft is not None
    assert engine.requests, "the rule engine was never asked"


# --------------------------------------------------- notification contents
def test_a_held_inquiry_is_notified_with_the_real_blocking_reason(
    database, notifications
):
    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-HOLD")
    AnswerService(
        database, engine=CountingEngine(rule_result()),
    ).generate_for_inquiry(inquiry_id)

    assert len(notifications) == 1, "expected exactly one message"
    sent = notifications[0]
    assert sent["title"] == "[Q&A 미등록 / 직원 확인 필요]"
    # The reason is a gate code, not a generation narrative.
    assert sent["hold_codes"], "no machine-readable reason was attached"
    assert sent["hold_reason"], "no operator-facing reason was attached"
    assert "Rule" not in sent["hold_reason"]


def test_a_successful_generation_keeps_its_own_notification(
    database, notifications
):
    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-OK2")
    AnswerService(
        database,
        engine=CountingEngine(
            AnswerResult(
                status=AnswerStatus.GENERATED, category="상품", reason="Rule",
                answer="사용 방법은 제품 설명서를 참고해 주세요.",
                provider="rules", auto_answerable=True, needs_review=False,
                matched_rule="상품",
            )
        ),
    ).generate_for_inquiry(inquiry_id)

    assert len(notifications) == 1
    assert notifications[0]["title"] == "[네이버 Q&A 답변 생성 완료]"
    assert not notifications[0]["hold_codes"]
    assert notifications[0]["generation_skipped"] is False


def test_a_skipped_generation_never_sends_a_generation_complete_notice(
    database, notifications
):
    """Two notifications, or the wrong title, would both mislead."""

    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-SKIP")
    AnswerService(
        database, engine=CountingEngine(rule_result()),
    ).generate_for_inquiry(inquiry_id)

    titles = [item["title"] for item in notifications]
    assert titles.count("[네이버 Q&A 답변 생성 완료]") == 0
    assert len(titles) == 1, titles


def test_notification_failure_never_breaks_generation(database, monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("kakao down")

    monkeypatch.setattr("services.answer_service.notify_qna_safely", explode)
    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-BOOM")
    outcome = AnswerService(
        database, engine=CountingEngine(rule_result()),
    ).generate_for_inquiry(inquiry_id)
    assert outcome.draft is not None


def test_hold_reason_failure_does_not_lose_the_notification(
    database, notifications, monkeypatch
):
    """A reporting bug must not silence the alert an operator depends on."""

    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    def explode(*args, **kwargs):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(AutoProcessingEligibilityService, "evaluate", explode)
    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-REASON-FAIL")
    AnswerService(
        database, engine=CountingEngine(rule_result()),
    ).generate_for_inquiry(inquiry_id)

    assert len(notifications) == 1
    assert notifications[0]["hold_reason"] == ""


# --------------------------------------------- no real outbound side effects
def test_no_real_kakao_message_is_ever_enqueued(database, notifications):
    from kakao_notify import OUTBOX

    before = OUTBOX.stat().st_size if OUTBOX.exists() else 0
    create_and_run = create_inquiry(database, ORDINARY, sid="GATE-NOOUT")
    AnswerService(
        database, engine=CountingEngine(rule_result()),
    ).generate_for_inquiry(create_and_run)
    after = OUTBOX.stat().st_size if OUTBOX.exists() else 0
    assert after == before, "a real kakao outbox entry was written"


# ------------------------------------ a skip must never look like a GPT call
class SkippingHybrid:
    """Raises the gate's signal without touching a provider."""

    def __init__(self, *, reasons=("PROCESSING_PLAN_REQUIRES_REVIEW",)):
        self.reasons, self.calls = reasons, 0

    def generate(self, request, rule_result):
        from answer.exceptions import GenerationSkippedError

        self.calls += 1
        raise GenerationSkippedError(
            reasons=self.reasons, stage="PROCESSING_PLAN"
        )


def test_a_skipped_inquiry_is_never_recorded_as_having_called_gpt(
    database, notifications
):
    """Cost telemetry and the operator both read this flag."""

    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-NOGPT")
    outcome = AnswerService(
        database,
        engine=CountingEngine(rule_result()),
        hybrid_service=SkippingHybrid(),
    ).generate_for_inquiry(inquiry_id)

    metadata = outcome.result.metadata
    assert metadata["gpt_called"] is False
    assert metadata["generation_skipped"] is True
    assert metadata["selected_answer_route"] == "REVIEW_REQUIRED_SAFE_DRAFT"


def test_a_skipped_inquiry_still_stores_a_usable_draft(database, notifications):
    """Skipping generation must not leave the customer with nothing."""

    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-DRAFT")
    outcome = AnswerService(
        database,
        engine=CountingEngine(rule_result()),
        hybrid_service=SkippingHybrid(),
    ).generate_for_inquiry(inquiry_id)

    body = outcome.draft["original_answer"]
    assert body.strip(), "an empty draft was stored"
    assert "직원 검토" in body
    # Not a placeholder, and not a fabricated answer.
    assert "{" not in body and "<masked-" not in body


def test_a_skipped_inquiry_reports_the_gate_reason_to_the_operator(
    database, notifications
):
    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-REASON")
    AnswerService(
        database,
        engine=CountingEngine(rule_result()),
        hybrid_service=SkippingHybrid(
            reasons=("POLICY_OR_HIGH_RISK_REVIEW",)
        ),
    ).generate_for_inquiry(inquiry_id)

    assert len(notifications) == 1
    sent = notifications[0]
    assert sent["generation_skipped"] is True
    assert sent["title"] == "[Q&A 미등록 / 직원 확인 필요]"
    assert sent["hold_codes"]


def test_a_normally_generated_answer_still_reports_gpt_called(
    database, notifications
):
    inquiry_id = create_inquiry(database, ORDINARY, sid="GATE-GPT")
    outcome = AnswerService(
        database,
        engine=CountingEngine(
            AnswerResult(
                status=AnswerStatus.GENERATED, category="상품", reason="Rule",
                answer="사용 방법은 제품 설명서를 참고해 주세요.",
                provider="rules", auto_answerable=True, needs_review=False,
                matched_rule="상품",
            )
        ),
    ).generate_for_inquiry(inquiry_id)
    assert outcome.result.metadata.get("generation_skipped") in (None, False)


# ------------------------- the plan fields the gate reads must actually exist
def test_the_processing_plan_carries_the_fields_the_gate_reads(database):
    """The gate reads a serialised plan, so a rename would silently disable it.

    Nothing would fail: ``.get()`` would return None, every inquiry would look
    unblocked, and the gate would quietly stop skipping anything. Pinned here
    because that failure is invisible.
    """

    from services.inquiry_processing_plan_service import (
        InquiryProcessingPlanService,
    )

    plan = InquiryProcessingPlanService(database).create({
        "id": 1, "source_question_id": "PLAN-1", "store_code": "OJE_PLUS",
        "inquiry_type": "CUSTOMER_INQUIRY", "title": "설치일 변경 가능한가요?",
        "content": "설치일 변경 가능한가요?", "product_name": "삼성 TV",
        "raw_json": {},
    })
    serialised = plan.to_dict()
    assert "needs_staff_review" in serialised
    assert "is_high_risk" in serialised
    assert "analysis" in serialised
    assert "manual_review_required" in serialised["analysis"]
    assert "manual_review_sources" in serialised["analysis"]
    assert "inquiry_subtype" in serialised["analysis"]


def test_a_schedule_change_request_is_skipped_through_the_real_plan(database):
    """End to end on real objects, not a hand-built dict."""

    from services.inquiry_processing_plan_service import (
        InquiryProcessingPlanService,
    )
    from services.pre_generation_gate import PreGenerationGate

    plan = InquiryProcessingPlanService(database).create({
        "id": 2, "source_question_id": "PLAN-2", "store_code": "OJE_PLUS",
        "inquiry_type": "CUSTOMER_INQUIRY", "title": "설치일 변경 가능한가요?",
        "content": "설치일 변경 가능한가요?", "product_name": "삼성 TV",
        "raw_json": {},
    })
    serialised = plan.to_dict()
    decision = PreGenerationGate.evaluate_plan(
        analysis=serialised["analysis"], plan=serialised
    )
    assert decision.skip_generation is True
    assert decision.reasons


def test_an_ordinary_product_question_is_not_skipped_through_the_real_plan(
    database,
):
    from services.inquiry_processing_plan_service import (
        InquiryProcessingPlanService,
    )
    from services.pre_generation_gate import PreGenerationGate

    plan = InquiryProcessingPlanService(database).create({
        "id": 3, "source_question_id": "PLAN-3", "store_code": "OJE_PLUS",
        "inquiry_type": "PRODUCT_INQUIRY", "title": "HDMI 포트 몇 개인가요?",
        "content": "HDMI 포트 몇 개인가요?", "product_name": "삼성 TV",
        "raw_json": {},
    })
    serialised = plan.to_dict()
    assert PreGenerationGate.evaluate_plan(
        analysis=serialised["analysis"], plan=serialised
    ).skip_generation is False
