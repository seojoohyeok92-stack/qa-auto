from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest
from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from answer.answer_format import format_final_answer
from api.naver_answer_client import (
    NaverAlreadyAnsweredError,
    NaverAnswerClientError,
    NaverAnswerResponse,
)
from config import NaverPostSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_post_repository import NaverPostRepository
from services.approval_service import ApprovalService
from services.naver_post_service import NaverPostService


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "post-v13.db")
    value.initialize()
    return value


def _store(_: str | None = None) -> StoreConfig:
    return StoreConfig("STORE", "스토어", "client-id", "client-secret", True)


def _approved(
    database: Database,
    *,
    source_type: str = "PRODUCT_INQUIRY",
    external_id: str = "12345",
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
            answer='한글 <확인> & "따옴표" 😀\n둘째 줄',
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=int(draft["id"]),
        actor="approver",
    )
    return inquiry_id


class RecordingClient:
    def __init__(self, outcome=None) -> None:
        self.requests = []
        self.outcome = outcome or NaverAnswerResponse(204, "response-1")

    def send(self, request, *, access_token):
        self.requests.append((request, access_token))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _service(
    database: Database,
    client,
    *,
    enabled: bool,
) -> NaverPostService:
    return NaverPostService(
        database,
        settings=NaverPostSettings(enabled=enabled),
        store_resolver=_store,
        token_provider=lambda **kwargs: "mock-token",
        client=client,
    )


