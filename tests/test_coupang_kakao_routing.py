"""Which chat room a Coupang notification reaches.

Both Coupang seller accounts report into one room, because the room is chosen
by marketplace and never by account.  Naver keeps its own room unchanged.

Nothing here touches KakaoTalk: the outbox is a file in ``tmp_path``, the
dispatcher is never launched, and the Coupang post client is a recorder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kakao_notify
from config import CoupangAccountSettings
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from services import market_policy
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.coupang_post_service import CoupangPostService
from services.inquiry_sync_service import normalize_work_item

NAVER_ROOM = "오제 네이버 자동답변 확인방"
COUPANG_ROOM = "오제 쿠팡 자동답변 확인방"

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
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"code": "200", "message": "OK"}
        self.requests: list[dict[str, Any]] = []

    def request(self, method, url, *, headers=None, data=None, timeout=None):
        self.requests.append({"method": method, "url": url})
        transport = self

        class _Response:
            status_code = transport.status
            text = json.dumps(transport.body, ensure_ascii=False)

            def json(self):
                return transport.body

        return _Response()


@pytest.fixture
def outbox(tmp_path: Path, monkeypatch) -> Path:
    service_dir = tmp_path / "common_service" / "kakao"
    service_dir.mkdir(parents=True)
    (service_dir / "kakao_dispatcher.py").write_text("# marker\n", encoding="utf-8")
    path = service_dir / "outbox_events.jsonl"
    monkeypatch.setenv("KAKAO_NOTIFY_ENABLED", "1")
    monkeypatch.delenv("KAKAO_QNA_RECIPIENT", raising=False)
    monkeypatch.delenv("KAKAO_COUPANG_QNA_RECIPIENT", raising=False)
    monkeypatch.setattr(kakao_notify, "KAKAO_SERVICE_DIR", service_dir)
    monkeypatch.setattr(kakao_notify, "OUTBOX", path)
    monkeypatch.setattr(
        kakao_notify, "NOTIFY_DB", tmp_path / "kakao_notify_history.sqlite3"
    )
    return path


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "coupang_kakao.db")
    value.initialize()
    return value


def events(outbox: Path) -> list[dict]:
    if not outbox.exists():
        return []
    return [
        json.loads(line)
        for line in outbox.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def coupang_inquiry(
    database: Database, *, account: str = "OJE_NS", inquiry_id: str = "160959847",
) -> int:
    payload = {
        "inquiryId": inquiry_id, "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM, "content": "방문설치는 어떻게 신청하나요?",
        "inquiryAt": "2026-09-17T10:00:00+09:00", "orderIds": [],
        "commentDtoList": [],
    }
    ready = normalize_work_item(
        CoupangInquiryNormalizer().online(payload, account_code=account).to_work_item()
    )
    row_id = InquiryRepository(database).upsert_work_item(ready).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


PRODUCT_NAME = "삼탠바이미(32\" M50D + VI) / 화이트 / 메인코드"
OPTION_NAME = "스탠드형 방문설치 32인치"


def seed_catalog(database: Database, *, account: str = "OJE_NS") -> None:
    """The catalogue rows the dashboard's own enrichment reads the name from."""

    from repositories.coupang_product_catalog_repository import (
        CoupangProductCatalogRepository,
    )

    catalog = CoupangProductCatalogRepository(database)
    catalog.upsert_product(
        account_code=account,
        data={
            "sellerProductId": SPID, "status": "APPROVED",
            "sellerProductName": PRODUCT_NAME,
        },
        sync_token="t",
    )
    catalog.upsert_option(
        account_code=account, seller_product_id=SPID,
        item={"vendorItemId": VENDOR_ITEM, "itemName": OPTION_NAME},
    )


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


def post(database: Database, inquiry_id: int, transport: RecordingTransport):
    from api.coupang_post_client import CoupangPostClient

    return CoupangPostService(
        database,
        client=CoupangPostClient(
            access_key="k", secret_key="s", transport=transport,
        ),
        account_resolver=lambda code: ACCOUNTS[str(code).upper()],
    ).post(inquiry_id, actor="관리자", confirmed=True)


# --- A / B. both accounts, one room ---------------------------------------------

@pytest.mark.parametrize("account", ["OJE_NS", "OJE_PLUS"])
def test_a_registered_coupang_answer_notifies_the_coupang_room(
    database, outbox, account,
) -> None:
    inquiry_id = coupang_inquiry(database, account=account, inquiry_id="17000010")
    add_draft(database, inquiry_id)
    transport = RecordingTransport()

    assert post(database, inquiry_id, transport).status == "POSTED"

    sent = events(outbox)
    assert len(sent) == 1
    assert sent[0]["recipient"] == COUPANG_ROOM
    assert sent[0]["title"] == "[쿠팡 Q&A 답변 등록 완료]"
    # The message is the existing format, with the marketplace named.
    assert "상품명:" in sent[0]["message"] and "질문:" in sent[0]["message"]
    assert ANSWER in sent[0]["message"]


