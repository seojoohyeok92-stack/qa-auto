from __future__ import annotations

import json
from pathlib import Path

from answer.answer_validator import AnswerValidator
from answer.fact_selection import FactSelectionService
from answer.facts import build_answer_facts
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
)
from answer.inquiry_analysis import (
    AnswerStrategy,
    InquiryType,
    OrderIdStatus,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.prompt_builder import PromptBuilder
from answer.providers.fake_gpt_provider import FakeGptProvider
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import mask_sensitive_data
from services.hybrid_answer_service import HybridAnswerService
from services.answer_service import AnswerService
from services.dps_enrichment_service import DpsEnrichmentService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.inquiry_sync_service import normalize_work_item
from services.phase9_answer_policy import apply_phase9_rule_policy
from streamlit.testing.v1 import AppTest


def request(
    question: str,
    *,
    order_id: str = "",
    product_order_id: str = "",
) -> AnswerRequest:
    return AnswerRequest(
        inquiry_id=1,
        question_id="P9",
        inquiry_type="고객문의",
        question=question,
        product_name="삼성 TV",
        order_id=order_id,
        product_order_id=product_order_id,
    )


def rule(answer: str = "확인이 필요합니다.") -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED,
        category="일반",
        reason="rule",
        answer=answer,
        provider="rules",
        auto_answerable=True,
        needs_review=False,
    )


def confirmed_request(
    date_value: str = "2026-08-03",
) -> tuple[AnswerRequest, object]:
    req = request("설치 일정은 언제인가요?", order_id="2026073012345678")
    req.metadata["dps"] = {
        "lookup_required": True,
        "lookup_status": "SUCCESS",
        "installation_date": date_value,
        "required_delivery_date": date_value,
        "installation_date_source": (
            "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
        ),
        "date_parse_status": "PARSED",
        "dps_lookup_id": 7,
    }
    analysis = InquiryAnalysisService().analyze(req)
    req.metadata["phase9_analysis"] = analysis.to_dict()
    return req, analysis


def validation_parts(answer: str):
    return (
        IntentResult(
            category="일정",
            questions=("설치 일정",),
            emotion=Emotion.NORMAL,
            urgency="NORMAL",
            confidence=1,
            requires_review=False,
            reason="test",
        ),
        DraftResult(answer=answer, confidence=1),
        SelfReviewResult(
            passed=True,
            answered_all_questions=True,
            has_speculation=False,
            facts_consistent=True,
            requires_review=False,
            reason="test",
        ),
    )


def test_delivery_with_valid_order_selects_only_installation_facts() -> None:
    req, analysis = confirmed_request()
    facts = build_answer_facts(req, rule())
    selected = FactSelectionService().select(facts, analysis)
    assert analysis.inquiry_type is InquiryType.DELIVERY_INSTALLATION_STATUS
    assert analysis.order_id_status is OrderIdStatus.VALIDATED
    assert "installation.date" in selected.keys
    assert "delivery.status" not in selected.keys
    assert "order.order_status" not in selected.keys


def test_delivery_without_order_requests_private_order_id() -> None:
    req = request("설치 일정은 언제인가요?")
    analysis = InquiryAnalysisService().analyze(req)
    result = apply_phase9_rule_policy(req, rule(), analysis)
    assert analysis.inquiry_type is InquiryType.ORDER_INFO_REQUIRED
    assert analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID
    assert "네이버쇼핑의 주문·배송 조회 화면" in result.answer
    assert "주문번호" in result.answer
    assert "오제 챗봇(Chat Bot)이 답변드립니다." in result.answer
    assert "확인 후" in result.answer
    assert "2026" not in result.answer


def test_general_number_is_not_treated_as_valid_order_id() -> None:
    analysis = InquiryAnalysisService().analyze(
        request("설치 비용이 30000원인가요?")
    )
    assert analysis.order_id_status is OrderIdStatus.NOT_REQUIRED
    assert not analysis.order_id_validated


def test_body_order_candidate_is_not_validated_without_api_snapshot() -> None:
    analysis = InquiryAnalysisService().analyze(
        request("설치일 확인 2026073012345678")
    )
    assert analysis.order_id_status is OrderIdStatus.CANDIDATE_FOUND
    assert not analysis.order_id_validated
    assert analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID


def test_product_order_id_is_never_used_as_general_order_id() -> None:
    analysis = InquiryAnalysisService().analyze(
        request(
            "배송일은 언제인가요?",
            product_order_id="202607301234567890",
        )
    )
    assert analysis.order_id_status is OrderIdStatus.AMBIGUOUS
    assert analysis.requires_dps_lookup
    assert not analysis.can_execute_dps_lookup
    assert analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID


def test_product_general_does_not_require_order_or_dps() -> None:
    analysis = InquiryAnalysisService().analyze(
        request("이 모델은 넷플릭스 기능을 지원하나요?")
    )
    assert analysis.inquiry_type is InquiryType.PRODUCT_GENERAL
    assert not analysis.requires_order_lookup
    assert not analysis.requires_dps_lookup
    assert analysis.answer_strategy is AnswerStrategy.GENERAL_GUIDANCE


def test_installation_general_does_not_require_order() -> None:
    analysis = InquiryAnalysisService().analyze(
        request("벽걸이 설치 조건이 궁금합니다.")
    )
    assert analysis.inquiry_type is InquiryType.INSTALLATION_GENERAL
    assert analysis.order_id_status is OrderIdStatus.NOT_REQUIRED


def test_confirmed_date_policy_removes_internal_purchase_status() -> None:
    req, analysis = confirmed_request("2026-07-31")
    result = apply_phase9_rule_policy(req, rule(), analysis)
    assert "2026년 7월 31일" in result.answer
    assert "구매요청 상태" not in result.answer
    assert "DPS" not in result.answer
    assert "요구납기일" not in result.answer


def test_selected_prompt_excludes_unselected_internal_facts() -> None:
    req, analysis = confirmed_request()
    facts = build_answer_facts(req, rule())
    selected = FactSelectionService().select(facts, analysis)
    payload = json.loads(
        PromptBuilder().build(
            task="DRAFT",
            facts=facts,
            analysis=analysis,
            selected_facts=selected,
        )
    )
    assert payload["allowed_facts"]["installation.date"] == "2026-08-03"
    assert "delivery.status" not in payload["allowed_facts"]
    assert payload["answer_strategy"] == "DIRECT_FACT_ANSWER"


def test_request_order_prompt_requires_private_guidance() -> None:
    req = request("배송 언제 오나요?")
    analysis = InquiryAnalysisService().analyze(req)
    req.metadata["phase9_analysis"] = analysis.to_dict()
    facts = build_answer_facts(req, rule())
    selected = FactSelectionService().select(facts, analysis)
    payload = json.loads(
        PromptBuilder().build(
            task="DRAFT",
            facts=facts,
            analysis=analysis,
            selected_facts=selected,
        )
    )
    assert any("비밀글" in item for item in payload["required_content"])


def test_validator_blocks_missing_private_post_guidance() -> None:
    req = request("배송일은 언제인가요?")
    analysis = InquiryAnalysisService().analyze(req)
    req.metadata["phase9_analysis"] = analysis.to_dict()
    facts = build_answer_facts(req, rule())
    selected = FactSelectionService().select(facts, analysis)
    intent, draft, review = validation_parts(
        "네이버 주문번호를 남겨주시면 확인 후 안내드리겠습니다."
    )
    result = AnswerValidator().validate(
        facts,
        intent,
        draft,
        review,
        analysis=analysis,
        selected_facts=selected,
    )
    assert result.status == "BLOCK"
    assert any(
        item.code == "PRIVATE_POST_GUIDANCE_REQUIRED"
        and item.status == "BLOCK"
        for item in result.rules
    )


def test_validator_blocks_unverified_date() -> None:
    req = request("배송일은 언제인가요?")
    analysis = InquiryAnalysisService().analyze(req)
    req.metadata["phase9_analysis"] = analysis.to_dict()
    facts = build_answer_facts(req, rule())
    selected = FactSelectionService().select(facts, analysis)
    intent, draft, review = validation_parts(
        "설치 예정일은 2026년 8월 11일입니다."
    )
    result = AnswerValidator().validate(
        facts,
        intent,
        draft,
        review,
        analysis=analysis,
        selected_facts=selected,
    )
    assert result.status == "BLOCK"
    assert any(
        item.code == "UNVERIFIED_DATE_BLOCK"
        and item.status == "BLOCK"
        for item in result.rules
    )


