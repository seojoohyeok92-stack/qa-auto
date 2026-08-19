from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from services.answer_service import AnswerService
from services.dps_enrichment_service import DpsEnrichmentService
from services.dps_lookup_orchestrator import DpsLookupOrchestrator
from services.dps_result_normalizer import normalize_dps_result
from services.uat_order_service import UatOrderService
from ui.review_workspace import _dps_status_label


def run(code: str) -> AppTest:
    return AppTest.from_string(code).run(timeout=30)


def add_inquiry(
    database: Database,
    *,
    question_id: str = "DPS-1",
    order_id: str | None = "2026073000000001",
    product_order_id: str | None = "2026073000000099",
    order_date: str | None = "2026-07-25",
    content: str = "언제 설치되나요?",
) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": question_id,
            "inquiry_type": "기타",
            "content": content,
            "order_id": order_id,
            "product_order_id": product_order_id,
            "raw_json": {},
        }
    ).inquiry_id
    if order_date:
        with database.transaction() as connection:
            connection.execute(
                "UPDATE inquiries SET order_date=? WHERE id=?",
                (order_date, inquiry_id),
            )
    return inquiry_id


def success_payload() -> dict:
    return {
        "success": True,
        "found": True,
        "code": "LOOKUP_COMPLETE",
        "data": {
            "dps_sales_number": "S-MASKED",
            "delivery_status": "배송 준비",
            "installation_status": "설치 대기",
            "delivery_scheduled_date": "2026-08-03",
            "installation_type": "방문 설치",
        },
    }


def test_explicit_dashboard_lookup_calls_agent_with_order_and_date(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "explicit.db")
    database.initialize()
    inquiry_id = add_inquiry(database, content="DPS 패턴이 없는 문의")
    calls: list[dict] = []

    def client(**kwargs):
        calls.append(kwargs)
        return success_payload()

    enrichment = DpsEnrichmentService(database, client=client)
    answer = AnswerService(database, dps_enrichment=enrichment)
    outcome = DpsLookupOrchestrator(
        database, answer_service=answer
    ).lookup(inquiry_id, correlation_id="corr-explicit")

    assert outcome.lookup_row is not None
    assert calls[0]["order_id"] == "2026073000000001"
    assert calls[0]["dps_query_value_type"] == "order_id"
    assert calls[0]["order_date"] == "2026-07-25"
    assert calls[0]["request_id"] == "corr-explicit"


def test_order_lookup_persists_dps_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(tmp_path / "order.db")
    database.initialize()
    inquiry_id = add_inquiry(
        database,
        order_id=None,
        product_order_id="2026073000000099",
        order_date=None,
    )
    monkeypatch.setattr(
        "services.uat_order_service.get_store_config",
        lambda code: type(
            "Store",
            (),
            {"code": "STORE"},
        )(),
    )
    service = UatOrderService(
        database,
        token_provider=lambda **kwargs: "masked",
        lookup=lambda *args, **kwargs: {
            "success": True,
            "lookup_number": "2026073000000099",
            "lookup_type": "PRODUCT_ORDER_ID",
            "orders": [
                {
                    "order_id": "2026073000000001",
                    "product_order_id": "2026073000000099",
                    "order_date": "2026-07-25T10:00:00+09:00",
                    "product_name": "TV",
                    "product_order_status": "PAYED",
                }
            ],
            "error_code": None,
            "error_message": None,
            "cached": False,
            "queried_at": "2026-07-30T10:00:00+09:00",
        },
    )
    result = service.lookup_for_inquiry(inquiry_id)
    saved = InquiryRepository(database).get(inquiry_id)
    assert result["selected_order"]["order_id"] == "2026073000000001"
    assert saved["order_id"] == "2026073000000001"
    assert saved["product_order_id"] == "2026073000000099"
    assert saved["order_date"] == "2026-07-25T10:00:00+09:00"
    assert saved["order_status"] == "PAYED"
    assert saved["order_lookup_at"] == "2026-07-30T10:00:00+09:00"
    assert saved["raw_json"]["order_lookup"]["product_name"] == "TV"


