from __future__ import annotations

from dataclasses import dataclass

import pytest

from api.naver_answer_client import (
    NaverAnswerClientError,
    NaverAnswerResponse,
)
from answer.models import AnswerResult, AnswerStatus
from config import NaverPostSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.post_review_repository import PostReviewRepository
from repositories.workflow_repository import WorkflowRepository
from services.auto_post_pipeline_service import (
    AUTO_POST_ROUTES,
    AutoPostPipelineService,
)
from services.naver_post_service import NaverPostService
from services.naver_auto_post_scheduler import NaverAutoPostScheduler
from services.post_review_service import PostReviewService
from answer.answer_format import (
    DEFAULT_CLOSING, DEFAULT_PREFIX, FINAL_FALLBACK_NOTICE,
    extract_answer_body, format_auto_answer, format_final_answer,
)


@dataclass
class MockClient:
    error: Exception | None = None
    calls: int = 0
    requests: list | None = None

    def send(self, request, *, access_token):
        self.calls += 1
        if self.requests is None:
            self.requests = []
        self.requests.append(request)
        assert access_token == "mock-token"
        if self.error:
            raise self.error
        return NaverAnswerResponse(204 if request.source_type == "PRODUCT_INQUIRY" else 200, "ANSWER-1")


def make_database(tmp_path) -> Database:
    database = Database(tmp_path / "auto-post.db")
    database.initialize()
    return database


def make_inquiry(
    database: Database,
    *,
    external_id: str = "Q-1",
    source_type: str = "PRODUCT_INQUIRY",
    answered: bool = False,
) -> int:
    id_field = "questionId" if source_type == "PRODUCT_INQUIRY" else "inquiryNo"
    result = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": source_type,
            "source_question_id": external_id,
            "external_inquiry_id": external_id,
            "inquiry_type": source_type,
            "title": "상품 문의",
            "content": "사용 방법을 알려주세요",
            "product_name": "스마트모니터",
            "source_answered": answered,
            "answer_status": "ANSWERED" if answered else "UNANSWERED",
            "post_status": "POSTED" if answered else "NOT_POSTED",
            "raw_json": {
                "source": source_type,
                id_field: external_id,
                "source_payload": {id_field: external_id},
            },
        }
    )
    WorkflowRepository(database).initialize_steps(result.inquiry_id)
    return result.inquiry_id


def make_draft(
    database: Database,
    inquiry_id: int,
    *,
    route: str,
    answer: str = "문의하신 기능은 설정 메뉴에서 이용할 수 있습니다.",
    needs_review: bool = False,
    dps_metadata: dict | None = None,
) -> dict:
    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.NEEDS_REVIEW if needs_review else AnswerStatus.GENERATED,
            category="일반",
            reason="테스트",
            answer=answer,
            provider="rules",
            auto_answerable=not needs_review,
            needs_review=needs_review,
            metadata={
                "selected_answer_route": route,
                "generation_mode": route,
                "requires_manual_review": needs_review,
                "validator_result": {"status": "PASS", "passed": True},
                **(
                    {
                        "processing_plan": {
                            "requires_dps_lookup": True,
                            "needs_staff_review": True,
                        },
                        "dps": dps_metadata,
                    }
                    if dps_metadata is not None
                    else {}
                ),
            },
        ),
    )


