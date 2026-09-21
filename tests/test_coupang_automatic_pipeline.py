"""Coupang on the existing automatic path: collect, draft, decide, post.

Nothing here is a Coupang pipeline.  It is the Naver one -- the same event
outbox, the same AutomaticDraftService, the same eligibility verdict, the same
queue -- with Coupang admitted to the market gates and one dispatch at the very
end choosing which post client sends the request.

The post client is either a recorder or the real service on a fake wire, and
every draft is prepared up front, so no Coupang request, no Naver request, no
GPT call and no Kakao send leaves the process.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_post_repository import NaverPostRepository
from repositories.workflow_repository import WorkflowRepository
from services import market_policy
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.coupang_inquiry_sync_service import CoupangInquirySyncService
from services.inquiry_sync_service import normalize_work_item

SPID = "15654321531"
VENDOR_ITEM = "93128932886"
ANSWER = "네, 스피커가 내장되어 있습니다."


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "auto.db")
    value.initialize()
    return value


def payload(inquiry_id: str, *, answered: bool = False) -> dict[str, Any]:
    return {
        "inquiryId": inquiry_id, "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM, "content": "스피커 내장되어 있나요?",
        "inquiryAt": "2026-09-17T10:00:00+09:00", "orderIds": [],
        "commentDtoList": [{
            "inquiryCommentId": "c1", "inquiryId": inquiry_id,
            "content": "판매자 답변입니다.",
            "inquiryCommentAt": "2026-09-17T11:00:00+09:00",
        }] if answered else [],
    }


def coupang_inquiry(
    database: Database, *, account: str = "OJE_NS",
    inquiry_id: str = "160959847", answered: bool = False,
) -> int:
    ready = normalize_work_item(
        CoupangInquiryNormalizer()
        .online(payload(inquiry_id, answered=answered), account_code=account)
        .to_work_item()
    )
    row_id = InquiryRepository(database).upsert_work_item(ready).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


class RecordingPoster:
    """Stands in for every post client; records instead of sending."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, inquiry_id, **kwargs):
        from services.coupang_post_service import CoupangPostResult

        self.calls.append({"inquiry_id": int(inquiry_id), **kwargs})
        return CoupangPostResult(
            "POSTED", int(inquiry_id), 1, 200, None, "등록 완료", 1,
        )


class FakeTransport:
    """Accepts every request the way Coupang accepts a reply, and counts."""

    def __init__(self) -> None:
        self.count = 0
        self.requests: list[tuple[Any, ...]] = []

    def request(self, *args, **kwargs):
        self.count += 1
        self.requests.append((args, kwargs))

        class _Response:
            status_code = 200
            text = '{"code":"200","message":"OK"}'

            def json(self):
                return {"code": "200", "message": "OK"}

        return _Response()


def coupang_account(account_code: str):
    """Distinct credentials per account, so a mix-up cannot go unnoticed."""

    from config import CoupangAccountSettings

    return CoupangAccountSettings(
        account_code, f"오제 {account_code}", f"key-{account_code}",
        f"secret-{account_code}", f"vendor-{account_code}",
        f"wing-{account_code}",
    )


def coupang_service(database: Database):
    """The real post service on a fake wire, so nothing leaves the process."""

    from api.coupang_post_client import CoupangPostClient
    from services.coupang_post_service import CoupangPostService

    transport = FakeTransport()
    service = CoupangPostService(
        database,
        client=CoupangPostClient(
            access_key="key", secret_key="secret", transport=transport,
        ),
        account_resolver=coupang_account,
    )
    return service, transport


def sent_bodies(transport: FakeTransport) -> list[dict[str, Any]]:
    return [
        json.loads(kwargs["data"].decode("utf-8"))
        for _args, kwargs in transport.requests
    ]


# --- the trigger: a collected inquiry reaches the shared outbox ------------------

class OneItemSync(CoupangInquirySyncService):
    def collect(self, account: str, item: dict[str, Any]):
        from services.coupang_inquiry_sync_service import CoupangInquirySyncResult

        result = CoupangInquirySyncResult(account_code=account)
        self._persist(account, item, result)
        return result


