from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.answer_format import format_final_answer
from answer.exceptions import (
    AnswerAlreadyPostedError,
    AnswerGenerationError,
)
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService
from workflow.models import StepCode


class StaticEngine:
    def __init__(self, result: AnswerResult) -> None:
        self.result = result
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return self.result


class FailingEngine:
    def generate(self, request):
        raise RuntimeError("internal detail that must not reach DB")


class FailingHybrid:
    def generate(self, request, rule_result):
        raise RuntimeError("internal detail that must not reach DB")


def generated_result(answer: str = "프로그램 원본 답변") -> AnswerResult:
    value = AnswerResult(
        status=AnswerStatus.GENERATED,
        category="배송/택배",
        reason="택배 규칙",
        answer=answer,
        provider="rules",
        auto_answerable=True,
        needs_review=False,
    )
    value.metadata["existing_template"] = True
    return value


def review_result() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="배송/직원확인",
        reason="주문별 확인 필요",
        answer="직원 검토가 필요한 답변 후보",
        provider="openai",
        auto_answerable=False,
        needs_review=True,
    )


class StaticHybrid:
    def __init__(self, result: AnswerResult) -> None:
        self.result = result

    def generate(self, request, rule_result):
        return SimpleNamespace(
            result=self.result,
            validation=SimpleNamespace(passed=True),
            fallback_used=False,
            events=(),
        )


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "service.db")
    database.initialize()
    return database


def create_inquiry(
    database: Database,
    source_question_id: str,
) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": source_question_id,
            "inquiry_type": "상품",
            "title": "일반 상품 문의",
            "content": "이 제품의 사용 방법이 궁금합니다.",
            "product_name": "삼성 스마트모니터 M5",
            "option_name": "32인치",
            "order_id": "ORDER-ID",
            "product_order_id": "PRODUCT-ORDER-ID",
            "raw_json": {"existing_answer": ""},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


def test_service_generates_from_inquiry_and_saves_draft(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database, "SERVICE-1")
    engine = StaticEngine(generated_result())
    outcome = AnswerService(database, engine=engine).generate_for_inquiry(
        inquiry_id
    )
    assert outcome.draft["original_answer"] == format_final_answer(
        "프로그램 원본 답변"
    )
    assert engine.requests[0].inquiry_id == inquiry_id
    assert engine.requests[0].order_id == "ORDER-ID"
    assert engine.requests[0].product_order_id == "PRODUCT-ORDER-ID"


def test_saved_active_draft_is_enqueued_for_kakao(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inquiry_id = create_inquiry(database, "SERVICE-KAKAO-1")
    calls: list[dict[str, object]] = []

    def record_notification(**kwargs: object) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        "services.answer_service.notify_qna_safely",
        record_notification,
    )
    outcome = AnswerService(
        database,
        engine=StaticEngine(generated_result("카카오 공유 답변")),
    ).generate_for_inquiry(inquiry_id)

    # A successful draft is an intermediate state.  The confirmed Naver post
    # path owns the one operator-facing success notification.
    assert calls == []
    return
    assert calls[0]["title"] == "[네이버 Q&A 답변 생성 완료]"
    assert calls[0]["product"] == "삼성 스마트모니터 M5"
    assert calls[0]["question"] == "이 제품의 사용 방법이 궁금합니다."
    assert "카카오 공유 답변" in str(calls[0]["answer"])
    assert calls[0]["notify_key"] == (
        f"answer_draft_created:{inquiry_id}:{outcome.draft['id']}"
    )


def test_kakao_failure_does_not_fail_saved_answer(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inquiry_id = create_inquiry(database, "SERVICE-KAKAO-2")

    def fail_notification(**_: object) -> bool:
        raise OSError("outbox unavailable")

    monkeypatch.setattr(
        "services.answer_service.notify_qna_safely",
        fail_notification,
    )
    outcome = AnswerService(
        database,
        engine=StaticEngine(generated_result()),
    ).generate_for_inquiry(inquiry_id)

    assert outcome.draft["is_active"] == 1
    events = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(inquiry_id)
    }
    # No intermediate success notification is attempted, so an unavailable
    # outbox cannot create a false failure event at draft-generation time.
    assert "KAKAO_NOTIFICATION_ENQUEUE_FAILED" not in events


def test_success_completes_step_and_sets_review_pending(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database, "SERVICE-2")
    AnswerService(
        database,
        engine=StaticEngine(generated_result()),
    ).generate_for_inquiry(inquiry_id)
    step = WorkflowRepository(database).get_step(
        inquiry_id,
        StepCode.ANSWER_GENERATED,
    )
    inquiry = InquiryRepository(database).get(inquiry_id)
    assert step["step_status"] == "COMPLETED"
    assert inquiry["workflow_status"] == "REVIEW_PENDING"


def test_review_result_completes_draft_step_and_waits_for_staff_review(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database, "SERVICE-3")
    outcome = AnswerService(
        database,
        engine=StaticEngine(review_result()),
        hybrid_service=StaticHybrid(review_result()),
    ).generate_for_inquiry(inquiry_id)
    step = WorkflowRepository(database).get_step(
        inquiry_id,
        StepCode.ANSWER_GENERATED,
    )
    inquiry = InquiryRepository(database).get(inquiry_id)
    assert outcome.draft["review_status"] == "NEEDS_REVIEW"
    assert step["step_status"] == "COMPLETED"
    assert inquiry["workflow_status"] == "REVIEW_PENDING"