def test_disabled_and_unconfirmed_post_make_zero_network_calls(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    client = RecordingClient()
    disabled = _service(database, client, enabled=False).post(
        inquiry_id, actor="tester", confirmed=True
    )
    unconfirmed = _service(database, client, enabled=True).post(
        inquiry_id, actor="tester", confirmed=False
    )
    assert disabled.status == unconfirmed.status == "BLOCKED"
    assert client.requests == []


@pytest.mark.parametrize(
    ("source_type", "method", "body_key", "success_status"),
    [
        ("PRODUCT_INQUIRY", "PUT", "commentContent", 204),
        ("CUSTOMER_INQUIRY", "POST", "answerComment", 200),
    ],
)
def test_manual_post_uses_official_method_and_raw_json_text(
    database: Database,
    source_type: str,
    method: str,
    body_key: str,
    success_status: int,
) -> None:
    inquiry_id = _approved(database, source_type=source_type)
    client = RecordingClient(
        NaverAnswerResponse(success_status, "answer-id")
    )
    result = _service(database, client, enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert result.status == "POSTED"
    assert len(client.requests) == 1
    request, token = client.requests[0]
    assert request.method == method
    assert request.payload == {
        body_key: format_final_answer('한글 <확인> & "따옴표" 😀\n둘째 줄')
    }
    assert "&lt;" not in request.payload[body_key]
    assert token == "mock-token"
    inquiry = InquiryRepository(database).get(inquiry_id)
    assert inquiry["post_status"] == "POSTED"
    assert inquiry["posted_at"]
    assert inquiry["posted_answer_hash"]
    assert inquiry["posted_draft_id"]
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)[
        "posted"
    ] == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            NaverAnswerClientError(
                "RATE_LIMITED", http_status=429, retryable=True
            ),
            "POST_FAILED",
        ),
        (
            NaverAnswerClientError(
                "API_TIMEOUT", uncertain=True
            ),
            "POST_UNKNOWN",
        ),
    ],
)
def test_failure_and_uncertain_timeout_states(
    database: Database, error: Exception, expected: str
) -> None:
    inquiry_id = _approved(database)
    client = RecordingClient(error)
    result = _service(database, client, enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert result.status == expected
    assert InquiryRepository(database).get(inquiry_id)["post_status"] == expected
    assert NaverPostRepository(database).latest(inquiry_id)["status"] == expected


def test_remote_404_is_terminal_and_scheduler_and_retry_do_not_resend(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    first_client = RecordingClient(
        NaverAnswerClientError(
            "REMOTE_TARGET_NOT_FOUND", http_status=404, retryable=False
        )
    )
    first = _service(database, first_client, enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert first.status == "POST_FAILED"
    assert first.error_code == "REMOTE_TARGET_NOT_FOUND"
    assert len(first_client.requests) == 1
    assert AutoPostRepository(database).candidates(max_retries=10) == []

    retry_client = RecordingClient()
    retry = _service(database, retry_client, enabled=True).post(
        inquiry_id,
        actor="tester",
        confirmed=True,
        retry_requested=True,
    )
    assert retry.status == "BLOCKED"
    assert retry.error_code == "RETRY_PROHIBITED_TARGET_ERROR"
    assert retry_client.requests == []


def test_store_credential_mapping_mismatch_is_blocked_before_network(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    client = RecordingClient()
    service = NaverPostService(
        database,
        settings=NaverPostSettings(enabled=True),
        store_resolver=lambda _: StoreConfig(
            "OTHER_STORE", "other", "id", "secret", True
        ),
        token_provider=lambda **kwargs: "mock-token",
        client=client,
    )
    result = service.post(inquiry_id, actor="tester", confirmed=True)
    assert result.status == "BLOCKED"
    assert result.error_code == "STORE_CREDENTIAL_MISMATCH"
    assert client.requests == []


def test_post_unknown_and_success_cannot_be_retried(
    database: Database,
) -> None:
    unknown_id = _approved(database, external_id="UNKNOWN-1")
    unknown_client = RecordingClient(
        NaverAnswerClientError("API_TIMEOUT", uncertain=True)
    )
    _service(database, unknown_client, enabled=True).post(
        unknown_id, actor="tester", confirmed=True
    )
    second = _service(
        database, RecordingClient(), enabled=True
    ).post(unknown_id, actor="tester", confirmed=True)
    assert second.status == "BLOCKED"

    posted_id = _approved(database, external_id="POSTED-1")
    first_client = RecordingClient()
    service = _service(database, first_client, enabled=True)
    assert service.post(posted_id, actor="tester", confirmed=True).status == "POSTED"
    again = service.post(posted_id, actor="tester", confirmed=True)
    assert again.status == "BLOCKED"
    assert len(first_client.requests) == 1


def test_local_already_answered_blocks_before_network(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_answered=1 WHERE id=?",
            (inquiry_id,),
        )
    client = RecordingClient()
    result = _service(database, client, enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert result.status == "BLOCKED"
    assert result.error_code == "ALREADY_ANSWERED"
    assert client.requests == []


@pytest.mark.parametrize(
    "mutation",
    [
        "approval",
        "final_none",
        "final_blank",
        "store",
        "external_id",
        "source_type",
        "posting",
        "too_long",
    ],
)
def test_invalid_local_conditions_block_before_network(
    database: Database, mutation: str
) -> None:
    inquiry_id = _approved(database)
    with database.transaction() as connection:
        if mutation == "approval":
            connection.execute(
                "UPDATE inquiries SET approval_status='PENDING' WHERE id=?",
                (inquiry_id,),
            )
        elif mutation == "final_none":
            connection.execute(
                "UPDATE answer_drafts SET final_answer=NULL WHERE inquiry_id=?",
                (inquiry_id,),
            )
        elif mutation == "final_blank":
            connection.execute(
                "UPDATE answer_drafts SET final_answer='   ' WHERE inquiry_id=?",
                (inquiry_id,),
            )
        elif mutation == "store":
            connection.execute(
                "UPDATE inquiries SET store_code='' WHERE id=?",
                (inquiry_id,),
            )
        elif mutation == "external_id":
            connection.execute(
                """
                UPDATE inquiries
                SET external_inquiry_id='', source_question_id=''
                WHERE id=?
                """,
                (inquiry_id,),
            )
        elif mutation == "source_type":
            connection.execute(
                "UPDATE inquiries SET source_type='UNKNOWN' WHERE id=?",
                (inquiry_id,),
            )
        elif mutation == "posting":
            connection.execute(
                "UPDATE inquiries SET post_status='POSTING' WHERE id=?",
                (inquiry_id,),
            )
        elif mutation == "too_long":
            connection.execute(
                "UPDATE answer_drafts SET final_answer=? WHERE inquiry_id=?",
                ("가" * 4001, inquiry_id),
            )
    client = RecordingClient()
    result = _service(database, client, enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert result.status == "BLOCKED"
    assert client.requests == []


def test_naver_already_answered_response_updates_local_without_retry(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    client = RecordingClient(
        NaverAlreadyAnsweredError("ALREADY_ANSWERED", http_status=400)
    )
    result = _service(database, client, enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert result.status == "ALREADY_ANSWERED"
    assert len(client.requests) == 1
    assert InquiryRepository(database).get(inquiry_id)["post_status"] == "POSTED"
    again = _service(database, RecordingClient(), enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert again.status == "BLOCKED"


def test_explicit_retry_from_post_failed_can_succeed(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    failed_client = RecordingClient(
        NaverAnswerClientError(
            "NAVER_SERVER_ERROR", http_status=503, retryable=True
        )
    )
    first = _service(database, failed_client, enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    assert first.status == "POST_FAILED"
    retry_client = RecordingClient()
    second = _service(database, retry_client, enabled=True).post(
        inquiry_id,
        actor="tester",
        confirmed=True,
        retry_requested=True,
    )
    assert second.status == "POSTED"
    assert len(retry_client.requests) == 1
    assert NaverPostRepository(database).latest(inquiry_id)[
        "retry_of_attempt_id"
    ] == first.attempt_id


def test_two_service_instances_only_send_once(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    entered = Event()
    release = Event()

    class BlockingClient(RecordingClient):
        def send(self, request, *, access_token):
            self.requests.append((request, access_token))
            entered.set()
            assert release.wait(timeout=10)
            return NaverAnswerResponse(204, "one")

    first_client = BlockingClient()
    first_result = {}

    def run_first():
        first_result["value"] = _service(
            database, first_client, enabled=True
        ).post(inquiry_id, actor="first", confirmed=True)

    thread = Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=10)
    second_client = RecordingClient()
    second = _service(database, second_client, enabled=True).post(
        inquiry_id, actor="second", confirmed=True
    )
    release.set()
    thread.join(timeout=10)
    assert second.status == "BLOCKED"
    assert second_client.requests == []
    assert first_result["value"].status == "POSTED"
    assert len(first_client.requests) == 1


def test_logs_contain_events_but_not_token_or_secret(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    _service(database, RecordingClient(), enabled=True).post(
        inquiry_id, actor="tester", confirmed=True
    )
    logs = LogRepository(database).recent_for_inquiry(
        inquiry_id, limit=100
    )
    events = {row["event_code"] for row in logs}
    assert {
        "NAVER_POST_REQUESTED",
        "NAVER_POST_STARTED",
        "NAVER_POST_SUCCEEDED",
    } <= events
    rendered = str(logs)
    assert "mock-token" not in rendered
    assert "client-secret" not in rendered


def test_streamlit_requires_second_explicit_confirmation(
    database: Database,
) -> None:
    inquiry_id = _approved(database)
    app = AppTest.from_string(
        f'''
from types import SimpleNamespace
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
import ui.review_workspace as workspace

class FakeDry:
    def __init__(self, database): pass
    def run(self, inquiry_id):
        return SimpleNamespace(to_dict=lambda: {{
          "eligible":True,"status":"READY","method":"PUT",
          "endpoint":"/mock","payload":{{"commentContent":"answer"}},
          "post_locked":False,"reasons":[]
        }})

class FakePost:
    def __init__(self, database): pass
    def post(self, inquiry_id, **kwargs):
        return SimpleNamespace(to_dict=lambda: {{
          "status":"POSTED","error_code":None,"message":"ok"
        }})

class FakeSettings:
    @classmethod
    def from_environment(cls):
        return SimpleNamespace(enabled=True)

workspace.NaverPostDryRunService=FakeDry
workspace.NaverPostService=FakePost
workspace.NaverPostSettings=FakeSettings
db=Database(r"{database.path}")
workspace._render_naver_post_prepare(
    db, InquiryRepository(db).get({inquiry_id})
)
'''
    ).run(timeout=30)
    assert not app.exception
    actual = next(
        button
        for button in app.button
        if button.label == "\ub124\uc774\ubc84 \uc2e4\uc81c \ub4f1\ub85d"
    )
    assert actual.disabled is False
    actual.click()
    app = app.run(timeout=30)
    labels = {button.label for button in app.button}
    assert {
        "\ucde8\uc18c",
        "\uc2e4\uc81c \ub4f1\ub85d \ud655\uc778",
    } <= labels
    next(
        button
        for button in app.button
        if button.label == "\uc2e4\uc81c \ub4f1\ub85d \ud655\uc778"
    ).click()
    app = app.run(timeout=30)
    assert not app.exception
    assert any(
        "\ub124\uc774\ubc84 \ub4f1\ub85d \uc644\ub8cc" in item.value
        for item in app.success
    )
