from __future__ import annotations

import pytest

from answer.facts import build_answer_facts
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService
from services.hybrid_answer_service import HybridAnswerService
from ui.review_workspace import build_gpt_diagnostics


def request(question: str = "넷플릭스 되나요?") -> AnswerRequest:
    return AnswerRequest(
        inquiry_id=1,
        question_id="HYBRID-1",
        inquiry_type="상품",
        question=question,
        product_name="삼성 스마트모니터 M5",
        metadata={
            "source_type": "PRODUCT_INQUIRY",
            "dps": {
                "lookup_required": False,
                "lookup_status": "NOT_REQUIRED",
                "warnings": [],
            },
        },
    )


def rule(
    *,
    answer: str = "인터넷 연결 시 넷플릭스를 사용할 수 있습니다.",
    needs_review: bool = False,
) -> AnswerResult:
    return AnswerResult(
        status=(
            AnswerStatus.NEEDS_REVIEW
            if needs_review
            else AnswerStatus.GENERATED
        ),
        category="스마트모니터/OTT",
        reason="OTT Rule",
        answer=answer,
        provider="rules",
        auto_answerable=not needs_review,
        needs_review=needs_review,
        matched_rule="스마트모니터/OTT",
    )


class StaticEngine:
    def __init__(self, result: AnswerResult) -> None:
        self.result = result

    def generate(self, request: AnswerRequest) -> AnswerResult:
        return self.result


def test_hybrid_approved_result_uses_fake_provider() -> None:
    outcome = HybridAnswerService(FakeGptProvider()).generate(
        request(), rule()
    )
    assert outcome.fallback_used is False
    assert outcome.result.provider == "fake_gpt_hybrid"
    assert outcome.result.answer == rule().answer
    assert outcome.validation and outcome.validation.passed


def test_hybrid_metadata_contains_all_diagnostics() -> None:
    outcome = HybridAnswerService(FakeGptProvider()).generate(
        request(), rule()
    )
    hybrid = outcome.result.metadata["hybrid"]
    assert set(hybrid) >= {
        "provider",
        "fallback_used",
        "facts",
        "intent",
        "draft",
        "self_review",
        "validation",
    }


def test_hybrid_compound_questions_are_tracked() -> None:
    outcome = HybridAnswerService(FakeGptProvider()).generate(
        request("넷플릭스 되나요? 배송은 언제 오나요? 설치도 하나요?"),
        rule(),
    )
    assert len(outcome.result.metadata["hybrid"]["intent"]["questions"]) == 3


def test_hybrid_provider_failure_falls_back_to_rule() -> None:
    outcome = HybridAnswerService(
        FakeGptProvider(fail_tasks={"DRAFT"})
    ).generate(request(), rule())
    assert outcome.fallback_used is True
    assert outcome.result.provider == "rules"
    assert outcome.result.answer == rule().answer
    assert outcome.result.metadata["hybrid"]["fallback_reason"] == "RUNTIMEERROR"


def test_hybrid_invalid_fact_reference_falls_back() -> None:
    provider = FakeGptProvider(
        responses={
            "DRAFT": {
                "answer": "확정 답변",
                "confidence": 0.9,
                "used_facts": ["installation.date"],
                "missing_information": [],
                "requires_review": False,
                "warnings": [],
            }
        }
    )
    outcome = HybridAnswerService(provider).generate(request(), rule())
    assert outcome.fallback_used is True
    assert outcome.result.answer == rule().answer
    assert any(
        "존재하지 않는 Fact" in error
        for error in outcome.validation.errors
    )


def test_hybrid_speculative_answer_falls_back() -> None:
    provider = FakeGptProvider(
        responses={
            "DRAFT": {
                "answer": "아마 내일 배송될 것 같습니다.",
                "confidence": 0.8,
                "used_facts": ["rule.answer"],
                "missing_information": [],
                "requires_review": False,
                "warnings": [],
            }
        }
    )
    outcome = HybridAnswerService(provider).generate(request(), rule())
    assert outcome.fallback_used is True
    assert outcome.result.answer == rule().answer


def test_hybrid_never_removes_rule_review_requirement() -> None:
    outcome = HybridAnswerService(FakeGptProvider()).generate(
        request(), rule(answer="담당자 확인이 필요합니다.", needs_review=True)
    )
    assert outcome.result.needs_review is True
    assert outcome.result.auto_answerable is False


