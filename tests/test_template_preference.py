from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from answer.answer_format import format_final_answer
from answer.models import AnswerResult, AnswerStatus
from answer.engine import AnswerEngine
from answer.answer_validator import AnswerValidator
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService
from services.approval_service import ApprovalService
from services.phase9_answer_policy import (
    DELIVERY_DATE_ANSWER,
    ORDER_ID_REQUEST_ANSWER,
)
from ui.review_workspace import template_preference_key
from scripts.list_answer_templates import catalog
from streamlit.testing.v1 import AppTest


def _result(
    answer: object,
    *,
    status: AnswerStatus = AnswerStatus.GENERATED,
    provider: str = "rules",
) -> AnswerResult:
    return AnswerResult(
        status=status,
        category="상품",
        reason="test",
        answer=answer,
        provider=provider,
        auto_answerable=status is AnswerStatus.GENERATED,
        needs_review=status is not AnswerStatus.GENERATED,
        matched_rule="OPERATIONS_TEMPLATE",
    )


class StaticEngine:
    def __init__(self, value: AnswerResult) -> None:
        self.value = value
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return self.value


class ForbiddenEngine:
    def generate(self, request):
        raise AssertionError("template/Rule Engine must be skipped")


class RecordingHybrid:
    def __init__(self, answers: list[object]) -> None:
        self.answers = list(answers)
        self.calls = 0

    def generate(self, request, rule_result):
        self.calls += 1
        answer = self.answers.pop(0)
        return SimpleNamespace(
            result=_result(answer, provider="openai_hybrid"),
            events=(),
        )


class ForbiddenHybrid:
    def generate(self, request, rule_result):
        raise AssertionError("GPT must not be called")


class RaisingEngine:
    def generate(self, request):
        raise RuntimeError("template render failed")


class RaisingHybrid:
    def generate(self, request, rule_result):
        raise RuntimeError("gpt failed")


class FakeDps:
    def __init__(self, metadata: dict | None = None) -> None:
        self.metadata = dict(metadata or {})
        self.lookup_calls = 0

    def enrich(self, request, **kwargs):
        self.lookup_calls += 1
        request.metadata["dps"] = dict(self.metadata)
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=True),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
        request.metadata["dps"] = {
            "lookup_required": False,
            "lookup_status": "NOT_REQUIRED",
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(tmp_path / "template-preference.db")
    database.initialize()
    return database


def _inquiry(
    database: Database,
    source_id: str,
    *,
    inquiry_type: str = "상품",
    content: str = "제품 문의입니다.",
    order_id: str | None = None,
    product_order_id: str | None = None,
) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE-A",
            "source_type": "NAVER",
            "source_question_id": source_id,
            "inquiry_type": inquiry_type,
            "content": content,
            "order_id": order_id,
            "product_order_id": product_order_id,
            "raw_json": {},
        }
    ).inquiry_id


def test_preferred_matching_template_records_template_metadata(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database, "MATCH")
    engine = StaticEngine(_result("운영 템플릿 원문"))
    outcome = AnswerService(
        database,
        engine=engine,
        dps_enrichment=FakeDps(),
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id, prefer_template=True)

    metadata = outcome.draft["metadata_json"]
    assert outcome.result.answer == format_final_answer("운영 템플릿 원문")
    assert metadata["generation_mode"] == "TEMPLATE"
    assert metadata["template_preferred"] is True
    assert metadata["template_override"] is False
    assert metadata["template_id"] == "OPERATIONS_TEMPLATE"
    assert metadata["gpt_called"] is False
    assert engine.calls == 1


@pytest.mark.parametrize(
    ("product_name", "question", "expected_category"),
    [
        ("삼성 스마트모니터 M5", "리뷰 포인트는 언제 지급되나요?", "리뷰이벤트"),
        ("삼성 스마트모니터 M5", "온누리상품권 신청 방법이 궁금합니다.", "행사/신청방법"),
        ("삼성 스마트모니터 M5", "넷플릭스 OTT 시청이 가능한가요?", "스마트모니터/OTT"),
        ("삼성 비즈니스TV", "NAS 공유폴더에 연결할 수 있나요?", "비즈니스TV/NAS연결"),
        (
            "M5 32인치 화이트 삼성정품스탠드 무빙스타일",
            "패키지코드가 무엇인가요?",
            "행사/패키지코드",
        ),
    ],
)
def test_connected_fixed_templates_preserve_rule_answers(
    product_name: str,
    question: str,
    expected_category: str,
) -> None:
    result = AnswerEngine().answer(product_name, question)
    assert result.status == "답변 가능"
    assert result.category == expected_category
    assert result.answer.strip()


