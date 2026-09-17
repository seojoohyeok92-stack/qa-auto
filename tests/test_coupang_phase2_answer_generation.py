"""Phase 2-1: a person may generate, review and approve a Coupang answer.

Nothing generates one unasked, nothing looks up a Coupang order or DPS
schedule, nothing posts it and nothing notifies about it.  What the answer
path is handed is pinned too: the product and option names the dashboard
shows, the model an operator confirmed for the option (never a guess), the
COUPANG_ONLY Learning both seller accounts share, and no Naver procedure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import kakao_notify
from answer.exceptions import (
    AnswerAlreadyPostedError,
    AnswerGenerationError,
    AutoAnswerProhibitedError,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.coupang_product_catalog_repository import CoupangProductCatalogRepository
from repositories.coupang_product_mapping_repository import (
    CONFIRMED,
    MANUAL,
    NEEDS_REVIEW,
    CoupangProductMappingRepository,
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from services import market_policy
from services.answer_service import AnswerService
from services.approval_service import ApprovalService
from services.automatic_draft_service import AutomaticDraftService
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.inquiry_sync_service import normalize_work_item
from services.product_knowledge_service import ProductKnowledgeResult, ProductKnowledgeService

PRODUCT = "삼성전자 스마트 모니터 M5 M50F / 모음전"
OPTION = "32인치 화이트"
SPID = "15654321531"
VENDOR_ITEM = "93128932886"
NAVER_ANSWER = "구매내역서는 네이버페이 > 결제내역에서 확인 가능합니다."


# --- fixtures ------------------------------------------------------------------

@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "phase2.db")
    value.initialize()
    return value


def coupang_inquiry(
    database: Database,
    *,
    account: str = "OJE_NS",
    inquiry_id: str = "170000001",
    content: str = "스피커 있나요?",
    answered: bool = False,
    order_ids: list | None = None,
) -> int:
    payload = {
        "inquiryId": inquiry_id, "sellerProductId": SPID, "vendorItemId": VENDOR_ITEM,
        "content": content, "inquiryAt": "2026-09-17T10:00:00+09:00",
        "orderIds": order_ids or [],
        "commentDtoList": [{
            "inquiryCommentId": "c1", "inquiryId": inquiry_id, "content": "판매자 답변",
            "inquiryCommentAt": "2026-09-17T11:00:00+09:00",
        }] if answered else [],
    }
    ready = normalize_work_item(
        CoupangInquiryNormalizer().online(payload, account_code=account).to_work_item()
    )
    row_id = InquiryRepository(database).upsert_work_item(ready).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def naver_inquiry(database: Database, question_id: str = "N-1", content: str = "스피커 있나요?") -> int:
    row_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": question_id, "inquiry_type": "상품",
        "title": "상품 문의", "content": content,
        "product_name": "삼성 스마트모니터 M5", "option_name": "32인치",
        "raw_json": {},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def seed_catalog(database: Database, account: str = "OJE_NS") -> None:
    catalog = CoupangProductCatalogRepository(database)
    catalog.upsert_product(
        account_code=account,
        data={"sellerProductId": SPID, "status": "APPROVED", "sellerProductName": PRODUCT},
        sync_token="t",
    )
    catalog.upsert_option(
        account_code=account, seller_product_id=SPID,
        item={"vendorItemId": VENDOR_ITEM, "itemName": OPTION},
    )


def map_option(database: Database, *, status: str, account: str = "OJE_NS", model: str = "32DM501") -> None:
    CoupangProductMappingRepository(database).upsert(
        account_code=account, vendor_item_id=VENDOR_ITEM, seller_product_id=SPID,
        canonical_model=model if status == CONFIRMED else model,
        mapping_source=MANUAL, mapping_status=status,
    )


def rule(answer: str = "스피커가 내장되어 있습니다.") -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED, category="상품", reason="rule", answer=answer,
        provider="rules", auto_answerable=True, needs_review=False,
    )


class RecordingEngine:
    def __init__(self, result: AnswerResult | None = None) -> None:
        self.result = result or rule()
        self.requests: list[AnswerRequest] = []

    def generate(self, request):
        self.requests.append(request)
        return self.result

    def candidates(self, request):
        return []


class RecordingHybrid:
    def __init__(self, answer: str = "네, 스피커가 내장되어 있습니다.") -> None:
        self.answer = answer
        self.requests: list[AnswerRequest] = []

    def generate(self, request, rule_result):
        self.requests.append(request)
        return SimpleNamespace(
            result=AnswerResult(
                status=AnswerStatus.GENERATED, category="상품", reason="gpt",
                answer=self.answer, provider="fake_gpt_hybrid",
                auto_answerable=False, needs_review=True,
            ),
            validation=SimpleNamespace(passed=True),
            fallback_used=False,
            events=(),
        )


class RecordingKnowledge:
    """Records what the answer path asked Product Knowledge; answers nothing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.catalog_repository = SimpleNamespace(
            match=lambda model_code=None, **_: SimpleNamespace(
                model_key="S32DM501" if str(model_code) == "32DM501" else None
            )
        )

    def facts_for_inquiry(self, **kwargs):
        self.calls.append(kwargs)
        return ProductKnowledgeResult(product_id=None, listing_id=None, matched=False)


