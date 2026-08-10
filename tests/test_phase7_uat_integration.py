from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from answer.exceptions import AnswerProviderUnavailableError
from answer.governance_models import GptMode, GptProviderSettings
from answer.provider_errors import (
    GptProviderAuthenticationError,
    GptProviderRetryableError,
    GptProviderTimeoutError,
)
from answer.providers.openai_json_provider import (
    OpenAIJsonProvider,
    OpenAIResponsesTransport,
)
from config import StoreConfig
from repositories.database import Database
from repositories.uat_repository import UatRepository
from services.operational_error_service import create_operational_error
from services.uat_diagnostic_service import UatDiagnosticService
from services.uat_error_mapper import classify_dps_uat_error
from services.uat_order_service import UatOrderService
from services.uat_sync_service import UatInquirySyncService
from ui.uat_presenters import answer_source_label, external_ai_called
from uat.models import UatStatus


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "uat.db")
    db.initialize()
    return db


def real_settings(**overrides) -> GptProviderSettings:
    values = {
        "provider_name": "openai",
        "mode": GptMode.SHADOW,
        "model": "approved-model",
        "approved_by_company": True,
        "api_key_present": True,
        "privacy_protection_enabled": True,
        "allowed_provider_names": ("openai",),
        "allowed_models": ("approved-model",),
    }
    values.update(overrides)
    return GptProviderSettings(**values)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response


def response_payload(answer='{"answer":"확인했습니다."}'):
    return {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": answer}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def test_unapproved_transport_is_blocked_before_network(monkeypatch) -> None:
    monkeypatch.setenv("QNA_GPT_API_KEY", "not-real")
    with pytest.raises(AnswerProviderUnavailableError):
        OpenAIResponsesTransport(real_settings(approved_by_company=False))


def test_missing_key_blocks_transport(monkeypatch) -> None:
    monkeypatch.delenv("QNA_GPT_API_KEY", raising=False)
    with pytest.raises(AnswerProviderUnavailableError, match="API key"):
        OpenAIResponsesTransport(real_settings())


def test_disallowed_provider_is_blocked() -> None:
    settings = real_settings(provider_name="unknown")
    assert any("Provider" in issue for issue in settings.validation_issues())


def test_disallowed_model_is_blocked() -> None:
    settings = real_settings(model="other")
    assert any("모델" in issue for issue in settings.validation_issues())


def test_openai_transport_parses_json_contract(monkeypatch) -> None:
    monkeypatch.setenv("QNA_GPT_API_KEY", "not-real")
    session = FakeSession(FakeResponse(payload=response_payload()))
    transport = OpenAIResponsesTransport(real_settings(), session=session)
    result = transport(
        task="draft",
        prompt="safe prompt",
        context={},
        model="approved-model",
        connect_timeout=5,
        read_timeout=30,
        total_timeout=40,
    )
    assert result["answer"] == "확인했습니다."
    assert result["_usage"]["total_tokens"] == 15
    assert len(session.calls) == 1


def test_transport_uses_responses_endpoint_and_json_mode(monkeypatch) -> None:
    monkeypatch.setenv("QNA_GPT_API_KEY", "not-real")
    session = FakeSession(FakeResponse(payload=response_payload()))
    transport = OpenAIResponsesTransport(real_settings(), session=session)
    transport(
        task="draft", prompt="safe", context={}, model="approved-model",
        connect_timeout=5, read_timeout=30, total_timeout=40,
    )
    args, kwargs = session.calls[0]
    assert args[0].endswith("/v1/responses")
    assert kwargs["json"]["text"]["format"]["type"] == "json_object"
    assert kwargs["timeout"] == (5, 30)


@pytest.mark.parametrize("status", [401, 403])
def test_transport_auth_errors_are_not_retryable(monkeypatch, status: int) -> None:
    monkeypatch.setenv("QNA_GPT_API_KEY", "not-real")
    transport = OpenAIResponsesTransport(
        real_settings(), session=FakeSession(FakeResponse(status))
    )
    with pytest.raises(GptProviderAuthenticationError):
        transport(
            task="draft", prompt="safe", context={}, model="approved-model",
            connect_timeout=5, read_timeout=30, total_timeout=40,
        )


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transport_transient_errors_are_retryable(
    monkeypatch, status: int
) -> None:
    monkeypatch.setenv("QNA_GPT_API_KEY", "not-real")
    transport = OpenAIResponsesTransport(
        real_settings(), session=FakeSession(FakeResponse(status))
    )
    with pytest.raises(GptProviderRetryableError) as caught:
        transport(
            task="draft", prompt="safe", context={}, model="approved-model",
            connect_timeout=5, read_timeout=30, total_timeout=40,
        )
    assert caught.value.status_code == status


def test_transport_timeout_is_distinct(monkeypatch) -> None:
    monkeypatch.setenv("QNA_GPT_API_KEY", "not-real")
    transport = OpenAIResponsesTransport(
        real_settings(),
        session=FakeSession(error=requests.Timeout("secret should not escape")),
    )
    with pytest.raises(GptProviderTimeoutError):
        transport(
            task="draft", prompt="safe", context={}, model="approved-model",
            connect_timeout=5, read_timeout=30, total_timeout=40,
        )


def test_transport_invalid_json_is_contract_failure(monkeypatch) -> None:
    monkeypatch.setenv("QNA_GPT_API_KEY", "not-real")
    transport = OpenAIResponsesTransport(
        real_settings(),
        session=FakeSession(
            FakeResponse(payload=response_payload(answer="not-json"))
        ),
    )
    with pytest.raises(json.JSONDecodeError):
        transport(
            task="draft", prompt="safe", context={}, model="approved-model",
            connect_timeout=5, read_timeout=30, total_timeout=40,
        )


def test_openai_provider_uses_injected_transport_without_network() -> None:
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {"answer": "fixture"}

    result = OpenAIJsonProvider(
        real_settings(), transport=transport
    ).generate_json(task="draft", prompt="safe", context={})
    assert result["answer"] == "fixture"
    assert len(calls) == 1


def work_item(question_id: str, content: str = "문의") -> dict:
    return {
        "store_code": "STORE",
        "source": "CUSTOMER_INQUIRY",
        "inquiry_id": question_id,
        "category": "배송",
        "title": "문의",
        "content": content,
        "product_name": "TV",
        "registered_at": "2026-07-29T00:00:00+09:00",
        "answered": False,
    }


def store(code: str) -> StoreConfig:
    return StoreConfig(code, code, "id", "secret", True)


def test_sync_zero_results_is_success_not_failure(database: Database) -> None:
    service = UatInquirySyncService(database, loader=lambda **kwargs: ([], []))
    result = service.run(stores=[store("A")])
    assert result.successful_store_count == 1
    assert result.fetched_count == 0
    assert result.failed_count == 0


def test_sync_isolates_store_error_and_saves_items(database: Database) -> None:
    def loader(**kwargs):
        return [work_item("q1")], [
            {
                "store_code": "B", "store_name": "B", "stage": "토큰 발급",
                "source": None, "inquiry_id": None, "message": "인증 실패",
            }
        ]

    result = UatInquirySyncService(database, loader=loader).run(
        stores=[store("A"), store("B")]
    )
    assert result.created_count == 1
    assert result.failed_count == 1
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0] == 1


