from __future__ import annotations

from dataclasses import replace

import pytest

from answer.governance_models import GptMode, GptProviderSettings
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.provider_errors import GptProviderTimeoutError
from answer.providers.fake_gpt_provider import FakeGptProvider
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.gpt_provider_run_repository import GptProviderRunRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService
from services.gpt_governance_service import (
    GovernedHybridAnswerService,
    canary_selected,
)
from ui.gpt_governance_panel import build_governance_status
from ui.review_workspace import build_gpt_diagnostics


def rule(
    answer: str = "검증된 Rule Answer",
    *,
    needs_review: bool = False,
) -> AnswerResult:
    return AnswerResult(
        status=(
            AnswerStatus.NEEDS_REVIEW
            if needs_review
            else AnswerStatus.GENERATED
        ),
        category="제품 기능",
        reason="Rule",
        answer=answer,
        provider="rules",
        auto_answerable=not needs_review,
        needs_review=needs_review,
        matched_rule="RULE",
    )


def request(
    inquiry_id: int = 1,
    question: str = "PC와 연결해서 OTT도 볼 수 있나요?",
    inquiry_type: str = "PRODUCT_INQUIRY",
) -> AnswerRequest:
    return AnswerRequest(
        inquiry_id=inquiry_id,
        question_id=f"Q-{inquiry_id}",
        inquiry_type=inquiry_type,
        question=question,
        product_name="삼성 스마트모니터 M5",
        metadata={"source_type": inquiry_type},
    )


def settings(mode: GptMode, **overrides) -> GptProviderSettings:
    values = {
        "provider_name": "fake",
        "mode": mode,
        "model": "fake-json-v1",
        "max_retries": 0,
        "requests_per_minute": 1_000,
        "daily_request_limit": 10_000,
        "per_inquiry_request_limit": 100,
        "regeneration_cooldown_seconds": 0,
    }
    values.update(overrides)
    return GptProviderSettings(**values)


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "governance.db")
    database.initialize()
    return database


def test_fake_mode_keeps_existing_hybrid_result(database: Database) -> None:
    outcome = GovernedHybridAnswerService(
        database, settings=settings(GptMode.FAKE)
    ).generate(request(), rule())
    assert outcome.result.provider == "fake_gpt_hybrid"
    assert outcome.result.metadata["governance"]["mode"] == "FAKE"
    assert outcome.fallback_used is False


def test_disabled_mode_skips_gpt_and_uses_rule(database: Database) -> None:
    provider = FakeGptProvider()
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.DISABLED),
        provider=provider,
    ).generate(request(), rule())
    assert outcome.result.answer == "검증된 Rule Answer"
    assert outcome.result.provider == "rules"
    assert provider.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"approved_by_company": False, "api_key_present": True},
        {"approved_by_company": True, "api_key_present": False},
        {"approved_by_company": True, "api_key_present": True, "model": ""},
    ],
)
def test_active_real_provider_invalid_configuration_falls_back(
    database: Database, overrides: dict
) -> None:
    config = settings(
        GptMode.ACTIVE,
        provider_name="openai",
        model=overrides.pop("model", "model"),
        **overrides,
    )
    outcome = GovernedHybridAnswerService(
        database, settings=config, provider=FakeGptProvider()
    ).generate(request(), rule())
    assert outcome.result.provider == "rules"
    assert (
        outcome.result.metadata["governance"]["fallback_reason"]
        == "CONFIGURATION_INVALID"
    )
    assert "GPT_CONFIGURATION_INVALID" in {
        event.code for event in outcome.events
    }


def test_active_fake_fixture_canary_independent_of_company_gate(
    database: Database,
) -> None:
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.ACTIVE),
        provider=FakeGptProvider(),
    ).generate(request(), rule())
    assert outcome.result.provider == "fake_gpt_hybrid"


def test_privacy_block_prevents_real_provider_call(database: Database) -> None:
    provider = FakeGptProvider()
    config = settings(
        GptMode.ACTIVE,
        provider_name="openai",
        model="approved-model",
        approved_by_company=True,
        api_key_present=True,
    )
    outcome = GovernedHybridAnswerService(
        database, settings=config, provider=provider
    ).generate(request(question="token=secret-value"), rule())
    assert provider.calls == []
    assert outcome.result.provider == "rules"
    assert "GPT_PRIVACY_BLOCKED" in {
        event.code for event in outcome.events
    }


