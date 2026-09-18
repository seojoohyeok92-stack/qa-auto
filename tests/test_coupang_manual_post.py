"""Registering a Coupang answer, on an operator's explicit click.

Manual only.  Every request here goes to a recorder, never to Coupang: the
service is constructed with a fake client, and the tests assert the number of
requests it made as often as they assert what it sent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.coupang_post_client import (
    CoupangPostClient,
    CoupangPostError,
    build_reply_path,
)
from config import CoupangAccountSettings
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_post_repository import NaverPostRepository
from repositories.workflow_repository import WorkflowRepository
from services import market_policy
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.coupang_post_service import CoupangPostService
from services.inquiry_sync_service import normalize_work_item

SPID = "15654321531"
VENDOR_ITEM = "93128932886"
ANSWER = "방문설치는 주문 시 설치 옵션을 선택하시면 기사님이 방문해 설치해 드립니다."

ACCOUNTS = {
    "OJE_NS": CoupangAccountSettings(
        "OJE_NS", "오제앤에스", "ns-access", "ns-secret", "A00000001", "ns-wing",
    ),
    "OJE_PLUS": CoupangAccountSettings(
        "OJE_PLUS", "오제플러스", "plus-access", "plus-secret", "A00000002", "plus-wing",
    ),
}


# --- doubles --------------------------------------------------------------------

class RecordingTransport:
    """Records requests; never reaches the network."""

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"code": "200", "message": "OK"}
        self.requests: list[dict[str, Any]] = []

    def request(self, method, url, *, headers=None, data=None, timeout=None):
        self.requests.append({
            "method": method, "url": url, "headers": dict(headers or {}),
            "data": data,
        })
        transport = self

        class _Response:
            status_code = transport.status
            text = json.dumps(transport.body, ensure_ascii=False)

            def json(self):
                return transport.body

        return _Response()


def client_for(transport: RecordingTransport) -> CoupangPostClient:
    return CoupangPostClient(
        access_key="ns-access", secret_key="ns-secret", transport=transport,
    )


def service(database: Database, transport: RecordingTransport) -> CoupangPostService:
    return CoupangPostService(
        database,
        client=client_for(transport),
        account_resolver=lambda code: ACCOUNTS[str(code).upper()],
    )


# --- fixtures -------------------------------------------------------------------

@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "coupang_post.db")
    value.initialize()
    return value


def coupang_inquiry(
    database: Database,
    *,
    account: str = "OJE_NS",
    inquiry_id: str = "160959847",
    answered: bool = False,
) -> int:
    payload = {
        "inquiryId": inquiry_id,
        "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM,
        "content": "방문설치는 어떻게 신청하나요?",
        "inquiryAt": "2026-09-17T10:00:00+09:00",
        "orderIds": [],
        "commentDtoList": [{
            "inquiryCommentId": "c1", "inquiryId": inquiry_id,
            "content": "판매자 답변입니다.",
            "inquiryCommentAt": "2026-09-17T11:00:00+09:00",
        }] if answered else [],
    }
    ready = normalize_work_item(
        CoupangInquiryNormalizer().online(payload, account_code=account).to_work_item()
    )
    row_id = InquiryRepository(database).upsert_work_item(ready).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def naver_inquiry(database: Database) -> int:
    row_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "N-1", "inquiry_type": "상품",
        "title": "상품 문의", "content": "스피커 있나요?", "raw_json": {},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def add_draft(database: Database, inquiry_id: int, answer: str = ANSWER) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO answer_drafts(
                inquiry_id, program_status, category, reason, provider,
                original_answer, review_status, posted, created_at, updated_at,
                source, validation_status, is_active
            ) VALUES (?, '답변 가능', '상품', 'r', 'gpt', ?, 'PENDING', 0,
                      '2026-09-17T12:00:00Z', '2026-09-17T12:00:00Z',
                      'GPT', 'PASS', 1)
            """,
            (inquiry_id, answer),
        )
        return int(cursor.lastrowid)


def post_status(database: Database, inquiry_id: int) -> str:
    row = InquiryRepository(database).get(inquiry_id) or {}
    return str(row.get("post_status") or "NOT_POSTED").upper()


def source_answered(database: Database, inquiry_id: int) -> bool:
    row = InquiryRepository(database).get(inquiry_id) or {}
    return bool(row.get("source_answered"))


# --- C / D. the documented request ----------------------------------------------

def test_the_request_matches_the_documented_endpoint_and_body(database) -> None:
    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()

    result = service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=True,
    )

    assert result.status == "POSTED"
    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent["method"] == "POST"
    assert sent["url"].endswith(
        "/v2/providers/openapi/apis/api/v4/vendors/A00000001"
        "/onlineInquiries/160959847/replies"
    )
    body = json.loads(sent["data"].decode("utf-8"))
    assert body == {
        "content": ANSWER, "vendorId": "A00000001", "replyBy": "ns-wing",
    }
    # The signature is sent, never logged back out.
    assert sent["headers"]["Authorization"].startswith("CEA algorithm=HmacSHA256")


# --- A / B. account routing -----------------------------------------------------