class Recorder:
    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls += 1
            raise AssertionError(f"{name} must not run")
        return record


def service(database: Database, **overrides) -> AnswerService:
    parts = {
        "engine": RecordingEngine(),
        "hybrid_service": RecordingHybrid(),
        "product_knowledge": RecordingKnowledge(),
    }
    parts.update(overrides)
    return AnswerService(database, **parts)


def draft_count(database: Database) -> int:
    with database.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM answer_drafts").fetchone()[0]


# --- A. policy -------------------------------------------------------------------

def test_naver_keeps_every_action() -> None:
    for check in (
        market_policy.is_store_answer_generation_enabled,
        market_policy.is_store_automatic_generation_enabled,
        market_policy.is_store_dps_enabled,
        market_policy.is_store_post_enabled,
    ):
        assert check("OJE_PLUS") is True
    assert market_policy.is_kakao_market_enabled("NAVER") is True


@pytest.mark.parametrize("store", ["COUPANG_OJE_NS", "COUPANG_OJE_PLUS"])
def test_coupang_generation_is_open_and_everything_else_closed(store) -> None:
    assert market_policy.is_store_answer_generation_enabled(store) is True
    assert market_policy.is_store_automatic_generation_enabled(store) is False
    assert market_policy.is_store_dps_enabled(store) is False
    assert market_policy.is_store_post_enabled(store) is False
    assert market_policy.is_kakao_market_enabled("COUPANG") is False


# --- B. only a person starts generation ---------------------------------------------

def test_automatic_generation_skips_coupang_without_calling_answer_service(database) -> None:
    inquiry_id = coupang_inquiry(database)
    recorder = Recorder()

    outcome = AutomaticDraftService(database, answer_service=recorder).ensure_for_inquiry(inquiry_id)

    assert outcome.status == "SKIPPED_MANUAL_GENERATION_ONLY"
    assert recorder.calls == 0
    assert draft_count(database) == 0


def test_auto_post_candidates_never_include_coupang(database) -> None:
    coupang_inquiry(database, account="OJE_NS", inquiry_id="1")
    coupang_inquiry(database, account="OJE_PLUS", inquiry_id="2")
    naver_inquiry(database)
    repository = AutoPostRepository(database)
    rows = repository.candidates(
        max_retries=3,
        store_codes=market_policy.post_enabled_store_codes(repository.distinct_store_codes()),
    )
    assert {row["store_code"] for row in rows} == {"OJE_PLUS"}


def test_an_explicit_generate_creates_a_draft(database) -> None:
    inquiry_id = coupang_inquiry(database)
    hybrid = RecordingHybrid()

    outcome = service(database, hybrid_service=hybrid).generate_for_inquiry(inquiry_id)

    assert hybrid.requests and outcome.draft["inquiry_id"] == inquiry_id
    assert draft_count(database) == 1


def test_an_answered_coupang_inquiry_is_not_generated(database) -> None:
    inquiry_id = coupang_inquiry(database, answered=True)
    # Refused before any work, whichever guard sees it first.
    with pytest.raises((AutoAnswerProhibitedError, AnswerGenerationError, AnswerAlreadyPostedError)):
        service(database).generate_for_inquiry(inquiry_id)
    assert draft_count(database) == 0


# --- C. product identity -------------------------------------------------------------