def test_shadow_never_replaces_program_answer(database: Database) -> None:
    provider = FakeGptProvider(
        responses={
            "DRAFT": {
                "answer": "비교용 외부 답변",
                "confidence": 0.9,
                "used_facts": ["rule.answer"],
                "missing_information": [],
                "requires_review": False,
                "warnings": [],
            }
        }
    )
    outcome = GovernedHybridAnswerService(
        database, settings=settings(GptMode.SHADOW), provider=provider
    ).generate(request(), rule())
    assert outcome.result.answer == "검증된 Rule Answer"
    assert outcome.result.provider == "rules"
    assert outcome.result.metadata["governance"]["shadow"] is True
    run = GptProviderRunRepository(database).recent(limit=1)[0]
    assert run["shadow_comparison_json"]["gpt_length"] == len(
        "비교용 외부 답변"
    )


def test_shadow_event_is_recorded(database: Database) -> None:
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.SHADOW),
        provider=FakeGptProvider(),
    ).generate(request(), rule())
    assert "GPT_SHADOW_COMPLETED" in {
        event.code for event in outcome.events
    }


def test_canary_hash_is_deterministic() -> None:
    first = canary_selected("same-inquiry", 37.5)
    assert all(canary_selected("same-inquiry", 37.5) == first for _ in range(20))


def test_canary_zero_and_hundred_percent_boundaries() -> None:
    assert canary_selected("any", 0) is False
    assert canary_selected("any", 100) is True


def test_canary_selected_requires_employee_review(database: Database) -> None:
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.CANARY, canary_percentage=100),
        provider=FakeGptProvider(),
    ).generate(request(), rule())
    assert outcome.result.metadata["governance"]["canary_selected"] is True
    assert outcome.result.needs_review is True
    assert outcome.result.auto_answerable is False
    assert outcome.result.status is AnswerStatus.NEEDS_REVIEW


def test_canary_percentage_skip_uses_fake_safely(database: Database) -> None:
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.CANARY, canary_percentage=0),
        provider=FakeGptProvider(),
    ).generate(request(), rule())
    assert outcome.result.metadata["governance"]["canary_selected"] is False
    assert "GPT_CANARY_SKIPPED" in {event.code for event in outcome.events}


@pytest.mark.parametrize(
    "question",
    ["환불해주세요", "법적 대응하겠습니다", "반품 분쟁 문의"],
)
def test_high_risk_canary_is_excluded(
    database: Database, question: str
) -> None:
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.CANARY, canary_percentage=100),
        provider=FakeGptProvider(),
    ).generate(request(question=question), rule())
    assert outcome.result.provider == "rules"
    assert (
        outcome.result.metadata["governance"]["fallback_reason"]
        == "CANARY_EXCLUDED"
    )


def test_rule_forced_review_is_excluded_from_canary(
    database: Database,
) -> None:
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.CANARY, canary_percentage=100),
    ).generate(request(), rule(needs_review=True))
    assert outcome.result.provider == "rules"
    assert outcome.result.needs_review is True


def test_timeout_fixture_falls_back_and_records_timeout(
    database: Database,
) -> None:
    provider = FakeGptProvider(fail_tasks={"DRAFT"})
    # Hybrid normalizes the injected failure as provider failure.
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.ACTIVE),
        provider=provider,
    ).generate(request(), rule())
    assert outcome.result.provider == "rules"
    assert "GPT_PROVIDER_FAILED" in {event.code for event in outcome.events}


class TimeoutProvider:
    name = "timeout-provider"

    def generate_json(self, **kwargs):
        raise GptProviderTimeoutError("read timeout")


def test_timeout_event_is_distinct(database: Database) -> None:
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.ACTIVE),
        provider=TimeoutProvider(),
    ).generate(request(), rule())
    assert "GPT_PROVIDER_TIMEOUT" in {event.code for event in outcome.events}
    assert GptProviderRunRepository(database).recent(limit=1)[0][
        "error_type"
    ] == "GPT_PROVIDER_TIMEOUT"


def test_rate_limit_falls_back_without_app_error(database: Database) -> None:
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.ACTIVE, requests_per_minute=0),
    ).generate(request(), rule())
    assert outcome.result.provider == "rules"
    assert "GPT_PROVIDER_RATE_LIMITED" in {
        event.code for event in outcome.events
    }