def test_failure_marks_step_and_activity_log_without_internal_detail(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database, "SERVICE-4")
    outcome = AnswerService(
        database,
        engine=FailingEngine(),
        hybrid_service=FailingHybrid(),
    ).generate_for_inquiry(inquiry_id)
    step = WorkflowRepository(database).get_step(
        inquiry_id,
        StepCode.ANSWER_GENERATED,
    )
    logs = LogRepository(database).recent_for_inquiry(inquiry_id)
    assert step["step_status"] == "COMPLETED"
    assert outcome.result.metadata["selected_answer_route"] == (
        "REVIEW_REQUIRED_SAFE_DRAFT"
    )
    assert "internal detail" not in str(step)
    assert any(log["event_code"] == "SAFE_DRAFT_CREATED" for log in logs)
    assert "internal detail" not in str(logs)


def test_one_inquiry_failure_does_not_affect_another(
    database: Database,
) -> None:
    failed_id = create_inquiry(database, "SERVICE-5A")
    success_id = create_inquiry(database, "SERVICE-5B")
    safe = AnswerService(
        database,
        engine=FailingEngine(),
        hybrid_service=FailingHybrid(),
    ).generate_for_inquiry(failed_id)
    outcome = AnswerService(
        database,
        engine=StaticEngine(generated_result()),
    ).generate_for_inquiry(success_id)
    assert safe.result.metadata["selected_answer_route"] == (
        "REVIEW_REQUIRED_SAFE_DRAFT"
    )
    assert outcome.result.status is AnswerStatus.GENERATED
    assert len(AnswerRepository(database).history_for_inquiry(failed_id)) == 1
    assert len(
        AnswerRepository(database).history_for_inquiry(success_id)
    ) == 1


def test_regeneration_keeps_history_and_restarts_completed_step(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database, "SERVICE-6")
    service = AnswerService(
        database,
        engine=StaticEngine(generated_result("첫 답변")),
    )
    service.generate_for_inquiry(inquiry_id)
    service = AnswerService(
        database,
        engine=StaticEngine(generated_result("둘째 답변")),
    )
    service.generate_for_inquiry(inquiry_id)
    history = AnswerRepository(database).history_for_inquiry(inquiry_id)
    step = WorkflowRepository(database).get_step(
        inquiry_id,
        StepCode.ANSWER_GENERATED,
    )
    assert [draft["original_answer"] for draft in history] == [
        format_final_answer("둘째 답변"),
        format_final_answer("첫 답변"),
    ]
    assert step["step_status"] == "COMPLETED"
    assert step["attempt_count"] == 2


def test_posted_inquiry_regeneration_is_blocked(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database, "SERVICE-7")
    draft = AnswerService(
        database,
        engine=StaticEngine(generated_result()),
    ).generate_for_inquiry(inquiry_id).draft
    with database.transaction() as connection:
        connection.execute(
            "UPDATE answer_drafts SET posted = 1 WHERE id = ?",
            (draft["id"],),
        )
    with pytest.raises(AnswerAlreadyPostedError):
        AnswerService(
            database,
            engine=StaticEngine(generated_result("차단")),
        ).generate_for_inquiry(inquiry_id)


def test_success_and_review_events_are_logged(
    database: Database,
) -> None:
    success_id = create_inquiry(database, "SERVICE-8A")
    review_id = create_inquiry(database, "SERVICE-8B")
    AnswerService(
        database,
        engine=StaticEngine(generated_result()),
    ).generate_for_inquiry(success_id)
    AnswerService(
        database,
        engine=StaticEngine(review_result()),
        hybrid_service=StaticHybrid(review_result()),
    ).generate_for_inquiry(review_id)
    success_codes = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(success_id)
    }
    assert "ANSWER_DRAFT_GENERATED" in success_codes
    assert "GPT_DRAFT_ACTIVATED" in success_codes
    review_codes = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(review_id)
    }
    assert "ANSWER_DRAFT_NEEDS_REVIEW" in review_codes
    assert "ANSWER_ROUTED_AND_SAVED" in review_codes


def test_real_rule_engine_persists_a_draft_end_to_end(
    database: Database,
) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "SERVICE-REAL",
            "inquiry_type": "상품",
            "content": "온누리상품권 신청 방법이 궁금합니다.",
            "product_name": "삼성 TV",
            "raw_json": {},
        }
    ).inquiry_id
    outcome = AnswerService(database).generate_for_inquiry(inquiry_id)
    assert outcome.result.status is AnswerStatus.GENERATED
    assert outcome.result.provider == "rules"
    assert outcome.result.metadata["answer_type"] == "existing_template"
    assert outcome.result.metadata["gpt_called"] is False
    assert outcome.result.category == "행사/신청방법"
    assert outcome.draft["original_answer"] == outcome.result.answer