def test_dps_required_login_required_blocks_auto_post(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(
        database,
        inquiry_id,
        route="DPS_LOOKUP_FAILED",
        dps_metadata={
            "lookup_required": True,
            "lookup_status": "AUTOMATION_ERROR",
            "error_code": "DPS_LOGIN_REQUIRED",
        },
    )
    client = MockClient()

    outcome = AutoPostPipelineService(
        database,
        post_service=post_service(database, client),
        dps_status_provider=lambda: {"session_status": "LOGIN_REQUIRED"},
    ).run_pending(run_id="RUN-DPS", owner_id="OWNER-DPS", max_retries=1)

    assert outcome.skipped_count == 1
    assert outcome.succeeded_count == 0
    assert client.calls == 0
    assert InquiryRepository(database).get(inquiry_id)["workflow_status"] == "NEEDS_ATTENTION"


def test_dps_not_required_is_unaffected_by_login_required(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")
    client = MockClient()

    outcome = AutoPostPipelineService(
        database,
        post_service=post_service(database, client),
        dps_status_provider=lambda: {"session_status": "LOGIN_REQUIRED"},
    ).run_pending(run_id="RUN-NO-DPS", owner_id="OWNER-NO-DPS", max_retries=1)

    assert outcome.succeeded_count == 1
    assert client.calls == 1


def post_service(database: Database, client: MockClient) -> NaverPostService:
    return NaverPostService(
        database,
        settings=NaverPostSettings(enabled=True),
        store_resolver=lambda _: StoreConfig("OJE_PLUS", "오제", "id", "secret"),
        token_provider=lambda **_: "mock-token",
        client=client,
    )


class MockConfirmation:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, str]] = []

    def confirm(self, inquiry_id: int, *, run_id: str):
        self.calls.append((inquiry_id, run_id))
        if self.error:
            raise self.error
        return {"source_answered": True, "body_matched": True}


def test_posted_answer_is_confirmed_before_pipeline_success(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")
    confirmation = MockConfirmation()
    outcome = AutoPostPipelineService(
        database,
        post_service=post_service(database, MockClient()),
        confirmation_service=confirmation,
    ).run_pending(run_id="RUN-1", owner_id="OWNER-1", max_retries=1)
    assert outcome.succeeded_count == 1
    assert confirmation.calls == [(inquiry_id, "RUN-1")]


def test_remote_body_mismatch_fails_pipeline_and_pauses_runtime(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")
    outcome = AutoPostPipelineService(
        database,
        post_service=post_service(database, MockClient()),
        confirmation_service=MockConfirmation(RuntimeError("REMOTE_ANSWER_MISMATCH")),
    ).run_pending(run_id="RUN-1", owner_id="OWNER-1", max_retries=1)
    assert outcome.succeeded_count == 0
    assert outcome.failed_count == 1
    assert AutoPostRepository(database).settings()["enabled"] is False


@pytest.mark.parametrize("route", sorted(AUTO_POST_ROUTES))
def test_every_supported_route_is_auto_finalized_posted_and_queued_for_review(
    tmp_path, route: str,
) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(
        database, inquiry_id, route=route,
        needs_review=False,
    )
    client = MockClient()
    outcome = AutoPostPipelineService(
        database, post_service=post_service(database, client)
    ).run_pending(run_id="RUN-1", owner_id="OWNER-1", max_retries=1)
    assert outcome.succeeded_count == 1
    inquiry = InquiryRepository(database).get(inquiry_id)
    assert inquiry["post_status"] == "POSTED"
    assert inquiry["approval_status"] == "PENDING"
    draft = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert draft["review_status"] == "AUTO_FINALIZED"
    assert draft["final_answer"].startswith("♣♧안녕하세요♧♣")
    assert draft["final_answer"].count("오제 챗봇(Chat Bot)이 답변드립니다.") == 1
    assert draft["final_answer"].count(DEFAULT_PREFIX) == 1
    assert draft["final_answer"].count(FINAL_FALLBACK_NOTICE) == 1
    assert draft["final_answer"].endswith("감사합니다.")
    assert client.requests[0].final_answer == draft["final_answer"]
    payload_field = (
        "commentContent"
        if client.requests[0].source_type == "PRODUCT_INQUIRY"
        else "answerComment"
    )
    assert client.requests[0].payload[payload_field] == draft["final_answer"]
    review = PostReviewRepository(database).get(inquiry_id)
    assert review["status"] == "AUTO_POSTED_UNREVIEWED"
    assert client.calls == 1
    with database.connection() as connection:
        attempt = connection.execute(
            "SELECT * FROM naver_post_attempts WHERE inquiry_id=?", (inquiry_id,)
        ).fetchone()
    assert attempt["auto_post_run_id"] == "RUN-1"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("고객님 {name} 확인 바랍니다.", "UNRESOLVED_PLACEHOLDER"),
        ("문의는 010-1234-5678로 주세요.", "PII_EXPOSURE"),
        ("Authorization: Bearer abcdefghijk", "SECRET_EXPOSURE"),
    ],
)
def test_minimum_technical_validation_blocks_without_network(
    tmp_path, answer: str, expected: str,
) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    if answer:
        make_draft(database, inquiry_id, route="TEMPLATE", answer=answer)
    client = MockClient()
    outcome = AutoPostPipelineService(
        database, post_service=post_service(database, client)
    ).run_pending(run_id="RUN-1", owner_id="OWNER-1", max_retries=1)
    assert outcome.skipped_count == 1
    assert client.calls == 0
    events = [
        row["details_json"]
        for row in __import__("repositories.log_repository", fromlist=["LogRepository"]).LogRepository(database).recent_for_inquiry(inquiry_id)
    ]
    assert expected in str(events)


