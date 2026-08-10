from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.answer_format import format_final_answer
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.answer_validator import AnswerValidator
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.phase9_answer_policy import (
    DELIVERY_DATE_ANSWER,
    DELIVERY_DATE_PENDING_ANSWER,
    DELIVERY_DATE_TIME_ANSWER,
    DELIVERY_INVALID_DATE_ANSWER,
    DELIVERY_LOOKUP_FAILED_ANSWER,
    DELIVERY_NOT_FOUND_ANSWER,
    ORDER_ID_REQUEST_ANSWER,
)


class ForbiddenEngine:
    def generate(self, request):
        raise AssertionError("delivery route must skip general templates")


class ForbiddenHybrid:
    def generate(self, request, rule_result):
        raise AssertionError("delivery route must not call GPT")


class StaticEngine:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return AnswerResult(
            status=AnswerStatus.GENERATED,
            category="정책",
            reason="policy",
            answer=self.answer,
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            matched_rule="DELIVERY_NOTIFICATION_POLICY",
        )


class FakeDps:
    def __init__(self, metadata: dict | None = None) -> None:
        self.metadata = dict(metadata or {})
        self.calls: list[str] = []

    def enrich(self, request, **kwargs):
        self.calls.append(request.order_id)
        request.metadata["dps"] = dict(self.metadata)
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=True),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
        request.metadata["dps"] = {
            "lookup_required": False,
            "lookup_status": "NOT_REQUIRED",
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "delivery-routing.db")
    value.initialize()
    return value


def create_inquiry(
    database: Database,
    source_id: str,
    *,
    content: str,
    order_id: str | None = None,
    product_order_id: str | None = None,
    inquiry_type: str = "CUSTOMER_INQUIRY",
) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE",
            "source_type": "NAVER",
            "source_question_id": source_id,
            "inquiry_type": inquiry_type,
            "content": content,
            "order_id": order_id,
            "product_order_id": product_order_id,
            "raw_json": {"order_snapshot": {"protected": True}},
        }
    ).inquiry_id


def generate(
    database: Database,
    inquiry_id: int,
    dps: FakeDps,
    *,
    engine=None,
):
    return AnswerService(
        database,
        engine=engine or ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("몇시에 도착할까요?", "DELIVERY_TIME"),
        ("언제 와요?", "DELIVERY_DATE"),
        ("설치 기사님 몇 시에 오시나요?", "INSTALLATION_TIME"),
        ("설치일이 언제인가요?", "INSTALLATION_DATE"),
    ],
)
def test_delivery_schedule_intents(question: str, intent: str) -> None:
    analysis = InquiryAnalysisService().analyze(
        AnswerRequest(
            question=question,
            inquiry_type="CUSTOMER_INQUIRY",
            order_id="2026072912345678",
        )
    )
    assert analysis.detected_intent == intent
    assert analysis.delivery_question is True
    assert analysis.requires_dps_lookup is True


