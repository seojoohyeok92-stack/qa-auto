from __future__ import annotations

from typing import Any

import pytest

from answer.exceptions import AnswerAlreadyPostedError, AutoAnswerProhibitedError
from answer.models import AnswerRequest, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService
from services.dps_enrichment_service import DpsEnrichmentService
from workflow.models import StepCode


class FakeClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return dict(self.response)


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "phase3.db")
    value.initialize()
    return value


def add_inquiry(
    database: Database,
    question: str,
    *,
    order_id: str = "2026072912345678",
    product_order_id: str = "PRODUCT-ORDER-ONLY",
    source_id: str = "PHASE3",
) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": source_id,
            "inquiry_type": "문의",
            "content": question,
            "product_name": "삼성 스마트모니터 M5 32인치",
            "order_id": order_id,
            "product_order_id": product_order_id,
            "raw_json": {"order_date": "2026-07-29"},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


def success_response() -> dict[str, Any]:
    return {
        "success": True,
        "found": True,
        "status": "RESULT_FOUND_WITH_DETAIL",
        "queried_at": "2026-07-29T14:30:00+09:00",
        "data": {
            "dps_sales_number": "SALE-100",
            "progress_status": "배송 준비 중",
            "installation_status": "설치 예정",
            "required_delivery_date": "2026-08-03",
            "raw_required_delivery_date": "2026-08-03",
            "installation_date_source": (
                "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
            ),
            "date_parse_status": "PARSED",
            "required_delivery_date_row_count": 1,
        },
    }


def service(
    database: Database, client: FakeClient
) -> DpsEnrichmentService:
    return DpsEnrichmentService(database, client=client)


def request(inquiry_id: int, question: str, order_id: str = "ORDER-1"):
    return AnswerRequest(
        inquiry_id=inquiry_id,
        question=question,
        product_name="삼성 스마트모니터 M5 32인치",
        order_id=order_id,
        product_order_id="PRODUCT-1",
        metadata={"order_date": "2026-07-29"},
    )


def test_general_question_calls_neither_client_nor_cache(database: Database) -> None:
    inquiry_id = add_inquiry(database, "넷플릭스 되나요?")
    client = FakeClient(success_response())
    outcome = service(database, client).enrich(
        request(inquiry_id, "넷플릭스 되나요?")
    )
    assert outcome.metadata["lookup_status"] == "NOT_REQUIRED"
    assert client.calls == []
    assert DpsRepository(database).list_history_by_inquiry_id(inquiry_id) == []


def test_delivery_uses_only_order_id_and_injects_metadata(database: Database) -> None:
    inquiry_id = add_inquiry(database, "설치는 언제 오나요?")
    client = FakeClient(success_response())
    value = request(inquiry_id, "설치는 언제 오나요?")
    outcome = service(database, client).enrich(value)
    assert client.calls[0]["order_id"] == "ORDER-1"
    assert client.calls[0]["dps_query_value"] == "ORDER-1"
    assert "product_order_id" not in client.calls[0]
    assert value.metadata["dps"]["installation_date"] == "2026-08-03"
    assert outcome.metadata["lookup_status"] == "SUCCESS"


def test_product_order_only_does_not_call_client(database: Database) -> None:
    inquiry_id = add_inquiry(
        database, "배송은 언제 오나요?", order_id=""
    )
    client = FakeClient(success_response())
    outcome = service(database, client).enrich(
        request(inquiry_id, "배송은 언제 오나요?", order_id="")
    )
    assert client.calls == []
    assert outcome.metadata["lookup_status"] == "WAITING_FOR_ORDER_ID"
    step = WorkflowRepository(database).get_step(
        inquiry_id, StepCode.DPS_LOOKUP
    )
    assert step["step_status"] == "NEEDS_REVIEW"


def test_success_updates_workflow_and_activity_logs(database: Database) -> None:
    inquiry_id = add_inquiry(database, "배송 상태를 알려주세요.")
    client = FakeClient(success_response())
    service(database, client).enrich(
        request(inquiry_id, "배송 상태를 알려주세요.")
    )
    step = WorkflowRepository(database).get_step(
        inquiry_id, StepCode.DPS_LOOKUP
    )
    events = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(inquiry_id)
    }
    assert step["step_status"] == "COMPLETED"
    assert {
        "DPS_LOOKUP_REQUESTED",
        "DPS_CACHE_MISS",
        "DPS_LOOKUP_STARTED",
        "DPS_LOOKUP_SUCCEEDED",
        "DPS_RESULT_INJECTED_TO_ANSWER",
    } <= events


