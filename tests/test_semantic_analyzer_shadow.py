"""Understanding the question before choosing an answer for it.

"고장난 기존 tv 수거 요청드려요" was auto-posted with the Samsung service-centre
number. The classifier matches anchors, ``WARRANTY_AS`` anchors on 고장, and
there is no anchor for collection at all -- so a request to take an old
television away became an A/S question. Semantic coverage recorded it as
answered, because the answer anchors on 고장 too.

No further anchor fixes that. 고장 is the *object's state* and 수거 is the
*action*, and a model made only of topics has nowhere to keep them apart. These
tests cover the structure that does: a closed action vocabulary, object states
that can never be actions, and atomic questions that keep their own action.

Two things this must not become. It must not send every inquiry to a model --
measured on the live store, 88.6% never reach a trigger and keep the
deterministic path they already had. And it must not be able to publish
anything: the analyzer produces an understanding, and the validator, coverage,
eligibility and auto-post gates decide, exactly as before. Every failure mode
here -- timeout, transport error, malformed JSON, an invented action, a model
that is unsure -- returns a value marked unusable and the pipeline carries on
down the path it would have taken anyway.
"""
from __future__ import annotations

import json

import pytest

from answer.source_adapter import answer_request_from_inquiry
from services.gpt_semantic_analyzer_service import (
    PROMPT_BUDGET,
    GptSemanticAnalyzerService,
    shadow_record,
)
from services.inquiry_analysis_service import InquiryAnalysisService
from services.semantic_analysis import (
    ACTIONS,
    OBJECT_STATES,
    SemanticAnalysisError,
    TRIGGER_DEADLINE,
    TRIGGER_SEMANTIC_FIRST,
    TRIGGER_STATE_ACTION_CONFLICT,
    TRIGGER_UNCLASSIFIED,
    parse,
    route,
    unavailable,
)


PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
ORDER = "2026082198559811"


def analysis_for(question: str, *, order_id: str | None = None) -> dict:
    service = InquiryAnalysisService()
    request = answer_request_from_inquiry({
        "title": "문의", "content": question, "product_name": PRODUCT,
        "product_id": "12139453925", "inquiry_type": "PRODUCT_INQUIRY",
        "order_id": order_id,
    })
    return service.analyze(request).to_dict()


def semantic_payload(**overrides) -> dict:
    payload = {
        "primary_action": "COLLECTION",
        "secondary_actions": [],
        "request_type": "ACTION_REQUEST",
        "objects": [{"type": "TV", "states": ["BROKEN", "EXISTING"]}],
        "atomic_questions": [
            {"text": "기존 TV 수거 요청", "action": "COLLECTION"},
        ],
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": False,
        "requires_delivery_schedule": False,
        "confidence": 0.95,
    }
    payload.update(overrides)
    return payload


class ScriptedProvider:
    """Returns prepared payloads, or raises what a real transport raises."""

    name = "scripted"

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.tasks: list[str] = []

    def generate_json(self, *, task, prompt, context):
        self.tasks.append(str(task))
        self.prompts.append(str(prompt))
        value = (
            self.responses.pop(0) if self.responses
            else semantic_payload()
        )
        if isinstance(value, Exception):
            raise value
        return value


# ==========================================================================
# 1. The action is what the customer wants done, not a word about the object
# ==========================================================================


def test_a_broken_television_may_still_be_a_collection_request() -> None:
    """CASE 1. The state is BROKEN and the action is COLLECTION."""

    service = GptSemanticAnalyzerService(ScriptedProvider(semantic_payload()))

    result = service.analyze("고장난 기존 tv 수거 요청드려요")

    assert result.usable
    assert result.primary_action == "COLLECTION"
    assert result.primary_action != "REPAIR"
    assert "BROKEN" in result.objects[0].states
    assert result.objects[0].states != ()