def test_template_catalog_lists_connected_sources_and_filters() -> None:
    rows = catalog()
    assert rows
    assert all(row["source_file"] for row in rows)
    assert all("connected" in row for row in rows)
    assert any("리뷰" in str(row) for row in rows)
    assert any(row["source_kind"] == "DELIVERY_SAFE_TEMPLATE" for row in rows)


def test_product_db_result_has_independent_route_and_validator(
    database: Database,
) -> None:
    inquiry_id = _inquiry(
        database,
        "PRODUCT-DB",
        inquiry_type="PRODUCT_INQUIRY",
        content="스피커가 내장되어 있나요?",
    )
    product_result = AnswerResult(
        status=AnswerStatus.GENERATED,
        category="모델스펙/스피커",
        reason="JSON 모델 스펙의 speaker 값이 true입니다.",
        answer="문의하신 모델은 스피커가 내장되어 있습니다.",
        provider="rules",
        auto_answerable=True,
        needs_review=False,
        matched_rule="모델스펙/스피커",
    )
    outcome = AnswerService(
        database,
        engine=StaticEngine(product_result),
        dps_enrichment=FakeDps(),
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id, prefer_template=True)
    metadata = outcome.draft["metadata_json"]
    assert metadata["generation_mode"] == "PRODUCT_DB"
    assert metadata["selected_answer_route"] == "PRODUCT_DB"
    assert metadata["gpt_called"] is False


def test_safe_rule_is_selected_before_gpt_and_preserves_original(
    database: Database,
) -> None:
    inquiry_id = _inquiry(
        database,
        "SAFE-RULE-PRE-GPT",
        inquiry_type="PRODUCT_INQUIRY",
        content="폐가전 수거가 가능한가요?",
    )
    original = (
        "안녕하세요. 오제 챗봇입니다.\n\n"
        "폐가전 수거는 방문설치 상품 여부 확인이 필요합니다.\n\n"
        "감사합니다."
    )
    safe_rule = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="폐가전수거",
        reason="방문설치 상품 여부 확인이 필요합니다.",
        answer=original,
        provider="rules",
        auto_answerable=False,
        needs_review=True,
        matched_rule="폐가전수거",
        metadata={"source_status": "추가정보 필요"},
    )
    hybrid = ForbiddenHybrid()

    outcome = AnswerService(
        database,
        engine=StaticEngine(safe_rule),
        dps_enrichment=FakeDps(),
        hybrid_service=hybrid,
    ).generate_for_inquiry(inquiry_id, prefer_template=True)

    metadata = outcome.draft["metadata_json"]
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    answer_step = next(
        row
        for row in WorkflowRepository(database).list_steps(inquiry_id)
        if row["step_code"] == "ANSWER_GENERATED"
    )
    logs = LogRepository(database).recent_for_inquiry(inquiry_id, limit=200)

    wrapped = format_final_answer(original)
    assert outcome.result.answer == wrapped
    assert outcome.draft["original_answer"] == wrapped
    assert metadata["selected_answer_route"] == "SAFE_RULE"
    assert metadata["generation_mode"] == "SAFE_RULE"
    assert metadata["gpt_called"] is False
    assert metadata["validator_result"]["status"] == "PASS"
    assert active is not None and active["id"] == outcome.draft["id"]
    assert active["original_answer"] == wrapped
    assert answer_step["step_status"] == "COMPLETED"
    assert any(row["event_code"] == "SAFE_RULE_SELECTED" for row in logs)
    assert not any(
        row["event_code"].startswith("SIMILAR_ANSWERS_")
        or row["event_code"] == "LEARNING_CONTEXT_APPLIED"
        for row in logs
    )


