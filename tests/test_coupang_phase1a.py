from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest

from api.coupang_read_client import (
    CoupangReadClient,
    CoupangReadError,
    build_authorization,
    serialize_query,
)
from config import CoupangReadSettings
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.coupang_inquiry_normalizer import (
    COUPANG_CONTACT_CENTER_INQUIRY,
    COUPANG_ONLINE_INQUIRY,
    CoupangInquiryNormalizer,
)
from services.coupang_inquiry_sync_service import CoupangInquirySyncService
from services.inquiry_sync_service import InquirySyncService


FIXED_NOW = datetime(2026, 9, 14, 12, 34, 56, tzinfo=UTC)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self.payload = {} if payload is None else payload

    def json(self) -> Any:
        return self.payload


class FakeTransport:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(transport: Any, **kwargs: Any) -> CoupangReadClient:
    return CoupangReadClient(
        access_key="test-access-key",
        secret_key="test-secret-key",
        vendor_id="A00000000",
        transport=transport,
        now=lambda: FIXED_NOW,
        min_request_interval_seconds=0,
        **kwargs,
    )


def online_payload(inquiry_id: int, *, order_ids: list[int] | None = None) -> dict:
    return {
        "inquiryId": inquiry_id,
        "productId": 1001,
        "sellerProductId": 2001,
        "sellerItemId": 3001,
        "vendorItemId": 4001,
        "content": "상품 문의입니다.",
        "inquiryAt": "2026-09-14T10:00:00+09:00",
        "orderIds": [] if order_ids is None else order_ids,
        "commentDtoList": [],
    }


def contact_payload(inquiry_id: int) -> dict:
    return {
        "inquiryId": inquiry_id,
        "inquiryStatus": "progress",
        "csPartnerCounselingStatus": "requestAnswer",
        "vendorItemId": 4001,
        "itemName": "테스트 TV 옵션",
        "content": "배송 관련 문의입니다.",
        "inquiryAt": "2026-09-14T10:10:00+09:00",
        "buyerEmail": "buyer@example.com",
        "buyerPhone": "010-1234-5678",
        "orderId": 1234567890123,
        "replies": [],
    }


def page(items: list[dict], *, current: int = 1, total: int = 1) -> dict:
    return {
        "code": 200,
        "message": "OK",
        "data": {
            "content": items,
            "pagination": {
                "currentPage": current,
                "totalPages": total,
                "totalElements": len(items),
                "countPerPage": len(items),
            },
        },
    }


def test_hmac_uses_utc_timestamp_and_exact_query() -> None:
    path = "/v2/providers/openapi/apis/api/v5/vendors/A00000000/onlineInquiries"
    query = (
        "answeredType=ALL&inquiryEndAt=2026-09-14&"
        "inquiryStartAt=2026-09-14&pageNum=1&pageSize=50&vendorId=A00000000"
    )
    authorization = build_authorization(
        "GET", path, query, "test-access-key", "test-secret-key", FIXED_NOW
    )
    message = "260914T123456ZGET" + path + query
    expected_signature = hmac.new(
        b"test-secret-key", message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert authorization == (
        "CEA algorithm=HmacSHA256, access-key=test-access-key, "
        "signed-date=260914T123456Z, signature=" + expected_signature
    )


def test_coupang_settings_are_lazy_and_do_not_require_startup_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "COUPANG_ACCESS_KEY",
        "COUPANG_SECRET_KEY",
        "COUPANG_VENDOR_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    assert not CoupangReadSettings.from_environment().configured
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "test-access-key")
    monkeypatch.setenv("COUPANG_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("COUPANG_VENDOR_ID", "A00000000")
    assert CoupangReadSettings.from_environment().configured


def test_read_client_signs_the_exact_transmitted_query() -> None:
    transport = FakeTransport([FakeResponse(200, page([]))])
    read_client = client(transport)
    read_client.list_online_inquiries(
        inquiry_start_at=date(2026, 9, 14),
        inquiry_end_at=date(2026, 9, 14),
    )
    call = transport.calls[0]
    parsed = urlsplit(call["url"])
    expected = build_authorization(
        "GET",
        parsed.path,
        parsed.query,
        "test-access-key",
        "test-secret-key",
        FIXED_NOW,
    )
    assert call["method"] == "GET"
    assert call["headers"]["Authorization"] == expected
    assert parsed.query == serialize_query({
        "vendorId": "A00000000",
        "answeredType": "ALL",
        "inquiryStartAt": "2026-09-14",
        "inquiryEndAt": "2026-09-14",
        "pageNum": 1,
        "pageSize": 50,
    })


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(400, "INVALID_PARAMETER"), (401, "AUTH_FAILED"), (403, "PERMISSION_DENIED"),
     (429, "RATE_LIMITED"), (500, "API_SERVER_ERROR")],
)
def test_read_client_classifies_http_errors(status_code: int, code: str) -> None:
    read_client = client(FakeTransport([FakeResponse(status_code)]), max_retries=0)
    with pytest.raises(CoupangReadError) as error:
        read_client.list_online_inquiries(
            inquiry_start_at=date(2026, 9, 14),
            inquiry_end_at=date(2026, 9, 14),
        )
    assert error.value.code == code
    assert "test-secret-key" not in str(error.value)


def test_read_client_retries_network_and_server_errors_without_real_network() -> None:
    sleeps: list[float] = []
    transport = FakeTransport([
        OSError("offline fake transport"),
        FakeResponse(200, page([])),
    ])
    read_client = client(
        transport,
        sleeper=sleeps.append,
        max_retries=1,
        retry_backoff_seconds=0.25,
    )
    assert read_client.list_online_inquiries(
        inquiry_start_at=date(2026, 9, 14),
        inquiry_end_at=date(2026, 9, 14),
    )["code"] == 200
    assert len(transport.calls) == 2
    assert sleeps == [0.25]