def test_the_same_object_state_with_a_repair_action() -> None:
    """CASE 2. Only the action differs, and it is the action that routes."""

    service = GptSemanticAnalyzerService(ScriptedProvider(semantic_payload(
        primary_action="REPAIR",
        atomic_questions=[{"text": "TV 수리 요청", "action": "REPAIR"}],
    )))

    result = service.analyze("고장난 TV 수리해주세요")

    assert result.primary_action == "REPAIR"
    assert "BROKEN" in result.objects[0].states


def test_a_qualifier_does_not_displace_the_action() -> None:
    """CASE 11. 고장난 is context for the object being collected."""

    service = GptSemanticAnalyzerService(ScriptedProvider(semantic_payload(
        request_type="QUESTION",
        objects=[
            {"type": "TV", "states": ["BROKEN", "EXISTING"]},
            {"type": "TV", "states": ["NEW"]},
        ],
    )))

    result = service.analyze("고장난 TV인데 새 제품 설치하면서 가져가실 수 있나요?")

    assert result.primary_action == "COLLECTION"
    assert {"BROKEN", "EXISTING"} <= set(result.objects[0].states)


def test_a_compound_inquiry_keeps_both_questions(
) -> None:
    """CASE 10. Delivery and collection are two questions, not one topic."""

    service = GptSemanticAnalyzerService(ScriptedProvider(semantic_payload(
        primary_action="DELIVERY_STATUS",
        secondary_actions=["COLLECTION"],
        request_type="MIXED",
        atomic_questions=[
            {"text": "배송은 언제 되나요", "action": "DELIVERY_STATUS"},
            {"text": "기존 TV도 가져가주시나요", "action": "COLLECTION"},
        ],
        requires_delivery_schedule=True,
    )))

    result = service.analyze("배송은 언제 되고 기존 TV도 가져가주시나요?")

    assert [item.action for item in result.atomic_questions] == [
        "DELIVERY_STATUS", "COLLECTION",
    ]
    assert result.actions == {"DELIVERY_STATUS", "COLLECTION"}


def test_a_deadline_is_carried_as_a_constraint() -> None:
    """CASE 4. The date the customer needs is part of the question."""

    service = GptSemanticAnalyzerService(ScriptedProvider(semantic_payload(
        primary_action="DELIVERY_DEADLINE_CONFIRMATION",
        request_type="QUESTION",
        objects=[],
        atomic_questions=[
            {"text": "9일까지 받을 수 있나요",
             "action": "DELIVERY_DEADLINE_CONFIRMATION"},
        ],
        deadline="9일",
        conditional=True,
        requires_delivery_schedule=True,
    )))

    result = service.analyze("오늘 주문하면 9일까지 받을 수 있나요?")

    assert result.primary_action == "DELIVERY_DEADLINE_CONFIRMATION"
    assert result.deadline == "9일"
    assert result.requires_delivery_schedule is True


# ==========================================================================
# 2. Which inquiries earn a call, and which never do
# ==========================================================================


@pytest.mark.parametrize("question,expected_trigger", [
    ("고장난 기존 tv 수거 요청드려요", TRIGGER_STATE_ACTION_CONFLICT),
    ("고장난 TV 수거해주세요", TRIGGER_STATE_ACTION_CONFLICT),
    ("고장난 TV 수리해주세요", TRIGGER_STATE_ACTION_CONFLICT),
    ("기존 TV 가져가주시나요?", TRIGGER_STATE_ACTION_CONFLICT),
    ("고장난 TV인데 새 제품 설치하면서 가져가실 수 있나요?",
     TRIGGER_STATE_ACTION_CONFLICT),
    ("오늘 주문하면 9일까지 받을 수 있나요?", TRIGGER_DEADLINE),
])
def test_an_unclear_action_earns_a_semantic_call(question, expected_trigger):
    decision = route(question, analysis=analysis_for(question))

    assert decision.use_semantic is True
    assert expected_trigger in decision.reasons