def test_missing_order_date_blocks_before_agent(tmp_path: Path) -> None:
    database = Database(tmp_path / "missing-date.db")
    database.initialize()
    inquiry_id = add_inquiry(database, order_date=None)
    calls: list[dict] = []

    def client(**kwargs):
        calls.append(kwargs)
        return success_payload()

    enrichment = DpsEnrichmentService(database, client=client)
    answer = AnswerService(database, dps_enrichment=enrichment)
    orchestrator = DpsLookupOrchestrator(database, answer_service=answer)
    try:
        orchestrator.lookup(inquiry_id, correlation_id="corr-date")
    except ValueError as error:
        assert "실제 주문일" in str(error)
    else:  # pragma: no cover
        raise AssertionError("Missing order_date was not blocked")
    assert calls == []


def test_product_order_id_is_blocked_before_agent(tmp_path: Path) -> None:
    database = Database(tmp_path / "product-id.db")
    database.initialize()
    inquiry_id = add_inquiry(
        database,
        order_id="2026073000000099",
        product_order_id="2026073000000099",
    )
    try:
        DpsLookupOrchestrator(database).lookup(
            inquiry_id, correlation_id="corr-product"
        )
    except ValueError as error:
        assert "상품주문번호" in str(error)
    else:  # pragma: no cover
        raise AssertionError("Product order ID was not blocked")


def test_timeout_and_offline_are_distinct() -> None:
    offline = normalize_dps_result(
        {"success": False, "code": "AGENT_CONNECT_TIMEOUT"},
        order_id="O",
        elapsed_seconds=7,
    )
    timeout = normalize_dps_result(
        {"success": False, "code": "AGENT_READ_TIMEOUT"},
        order_id="O",
        elapsed_seconds=100,
    )
    assert offline["lookup_status"] == "AGENT_OFFLINE"
    assert timeout["lookup_status"] == "TIMEOUT"


def test_not_found_is_rendered_as_completed_empty_result() -> None:
    assert _dps_status_label(
        {"lookup_status": "NOT_FOUND"},
        in_progress=False,
        has_order_id=True,
        has_order_date=True,
    ) == "조회 성공 / 결과 없음"


def test_success_is_saved_and_reloaded_with_trace_fields(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "saved.db")
    database.initialize()
    inquiry_id = add_inquiry(database)
    enrichment = DpsEnrichmentService(
        database, client=lambda **kwargs: success_payload()
    )
    outcome = DpsLookupOrchestrator(
        database,
        answer_service=AnswerService(
            database, dps_enrichment=enrichment
        ),
    ).lookup(inquiry_id, correlation_id="corr-saved")
    saved = DpsRepository(database).get_latest_by_inquiry_id(inquiry_id)
    assert saved["id"] == outcome.lookup_row["id"]
    assert saved["correlation_id"] == "corr-saved"
    assert saved["lookup_started_at"]
    assert saved["lookup_completed_at"]
    assert saved["duration_seconds"] >= 0
    assert saved["normalized_result_json"]["sales_number"] == "S-MASKED"


def test_cache_result_is_separated_by_inquiry(tmp_path: Path) -> None:
    database = Database(tmp_path / "separate.db")
    database.initialize()
    first = add_inquiry(database, question_id="FIRST")
    second = add_inquiry(database, question_id="SECOND")
    enrichment = DpsEnrichmentService(
        database, client=lambda **kwargs: success_payload()
    )
    first_orchestrator = DpsLookupOrchestrator(
        database,
        answer_service=AnswerService(
            database, dps_enrichment=enrichment
        ),
    )
    first_orchestrator.lookup(first, correlation_id="corr-first")
    second_orchestrator = DpsLookupOrchestrator(
        database,
        answer_service=AnswerService(
            database,
            dps_enrichment=DpsEnrichmentService(
                database,
                client=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("cache should be used")
                ),
            ),
        ),
    )
    second_orchestrator.lookup(second, correlation_id="corr-second")
    first_row = DpsRepository(database).get_latest_by_inquiry_id(first)
    second_row = DpsRepository(database).get_latest_by_inquiry_id(second)
    assert first_row["inquiry_id"] == first
    assert second_row["inquiry_id"] == second
    assert first_row["id"] != second_row["id"]
    assert second_row["cached"] == 1