def test_sync_duplicate_upsert_preserves_single_row(database: Database) -> None:
    service = UatInquirySyncService(
        database, loader=lambda **kwargs: ([work_item("q1")], [])
    )
    first = service.run(stores=[store("A")])
    second = service.run(stores=[store("A")])
    assert first.created_count == 1
    assert second.unchanged_count == 1


def test_sync_rejects_bad_date_range(database: Database) -> None:
    with pytest.raises(ValueError, match="1~90"):
        UatInquirySyncService(database, loader=lambda **kwargs: ([], [])).run(
            stores=[], days=0
        )


def seed_inquiry(
    database: Database,
    *,
    order_id: str | None = "2026072912345678",
    product_order_id: str | None = None,
) -> int:
    with database.transaction() as connection:
        return int(
            connection.execute(
                """
                INSERT INTO inquiries (
                    store_code, source_type, source_question_id,
                    inquiry_type, content, order_id, product_order_id
                ) VALUES ('STORE','CUSTOMER_INQUIRY','q1','배송','배송 문의',?,?)
                """,
                (order_id, product_order_id),
            ).lastrowid
        )


def test_order_service_uses_order_id_first(database: Database, monkeypatch) -> None:
    inquiry_id = seed_inquiry(database)
    monkeypatch.setattr(
        "services.uat_order_service.get_store_config",
        lambda code: store("STORE"),
    )
    seen = []

    def lookup(token, number, **kwargs):
        seen.append(number)
        return {
            "success": True, "lookup_number": number, "lookup_type": "ORDER_ID",
            "orders": [], "error_code": None, "error_message": None,
            "cached": False, "queried_at": "now",
        }

    UatOrderService(
        database, token_provider=lambda **kwargs: "token", lookup=lookup
    ).lookup_for_inquiry(inquiry_id)
    assert seen == ["2026072912345678"]


def test_order_service_can_use_product_order_for_naver_only(
    database: Database, monkeypatch
) -> None:
    inquiry_id = seed_inquiry(
        database, order_id=None, product_order_id="2026072912345679"
    )
    monkeypatch.setattr(
        "services.uat_order_service.get_store_config",
        lambda code: store("STORE"),
    )
    seen = []
    UatOrderService(
        database,
        token_provider=lambda **kwargs: "token",
        lookup=lambda token, number, **kwargs: (
            seen.append(number)
            or {
                "success": False, "lookup_number": number, "lookup_type": None,
                "orders": [], "error_code": "NOT_FOUND",
                "error_message": "없음", "cached": False, "queried_at": "now",
            }
        ),
    ).lookup_for_inquiry(inquiry_id)
    assert seen == ["2026072912345679"]