@pytest.mark.parametrize("question,order_id", [
    ("언제 배송되나요?", ORDER),                 # CASE 5
    ("토요일에도 배송하나요?", None),             # CASE 6
    ("이번 토요일에 설치해주세요", ORDER),        # CASE 7
    ("벽걸이 설치 가능한가요?", None),            # CASE 8
    ("HDMI 단자 몇 개인가요?", None),             # CASE 9
    ("배송은 보통 며칠 걸리나요?", None),         # CASE 12
    ("설치 일정은 어떻게 안내받나요?", None),
    ("A/S는 어디서 받나요?", None),
])
def test_every_nonempty_inquiry_uses_semantic_first(question, order_id):
    """Deterministic analysis is fallback, never a keyword-first bypass."""

    decision = route(question, analysis=analysis_for(question, order_id=order_id))

    assert decision.use_semantic is True
    assert TRIGGER_SEMANTIC_FIRST in decision.reasons


def test_an_unclassified_request_earns_a_call() -> None:
    question = "기존에 쓰던 거 처분 좀 부탁드려요"
    decision = route(question, analysis=analysis_for(question))

    assert decision.use_semantic is True
    assert TRIGGER_UNCLASSIFIED in decision.reasons


def test_the_router_never_calls_anything() -> None:
    """The decision is made from text and existing analysis, at no cost."""

    class Exploding:
        name = "exploding"

        def generate_json(self, *, task, prompt, context):
            raise AssertionError("route() must not reach a provider")

    GptSemanticAnalyzerService(Exploding())
    assert route("배송은 보통 며칠 걸리나요?", analysis={}).use_semantic is True


# ==========================================================================
# 3. Failure is ordinary: every fault falls back, nothing blocks
# ==========================================================================


def test_a_timeout_falls_back_without_raising() -> None:
    """CASE 13."""

    service = GptSemanticAnalyzerService(
        ScriptedProvider(TimeoutError("read timed out"))
    )

    result = service.analyze("고장난 TV 수거해주세요")

    assert result.usable is False
    assert result.source == "UNAVAILABLE"
    assert "PROVIDER_ERROR" in result.reason
    assert service.last_trace["outcome"] == "PROVIDER_ERROR"


@pytest.mark.parametrize("payload,expected", [
    ("not a dict", "INVALID_OUTPUT"),
    ({"primary_action": "TAKE_IT_AWAY", "confidence": 0.9}, "INVALID_OUTPUT"),
    ({"primary_action": "COLLECTION", "confidence": "high"}, "INVALID_OUTPUT"),
    ({"primary_action": "COLLECTION", "confidence": 1.4}, "INVALID_OUTPUT"),
    ({"primary_action": "COLLECTION", "confidence": 0.9,
      "atomic_questions": [{"text": "x", "action": "NOPE"}]}, "INVALID_OUTPUT"),
    ({"primary_action": "COLLECTION", "confidence": 0.9,
      "secondary_actions": "COLLECTION"}, "INVALID_OUTPUT"),
])
def test_invalid_structured_output_falls_back(payload, expected) -> None:
    """CASE 14. An invented action is refused, not routed on."""

    service = GptSemanticAnalyzerService(ScriptedProvider(payload))

    result = service.analyze("고장난 TV 수거해주세요")

    assert result.usable is False
    assert service.last_trace["outcome"] == expected


def test_a_model_that_is_unsure_is_not_acted_on() -> None:
    service = GptSemanticAnalyzerService(
        ScriptedProvider(semantic_payload(confidence=0.42))
    )

    result = service.analyze("고장난 TV 수거해주세요")

    assert result.usable is False
    assert service.last_trace["outcome"] == "LOW_CONFIDENCE"


def test_an_unknown_action_never_becomes_a_route() -> None:
    with pytest.raises(SemanticAnalysisError):
        parse(semantic_payload(primary_action="ARRANGE_HELICOPTER"))


def test_a_state_is_never_promoted_to_an_action() -> None:
    assert not (OBJECT_STATES & ACTIONS)
    result = parse(semantic_payload(
        objects=[{"type": "TV", "states": ["BROKEN", "COLLECTION"]}],
    ))
    assert result.objects[0].states == ("BROKEN",)


