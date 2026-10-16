"""A 2-b field, from the catalogue file to the answer the customer reads.

The field-level tests prove a section token becomes a fact and reaches
``select_prompt_facts``. They stop there, which leaves the last and longest
part of the path unmeasured: AnswerService's own lookup, the
PRODUCT_CATALOG_JSON block, GPT ②'s structured draft, the
``used_product_facts`` it reports, the validator, and the body that is
actually sent.

So three representative fields run the whole path here, with the real
catalogue and only the two providers stubbed. GPT ② is given no answer text:
it reads the value out of the prompt context it was handed and answers with
it, so a fact that fails to arrive cannot produce a passing answer.

Two of the three questions are paraphrases the keyword router does not match
("색을 몇 비트로 표현하나요?", "C 단자 있나요?"). They answer only because
GPT ①'s ``requested_information``/``retrieval_queries`` reach field routing,
which is the other half of what this file protects.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.engine import AnswerEngine
from answer.governance_models import GptProviderSettings
from answer.providers.fake_gpt_provider import FakeGptProvider
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.dps_enrichment_service import DpsEnrichmentService
from services.gpt_governance_service import GovernedHybridAnswerService
from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService
from services.product_fact_grounding import unsupported_fact_claims


class Case(SimpleNamespace):
    pass


# model, listing title, the customer's words, what GPT ① understands, the
# field it must reach, and the value the catalogue file holds for it.
CASES = (
    Case(
        name="color_bit_depth",
        model="32QN650",
        product_name="LG 32QN650 80cm(32인치) 4K UHD 모니터",
        question="색을 몇 비트로 표현하나요?",
        requested_information="색상 비트 심도",
        retrieval_queries=("컬러 비트 심도", "10bit 패널 여부"),
        field="color_bit_depth",
        value="10bit (8bit + FRC)",
    ),
    Case(
        name="usb_c_present",
        model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치) 화이트",
        question="C 단자 있나요?",
        requested_information="USB-C 단자 지원 여부",
        retrieval_queries=("USB C타입 포트 유무",),
        field="usb_c_present",
        value="USB C타입",
    ),
    Case(
        name="freesync_spec",
        model="S49CG954",
        product_name="삼성 오디세이 OLED G9 S49CG954 123.8cm(49인치)",
        question="프리싱크 지원하나요?",
        requested_information="FreeSync 지원 등급",
        retrieval_queries=(),
        field="freesync_spec",
        value="FreeSync Premium Pro",
    ),
)


class _SemanticProvider:
    """GPT ①. Reads the question and names the fact that is wanted."""

    name = "semantic_fixture"

    def __init__(self, case: Case) -> None:
        self.case = case
        self.calls: list[dict] = []

    def generate_json(self, *, task, prompt, context):
        self.calls.append({"task": task, "prompt": prompt})
        return {
            "primary_action": "PRODUCT_SPEC",
            "secondary_actions": [],
            "request_type": "QUESTION",
            "objects": [{"type": "MONITOR", "states": ["NEW"]}],
            "atomic_questions": [{
                "text": self.case.question,
                "action": "PRODUCT_SPEC",
                "requested_information": self.case.requested_information,
                "requested_attribute": "VALUE",
                "retrieval_queries": list(self.case.retrieval_queries),
            }],
            "deadline": None,
            "constraints": [],
            "negation": False,
            "conditional": False,
            "requires_order_context": False,
            "requires_delivery_schedule": False,
            "asks_delivery_schedule": False,
            "asks_delivery_outcome": False,
            "purchase_state": "PRE_PURCHASE",
            "confidence": 0.95,
        }


class _DraftProvider(FakeGptProvider):
    """GPT ②. Answers from the catalogue fact it was actually given.

    It never carries the expected value: it looks the field up in the
    ``product_catalog`` context and writes the value it finds there. If the
    fact does not reach the prompt there is nothing to answer with, and the
    draft says so instead.
    """

    name = "draft_fixture"

    def __init__(self, case: Case) -> None:
        super().__init__(responses={})
        self.case = case
        self.draft_contexts: list[dict] = []

    def generate_json(self, *, task, prompt, context):
        if str(task).upper() != "DRAFT":
            return super().generate_json(
                task=task, prompt=prompt, context=context)
        self.calls.append({"task": task, "prompt": prompt, "context": context})
        self.draft_contexts.append(context)
        catalog = context.get("product_catalog") or {}
        found = [
            str(item.get("value"))
            for item in (catalog.get("facts") or [])
            if str(item.get("field_key") or item.get("field") or "")
            == self.case.field
        ]
        if found:
            answer = f"해당 제품의 사양은 {found[0]} 입니다."
            used = [self.case.field]
            unresolved: list = []
        else:
            answer = "해당 사양은 확인이 필요합니다."
            used = []
            unresolved = [self.case.question]
        return {
            "answer": answer,
            "confidence": 0.95,
            "used_facts": list(used),
            "missing_information": [],
            "requires_review": False,
            "warnings": [],
            "learning_usage": [],
            "historical_usage": [],
            "subquestion_results": [{
                "subquestion": self.case.question,
                "status": "ANSWERABLE" if used else "UNANSWERABLE",
                "learning_ids": [],
                "answered": bool(used),
            }],
            "evidence_decisions": [
                {
                    "kind": "PRODUCT_FACT",
                    "id": self.case.field,
                    "decision": "USED",
                    "matched_subquestion": self.case.question,
                    "reason": "상품 카탈로그에 검증된 사양 값",
                }
                for _ in used
            ],
            "used_template_ids": [],
            "used_product_facts": list(used),
            "used_learning_ids": [],
            "used_historical_ids": [],
            "ignored_evidence": [],
            "unresolved": unresolved,
            "can_auto_post": bool(used),
            "reason": "카탈로그 사양 값으로 답변했습니다.",
        }


class _DpsSpy(DpsEnrichmentService):
    """The real service, with the outbound lookup made fatal."""

    def __init__(self, database) -> None:
        super().__init__(database)
        self.enrich_calls = 0

    def enrich(self, request, **_kwargs):
        self.enrich_calls += 1
        raise AssertionError("DPS must not run for a specification question")


class _OrderSpy:
    def __init__(self) -> None:
        self.calls = 0

    def lookup_for_inquiry(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Order lookup must not run for a spec question")


def _run(tmp_path, monkeypatch, case: Case):
    """One full AnswerService generation for this case."""

    database = Database(tmp_path / f"{case.name}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "TEST",
        "source_question_id": f"pk-e2e-{case.name}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": case.question,
        "product_id": f"listing-{case.name}",
        "product_name": case.product_name,
        "option_name": "",
        "raw_json": {},
    }).inquiry_id

    semantic_provider = _SemanticProvider(case)
    draft_provider = _DraftProvider(case)
    governed = GovernedHybridAnswerService(
        database,
        provider=draft_provider,
        settings=GptProviderSettings(provider_name="fake"),
    )

    import services.answer_service as answer_module

    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(
        answer_module, "notify_qna_safely", lambda **_kwargs: False,
    )

    outcome = AnswerService(
        database,
        engine=AnswerEngine(),
        hybrid_service=governed,
        dps_enrichment=_DpsSpy(database),
        order_lookup_service=_OrderSpy(),
        semantic_analyzer=GptSemanticAnalyzerService(semantic_provider),
    ).generate_for_inquiry(inquiry_id)

    draft_calls = [call for call in draft_provider.calls
                   if str(call["task"]).upper() == "DRAFT"]
    assert draft_calls, "GPT ② was never called"
    return SimpleNamespace(
        outcome=outcome,
        result=outcome.result,
        draft=outcome.draft or {},
        prompt=draft_calls[-1]["prompt"],
        context=draft_calls[-1]["context"],
        metadata=dict(outcome.result.metadata or {}),
    )


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_a_catalogue_field_reaches_the_answer_the_customer_reads(
    tmp_path, monkeypatch, case: Case,
) -> None:
    run = _run(tmp_path, monkeypatch, case)
    metadata = run.metadata

    # 1. The lookup AnswerService itself performed found the fact. Read from
    #    the recorded catalogue evidence, which is what the rest of the
    #    pipeline and the review UI read.
    catalog_facts = [
        item for item in (metadata.get("product_catalog") or [])
        if isinstance(item, dict)
    ]
    values = [
        str(item.get("value")) for item in catalog_facts
        if str(item.get("field_key")) == case.field
    ]
    assert values == [case.value], {
        "field": case.field, "found": values,
        "fields": sorted({str(item.get("field_key")) for item in catalog_facts}),
    }
    assert case.field in list(
        (metadata.get("hybrid") or {}).get("product_fact_fields") or ())

    # 2. It is in the prompt GPT ② was given, inside the catalogue block.
    assert "PRODUCT_CATALOG_JSON" in run.prompt
    catalog = run.context.get("product_catalog") or {}
    assert case.value in str(catalog.get("instructions") or "")
    assert case.field in [
        str(item.get("field_key") or item.get("field") or "")
        for item in (catalog.get("facts") or [])
    ]

    # 3. GPT ② reported using that fact.
    hybrid = metadata.get("hybrid") or {}
    draft = hybrid.get("draft") or {}
    assert case.field in list(draft.get("used_product_facts") or ())
    assert hybrid.get("product_catalog_in_prompt") is True

    # 4. The validator cleared the answer.
    validation = hybrid.get("validation") or {}
    assert validation.get("passed") is True, validation

    # 5. The body the customer reads carries the catalogue's own value.
    assert case.value in run.result.answer


def test_a_withheld_field_cannot_be_answered(tmp_path, monkeypatch) -> None:
    """The same path, asked for something the catalogue does not state.

    A launch price is withheld on purpose (listing/promotion data), so the
    draft has nothing to answer from and says so. Without this, the test
    above could pass on a pipeline that quotes anything it is asked for.
    """

    case = Case(
        name="withheld_price",
        model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치) 화이트",
        question="출시가가 얼마였나요?",
        requested_information="출시 가격",
        retrieval_queries=("출시가",),
        field="launch_price",
        value="",
    )
    run = _run(tmp_path, monkeypatch, case)
    catalog = run.context.get("product_catalog") or {}
    assert case.field not in [
        str(item.get("field_key") or item.get("field") or "")
        for item in (catalog.get("facts") or [])
    ]
    # Nothing in the block states it, in any field. A presence field used to
    # carry the whole spec as its value, so "출시가: 500,000원" reached the
    # prompt through ``usb_present`` and the pipeline answered with it.
    assert "출시가" not in str(catalog.get("instructions") or "")
    assert "500,000원" not in str(catalog.get("instructions") or "")
    # The customer's own words are in the prompt, naturally; the catalogue's
    # price is what must not be.
    assert "500,000" not in run.prompt
    assert not [
        item for item in (catalog.get("facts") or [])
        if "출시가" in str(item.get("value") or "")
        or "원" == str(item.get("value") or "")[-1:]
    ]
    hybrid = run.metadata.get("hybrid") or {}
    assert list((hybrid.get("draft") or {}).get("used_product_facts") or ()) == []
    # And the answer cannot be auto-posted off a fact nothing stated.
    assert "500,000" not in run.result.answer
    assert run.result.auto_answerable is False

class _InventingProvider(_DraftProvider):
    """GPT ②, reporting product facts the prompt never carried.

    The point of the fixture is that the *name* is the only thing it has: the
    delivered facts say nothing about pivot support, and the draft asserts it
    anyway while citing a field that does not exist.
    """

    name = "inventing_fixture"

    def __init__(self, case: Case, used: list[str], answer: str) -> None:
        super().__init__(case)
        self.used = list(used)
        self.answer_text = answer

    def generate_json(self, *, task, prompt, context):
        if str(task).upper() != "DRAFT":
            return super().generate_json(
                task=task, prompt=prompt, context=context)
        self.calls.append({"task": task, "prompt": prompt, "context": context})
        self.draft_contexts.append(context)
        return {
            "answer": self.answer_text,
            "confidence": 0.95,
            "used_facts": [],
            "missing_information": [],
            "requires_review": False,
            "warnings": [],
            "learning_usage": [],
            "historical_usage": [],
            "subquestion_results": [{
                "subquestion": self.case.question,
                "status": "ANSWERABLE",
                "learning_ids": [],
                "answered": True,
            }],
            "evidence_decisions": [],
            "used_template_ids": [],
            "used_product_facts": list(self.used),
            "used_learning_ids": [],
            "used_historical_ids": [],
            "ignored_evidence": [],
            "unresolved": [],
            "can_auto_post": True,
            "reason": "상품 Fact로 답변했습니다.",
        }


PIVOT = Case(
    name="invented_pivot",
    model="S32DM701",
    product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치) 화이트",
    question="피벗 지원하나요?",
    requested_information="피벗(화면 회전) 지원 여부",
    retrieval_queries=("피벗 회전 지원",),
    field="pivot_spec",
    value="",
)


def _run_with(tmp_path, monkeypatch, case: Case, provider_factory):
    """``_run``, with the draft provider supplied by the caller."""

    database = Database(tmp_path / f"{case.name}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "TEST",
        "source_question_id": f"pk-e2e-{case.name}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": case.question,
        "product_id": f"listing-{case.name}",
        "product_name": case.product_name,
        "option_name": "",
        "raw_json": {},
    }).inquiry_id

    draft_provider = provider_factory(case)
    governed = GovernedHybridAnswerService(
        database,
        provider=draft_provider,
        settings=GptProviderSettings(provider_name="fake"),
    )

    import services.answer_service as answer_module

    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(
        answer_module, "notify_qna_safely", lambda **_kwargs: False,
    )
    outcome = AnswerService(
        database,
        engine=AnswerEngine(),
        hybrid_service=governed,
        dps_enrichment=_DpsSpy(database),
        order_lookup_service=_OrderSpy(),
        semantic_analyzer=GptSemanticAnalyzerService(_SemanticProvider(case)),
    ).generate_for_inquiry(inquiry_id)
    draft_calls = [call for call in draft_provider.calls
                   if str(call["task"]).upper() == "DRAFT"]
    assert draft_calls, "GPT ② was never called"
    return SimpleNamespace(
        result=outcome.result,
        metadata=dict(outcome.result.metadata or {}),
        prompt=draft_calls[-1]["prompt"],
        context=draft_calls[-1]["context"],
    )


def test_a_fact_name_the_prompt_never_delivered_verifies_nothing(
    tmp_path, monkeypatch,
) -> None:
    """Naming a field that does not exist used to create the authority."""

    run = _run_with(
        tmp_path, monkeypatch, PIVOT,
        lambda case: _InventingProvider(
            case, ["invented_pivot_support"],
            "네, 이 제품은 피벗을 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    draft = (run.metadata.get("hybrid") or {}).get("draft") or {}

    assert guard.get("used_product_facts_unknown") == ["invented_pivot_support"]
    assert guard.get("used_product_facts_delivered") == []
    assert guard.get("current_fact_verified") is False
    assert guard.get("current_fact_source") is None
    assert guard.get("auto_post_allowed") is False
    # The name is not laundered into the used list.
    assert list(draft.get("used_product_facts") or ()) == []
    assert list(draft.get("unknown_product_facts") or ()) == [
        "invented_pivot_support"]
    assert run.result.auto_answerable is False
    assert run.result.needs_review is True
    assert run.metadata.get("product_fact_guard_reason") == (
        "GPT_REPORTED_UNDELIVERED_PRODUCT_FACT")


def test_a_mix_of_a_real_and_an_invented_name_also_fails_closed(
    tmp_path, monkeypatch,
) -> None:
    """One good citation does not license the invented one beside it."""

    # A question that really does deliver one of the two names, so the mix is
    # a mix: usb_c_present is in the prompt, pivot support is not.
    case = Case(**{
        **vars(PIVOT),
        "name": "mixed_pivot",
        "question": "C타입 단자 있고 피벗도 되나요?",
        "requested_information": "USB-C 단자 지원 여부와 피벗 지원 여부",
        "retrieval_queries": ("USB C타입 포트 유무", "피벗 회전 지원"),
    })
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["usb_c_present", "invented_pivot_support"],
            "USB C타입 단자가 있으며 피벗도 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    draft = (run.metadata.get("hybrid") or {}).get("draft") or {}

    assert guard.get("used_product_facts_delivered") == ["usb_c_present"]
    assert guard.get("used_product_facts_unknown") == ["invented_pivot_support"]
    assert guard.get("current_fact_verified") is False
    assert guard.get("auto_post_allowed") is False
    assert list(draft.get("used_product_facts") or ()) == ["usb_c_present"]
    assert run.result.auto_answerable is False
    assert run.result.needs_review is True

# -- the value, not only the name -------------------------------------------
#
# Checking that a cited field was delivered left the other half open: a right
# name beside a wrong value. All three of these were GENERATED and
# auto-answerable before the value check.

VALUE_CASES = (
    # (name, model, listing, question, semantic, field, answer, blocked)
    # Unit changed: the number is right and the unit is out by ten.
    ("stand_spacing_unit", "LH43BEFHLGFXKR",
     "삼성 4K UHD 스마트 사이니지 TV LH43BEFHLGFXKR 107.9cm(43인치)",
     "다리 사이 간격이 얼마인가요?", "스탠드 다리 사이 간격", "stand_spacing",
     "스탠드 다리 사이 간격은 674.9cm입니다.", True),
    ("refresh_rate_unit", "S49CG954", "삼성 오디세이 OLED G9 S49CG954",
     "주사율 몇 Hz인가요?", "주사율", "refresh_rate",
     "주사율은 240cd입니다.", True),
    ("brightness_peak_unit", "S49CG954", "삼성 오디세이 OLED G9 S49CG954",
     "HDR 최대 밝기 얼마인가요?", "HDR 피크 밝기", "brightness_peak",
     "최대 밝기는 1000Hz입니다.", True),
    # A secondary number of a compound value standing in for the leading one.
    ("bit_depth_compound", "32QN650", "LG 32QN650 80cm(32인치) 4K UHD 모니터",
     "색을 몇 비트로 표현하나요?", "색상 비트 심도", "color_bit_depth",
     "이 제품은 12bit (8bit + FRC) 색심도를 지원합니다.", True),
    # A list member the delivered value states, denied.
    ("ott_member_denied", "S32CM501", "삼성 스마트모니터 M5 S32CM501",
     "쿠팡플레이 되나요?", "쿠팡플레이 지원 여부", "ott_apps_spec",
     "쿠팡플레이는 지원하지 않습니다.", True),
    # A wrong tier asserted, then disclaimed in a later sentence.
    ("freesync_hedge_laundering", "S49CG954",
     "삼성 오디세이 OLED G9 S49CG954", "프리싱크 지원하나요?",
     "FreeSync 지원 등급", "freesync_spec",
     "FreeSync Premium만 지원합니다. 정확한 다른 사양은 확인이 필요합니다.", True),
    # The same facts, answered as the catalogue states them.
    ("refresh_rate_right", "S49CG954", "삼성 오디세이 OLED G9 S49CG954",
     "주사율 몇 Hz인가요?", "주사율", "refresh_rate",
     "주사율은 240Hz입니다.", False),
    ("brightness_peak_right", "S49CG954", "삼성 오디세이 OLED G9 S49CG954",
     "HDR 최대 밝기 얼마인가요?", "HDR 피크 밝기", "brightness_peak",
     "최대 밝기는 1,000cd입니다.", False),
    ("ott_member_right", "S32CM501", "삼성 스마트모니터 M5 S32CM501",
     "쿠팡플레이 되나요?", "쿠팡플레이 지원 여부", "ott_apps_spec",
     "쿠팡플레이를 지원합니다.", False),
    ("bit_depth_wrong", "32QN650", "LG 32QN650 80cm(32인치) 4K UHD 모니터",
     "색을 몇 비트로 표현하나요?", "색상 비트 심도", "color_bit_depth",
     "이 제품은 12비트 색심도를 지원합니다.", True),
    ("bit_depth_right", "32QN650", "LG 32QN650 80cm(32인치) 4K UHD 모니터",
     "색을 몇 비트로 표현하나요?", "색상 비트 심도", "color_bit_depth",
     "이 제품은 10bit (8bit + FRC) 색심도를 지원합니다.", False),
    ("freesync_denied", "S49CG954", "삼성 오디세이 OLED G9 S49CG954",
     "프리싱크 지원하나요?", "FreeSync 지원 등급", "freesync_spec",
     "이 제품은 FreeSync를 지원하지 않습니다.", True),
    ("freesync_right", "S49CG954", "삼성 오디세이 OLED G9 S49CG954",
     "프리싱크 지원하나요?", "FreeSync 지원 등급", "freesync_spec",
     "이 제품은 FreeSync Premium Pro를 지원합니다.", False),
    ("usb_c_denied", "S32DM701", "삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
     "C 단자 있나요?", "USB-C 단자 지원 여부", "usb_c_present",
     "USB-C 단자는 지원하지 않습니다.", True),
    ("usb_c_right", "S32DM701", "삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
     "C 단자 있나요?", "USB-C 단자 지원 여부", "usb_c_present",
     "USB C타입 단자를 지원합니다.", False),
    # The measurement, asked as a measurement. "80cm 이내인가요?" is a
    # compatibility verdict, which an older gate holds for staff before GPT ②
    # is consulted at all -- a different rule, left alone; the unit tests in
    # test_product_fact_value_grounding.py cover that wording.
    ("stand_spacing_right", "LH43BEFHLGFXKR",
     "삼성 4K UHD 스마트 사이니지 TV LH43BEFHLGFXKR 107.9cm(43인치)",
     "다리 사이 간격이 얼마인가요?", "스탠드 다리 사이 간격",
     "stand_spacing",
     "스탠드 다리 사이 간격은 674.9mm입니다.", False),
)


@pytest.mark.parametrize(
    "name, model, product_name, question, wanted, field, answer, blocked",
    VALUE_CASES, ids=[row[0] for row in VALUE_CASES],
)
def test_the_answer_has_to_carry_the_value_it_cites(
    tmp_path, monkeypatch, name, model, product_name, question, wanted,
    field, answer, blocked,
) -> None:
    case = Case(
        name=name, model=model, product_name=product_name, question=question,
        requested_information=wanted, retrieval_queries=(wanted,),
        field=field, value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(item, [field], answer),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    delivered = {
        str(item.get("field_key")): str(item.get("value"))
        for item in (run.metadata.get("product_catalog") or ())
        if isinstance(item, dict)
    }
    # The fixture is only meaningful if the fact really was delivered.
    assert field in delivered, sorted(delivered)
    assert guard.get("used_product_facts_unknown") == []
    unsupported = guard.get("used_product_facts_unsupported") or []

    if blocked:
        # The answer must not reach the customer. Which gate stops it is not
        # fixed here: a changed quantity in a unit the validator already
        # knows ("1000Hz") is rejected before the draft is ever accepted, so
        # that draft never reaches this gate at all. What is asserted for
        # every case is that nothing is auto-answerable; where the draft did
        # reach the gate, the value check is what held it.
        assert run.result.auto_answerable is False
        assert guard.get("current_fact_verified") is not True
        if guard.get("used_product_facts_delivered"):
            assert [item["field_key"] for item in unsupported] == [field], guard
            assert guard.get("auto_post_allowed") is False
            assert run.result.needs_review is True
            assert run.metadata.get("product_fact_guard_reason") == (
                "PRODUCT_FACT_VALUE_NOT_PRESERVED")
        # The draft body still goes to staff -- that is what review is -- so
        # what matters is that it cannot be sent, asserted above.
    else:
        assert guard.get("used_product_facts_delivered") == [field]
        assert unsupported == [], guard
        assert guard.get("auto_post_allowed") is True
        assert run.result.auto_answerable is True
        # ``current_fact_verified`` is only required of a question the
        # keyword guard calls a product-fact question; the others never ask
        # for verification at all, which is unchanged by this work.
        if guard.get("sensitive"):
            assert guard.get("current_fact_verified") is True
        # And the body the customer reads keeps the catalogue's own value,
        # which is the rule this file is about.
        assert unsupported_fact_claims(
            run.result.answer, {field: delivered[field]}) == []

# -- a claim with no citation at all -----------------------------------------
#
# The value check reads the rows GPT ② reported, so an empty report meant no
# check ran. Measured: with ``color_bit_depth`` in the prompt and
# ``used_product_facts: []``, "12비트 색심도를 지원합니다" was GENERATED and
# auto-answerable; the same with ``usb_c_present`` and "USB-C 단자는 지원하지
# 않습니다".

UNCITED_CASES = (
    ("uncited_bit_depth", "32QN650", "LG 32QN650 80cm(32인치) 4K UHD 모니터",
     "색을 몇 비트로 표현하나요?", "색상 비트 심도", "color_bit_depth",
     "이 제품은 12비트 색심도를 지원합니다.", True),
    ("uncited_usb_c", "S32DM701", "삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
     "C 단자 있나요?", "USB-C 단자 지원 여부", "usb_c_present",
     "USB-C 단자는 지원하지 않습니다.", True),
    # Even a correct body has to name its evidence: the answer may be right,
    # and nothing was offered as the reason it is.
    ("uncited_but_correct", "32QN650", "LG 32QN650 80cm(32인치) 4K UHD 모니터",
     "색을 몇 비트로 표현하나요?", "색상 비트 심도", "color_bit_depth",
     "이 제품은 10bit (8bit + FRC) 색심도를 지원합니다.", True),
)


@pytest.mark.parametrize(
    "name, model, product_name, question, wanted, field, answer, blocked",
    UNCITED_CASES, ids=[row[0] for row in UNCITED_CASES],
)
def test_a_product_claim_has_to_name_its_evidence(
    tmp_path, monkeypatch, name, model, product_name, question, wanted,
    field, answer, blocked,
) -> None:
    case = Case(
        name=name, model=model, product_name=product_name, question=question,
        requested_information=wanted, retrieval_queries=(wanted,),
        field=field, value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        # No citations at all: not a product fact, not a Learning id, not a
        # template.
        lambda item: _InventingProvider(item, [], answer),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    uncited = guard.get("product_claims_without_citation") or []

    # One sentence can be a claim about more than one delivered attribute --
    # "USB-C 단자는 지원하지 않습니다" speaks to usb_c_present and usb_present
    # alike -- so what is asserted is that the asked one is among them.
    assert field in [item["field_key"] for item in uncited], guard
    assert guard.get("auto_post_allowed") is False
    assert guard.get("current_fact_verified") is not True
    assert run.result.auto_answerable is False
    assert run.result.needs_review is True
    assert run.metadata.get("product_fact_guard_reason") in {
        "PRODUCT_CLAIM_WITHOUT_CITATION", "PRODUCT_FACT_VALUE_NOT_PRESERVED",
    }


def test_a_policy_answer_that_cites_nothing_is_untouched(
    tmp_path, monkeypatch,
) -> None:
    """The requirement is about product claims, not about every answer.

    A delivery answer names no specification, so nothing here applies to it
    and it keeps the path it had.
    """

    case = Case(
        name="policy_no_citation", model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
        question="배송은 얼마나 걸리나요?", requested_information="배송 소요 기간",
        retrieval_queries=("배송 기간",), field="usb_c_present", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, [], "결제 후 2~3일 내에 택배로 발송됩니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    assert (guard.get("product_claims_without_citation") or []) == []
    assert run.metadata.get("product_fact_guard_reason") != (
        "PRODUCT_CLAIM_WITHOUT_CITATION")


def test_an_answer_that_cites_its_fact_correctly_still_posts(
    tmp_path, monkeypatch,
) -> None:
    """The control for the three above: citation present, value kept."""

    case = Case(
        name="cited_and_correct", model="32QN650",
        product_name="LG 32QN650 80cm(32인치) 4K UHD 모니터",
        question="색을 몇 비트로 표현하나요?",
        requested_information="색상 비트 심도",
        retrieval_queries=("색상 비트 심도",), field="color_bit_depth",
        value="10bit (8bit + FRC)",
    )
    run = _run_with(tmp_path, monkeypatch, case, _DraftProvider)
    guard = run.metadata.get("product_fact_guard") or {}
    assert (guard.get("product_claims_without_citation") or []) == []
    assert (guard.get("used_product_facts_unsupported") or []) == []
    assert guard.get("auto_post_allowed") is True
    assert run.result.auto_answerable is True
    assert "10bit (8bit + FRC)" in run.result.answer

# -- the asked item reaches the production call ------------------------------
#
# ``unsupported_fact_claims`` judges a list by the item the customer asked
# about, and the unit tests passed ``asked=``. AnswerService did not, so in
# production an OTT list was judged by nothing: asked "애플TV 되나요?" with
# ``ott_apps_spec`` cited and delivered, both "지원합니다." and "애플TV를
# 지원합니다." were GENERATED and auto-answerable.

OTT_CASES = (
    # GPT ① names the attribute as well as the item, which is how the OTT
    # list is requested at all for an app the router has never seen.
    ("ott_unlisted_bare", "애플TV 되나요?", "애플TV 등 OTT 앱 지원 여부",
     "지원합니다.", True),
    ("ott_unlisted_named", "애플TV 되나요?", "애플TV 등 OTT 앱 지원 여부",
     "애플TV를 지원합니다.", True),
    ("ott_listed_bare", "쿠팡플레이 되나요?", "쿠팡플레이 등 OTT 앱 지원 여부",
     "지원합니다.", False),
    ("ott_listed_named", "쿠팡플레이 되나요?", "쿠팡플레이 등 OTT 앱 지원 여부",
     "쿠팡플레이를 지원합니다.", False),
    ("ott_listed_denied", "쿠팡플레이 되나요?", "쿠팡플레이 등 OTT 앱 지원 여부",
     "지원하지 않습니다.", True),
)


@pytest.mark.parametrize("name, question, wanted, answer, blocked",
                         OTT_CASES, ids=[row[0] for row in OTT_CASES])
def test_a_list_fact_is_judged_by_the_item_that_was_asked(
    tmp_path, monkeypatch, name, question, wanted, answer, blocked,
) -> None:
    case = Case(
        name=name, model="S32CM501",
        product_name="삼성 스마트모니터 M5 S32CM501 80cm(32인치)",
        question=question, requested_information=wanted,
        retrieval_queries=(wanted,), field="ott_apps_spec", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(item, ["ott_apps_spec"], answer),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    delivered = {
        str(item.get("field_key")): str(item.get("value"))
        for item in (run.metadata.get("product_catalog") or ())
        if isinstance(item, dict)
    }
    assert "쿠팡플레이" in delivered.get("ott_apps_spec", ""), delivered
    unsupported = guard.get("used_product_facts_unsupported") or []

    if blocked:
        assert run.result.auto_answerable is False
        assert guard.get("auto_post_allowed") is False
        assert [item["field_key"] for item in unsupported] == [
            "ott_apps_spec"], guard
        assert {item["reason"] for item in unsupported} <= {
            "LIST_MEMBER_NOT_DELIVERED", "LIST_MEMBER_DENIED"}
    else:
        assert unsupported == [], guard
        assert guard.get("auto_post_allowed") is True
        assert run.result.auto_answerable is True


# -- a citation covers its own attribute, not the answer ---------------------


def test_citing_one_attribute_does_not_license_a_claim_about_another(
    tmp_path, monkeypatch,
) -> None:
    """The reported bypass: cite HDMI, then state the bit depth wrongly."""

    case = Case(
        name="unrelated_citation", model="32QN650",
        product_name="LG 32QN650 80cm(32인치) 4K UHD 모니터",
        question="색을 몇 비트로 표현하나요?",
        requested_information="색상 비트 심도",
        retrieval_queries=("색상 비트 심도",), field="color_bit_depth",
        value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["hdmi_present"],
            "HDMI를 지원하며 색심도는 12비트입니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    uncited = guard.get("product_claims_without_citation") or []

    assert "color_bit_depth" in [item["field_key"] for item in uncited], guard
    assert guard.get("auto_post_allowed") is False
    assert guard.get("current_fact_verified") is not True
    assert run.result.auto_answerable is False
    assert run.result.needs_review is True


def test_citing_the_attribute_it_answers_still_posts(
    tmp_path, monkeypatch,
) -> None:
    case = Case(
        name="related_citation", model="32QN650",
        product_name="LG 32QN650 80cm(32인치) 4K UHD 모니터",
        question="색을 몇 비트로 표현하나요?",
        requested_information="색상 비트 심도",
        retrieval_queries=("색상 비트 심도",), field="color_bit_depth",
        value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["color_bit_depth"],
            "색심도는 10bit (8bit + FRC)입니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    assert (guard.get("product_claims_without_citation") or []) == []
    assert (guard.get("used_product_facts_unsupported") or []) == []
    assert guard.get("auto_post_allowed") is True
    assert run.result.auto_answerable is True


def test_several_attributes_cited_and_answered_correctly_post(
    tmp_path, monkeypatch,
) -> None:
    case = Case(
        name="two_citations", model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
        question="C 단자 있나요? HDMI 버전은요?",
        requested_information="USB-C 단자 지원 여부와 HDMI 버전",
        retrieval_queries=("USB C타입 포트 유무", "HDMI 버전"),
        field="usb_c_present", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["usb_c_present", "hdmi_version_spec"],
            "USB C타입 단자를 지원하며 HDMI 2.0을 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    assert (guard.get("used_product_facts_unsupported") or []) == []
    assert (guard.get("product_claims_without_citation") or []) == []
    assert guard.get("auto_post_allowed") is True
    assert run.result.auto_answerable is True


def test_one_right_citation_does_not_cover_a_second_wrong_claim(
    tmp_path, monkeypatch,
) -> None:
    """Cited and correct about the ports; uncited and wrong about the panel."""

    case = Case(
        name="mixed_claims", model="32QN650",
        product_name="LG 32QN650 80cm(32인치) 4K UHD 모니터",
        question="HDMI 단자 있나요? 색심도는요?",
        requested_information="HDMI 단자 지원 여부와 색상 비트 심도",
        retrieval_queries=("HDMI 단자 유무", "색상 비트 심도"),
        field="hdmi_present", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["hdmi_present"],
            "HDMI 단자를 지원하며 색심도는 12비트입니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    uncited = guard.get("product_claims_without_citation") or []
    assert uncited, guard
    assert guard.get("auto_post_allowed") is False
    assert run.result.auto_answerable is False

# -- an attribute nothing delivered ------------------------------------------
#
# The value and citation checks both start from a delivered fact, so a
# specification the catalogue never supplied had nothing to compare. Measured
# on S49CG954, asked "FreeSync와 G-Sync를 지원하나요?" where only
# ``freesync_spec`` is delivered: "FreeSync Premium Pro를 지원하며 G-Sync도
# 지원합니다" was GENERATED, auto-answerable and recorded as verified.


def test_an_attribute_with_no_delivered_fact_cannot_be_settled(
    tmp_path, monkeypatch,
) -> None:
    case = Case(
        name="gsync_no_evidence", model="S49CG954",
        product_name="삼성 오디세이 OLED G9 S49CG954",
        question="FreeSync와 G-Sync를 지원하나요?",
        requested_information="FreeSync와 G-Sync 지원 여부",
        retrieval_queries=("FreeSync 지원 등급", "G-Sync 지원 여부"),
        field="freesync_spec", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["freesync_spec"],
            "FreeSync Premium Pro를 지원하며 G-Sync도 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    delivered = {
        str(item.get("field_key"))
        for item in (run.metadata.get("product_catalog") or ())
        if isinstance(item, dict)
    }
    # The fixture only means something while the catalogue is silent on it.
    assert "freesync_spec" in delivered
    assert "gsync_spec" not in delivered

    without_evidence = guard.get("product_claims_without_evidence") or []
    assert [item["field_key"] for item in without_evidence] == ["gsync_spec"]
    assert guard.get("auto_post_allowed") is False
    assert guard.get("current_fact_verified") is not True
    assert run.result.auto_answerable is False
    assert run.result.needs_review is True
    assert run.metadata.get("product_fact_guard_reason") == (
        "PRODUCT_ATTRIBUTE_WITHOUT_EVIDENCE")


def test_the_attribute_that_was_delivered_still_posts(
    tmp_path, monkeypatch,
) -> None:
    """The control: ask only what the catalogue answers, and answer it."""

    case = Case(
        name="freesync_only", model="S49CG954",
        product_name="삼성 오디세이 OLED G9 S49CG954",
        question="프리싱크 지원하나요?", requested_information="FreeSync 지원 등급",
        retrieval_queries=("FreeSync 지원 등급",), field="freesync_spec",
        value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["freesync_spec"],
            "FreeSync Premium Pro를 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    assert (guard.get("product_claims_without_evidence") or []) == []
    assert (guard.get("used_product_facts_unsupported") or []) == []
    assert guard.get("auto_post_allowed") is True
    assert run.result.auto_answerable is True


def test_saying_the_missing_attribute_needs_checking_is_safe(
    tmp_path, monkeypatch,
) -> None:
    """A hedge about the attribute nothing covers settles nothing."""

    case = Case(
        name="gsync_hedged", model="S49CG954",
        product_name="삼성 오디세이 OLED G9 S49CG954",
        question="FreeSync와 G-Sync를 지원하나요?",
        requested_information="FreeSync와 G-Sync 지원 여부",
        retrieval_queries=("FreeSync 지원 등급", "G-Sync 지원 여부"),
        field="freesync_spec", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["freesync_spec"],
            "FreeSync Premium Pro를 지원합니다. "
            "G-Sync 지원 여부는 확인이 필요하여 담당자가 안내드리겠습니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    assert (guard.get("product_claims_without_evidence") or []) == []
    assert run.metadata.get("product_fact_guard_reason") != (
        "PRODUCT_ATTRIBUTE_WITHOUT_EVIDENCE")


def test_an_unrequested_attribute_added_on_the_way_out_is_held(
    tmp_path, monkeypatch,
) -> None:
    """Nobody asked about pivot, and nothing delivered it."""

    case = Case(
        name="added_pivot", model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
        question="C 단자 있나요?", requested_information="USB-C 단자 지원 여부",
        retrieval_queries=("USB C타입 포트 유무",), field="usb_c_present",
        value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["usb_c_present"],
            "USB C타입 단자를 지원하며 피벗도 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    assert "pivot_spec" in [
        item["field_key"]
        for item in (guard.get("product_claims_without_evidence") or [])
    ], guard
    assert run.result.auto_answerable is False


def test_a_policy_answer_is_not_a_product_attribute_claim(
    tmp_path, monkeypatch,
) -> None:
    case = Case(
        name="policy_attribute", model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
        question="배송은 얼마나 걸리나요?", requested_information="배송 소요 기간",
        retrieval_queries=("배송 기간",), field="usb_c_present", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, [], "결제 후 2~3일 내에 택배로 발송됩니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    assert (guard.get("product_claims_without_evidence") or []) == []
    assert run.metadata.get("product_fact_guard_reason") not in {
        "PRODUCT_ATTRIBUTE_WITHOUT_EVIDENCE", "PRODUCT_CLAIM_WITHOUT_CITATION",
    }


def test_citing_hdmi_presence_does_not_cover_an_hdmi_version_claim(
    tmp_path, monkeypatch,
) -> None:
    """Having a port, having two of them and having 2.1 are three claims."""

    case = Case(
        name="hdmi_version_bypass", model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
        question="HDMI 단자 있나요? HDMI 버전은요?",
        requested_information="HDMI 단자 지원 여부와 HDMI 버전",
        retrieval_queries=("HDMI 단자 유무", "HDMI 버전"),
        field="hdmi_present", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["hdmi_present"], "HDMI를 지원하며 HDMI 9.9를 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    flagged = [
        item["field_key"]
        for item in (guard.get("product_claims_without_citation") or [])
    ] + [
        item["field_key"]
        for item in (guard.get("used_product_facts_unsupported") or [])
    ] + [
        item["field_key"]
        for item in (guard.get("product_claims_without_evidence") or [])
    ]
    assert "hdmi_version_spec" in flagged, guard
    assert guard.get("auto_post_allowed") is False
    assert run.result.auto_answerable is False

# -- two claims in one sentence ---------------------------------------------
#
# A citation was briefly allowed to exempt the whole sentence it appeared in,
# to stop "최대 밝기는 1,000cd입니다" from demanding a citation of the typical
# and minimum brightness beside the peak. That exemption covered a second
# claim in the same sentence: on S32DM701, citing only ``usb_c_present`` and
# answering "USB C타입 단자와 HDMI 단자를 모두 지원합니다" was GENERATED and
# auto-answerable with the HDMI half citing nothing.


def test_one_citation_does_not_cover_a_second_claim_in_the_same_sentence(
    tmp_path, monkeypatch,
) -> None:
    case = Case(
        name="two_claims_one_sentence", model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
        question="USB-C 단자와 HDMI 단자 둘 다 있나요?",
        requested_information="USB-C 단자와 HDMI 단자 지원 여부",
        retrieval_queries=("USB C타입 포트 유무", "HDMI 단자 유무"),
        field="usb_c_present", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["usb_c_present"],
            "USB C타입 단자와 HDMI 단자를 모두 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    uncited = [
        item["field_key"]
        for item in (guard.get("product_claims_without_citation") or [])
    ]
    assert "hdmi_present" in uncited, guard
    assert guard.get("auto_post_allowed") is False
    assert run.result.auto_answerable is False
    assert run.result.needs_review is True


def test_citing_both_attributes_in_that_sentence_posts(
    tmp_path, monkeypatch,
) -> None:
    case = Case(
        name="two_claims_both_cited", model="S32DM701",
        product_name="삼성 스마트모니터 M7 S32DM701 80cm(32인치)",
        question="USB-C 단자와 HDMI 단자 둘 다 있나요?",
        requested_information="USB-C 단자와 HDMI 단자 지원 여부",
        retrieval_queries=("USB C타입 포트 유무", "HDMI 단자 유무"),
        field="usb_c_present", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["usb_c_present", "hdmi_present"],
            "USB C타입 단자와 HDMI 단자를 모두 지원합니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    assert (guard.get("product_claims_without_citation") or []) == []
    assert (guard.get("used_product_facts_unsupported") or []) == []
    assert guard.get("auto_post_allowed") is True
    assert run.result.auto_answerable is True


def test_citing_the_peak_does_not_demand_the_other_brightnesses(
    tmp_path, monkeypatch,
) -> None:
    """The over-block the sentence exemption was there for, kept closed."""

    case = Case(
        name="peak_only_citation", model="S49CG954",
        product_name="삼성 오디세이 OLED G9 S49CG954",
        question="HDR 최대 밝기 얼마인가요?", requested_information="HDR 피크 밝기",
        retrieval_queries=("HDR 피크 밝기",), field="brightness_peak",
        value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(
            item, ["brightness_peak"], "최대 밝기는 1,000cd입니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    uncited = [
        item["field_key"]
        for item in (guard.get("product_claims_without_citation") or [])
    ]
    assert "brightness_typical" not in uncited, guard
    assert "brightness_min" not in uncited, guard
    assert (guard.get("used_product_facts_unsupported") or []) == []
    assert guard.get("auto_post_allowed") is True
    assert run.result.auto_answerable is True

# -- the number has to belong to the attribute it is said of -----------------
#
# ``grounded_measures`` once held bare ``(number, unit)`` pairs, so a figure a
# citation accounted for was accounted for anywhere in the answer. Measured on
# S49CG954 (typical 450, minimum 360, peak 1,000cd), citing ``brightness_peak``
# alone: "일반 밝기는 1,000cd입니다", "최소 밝기는 1,000cd입니다" and even
# "주사율은 1,000cd입니다" were all GENERATED and auto-answerable -- the right
# number attached to the wrong attribute.

BRIGHTNESS_CASES = (
    ("peak_said_of_peak", ["brightness_peak"],
     "최대 밝기는 1,000cd입니다.", False),
    ("peak_said_of_typical", ["brightness_peak"],
     "일반 밝기는 1,000cd입니다.", True),
    ("peak_said_of_minimum", ["brightness_peak"],
     "최소 밝기는 1,000cd입니다.", True),
    ("peak_said_of_refresh_rate", ["brightness_peak"],
     "주사율은 1,000cd입니다.", True),
    ("both_said_correctly", ["brightness_peak", "brightness_typical"],
     "일반 밝기는 450cd이고 최대 밝기는 1,000cd입니다.", False),
    ("both_said_swapped", ["brightness_peak", "brightness_typical"],
     "일반 밝기는 1,000cd이고 최대 밝기는 450cd입니다.", True),
)


@pytest.mark.parametrize("name, cited, answer, blocked", BRIGHTNESS_CASES,
                         ids=[row[0] for row in BRIGHTNESS_CASES])
def test_a_cited_figure_grounds_only_its_own_attribute(
    tmp_path, monkeypatch, name, cited, answer, blocked,
) -> None:
    case = Case(
        name=name, model="S49CG954",
        product_name="삼성 오디세이 OLED G9 S49CG954",
        question="일반 밝기와 최대 밝기는 각각 얼마인가요?",
        requested_information="일반 밝기와 HDR 최대 밝기",
        retrieval_queries=("일반 밝기", "HDR 피크 밝기"),
        field="brightness_peak", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(item, list(cited), answer),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    delivered = {
        str(item.get("field_key")): str(item.get("value"))
        for item in (run.metadata.get("product_catalog") or ())
        if isinstance(item, dict)
    }
    # The fixture only means something while all three readings are delivered.
    assert delivered.get("brightness_peak") == "1,000cd", delivered
    assert delivered.get("brightness_typical") == "450"
    assert delivered.get("brightness_min") == "360"

    flagged = [
        item["field_key"]
        for key in ("used_product_facts_unsupported",
                    "product_claims_without_citation",
                    "product_claims_without_evidence")
        for item in (guard.get(key) or [])
    ]
    if blocked:
        assert flagged, guard
        assert guard.get("auto_post_allowed") is False
        assert guard.get("current_fact_verified") is not True
        assert run.result.auto_answerable is False
        assert run.result.needs_review is True
    else:
        assert flagged == [], guard
        assert guard.get("auto_post_allowed") is True
        assert run.result.auto_answerable is True

# -- one figure, two subjects ------------------------------------------------
#
# A cited figure grounds the attribute it is said of. Said of two attributes
# at once it grounded both, and the second one was never cited: measured on
# S49CG954, citing ``brightness_peak`` alone, "일반 밝기와 최대 밝기는
# 1,000cd입니다", "최대 밝기와 주사율은 1,000cd입니다" and "최대 밝기는
# 1,000cd이고 일반 밝기도 동일합니다" were all GENERATED and auto-answerable,
# as was "일반 밝기와 최소 밝기는 450cd입니다" citing only the typical.

SHARED_FIGURE_CASES = (
    ("one_subject_one_figure", ["brightness_peak"],
     "최대 밝기는 1,000cd입니다.", False),
    ("two_subjects_one_figure", ["brightness_peak"],
     "일반 밝기와 최대 밝기는 1,000cd입니다.", True),
    ("two_unrelated_subjects", ["brightness_peak"],
     "최대 밝기와 주사율은 1,000cd입니다.", True),
    ("typical_shared_with_minimum", ["brightness_typical"],
     "일반 밝기와 최소 밝기는 450cd입니다.", True),
    ("figure_carried_over_by_reference", ["brightness_peak"],
     "최대 밝기는 1,000cd이고 일반 밝기도 동일합니다.", True),
    ("each_attribute_its_own_figure",
     ["brightness_peak", "brightness_typical"],
     "일반 밝기는 450cd이고 최대 밝기는 1,000cd입니다.", False),
    ("brightness_and_refresh_rate_each_cited",
     ["brightness_peak", "refresh_rate"],
     "최대 밝기는 1,000cd이고 주사율은 240Hz입니다.", False),
)


@pytest.mark.parametrize("name, cited, answer, blocked", SHARED_FIGURE_CASES,
                         ids=[row[0] for row in SHARED_FIGURE_CASES])
def test_a_cited_figure_grounds_one_attribute_not_two(
    tmp_path, monkeypatch, name, cited, answer, blocked,
) -> None:
    case = Case(
        name=name, model="S49CG954",
        product_name="삼성 오디세이 OLED G9 S49CG954",
        question="일반 밝기와 최대 밝기, 주사율은 각각 얼마인가요?",
        requested_information="일반 밝기, HDR 최대 밝기, 주사율",
        retrieval_queries=("일반 밝기", "HDR 피크 밝기", "주사율"),
        field="brightness_peak", value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda item: _InventingProvider(item, list(cited), answer),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    delivered = {
        str(item.get("field_key")): str(item.get("value"))
        for item in (run.metadata.get("product_catalog") or ())
        if isinstance(item, dict)
    }
    # The fixture only means something while every reading is delivered.
    for field in ("brightness_peak", "brightness_typical", "brightness_min",
                  "refresh_rate"):
        assert field in delivered, (field, sorted(delivered))

    flagged = [
        item["field_key"]
        for key in ("used_product_facts_unsupported",
                    "product_claims_without_citation",
                    "product_claims_without_evidence")
        for item in (guard.get(key) or [])
    ]
    if blocked:
        assert flagged, guard
        assert guard.get("auto_post_allowed") is False
        assert guard.get("current_fact_verified") is not True
        assert run.result.auto_answerable is False
        assert run.result.needs_review is True
    else:
        assert flagged == [], guard
        assert guard.get("auto_post_allowed") is True
        assert run.result.auto_answerable is True


# ---------------------------------------------------------------------------
# 2-c: a field read from an unlabelled enumeration token
# ---------------------------------------------------------------------------
# One case per shape the new fields take, because the shape is what the
# grounding gate reads and each one fails differently: a yes/no has to be
# contradicted by wording, a measurement by a figure, a list by a member, and
# a plain text value by the quote. A field stored in the catalogue and never
# used in production is not a recovery, so these run the whole path.
ENUM_CASES = (
    Case(
        name="tilt_polarity",
        model="G7 G70B S32BG700",
        product_name="삼성 오디세이 G7 S32BG700 80.1cm(32인치)",
        question="틸트 되나요?",
        requested_information="스탠드 틸트 지원 여부",
        retrieval_queries=("상하 각도 조절",),
        field="tilt",
        value="지원",
    ),
    Case(
        name="flat_curved_text",
        model="S27F322",
        product_name="삼성 에센셜 모니터 S27F322 68.4cm(27인치)",
        question="화면이 평면인가요 커브드인가요?",
        requested_information="화면 곡면 여부",
        retrieval_queries=("평면 커브드",),
        field="flat_curved",
        value="평면",
    ),
    Case(
        name="curvature_numeric",
        model="LC32HG70QQKXKR",
        product_name="삼성 게이밍 모니터 LC32HG70QQKXKR 80cm(32인치)",
        question="곡률이 어떻게 되나요?",
        requested_information="화면 곡률 반경",
        retrieval_queries=("곡률 R값",),
        field="curvature",
        value="1800R",
    ),
    Case(
        name="power_typical_condition",
        model="S22D300",
        product_name="삼성 에센셜 모니터 S22D300 54.6cm(22인치)",
        question="일반 소비전력이 몇 W인가요?",
        requested_information="표준 소비전력",
        retrieval_queries=("typical 소비전력",),
        field="power_typical",
        value="17W",
    ),
    Case(
        name="product_category_list",
        model="LH32DCE2LGA",
        product_name="삼성 상업용 디스플레이 LH32DCE2LGA 80cm(32인치)",
        question="품목이 무엇인가요?",
        requested_information="제품 품목 분류",
        retrieval_queries=("제품 종류",),
        field="product_category",
        value="상업용디스플레이, 디스플레이모듈",
    ),
    Case(
        name="headphone_out_presence",
        model="S27BG400",
        product_name="삼성 오디세이 G4 S27BG400 68.4cm(27인치)",
        question="헤드폰 단자 있나요?",
        requested_information="헤드폰 출력 단자 지원 여부",
        retrieval_queries=("이어폰 단자",),
        field="headphone_out",
        value="있음",
    ),
    Case(
        name="swivel_range_repaired",
        model="S27BG400",
        product_name="삼성 오디세이 G4 S27BG400 68.4cm(27인치)",
        question="스위블 각도가 어떻게 되나요?",
        requested_information="좌우 회전 각도",
        retrieval_queries=("스위블 범위",),
        field="swivel_range_spec",
        value="좌, 우: 15.0°",
    ),
)


@pytest.mark.parametrize("case", ENUM_CASES, ids=[c.name for c in ENUM_CASES])
def test_an_enumeration_field_reaches_the_answer_the_customer_reads(
    tmp_path, monkeypatch, case: Case,
) -> None:
    """The same whole path as the 2-b cases above, for a 2-c field.

    ``swivel_range_repaired`` is the one that was not merely missing before:
    "스위블(좌우): 좌,우: 45.0°" was read as the single word "좌", so the
    attribute was answered and the angle -- all that was asked -- was gone.
    """

    run = _run(tmp_path, monkeypatch, case)
    metadata = run.metadata

    catalog_facts = [
        item for item in (metadata.get("product_catalog") or [])
        if isinstance(item, dict)
    ]
    values = [
        str(item.get("value")) for item in catalog_facts
        if str(item.get("field_key")) == case.field
    ]
    assert values == [case.value], {
        "field": case.field, "found": values,
        "fields": sorted({str(i.get("field_key")) for i in catalog_facts}),
    }
    assert case.field in list(
        (metadata.get("hybrid") or {}).get("product_fact_fields") or ())

    assert "PRODUCT_CATALOG_JSON" in run.prompt
    catalog = run.context.get("product_catalog") or {}
    assert case.value in str(catalog.get("instructions") or "")

    hybrid = metadata.get("hybrid") or {}
    draft = hybrid.get("draft") or {}
    assert case.field in list(draft.get("used_product_facts") or ())
    assert hybrid.get("product_catalog_in_prompt") is True
    assert not list(hybrid.get("used_product_facts_unsupported") or ())
    assert not list(hybrid.get("product_claims_without_citation") or ())
    assert not list(hybrid.get("product_claims_without_evidence") or ())

    validation = hybrid.get("validation") or {}
    assert validation.get("passed") is True, validation
    assert case.value in run.result.answer


# ---------------------------------------------------------------------------
# A conditionless figure is not evidence for a condition
# ---------------------------------------------------------------------------
# ``power_consumption_labelled`` holds what the source stated with no
# condition: 7 models write "25W" and nothing else. Its own wording is
# "소비전력", and "일반 소비전력" and "최대 소비전력" both contain it, so a
# question about one condition used to be handed the conditionless figure --
# measured on S27BG400, a bare 48W answered "일반 소비전력은 48W입니다" and
# auto-posted. The registry says which wording takes an attribute out of a
# question, and now says it on the answer side too.
CONDITIONLESS_POWER = (
    Case(
        name="power_generic_is_answerable",
        model="S24D300",
        product_name="삼성 에센셜 모니터 S24D300 60.4cm(24인치)",
        question="소비전력이 몇 W인가요?",
        requested_information="소비전력",
        retrieval_queries=("소비전력",),
        field="power_consumption_labelled",
        value="25W",
        posts=True,
    ),
    Case(
        name="power_typical_is_not_asserted",
        model="S24D300",
        product_name="삼성 에센셜 모니터 S24D300 60.4cm(24인치)",
        question="일반 소비전력이 몇 W인가요?",
        requested_information="일반 소비전력",
        retrieval_queries=("표준 소비전력",),
        field="power_typical",
        value="",
        posts=False,
    ),
    Case(
        name="power_max_is_not_asserted",
        model="S24D300",
        product_name="삼성 에센셜 모니터 S24D300 60.4cm(24인치)",
        question="최대 소비전력이 몇 W인가요?",
        requested_information="최대 소비전력",
        retrieval_queries=("max 소비전력",),
        field="power_max",
        value="",
        posts=False,
    ),
    Case(
        name="power_typical_stated_by_the_source",
        model="S22D300",
        product_name="삼성 에센셜 모니터 S22D300 54.6cm(22인치)",
        question="일반 소비전력이 몇 W인가요?",
        requested_information="일반 소비전력",
        retrieval_queries=("표준 소비전력",),
        field="power_typical",
        value="17W",
        posts=True,
    ),
    Case(
        name="power_max_stated_by_the_source",
        model="S22D300",
        product_name="삼성 에센셜 모니터 S22D300 54.6cm(22인치)",
        question="최대 소비전력이 몇 W인가요?",
        requested_information="최대 소비전력",
        retrieval_queries=("max 소비전력",),
        field="power_max",
        value="25W",
        posts=True,
    ),
)


@pytest.mark.parametrize("case", CONDITIONLESS_POWER,
                         ids=[c.name for c in CONDITIONLESS_POWER])
def test_a_condition_is_answered_only_by_the_field_that_states_it(
    tmp_path, monkeypatch, case: Case,
) -> None:
    """The conditionless value answers the conditionless question, and no other.

    The draft provider reads the field out of the prompt it was given, so a
    case with no expected value has nothing to answer from: that is the point
    for the two S24D300 conditions, whose source states 25W without saying
    which draw it measured.
    """

    run = _run(tmp_path, monkeypatch, case)
    metadata = run.metadata
    catalog = run.context.get("product_catalog") or {}
    delivered = {
        str(item.get("field_key") or item.get("field") or ""): str(
            item.get("value"))
        for item in (catalog.get("facts") or [])
    }

    if case.value:
        assert delivered.get(case.field) == case.value, delivered
    else:
        # The condition the question named has no value in this record. The
        # conditionless figure is still in the prompt -- a specification
        # question carries the affordable part of the whole record, which is
        # why routing alone could not settle this -- so what has to hold is
        # that the condition is not answered from it.
        assert case.field not in delivered, delivered
        assert delivered.get("power_consumption_labelled") == "25W", delivered

    guard = metadata.get("product_fact_guard") or {}
    if case.posts:
        assert case.value in run.result.answer
        assert run.result.auto_answerable is True
        assert guard.get("auto_post_allowed") is True, guard
        assert ((metadata.get("hybrid") or {}).get("validation")
                or {}).get("passed") is True
    else:
        # Nothing was answered from the conditionless figure, and the
        # condition was not settled from anything else either, so the
        # inquiry is staff's.
        assert "25W" not in run.result.answer, run.result.answer
        assert run.result.auto_answerable is False
        assert run.result.needs_review is True


def test_citing_a_conditionless_figure_for_a_condition_is_blocked(
    tmp_path, monkeypatch,
) -> None:
    """S27BG400's bare 48W, cited and stated as the typical draw.

    This is the reported defect, at the only place it can be judged: the
    answer the customer would have read. The conditioned question no longer
    asks for the conditionless field, so the name GPT ② reports was never
    delivered -- and whichever way the draft gets there, the gate holds.
    """

    case = Case(
        name="power_conditionless_cited",
        model="S27BG400",
        product_name="삼성 오디세이 G4 S27BG400 68.4cm(27인치)",
        question="일반 소비전력이 몇 W인가요?",
        requested_information="일반 소비전력",
        retrieval_queries=("표준 소비전력",),
        field="power_consumption_labelled",
        value="",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda c: _InventingProvider(
            c, ["power_consumption_labelled"], "일반 소비전력은 48W입니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    unsupported = guard.get("used_product_facts_unsupported") or []
    assert any(
        item.get("field_key") == "power_consumption_labelled"
        and item.get("reason") == "VALUE_ATTACHED_TO_ANOTHER_ATTRIBUTE"
        for item in unsupported), guard
    assert guard.get("auto_post_allowed") is False, guard
    assert guard.get("current_fact_verified") is False, guard
    assert run.result.auto_answerable is False
    assert run.result.needs_review is True
    assert "48W" not in run.result.answer or run.result.needs_review


def test_a_conditionless_figure_delivered_cannot_become_a_condition(
    tmp_path, monkeypatch,
) -> None:
    """The same defect's other door, and the reason the registry says it twice.

    Asked without a condition, the conditionless field *is* delivered -- that
    is what it is for. The draft may then write a condition the source never
    stated, and the figure is the one that was cited, so only the wording
    separates the two claims.
    """

    case = Case(
        name="power_conditionless_promoted",
        model="S24D300",
        product_name="삼성 에센셜 모니터 S24D300 60.4cm(24인치)",
        question="소비전력이 몇 W인가요?",
        requested_information="소비전력",
        retrieval_queries=("소비전력",),
        field="power_consumption_labelled",
        value="25W",
    )
    run = _run_with(
        tmp_path, monkeypatch, case,
        lambda c: _InventingProvider(
            c, ["power_consumption_labelled"], "일반 소비전력은 25W입니다."),
    )
    guard = run.metadata.get("product_fact_guard") or {}
    delivered = [
        str(item.get("field_key") or item.get("field") or "")
        for item in ((run.context.get("product_catalog") or {}).get("facts")
                     or [])
    ]
    assert "power_consumption_labelled" in delivered, delivered
    assert guard.get("used_product_facts_delivered") == [
        "power_consumption_labelled"], guard
    unsupported = guard.get("used_product_facts_unsupported") or []
    assert any(
        item.get("field_key") == "power_consumption_labelled"
        and item.get("reason") == "VALUE_ATTACHED_TO_ANOTHER_ATTRIBUTE"
        for item in unsupported), guard
    assert guard.get("auto_post_allowed") is False, guard
    assert run.result.auto_answerable is False
    assert run.result.needs_review is True


def test_the_wording_of_a_condition_asks_only_for_that_condition() -> None:
    """Routing, at the level it is decided.

    ``not_when`` is how the registry already says that a wording takes an
    attribute out of a question. The conditionless field's own wording is a
    substring of every condition's, so without it a conditioned question
    asked for the conditionless figure as well -- and a field that was asked
    for is ranked as what the question wants.
    """

    from services.product_knowledge_service import fields_for_question

    def power(question: str) -> list[str]:
        fields, _topics = fields_for_question(question)
        return [field for field in fields
                if field in {"power_consumption_labelled", "power_typical",
                             "power_max", "power_standby"}]

    assert power("소비전력이 몇 W인가요?") == [
        "power_consumption_labelled", "power_typical", "power_max"]
    assert power("전기 사용량이 얼마인가요?") == [
        "power_consumption_labelled", "power_typical", "power_max"]
    assert power("일반 소비전력이 몇 W인가요?") == ["power_typical"]
    assert power("표준소비전력 알려주세요") == ["power_typical"]
    assert power("최대 소비전력이 몇 W인가요?") == ["power_max"]
    assert power("대기전력이 몇 W인가요?") == ["power_standby"]