def test_read_client_rejects_invalid_range_and_missing_configuration() -> None:
    read_client = client(FakeTransport([]))
    with pytest.raises(CoupangReadError, match="7 days"):
        read_client.list_online_inquiries(
            inquiry_start_at=date(2026, 9, 1),
            inquiry_end_at=date(2026, 9, 10),
        )
    unconfigured = CoupangReadClient(
        access_key="", secret_key="", vendor_id="", transport=FakeTransport([])
    )
    with pytest.raises(CoupangReadError) as error:
        unconfigured.list_online_inquiries(
            inquiry_start_at=date(2026, 9, 14),
            inquiry_end_at=date(2026, 9, 14),
        )
    assert error.value.code == "CONFIGURATION_ERROR"


def test_online_normalizer_preserves_ids_and_handles_order_cardinality() -> None:
    normalizer = CoupangInquiryNormalizer()
    no_order = normalizer.online(online_payload(11)).to_work_item()
    one_order = normalizer.online(online_payload(12, order_ids=[111])).to_work_item()
    many_orders = normalizer.online(online_payload(13, order_ids=[111, 222])).to_work_item()
    assert no_order["source"] == COUPANG_ONLINE_INQUIRY
    assert no_order["product_id"] == "2001"
    assert no_order["order_id"] is None
    assert one_order["order_id"] == "111"
    assert many_orders["order_id"] is None
    assert many_orders["raw_payload"]["orderIds"] == [111, 222]
    assert many_orders["raw_payload"]["vendorItemId"] == 4001

    answered = online_payload(14)
    answered["commentDtoList"] = [{"inquiryCommentId": 9001, "content": "답변"}]
    assert normalizer.online(answered).answered is True


def test_contact_normalizer_masks_pii_and_preserves_documented_metadata() -> None:
    work_item = CoupangInquiryNormalizer().contact_center(
        contact_payload(21)
    ).to_work_item()
    raw = work_item["raw_payload"]
    assert work_item["source"] == COUPANG_CONTACT_CENTER_INQUIRY
    assert work_item["product_id"] is None
    assert work_item["product_name"] == "테스트 TV 옵션"
    assert work_item["order_id"] == "1234567890123"
    assert raw["vendorItemId"] == 4001
    assert raw["buyerEmail"] == "<masked-email>"
    assert raw["buyerPhone"] == "<masked-phone>"
    assert "buyer@example.com" not in str(raw)
    assert "010-1234-5678" not in str(raw)

    answered = contact_payload(22)
    answered["csPartnerCounselingStatus"] = "answered"
    assert CoupangInquiryNormalizer().contact_center(answered).answered is True


class RecordingReadClient:
    def __init__(self) -> None:
        self.online_calls: list[tuple[date, date, int]] = []
        self.contact_calls: list[tuple[date, date, int, str]] = []

    def list_online_inquiries(self, **kwargs: Any) -> dict[str, Any]:
        self.online_calls.append((
            kwargs["inquiry_start_at"], kwargs["inquiry_end_at"], kwargs["page_num"]
        ))
        if kwargs["page_num"] == 1 and kwargs["inquiry_start_at"] == date(2026, 9, 1):
            return page([online_payload(101)], current=1, total=2)
        if kwargs["page_num"] == 2:
            return page([online_payload(102)], current=2, total=2)
        return page([])

    def list_contact_center_inquiries(self, **kwargs: Any) -> dict[str, Any]:
        self.contact_calls.append((
            kwargs["inquiry_start_at"], kwargs["inquiry_end_at"],
            kwargs["page_num"], kwargs["partner_counseling_status"],
        ))
        inquiry_id = 201 if kwargs["inquiry_start_at"] == date(2026, 9, 1) else 202
        return page([contact_payload(inquiry_id)]) if kwargs["page_num"] == 1 else page([])


class CountingAutomaticDrafts:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_for_inquiry(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("Coupang collection must not draft answers")


def test_sync_paginates_chunks_upserts_idempotently_and_never_drafts(tmp_path) -> None:
    database = Database(tmp_path / "coupang-sync.db")
    database.initialize()
    drafts = CountingAutomaticDrafts()
    persistence = InquirySyncService(
        InquiryRepository(database),
        WorkflowRepository(database),
        LogRepository(database),
        automatic_drafts=drafts,  # type: ignore[arg-type]
        automatic_processing_enabled=lambda: False,
    )
    read_client = RecordingReadClient()
    service = CoupangInquirySyncService(read_client, persistence)  # type: ignore[arg-type]

    first = service.sync_inquiries(
        start_date=date(2026, 9, 1), end_date=date(2026, 9, 10)
    )
    second = service.sync_inquiries(
        start_date=date(2026, 9, 1), end_date=date(2026, 9, 10)
    )

    assert first.new == 4
    assert second.new == 0
    assert second.unchanged == 4
    assert drafts.calls == 0
    assert read_client.online_calls[:3] == [
        (date(2026, 9, 1), date(2026, 9, 8), 1),
        (date(2026, 9, 1), date(2026, 9, 8), 2),
        (date(2026, 9, 9), date(2026, 9, 10), 1),
    ]
    assert all(call[3] == "NONE" for call in read_client.contact_calls)
    repository = InquiryRepository(database)
    assert repository.count() == 4
    stored = repository.get_by_source("COUPANG", COUPANG_CONTACT_CENTER_INQUIRY, "201")
    assert stored is not None
    assert "buyer@example.com" not in str(stored["raw_json"])
    assert "010-1234-5678" not in str(stored["raw_json"])