@pytest.mark.parametrize(
    "account, vendor, wing",
    [("OJE_NS", "A00000001", "ns-wing"), ("OJE_PLUS", "A00000002", "plus-wing")],
)
def test_each_account_signs_and_addresses_with_its_own_identity(
    database, account, vendor, wing,
) -> None:
    inquiry_id = coupang_inquiry(database, account=account, inquiry_id="17000001")
    add_draft(database, inquiry_id)
    transport = RecordingTransport()

    assert service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=True,
    ).status == "POSTED"

    sent = transport.requests[0]
    body = json.loads(sent["data"].decode("utf-8"))
    assert f"/vendors/{vendor}/" in sent["url"]
    assert body["vendorId"] == vendor
    assert body["replyBy"] == wing


def test_one_accounts_inquiry_never_uses_the_others_credentials(database) -> None:
    ns = coupang_inquiry(database, account="OJE_NS", inquiry_id="17000002")
    plus = coupang_inquiry(database, account="OJE_PLUS", inquiry_id="17000003")
    add_draft(database, ns)
    add_draft(database, plus)
    seen: list[tuple[str, str]] = []

    def resolver(code):
        account = ACCOUNTS[str(code).upper()]
        seen.append((str(code).upper(), account.vendor_id))
        return account

    for inquiry_id in (ns, plus):
        transport = RecordingTransport()
        CoupangPostService(
            database, client=client_for(transport), account_resolver=resolver,
        ).post(inquiry_id, actor="관리자", confirmed=True)

    assert seen == [("OJE_NS", "A00000001"), ("OJE_PLUS", "A00000002")]


# --- E. confirmation, F/G/H. nothing is posted twice ----------------------------

def test_without_confirmation_nothing_is_sent(database) -> None:
    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()

    result = service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=False,
    )

    assert result.status == "BLOCKED"
    assert result.error_code == "CONFIRMATION_REQUIRED"
    assert transport.requests == []


def test_an_inquiry_the_marketplace_already_answered_is_not_posted(database) -> None:
    """F/G: source_answered and a stored seller reply each stop it."""

    inquiry_id = coupang_inquiry(database, answered=True)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()

    result = service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=True,
    )

    assert result.status == "BLOCKED"
    assert result.error_code in {"ALREADY_ANSWERED", "SELLER_ANSWER_EXISTS"}
    assert transport.requests == []


def test_a_second_click_sends_nothing(database) -> None:
    """H: idempotency comes from the existing post state, not a new table."""

    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()
    poster = service(database, transport)

    assert poster.post(inquiry_id, actor="관리자", confirmed=True).status == "POSTED"
    second = poster.post(inquiry_id, actor="관리자", confirmed=True)

    assert len(transport.requests) == 1
    assert second.status == "BLOCKED"
    assert second.error_code == "ALREADY_POSTED"


def test_a_failed_post_is_not_resent_without_an_explicit_retry(database) -> None:
    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport(
        status=400, body={"code": "400", "message": "content is required"},
    )
    poster = service(database, transport)

    first = poster.post(inquiry_id, actor="관리자", confirmed=True)
    assert first.status == "POST_FAILED"
    assert len(transport.requests) == 1

    again = poster.post(inquiry_id, actor="관리자", confirmed=True)
    assert again.status == "BLOCKED"
    assert again.error_code == "EXPLICIT_RETRY_REQUIRED"
    assert len(transport.requests) == 1


# --- I. the reused safety gate --------------------------------------------------

def test_an_answer_the_technical_validator_rejects_is_not_posted(database) -> None:
    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id, answer="연락처는 010-1234-5678 입니다.")
    transport = RecordingTransport()

    result = service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=True,
    )

    assert result.status == "BLOCKED"
    assert transport.requests == []


def test_without_a_draft_nothing_is_sent(database) -> None:
    """J: a DPS/delivery inquiry never gets a draft, so it never reaches POST."""

    inquiry_id = coupang_inquiry(database)
    transport = RecordingTransport()

    result = service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=True,
    )

    assert result.status == "BLOCKED"
    assert result.error_code == "LOCAL_STATE_MISSING"
    assert transport.requests == []


def test_a_naver_inquiry_is_refused_by_this_service(database) -> None:
    inquiry_id = naver_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()

    result = service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=True,
    )

    assert result.status == "BLOCKED"
    assert result.error_code == "NOT_COUPANG"
    assert transport.requests == []


# --- the WING id is configuration, never a guess --------------------------------

def test_a_missing_wing_id_blocks_the_post_and_names_the_variable(database) -> None:
    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()
    unconfigured = CoupangAccountSettings(
        "OJE_NS", "오제앤에스", "ns-access", "ns-secret", "A00000001", "",
    )

    result = CoupangPostService(
        database, client=client_for(transport),
        account_resolver=lambda _code: unconfigured,
    ).post(inquiry_id, actor="관리자", confirmed=True)

    assert result.status == "BLOCKED"
    assert result.error_code == "COUPANG_POST_NOT_CONFIGURED"
    assert "COUPANG_WING_ID" in result.message
    assert transport.requests == []
    # And the other account's id is never borrowed to fill the gap.
    assert "plus-wing" not in result.message