def test_real_engine_need_info_answer_uses_safe_rule_before_gpt(
    database: Database,
) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "SAFE-RULE-REAL-ENGINE",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "폐가전 수거 문의",
            "content": "폐가전 수거가 가능한가요?",
            "product_name": "일반 상품",
            "raw_json": {},
        }
    ).inquiry_id
    hybrid = ForbiddenHybrid()

    outcome = AnswerService(
        database,
        engine=AnswerEngine(),
        dps_enrichment=FakeDps(),
        hybrid_service=hybrid,
    ).generate_for_inquiry(inquiry_id)

    metadata = outcome.draft["metadata_json"]
    assert outcome.result.status is AnswerStatus.NEEDS_REVIEW
    assert outcome.result.answer.strip()
    assert metadata["selected_answer_route"] == "SAFE_RULE"
    assert metadata["gpt_called"] is False
    assert metadata["validator_result"]["status"] == "PASS"


@pytest.mark.parametrize(
    ("answer", "product_name"),
    [
        ("CONFLICT 상태의 HDMI 사양입니다.", "삼성 모니터"),
        ("model_mismatch 상태입니다.", "삼성 모니터"),
        ("스피커가 내장되어 있습니다.", ""),
    ],
)
def test_product_db_validator_blocks_unverified_internal_states(
    answer: str,
    product_name: str,
) -> None:
    validation = AnswerValidator().validate_product_db_text(
        answer,
        product_name=product_name,
    )
    assert validation.passed is False


def test_preferred_template_miss_calls_gpt_without_override(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database, "MISS")
    engine = StaticEngine(
        _result("", status=AnswerStatus.NOT_SUPPORTED)
    )
    hybrid = RecordingHybrid(["GPT 신규 초안"])
    outcome = AnswerService(
        database,
        engine=engine,
        dps_enrichment=FakeDps(),
        hybrid_service=hybrid,
    ).generate_for_inquiry(inquiry_id)

    metadata = outcome.draft["metadata_json"]
    assert outcome.result.answer == format_final_answer("GPT 신규 초안")
    assert metadata["generation_mode"] == "GPT_FALLBACK"
    assert metadata["template_preferred"] is True
    assert metadata["template_override"] is False
    assert metadata["gpt_called"] is True
    assert hybrid.calls == 1


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"active": False}, "INACTIVE"),
        ({"allowed_stores": ["OTHER"]}, "STORE_MISMATCH"),
        ({"allowed_inquiry_types": ["OTHER"]}, "INQUIRY_TYPE_MISMATCH"),
        ({"relevant": False}, "IRRELEVANT"),
    ],
)
def test_unusable_template_conditions_fall_back_to_gpt(
    database: Database,
    metadata: dict,
    reason: str,
) -> None:
    inquiry_id = _inquiry(database, f"UNUSABLE-{reason}")
    candidate = _result("기존 템플릿")
    candidate.metadata.update(metadata)
    outcome = AnswerService(
        database,
        engine=StaticEngine(candidate),
        dps_enrichment=FakeDps(),
        hybrid_service=RecordingHybrid(["GPT Fallback 답변"]),
    ).generate_for_inquiry(inquiry_id, prefer_template=True)
    assert outcome.result.metadata["generation_mode"] == "GPT_FALLBACK"
    logs = LogRepository(database).recent_for_inquiry(inquiry_id, limit=50)
    assert "GPT_FALLBACK_SUCCESS" in {row["event_code"] for row in logs}


@pytest.mark.parametrize(
    "template_answer",
    [
        "",
        "   ",
        "안녕하세요 {{customer_name}}",
        "답변을 생성할 수 없습니다",
    ],
)
def test_empty_placeholder_or_system_template_falls_back(
    database: Database, template_answer: str
) -> None:
    inquiry_id = _inquiry(database, f"INVALID-{repr(template_answer)}")
    outcome = AnswerService(
        database,
        engine=StaticEngine(_result(template_answer)),
        dps_enrichment=FakeDps(),
        hybrid_service=RecordingHybrid(["검증된 GPT 답변"]),
    ).generate_for_inquiry(inquiry_id)
    assert outcome.result.answer == format_final_answer("검증된 GPT 답변")
    assert outcome.result.metadata["generation_mode"] == "GPT_FALLBACK"


def test_template_render_exception_falls_back_in_one_request(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database, "RENDER-FAIL")
    outcome = AnswerService(
        database,
        engine=RaisingEngine(),
        dps_enrichment=FakeDps(),
        hybrid_service=RecordingHybrid(["한 번의 클릭으로 생성된 GPT 답변"]),
    ).generate_for_inquiry(inquiry_id)
    assert outcome.result.metadata["generation_mode"] == "GPT_FALLBACK"
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)[
        "original_answer"
    ] == format_final_answer("한 번의 클릭으로 생성된 GPT 답변")


