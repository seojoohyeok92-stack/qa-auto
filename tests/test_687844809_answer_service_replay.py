"""Full provider-free AnswerService replay for source_question_id 687844809."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from answer.governance_models import GptProviderSettings
from answer.engine import AnswerEngine
from answer.models import AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.gpt_governance_service import GovernedHybridAnswerService
from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService
from services.product_knowledge_service import ProductKnowledgeService


QUESTION = """안녕하세요.
이 상품 uhd상품 맞는지요?
그리고 벽걸이로 설치 가능한가요?
(벽걸이는 추가요금 있는지도 알려주세요.)
서울지역이고 지금 주문시 배송 기간 얼마나 소요 되나요?"""
SOURCE_IDS = (113, 114, 206724, 317086, 154551, 317404, 227655, 226929)
SOURCE_DB = Path("data/oje_automation.db")
CATALOG = Path("data/model_data_with_color.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _SemanticProvider:
    name = "semantic_fixture"

    def __init__(self):
        self.calls = []

    def generate_json(self, *, task, prompt, context):
        self.calls.append({"task": task, "prompt": prompt, "context": context})
        return {
            "primary_action": "PRODUCT_SPEC",
            "secondary_actions": ["INSTALLATION_METHOD", "DELIVERY_POLICY"],
            "request_type": "MIXED", "objects": [{"type": "TV", "states": ["NEW"]}],
            "atomic_questions": [
                {"text": "UHD 여부", "action": "PRODUCT_SPEC", "requested_attribute": "SPEC_VALUE"},
                {"text": "벽걸이 설치 가능 여부", "action": "INSTALLATION_METHOD", "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
                {"text": "벽걸이 추가요금", "action": "INSTALLATION_METHOD", "requested_attribute": "AMOUNT_OR_COST"},
                {"text": "지금 주문 시 서울지역 배송기간", "action": "DELIVERY_POLICY", "requested_attribute": "TIMING"},
            ],
            "deadline": None, "constraints": [], "negation": False, "conditional": False,
            "requires_order_context": False, "requires_delivery_schedule": False,
            "purchase_state": "PRE_PURCHASE", "confidence": 0.99,
        }


class _DraftProvider(FakeGptProvider):
    name = "draft_fixture"

    def __init__(self):
        super().__init__(responses={"DRAFT": {
            "answer": "문의하신 상품은 4K UHD 해상도 정보가 확인됩니다. 벽걸이 추가비용과 현재 신규 주문 배송기간은 확인 후 안내드리겠습니다.",
            # Product Catalog facts are supplied as evidence context, not an
            # AnswerFacts namespace path.  The production prompt therefore
            # does not advertise ``product_catalog.resolution`` as an allowed
            # ``used_facts`` value; grounding is checked against the supplied
            # catalog corpus below.
            "confidence": 0.9, "used_facts": [],
            "missing_information": ["벽걸이 추가비용", "현재 신규 주문 배송기간"],
            "requires_review": True, "warnings": [],
        }})


class _ProductSpy(ProductKnowledgeService):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.results = []

    def facts_for_inquiry(self, *args, **kwargs):
        self.calls += 1
        result = super().facts_for_inquiry(*args, **kwargs)
        self.results.append(result)
        return result


class _DpsSpy:
    def __init__(self):
        self.enrich_calls = 0
        self.skip_calls = 0

    def skip_for_phase9(self, request, **_kwargs):
        self.skip_calls += 1
        return SimpleNamespace(metadata={"lookup_status": "NOT_REQUIRED"}, lookup_row=None)

    def enrich(self, request, **_kwargs):
        self.enrich_calls += 1
        raise AssertionError("DPS must not run for PRE_PURCHASE")


class _OrderSpy:
    def __init__(self):
        self.calls = 0

    def lookup_for_inquiry(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Order lookup must not run for PRE_PURCHASE")


class _Engine(AnswerEngine):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return super().generate(request)


def _copy_learning(source: sqlite3.Connection, target: Database) -> list[int]:
    source.row_factory = sqlite3.Row
    marks = ",".join("?" for _ in SOURCE_IDS)
    learning = source.execute(
        f"SELECT * FROM learning_examples WHERE id IN ({marks})", SOURCE_IDS,
    ).fetchall()
    inquiry_ids = sorted({int(row["inquiry_id"]) for row in learning if row["inquiry_id"] is not None})
    inquiries = []
    if inquiry_ids:
        inquiry_marks = ",".join("?" for _ in inquiry_ids)
        inquiries = source.execute(
            f"SELECT * FROM inquiries WHERE id IN ({inquiry_marks})", inquiry_ids,
        ).fetchall()
    with target.transaction() as connection:
        for table, rows in (("inquiries", inquiries), ("learning_examples", learning)):
            if not rows:
                continue
            target_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            columns = [name for name in rows[0].keys() if name in target_columns]
            sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
            connection.executemany(sql, [tuple(row[column] for column in columns) for row in rows])
    return [int(row["id"]) for row in learning]


def test_687844809_full_answer_service_replay(tmp_path, monkeypatch):
    source_hash_before = _sha256(SOURCE_DB)
    catalog_hash_before = _sha256(CATALOG)
    source = sqlite3.connect(f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True)
    try:
        db = Database(tmp_path / "replay.db")
        db.initialize()
        copied_ids = _copy_learning(source, db)
    finally:
        source.close()
    assert copied_ids

    inquiry_id = InquiryRepository(db).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "TEST", "source_question_id": "687844809",
        "inquiry_type": "PRODUCT_INQUIRY", "content": QUESTION, "product_id": "9645661432",
        "product_name": "삼성 4K UHD 스마트 사이니지 TV LH43BEDH 기사님 방문설치",
        "option_name": "107.9cm(43인치), 스탠드", "raw_json": {},
    }).inquiry_id

    semantic_provider, draft_provider = _SemanticProvider(), _DraftProvider()
    semantic = GptSemanticAnalyzerService(semantic_provider)
    governed = GovernedHybridAnswerService(
        db, provider=draft_provider, settings=GptProviderSettings(provider_name="fake"),
    )
    engine, dps, order, product = _Engine(), _DpsSpy(), _OrderSpy(), _ProductSpy()
    counts = {
        "learning_build": 0, "hybrid": 0, "draft": 0,
        "phase9_evaluated": 0, "phase9_taken": 0,
        "phase9_policy": 0,
    }
    captured = {}
    import services.answer_service as answer_module
    import services.gpt_governance_service as governance_module
    import services.hybrid_answer_service as hybrid_module
    build, hybrid, draft, phase9, phase9_shortcut = (
        governance_module.LearningContextService.build,
        governance_module.HybridAnswerService.generate,
        hybrid_module.DraftGenerationService.generate,
        answer_module.apply_phase9_rule_policy,
        answer_module.AnswerService._phase9_shortcut_allowed,
    )

    def build_spy(self, *args, **kwargs):
        counts["learning_build"] += 1
        captured["hard_conflicts_only"] = self.hard_conflicts_only
        context = build(self, *args, **kwargs)
        captured["learning_context"] = context
        return context

    def hybrid_spy(self, *args, **kwargs):
        counts["hybrid"] += 1
        request = args[0]
        captured["hybrid_request_metadata"] = dict(request.metadata)
        outcome = hybrid(self, *args, **kwargs)
        captured["hybrid_outcome"] = outcome
        return outcome

    def draft_spy(self, *args, **kwargs):
        counts["draft"] += 1
        return draft(self, *args, **kwargs)

    def phase9_spy(*args, **kwargs):
        counts["phase9_policy"] += 1
        return phase9(*args, **kwargs)

    def phase9_shortcut_spy(request):
        counts["phase9_evaluated"] += 1
        allowed = phase9_shortcut(request)
        if allowed:
            counts["phase9_taken"] += 1
        return allowed

    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(governance_module.LearningContextService, "build", build_spy)
    monkeypatch.setattr(governance_module.HybridAnswerService, "generate", hybrid_spy)
    monkeypatch.setattr(hybrid_module.DraftGenerationService, "generate", draft_spy)
    monkeypatch.setattr(answer_module, "apply_phase9_rule_policy", phase9_spy)
    monkeypatch.setattr(
        answer_module.AnswerService,
        "_phase9_shortcut_allowed",
        staticmethod(phase9_shortcut_spy),
    )
    external_calls = {"kakao_boundary": 0}

    def notification_spy(**_kwargs):
        # Keep the test's held draft away from the real Kakao transport.
        external_calls["kakao_boundary"] += 1
        return False

    monkeypatch.setattr(answer_module, "notify_qna_safely", notification_spy)
    outcome = AnswerService(
        db, engine=engine, hybrid_service=governed, dps_enrichment=dps,
        order_lookup_service=order, product_knowledge=product,
        semantic_analyzer=semantic,
    ).generate_for_inquiry(inquiry_id)

    assert counts["hybrid"] >= 1 and counts["learning_build"] >= 1, {
        "metadata": outcome.result.metadata,
        "counts": counts,
        "semantic_calls": semantic_provider.calls,
        "draft_calls": draft_provider.calls,
    }
    assert counts["draft"] >= 1 and draft_provider.calls, {
        "metadata": outcome.result.metadata,
        "counts": counts,
    }
    with db.connection() as connection:
        events = [dict(row) for row in connection.execute(
            "SELECT event_code, details_json FROM activity_logs ORDER BY id"
        ).fetchall()]
    contract = captured["hybrid_request_metadata"]["gpt_understanding"]
    draft_call = next(call for call in draft_provider.calls if call["task"] == "DRAFT")
    context = draft_call["context"]
    prompt = draft_call["prompt"]
    learning_ids = [
        int(item["learning_example_id"])
        for item in context.get("similar_approved_answers", [])
    ]
    assert len(semantic_provider.calls) == 1
    assert contract["purchase_state"] == "PRE_PURCHASE"
    assert {key: contract[key] for key in ("need_template", "need_product", "need_learning", "need_order", "need_dps")} == {
        "need_template": True, "need_product": True, "need_learning": True,
        "need_order": False, "need_dps": False,
    }
    assert len(contract["questions"]) == 4
    assert product.calls >= 1 and "4K UHD" in context["product_catalog"]["instructions"]
    assert "4K UHD" in prompt
    product_result = product.results[-1]
    catalog_fields = {fact.field_key: fact.value for fact in product_result.safe_facts}
    assert product_result.listing_id == "LH43BEDH"
    assert catalog_fields["resolution"] == "4K UHD"
    assert catalog_fields["vesa_mm"] == "200x200"
    assert engine.calls >= 1 and captured["hybrid_request_metadata"].get(
        "template_candidates"
    ), {
        "metadata": captured["hybrid_request_metadata"],
        "draft_context_keys": sorted(context),
    }
    assert context.get("template_candidates"), {
        "draft_context_keys": sorted(context),
        "learning_context_keys": sorted(captured["learning_context"]),
    }
    assert counts["learning_build"] >= 1 and counts["hybrid"] >= 1 and counts["draft"] >= 1
    assert captured["hard_conflicts_only"] is True
    assert learning_ids, {
        "learning_context_keys": sorted(captured["learning_context"]),
        "learning_retrieval": captured["learning_context"].get("learning_retrieval"),
    }
    # This mixed product inquiry does not enter the delivery-only Phase9
    # branch at all.  In particular, it cannot take a Phase9 final shortcut.
    assert counts["phase9_evaluated"] == 0 and counts["phase9_taken"] == 0
    assert counts["phase9_policy"] == 0
    assert dps.enrich_calls == 0 and dps.skip_calls >= 1
    assert order.calls == 0
    assert len([call for call in draft_provider.calls if call["task"] == "DRAFT"]) == 1
    retrieval = captured["learning_context"]["learning_retrieval"]
    assert set(learning_ids).issubset(set(copied_ids))
    prompt_learning_ids = {
        *learning_ids,
        *(
            int(item["learning_example_id"])
            for item in context.get("seller_style_examples", [])
        ),
    }
    assert prompt_learning_ids <= set(retrieval["selected_learning_ids"])
    assert captured["hybrid_outcome"].draft is not None
    assert captured["hybrid_outcome"].draft.missing_information
    assert "4K UHD" in outcome.result.answer and "1~2주" not in outcome.result.answer
    assert outcome.result.status is AnswerStatus.NEEDS_REVIEW
    assert outcome.result.needs_review is True and outcome.result.auto_answerable is False
    # AnswerService creates a draft only; it does not execute Naver posting.
    # The held-draft notification boundary is reached once, but the local spy
    # returns before any real Kakao transport can run.
    assert external_calls["kakao_boundary"] == 1
    assert _sha256(SOURCE_DB) == source_hash_before
    assert _sha256(CATALOG) == catalog_hash_before
