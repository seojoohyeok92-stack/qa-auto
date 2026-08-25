"""Golden production-path scenarios for the automatic-posting core.

No network client in this module can reach Naver, DPS or Kakao.  Inquiries are
stored in a temporary operational DB and pass through AnswerService,
AutomaticDraftService, eligibility and AutoPostPipelineService.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.naver_answer_client import NaverAnswerResponse
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from config import NaverPostSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.product_fact_repository import ProductFactRepository
from services import learning_evidence_policy
from services.answer_service import AnswerService
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_post_validation_service import AutoPostTechnicalValidator
from services.automatic_draft_service import AutomaticDraftService
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.naver_post_service import NaverPostService
from services.product_knowledge_service import (
    ProductKnowledgeResult,
    ProductKnowledgeService,
    fields_for_question,
)


REAL_PRODUCT_DB = Path("data") / "product_facts.db"
M5_PRODUCT_ID = "10198648691"
M5_NAME = "삼성 M5 LS32DM501EKXKR 스마트모니터"


@pytest.fixture(autouse=True)
def _disable_kakao(monkeypatch):
    monkeypatch.setattr(
        "services.answer_service.notify_qna_safely", lambda **_: False
    )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "golden-auto-post.db")
    value.initialize()
    return value


@dataclass
class DryRunNaverClient:
    calls: int = 0
    requests: list | None = None

    def send(self, request, *, access_token):
        assert access_token == "dry-run-token"
        self.calls += 1
        if self.requests is None:
            self.requests = []
        self.requests.append(request)
        return NaverAnswerResponse(204, f"DRY-{self.calls}")


class ForbiddenGenerator:
    name = "forbidden"

    def generate(self, *_args, **_kwargs):
        raise AssertionError("GPT/Rule generation was not expected")

    def generate_json(self, **_kwargs):
        raise AssertionError("GPT generation was not expected")


class RecordingOrderLookup:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.calls: list[dict] = []

    def lookup_for_inquiry(
        self, inquiry_id: int, *, validated_order_number=None, **_kwargs
    ):
        self.calls.append({
            "inquiry_id": inquiry_id,
            "validated_order_number": validated_order_number,
        })
        return {
            "success": self.success,
            "lookup_number": validated_order_number,
            "lookup_type": "ORDER_ID",
            "orders": (
                [{"order_id": validated_order_number}]
                if self.success else []
            ),
            "error_code": None if self.success else "ORDER_NOT_FOUND",
        }


class RecordingDps:
    def __init__(self, *, date: str = "2026-08-28") -> None:
        self.date = date
        self.calls = 0
        self.skip_calls = 0

    def enrich(self, request, **_kwargs):
        self.calls += 1
        metadata = {
            "lookup_required": True,
            "lookup_status": "SUCCESS",
            "order_id": request.order_id,
            "installation_date": self.date,
            "required_delivery_date": self.date,
            "installation_date_source": "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE",
            "date_parse_status": "PARSED",
            "source": "DPS_AGENT_DRY_RUN",
            "trusted": True,
            "validated": True,
        }
        request.metadata["dps"] = metadata
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=True),
            metadata=metadata,
            lookup_row=None,
        )

    def skip_for_phase9(self, request, **_kwargs):
        self.skip_calls += 1
        metadata = {
            "lookup_required": False,
            "lookup_status": "NOT_REQUIRED",
        }
        request.metadata["dps"] = metadata
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=metadata,
            lookup_row=None,
        )


class EmptyProductKnowledge:
    def facts_for_inquiry(self, *, product_id=None, **_kwargs):
        return ProductKnowledgeResult(
            product_id=str(product_id or "") or None,
            listing_id=None,
            matched=bool(product_id),
            unavailable_reason="NO_RELEVANT_VERIFIED_FACT",
        )


def _provider(answer: str) -> FakeGptProvider:
    return FakeGptProvider(responses={
        "DRAFT": {
            "answer": answer,
            "used_facts": [],
            "missing_information": [],
            "requires_review": False,
            "confidence": 0.96,
        },
        "SELF_REVIEW": {
            "passed": True,
            "answered_all_questions": True,
            "facts_consistent": True,
            "has_speculation": False,
            "requires_review": False,
            "warnings": [],
            "reason": "golden",
        },
    })


def _insert(
    database: Database,
    source_id: str,
    question: str,
    *,
    inquiry_type: str = "PRODUCT_INQUIRY",
    order_id: str | None = None,
    product_id: str | None = None,
    product_name: str = "삼성 TV",
) -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source": "PRODUCT_INQUIRY",
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_id": source_id,
        "source_question_id": source_id,
        "external_inquiry_id": source_id,
        "inquiry_type": inquiry_type,
        "title": "문의",
        "content": question,
        "order_id": order_id,
        "product_id": product_id,
        "product_name": product_name,
        "source_answered": False,
        "answer_status": "UNANSWERED",
        "post_status": "NOT_POSTED",
        "raw_json": {
            "source": "PRODUCT_INQUIRY",
            "questionId": source_id,
            "source_payload": {"questionId": source_id},
        },
    }).inquiry_id


def _post_service(database: Database, client: DryRunNaverClient):
    return NaverPostService(
        database,
        settings=NaverPostSettings(enabled=True),
        store_resolver=lambda _: StoreConfig(
            "OJE_PLUS", "dry-run", "client", "secret"
        ),
        token_provider=lambda **_: "dry-run-token",
        client=client,
    )


def _run(
    database: Database,
    inquiry_id: int,
    answer_service: AnswerService,
):
    client = DryRunNaverClient()
    pipeline = AutoPostPipelineService(
        database,
        draft_service=AutomaticDraftService(
            database, answer_service=answer_service
        ),
        post_service=_post_service(database, client),
        dps_status_provider=lambda: {"session_status": "READY"},
    )
    outcome = pipeline.run_pending(
        run_id=f"GOLDEN-{inquiry_id}",
        owner_id="GOLDEN-DRY-RUN",
        max_retries=1,
        inquiry_ids=[inquiry_id],
    )
    return outcome, client, AnswerRepository(database).active_for_inquiry(
        inquiry_id
    )


def _template_service(
    database: Database, *, order=None, dps=None
) -> AnswerService:
    return AnswerService(
        database,
        engine=ForbiddenGenerator(),
        hybrid_service=ForbiddenGenerator(),
        order_lookup_service=order or RecordingOrderLookup(),
        dps_enrichment=dps or RecordingDps(),
        product_knowledge=EmptyProductKnowledge(),
    )


def _learning_context(question: str, *, match="EXACT_MODEL", conflict=False):
    rows = [{
        "authority": "APPROVED",
        "compatibility": {"product_match": match},
        "answer_support": 1.0,
        "answer": "기본 스탠드 다리는 탈부착 가능합니다.",
        "learning_example_id": 701,
        "matched_subquestion": question,
    }]
    if conflict:
        rows.append({
            "authority": "APPROVED",
            "compatibility": {"product_match": "EXACT_MODEL"},
            "answer_support": 1.0,
            "answer": "기본 스탠드 다리는 탈부착할 수 없습니다.",
            "learning_example_id": 702,
            "matched_subquestion": question,
        })
    return {
        "similar_approved_answers": rows,
        "subquestion_evidence": [{
            "subquestion": question,
            "status": "ANSWERABLE",
            "evidence_coverage": "SUPPORTED",
            "source": "ACTIVE_POSITIVE_LEARNING",
            "answer_required": True,
        }],
    }


# GS-01
def test_gs01_delivery_without_order_posts_confirmed_request_template(database):
    inquiry_id = _insert(
        database, "GS-01", "배송이 언제쯤 되는지 확인 부탁드립니다.",
        inquiry_type="배송",
    )
    dps = RecordingDps()
    outcome, client, draft = _run(
        database, inquiry_id, _template_service(database, dps=dps)
    )
    metadata = draft["metadata_json"]
    assert outcome.succeeded_count == 1
    assert client.calls == 1
    assert metadata["selected_answer_route"] == "ORDER_ID_REQUEST"
    assert metadata["gpt_called"] is False
    assert metadata["dps_lookup_attempted"] is False
    assert metadata["validator_result"]["status"] == "PASS"
    assert dps.calls == 0


# GS-02
def test_gs02_body_order_number_reaches_order_lookup_and_dps(database):
    number = "2026082351391541"
    inquiry_id = _insert(
        database,
        "GS-02",
        "23일 주문했는데요. 주문번호 2026082351391541입니다. "
        "배송이나 설치 예정일이 언제인지 알 수 있을까요?",
        inquiry_type="배송",
    )
    order = RecordingOrderLookup()
    dps = RecordingDps()
    outcome, client, draft = _run(
        database, inquiry_id,
        _template_service(database, order=order, dps=dps),
    )
    assert order.calls == [{
        "inquiry_id": inquiry_id,
        "validated_order_number": number,
    }]
    assert dps.calls == 1
    assert outcome.succeeded_count == 1
    assert client.calls == 1
    assert draft["metadata_json"]["selected_answer_route"] == (
        "DELIVERY_WITH_INSTALLATION_DATE"
    )


# GS-03
def test_gs03_invalid_order_never_reaches_lookup_or_dps(database):
    inquiry_id = _insert(
        database, "GS-03",
        "주문번호 20260823513915인데 설치 예정일이 언제인가요?",
        inquiry_type="배송",
    )
    order, dps = RecordingOrderLookup(), RecordingDps()
    outcome, client, draft = _run(
        database, inquiry_id,
        _template_service(database, order=order, dps=dps),
    )
    assert order.calls == []
    assert dps.calls == 0
    assert draft is None or draft["metadata_json"]["selected_answer_route"] == "ORDER_ID_REQUEST"
    assert client.calls == 1
    assert outcome.succeeded_count == 1


# GS-04
def test_gs04_schedule_change_blocks_even_with_healthy_order_and_dps(database):
    inquiry_id = _insert(
        database, "GS-04",
        "설치 예정일이 다음 주라고 하는데 급해요. "
        "이번 주 안으로 좀 당겨주실 수 있나요?",
        inquiry_type="배송", order_id="2026082351391541",
    )
    order, dps = RecordingOrderLookup(), RecordingDps()
    outcome, client, draft = _run(
        database, inquiry_id,
        _template_service(database, order=order, dps=dps),
    )
    assert InquiryAnalysisService().analyze(
        AnswerRequest(
            question="설치 예정일이 다음 주라고 하는데 급해요. 이번 주 안으로 좀 당겨주실 수 있나요?",
            inquiry_type="배송", order_id="2026082351391541",
            product_order_id=None, product_name="삼성 TV", metadata={},
        )
    ).manual_review_required is True
    assert outcome.succeeded_count == 0
    assert client.calls == 0
    assert draft is None
    assert order.calls == [] and dps.calls == 0


# GS-05
def test_gs05_plain_schedule_lookup_is_not_change_request(database):
    inquiry_id = _insert(
        database, "GS-05", "설치 예정일이 언제인가요?", inquiry_type="배송"
    )
    outcome, client, draft = _run(
        database, inquiry_id, _template_service(database)
    )
    assert outcome.succeeded_count == 1 and client.calls == 1
    assert draft["metadata_json"]["selected_answer_route"] == "ORDER_ID_REQUEST"


# GS-06
@pytest.mark.skipif(not REAL_PRODUCT_DB.is_file(), reason="product facts DB absent")
def test_gs06_verified_hdmi_fact_reaches_post_for_exact_product(database):
    inquiry_id = _insert(
        database, "GS-06", "이 제품 HDMI 단자가 몇 개 있나요?",
        product_id=M5_PRODUCT_ID, product_name=M5_NAME,
    )
    service = AnswerService(
        database,
        hybrid_service=HybridAnswerService(_provider("HDMI 단자는 2개입니다.")),
        dps_enrichment=RecordingDps(),
        order_lookup_service=RecordingOrderLookup(),
        product_knowledge=ProductKnowledgeService(
            ProductFactRepository(REAL_PRODUCT_DB)
        ),
    )
    outcome, client, draft = _run(database, inquiry_id, service)
    guard = draft["metadata_json"]["product_fact_guard"]
    assert outcome.succeeded_count == 1 and client.calls == 1
    assert guard["current_fact_verified"] is True
    assert guard["product_fact_claims_supported"] is True
    assert any(
        item["field_key"] == "hdmi_port_count"
        for item in guard["product_knowledge"]["safe_facts"]
    )


# GS-07 / GS-08 / GS-09
@pytest.mark.parametrize(
    "scenario,match,conflict,expected_post",
    [
        ("GS-07", "EXACT_MODEL", False, 1),
        ("GS-08", "MODEL_MISMATCH", False, 0),
        ("GS-09", "EXACT_MODEL", True, 0),
    ],
)
def test_gs07_to_gs09_learning_scope_and_conflict(
    database, scenario, match, conflict, expected_post
):
    question = (
        "43인치 스탠드형인데 기본 스탠드 다리를 떼었다가 "
        "다시 장착할 수 있나요?"
    )
    inquiry_id = _insert(
        database, scenario, question,
        product_id="TV-43-GOLDEN", product_name="삼성 43인치 TV QN43G",
    )
    context = _learning_context(question, match=match, conflict=conflict)
    answer = (
        "기본 스탠드 다리는 탈부착 가능합니다."
        if not conflict else "정확한 탈부착 가능 여부는 확인이 필요합니다."
    )
    hybrid = HybridAnswerService(
        _provider(answer), learning_context_provider=lambda *_: context
    )
    service = AnswerService(
        database,
        hybrid_service=hybrid,
        dps_enrichment=RecordingDps(),
        order_lookup_service=RecordingOrderLookup(),
        product_knowledge=EmptyProductKnowledge(),
    )
    outcome, client, draft = _run(database, inquiry_id, service)
    assert client.calls == expected_post, (outcome, draft)
    assert outcome.succeeded_count == expected_post, (outcome, draft)
    if draft is not None:
        evidence = draft["metadata_json"].get("product_fact_guard", {}).get(
            "approved_learning_evidence", {}
        )
        if evidence:
            assert evidence.get("usable") is (expected_post == 1)
            assert evidence.get("conflict") is conflict
        else:
            # Evidence conflict may stop generation before a product guard is
            # assembled; the separate policy assertion below pins its cause.
            assert conflict is True


# GS-10
@pytest.mark.skipif(not REAL_PRODUCT_DB.is_file(), reason="product facts DB absent")
def test_gs10_unrelated_facts_do_not_ground_airplay_or_staff_assertion(database):
    question = "아이폰 AirPlay 지원되나요? 와이파이 없이도 미러링 가능한가요?"
    inquiry_id = _insert(
        database, "GS-10", question,
        product_id=M5_PRODUCT_ID, product_name=M5_NAME,
    )
    knowledge = ProductKnowledgeService(
        ProductFactRepository(REAL_PRODUCT_DB)
    ).facts_for_inquiry(product_id=M5_PRODUCT_ID, question=question)
    assert knowledge.has_safe_facts
    assert knowledge.supports_question(question) is False
    service = AnswerService(
        database,
        hybrid_service=HybridAnswerService(
            _provider("AirPlay를 지원합니다. 와이파이 없이 미러링 가능합니다.")
        ),
        dps_enrichment=RecordingDps(),
        order_lookup_service=RecordingOrderLookup(),
        product_knowledge=ProductKnowledgeService(
            ProductFactRepository(REAL_PRODUCT_DB)
        ),
    )
    outcome, client, draft = _run(database, inquiry_id, service)
    assert outcome.succeeded_count == 0 and client.calls == 0
    if draft is not None:
        text = str(draft["original_answer"])
        assert "AirPlay를 지원합니다" not in text
        assert "미러링 가능합니다" not in text


# GS-11 / GS-12
@pytest.mark.parametrize(
    "token,expected",
    [
        ("<masked-phone>", False),
        ("<masked-email>", False),
        ("<masked-product-order-id>", False),
        ("1588-3366", True),
        ("02-706-2678", True),
    ],
)
def test_gs11_gs12_placeholder_and_official_contacts(token, expected):
    verdict = AutoPostTechnicalValidator().validate_answer(
        f"문의는 {token}로 부탁드립니다."
    )
    assert verdict.passed is expected
    if not expected:
        assert "INTERNAL_PLACEHOLDER_EXPOSURE" in verdict.errors


def test_gs11_placeholder_never_reaches_post_client(database):
    inquiry_id = _insert(database, "GS-11-POST", "연락처를 알려주세요.")
    AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="운영",
            reason="golden",
            answer="<masked-phone>로 문의해 주세요.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            metadata={
                "selected_answer_route": "TEMPLATE",
                "generation_mode": "TEMPLATE",
                "validator_result": {"status": "PASS", "passed": True},
            },
        ),
    )
    client = DryRunNaverClient()
    outcome = AutoPostPipelineService(
        database, post_service=_post_service(database, client)
    ).run_pending(
        run_id="GS-11", owner_id="GOLDEN", max_retries=1,
        inquiry_ids=[inquiry_id],
    )
    assert client.calls == 0
    assert outcome.succeeded_count == 0


@pytest.mark.parametrize(
    "question,expected_post",
    [
        ("배송 언제 와요", 1),
        ("배송 언제되나요", 1),
        ("설치 언제 오나요", 1),
        ("기사님 언제 오시나요", 1),
        ("배송 일정 확인 부탁드려요", 1),
        ("설치 예정일 알려주세요", 1),
        ("언제쯤 배송될까요", 1),
        ("설치일이 궁금합니다", 1),
        ("배송 날짜가 언제인가요", 1),
        ("기사 방문 예정일 확인해주세요", 1),
        ("배송 상태와 예정일 알려주세요", 1),
        ("설치 일정 조회 부탁드립니다", 1),
        ("주문한 제품 배송 예정일은 언제인가요", 1),
        ("배송일 확인 가능할까요", 1),
        ("설치 날짜 언제예요", 1),
        ("이번 주로 당겨주세요", 0),
        ("설치일을 하루만 앞당길 수 있나요", 0),
        ("배송 날짜 변경 가능할까요", 0),
        ("설치일 바꿔주세요", 0),
        ("배송일을 내일로 변경해주세요", 0),
        ("기사님 일정을 조정하고 싶어요", 0),
        ("방문일을 다음 주로 미뤄주세요", 0),
        ("설치 요청일 변경 부탁드립니다", 0),
        ("배송을 더 빨리 받을 수 있나요", 0),
        ("급해서 설치를 당겨주세요", 0),
        ("배송 예정일을 이번 주로 바꿔주세요", 0),
        ("기사 방문시간 변경해주세요", 0),
        ("설치 예정인데 날짜 조정 부탁드려요", 0),
        ("배송일 다른 날로 옮겨주세요", 0),
        ("설치 일정 연기 가능한가요", 0),
    ],
)
def test_extended_30_delivery_schedule_variants(
    database, question, expected_post
):
    inquiry_id = _insert(
        database, f"EXT-{abs(hash(question))}", question, inquiry_type="배송"
    )
    outcome, client, _draft = _run(
        database, inquiry_id, _template_service(database)
    )
    assert client.calls == expected_post
    assert outcome.succeeded_count == expected_post


def test_product_scope_helpers_distinguish_base_vesa_and_basic_stand():
    base_fields, _ = fields_for_question(
        "이 제품 베사홀 규격이 어떻게 되나요? 벽걸이 브라켓을 설치하려고 합니다."
    )
    accessory_fields, _ = fields_for_question(
        "이 브라켓의 VESA 지원 범위가 어떻게 되나요?"
    )
    assert "vesa_mm" in base_fields
    assert "accessory_vesa_mm" not in base_fields
    assert "accessory_vesa_mm" in accessory_fields
    analysis = InquiryAnalysisService()
    own_stand = analysis.analyze(AnswerRequest(
        question="기본 스탠드 다리를 떼었다 다시 장착할 수 있나요?",
        product_name="삼성 TV",
    ))
    bracket = analysis.analyze(AnswerRequest(
        question="제가 가진 브라켓이 이 제품에 맞나요?",
        product_name="삼성 TV",
    ))
    assert own_stand.detected_intent != "PRODUCT_COMPATIBILITY"
    assert bracket.detected_intent == "PRODUCT_COMPATIBILITY"


def test_learning_policy_exact_model_other_model_and_conflict():
    question = "기본 스탠드 다리를 떼었다 다시 장착할 수 있나요?"
    exact = learning_evidence_policy.evaluate(
        learning_context=_learning_context(question), safe_facts=()
    )
    other = learning_evidence_policy.evaluate(
        learning_context=_learning_context(question, match="MODEL_MISMATCH"),
        safe_facts=(),
    )
    conflict = learning_evidence_policy.evaluate(
        learning_context=_learning_context(question, conflict=True),
        safe_facts=(),
    )
    assert exact.usable is True
    assert other.usable is False
    assert conflict.usable is False and conflict.conflict is True