def test_gpt_fallback_failure_preserves_existing_draft(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database, "FALLBACK-FAIL")
    old = AnswerRepository(database).create_program_draft(
        inquiry_id, _result("기존 Draft")
    )
    with pytest.raises(RuntimeError):
        AnswerService(
            database,
            engine=StaticEngine(
                _result("", status=AnswerStatus.NOT_SUPPORTED)
            ),
            dps_enrichment=FakeDps(),
            hybrid_service=RaisingHybrid(),
        ).generate_for_inquiry(inquiry_id)
    assert AnswerRepository(database).active_for_inquiry(inquiry_id)[
        "id"
    ] == old["id"]
    assert len(AnswerRepository(database).history_for_inquiry(inquiry_id)) == 1


def test_gpt_failure_without_existing_draft_creates_review_safe_draft(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database, "FALLBACK-SAFE-NO-DRAFT")
    outcome = AnswerService(
        database,
        engine=StaticEngine(_result("", status=AnswerStatus.NOT_SUPPORTED)),
        dps_enrichment=FakeDps(),
        hybrid_service=RaisingHybrid(),
    ).generate_for_inquiry(inquiry_id)

    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active is not None
    assert active["original_answer"].strip()
    assert outcome.result.status is AnswerStatus.NEEDS_REVIEW
    assert outcome.result.metadata["selected_answer_route"] == (
        "REVIEW_REQUIRED_SAFE_DRAFT"
    )
    assert outcome.result.metadata["generation_mode"] == "SAFE_RULE"


def test_trade_statement_policy_has_no_fixed_answer_and_uses_gpt_fallback(
    database: Database,
) -> None:
    inquiry_id = _inquiry(
        database,
        "TRADE-STATEMENT",
        content="거래명세서를 메일로 보내주세요.",
    )
    outcome = AnswerService(
        database,
        dps_enrichment=FakeDps(),
        hybrid_service=RecordingHybrid(
            ["거래명세서 요청을 확인할 수 있도록 안내드리겠습니다."]
        ),
    ).generate_for_inquiry(inquiry_id, prefer_template=True)
    assert outcome.result.metadata["generation_mode"] == "GPT_FALLBACK"


def test_template_override_skips_rule_engine_and_calls_gpt(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database, "OVERRIDE")
    hybrid = RecordingHybrid(["템플릿과 다른 GPT 신규 초안"])
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=FakeDps(),
        hybrid_service=hybrid,
    ).generate_for_inquiry(inquiry_id, prefer_template=False)

    metadata = outcome.draft["metadata_json"]
    assert outcome.result.answer == format_final_answer(
        "템플릿과 다른 GPT 신규 초안"
    )
    assert metadata["generation_mode"] == "GPT_DIRECT"
    assert metadata["template_preferred"] is False
    assert metadata["template_override"] is True
    assert metadata["template_id"] is None
    assert metadata["gpt_called"] is True
    assert hybrid.calls == 1


@pytest.mark.parametrize("prefer_template", [True, False])
def test_missing_order_delivery_safety_cannot_be_overridden(
    database: Database,
    prefer_template: bool,
) -> None:
    inquiry_id = _inquiry(
        database,
        f"DELIVERY-NO-ORDER-{prefer_template}",
        inquiry_type="배송",
        content="배송은 언제 오나요?",
        product_order_id="PRODUCT-ORDER-ONLY",
    )
    dps = FakeDps()
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(
        inquiry_id,
        prefer_template=prefer_template,
    )

    metadata = outcome.draft["metadata_json"]
    assert outcome.result.answer == ORDER_ID_REQUEST_ANSWER
    assert metadata["generation_mode"] == "RULE"
    assert metadata["answer_source"] == "ORDER_ID_REQUEST"
    assert metadata["answer_type"] == "order_id_required"
    assert metadata["template_preferred"] is prefer_template
    assert metadata["template_override"] is False
    assert metadata["gpt_called"] is False
    assert metadata["dps_lookup_attempted"] is False
    assert dps.lookup_calls == 0