def test_hybrid_emits_required_success_events() -> None:
    outcome = HybridAnswerService(FakeGptProvider()).generate(
        request(), rule()
    )
    codes = [event.code for event in outcome.events]
    assert codes == [
        "PHASE9_FACTS_SELECTED",
        "GPT_FACTS_READY",
        "ANSWER_FACTS_INSTALLATION_DATE_INCLUDED",
        "GPT_PROMPT_FACTS_READY",
        "GPT_PROMPT_READY",
        "GPT_ANALYSIS_STARTED",
        "GPT_PROVIDER_STARTED",
        "GPT_ANALYSIS_COMPLETED",
        "GPT_RESPONSE_NORMALIZED",
        "GPT_RESPONSE_RECEIVED",
        "GPT_DRAFT_CREATED",
        "GPT_PROVIDER_FINISHED",
        "GPT_SELF_REVIEW",
        "GPT_VALIDATOR_STARTED",
        "GPT_VALIDATOR_FINISHED",
        "GPT_APPROVED",
    ]


def test_hybrid_emits_validation_and_fallback_events() -> None:
    provider = FakeGptProvider(
        responses={
            "DRAFT": {
                "answer": "2026-12-31에 설치됩니다.",
                "confidence": 0.9,
                "used_facts": ["rule.answer"],
                "missing_information": [],
                "requires_review": False,
                "warnings": [],
            }
        }
    )
    codes = [
        event.code
        for event in HybridAnswerService(provider).generate(
            request(), rule()
        ).events
    ]
    assert "GPT_VALIDATION_FAILED" in codes
    assert codes[-1] == "GPT_FALLBACK_RULE"


def test_answer_facts_do_not_include_customer_display() -> None:
    req = request()
    req.customer_display = "홍길동 010-1234-5678"
    facts = build_answer_facts(req, rule())
    assert "홍길동" not in str(facts.to_prompt_dict())
    assert "010-1234-5678" not in str(facts.to_prompt_dict())


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "hybrid.db")
    database.initialize()
    return database


def create_inquiry(database: Database) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "HYBRID-DB-1",
            "inquiry_type": "상품",
            "content": "넷플릭스 되나요?",
            "product_name": "삼성 스마트모니터 M5",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


def test_answer_service_persists_hybrid_metadata(database: Database) -> None:
    inquiry_id = create_inquiry(database)
    outcome = AnswerService(
        database,
        engine=StaticEngine(rule(needs_review=True)),
        hybrid_service=HybridAnswerService(FakeGptProvider()),
    ).generate_for_inquiry(inquiry_id)
    stored = AnswerRepository(database).get(outcome.draft["id"])
    assert stored["metadata_json"]["hybrid"]["provider"] == "fake_gpt"
    assert stored["provider"] == "fake_gpt_hybrid"


def test_answer_service_records_all_gpt_success_events(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database)
    AnswerService(
        database,
        engine=StaticEngine(rule(needs_review=True)),
        hybrid_service=HybridAnswerService(FakeGptProvider()),
    ).generate_for_inquiry(inquiry_id)
    codes = {
        row["event_code"]
        for row in LogRepository(database).recent_for_inquiry(
            inquiry_id, limit=30
        )
    }
    assert {
        "GPT_ANALYSIS_STARTED",
        "GPT_ANALYSIS_COMPLETED",
        "GPT_DRAFT_CREATED",
        "GPT_SELF_REVIEW",
        "GPT_APPROVED",
    } <= codes


def test_answer_service_fallback_keeps_workflow_success(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database)
    outcome = AnswerService(
        database,
        engine=StaticEngine(rule()),
        hybrid_service=HybridAnswerService(
            FakeGptProvider(fail_tasks={"DRAFT"})
        ),
    ).generate_for_inquiry(inquiry_id)
    step = WorkflowRepository(database).get_step(
        inquiry_id, "ANSWER_GENERATED"
    )
    assert outcome.result.provider == "rules"
    assert step["step_status"] == "COMPLETED"


def test_ui_diagnostics_reads_persisted_metadata(
    database: Database,
) -> None:
    inquiry_id = create_inquiry(database)
    draft = AnswerService(
        database,
        engine=StaticEngine(rule(needs_review=True)),
        hybrid_service=HybridAnswerService(FakeGptProvider()),
    ).generate_for_inquiry(inquiry_id).draft
    diagnostics = build_gpt_diagnostics(draft)
    assert diagnostics["confidence"] == 0.97
    assert diagnostics["intent"]["emotion"] == "NORMAL"
    assert diagnostics["validation"]["passed"] is True


def test_ui_diagnostics_handles_legacy_draft() -> None:
    assert build_gpt_diagnostics(
        {"metadata_json": {}, "original_answer": "기존 답변"}
    ) is None


def test_migration_v4_is_reentrant_and_preserves_metadata(
    database: Database,
) -> None:
    assert database.migration_versions() == list(range(1, 24))
    assert database.initialize() == []
    with database.connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(answer_drafts)"
            )
        }
    assert "metadata_json" in columns
