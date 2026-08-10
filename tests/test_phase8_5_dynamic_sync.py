from __future__ import annotations

import json
from pathlib import Path

import pytest

from answer.answer_validator import AnswerValidator
from answer.answer_format import format_final_answer
from answer.facts import build_answer_facts
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.prompt_builder import PromptBuilder
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from services.dps_lookup_policy import DpsLookupStatus
from ui.dps_presenter import build_dps_display
from ui.review_workspace import program_answer_widget_key


SOURCE = "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
CASE_DATES = ("2026-07-31", "2026-08-03", "2026-08-11")


def _rule(answer: str = "현재 일정을 확인했습니다.") -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED,
        category="배송/설치현황",
        reason="rule",
        answer=answer,
        provider="rules",
        auto_answerable=True,
        needs_review=False,
    )


def _gpt(answer: str) -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED,
        category="배송/설치현황",
        reason="validated",
        answer=answer,
        provider="openai_hybrid",
        auto_answerable=True,
        needs_review=False,
        metadata={
            "hybrid": {
                "fallback_used": False,
                "validation": {"passed": True},
            },
            "governance": {
                "model": "test-model",
                "prompt_version": "phase8.5",
            },
        },
    )


def _dps(date_value: str) -> dict:
    return {
        "lookup_status": "SUCCESS",
        "installation_date": date_value,
        "required_delivery_date": date_value,
        "installation_date_source": SOURCE,
        "date_parse_status": "PARSED",
        "requires_human_review": False,
        "dps_lookup_id": 10,
        "lookup_timestamp": "2026-01-01T00:00:00+09:00",
    }


@pytest.mark.parametrize("date_value", CASE_DATES)
def test_dynamic_date_flows_through_ui_facts_and_prompt(date_value):
    normalized = _dps(date_value)
    display = build_dps_display(
        lookup_required=True,
        order_id="ORDER",
        latest_row={
            "id": 10,
            "lookup_status": "SUCCESS",
            "normalized_result_json": normalized,
        },
    )
    request = AnswerRequest(
        question="설치 일정은 언제인가요?",
        order_id="ORDER",
        metadata={"dps": normalized},
    )
    facts = build_answer_facts(request, _rule())
    prompt = json.loads(
        PromptBuilder().build(task="DRAFT", facts=facts)
    )

    assert display["installation_date_value"] == date_value
    assert display["installation_date_status_message"] is None
    assert facts.installation["date"] == date_value
    assert facts.installation["required_delivery_date"] == date_value
    assert facts.installation["installation_date_confirmed"] is True
    assert prompt["confirmed_facts"]["installation_date"] == date_value
    assert prompt["confirmed_facts"]["installation_date_status"] == (
        "CONFIRMED"
    )
    assert all(
        other == date_value
        or other not in json.dumps(prompt, ensure_ascii=False)
        for other in CASE_DATES
    )


def test_dashboard_date_value_and_status_message_are_separate():
    display = build_dps_display(
        lookup_required=True,
        order_id="ORDER",
        latest_row={
            "id": 1,
            "lookup_status": "SUCCESS",
            "normalized_result_json": {
                "date_parse_status": "MISSING"
            },
        },
    )
    assert display["installation_date_value"] is None
    assert display["installation_date_status_message"] == (
        "DPS 상세에 요구납기일이 없습니다."
    )


def _inquiry(database: Database, source_id: str, order_id: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": source_id,
            "content": "설치 일정 문의",
            "order_id": order_id,
            "raw_json": {"order_date": "2026-01-01"},
        }
    ).inquiry_id


def _save_dps(
    repository: DpsRepository,
    inquiry_id: int,
    order_id: str,
    date_value: str | None,
    *,
    status: str = "SUCCESS",
) -> dict:
    normalized = (
        _dps(date_value)
        if date_value
        else {
            "lookup_status": status,
            "installation_date": None,
            "date_parse_status": "MISSING",
        }
    )
    return repository.create_lookup_result(
        inquiry_id=inquiry_id,
        order_id=order_id,
        lookup_status=status,
        raw_result={},
        normalized_result=normalized,
    )


def test_latest_success_is_isolated_by_inquiry_and_order(tmp_path):
    database = Database(tmp_path / "isolated.db")
    database.initialize()
    inquiry_a = _inquiry(database, "A", "ORDER-A")
    inquiry_b = _inquiry(database, "B", "ORDER-B")
    repository = DpsRepository(database)
    _save_dps(repository, inquiry_a, "ORDER-A", CASE_DATES[0])
    _save_dps(repository, inquiry_b, "ORDER-B", CASE_DATES[1])
    _save_dps(
        repository,
        inquiry_a,
        "ORDER-A",
        None,
        status="TIMEOUT",
    )

    a = repository.get_preferred_for_inquiry_and_order(
        inquiry_a, "ORDER-A"
    )
    b = repository.get_preferred_for_inquiry_and_order(
        inquiry_b, "ORDER-B"
    )
    assert a["installation_date"] == CASE_DATES[0]
    assert b["installation_date"] == CASE_DATES[1]
    assert repository.get_preferred_for_inquiry_and_order(
        inquiry_a, "ORDER-B"
    ) is None


def _facts(date_value: str | None):
    dps = _dps(date_value) if date_value else {
        "installation_date": None,
        "required_delivery_date": None,
        "installation_date_source": SOURCE,
        "date_parse_status": "MISSING",
    }
    return build_answer_facts(
        AnswerRequest(
            question="설치 일정은 언제인가요?",
            order_id="ORDER",
            metadata={"dps": dps},
        ),
        _rule(),
    )