@pytest.mark.parametrize("prefer_template", [True, False])
def test_delivery_with_order_always_uses_dps(
    database: Database,
    prefer_template: bool,
) -> None:
    inquiry_id = _inquiry(
        database,
        f"DELIVERY-DPS-{prefer_template}",
        inquiry_type="배송",
        content="배송은 언제 오나요?",
        order_id="2026073012345678",
    )
    dps = FakeDps(
        {
            "lookup_required": True,
            "lookup_status": "SUCCESS",
            "installation_date": "2026-08-05",
            "installation_date_source": (
                "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
            ),
            "date_parse_status": "PARSED",
        }
    )
    outcome = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=dps,
        hybrid_service=ForbiddenHybrid(),
    ).generate_for_inquiry(
        inquiry_id,
        prefer_template=prefer_template,
    )

    metadata = outcome.draft["metadata_json"]
    assert outcome.result.answer == DELIVERY_DATE_ANSWER.format(
        delivery_date="2026년 8월 5일"
    )
    assert metadata["generation_mode"] == "DPS"
    assert metadata["template_override"] is False
    assert metadata["gpt_called"] is False
    assert metadata["dps_lookup_attempted"] is True
    assert dps.lookup_calls == 1


@pytest.mark.parametrize("invalid_answer", [None, "", "   ", "\n\r\n"])
def test_invalid_gpt_answer_keeps_existing_active_draft(
    database: Database,
    invalid_answer: object,
) -> None:
    inquiry_id = _inquiry(database, f"EMPTY-{repr(invalid_answer)}")
    answers = AnswerRepository(database)
    previous = answers.create_program_draft(
        inquiry_id,
        _result("기존 Program Answer"),
    )

    with pytest.raises(Exception):
        AnswerService(
            database,
            engine=ForbiddenEngine(),
            dps_enrichment=FakeDps(),
            hybrid_service=RecordingHybrid([invalid_answer]),
        ).generate_for_inquiry(inquiry_id, prefer_template=False)

    active = answers.active_for_inquiry(inquiry_id)
    assert active["id"] == previous["id"]
    assert active["original_answer"] == format_final_answer("기존 Program Answer")
    assert len(answers.history_for_inquiry(inquiry_id)) == 1


def test_regeneration_creates_new_active_version_and_keeps_history(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database, "VERSIONS")
    answers = AnswerRepository(database)
    first = answers.create_program_draft(
        inquiry_id,
        _result("직원이 수정할 첫 초안"),
    )
    answers.save_edited_answer(first["id"], "직원 수정 내용")
    hybrid = RecordingHybrid(["새 GPT 초안"])

    second = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=FakeDps(),
        hybrid_service=hybrid,
    ).generate_for_inquiry(
        inquiry_id,
        prefer_template=False,
    ).draft

    assert second["id"] != first["id"]
    assert second["is_active"] == 1
    assert answers.active_for_inquiry(inquiry_id)["id"] == second["id"]
    history = answers.history_for_inquiry(inquiry_id)
    assert {row["id"] for row in history} == {first["id"], second["id"]}
    assert answers.get(first["id"])["edited_answer"] == format_final_answer(
        "직원 수정 내용"
    )


def test_approved_final_answer_remains_active_when_new_draft_is_created(
    database: Database,
) -> None:
    inquiry_id = _inquiry(database, "APPROVED")
    answers = AnswerRepository(database)
    approved = answers.create_program_draft(
        inquiry_id,
        _result("승인 대상"),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=approved["id"],
        actor="승인자",
    )
    approval_before = ApprovalRepository(database).get_inquiry_approval(
        inquiry_id
    )

    new_draft = AnswerService(
        database,
        engine=ForbiddenEngine(),
        dps_enrichment=FakeDps(),
        hybrid_service=RecordingHybrid(["별도 신규 초안"]),
    ).generate_for_inquiry(
        inquiry_id,
        prefer_template=False,
    ).draft

    assert new_draft["id"] != approved["id"]
    assert new_draft["is_active"] == 0
    active = answers.active_for_inquiry(inquiry_id)
    assert active["id"] == approved["id"]
    from answer.answer_format import format_final_answer
    assert active["final_answer"] == format_final_answer("승인 대상")
    assert active["review_status"] == "APPROVED"
    approval_after = ApprovalRepository(database).get_inquiry_approval(
        inquiry_id
    )
    assert approval_after["approval_status"] == "APPROVED"
    assert approval_after["approved_by"] == "승인자"
    assert approval_after == approval_before