def enable_auto_processing(database: Database) -> None:
    AutoPostRepository(database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1,
    )


def test_a_newly_collected_coupang_inquiry_is_announced(database) -> None:
    """Before this the sync wrote no event, so nothing ever offered the row."""

    enable_auto_processing(database)

    result = OneItemSync(database).collect("OJE_NS", payload("160959847"))

    assert result.new == 1
    assert result.announced == 1
    assert result.announce_failed == 0
    events = AutoPostEventRepository(database)
    assert events.pending_inquiry_ids(exclude_inquiry_ids=set(), limit=10) == [
        InquiryRepository(database).get_by_source(
            "COUPANG_OJE_NS", "COUPANG_ONLINE_INQUIRY", "160959847"
        )["id"]
    ]


def test_an_announcement_obeys_the_operator_switch(database) -> None:
    """The switch is the operator's, not the market's: OFF holds the event.

    The row still enters the outbox so nothing is lost, but it is not
    claimable until the operator turns auto-processing back on -- exactly
    what an announced Naver inquiry does.
    """

    events = AutoPostEventRepository(database)
    result = OneItemSync(database).collect("OJE_NS", payload("160959847"))

    assert result.announced == 1
    inquiry_id = InquiryRepository(database).get_by_source(
        "COUPANG_OJE_NS", "COUPANG_ONLINE_INQUIRY", "160959847"
    )["id"]
    held = events.get_for_inquiry(inquiry_id) or {}
    assert held["status"] == "BLOCKED_AUTO_POST_OFF"
    assert events.pending_inquiry_ids(exclude_inquiry_ids=set(), limit=10) == []

    events.unblock_after_runtime_enable()
    assert events.pending_inquiry_ids(
        exclude_inquiry_ids=set(), limit=10
    ) == [inquiry_id]


def test_re_collecting_the_same_inquiry_announces_nothing(database) -> None:
    """Only a genuinely new row is announced, so it cannot queue twice."""

    sync = OneItemSync(database)
    first = sync.collect("OJE_NS", payload("160959847"))
    second = sync.collect("OJE_NS", payload("160959847"))

    assert (first.new, first.announced) == (1, 1)
    assert second.announced == 0
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM auto_sync_events"
        ).fetchone()[0] == 1