def test_empty_answer_is_rejected_by_technical_validator() -> None:
    from services.auto_post_validation_service import AutoPostTechnicalValidator

    result = AutoPostTechnicalValidator().validate_answer("   ")
    assert result.passed is False
    assert "FINAL_ANSWER_REQUIRED" in result.errors


def test_post_unknown_is_not_selected_for_retry_and_restart_recovers_posting(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")
    client = MockClient(NaverAnswerClientError("API_TIMEOUT", uncertain=True))
    outcome = AutoPostPipelineService(
        database, post_service=post_service(database, client)
    ).run_pending(run_id="RUN-1", owner_id="OWNER-1", max_retries=3)
    assert outcome.unknown_count == 1
    assert InquiryRepository(database).get(inquiry_id)["post_status"] == "POST_UNKNOWN"
    assert AutoPostRepository(database).candidates(max_retries=3) == []
    second = AutoPostPipelineService(
        database, post_service=post_service(database, client)
    ).run_pending(run_id="RUN-2", owner_id="OWNER-2", max_retries=3)
    assert second.processed_count == 0
    assert client.calls == 1


def test_one_failure_does_not_stop_next_inquiry(tmp_path) -> None:
    database = make_database(tmp_path)
    first = make_inquiry(database, external_id="Q-1")
    second = make_inquiry(database, external_id="Q-2")
    make_draft(database, first, route="TEMPLATE", answer="{name} 확인")
    make_draft(database, second, route="TEMPLATE")
    client = MockClient()
    outcome = AutoPostPipelineService(
        database, post_service=post_service(database, client)
    ).run_pending(run_id="RUN-1", owner_id="OWNER-1", max_retries=1)
    assert outcome.skipped_count == 1
    assert outcome.succeeded_count == 1
    assert InquiryRepository(database).get(second)["post_status"] == "POSTED"


def test_staff_correction_creates_versions_and_highest_priority_learning(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")
    client = MockClient()
    service = post_service(database, client)
    AutoPostPipelineService(database, post_service=service).run_pending(
        run_id="RUN-1", owner_id="OWNER-1", max_retries=1
    )
    result = service.correct(
        inquiry_id,
        edited_answer="직원이 확인한 최종 사용 방법입니다.",
        actor="직원A",
    )
    assert result.status == "CORRECTED_AND_REPOSTED"
    versions = PostReviewRepository(database).versions(inquiry_id)
    assert [item["version_number"] for item in versions] == [1, 2, 3]
    assert versions[1]["naver_status"] == "CORRECTION_PENDING"
    assert versions[2]["naver_status"] == "POSTED"
    learning = LearningRepository(database).candidates(store_code="OJE_PLUS")
    assert learning[0]["learning_source"] == "AUTO_POST_CORRECTED"
    assert learning[0]["rating"] == 5


def test_failed_staff_correction_preserves_original_and_is_not_learned(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")
    initial_client = MockClient()
    AutoPostPipelineService(
        database, post_service=post_service(database, initial_client)
    ).run_pending(run_id="RUN-1", owner_id="OWNER-1", max_retries=1)
    failing = MockClient(NaverAnswerClientError("INVALID_REQUEST", http_status=400))
    result = post_service(database, failing).correct(
        inquiry_id, edited_answer="수정 시도 답변입니다.", actor="직원A"
    )
    assert result.status == "CORRECTION_FAILED"
    versions = PostReviewRepository(database).versions(inquiry_id)
    assert len(versions) == 2
    assert PostReviewRepository(database).get(inquiry_id)["status"] == "CORRECTION_FAILED"
    assert LearningRepository(database).count() == 0


def test_wrapper_is_idempotent_and_applied_once() -> None:
    wrapped = format_final_answer("본문 안내입니다.")
    assert wrapped == (
        "♣♧안녕하세요♧♣\n"
        "오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
        "본문 안내입니다.\n\n"
        "안내드린 내용이 문의하신 내용과 다른 경우,\n"
        "네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다.\n\n"
        "감사합니다."
    )
    assert format_final_answer(wrapped) == wrapped
    assert wrapped.count(DEFAULT_PREFIX) == 1
    assert wrapped.count(DEFAULT_CLOSING) == 1


@pytest.mark.parametrize(
    "legacy_answer",
    [
        "안녕하세요, 고객님.\n삼성공식파트너 오제입니다.\n\n본문 안내입니다.\n\n감사합니다.",
        "안녕하세요. 오제 챗봇입니다.\n삼성공식파트너 오제입니다.\n\n본문 안내입니다.\n\n감사합니다.",
        (
            "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
            "본문 안내입니다.\n\n"
            "안내드린 내용이 문의하신 내용과 다른 경우,\n"
            "네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다.\n\n"
            "감사합니다."
        ),
        format_auto_answer("본문 안내입니다."),
    ],
)
def test_legacy_wrappers_are_replaced_without_changing_body(legacy_answer: str) -> None:
    wrapped = format_final_answer(legacy_answer)
    assert extract_answer_body(wrapped) == "본문 안내입니다."
    assert wrapped.count(DEFAULT_PREFIX) == 1
    assert wrapped.count(FINAL_FALLBACK_NOTICE) == 1
    assert "삼성공식파트너 오제입니다." not in wrapped


def test_wrapper_cleanup_does_not_remove_matching_lines_inside_body() -> None:
    body = "첫 문장입니다.\n안녕하세요, 고객님.\n감사합니다.\n마지막 문장입니다."
    assert extract_answer_body(format_final_answer(body)) == body


def test_empty_body_does_not_become_a_wrapper_only_final_answer() -> None:
    assert format_final_answer("") == ""
    assert format_final_answer("안녕하세요, 고객님.\n\n감사합니다.") == ""


def test_legacy_order_id_metadata_is_read_as_order_id_request() -> None:
    assert AutoPostPipelineService._route(
        {
            "metadata_json": {
                "generation_mode": "SAFE_TEMPLATE",
                "answer_type": "order_id_required",
                "template_id": "PHASE9_REQUEST_ORDER_ID",
            }
        }
    ) == "ORDER_ID_REQUEST"


def test_post_failed_retries_only_within_configured_limit(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")
    client = MockClient(NaverAnswerClientError("NETWORK_ERROR", retryable=True))
    pipeline = AutoPostPipelineService(
        database, post_service=post_service(database, client)
    )
    assert pipeline.run_pending(
        run_id="RUN-1", owner_id="OWNER-1", max_retries=1
    ).failed_count == 1
    assert pipeline.run_pending(
        run_id="RUN-2", owner_id="OWNER-2", max_retries=1
    ).failed_count == 1
    third = pipeline.run_pending(
        run_id="RUN-3", owner_id="OWNER-3", max_retries=1
    )
    assert third.processed_count == 0
    assert client.calls == 2


def test_inquiry_lock_prevents_concurrent_owner(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    repository = AutoPostRepository(database)
    assert repository.acquire_inquiry_lock(
        inquiry_id=inquiry_id, store_code="OJE_PLUS", external_id="Q-1",
        owner_id="OWNER-1", run_id="RUN-1",
    ) is True
    assert repository.acquire_inquiry_lock(
        inquiry_id=inquiry_id, store_code="OJE_PLUS", external_id="Q-1",
        owner_id="OWNER-2", run_id="RUN-2",
    ) is False


def test_process_restart_recovers_stale_posting_as_unknown(tmp_path) -> None:
    import hashlib
    from repositories.naver_post_repository import NaverPostRepository

    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    draft = make_draft(database, inquiry_id, route="TEMPLATE")
    version, finalized = PostReviewRepository(database).finalize_auto(
        inquiry_id=inquiry_id, draft_id=draft["id"], run_id="RUN-1"
    )
    NaverPostRepository(database).acquire(
        inquiry_id=inquiry_id, draft_id=finalized["id"], idempotency_key="KEY-1",
        external_id="Q-1", store_code="OJE_PLUS",
        source_type="PRODUCT_INQUIRY", method="PUT",
        endpoint_kind="PRODUCT_INQUIRY_ANSWER",
        final_answer_hash=hashlib.sha256(
            finalized["final_answer"].encode("utf-8")
        ).hexdigest(),
        payload_hash="b" * 64,
        actor="SYSTEM_AUTO_POST", allow_unapproved=True,
        auto_post_run_id="RUN-1",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE naver_post_attempts SET started_at='2000-01-01T00:00:00+00:00' "
            "WHERE inquiry_id=?", (inquiry_id,),
        )
    assert AutoPostRepository(database).recover_stale_posting() == 1
    assert InquiryRepository(database).get(inquiry_id)["post_status"] == "POST_UNKNOWN"
    assert AutoPostRepository(database).candidates(max_retries=10) == []


def test_review_without_change_is_learned_but_unreviewed_is_not(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")
    AutoPostPipelineService(
        database, post_service=post_service(database, MockClient())
    ).run_pending(run_id="RUN-1", owner_id="OWNER-1", max_retries=1)
    assert LearningRepository(database).count() == 0
    PostReviewService(database).complete_without_change(
        inquiry_id=inquiry_id, actor="직원A"
    )
    item = LearningRepository(database).candidates(store_code="OJE_PLUS")[0]
    assert item["learning_source"] == "AUTO_POST_REVIEWED_NO_CHANGE"
    assert item["rating"] == 4


def test_customer_correction_uses_update_endpoint_and_new_version(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(
        database, external_id="C-1", source_type="CUSTOMER_INQUIRY"
    )
    make_draft(database, inquiry_id, route="TEMPLATE")
    client = MockClient()
    service = post_service(database, client)
    AutoPostPipelineService(database, post_service=service).run_pending(
        run_id="RUN-1", owner_id="OWNER-1", max_retries=1
    )
    result = service.correct(
        inquiry_id, edited_answer="직원이 수정한 안내입니다.", actor="직원A"
    )
    assert result.status == "CORRECTED_AND_REPOSTED"
    request = client.requests[-1]
    assert request.method == "PUT"
    assert request.endpoint.endswith("/inquiries/C-1/answer/ANSWER-1")
    assert request.payload["answerComment"] == format_final_answer(
        "직원이 수정한 안내입니다."
    )


def test_scheduler_requires_both_environment_flags_and_persists_run(
    tmp_path, monkeypatch,
) -> None:
    database = make_database(tmp_path)
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )

    class FakePipeline:
        def run_pending(self, **kwargs):
            from services.auto_post_pipeline_service import AutoPostRunResult
            return AutoPostRunResult(processed_count=1, succeeded_count=1)

    scheduler = NaverAutoPostScheduler(
        database, pipeline_factory=lambda _: FakePipeline(), owner_id="OWNER-1"
    )
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "false")
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    assert scheduler.run_once()["status"] == "DISABLED"
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    result = scheduler.run_once()
    assert result["status"] == "SUCCESS"
    assert result["succeeded_count"] == 1
    with database.connection() as connection:
        run = connection.execute(
            "SELECT * FROM naver_auto_post_runs WHERE run_id=?",
            (result["auto_post_run_id"],),
        ).fetchone()
    assert run["status"] == "SUCCESS"


def test_environment_permission_does_not_auto_enable_persisted_scheduler(
    tmp_path,
) -> None:
    database = make_database(tmp_path)
    settings = AutoPostRepository(database).ensure_settings(
        enabled=True, interval_minutes=10, max_retries=1
    )
    assert settings["enabled"] is False