def test_product_and_option_names_reach_the_request(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)
    engine, hybrid = RecordingEngine(), RecordingHybrid()

    service(database, engine=engine, hybrid_service=hybrid).generate_for_inquiry(inquiry_id)

    for request in (engine.requests[0], hybrid.requests[0]):
        assert request.product_name and "M50F" in request.product_name
        assert request.option_name == OPTION
        assert request.store_code == "COUPANG_OJE_NS"
        assert request.metadata["market"] == "COUPANG"


def test_a_confirmed_mapping_names_the_model(database) -> None:
    seed_catalog(database)
    map_option(database, status=CONFIRMED)
    inquiry_id = coupang_inquiry(database)
    knowledge, hybrid = RecordingKnowledge(), RecordingHybrid()

    service(database, product_knowledge=knowledge, hybrid_service=hybrid).generate_for_inquiry(inquiry_id)

    metadata = hybrid.requests[0].metadata
    assert metadata["canonical_model"] == "32DM501"
    assert metadata["model_code"] == "S32DM501"
    assert metadata["model_identity_source"] == "COUPANG_CONFIRMED_MAPPING"
    assert knowledge.calls[0]["model_code"] == "S32DM501"
    # A Coupang sellerProductId is not a Product Knowledge listing id.
    assert knowledge.calls[0]["product_id"] is None
    assert "product_knowledge" in metadata


def test_an_unconfirmed_mapping_is_not_used(database) -> None:
    seed_catalog(database)
    map_option(database, status=NEEDS_REVIEW)
    inquiry_id = coupang_inquiry(database)
    knowledge, hybrid = RecordingKnowledge(), RecordingHybrid()

    service(database, product_knowledge=knowledge, hybrid_service=hybrid).generate_for_inquiry(inquiry_id)

    metadata = hybrid.requests[0].metadata
    assert "model_identity_source" not in metadata
    assert "canonical_model" not in metadata
    assert knowledge.calls[0]["model_code"] != "S32DM501"


def test_another_accounts_mapping_is_not_borrowed(database) -> None:
    seed_catalog(database, account="OJE_NS")
    map_option(database, status=CONFIRMED, account="OJE_PLUS")
    inquiry_id = coupang_inquiry(database, account="OJE_NS")
    hybrid = RecordingHybrid()
    service(database, hybrid_service=hybrid).generate_for_inquiry(inquiry_id)
    assert "canonical_model" not in hybrid.requests[0].metadata


def test_the_confirmed_model_reaches_real_product_knowledge_facts() -> None:
    result = ProductKnowledgeService().facts_for_inquiry(
        product_id=None, question="스피커 있나요?", model_code="S32DM501",
        product_name=PRODUCT, option_name=OPTION,
    )
    assert result.matched is True
    assert result.safe_facts


def test_naver_product_knowledge_call_is_unchanged(database) -> None:
    inquiry_id = naver_inquiry(database)
    knowledge, hybrid = RecordingKnowledge(), RecordingHybrid()
    service(database, product_knowledge=knowledge, hybrid_service=hybrid).generate_for_inquiry(inquiry_id)
    assert "market" not in hybrid.requests[0].metadata
    assert "model_identity_source" not in hybrid.requests[0].metadata
    assert knowledge.calls[0]["product_id"] == hybrid.requests[0].metadata.get("product_id")


# --- D. Learning -----------------------------------------------------------------------

def _approve(database: Database, inquiry_id: int) -> None:
    draft = AnswerRepository(database).active_for_inquiry(inquiry_id)
    ApprovalService(database).approve(inquiry_id=inquiry_id, draft_id=draft["id"])


def test_a_coupang_approval_is_coupang_only_learning_shared_by_both_accounts(database) -> None:
    inquiry_id = coupang_inquiry(database, account="OJE_NS")
    service(database).generate_for_inquiry(inquiry_id)
    _approve(database, inquiry_id)
    repository = LearningRepository(database)

    learned = [row for row in repository.candidates(store_code="COUPANG_OJE_NS")
               if row.get("inquiry_id") == inquiry_id]
    assert learned
    row = learned[0]
    assert row["store_code"] is None
    assert row["metadata_json"]["market_applicability"] == "COUPANG_ONLY"
    assert any(r.get("inquiry_id") == inquiry_id
               for r in repository.candidates(store_code="COUPANG_OJE_PLUS"))
    assert not any(r.get("inquiry_id") == inquiry_id
                   for r in repository.candidates(store_code="OJE_PLUS"))