def test_dashboard_button_updates_per_inquiry_session_and_card(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ui.db"
    database = Database(path)
    database.initialize()
    inquiry_id = add_inquiry(database)
    at = run(
        f'''
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
import ui.review_workspace as workspace
class Outcome:
    lookup_row=None
class FakeOrchestrator:
    def __init__(self, database): self.database=database
    def lookup(self, inquiry_id, **kwargs):
        row=DpsRepository(self.database).create_lookup_result(
            inquiry_id=inquiry_id, order_id="2026073000000001",
            lookup_status="SUCCESS", raw_result={{"success":True}},
            normalized_result={{
                "lookup_status":"SUCCESS","sales_number":"S-MASKED",
                "delivery_status":"배송 준비","elapsed_seconds":1.2
            }},
            correlation_id=kwargs["correlation_id"],
            lookup_started_at="2026-07-30T10:00:00+09:00",
            lookup_completed_at="2026-07-30T10:00:01+09:00",
            duration_seconds=1.2
        )
        outcome=Outcome()
        outcome.lookup_row=row
        return outcome
workspace.DpsLookupOrchestrator=FakeOrchestrator
db=Database(r"{path}")
db.initialize()
workspace._render_dps(db, InquiryRepository(db).get({inquiry_id}))
'''
    )
    next(button for button in at.button if button.label == "DPS 재조회").click()
    at.run(timeout=30)
    assert not at.exception
    assert at.session_state["selected_inquiry_id"] == inquiry_id
    assert at.session_state["selected_order_id"] == "2026073000000001"
    assert at.session_state["selected_order_date"] == "2026-07-25"
    assert at.session_state["dps_lookup_in_progress"][inquiry_id] is False
    assert at.session_state["dps_result"][inquiry_id]["lookup_status"] == "SUCCESS"
    rendered = "\n".join(item.value for item in at.markdown)
    assert "조회 성공" in rendered
    assert "S-MASKED" in rendered


def test_in_progress_disables_duplicate_click(tmp_path: Path) -> None:
    path = tmp_path / "progress.db"
    database = Database(path)
    database.initialize()
    inquiry_id = add_inquiry(database)
    at = run(
        f'''
import streamlit as st
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_dps
st.session_state["dps_lookup_in_progress"]={{{inquiry_id}:True}}
db=Database(r"{path}")
db.initialize()
_render_dps(db, InquiryRepository(db).get({inquiry_id}))
'''
    )
    assert not at.exception
    assert all(button.disabled for button in at.button)
    assert "조회 중" in "\n".join(item.value for item in at.markdown)


def test_required_activity_log_events_are_recorded(tmp_path: Path) -> None:
    database = Database(tmp_path / "logs.db")
    database.initialize()
    inquiry_id = add_inquiry(database)
    enrichment = DpsEnrichmentService(
        database, client=lambda **kwargs: success_payload()
    )
    DpsLookupOrchestrator(
        database,
        answer_service=AnswerService(
            database, dps_enrichment=enrichment
        ),
    ).lookup(inquiry_id, correlation_id="corr-events")
    events = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(
            inquiry_id, limit=100
        )
    }
    assert {
        "DPS_LOOKUP_REQUESTED",
        "DPS_LOOKUP_STARTED",
        "DPS_AGENT_CONNECTED",
        "DPS_LOOKUP_SUCCEEDED",
        "DPS_RESULT_SAVED",
    } <= events