def test_order_service_no_identifier_does_not_call_api(database: Database) -> None:
    inquiry_id = seed_inquiry(database, order_id=None, product_order_id=None)
    result = UatOrderService(
        database,
        token_provider=lambda **kwargs: pytest.fail("token called"),
        lookup=lambda *args, **kwargs: pytest.fail("lookup called"),
    ).lookup_for_inquiry(inquiry_id)
    assert result["error_code"] == "NO_ORDER_NUMBER"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AGENT_CONNECTION_FAILED", "AGENT_OFFLINE"),
        ("AGENT_READ_TIMEOUT", "AGENT_TIMEOUT"),
        ("LOGIN_REQUIRED", "DPS_LOGIN_REQUIRED"),
        ("CHROME_NOT_FOUND", "CHROME_NOT_FOUND"),
        ("DPS_TAB_NOT_FOUND", "DPS_TAB_NOT_FOUND"),
        ("ORDER_INPUT_NOT_FOUND", "ORDER_INPUT_NOT_FOUND"),
        ("QUERY_CONTROL_NOT_FOUND", "QUERY_CONTROL_NOT_FOUND"),
        ("NOT_FOUND", "ORDER_NOT_FOUND"),
        ("MULTIPLE_RESULTS", "MULTIPLE_RESULTS"),
        ("DETAIL_OPEN_FAILED", "DETAIL_OPEN_FAILED"),
        ("PARSE_ERROR", "DETAIL_PARSE_FAILED"),
        ("WINDOW_RESTORE_FAILED", "WINDOW_RESTORE_FAILED"),
        ("OTHER", "UNKNOWN"),
    ],
)
def test_dps_uat_error_mapping(code: str, expected: str) -> None:
    assert classify_dps_uat_error({"success": False, "code": code}) == expected


def test_dps_success_has_no_error() -> None:
    assert classify_dps_uat_error({"success": True}) is None


def test_dps_agent_can_be_running_while_tab_is_missing() -> None:
    assert classify_dps_uat_error(
        {"ok": True, "code": "OK", "login_status": "DPS_TAB_NOT_FOUND"}
    ) == "DPS_TAB_NOT_FOUND"


@pytest.mark.parametrize(
    ("draft", "run", "expected"),
    [
        ({"provider": "rules"}, None, "RULE"),
        ({"provider": "fake_gpt_hybrid"}, None, "FAKE_PROVIDER"),
        (
            {"metadata_json": {"governance": {"fallback_reason": "TIMEOUT"}}},
            None,
            "RULE_FALLBACK",
        ),
        ({}, {"provider": "openai", "mode": "SHADOW"}, "OPENAI_SHADOW"),
        ({}, {"provider": "openai", "mode": "CANARY"}, "OPENAI_CANARY"),
        ({}, {"provider": "openai", "mode": "ACTIVE"}, "OPENAI_ACTIVE"),
    ],
)
def test_answer_source_labels(draft, run, expected) -> None:
    assert answer_source_label(draft, run) == expected


def test_external_ai_flag_is_false_for_fake() -> None:
    assert not external_ai_called({"provider": "fake_gpt_hybrid"})


def test_external_ai_flag_is_true_for_openai() -> None:
    assert external_ai_called({}, {"provider": "openai", "mode": "SHADOW"})


def test_operational_error_contains_structure_without_secret() -> None:
    error = create_operational_error(
        component="Naver", operation="sync",
        error=RuntimeError("token=secret-value"),
        user_message="동기화에 실패했습니다.",
        preserved_data=("기존 문의", "승인"),
        action="인증을 확인하세요.", retryable=True,
    )
    text = str(error)
    assert "secret-value" not in text
    assert error.technical_details["correlation_id"]
    assert error.retryable


def test_uat_diagnostic_has_fourteen_steps(database: Database) -> None:
    report = UatDiagnosticService(database).run(actor="admin", check_dps=False)
    assert len(report.items) == 14
    assert {item.code for item in report.items} == {
        "ENVIRONMENT", "DATABASE", "NAVER_AUTH", "STORE_LOOKUP",
        "INQUIRY_SYNC", "ORDER_LOOKUP", "DPS_AGENT", "DPS_CHROME",
        "DPS_LOOKUP", "RULE_ANSWER", "GPT_GOVERNANCE", "VALIDATOR",
        "APPROVAL", "ACTIVITY_LOG",
    }


def test_uat_dps_offline_is_distinct(database: Database) -> None:
    report = UatDiagnosticService(
        database,
        dps_status_checker=lambda: {
            "ok": False, "code": "AGENT_CONNECTION_FAILED"
        },
    ).run(check_dps=True)
    dps = next(item for item in report.items if item.code == "DPS_AGENT")
    assert dps.failure_type == "AGENT_OFFLINE"


def test_uat_repository_does_not_store_secrets(database: Database) -> None:
    UatDiagnosticService(database).run(actor="admin")
    text = str(UatRepository(database).recent_runs())
    assert "API_KEY=" not in text
    assert "CLIENT_SECRET=" not in text