def test_valid_success_cache_skips_agent(database: Database) -> None:
    inquiry_id = add_inquiry(database, "설치는 언제 오나요?")
    first = FakeClient(success_response())
    value = request(inquiry_id, "설치는 언제 오나요?")
    service(database, first).enrich(value)
    second = FakeClient({"success": False})
    outcome = service(database, second).enrich(value)
    assert second.calls == []
    assert outcome.metadata["cache_used"] is True
    assert len(DpsRepository(database).list_history_by_inquiry_id(inquiry_id)) == 1


def test_force_refresh_ignores_cache_and_preserves_history(database: Database) -> None:
    inquiry_id = add_inquiry(database, "설치는 언제 오나요?")
    client = FakeClient(success_response())
    target = service(database, client)
    value = request(inquiry_id, "설치는 언제 오나요?")
    target.enrich(value)
    target.enrich(value, force_refresh=True)
    assert len(client.calls) == 2
    assert len(DpsRepository(database).list_history_by_inquiry_id(inquiry_id)) == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"success": True, "found": False, "code": "NO_DPS_RESULT"}, "NOT_FOUND"),
        ({"success": False, "code": "AGENT_READ_TIMEOUT"}, "TIMEOUT"),
        ({"success": False, "code": "AGENT_CONNECTION_FAILED"}, "AGENT_OFFLINE"),
        ({"success": False, "code": "DETAIL_PARSE_FAILED"}, "PARSE_ERROR"),
    ],
)
def test_failures_are_distinct_and_need_attention(
    database: Database, raw: dict[str, Any], expected: str
) -> None:
    inquiry_id = add_inquiry(database, "배송은 언제 오나요?")
    outcome = service(database, FakeClient(raw)).enrich(
        request(inquiry_id, "배송은 언제 오나요?")
    )
    inquiry = InquiryRepository(database).get(inquiry_id)
    assert outcome.metadata["lookup_status"] == expected
    assert inquiry["workflow_status"] == "NEEDS_ATTENTION"


def test_manual_dps_lookup_reopens_skipped_step_then_succeeds(
    database: Database,
) -> None:
    inquiry_id = add_inquiry(database, "배송은 언제 오나요?")
    workflows = WorkflowRepository(database)
    workflows.skip_step(
        inquiry_id,
        StepCode.DPS_LOOKUP,
        metadata={"reason": "PREVIOUS_PLAN_NOT_REQUIRED"},
    )

    outcome = service(database, FakeClient(success_response())).enrich(
        request(inquiry_id, "배송은 언제 오나요?"),
        force_refresh=True,
    )

    assert outcome.metadata["lookup_status"] == "SUCCESS"
    assert workflows.get_step(
        inquiry_id, StepCode.DPS_LOOKUP
    )["step_status"] == "COMPLETED"


def test_manual_dps_lookup_reopens_skipped_step_then_records_failure(
    database: Database,
) -> None:
    inquiry_id = add_inquiry(database, "배송은 언제 오나요?")
    workflows = WorkflowRepository(database)
    workflows.skip_step(
        inquiry_id,
        StepCode.DPS_LOOKUP,
        metadata={"reason": "PREVIOUS_PLAN_NOT_REQUIRED"},
    )

    outcome = service(
        database,
        FakeClient({"success": False, "code": "AGENT_READ_TIMEOUT"}),
    ).enrich(
        request(inquiry_id, "배송은 언제 오나요?"),
        force_refresh=True,
    )

    assert outcome.metadata["lookup_status"] == "TIMEOUT"
    assert workflows.get_step(
        inquiry_id, StepCode.DPS_LOOKUP
    )["step_status"] == "FAILED"


