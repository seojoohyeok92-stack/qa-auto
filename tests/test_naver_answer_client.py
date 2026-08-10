from __future__ import annotations

import pytest
import requests

from api.naver_answer_client import (
    NaverAlreadyAnsweredError,
    NaverAnswerClient,
    NaverAnswerClientError,
)
from services.naver_post_payload_builder import NaverPostPayloadBuilder


class FakeResponse:
    def __init__(self, status_code: int, body=None) -> None:
        self.status_code = status_code
        self.body = body if body is not None else {}

    def json(self):
        return self.body


class RecordingSession:
    def __init__(self, response=None, error=None) -> None:
        self.response = response or FakeResponse(204)
        self.error = error
        self.calls = []

    def request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _request(source_type: str):
    return NaverPostPayloadBuilder().build(
        source_type=source_type,
        external_id="12345",
        store="STORE",
        final_answer='안내 <완료> & "확인"\n둘째 줄 😀',
    )


def test_product_answer_uses_put_and_raw_json_body() -> None:
    session = RecordingSession(FakeResponse(204))
    result = NaverAnswerClient(session=session).send(
        _request("PRODUCT_INQUIRY"), access_token="test-token"
    )
    method, endpoint, kwargs = session.calls[0]
    assert result.http_status == 204
    assert method == "PUT"
    assert endpoint.endswith("/v1/contents/qnas/12345")
    assert kwargs["json"] == {
        "commentContent": '안내 <완료> & "확인"\n둘째 줄 😀'
    }
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_customer_answer_uses_post_and_answer_comment() -> None:
    session = RecordingSession(
        FakeResponse(200, {"data": {"inquiryCommentNo": "reply-1"}})
    )
    result = NaverAnswerClient(session=session).send(
        _request("CUSTOMER_INQUIRY"), access_token="test-token"
    )
    method, endpoint, kwargs = session.calls[0]
    assert result.response_id == "reply-1"
    assert method == "POST"
    assert endpoint.endswith(
        "/v1/pay-merchant/inquiries/12345/answer"
    )
    assert kwargs["json"] == {
        "answerComment": '안내 <완료> & "확인"\n둘째 줄 😀'
    }


def test_timeout_is_uncertain_and_never_automatically_retried() -> None:
    session = RecordingSession(error=requests.Timeout("timeout"))
    with pytest.raises(NaverAnswerClientError) as captured:
        NaverAnswerClient(session=session).send(
            _request("PRODUCT_INQUIRY"), access_token="test-token"
        )
    assert captured.value.code == "API_TIMEOUT"
    assert captured.value.uncertain is True
    assert len(session.calls) == 1


def test_already_answered_code_is_distinct() -> None:
    session = RecordingSession(
        FakeResponse(400, {"code": "ERR-NC-101010"})
    )
    with pytest.raises(NaverAlreadyAnsweredError):
        NaverAnswerClient(session=session).send(
            _request("CUSTOMER_INQUIRY"), access_token="test-token"
        )
    assert len(session.calls) == 1