def test_the_client_refuses_to_build_a_reply_without_a_wing_id() -> None:
    from api.coupang_post_client import build_reply_payload

    with pytest.raises(CoupangPostError) as error:
        build_reply_payload(content="본문", vendor_id="A00000001", reply_by="")
    assert error.value.code == "WING_ID_REQUIRED"


# --- K / L. state after the request ---------------------------------------------

def test_a_successful_post_records_the_attempt_and_leaves_marketplace_truth(
    database,
) -> None:
    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()

    result = service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=True,
    )

    assert post_status(database, inquiry_id) == "POSTED"
    # source_answered stays what Coupang says; the next sync sets it.
    assert source_answered(database, inquiry_id) is False
    attempt = NaverPostRepository(database).latest(inquiry_id) or {}
    assert attempt["status"] == "POSTED"
    assert attempt["method"] == "POST"
    assert attempt["endpoint_kind"] == "COUPANG_ONLINE_INQUIRY_REPLY"
    assert attempt["store_code"] == "COUPANG_OJE_NS"
    assert int(attempt["id"]) == int(result.attempt_id)


def test_a_failed_post_marks_neither_posted_nor_answered(database) -> None:
    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport(
        status=400,
        body={"code": "400", "message": "replyBy is not a valid WING ID"},
    )

    result = service(database, transport).post(
        inquiry_id, actor="관리자", confirmed=True,
    )

    assert result.status == "POST_FAILED"
    assert result.error_code == "REPLY_BY_INVALID"
    assert post_status(database, inquiry_id) == "POST_FAILED"
    assert source_answered(database, inquiry_id) is False
    assert (NaverPostRepository(database).latest(inquiry_id) or {})["status"] == "POST_FAILED"


def test_a_network_failure_is_unknown_rather_than_failed(database) -> None:
    """An outcome nobody can confirm is never silently retried."""

    import requests

    class Exploding:
        def request(self, *_args, **_kwargs):
            raise requests.RequestException("boom")

    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)

    result = CoupangPostService(
        database,
        client=CoupangPostClient(
            access_key="k", secret_key="s", transport=Exploding(),
        ),
        account_resolver=lambda code: ACCOUNTS[str(code).upper()],
    ).post(inquiry_id, actor="관리자", confirmed=True)

    assert result.status == "POST_UNKNOWN"
    assert result.error_code == "NETWORK_ERROR"
    assert source_answered(database, inquiry_id) is False


# --- the preflight writes nothing -----------------------------------------------

def test_the_preflight_sends_nothing_and_agrees_with_the_post(database) -> None:
    inquiry_id = coupang_inquiry(database)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()

    ready = service(database, transport).preflight(inquiry_id)

    assert ready["eligible"] is True
    assert ready["network_call_count"] == 0
    assert ready["vendor_id"] == "A00000001"
    assert ready["path"] == build_reply_path(
        vendor_id="A00000001", inquiry_id="160959847",
    )
    # The WING id and the keys are not echoed to the screen.
    assert "ns-wing" not in json.dumps(ready, ensure_ascii=False)
    assert "ns-secret" not in json.dumps(ready, ensure_ascii=False)
    assert transport.requests == []


def test_the_preflight_reports_the_same_refusal_the_post_would(database) -> None:
    inquiry_id = coupang_inquiry(database, answered=True)
    add_draft(database, inquiry_id)
    transport = RecordingTransport()
    poster = service(database, transport)

    blocked = poster.preflight(inquiry_id)
    result = poster.post(inquiry_id, actor="관리자", confirmed=True)

    assert blocked["eligible"] is False
    assert blocked["error_code"] == result.error_code
    assert transport.requests == []


# --- N / O. nothing else was opened ---------------------------------------------

def test_manual_post_is_open_but_automatic_posting_is_not() -> None:
    for store in ("COUPANG_OJE_NS", "COUPANG_OJE_PLUS"):
        assert market_policy.is_store_manual_post_enabled(store) is True
        # The auto-post queue scopes on this one, so it stays closed.
        assert market_policy.is_store_post_enabled(store) is False
        assert market_policy.is_store_automatic_generation_enabled(store) is False
        assert market_policy.is_store_dps_enabled(store) is False
    assert market_policy.post_enabled_store_codes(
        ["COUPANG_OJE_NS", "COUPANG_OJE_PLUS", "OJE_PLUS"]
    ) == ["OJE_PLUS"]


def test_an_answered_coupang_inquiry_stays_read_only() -> None:
    """Opening manual POST must not re-open editing on answered inquiries."""

    from ui import review_workspace

    assert review_workspace._is_read_only_inquiry(
        {"store_code": "COUPANG_OJE_NS", "source_answered": True}
    ) is True


# --- M. Naver is untouched ------------------------------------------------------

def test_naver_keeps_both_manual_and_automatic_posting() -> None:
    assert market_policy.is_store_manual_post_enabled("OJE_PLUS") is True
    assert market_policy.is_store_post_enabled("OJE_PLUS") is True