def test_sensitive_raw_values_are_not_stored_or_logged(database: Database) -> None:
    inquiry_id = add_inquiry(database, "배송은 언제 오나요?")
    raw = success_response()
    raw["data"]["buyer_phone"] = "010-1234-5678"
    raw["data"]["delivery_address"] = "서울시 비공개 주소"
    service(database, FakeClient(raw)).enrich(
        request(inquiry_id, "배송은 언제 오나요?")
    )
    row = DpsRepository(database).get_latest_by_inquiry_id(inquiry_id)
    logs = LogRepository(database).recent_for_inquiry(inquiry_id)
    assert "010-1234-5678" not in str(row)
    assert "서울시 비공개 주소" not in str(row)
    assert "010-1234-5678" not in str(logs)


def test_success_date_is_in_answer_and_draft_is_saved(database: Database) -> None:
    inquiry_id = add_inquiry(database, "설치는 언제 오나요?")
    enrichment = service(database, FakeClient(success_response()))
    outcome = AnswerService(
        database, dps_enrichment=enrichment
    ).generate_for_inquiry(inquiry_id)
    assert outcome.result.status is AnswerStatus.GENERATED
    assert "2026년 8월 3일" in outcome.result.answer
    assert outcome.result.metadata["dps"]["lookup_status"] == "SUCCESS"
    assert len(AnswerRepository(database).history_for_inquiry(inquiry_id)) == 1


def test_promised_deadline_question_runs_dps_and_uses_authoritative_date(
    database: Database,
) -> None:
    question = (
        "예정일이 8/25일이라던 것 같은데 말일까지 가능할까요? "
        "잊어먹고 있으면 오겠지 했는데 기다리다 지쳐가네요."
    )
    inquiry_id = add_inquiry(
        database,
        question,
        source_id="PROMISED-DEADLINE-E2E",
    )
    client = FakeClient(success_response())

    outcome = AnswerService(
        database,
        dps_enrichment=service(database, client),
    ).generate_for_inquiry(inquiry_id)

    assert len(client.calls) == 1
    assert client.calls[0]["order_id"] == "2026072912345678"
    assert outcome.result.metadata["phase9"]["analysis"][
        "detected_intent"
    ] == "DELIVERY_DATE"
    assert outcome.result.metadata["phase9"]["analysis"][
        "order_id_status"
    ] == "VALIDATED"
    assert outcome.result.metadata["dps"]["lookup_status"] == "SUCCESS"
    assert outcome.result.metadata["selected_answer_route"] == (
        "DELIVERY_WITH_INSTALLATION_DATE"
    )
    assert "2026년 8월 3일" in outcome.result.answer
    assert "8월 25일" not in outcome.result.answer


def test_change_request_is_never_auto_answerable(database: Database) -> None:
    inquiry_id = add_inquiry(database, "설치일을 변경해 주세요.")
    client = FakeClient(success_response())
    with pytest.raises(AutoAnswerProhibitedError):
        AnswerService(
        database,
        dps_enrichment=service(database, client),
        ).generate_for_inquiry(inquiry_id)
    assert client.calls == []
    assert AnswerRepository(database).active_for_inquiry(inquiry_id) is None


def test_mixed_question_keeps_general_answer_when_dps_fails(
    database: Database,
) -> None:
    inquiry_id = add_inquiry(
        database,
        "넷플릭스 되나요? 설치는 언제 오나요?",
    )
    outcome = AnswerService(
        database,
        dps_enrichment=service(
            database,
            FakeClient({"success": False, "code": "AGENT_READ_TIMEOUT"}),
        ),
    ).generate_for_inquiry(inquiry_id)
    assert outcome.result.status is AnswerStatus.GENERATED
    assert outcome.result.answer.strip()
    assert "담당자 확인" in outcome.result.answer
    assert outcome.result.metadata["answer_type"] == "manual_review_required"


def test_posted_inquiry_blocks_dps_lookup(database: Database) -> None:
    inquiry_id = add_inquiry(database, "배송은 언제 오나요?")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET post_status = 'POSTED' WHERE id = ?",
            (inquiry_id,),
        )
    client = FakeClient(success_response())
    with pytest.raises(AnswerAlreadyPostedError):
        AnswerService(
            database, dps_enrichment=service(database, client)
        ).enrich_dps_for_inquiry(inquiry_id)
    assert client.calls == []