def _intent() -> IntentResult:
    return IntentResult(
        category="배송/설치현황",
        questions=("설치 일정은 언제인가요?",),
        emotion=Emotion.NORMAL,
        urgency="NORMAL",
        confidence=1.0,
        requires_review=False,
        reason="schedule",
    )


def _review() -> SelfReviewResult:
    return SelfReviewResult(
        passed=True,
        answered_all_questions=True,
        has_speculation=False,
        facts_consistent=True,
        requires_review=False,
        reason="ok",
    )


def _draft(answer: str) -> DraftResult:
    return DraftResult(
        answer=answer,
        confidence=1.0,
        used_facts=(),
        missing_information=(),
        requires_review=False,
        warnings=(),
    )


def test_validator_enforces_dynamic_confirmed_date():
    validator = AnswerValidator()
    passed = validator.validate(
        _facts(CASE_DATES[1]),
        _intent(),
        _draft("현재 확인되는 설치예정일은 2026년 8월 3일입니다."),
        _review(),
    )
    wrong = validator.validate(
        _facts(CASE_DATES[1]),
        _intent(),
        _draft("현재 확인되는 설치예정일은 2026년 8월 11일입니다."),
        _review(),
    )
    missing = validator.validate(
        _facts(CASE_DATES[1]),
        _intent(),
        _draft("구체적인 배송·설치 예정일을 확인할 수 없습니다."),
        _review(),
    )
    invented = validator.validate(
        _facts(None),
        _intent(),
        _draft("현재 확인되는 설치예정일은 2026년 8월 11일입니다."),
        _review(),
    )
    internal = validator.validate(
        _facts(CASE_DATES[1]),
        _intent(),
        _draft("DPS 요구납기일은 2026년 8월 3일입니다."),
        _review(),
    )
    assert passed.passed is True
    assert wrong.passed is False
    assert missing.passed is False
    assert invented.passed is False
    assert internal.passed is False


def test_regeneration_activates_new_version_and_preserves_staff_edit(tmp_path):
    database = Database(tmp_path / "drafts.db")
    database.initialize()
    inquiry_id = _inquiry(database, "DRAFT", "ORDER-D")
    answers = AnswerRepository(database)

    rule = answers.create_program_draft(
        inquiry_id, _rule("Rule answer"), order_id="ORDER-D"
    )
    first_gpt = answers.create_program_draft(
        inquiry_id,
        _gpt("GPT answer one"),
        order_id="ORDER-D",
        dps_lookup_id=1,
    )
    assert rule["is_active"] == 1
    assert first_gpt["is_active"] == 1
    assert answers.active_for_inquiry(inquiry_id)["id"] == first_gpt["id"]

    answers.save_edited_answer(first_gpt["id"], "Protected staff edit")
    second_gpt = answers.create_program_draft(
        inquiry_id,
        _gpt("GPT answer two"),
        order_id="ORDER-D",
        dps_lookup_id=2,
    )
    assert second_gpt["is_active"] == 1
    active = answers.active_for_inquiry(inquiry_id)
    assert active["id"] == second_gpt["id"]
    preserved = answers.get(first_gpt["id"])
    assert preserved["is_active"] == 0
    assert preserved["edited_answer"] == format_final_answer(
        "Protected staff edit"
    )


def test_new_rule_draft_is_a_new_active_version(tmp_path):
    database = Database(tmp_path / "priority.db")
    database.initialize()
    inquiry_id = _inquiry(database, "PRIORITY", "ORDER-P")
    answers = AnswerRepository(database)
    gpt = answers.create_program_draft(
        inquiry_id,
        _gpt("Validated GPT answer"),
        order_id="ORDER-P",
    )
    fallback = answers.create_program_draft(
        inquiry_id,
        _rule("Fallback Rule answer"),
        order_id="ORDER-P",
    )
    assert gpt["is_active"] == 1
    assert fallback["is_active"] == 1
    assert answers.active_for_inquiry(inquiry_id)["id"] == fallback["id"]
    assert answers.get(gpt["id"])["is_active"] == 0


def test_dps_refresh_can_mark_existing_draft_stale(tmp_path):
    database = Database(tmp_path / "stale.db")
    database.initialize()
    inquiry_id = _inquiry(database, "STALE", "ORDER-S")
    answers = AnswerRepository(database)
    draft = answers.create_program_draft(
        inquiry_id,
        _gpt("Validated GPT answer"),
        order_id="ORDER-S",
        dps_lookup_id=1,
    )
    assert answers.mark_unposted_drafts_stale(
        inquiry_id,
        reason="DPS_INSTALLATION_DATE_CHANGED",
    ) == 1
    reloaded = answers.get(draft["id"])
    assert reloaded["stale"] == 1
    assert reloaded["stale_reason"] == (
        "DPS_INSTALLATION_DATE_CHANGED"
    )


def test_widget_key_changes_by_inquiry_and_draft():
    assert program_answer_widget_key(1, 10) != (
        program_answer_widget_key(1, 11)
    )
    assert program_answer_widget_key(1, 10) != (
        program_answer_widget_key(2, 10)
    )


def test_operational_python_has_no_fixture_date_hardcoding():
    root = Path(__file__).resolve().parents[1]
    forbidden = set(CASE_DATES)
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if any(value in text for value in forbidden):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
