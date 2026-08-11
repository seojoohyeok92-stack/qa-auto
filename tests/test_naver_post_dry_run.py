from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from answer.answer_format import format_final_answer
from config import StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from services.approval_service import ApprovalService
from services.naver_post_dry_run_service import (
    NAVER_POST_LOCK,
    NaverPostDryRunService,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "dry-run.db")
    value.initialize()
    return value


@pytest.fixture(autouse=True)
def disable_live_post_for_dry_run_tests(monkeypatch) -> None:
    monkeypatch.setenv("NAVER_POST_ENABLED", "false")


def _store(_: str | None = None) -> StoreConfig:
    return StoreConfig("STORE", "스토어", "client", "secret", True)


def _inquiry(
    database: Database,
    *,
    source_type: str = "PRODUCT_INQUIRY",
    external_id: str = "12345",
    answer: str = "안내 <완료> & \"확인\"",
    approve: bool = True,
) -> int:
    id_field = "questionId" if source_type == "PRODUCT_INQUIRY" else "inquiryNo"
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": source_type,
            "source_question_id": external_id,
            "external_inquiry_id": external_id,
            "raw_json": {
                "source": source_type,
                id_field: external_id,
                "source_payload": {id_field: external_id},
            },
            "content": "문의",
        }
    ).inquiry_id
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="기타",
            reason="test",
            answer=answer,
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    if approve:
        ApprovalService(database).approve(
            inquiry_id=inquiry_id,
            draft_id=int(draft["id"]),
            actor="tester",
        )
    return inquiry_id


def test_dry_run_builds_escaped_payload_without_http_write(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database)
    service = NaverPostDryRunService(database, store_resolver=_store)

    with (
        patch("requests.post") as post,
        patch("requests.put") as put,
        patch("requests.patch") as patch_request,
    ):
        result = service.run(inquiry_id)

    assert result.eligible is True
    assert result.status == "READY"
    assert result.method == "PUT"
    assert result.endpoint.endswith("/v1/contents/qnas/12345")
    assert result.payload == {
        "commentContent": format_final_answer('안내 <완료> & "확인"'),
    }
    assert result.authorization["prepared"] is True
    assert result.authorization["network_call"] is False
    assert result.post_locked is NAVER_POST_LOCK is True
    post.assert_not_called()
    put.assert_not_called()
    patch_request.assert_not_called()
    events = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(
            inquiry_id, limit=20
        )
    }
    assert {"POST_LOCKED", "PAYLOAD_CREATED", "DRY_RUN_SUCCESS"} <= events


def test_customer_inquiry_preview_uses_post_but_never_calls_it(
    database: Database,
) -> None:
    inquiry_id = _inquiry(
        database,
        source_type="CUSTOMER_INQUIRY",
        external_id="98765",
    )
    with patch("requests.post") as post:
        result = NaverPostDryRunService(
            database, store_resolver=_store
        ).run(inquiry_id)
    assert result.method == "POST"
    assert result.endpoint.endswith(
        "/v1/pay-merchant/inquiries/98765/answer"
    )
    assert result.payload["answerComment"] == format_final_answer(
        '안내 <완료> & "확인"'
    )
    post.assert_not_called()


def test_unapproved_and_missing_final_answer_are_not_eligible(
    database: Database,
) -> None:
    unapproved_id = _inquiry(database, approve=False)
    unapproved = NaverPostDryRunService(
        database, store_resolver=_store
    ).run(unapproved_id)
    assert unapproved.eligible is False
    assert "승인되지 않음" in unapproved.reasons
    assert unapproved.payload is None

    missing_final_id = _inquiry(database, approve=True)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE answer_drafts SET final_answer=NULL WHERE inquiry_id=?",
            (missing_final_id,),
        )
    missing = NaverPostDryRunService(
        database, store_resolver=_store
    ).run(missing_final_id)
    assert missing.eligible is False
    assert "Final Answer 없음" in missing.reasons
    events = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(
            missing_final_id, limit=20
        )
    }
    assert {"POST_LOCKED", "VALIDATION_FAILED", "DRY_RUN_FAILED"} <= events


def test_posted_and_payload_validation_failures_are_blocked(
    database: Database,
) -> None:
    posted_id = _inquiry(database)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET post_status='POSTED' WHERE id=?",
            (posted_id,),
        )
    posted = NaverPostDryRunService(
        database, store_resolver=_store
    ).run(posted_id)
    assert posted.eligible is False
    assert "이미 등록됨" in posted.reasons

    unsupported_id = _inquiry(database, source_type="UNKNOWN")
    unsupported = NaverPostDryRunService(
        database, store_resolver=_store
    ).run(unsupported_id)
    assert unsupported.eligible is False
    assert "UNSUPPORTED_SOURCE_TYPE" in unsupported.reasons


def test_sqlite_backup_api_preserves_source_and_rows(
    database: Database, tmp_path: Path
) -> None:
    inquiry_id = _inquiry(database)
    destination = tmp_path / "backup" / "snapshot.db"
    database.backup_to(destination)
    backup = Database(destination)
    assert backup.migration_versions() == database.migration_versions()
    assert InquiryRepository(backup).get(inquiry_id)["source_question_id"] == "12345"
    assert destination.read_bytes() != b""


def test_streamlit_post_prepare_panel_keeps_actual_button_locked(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database)
    code = f'''
import os
os.environ["NAVER_POST_ENABLED"]="false"
from config import NaverPostSettings
NaverPostSettings.from_environment=classmethod(
    lambda cls: cls(enabled=False)
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_naver_post_prepare
db=Database(r"{database.path}")
inquiry=InquiryRepository(db).get({inquiry_id})
_render_naver_post_prepare(db, inquiry)
'''
    app = AppTest.from_string(code).run(timeout=30)
    assert not app.exception
    labels = {button.label: button for button in app.button}
    assert "등록 Dry Run" not in labels
    assert labels["네이버 실제 등록"].disabled is True
    assert any("내부 preflight" in item.value for item in app.info)