def test_naver_learning_stays_naver(database) -> None:
    inquiry_id = naver_inquiry(database)
    service(database).generate_for_inquiry(inquiry_id)
    _approve(database, inquiry_id)
    repository = LearningRepository(database)

    row = next(r for r in repository.candidates(store_code="OJE_PLUS") if r.get("inquiry_id") == inquiry_id)
    assert row["store_code"] == "OJE_PLUS"
    assert "market_applicability" not in row["metadata_json"]
    # A metadata-free row, even with no store, never reaches Coupang.
    with database.transaction() as connection:
        connection.execute("UPDATE learning_examples SET store_code=NULL WHERE id=?", (row["id"],))
    assert not any(r["id"] == row["id"] for r in repository.candidates(store_code="COUPANG_OJE_NS"))
    assert any(r["id"] == row["id"] for r in repository.candidates(store_code="OJE_PLUS"))


# --- E. answer wording --------------------------------------------------------------------

def test_a_naver_procedure_template_is_not_offered_to_a_coupang_answer(database) -> None:
    inquiry_id = coupang_inquiry(database, content="구매내역서 확인 방법 알려주세요")
    hybrid = RecordingHybrid()
    service(database, engine=RecordingEngine(rule(NAVER_ANSWER)), hybrid_service=hybrid).generate_for_inquiry(inquiry_id)
    candidates = hybrid.requests[0].metadata.get("template_candidates") or []
    assert all("네이버" not in str(item.get("answer")) for item in candidates)


def test_naver_still_receives_its_own_template(database) -> None:
    inquiry_id = naver_inquiry(database, content="구매내역서 확인 방법 알려주세요")
    hybrid = RecordingHybrid()
    service(database, engine=RecordingEngine(rule(NAVER_ANSWER)), hybrid_service=hybrid).generate_for_inquiry(inquiry_id)
    candidates = hybrid.requests[0].metadata.get("template_candidates") or []
    assert any("네이버" in str(item.get("answer")) for item in candidates)


def test_a_naver_template_cannot_be_a_coupang_final_answer() -> None:
    from answer.answer_validator import AnswerValidator
    from services.answer_service import _template_unavailable_reason

    coupang = AnswerRequest(store_code="COUPANG_OJE_NS", question="구매내역서")
    naver = AnswerRequest(store_code="OJE_PLUS", question="구매내역서")
    assert _template_unavailable_reason(rule(NAVER_ANSWER), coupang, AnswerValidator()) == "MARKET_WORDING_MISMATCH"
    assert _template_unavailable_reason(rule(NAVER_ANSWER), naver, AnswerValidator()) != "MARKET_WORDING_MISMATCH"


def _hybrid_request(store_code: str) -> AnswerRequest:
    request = AnswerRequest(
        inquiry_id=1, question_id="Q", store_code=store_code, inquiry_type="상품",
        question="넷플릭스 되나요?", product_name="삼성 스마트모니터 M5",
        metadata={"source_type": "PRODUCT_INQUIRY",
                  "dps": {"lookup_required": False, "lookup_status": "NOT_REQUIRED", "warnings": []}},
    )
    if store_code.startswith("COUPANG_"):
        request.metadata["market"] = "COUPANG"
    return request


def test_template_rule_gpt_path_runs_for_coupang_with_the_validator() -> None:
    from answer.providers.fake_gpt_provider import FakeGptProvider
    from services.hybrid_answer_service import HybridAnswerService

    outcome = HybridAnswerService(FakeGptProvider()).generate(
        _hybrid_request("COUPANG_OJE_NS"), rule("인터넷 연결 시 넷플릭스를 사용할 수 있습니다.")
    )
    assert outcome.fallback_used is False
    assert outcome.validation is not None and outcome.validation.passed


def _naver_wording_outcome(store_code: str):
    from answer.providers.fake_gpt_provider import FakeGptProvider
    from services.hybrid_answer_service import HybridAnswerService

    provider = FakeGptProvider(responses={"DRAFT": {
        "answer": "구매내역은 네이버페이 결제내역에서 확인해 주세요.", "confidence": 0.9,
        "used_facts": [], "missing_information": [], "requires_review": False, "warnings": [],
    }})
    return HybridAnswerService(provider).generate(_hybrid_request(store_code), rule(""))


