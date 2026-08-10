from __future__ import annotations

from datetime import UTC, datetime

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from services.approval_service import ApprovalService
from services.inquiry_metadata_repair_service import (
    InquiryMetadataRepairService,
    protected_state_fingerprint,
)
from services.inquiry_sync_service import normalize_work_item
from services.naver_inquiry_normalizer import InquiryNormalizer
from ui.dashboard import (
    UNCLASSIFIED_FILTER_VALUE,
    metadata_filter_matches,
)


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "metadata.db")
    database.initialize()
    return database


def _normalized_product(question_id: str, content: str = "일반 상품 문의"):
    return normalize_work_item(
        InquiryNormalizer().product(
            {
                "questionId": question_id,
                "question": content,
                "productId": "P-1",
                "productName": "TV",
                "answered": False,
                "createDate": "2026-07-31T10:00:00+09:00",
                "status": "WAITING",
            },
            store_code="STORE",
        ).to_work_item()
    )


def test_new_sync_keeps_queue_priority_and_analysis(tmp_path) -> None:
    database = _database(tmp_path)
    result = InquiryRepository(database).upsert_work_item(
        _normalized_product("Q-1")
    )
    raw = InquiryRepository(database).get(result.inquiry_id)["raw_json"]

    assert raw["queue"] == "GENERAL_INQUIRY"
    assert raw["priority"] == "NORMAL"
    assert isinstance(raw["analysis"], dict)
    assert raw["source_payload"]["questionId"] == "Q-1"


def test_resync_preserves_arbitrary_local_snapshots(tmp_path) -> None:
    database = _database(tmp_path)
    repository = InquiryRepository(database)
    item = _normalized_product("Q-1")
    item["raw_json"].update(
        {
            "staff_metadata": {"owner": "local"},
            "validation_metadata": {"valid": True},
            "order_lookup": {"lookup_type": "ORDER_ID"},
            "dps_result": {"date": "2026-08-01"},
        }
    )
    first = repository.upsert_work_item(item)

    outcome = repository.upsert_work_item(_normalized_product("Q-1"))
    raw = repository.get(first.inquiry_id)["raw_json"]

    assert outcome.outcome == "unchanged"
    assert raw["staff_metadata"] == {"owner": "local"}
    assert raw["validation_metadata"] == {"valid": True}
    assert raw["order_lookup"]["lookup_type"] == "ORDER_ID"
    assert raw["dps_result"]["date"] == "2026-08-01"


def test_none_metadata_does_not_delete_valid_existing_values(tmp_path) -> None:
    database = _database(tmp_path)
    repository = InquiryRepository(database)
    item = _normalized_product("Q-1")
    first = repository.upsert_work_item(item)
    incoming = _normalized_product("Q-1")
    for field in ("queue", "priority", "analysis"):
        incoming["raw_json"][field] = None

    outcome = repository.upsert_work_item(incoming)
    raw = repository.get(first.inquiry_id)["raw_json"]

    assert outcome.outcome == "unchanged"
    assert raw["queue"] == "GENERAL_INQUIRY"
    assert raw["priority"] == "NORMAL"
    assert isinstance(raw["analysis"], dict)


def test_metadata_enrichment_is_unchanged_but_source_change_is_updated(
    tmp_path,
) -> None:
    database = _database(tmp_path)
    repository = InquiryRepository(database)
    original = _normalized_product("Q-1")
    original["raw_json"] = {"questionId": "Q-1"}
    repository.upsert_work_item(original)

    enriched = repository.upsert_work_item(_normalized_product("Q-1"))
    changed_item = _normalized_product("Q-1", "변경된 상품 문의 본문")
    changed = repository.upsert_work_item(changed_item)

    assert enriched.outcome == "unchanged"
    assert changed.outcome == "updated"


def test_source_refresh_omission_preserves_existing_order_identifiers(
    tmp_path,
) -> None:
    database = _database(tmp_path)
    repository = InquiryRepository(database)
    original = _normalized_product("Q-ORDER")
    original["order_id"] = "2026073112345678"
    original["product_order_id"] = "2026073112345679"
    created = repository.upsert_work_item(original)

    refresh = _normalized_product("Q-ORDER")
    refresh["order_id"] = None
    refresh["product_order_id"] = None
    outcome = repository.upsert_work_item(refresh)
    saved = repository.get(created.inquiry_id)

    assert outcome.outcome == "unchanged"
    assert saved["order_id"] == "2026073112345678"
    assert saved["product_order_id"] == "2026073112345679"