def test_a_failed_announcement_never_fails_the_collection(
    database, monkeypatch,
) -> None:
    sync = OneItemSync(database)

    def exploding(*_args, **_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(AutoPostEventRepository, "create", exploding)

    result = sync.collect("OJE_NS", payload("160959847"))

    assert result.new == 1
    assert result.announce_failed == 1
    assert result.failed == 0


# --- the gates the pipeline reads ----------------------------------------------

def test_coupang_is_admitted_to_generation_and_posting_only() -> None:
    for store in ("COUPANG_OJE_NS", "COUPANG_OJE_PLUS"):
        assert market_policy.is_store_automatic_generation_enabled(store) is True
        assert market_policy.is_store_automatic_post_enabled(store) is True
        # DPS stays closed, and so does the read-only rule for answered rows.
        assert market_policy.is_store_dps_enabled(store) is False
        assert market_policy.is_store_post_enabled(store) is False


def test_the_read_only_rule_for_answered_coupang_is_unchanged() -> None:
    """Opening the queue must not re-open editing on an answered inquiry."""

    from ui import review_workspace

    assert review_workspace._is_read_only_inquiry(
        {"store_code": "COUPANG_OJE_NS", "source_answered": True}
    ) is True
    assert review_workspace._is_read_only_inquiry(
        {"store_code": "COUPANG_OJE_NS", "source_answered": False}
    ) is False


# --- G/H. an answered inquiry is never drafted or posted ------------------------

@pytest.mark.parametrize("answered", [True, False])
def test_an_answered_inquiry_is_skipped_before_any_work(database, answered) -> None:
    from services.automatic_draft_service import AutomaticDraftService

    inquiry_id = coupang_inquiry(database, answered=answered, inquiry_id="17000050")

    class Recorder:
        calls = 0

        def __getattr__(self, name):
            def record(*_a, **_k):
                type(self).calls += 1
                raise AssertionError(f"{name} must not run")
            return record

    recorder = Recorder()
    outcome = AutomaticDraftService(
        database, answer_service=recorder,
    ).ensure_for_inquiry(inquiry_id)

    if answered:
        assert outcome.status == "SKIPPED_ALREADY_ANSWERED"
        assert Recorder.calls == 0
    else:
        # Not skipped for being answered; it proceeds into the answer path.
        assert outcome.status != "SKIPPED_ALREADY_ANSWERED"


# --- the post dispatch picks the market's own client ----------------------------

def test_an_injected_poster_owns_every_market(database) -> None:
    """A test double must reach every market, so no real client is built."""

    poster = RecordingPoster()
    pipeline = AutoPostPipelineService(database, post_service=poster)

    assert pipeline._poster_for({"store_code": "COUPANG_OJE_NS"}) is poster
    assert pipeline._poster_for({"store_code": "OJE_PLUS"}) is poster


def test_without_an_injected_poster_each_market_gets_its_own_client(
    database,
) -> None:
    from services.coupang_post_service import CoupangPostService
    from services.naver_post_service import NaverPostService

    pipeline = AutoPostPipelineService(database)

    coupang = pipeline._poster_for({"store_code": "COUPANG_OJE_NS"})
    naver = pipeline._poster_for({"store_code": "OJE_PLUS"})

    assert isinstance(coupang, CoupangPostService)
    assert isinstance(naver, NaverPostService)
    # Built once and reused, and the Naver client is the pipeline's own.
    assert pipeline._poster_for({"store_code": "COUPANG_OJE_PLUS"}) is coupang
    assert naver is pipeline.posts


def test_only_naver_is_confirmed_by_re_reading_it(database) -> None:
    """Coupang's answer becomes visible on its next sync, not on a read now.

    The Naver confirmation refuses a Coupang source type outright, so asking
    it would turn every successful Coupang post into a failure.
    """

    pipeline = AutoPostPipelineService(database, post_service=RecordingPoster())

    assert pipeline._confirms_remotely({"store_code": "OJE_PLUS"}) is True
    assert pipeline._confirms_remotely({"store_code": "COUPANG_OJE_NS"}) is False
    assert pipeline._confirms_remotely({"store_code": "COUPANG_OJE_PLUS"}) is False


# --- I/J. idempotency is the existing post state --------------------------------

def test_an_already_posted_inquiry_is_not_posted_again(database) -> None:
    """The Coupang post service refuses on the state the manual path uses."""

    inquiry_id = coupang_inquiry(database, inquiry_id="17000051")
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO answer_drafts(
                inquiry_id, program_status, category, reason, provider,
                original_answer, review_status, posted, created_at, updated_at,
                source, validation_status, is_active
            ) VALUES (?, '답변 가능', '상품', 'r', 'gpt', ?, 'PENDING', 0,
                      't', 't', 'GPT', 'PASS', 1)
            """,
            (inquiry_id, ANSWER),
        )

    service, transport = coupang_service(database)

    first = service.post(inquiry_id, actor="SYSTEM_AUTO_POST", confirmed=True)
    second = service.post(inquiry_id, actor="SYSTEM_AUTO_POST", confirmed=True)

    assert first.status == "POSTED"
    assert second.status == "BLOCKED"
    assert transport.count == 1
    # Marketplace truth is still the next sync's to set.
    row = InquiryRepository(database).get(inquiry_id)
    assert bool(row.get("source_answered")) is False
    assert str(row.get("post_status")).upper() == "POSTED"
    attempt = NaverPostRepository(database).latest(inquiry_id) or {}
    assert attempt["endpoint_kind"] == "COUPANG_ONLINE_INQUIRY_REPLY"


# --- M. Naver is unchanged ------------------------------------------------------

def test_naver_keeps_every_automatic_capability() -> None:
    assert market_policy.is_store_automatic_generation_enabled("OJE_PLUS") is True
    assert market_policy.is_store_automatic_post_enabled("OJE_PLUS") is True
    assert market_policy.is_store_post_enabled("OJE_PLUS") is True
    assert market_policy.is_store_dps_enabled("OJE_PLUS") is True
    assert market_policy.is_store_manual_post_enabled("OJE_PLUS") is True


# --- no external call anywhere --------------------------------------------------

def test_nothing_reaches_the_network(database, monkeypatch) -> None:
    import requests

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no request may be made")

    for name in ("request", "get", "post"):
        monkeypatch.setattr(requests, name, forbidden)

    result = OneItemSync(database).collect("OJE_NS", payload("17000052"))
    pipeline = AutoPostPipelineService(database, post_service=RecordingPoster())

    assert result.announced == 1
    assert pipeline._poster_for({"store_code": "COUPANG_OJE_NS"}).calls == []


# --- A/B/J/M. the whole pipeline, end to end -----------------------------------

def make_draft(database: Database, inquiry_id: int, *, needs_review: bool = False,
               answer: str = ANSWER,
               processing_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """A prepared draft, so the run needs no GPT call to reach the decision."""

    from answer.models import AnswerResult, AnswerStatus
    from repositories.answer_repository import AnswerRepository

    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=(
                AnswerStatus.NEEDS_REVIEW if needs_review
                else AnswerStatus.GENERATED
            ),
            category="상품", reason="테스트", answer=answer,
            provider="rules", auto_answerable=not needs_review,
            needs_review=needs_review,
            metadata={
                "selected_answer_route": "TEMPLATE",
                "generation_mode": "TEMPLATE",
                "requires_manual_review": needs_review,
                "validator_result": {"status": "PASS", "passed": True},
                **({"processing_plan": processing_plan}
                   if processing_plan is not None else {}),
            },
        ),
    )


def naver_inquiry(database: Database, *, external_id: str = "N-1") -> int:
    row = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external_id, "external_inquiry_id": external_id,
        "inquiry_type": "PRODUCT_INQUIRY", "title": "상품 문의",
        "content": "스피커 내장되어 있나요?",
        "product_name": "스마트모니터", "source_answered": 0,
        "answer_status": "UNANSWERED", "post_status": "NOT_POSTED",
        "raw_json": {"source": "PRODUCT_INQUIRY", "questionId": external_id},
    })
    WorkflowRepository(database).initialize_steps(row.inquiry_id)
    return row.inquiry_id


class RecordingConfirmation:
    """Stands in for the remote re-read, which a Coupang post never asks."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def confirm(self, inquiry_id, *, run_id):
        self.calls.append(int(inquiry_id))
        return True


def run(database: Database, poster, confirmation=None, *, run_id="RUN-1",
        dps_status_provider=None):
    return AutoPostPipelineService(
        database, post_service=poster, confirmation_service=confirmation,
        dps_status_provider=dps_status_provider or (lambda: {"session_status": "OK"}),
    ).run_pending(run_id=run_id, owner_id="OWNER-1", max_retries=1)


def test_a_coupang_inquiry_with_a_safe_draft_is_posted_once(database) -> None:
    inquiry_id = coupang_inquiry(database, inquiry_id="17000060")
    make_draft(database, inquiry_id)
    poster, confirmation = RecordingPoster(), RecordingConfirmation()

    outcome = run(database, poster, confirmation)

    assert outcome.succeeded_count == 1
    assert [call["inquiry_id"] for call in poster.calls] == [inquiry_id]
    # The Coupang request takes no unattended-run context, and its answer is
    # confirmed by the next sync rather than by a read from here.
    assert "automatic" not in poster.calls[0]
    assert "auto_post_run_id" not in poster.calls[0]
    assert poster.calls[0]["actor"] == "SYSTEM_AUTO_POST"
    assert confirmation.calls == []


def test_m_the_same_naver_fixture_still_posts_the_naver_way(database) -> None:
    """Naver keeps its run context and its remote confirmation."""

    inquiry_id = naver_inquiry(database)
    make_draft(database, inquiry_id)
    poster, confirmation = RecordingPoster(), RecordingConfirmation()

    outcome = run(database, poster, confirmation, run_id="RUN-NAVER")

    assert outcome.succeeded_count == 1
    assert poster.calls[0]["automatic"] is True
    assert poster.calls[0]["auto_post_run_id"] == "RUN-NAVER"
    assert confirmation.calls == [inquiry_id]


def test_both_markets_in_one_run_each_get_their_own_shape(database) -> None:
    coupang_id = coupang_inquiry(database, inquiry_id="17000061")
    naver_id = naver_inquiry(database, external_id="N-2")
    for inquiry_id in (coupang_id, naver_id):
        make_draft(database, inquiry_id)
    poster, confirmation = RecordingPoster(), RecordingConfirmation()

    outcome = run(database, poster, confirmation)

    assert outcome.succeeded_count == 2
    by_id = {call["inquiry_id"]: call for call in poster.calls}
    assert "automatic" not in by_id[coupang_id]
    assert by_id[naver_id]["automatic"] is True
    assert confirmation.calls == [naver_id]


def test_b_a_draft_held_for_review_is_never_posted(database, monkeypatch) -> None:
    """Held for staff, and the hold is told to the Coupang room only."""

    import kakao_notify

    inquiry_id = coupang_inquiry(database, inquiry_id="17000062")
    make_draft(
        database, inquiry_id,
        processing_plan={
            "requires_order_lookup": True,
            "order_id_status": "MISSING",
            "order_lookup_status": "CUSTOMER_INFORMATION_REQUIRED",
        },
    )
    notifications: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "services.auto_post_pipeline_service.notify_qna_safely",
        lambda **kwargs: notifications.append(kwargs) or True,
    )
    poster = RecordingPoster()

    outcome = run(database, poster)

    assert outcome.succeeded_count == 0
    assert outcome.skipped_count == 1
    assert poster.calls == []
    assert [item["store_code"] for item in notifications] == ["COUPANG_OJE_NS"]
    assert kakao_notify.recipient_for_market(
        market_policy.market_of(notifications[0]["store_code"])
    ) == kakao_notify.recipient_for_market("COUPANG")