def test_template_widget_keys_and_program_answers_are_inquiry_scoped(
    database: Database,
) -> None:
    inquiry_a = _inquiry(database, "A")
    inquiry_b = _inquiry(database, "B")
    repository = InquiryRepository(database)
    row_a = repository.get(inquiry_a)
    row_b = repository.get(inquiry_b)
    assert template_preference_key(row_a) != template_preference_key(row_b)
    assert template_preference_key(row_a).endswith(f"_{inquiry_a}")
    assert template_preference_key(row_b).endswith(f"_{inquiry_b}")

    answers = AnswerRepository(database)
    answers.create_program_draft(inquiry_a, _result("문의 A 답변"))
    answers.create_program_draft(inquiry_b, _result("문의 B 답변"))
    assert answers.active_for_inquiry(inquiry_a)["original_answer"] == (
        format_final_answer("문의 A 답변")
    )
    assert answers.active_for_inquiry(inquiry_b)["original_answer"] == (
        format_final_answer("문의 B 답변")
    )


def _answer_app(
    monkeypatch,
    database: Database,
    inquiry_id: int,
    *,
    fake_answer: str = "",
    empty_answer: bool = False,
) -> AppTest:
    monkeypatch.setenv("OJE_AUTOMATION_DB_PATH", str(database.path))
    monkeypatch.setenv("PHASE86_INQUIRY_ID", str(inquiry_id))
    monkeypatch.setenv("PHASE86_PANEL", "answer")
    monkeypatch.setenv("QNA_GPT_PROVIDER", "fake")
    if fake_answer:
        monkeypatch.setenv("PHASE86_FAKE_ANSWER", fake_answer)
    else:
        monkeypatch.delenv("PHASE86_FAKE_ANSWER", raising=False)
    if empty_answer:
        monkeypatch.setenv("PHASE86_EMPTY_ANSWER", "1")
    else:
        monkeypatch.delenv("PHASE86_EMPTY_ANSWER", raising=False)
    return AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "uat" / "phase86_streamlit_probe.py")
    ).run(timeout=30)