def test_validator_blocks_internal_dps_term() -> None:
    req, analysis = confirmed_request()
    facts = build_answer_facts(req, rule())
    selected = FactSelectionService().select(facts, analysis)
    intent, draft, review = validation_parts(
        "DPS 확인 결과 설치 예정일은 2026년 8월 3일입니다."
    )
    result = AnswerValidator().validate(
        facts,
        intent,
        draft,
        review,
        analysis=analysis,
        selected_facts=selected,
    )
    assert any(
        item.code == "INTERNAL_STATUS_LEAK" and item.status == "BLOCK"
        for item in result.rules
    )


def test_validator_blocks_cross_inquiry_date() -> None:
    req, analysis = confirmed_request("2026-08-03")
    facts = build_answer_facts(req, rule())
    selected = FactSelectionService().select(facts, analysis)
    intent, draft, review = validation_parts(
        "설치 예정일은 2026년 8월 11일입니다."
    )
    result = AnswerValidator().validate(
        facts,
        intent,
        draft,
        review,
        analysis=analysis,
        selected_facts=selected,
    )
    assert any(
        item.code == "CROSS_INQUIRY_CONTAMINATION"
        and item.status == "BLOCK"
        for item in result.rules
    )


def test_safe_order_request_template_uses_dedicated_validation_route() -> None:
    req = request("배송은 언제 오나요?")
    analysis = InquiryAnalysisService().analyze(req)
    req.metadata["phase9_analysis"] = analysis.to_dict()
    policy = apply_phase9_rule_policy(req, rule(), analysis)
    assert policy.status is AnswerStatus.GENERATED
    assert policy.metadata["answer_type"] == "order_id_required"
    assert policy.metadata["answer_source"] == "ORDER_ID_REQUEST"
    assert policy.metadata["gpt_called"] is False


def test_phase9_draft_columns_persist_atomically(tmp_path) -> None:
    database = Database(tmp_path / "phase9.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S",
            "source_type": "Q",
            "source_question_id": "P9-DB",
            "content": "설치 일정은 언제인가요?",
            "raw_json": {},
        }
    ).inquiry_id
    req, analysis = confirmed_request()
    req.inquiry_id = inquiry_id
    req.metadata["phase9_analysis"] = analysis.to_dict()
    policy = apply_phase9_rule_policy(req, rule(), analysis)
    outcome = HybridAnswerService(FakeGptProvider()).generate(req, policy)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        outcome.result,
        order_id=req.order_id,
        facts_version="phase9-selected-facts-v1",
    )
    assert draft["answer_strategy"] == "DIRECT_FACT_ANSWER"
    assert draft["inquiry_analysis_json"]["inquiry_type"]
    assert "installation.date" in draft["selected_facts_json"]["keys"]
    assert draft["validator_result_json"]["status"] == "PASS"


def test_private_metadata_is_preserved_when_source_provides_it() -> None:
    normalized = normalize_work_item(
        {
            "store_code": "S",
            "source": "NAVER",
            "inquiry_id": "PRIVATE-1",
            "content": "비밀 문의",
            "isPrivate": True,
        }
    )
    assert normalized["is_private"] is True
    assert normalized["source_metadata_json"]["privacy_source_present"]


def test_missing_private_metadata_is_not_guessed() -> None:
    normalized = normalize_work_item(
        {
            "store_code": "S",
            "source": "NAVER",
            "inquiry_id": "PUBLIC-UNKNOWN",
            "content": "문의",
        }
    )
    assert normalized["is_private"] is None
    assert not normalized["source_metadata_json"]["privacy_source_present"]


def test_log_masking_hides_full_order_identifier() -> None:
    masked = mask_sensitive_data(
        {"order_id": "2026073012345678", "status": "READY"}
    )
    assert "2026073012345678" not in str(masked)


