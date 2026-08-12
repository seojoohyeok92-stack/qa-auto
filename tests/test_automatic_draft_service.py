from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.models import AnswerResult, AnswerStatus
from answer.answer_format import DEFAULT_PREFIX, FINAL_FALLBACK_NOTICE
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.automatic_draft_service import (
    AutomaticDraftOutcome,
    AutomaticDraftService,
)
from services.answer_service import AnswerService
from services.inquiry_sync_service import InquirySyncService


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "automatic-draft.db")
    value.initialize()
    return value


def _work_item(source_id: str = "AUTO-1") -> dict:
    return {
        "store_code": "OJE_PLUS",
        "source": "PRODUCT_INQUIRY",
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_id": source_id,
        "source_question_id": source_id,
        "external_inquiry_id": source_id,
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "상품 문의",
        "content": "tv로도 사용하려면 어떻게 해야 하나요?",
        "registered_at": "2026-08-05T14:00:00+09:00",
        "raw_json": {},
    }


class DraftCreatingAnswerService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.calls = 0

    def generate_for_inquiry(self, inquiry_id: int, **kwargs):
        self.calls += 1
        result = AnswerResult(
            status=AnswerStatus.GENERATED,
            category="일반문의",
            reason="GPT fallback",
            answer="안녕하세요, 고객님. 문의하신 TV 사용 방법을 안내드립니다.",
            provider="fake_gpt",
            auto_answerable=True,
            needs_review=False,
            metadata={
                "selected_answer_route": "GPT_FALLBACK",
                "generation_mode": "GPT_FALLBACK",
            },
        )
        draft = AnswerRepository(self.database).create_program_draft(
            inquiry_id, result
        )
        return SimpleNamespace(result=result, draft=draft)


def test_non_delivery_without_order_gets_automatic_active_draft(
    database: Database,
) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        _work_item()
    ).inquiry_id
    generator = DraftCreatingAnswerService(database)
    service = AutomaticDraftService(database, answer_service=generator)

    first = service.ensure_for_inquiry(inquiry_id, correlation_id="auto-test")
    second = service.ensure_for_inquiry(inquiry_id, correlation_id="auto-test")

    assert first.status == "CREATED"
    assert first.route == "GPT_FALLBACK"
    assert second.status == "EXISTING"
    assert generator.calls == 1
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active is not None
    assert active["original_answer"].strip()


class RecordingAutomaticDrafts:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def ensure_for_inquiry(self, inquiry_id: int, **kwargs):
        self.calls.append(inquiry_id)
        return AutomaticDraftOutcome(
            status="CREATED",
            inquiry_id=inquiry_id,
            draft_id=100 + inquiry_id,
            route="GPT_FALLBACK",
        )


def test_sync_starts_idempotent_automatic_draft_for_every_synced_inquiry(
    database: Database,
) -> None:
    automatic = RecordingAutomaticDrafts()
    service = InquirySyncService(
        InquiryRepository(database),
        WorkflowRepository(database),
        LogRepository(database),
        automatic_drafts=automatic,
        automatic_processing_enabled=lambda: True,
    )

    first = service.sync([_work_item("SYNC-AUTO")], correlation_id="sync-1")
    unchanged = service.sync(
        [_work_item("SYNC-AUTO")], correlation_id="sync-2"
    )
    updated_item = _work_item("SYNC-AUTO")
    updated_item["content"] = "HDMI 연결 방법을 알려주세요."
    updated = service.sync([updated_item], correlation_id="sync-3")

    assert first["new"] == 1
    assert unchanged["unchanged"] == 1
    assert updated["updated"] == 1
    assert len(automatic.calls) == 3


def test_delivery_sync_automatically_runs_order_dps_and_answer(
    database: Database,
) -> None:
    sequence: list[str] = []

    class OrderLookup:
        def lookup_for_inquiry(self, inquiry_id: int, **kwargs):
            sequence.append("ORDER_LOOKUP")
            return {
                "success": True,
                "orders": [{"order_id": "2026080612345678"}],
            }

    class DpsLookup:
        def enrich(self, request, **kwargs):
            sequence.append("DPS_LOOKUP")
            metadata = {
                "lookup_required": True,
                "lookup_status": "SUCCESS",
                "installation_date": "2026-08-10",
                "required_delivery_date": "2026-08-10",
                "date_parse_status": "PARSED",
                "source": "DPS_AGENT",
            }
            request.metadata["dps"] = metadata
            return SimpleNamespace(
                decision=SimpleNamespace(lookup_required=True),
                metadata=metadata,
                lookup_row=None,
            )

        def skip_for_phase9(self, request, **kwargs):
            raise AssertionError("배송문의 DPS 자동조회를 건너뛰면 안 됩니다.")

    class ForbiddenProvider:
        def generate(self, *args, **kwargs):
            raise AssertionError("배송 확정일 답변은 Template/GPT를 호출하면 안 됩니다.")

    delivery = _work_item("AUTO-DELIVERY")
    delivery.update(
        {
            "title": "배송 문의",
            "content": "설치 예정일이 언제인가요?",
            "order_id": "2026080612345678",
        }
    )
    answer_service = AnswerService(
        database,
        engine=ForbiddenProvider(),
        hybrid_service=ForbiddenProvider(),
        dps_enrichment=DpsLookup(),
        order_lookup_service=OrderLookup(),
    )
    sync = InquirySyncService(
        InquiryRepository(database),
        WorkflowRepository(database),
        LogRepository(database),
        automatic_drafts=AutomaticDraftService(
            database, answer_service=answer_service
        ),
        automatic_processing_enabled=lambda: True,
    )

    result = sync.sync([delivery], correlation_id="automatic-delivery")

    assert result["new"] == 1
    assert sequence == ["ORDER_LOOKUP", "DPS_LOOKUP"]
    inquiry = InquiryRepository(database).get_by_source(
        "OJE_PLUS", "PRODUCT_INQUIRY", "AUTO-DELIVERY"
    )
    active = AnswerRepository(database).active_for_inquiry(inquiry["id"])
    assert active["metadata_json"]["selected_answer_route"] == (
        "DELIVERY_WITH_INSTALLATION_DATE"
    )
    assert active["metadata_json"]["gpt_called"] is False
    assert active["original_answer"].count(DEFAULT_PREFIX) == 1
    assert active["original_answer"].count(FINAL_FALLBACK_NOTICE) == 1