def test_a_dps_dependent_draft_is_held_and_never_posted(database) -> None:
    """DPS stays off for Coupang: the hold happens before any post."""

    inquiry_id = coupang_inquiry(database, inquiry_id="17000063")
    make_draft(
        database, inquiry_id,
        processing_plan={"requires_dps_lookup": True, "needs_staff_review": True},
    )
    poster = RecordingPoster()
    lookups: list[int] = []

    outcome = run(
        database, poster,
        dps_status_provider=lambda: lookups.append(1) or {
            "session_status": "LOGIN_REQUIRED"
        },
    )

    assert outcome.skipped_count == 1
    assert poster.calls == []
    # Held before the DPS branch, so the agent is never even asked.
    assert lookups == []


def test_j_running_the_pipeline_twice_posts_at_most_once(database) -> None:
    """The real Coupang service, so the second run meets the real state."""

    inquiry_id = coupang_inquiry(database, inquiry_id="17000064")
    make_draft(database, inquiry_id)
    service, transport = coupang_service(database)

    first = run(database, service, run_id="RUN-1")
    second = run(database, service, run_id="RUN-2")

    assert first.succeeded_count == 1
    assert second.succeeded_count == 0
    assert transport.count == 1
    assert str(
        InquiryRepository(database).get(inquiry_id).get("post_status")
    ).upper() == "POSTED"
    # Marketplace truth is still the next Coupang sync's to write.
    assert bool(
        InquiryRepository(database).get(inquiry_id).get("source_answered")
    ) is False