def test_a_fallback_is_never_mistaken_for_an_understanding() -> None:
    value = unavailable("PROVIDER_ERROR:TimeoutError")

    assert value.usable is False
    assert value.primary_action == "OTHER"
    assert value.to_dict()["source"] == "UNAVAILABLE"


# ==========================================================================
# 4. Cost: the same question is not paid for twice
# ==========================================================================


def test_the_same_question_is_analysed_once() -> None:
    """CASE 15. A rerun, a regeneration and a retry are one question."""

    provider = ScriptedProvider(semantic_payload(), semantic_payload())
    service = GptSemanticAnalyzerService(provider)

    first = service.analyze("고장난 기존 tv 수거 요청드려요")
    second = service.analyze("고장난 기존 tv  수거 요청드려요")

    assert first == second
    assert service.call_count == 1
    assert service.cache_hits == 1
    assert len(provider.prompts) == 1
    assert service.last_trace["cache_hit"] is True
    assert service.last_trace["latency_ms"] == 0.0


def test_a_failure_is_never_cached() -> None:
    """One timeout must not become a permanent verdict on that question."""

    provider = ScriptedProvider(TimeoutError("t"), semantic_payload())
    service = GptSemanticAnalyzerService(provider)

    assert service.analyze("고장난 TV 수거해주세요").usable is False
    assert service.analyze("고장난 TV 수거해주세요").usable is True
    assert service.call_count == 2


def test_the_prompt_carries_no_prose_budget() -> None:
    """Every token is paid on every semantic call."""

    service = GptSemanticAnalyzerService(ScriptedProvider())
    prompt = service.build_prompt("고장난 기존 tv 수거 요청드려요")

    # See PROMPT_BUDGET: the ceiling is what the field contract costs, and the
    # paragraph count is what stops prose from returning under it.
    assert len(prompt) < PROMPT_BUDGET
    assert "COLLECTION" in prompt
    assert prompt.count("\n\n") <= 4


def test_the_task_is_its_own_kind() -> None:
    provider = ScriptedProvider(semantic_payload())
    GptSemanticAnalyzerService(provider).analyze("고장난 TV 수거해주세요")

    assert provider.tasks == ["SEMANTIC_ANALYSIS"]


# ==========================================================================
# 5. Shadow record: comparable, and carrying no authority
# ==========================================================================


def test_the_shadow_record_compares_both_sides() -> None:
    question = "고장난 기존 tv 수거 요청드려요"
    existing = analysis_for(question)
    decision = route(question, analysis=existing)
    service = GptSemanticAnalyzerService(ScriptedProvider(semantic_payload()))
    semantic = service.analyze(question)

    record = shadow_record(
        question=question, decision=decision.to_dict(), analysis=existing,
        semantic=semantic, trace=service.last_trace,
    )

    assert record["semantic_action"] == "COLLECTION"
    assert record["existing_intent"] == "GENERAL"
    assert record["agreement"] is False
    assert record["semantic_used"] is True
    assert TRIGGER_STATE_ACTION_CONFLICT in record["semantic_trigger_reasons"]
    # Serialisable into the draft metadata that already exists -- no new table.
    assert json.loads(json.dumps(record, ensure_ascii=False))


def test_the_shadow_record_marks_a_fallback_as_such() -> None:
    question = "고장난 TV 수거해주세요"
    service = GptSemanticAnalyzerService(ScriptedProvider(TimeoutError("t")))
    semantic = service.analyze(question)

    record = shadow_record(
        question=question, decision={"use_semantic": True, "reasons": []},
        analysis=analysis_for(question), semantic=semantic,
        trace=service.last_trace,
    )

    assert record["semantic_action"] is None
    assert record["agreement"] is None
    assert record["semantic_source"] == "UNAVAILABLE"


def test_the_analyzer_holds_no_publishing_authority() -> None:
    """Nothing it returns is a decision about the customer's answer."""

    result = parse(semantic_payload(confidence=1.0))
    fields = set(result.to_dict())

    assert not fields & {
        "auto_post", "safe", "eligibility", "decision", "publish",
        "validation_status", "review_status",
    }