def test_apptest_default_checked_then_override_creates_rendered_version(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = _inquiry(database, "APPTEST-OVERRIDE")
    answers = AnswerRepository(database)
    old = answers.create_program_draft(
        inquiry_id,
        _result("기존 Program Answer"),
    )
    app = _answer_app(
        monkeypatch,
        database,
        inquiry_id,
        fake_answer="체크 해제 후 GPT 새 초안",
    )

    preference = next(
        item
        for item in app.checkbox
        if item.label == "확정 운영 템플릿 사용"
    )
    assert preference.value is True
    assert preference.key.endswith(f"_{inquiry_id}")
    preference.uncheck()
    app = app.run(timeout=30)
    assert any(
        button.label == "GPT 새 답변 생성" for button in app.button
    )
    next(
        button
        for button in app.button
        if button.label == "GPT 새 답변 생성"
    ).click()
    app = app.run(timeout=30)

    assert not app.exception
    current = answers.active_for_inquiry(inquiry_id)
    assert current["id"] != old["id"]
    assert current["original_answer"] == format_final_answer(
        "체크 해제 후 GPT 새 초안"
    )
    assert len(answers.history_for_inquiry(inquiry_id)) == 2
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.value == format_final_answer("체크 해제 후 GPT 새 초안")
    assert any("GPT" in item.value for item in app.success)
    rendered_log = next(
        row
        for row in LogRepository(database).recent_for_inquiry(inquiry_id)
        if row["event_code"] == "ANSWER_GENERATION_RENDERED"
    )
    required_log_fields = {
        "inquiry_id",
        "answer_type",
        "answer_source",
        "generation_mode",
        "template_preferred",
        "template_override",
        "template_id",
        "order_id_present",
        "delivery_question",
        "dps_lookup_attempted",
        "delivery_date_found",
        "gpt_called",
        "draft_id",
        "draft_length",
        "draft_saved",
        "active_draft_id",
        "rendered_draft_id",
    }
    assert required_log_fields <= rendered_log["details_json"].keys()
    assert rendered_log["details_json"]["rendered_draft_id"] == current["id"]
    assert "order_id" not in rendered_log["details_json"]


def test_apptest_empty_gpt_result_has_no_success_and_keeps_program_answer(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = _inquiry(database, "APPTEST-EMPTY")
    answers = AnswerRepository(database)
    old = answers.create_program_draft(
        inquiry_id,
        _result("보존할 기존 Program Answer"),
    )
    app = _answer_app(
        monkeypatch,
        database,
        inquiry_id,
        empty_answer=True,
    )
    next(
        button
        for button in app.button
        if button.label == "GPT 새 답변 생성"
    ).click()
    app = app.run(timeout=30)

    assert not app.exception
    assert not app.success
    assert app.error
    assert answers.active_for_inquiry(inquiry_id)["id"] == old["id"]
    assert len(answers.history_for_inquiry(inquiry_id)) == 1
    app.segmented_control[0].set_value("Program Answer")
    app = app.run(timeout=30)
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.value == format_final_answer("보존할 기존 Program Answer")


def test_apptest_checked_general_inquiry_uses_template_and_survives_rerun(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = _inquiry(
        database,
        "APPTEST-TEMPLATE",
        content="온누리상품권 신청 방법이 궁금합니다.",
    )
    app = _answer_app(monkeypatch, database, inquiry_id)
    preference = next(
        item
        for item in app.checkbox
        if item.label == "확정 운영 템플릿 사용"
    )
    assert preference.value is True
    next(
        button
        for button in app.button
        if button.label == "GPT 새 답변 생성"
    ).click()
    app = app.run(timeout=30)

    assert not app.exception
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active["metadata_json"]["generation_mode"] == "TEMPLATE"
    assert active["metadata_json"]["gpt_called"] is False
    expected = active["original_answer"]
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.value == expected
    app = app.run(timeout=30)
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.value == expected


def test_apptest_684104045_checked_template_miss_renders_gpt_fallback(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "684104045",
            "external_inquiry_id": "684104045",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품 문의",
            "content": "tv로도 사용하려면 어떻게 해야 하나요??",
            "product_name": (
                "삼성 삼탠바이미 32인치(80cm) M5 스마트 모니터 "
                "IPTV+2in1 이동식 거치대"
            ),
            "raw_json": {},
        }
    ).inquiry_id
    expected = (
        "TV로 사용하는 방법은 연결 환경에 따라 달라집니다. "
        "사용하려는 방송 수신기나 셋톱박스 정보를 알려주세요."
    )
    app = _answer_app(
        monkeypatch,
        database,
        inquiry_id,
        fake_answer=expected,
    )
    preference = next(
        item
        for item in app.checkbox
        if item.label == "확정 운영 템플릿 사용"
    )
    assert preference.value is True
    next(
        button
        for button in app.button
        if button.label == "GPT 새 답변 생성"
    ).click()
    app = app.run(timeout=30)

    assert not app.exception
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active is not None
    assert active["metadata_json"]["generation_mode"] == "GPT_FALLBACK"
    assert active["metadata_json"]["template_preferred"] is True
    assert active["metadata_json"]["gpt_called"] is True
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.value == active["original_answer"]
    assert any(
        "적용 가능한 기존 템플릿이 없어 GPT로 새 답변" in item.value
        for item in app.success
    )


def test_apptest_unchecked_delivery_still_uses_safe_template(
    database: Database,
    monkeypatch,
) -> None:
    inquiry_id = _inquiry(
        database,
        "APPTEST-DELIVERY-SAFE",
        inquiry_type="배송",
        content="배송은 언제 오나요?",
    )
    app = _answer_app(monkeypatch, database, inquiry_id)
    preference = next(
        item
        for item in app.checkbox
        if item.label == "확정 운영 템플릿 사용"
    )
    preference.uncheck()
    app = app.run(timeout=30)
    next(
        button
        for button in app.button
        if button.label == "주문번호 요청 답변 생성"
    ).click()
    app = app.run(timeout=30)

    assert not app.exception
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active["original_answer"] == ORDER_ID_REQUEST_ANSWER
    assert active["metadata_json"]["generation_mode"] == "RULE"
    assert active["metadata_json"]["answer_source"] == "ORDER_ID_REQUEST"
    assert active["metadata_json"]["template_preferred"] is False
    assert active["metadata_json"]["template_override"] is False
    program = next(
        area for area in app.text_area if area.label == "Program Answer"
    )
    assert program.value == ORDER_ID_REQUEST_ANSWER