def test_an_answered_coupang_inquiry_never_reaches_the_poster(database) -> None:
    """A seller answered on Coupang between the draft and the run."""

    inquiry_id = coupang_inquiry(database, inquiry_id="17000065")
    make_draft(database, inquiry_id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_answered=1 WHERE id=?", (inquiry_id,)
        )
    poster = RecordingPoster()

    outcome = run(database, poster)

    assert outcome.succeeded_count == 0
    assert poster.calls == []


# --- K/L. two accounts, each answering as itself --------------------------------

def test_k_each_account_replies_as_itself(database) -> None:
    """One run, both accounts: the reply carries its own vendor and WING id."""

    ns = coupang_inquiry(database, account="OJE_NS", inquiry_id="17000070")
    plus = coupang_inquiry(database, account="OJE_PLUS", inquiry_id="17000071")
    for inquiry_id in (ns, plus):
        make_draft(database, inquiry_id)
    service, transport = coupang_service(database)

    outcome = run(database, service)

    assert outcome.succeeded_count == 2
    assert transport.count == 2
    bodies = {body["vendorId"]: body for body in sent_bodies(transport)}
    assert bodies.keys() == {"vendor-OJE_NS", "vendor-OJE_PLUS"}
    assert bodies["vendor-OJE_NS"]["replyBy"] == "wing-OJE_NS"
    assert bodies["vendor-OJE_PLUS"]["replyBy"] == "wing-OJE_PLUS"
    # Each request addresses its own inquiry on the documented reply path.
    urls = [args[1] for args, _kwargs in transport.requests]
    assert any("/vendors/vendor-OJE_NS/" in url for url in urls)
    assert all(url.endswith("/replies") for url in urls)