def test_notification_question_remains_general_policy(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(
        database,
        "NOTIFICATION",
        content="배송 알림톡은 언제 오나요?",
    )
    engine = StaticEngine("설치 전날 알림톡이 발송됩니다.")
    result = generate(database, inquiry_id, FakeDps(), engine=engine)

    assert engine.calls == 1
    assert result.result.answer == format_final_answer(
        "설치 전날 알림톡이 발송됩니다."
    )
    assert result.result.metadata["generation_mode"] == "TEMPLATE"


@pytest.mark.parametrize(
    ("order_id", "product_order_id", "status"),
    [
        (None, None, "MISSING"),
        (None, "202607291234567890", "AMBIGUOUS"),
        ("ORDER-123", None, "INVALID"),
    ],
)
def test_missing_product_only_or_invalid_order_requests_general_order_id(
    database: Database,
    order_id: str | None,
    product_order_id: str | None,
    status: str,
) -> None:
    inquiry_id = create_inquiry(
        database,
        f"ORDER-{status}",
        content="몇시에 도착할까요?",
        order_id=order_id,
        product_order_id=product_order_id,
    )
    dps = FakeDps()
    outcome = generate(database, inquiry_id, dps)

    assert dps.calls == []
    assert outcome.result.answer == ORDER_ID_REQUEST_ANSWER
    assert outcome.result.metadata["selected_answer_route"] == (
        "ORDER_ID_REQUEST"
    )
    analysis = outcome.result.metadata["phase9"]["analysis"]
    assert analysis["order_id_status"] == status
    saved = InquiryRepository(database).get(inquiry_id)
    assert saved["raw_json"]["queue"] == "CUSTOMER_CONFIRMATION_REQUIRED"


def _successful_dps(**values) -> FakeDps:
    metadata = {
        "lookup_required": True,
        "lookup_status": "SUCCESS",
        "installation_date": "2026-08-02",
        "required_delivery_date": "2026-08-02",
        "installation_date_source": (
            "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
        ),
        "date_parse_status": "PARSED",
    }
    metadata.update(values)
    return FakeDps(metadata)


def test_actual_time_question_uses_dps_date_and_not_policy_template(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(
        database,
        "OPERATING-REPRO",
        content="몇시에 도착할까요?",
        order_id="2026072912345678",
        product_order_id="202607291234567890",
    )
    outcome = generate(
        database,
        inquiry_id,
        _successful_dps(installation_time_text="하루"),
    )

    assert "2026년 8월 2일" in outcome.result.answer
    assert "정확한 방문 시간" in outcome.result.answer
    assert "담당 기사님" in outcome.result.answer
    assert outcome.result.metadata["selected_answer_route"] == (
        "DELIVERY_WITH_INSTALLATION_DATE"
    )
    context = outcome.result.metadata["delivery_context"]
    assert context["intent"] == "DELIVERY_TIME"
    assert context["installation_date_raw"] == "2026-08-02"
    assert context["installation_date_display"] == "2026년 8월 2일"
    assert context["installation_time"] is None
    assert outcome.result.answer != "설치 예정일 관련 알림톡은 설치일 전날 발송됩니다."


def test_real_date_and_time_are_both_rendered(database: Database) -> None:
    inquiry_id = create_inquiry(
        database,
        "DATE-TIME",
        content="설치 기사님 몇 시에 오시나요?",
        order_id="2026072912345678",
    )
    outcome = generate(
        database,
        inquiry_id,
        _successful_dps(installation_time_text="14:30"),
    )

    assert outcome.result.answer == DELIVERY_DATE_TIME_ANSWER.format(
        delivery_date="2026년 8월 2일",
        installation_time="14:30",
    )


def test_success_without_date_is_unconfirmed_not_general_template(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(
        database,
        "NO-DATE",
        content="설치일이 언제인가요?",
        order_id="2026072912345678",
    )
    outcome = generate(
        database,
        inquiry_id,
        FakeDps(
            {
                "lookup_status": "SUCCESS",
                "installation_date": None,
                "date_parse_status": "MISSING",
            }
        ),
    )
    assert outcome.result.answer == DELIVERY_DATE_PENDING_ANSWER
    assert outcome.result.metadata["selected_answer_route"] == (
        "DELIVERY_DATE_UNCONFIRMED"
    )


@pytest.mark.parametrize(
    ("status", "answer", "route"),
    [
        ("NOT_FOUND", DELIVERY_NOT_FOUND_ANSWER, "DELIVERY_ORDER_NOT_FOUND"),
        ("TIMEOUT", DELIVERY_LOOKUP_FAILED_ANSWER, "DPS_LOOKUP_FAILED"),
    ],
)
def test_dps_failure_states_never_fall_back_to_general_template(
    database: Database,
    status: str,
    answer: str,
    route: str,
) -> None:
    inquiry_id = create_inquiry(
        database,
        f"FAIL-{status}",
        content="배송 일정 확인",
        order_id="2026072912345678",
    )
    outcome = generate(
        database,
        inquiry_id,
        FakeDps({"lookup_status": status, "error_code": status}),
    )
    assert outcome.result.answer == answer
    assert outcome.result.metadata["selected_answer_route"] == route


def test_verified_past_dps_date_remains_a_grounded_fact(database: Database) -> None:
    inquiry_id = create_inquiry(
        database,
        "PAST-DATE",
        content="언제 설치되나요?",
        order_id="2026072912345678",
    )
    outcome = generate(
        database,
        inquiry_id,
        _successful_dps(
            installation_date="2020-01-01",
            required_delivery_date="2020-01-01",
        ),
    )
    assert outcome.result.answer == DELIVERY_DATE_ANSWER.format(
        delivery_date="2020년 1월 1일"
    )
    assert outcome.result.status is AnswerStatus.GENERATED
    assert outcome.result.metadata["selected_answer_route"] == (
        "DELIVERY_WITH_INSTALLATION_DATE"
    )


def test_regeneration_preserves_old_draft_and_uses_latest_dps(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(
        database,
        "REGENERATE",
        content="몇시에 도착할까요?",
        order_id="2026072912345678",
    )
    old = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="배송",
            reason="old",
            answer="설치 예정일 관련 알림톡은 설치일 전날 발송됩니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    outcome = generate(database, inquiry_id, _successful_dps())
    history = AnswerRepository(database).history_for_inquiry(inquiry_id)

    assert len(history) == 2
    assert outcome.draft["id"] != old["id"]
    assert "2026년 8월 2일" in outcome.draft["original_answer"]
    assert any(row["id"] == old["id"] for row in history)
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)["id"] == (
        outcome.draft["id"]
    )
    inquiry_after = InquiryRepository(database).get(inquiry_id)
    assert inquiry_after["raw_json"]["order_snapshot"] == {"protected": True}


def test_standard_date_template_remains_grounded(database: Database) -> None:
    inquiry_id = create_inquiry(
        database,
        "DATE",
        content="설치일이 언제인가요?",
        order_id="2026072912345678",
    )
    outcome = generate(database, inquiry_id, _successful_dps())
    assert outcome.result.answer == DELIVERY_DATE_ANSWER.format(
        delivery_date="2026년 8월 2일"
    )


def test_order_id_request_dedicated_validator_passes() -> None:
    validation = AnswerValidator().validate_order_id_request(
        ORDER_ID_REQUEST_ANSWER
    )
    assert validation.passed is True
    assert validation.status == "PASS"
    assert not validation.errors
    assert all(rule.status == "PASS" for rule in validation.rules)


def test_order_id_request_validator_blocks_invented_schedule() -> None:
    validation = AnswerValidator().validate_order_id_request(
        ORDER_ID_REQUEST_ANSWER + "\n2026년 8월 2일 오후 2시에 방문합니다."
    )
    assert validation.passed is False
    assert any(
        rule.code == "NO_INVENTED_SCHEDULE" and rule.status == "BLOCK"
        for rule in validation.rules
    )
