from __future__ import annotations

from dps.sales_detail import resolve_item_required_delivery_date
from answer.facts import build_answer_facts
from answer.models import AnswerRequest, AnswerResult, AnswerStatus


SOURCE = "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"


def _item(date: str | None, *, parse_failed: bool = False) -> dict:
    if date is None and not parse_failed:
        return {"required_delivery_date": None, "date_parse_status": "MISSING"}
    if parse_failed:
        return {
            "required_delivery_date": None,
            "raw_required_delivery_date": "INVALID",
            "date_parse_status": "PARSE_FAILED",
        }
    return {
        "required_delivery_date": date,
        "raw_required_delivery_date": date,
        "date_parse_status": "PARSED",
    }


def _rule_result() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED, category="배송", reason="rule",
        answer="확인했습니다.", provider="rules", auto_answerable=True,
        needs_review=False,
    )


def _facts_for(dps: dict):
    request = AnswerRequest(
        question="설치일은 언제인가요?", order_id="ORDER-1",
        metadata={"dps": dps},
    )
    return build_answer_facts(request, _rule_result())


# CASE DPS-A -- single item
def test_dps_a_single_item():
    result = resolve_item_required_delivery_date([_item("2026-08-25")])
    assert result["installation_date"] == "2026-08-25"
    assert result["date_parse_status"] == "PARSED"
    assert result["requires_human_review"] is False


# CASE DPS-B -- multi item, same date
def test_dps_b_multi_item_same_date():
    result = resolve_item_required_delivery_date(
        [_item("2026-08-25"), _item("2026-08-25")]
    )
    assert result["installation_date"] == "2026-08-25"
    assert result["date_parse_status"] == "PARSED"


# CASE DPS-C -- multi item, two different dates -> MAX
def test_dps_c_multi_item_two_different_dates_use_max():
    result = resolve_item_required_delivery_date(
        [_item("2026-08-25"), _item("2026-08-28")]
    )
    assert result["installation_date"] == "2026-08-28"
    assert result["date_parse_status"] == "PARSED"
    assert result["requires_human_review"] is False


# CASE DPS-D -- multi item, three different dates -> MAX
def test_dps_d_multi_item_three_different_dates_use_max():
    result = resolve_item_required_delivery_date(
        [_item("2026-08-25"), _item("2026-08-30"), _item("2026-08-27")]
    )
    assert result["installation_date"] == "2026-08-30"


# CASE DPS-E -- multi item, one date missing -> block + review required
def test_dps_e_partial_missing_blocks_confirmation():
    result = resolve_item_required_delivery_date(
        [_item("2026-08-25"), _item(None), _item("2026-08-27")]
    )
    assert result["installation_date"] is None
    assert result["date_parse_status"] == "PARTIAL"
    assert result["requires_human_review"] is True

    facts = _facts_for(
        {
            "installation_date": None,
            "required_delivery_date": None,
            "installation_date_source": SOURCE,
            "date_parse_status": "PARTIAL",
            "requires_human_review": True,
        }
    )
    assert facts.installation["date"] is None
    assert facts.installation["installation_date_confirmed"] is False
    assert facts.policy["requires_review"] is True


# CASE DPS-F -- all dates missing -> no date, review required if a
# schedule-specific question needs it (handled upstream by
# learning_context_service's NEEDS_DPS branch); at the Facts layer no date
# is ever asserted.
def test_dps_f_all_missing_generates_no_date():
    result = resolve_item_required_delivery_date([_item(None), _item(None)])
    assert result["installation_date"] is None
    assert result["date_parse_status"] == "MISSING"

    facts = _facts_for(
        {
            "installation_date": None,
            "required_delivery_date": None,
            "installation_date_source": SOURCE,
            "date_parse_status": "MISSING",
            "requires_human_review": False,
        }
    )
    assert facts.installation["date"] is None
    assert facts.installation["installation_date_confirmed"] is False