def test_l_the_signing_credentials_come_from_the_resolved_account(
    database, monkeypatch,
) -> None:
    """With no injected client, each account signs with its own key."""

    from services import coupang_post_service as module

    built: list[dict[str, str]] = []

    class RecordingClient:
        def __init__(self, *, access_key: str, secret_key: str, **_kwargs) -> None:
            built.append({"access_key": access_key, "secret_key": secret_key})

        def reply(self, **_kwargs):
            from api.coupang_post_client import CoupangReplyResult

            return CoupangReplyResult(200, "200", "OK")

    monkeypatch.setattr(module, "CoupangPostClient", RecordingClient)
    inquiry_id = coupang_inquiry(database, account="OJE_PLUS", inquiry_id="17000072")
    make_draft(database, inquiry_id)
    service = module.CoupangPostService(
        database, account_resolver=coupang_account,
    )

    outcome = run(database, service)

    assert outcome.succeeded_count == 1
    assert built == [
        {"access_key": "key-OJE_PLUS", "secret_key": "secret-OJE_PLUS"}
    ]


def test_an_unconfigured_account_is_held_rather_than_posted(database) -> None:
    """Fail closed: no credentials means staff review, never a blind send."""

    from config import CoupangAccountSettings
    from services.coupang_post_service import CoupangPostService

    inquiry_id = coupang_inquiry(database, inquiry_id="17000073")
    make_draft(database, inquiry_id)
    service = CoupangPostService(
        database,
        account_resolver=lambda code: CoupangAccountSettings(code, code),
    )

    outcome = run(database, service)

    assert outcome.succeeded_count == 0
    assert str(
        InquiryRepository(database).get(inquiry_id).get("post_status")
    ).upper() != "POSTED"


# --- the validator is still the last line ---------------------------------------

def test_an_answer_that_fails_the_validator_is_never_posted(database) -> None:
    """A phone number in the body blocks the post, as it does for Naver."""

    inquiry_id = coupang_inquiry(database, inquiry_id="17000074")
    make_draft(database, inquiry_id, answer="문의는 010-1234-5678로 연락 주세요.")
    service, transport = coupang_service(database)

    outcome = run(database, service)

    assert outcome.succeeded_count == 0
    assert transport.count == 0
    assert InquiryRepository(database).get(inquiry_id)[
        "workflow_status"
    ] == "NEEDS_ATTENTION"