def test_the_validator_rejects_naver_wording_in_a_coupang_answer() -> None:
    coupang = _naver_wording_outcome("COUPANG_OJE_NS")
    assert any("다른 마켓 안내 문구" in error for error in coupang.validation.errors)
    assert "네이버" not in coupang.result.answer

    naver = _naver_wording_outcome("OJE_PLUS")
    assert not any("다른 마켓 안내 문구" in error for error in naver.validation.errors)


def _draft_prompt(store_code: str) -> str:
    from answer.providers.fake_gpt_provider import FakeGptProvider
    from services.hybrid_answer_service import HybridAnswerService

    provider = FakeGptProvider()
    HybridAnswerService(provider).generate(_hybrid_request(store_code), rule("인터넷 연결 시 사용 가능합니다."))
    return next(call["prompt"] for call in provider.calls if call.get("task") == "DRAFT")


def test_the_prompt_names_coupang_and_leaves_naver_alone() -> None:
    coupang = _draft_prompt("COUPANG_OJE_NS")
    naver = _draft_prompt("OJE_PLUS")

    assert "쿠팡 판매 페이지" in coupang and "네이버 판매 페이지" not in coupang
    assert '"marketplace": "COUPANG"' in coupang
    assert "다른 마켓의 주문·결제·구매내역·등록 절차를 안내하지 않습니다" in coupang
    assert '"inquiry.market": "COUPANG"' in coupang
    assert "네이버 판매 페이지" in naver
    assert '"marketplace"' not in naver and '"inquiry.market"' not in naver


# --- F. safety boundary ------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "설치 날짜가 언제인가요? 주문번호 2026091712345678",
    "배송 언제 오나요?",
])
def test_order_and_dps_never_run_for_coupang(database, content) -> None:
    inquiry_id = coupang_inquiry(database, content=content, order_ids=[2026091712345678])
    dps, orders = Recorder(), Recorder()
    answer_service = service(database, dps_enrichment=dps, order_lookup_service=orders)

    plan = answer_service.plans.create(InquiryRepository(database).get(inquiry_id))
    if not (plan.is_delivery or plan.requires_order_lookup or plan.requires_dps_lookup):
        pytest.skip("the analyser did not read this wording as an order/DPS inquiry")
    with pytest.raises(AnswerGenerationError):
        answer_service.generate_for_inquiry(inquiry_id)
    assert dps.calls == 0 and orders.calls == 0
    assert draft_count(database) == 0
    with pytest.raises(AutoAnswerProhibitedError):
        answer_service.enrich_dps_for_inquiry(inquiry_id)


def test_generation_sends_no_kakao_for_coupang(database, monkeypatch) -> None:
    sent: list = []
    monkeypatch.setenv("KAKAO_NOTIFY_ENABLED", "1")
    monkeypatch.setattr(kakao_notify, "enqueue_kakao_message",
                        lambda **kwargs: sent.append(kwargs) or kakao_notify.OUTBOX)
    monkeypatch.setattr(kakao_notify, "_claim_notification", lambda **_k: True)
    monkeypatch.setattr(kakao_notify, "_mark_notification_sent", lambda *_a: None)
    monkeypatch.setattr(kakao_notify, "_validate_kakao_service", lambda: None)

    inquiry_id = coupang_inquiry(database)
    service(database).generate_for_inquiry(inquiry_id)
    _approve(database, inquiry_id)

    assert sent == []
    assert kakao_notify.notify_qna_safely(
        title="t", store_code="COUPANG_OJE_NS", product="p", option_name="",
        question="q", answer="a", action="posted", inquiry_id="x",
    ) is False


def test_an_approved_coupang_answer_is_not_posted(database) -> None:
    inquiry_id = coupang_inquiry(database)
    service(database).generate_for_inquiry(inquiry_id)
    _approve(database, inquiry_id)

    row = InquiryRepository(database).get(inquiry_id)
    assert row["post_status"] != "POSTED"
    assert not AnswerRepository(database).is_inquiry_posted(inquiry_id)
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM naver_post_attempts").fetchone()[0] == 0