def test_both_accounts_share_one_room_rather_than_splitting(database, outbox) -> None:
    for account, inquiry in (("OJE_NS", "17000011"), ("OJE_PLUS", "17000012")):
        inquiry_id = coupang_inquiry(database, account=account, inquiry_id=inquiry)
        add_draft(database, inquiry_id)
        assert post(database, inquiry_id, RecordingTransport()).status == "POSTED"

    rooms = {event["recipient"] for event in events(outbox)}
    assert rooms == {COUPANG_ROOM}
    assert len(events(outbox)) == 2


# --- C. a failed registration --------------------------------------------------

def test_a_failed_coupang_post_sends_no_notification(database, outbox) -> None:
    """The Naver post service notifies only on success; this matches it.

    A failure is recorded on the attempt and shown on the screen the operator
    is already looking at, so inventing a notification here would be a Coupang
    policy Naver does not have.
    """

    inquiry_id = coupang_inquiry(database, inquiry_id="17000013")
    add_draft(database, inquiry_id)
    transport = RecordingTransport(
        status=400, body={"code": "400", "message": "content is required"},
    )

    assert post(database, inquiry_id, transport).status == "POST_FAILED"
    assert events(outbox) == []


# --- the hold / 직원 확인 필요 path ----------------------------------------------

def test_a_held_coupang_inquiry_notifies_the_coupang_room(database, outbox) -> None:
    """The answer pipeline's own notification, routed by store code."""

    sent = kakao_notify.notify_qna_safely(
        title="[Q&A 미등록 / 직원 확인 필요]",
        store_code="COUPANG_OJE_NS",
        product="삼성 스마트모니터 M5",
        option_name="32인치",
        question="방문설치는 어떻게 신청하나요?",
        answer="-",
        action="needs_review",
        inquiry_id="160959847",
        notify_key="review-required:1",
        hold_codes=("ANSWER_REQUIRES_MANUAL_REVIEW",),
    )

    assert sent is True
    event = events(outbox)[0]
    assert event["recipient"] == COUPANG_ROOM
    # The existing hold format, naming this marketplace rather than Naver.
    assert "쿠팡 등록: 안 됨" in event["message"]
    assert "네이버" not in event["message"]


# --- D. Naver is unchanged ------------------------------------------------------

def test_naver_still_notifies_its_own_room(database, outbox) -> None:
    assert kakao_notify.notify_qna_safely(
        title="[네이버 Q&A 답변 등록 완료]",
        store_code="OJE_PLUS",
        product="삼성 스마트모니터 M5",
        option_name="32인치",
        question="스피커 있나요?",
        answer="네, 내장되어 있습니다.",
        action="posted",
        inquiry_id="N-1",
        notify_key="naver-posted:1:1",
    ) is True

    event = events(outbox)[0]
    assert event["recipient"] == NAVER_ROOM
    assert "네이버 자동답변" in event["recipient"]


def test_a_held_naver_inquiry_keeps_its_wording(database, outbox) -> None:
    assert kakao_notify.notify_qna_safely(
        title="[Q&A 미등록 / 직원 확인 필요]",
        store_code="OJE_PLUS",
        product="삼성 스마트모니터 M5",
        option_name="32인치",
        question="스피커 있나요?",
        answer="-",
        action="needs_review",
        inquiry_id="N-2",
        notify_key="review-required:2",
        hold_codes=("ANSWER_REQUIRES_MANUAL_REVIEW",),
    ) is True

    event = events(outbox)[0]
    assert event["recipient"] == NAVER_ROOM
    assert "네이버 등록: 안 됨" in event["message"]


# --- E / F. fail closed ---------------------------------------------------------

def test_a_market_that_may_not_be_notified_sends_nothing(outbox) -> None:
    assert market_policy.is_kakao_market_enabled("GMARKET") is False
    assert kakao_notify.notify_qna_safely(
        title="[Q&A 미등록 / 직원 확인 필요]",
        market="GMARKET",
        product="p", option_name="", question="q", answer="-",
        action="needs_review", inquiry_id="G-1", notify_key="gmarket:1",
    ) is False
    assert events(outbox) == []


def test_a_market_with_no_room_sends_nothing(outbox, monkeypatch) -> None:
    """No borrowed Naver room, no test room, no event."""

    monkeypatch.setattr(kakao_notify, "recipient_for_market", lambda _m: "")

    assert kakao_notify.notify_qna_safely(
        title="[쿠팡 Q&A 답변 등록 완료]",
        store_code="COUPANG_OJE_NS",
        product="p", option_name="", question="q", answer="a",
        action="posted", inquiry_id="C-1", notify_key="coupang:none",
    ) is False
    assert events(outbox) == []


def test_the_room_is_configurable_without_code_changes(outbox, monkeypatch) -> None:
    monkeypatch.setenv("KAKAO_COUPANG_QNA_RECIPIENT", "오제 쿠팡 자동답변 확인방2")

    assert kakao_notify.notify_qna_safely(
        title="[쿠팡 Q&A 답변 등록 완료]",
        store_code="COUPANG_OJE_PLUS",
        product="p", option_name="", question="q", answer="a",
        action="posted", inquiry_id="C-2", notify_key="coupang:cfg",
    ) is True
    assert events(outbox)[0]["recipient"] == "오제 쿠팡 자동답변 확인방2"
    # Naver's room is not affected by the Coupang variable.
    assert kakao_notify.recipient_for_market("NAVER") == NAVER_ROOM