def test_unclassified_filter_is_included_by_default_and_excludable() -> None:
    assert metadata_filter_matches(
        None, ["GENERAL_INQUIRY", UNCLASSIFIED_FILTER_VALUE]
    )
    assert not metadata_filter_matches(None, ["GENERAL_INQUIRY"])
    assert metadata_filter_matches(None, [])


def test_repair_dry_run_reports_284_without_writing(tmp_path) -> None:
    database = _database(tmp_path)
    repository = InquiryRepository(database)
    for index in range(284):
        item = _normalized_product(f"Q-{index}")
        item["raw_json"] = {
            "source_payload": {"questionId": f"Q-{index}"}
        }
        repository.upsert_work_item(item)

    result = InquiryMetadataRepairService(database).run(dry_run=True)

    assert result.target_count == 284
    assert result.queue_recoverable_count == 284
    assert result.priority_recoverable_count == 284
    assert result.analysis_recoverable_count == 284
    assert result.repaired_count == 284
    assert result.unclassified_count == 0
    assert result.protected_fields_changed is False
    assert all(
        "queue" not in item["raw_json"] for item in repository.list(limit=300)
    )


def test_repair_preserves_draft_final_approval_dps_and_order_snapshot(
    tmp_path,
) -> None:
    database = _database(tmp_path)
    repository = InquiryRepository(database)
    item = _normalized_product("Q-1", "배송 일정 문의")
    item["raw_json"] = {"order_lookup": {"product_name": "TV"}}
    inserted = repository.upsert_work_item(item)
    inquiry_id = inserted.inquiry_id
    repository.update_order_snapshot(
        inquiry_id,
        order_id="ORDER-1",
        product_order_id=None,
        order_date="2026-07-30",
        product_name="TV",
        order_status="PAYED",
        lookup_at="2026-07-31T10:00:00+09:00",
        lookup_type="ORDER_ID",
        cached=False,
    )
    answers = AnswerRepository(database)
    draft = answers.create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="배송",
            reason="test",
            answer="보존할 답변",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        actor="관리자",
    )
    DpsRepository(database).create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="ORDER-1",
        lookup_status="SUCCESS",
        raw_result={"date": "2026-08-01"},
        normalized_result={"required_delivery_date": "2026-08-01"},
        queried_at=datetime.now(UTC).isoformat(),
    )
    before = protected_state_fingerprint(database)

    result = InquiryMetadataRepairService(database).run(dry_run=False)
    after = protected_state_fingerprint(database)

    assert result.repaired_count == 1
    assert result.protected_fields_changed is False
    assert before == after
    assert answers.active_for_inquiry(inquiry_id)["id"] == draft["id"]
    from answer.answer_format import format_final_answer
    assert answers.get(draft["id"])["final_answer"] == format_final_answer(
        "보존할 답변"
    )
    assert repository.get(inquiry_id)["order_id"] == "ORDER-1"
    assert (
        DpsRepository(database).get_latest_by_inquiry_id(inquiry_id)
        is not None
    )


def test_dashboard_uses_count_and_database_pagination_without_500_cap(
    tmp_path,
) -> None:
    database = _database(tmp_path)
    repository = InquiryRepository(database)
    for index in range(620):
        item = _normalized_product(f"Q-{index}")
        item["registered_at"] = (
            f"2026-07-{1 + index % 30:02d}T"
            f"{index % 24:02d}:{index % 60:02d}:00+09:00"
        )
        item["source_created_at"] = item["registered_at"]
        repository.upsert_work_item(item)

    rows, total, total_pages = repository.dashboard_page(
        store_codes=["STORE"],
        source="ALL",
        queues=["GENERAL_INQUIRY", UNCLASSIFIED_FILTER_VALUE],
        priorities=["NORMAL", UNCLASSIFIED_FILTER_VALUE],
        answer_status="ALL",
        delivery_only=False,
        search_query="",
        start_date="2026-07-01",
        end_date="2026-07-31",
        kpi_filter=None,
        page=2,
        page_size=20,
    )

    assert total == 620
    assert total_pages == 31
    assert len(rows) == 20
    assert repository.count() == 620