def test_cost_limit_falls_back(database: Database) -> None:
    repository = GptProviderRunRepository(database)
    now = "2026-07-29T00:00:00+00:00"
    repository.create_run(
        inquiry_id=None,
        correlation_id="cost-existing",
        provider="fake",
        model="fake",
        mode="FAKE",
        prompt_version="p",
        policy_version="p",
        privacy_policy_version="p",
        validator_policy_version="p",
        company_tone_version="p",
        started_at=now,
        completed_at=now,
        success=True,
        estimated_cost_krw=100,
    )
    outcome = GovernedHybridAnswerService(
        database,
        settings=settings(GptMode.ACTIVE, daily_cost_limit_krw=50),
    ).generate(request(), rule())
    assert outcome.result.provider == "rules"
    assert "GPT_PROVIDER_COST_LIMITED" in {
        event.code for event in outcome.events
    }


class StaticEngine:
    def generate(self, request):
        return rule(needs_review=True)


def create_db_inquiry(database: Database, *, posted: bool = False) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "GOVERNED-DB",
            "inquiry_type": "PRODUCT_INQUIRY",
            "content": "PC와 연결해서 OTT도 볼 수 있나요?",
            "product_name": "삼성 스마트모니터 M5",
            "post_status": "POSTED" if posted else "NOT_POSTED",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


def test_answer_service_attaches_provider_run_to_draft(
    database: Database,
) -> None:
    inquiry_id = create_db_inquiry(database)
    service = GovernedHybridAnswerService(
        database, settings=settings(GptMode.FAKE)
    )
    outcome = AnswerService(
        database, engine=StaticEngine(), hybrid_service=service
    ).generate_for_inquiry(inquiry_id)
    run = GptProviderRunRepository(database).recent(
        inquiry_id=inquiry_id, limit=1
    )[0]
    assert run["draft_id"] == outcome.draft["id"]


def test_answer_service_activity_contains_governance_events(
    database: Database,
) -> None:
    inquiry_id = create_db_inquiry(database)
    service = GovernedHybridAnswerService(
        database, settings=settings(GptMode.FAKE)
    )
    AnswerService(
        database, engine=StaticEngine(), hybrid_service=service
    ).generate_for_inquiry(inquiry_id)
    codes = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(
            inquiry_id, limit=50
        )
    }
    assert "GPT_PROVIDER_REQUESTED" in codes
    assert "GPT_PROVIDER_SUCCEEDED" in codes


def test_posted_inquiry_blocks_before_provider_call(
    database: Database,
) -> None:
    inquiry_id = create_db_inquiry(database, posted=True)
    provider = FakeGptProvider()
    service = GovernedHybridAnswerService(
        database, settings=settings(GptMode.ACTIVE), provider=provider
    )
    with pytest.raises(Exception, match="등록"):
        AnswerService(
            database, engine=StaticEngine(), hybrid_service=service
        ).generate_for_inquiry(inquiry_id)
    assert provider.calls == []
    assert GptProviderRunRepository(database).recent(
        inquiry_id=inquiry_id
    ) == []


def test_api_key_never_appears_in_draft_metadata(
    database: Database, monkeypatch
) -> None:
    secret = "company-secret-key-value"
    monkeypatch.setenv("QNA_GPT_API_KEY", secret)
    outcome = GovernedHybridAnswerService(
        database, settings=GptProviderSettings.from_environment()
    ).generate(request(), rule())
    assert secret not in str(outcome.result.metadata)


def test_ui_diagnostics_include_governance_run() -> None:
    draft = {
        "metadata_json": {
            "governance": {
                "mode": "SHADOW",
                "provider": "fake",
                "model": "fake-json-v1",
            }
        }
    }
    run = {"duration_ms": 123, "retry_count": 1}
    diagnostics = build_gpt_diagnostics(draft, run)
    assert diagnostics["governance"]["mode"] == "SHADOW"
    assert diagnostics["provider_run"]["duration_ms"] == 123


def test_governance_settings_ui_status_hides_key_value() -> None:
    config = settings(GptMode.FAKE, api_key_present=True)
    status = build_governance_status(config, {"requests": 3})
    assert status["api_key_configured"] is True
    assert "api_key" not in str(status).lower().replace(
        "api_key_configured", ""
    )