# --- one notification per registration -----------------------------------------

def test_a_repeated_registration_does_not_duplicate_the_notification(
    database, outbox,
) -> None:
    inquiry_id = coupang_inquiry(database, inquiry_id="17000014")
    add_draft(database, inquiry_id)

    assert post(database, inquiry_id, RecordingTransport()).status == "POSTED"
    second = post(database, inquiry_id, RecordingTransport())

    assert second.status == "BLOCKED"
    assert len(events(outbox)) == 1


# --- G / H. nothing else was opened ---------------------------------------------

def test_opening_kakao_opens_neither_generation_nor_automatic_posting() -> None:
    for store in ("COUPANG_OJE_NS", "COUPANG_OJE_PLUS"):
        assert market_policy.is_kakao_market_enabled("COUPANG") is True
        assert market_policy.is_store_answer_generation_enabled(store) is True
        assert market_policy.is_store_manual_post_enabled(store) is True
        # Still closed.
        assert market_policy.is_store_automatic_generation_enabled(store) is False
        assert market_policy.is_store_post_enabled(store) is False
        assert market_policy.is_store_dps_enabled(store) is False
    assert market_policy.post_enabled_store_codes(
        ["COUPANG_OJE_NS", "COUPANG_OJE_PLUS", "OJE_PLUS"]
    ) == ["OJE_PLUS"]


# --- the product name the dashboard already shows --------------------------------

@pytest.mark.parametrize("account", ["OJE_NS", "OJE_PLUS"])
def test_a_registration_notification_names_the_product(
    database, outbox, account,
) -> None:
    """A/C: the name is the dashboard's, for either seller account.

    A Coupang row stores only ids, so the raw inquiry has no product name and
    the message used to read "상품명: -" for an inquiry whose card showed the
    product.
    """

    seed_catalog(database, account=account)
    inquiry_id = coupang_inquiry(database, account=account, inquiry_id="17000020")
    add_draft(database, inquiry_id)

    assert post(database, inquiry_id, RecordingTransport()).status == "POSTED"

    message = events(outbox)[0]["message"]
    assert f"상품명: {PRODUCT_NAME}" in message
    assert f"옵션명: {OPTION_NAME}" in message
    assert "상품명: -" not in message


def test_the_notified_name_is_the_one_the_screen_reads(database, outbox) -> None:
    """Same repository call, so the two can never disagree."""

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17000021")
    add_draft(database, inquiry_id)
    row = InquiryRepository(database)
    displayed = row.get_by_source("COUPANG_OJE_NS", "COUPANG_ONLINE_INQUIRY", "17000021")

    assert post(database, inquiry_id, RecordingTransport()).status == "POSTED"

    assert f"상품명: {displayed['product_name']}" in events(outbox)[0]["message"]


def test_an_inquiry_with_no_catalogued_name_keeps_the_dash(database, outbox) -> None:
    """B: nothing is invented from the model, the option or the product id."""

    inquiry_id = coupang_inquiry(database, inquiry_id="17000022")
    add_draft(database, inquiry_id)

    assert post(database, inquiry_id, RecordingTransport()).status == "POSTED"

    message = events(outbox)[0]["message"]
    assert "상품명: -" in message
    # Not an id, not a model code, not the option text standing in for a name.
    assert SPID not in message
    assert VENDOR_ITEM not in message


def test_another_inquiry_never_lends_its_product_name(database, outbox) -> None:
    """The enrichment is keyed on the source id, so it must match this row."""

    seed_catalog(database)
    named = coupang_inquiry(database, inquiry_id="17000023")
    add_draft(database, named)
    assert post(database, named, RecordingTransport()).status == "POSTED"
    assert f"상품명: {PRODUCT_NAME}" in events(outbox)[0]["message"]

    # A different account has no catalogue row, so it gets no name at all.
    other = coupang_inquiry(database, account="OJE_PLUS", inquiry_id="17000024")
    add_draft(database, other)
    assert post(database, other, RecordingTransport()).status == "POSTED"
    assert "상품명: -" in events(outbox)[1]["message"]


def test_a_missing_catalogue_never_blocks_the_registration(database, outbox) -> None:
    """A display name is a display name; it cannot fail a confirmed post."""

    from services.coupang_post_service import CoupangPostService as Service

    inquiry_id = coupang_inquiry(database, inquiry_id="17000025")
    add_draft(database, inquiry_id)

    def exploding(*_args, **_kwargs):
        raise RuntimeError("catalogue unavailable")

    from api.coupang_post_client import CoupangPostClient

    poster = Service(
        database,
        client=CoupangPostClient(
            access_key="k", secret_key="s", transport=RecordingTransport(),
        ),
        account_resolver=lambda code: ACCOUNTS[str(code).upper()],
    )
    poster.inquiries.get_by_source = exploding

    assert poster.post(inquiry_id, actor="관리자", confirmed=True).status == "POSTED"
    assert "상품명: -" in events(outbox)[0]["message"]