# CASE DPS-G -- one item fails to parse -> block + review required
def test_dps_g_parse_failure_blocks_confirmation():
    result = resolve_item_required_delivery_date(
        [_item("2026-08-25"), _item(None, parse_failed=True)]
    )
    assert result["installation_date"] is None
    assert result["date_parse_status"] == "PARTIAL"
    assert result["requires_human_review"] is True


# CASE DPS-H -- DPS MAX must outrank a conflicting Historical/Learning date
def test_dps_h_dps_max_outranks_historical_learning():
    from answer.hybrid_models import Emotion, IntentResult
    from services.learning_context_service import LearningContextService
    from repositories.database import Database
    from repositories.inquiry_repository import InquiryRepository
    from repositories.learning_repository import LearningRepository

    import tempfile
    import pathlib

    d = pathlib.Path(tempfile.mkdtemp()) / "dps-authority.db"
    database = Database(d)
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "DPS-H-1",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "설치 문의",
            "content": "설치 예정일이 언제인가요?",
            "product_name": "삼성 TV",
            "raw_json": {},
        }
    ).inquiry_id
    LearningRepository(database).upsert(
        {
            "source_key": "dps-h-historical",
            "learning_source": "APPROVED_EDITED",
            "inquiry_id": inquiry_id,
            "answer_draft_id": None,
            "approval_history_id": None,
            "question_original_masked": "설치 예정일이 언제인가요?",
            "question_normalized": "설치 예정일이 언제인가요",
            "store_code": "OJE_PLUS",
            "inquiry_type": "PRODUCT_INQUIRY",
            "intent": "GENERAL",
            "product_name": "삼성 TV",
            "model_code": None,
            "final_answer": "설치 예정일은 8월 25일입니다.",
            "rating": 5,
            "quality_score": 1.0,
            "generation_mode": "TEST",
            "template_id": None,
            "processing_route": "TEST",
            "validator_result": "HUMAN_VERIFIED_NAVER_POSTED",
            "posted": True,
            "auto_posted": False,
            "edit_ratio": 0.1,
            "style_only": False,
            "version": 1,
            "style_features_json": {},
            "metadata_json": {
                "human_verified": True, "learning_signal_type": "POSITIVE",
            },
            "active": True,
        }
    )

    resolved = resolve_item_required_delivery_date(
        [_item("2026-08-25"), _item("2026-08-28")]
    )
    assert resolved["installation_date"] == "2026-08-28"

    facts = _facts_for(
        {
            "installation_date": resolved["installation_date"],
            "required_delivery_date": resolved["required_delivery_date"],
            "installation_date_source": resolved["installation_date_source"],
            "date_parse_status": resolved["date_parse_status"],
            "requires_human_review": resolved["requires_human_review"],
        }
    )
    facts = type(facts)(
        inquiry={
            **facts.inquiry, "inquiry_id": inquiry_id,
            "question": "설치 예정일이 언제인가요?",
        },
        product={**facts.product, "name": "삼성 TV"},
        order=facts.order, delivery=facts.delivery,
        installation=facts.installation, dps=facts.dps, rule=facts.rule,
        activity=facts.activity, policy=facts.policy, warnings=facts.warnings,
    )
    assert facts.installation["date"] == "2026-08-28"
    assert facts.installation["installation_date_confirmed"] is True

    intent = IntentResult(
        "DELIVERY_INSTALLATION_STATUS", ("설치 예정일이 언제인가요?",),
        Emotion.NORMAL, "NORMAL", 0.9, False, "test",
    )
    context = LearningContextService(database).build(facts, intent)
    evidence = context["subquestion_evidence"][0]
    assert evidence["status"] == "ANSWERABLE"
    assert evidence["source"] == "CURRENT_DPS"


# CASE DPS-I -- row order must not matter
def test_dps_i_row_order_does_not_change_the_result():
    forward = resolve_item_required_delivery_date(
        [_item("2026-08-28"), _item("2026-08-25")]
    )
    reversed_ = resolve_item_required_delivery_date(
        [_item("2026-08-25"), _item("2026-08-28")]
    )
    assert forward["installation_date"] == "2026-08-28"
    assert reversed_["installation_date"] == "2026-08-28"
