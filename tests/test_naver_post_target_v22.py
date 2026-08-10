from __future__ import annotations

import pytest

from api.naver_answer_client import NaverAnswerClient, NaverAnswerClientError
from services.naver_post_payload_builder import (
    NaverPostPayloadBuilder,
    NaverPostTargetError,
)


def inquiry(
    *,
    local_id: int = 2054,
    source_type: str = "PRODUCT_INQUIRY",
    external_id: str = "684140427",
    raw_id: object = 684140427,
    raw_field: str | None = None,
    store: str = "OJE_PLUS",
) -> dict:
    field = raw_field or (
        "questionId" if source_type == "PRODUCT_INQUIRY" else "inquiryNo"
    )
    return {
        "id": local_id,
        "store_code": store,
        "source_type": source_type,
        "external_inquiry_id": external_id,
        "source_question_id": external_id,
        "raw_json": {
            "source": source_type,
            field: raw_id,
            "source_payload": {field: raw_id},
        },
    }


def test_product_target_uses_raw_question_id_not_local_inquiry_id() -> None:
    target = NaverPostPayloadBuilder().resolve_target(
        inquiry(), require_remote_snapshot=True
    )
    assert target.external_id == "684140427"
    assert target.external_id != "2054"
    assert target.method == "PUT"
    assert target.body_field == "commentContent"
    assert target.expected_success_status == 204
    assert target.endpoint.endswith("/v1/contents/qnas/684140427")


def test_local_inquiry_id_cannot_be_used_as_product_question_id() -> None:
    with pytest.raises(NaverPostTargetError) as captured:
        NaverPostPayloadBuilder().resolve_target(
            inquiry(external_id="2054", raw_id=2054),
            require_remote_snapshot=True,
        )
    assert captured.value.code == "TARGET_ID_MAPPING_ERROR"


def test_customer_target_uses_inquiry_no_post_and_answer_comment() -> None:
    target = NaverPostPayloadBuilder().resolve_target(
        inquiry(
            source_type="CUSTOMER_INQUIRY",
            external_id="90001",
            raw_id=90001,
        ),
        require_remote_snapshot=True,
    )
    built = NaverPostPayloadBuilder().build_for_target(
        target=target, final_answer="answer"
    )
    assert target.external_id == "90001"
    assert built.method == "POST"
    assert built.payload == {"answerComment": "answer"}
    assert built.expected_success_status == 200
    assert built.endpoint.endswith("/v1/pay-merchant/inquiries/90001/answer")


def test_product_target_builds_put_comment_content() -> None:
    builder = NaverPostPayloadBuilder()
    target = builder.resolve_target(inquiry(), require_remote_snapshot=True)
    built = builder.build_for_target(target=target, final_answer="answer")
    assert built.method == "PUT"
    assert built.payload == {"commentContent": "answer"}


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "EXTERNAL_ID_REQUIRED"),
        ("684***427", "MASKED_EXTERNAL_ID"),
    ],
)
def test_missing_or_masked_external_id_is_blocked(value: str, code: str) -> None:
    with pytest.raises(NaverPostTargetError) as captured:
        NaverPostPayloadBuilder().resolve_target(
            inquiry(external_id=value, raw_id=value),
            require_remote_snapshot=True,
        )
    assert captured.value.code == code


def test_inquiry_type_and_raw_endpoint_identity_mismatch_is_blocked() -> None:
    with pytest.raises(NaverPostTargetError) as captured:
        NaverPostPayloadBuilder().resolve_target(
            inquiry(raw_field="inquiryNo"), require_remote_snapshot=True
        )
    assert captured.value.code == "INQUIRY_TYPE_ENDPOINT_MISMATCH"


def test_persisted_and_raw_external_ids_must_match() -> None:
    with pytest.raises(NaverPostTargetError) as captured:
        NaverPostPayloadBuilder().resolve_target(
            inquiry(external_id="111", raw_id=222),
            require_remote_snapshot=True,
        )
    assert captured.value.code == "TARGET_ID_MAPPING_ERROR"


def test_target_missing_from_latest_successful_snapshot_is_blocked() -> None:
    stale = inquiry()
    stale["last_synced_at"] = "2026-08-05T06:02:39.885+00:00"
    with pytest.raises(NaverPostTargetError) as captured:
        NaverPostPayloadBuilder().resolve_target(
            stale,
            require_remote_snapshot=True,
            latest_sync_started_at="2026-08-06T01:56:44.000+00:00",
        )
    assert captured.value.code == "TARGET_NOT_FOUND"


def test_target_seen_in_latest_successful_snapshot_is_verified() -> None:
    current = inquiry()
    current["last_synced_at"] = "2026-08-06T01:56:50.000+00:00"
    target = NaverPostPayloadBuilder().resolve_target(
        current,
        require_remote_snapshot=True,
        latest_sync_started_at="2026-08-06T01:56:44.000+00:00",
    )
    assert target.remote_snapshot_verified is True


class Response404:
    status_code = 404

    @staticmethod
    def json() -> dict:
        return {}


class OneCallSession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        return Response404()


def test_verified_404_is_remote_target_not_found_and_never_retried() -> None:
    builder = NaverPostPayloadBuilder()
    request = builder.build_for_target(
        target=builder.resolve_target(inquiry(), require_remote_snapshot=True),
        final_answer="answer",
    )
    session = OneCallSession()
    with pytest.raises(NaverAnswerClientError) as captured:
        NaverAnswerClient(session=session).send(
            request, access_token="mock-token"
        )
    assert captured.value.code == "REMOTE_TARGET_NOT_FOUND"
    assert captured.value.http_status == 404
    assert captured.value.retryable is False
    assert captured.value.uncertain is False
    assert session.calls == 1