def test_program_answer_css_is_scoped_and_high_contrast() -> None:
    source = (
        __import__("pathlib")
        .Path("ui/review_workspace.py")
        .read_text(encoding="utf-8")
    )
    assert ".st-key-{draft_session_key} textarea:disabled" in source
    assert "-webkit-text-fill-color: #ffffff" in source
    assert "program_answer_widget_key" in source


def test_naver_post_is_gated_by_disabled_by_default_setting() -> None:
    source = (
        __import__("pathlib")
        .Path("ui/review_workspace.py")
        .read_text(encoding="utf-8")
    )
    assert 'code == "NAVER_POST"' in source
    assert "NaverPostSettings.from_environment()" in source
    assert "not settings.enabled" in source


def test_order_info_required_service_never_calls_dps_agent(
    tmp_path,
) -> None:
    database = Database(tmp_path / "order-info.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S",
            "source_type": "NAVER",
            "source_question_id": "NO-ORDER",
            "content": "설치 일정은 언제인가요?",
            "raw_json": {},
        }
    ).inquiry_id
    calls: list[object] = []

    def forbidden_client(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("DPS Agent must not be called")

    class StaticEngine:
        def generate(self, answer_request):
            result = rule()
            result.status = AnswerStatus.NEEDS_REVIEW
            result.auto_answerable = False
            result.needs_review = True
            return result

    outcome = AnswerService(
        database,
        engine=StaticEngine(),
        dps_enrichment=DpsEnrichmentService(
            database,
            client=forbidden_client,
        ),
        hybrid_service=HybridAnswerService(FakeGptProvider()),
    ).generate_for_inquiry(inquiry_id)
    saved = InquiryRepository(database).get(inquiry_id)
    assert calls == []
    assert outcome.result.status is AnswerStatus.GENERATED
    assert "네이버쇼핑의 주문·배송 조회 화면" in outcome.draft[
        "original_answer"
    ]
    assert saved["phase9_status"] == "ORDER_INFO_REQUIRED"
    assert outcome.draft["validation_status"] == "PASS"


def test_private_flag_round_trips_through_migration_v10(tmp_path) -> None:
    database = Database(tmp_path / "private.db")
    database.initialize()
    normalized = normalize_work_item(
        {
            "store_code": "S",
            "source": "NAVER",
            "inquiry_id": "PRIVATE-ROUNDTRIP",
            "content": "비밀 문의",
            "isPrivate": True,
        }
    )
    inquiry_id = InquiryRepository(database).upsert_work_item(
        normalized
    ).inquiry_id
    stored = InquiryRepository(database).get(inquiry_id)
    assert stored["is_private"] is True
    assert stored["source_metadata_json"]["privacy_source_present"] is True


def test_order_info_required_program_answer_renders_immediately(
    tmp_path,
    monkeypatch,
) -> None:
    database = Database(tmp_path / "phase9-ui.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S",
            "source_type": "NAVER",
            "source_question_id": "PHASE9-UI",
            "content": "설치 일정은 언제인가요?",
            "raw_json": {},
        }
    ).inquiry_id
    req = request("설치 일정은 언제인가요?")
    req.inquiry_id = inquiry_id
    analysis = InquiryAnalysisService().analyze(req)
    req.metadata["phase9_analysis"] = analysis.to_dict()
    policy = apply_phase9_rule_policy(req, rule(), analysis)
    outcome = HybridAnswerService(FakeGptProvider()).generate(req, policy)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        outcome.result,
        facts_version="phase9-selected-facts-v1",
    )
    monkeypatch.setenv("OJE_AUTOMATION_DB_PATH", str(database.path))
    monkeypatch.setenv("PHASE86_INQUIRY_ID", str(inquiry_id))
    monkeypatch.setenv("PHASE86_PANEL", "answer")
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "uat" / "phase86_streamlit_probe.py")
    ).run(timeout=30)
    assert not app.exception
    app.segmented_control[0].set_value("Program Answer")
    app.run(timeout=30)
    widgets = [
        item
        for item in app.text_area
        if item.label == "Program Answer"
    ]
    assert widgets
    assert widgets[0].value == draft["original_answer"]
    assert widgets[0].disabled
    assert widgets[0].key == f"draft_text_{inquiry_id}"
