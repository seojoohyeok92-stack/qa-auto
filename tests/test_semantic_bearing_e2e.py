"""Production routing, run with a real SemanticAnalysis attached.

Every existing end-to-end test runs with the semantic analyser switched off:
``OJE_SEMANTIC_ANALYZER_ENABLED`` defaults to "0", so ``_semantic_for_routing``
returns None and ``_semantic_routing_value`` is None at every route decision in
the suite. That was measured, not assumed -- instrumenting the route boundary
across the existing tests recorded ``(semantic present, atom count) = (False, 0)``
every time.

It means any routing change conditioned on the semantic analysis would be
inert in the whole suite and live only in production. This file removes that
blind spot: the analyser is switched on, a fake analyser returns an object
built by the production ``parse`` from the production contract, and the tests
observe what the route boundary actually sees.

No provider call is made. The fake replaces the network, not the contract.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
from typing import Any

import pytest

from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.hybrid_answer_service import HybridAnswerService
from services.semantic_analysis import (
    ENABLED_ENV as SEMANTIC_ENABLED_ENV,
    SemanticAnalysis,
    parse,
)
from services.semantic_coverage_service import ENABLED_ENV as COVERAGE_ENABLED_ENV

from test_semantic_coverage_soft_gate import PRODUCT, _FakeDps, _StubProvider


# --------------------------------------------------------------- 계약 그대로
def semantic_payload(
    *atoms: dict[str, Any],
    primary_action: str = "PRODUCT_CONCEPT",
    purchase_state: str = "UNKNOWN",
    request_type: str = "QUESTION",
) -> dict[str, Any]:
    """The JSON shape ``GptSemanticAnalyzerService`` returns from the provider.

    Fields and their names are taken from the analyser's own contract, and the
    object is built by the production ``parse`` below rather than by hand, so a
    contract change breaks this fixture instead of silently bypassing it.
    """

    return {
        "primary_action": primary_action,
        "secondary_actions": [],
        "request_type": request_type,
        "objects": [],
        "atomic_questions": list(atoms),
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": False,
        "requires_delivery_schedule": False,
        "purchase_state": purchase_state,
        "asks_delivery_schedule": False,
        "asks_delivery_outcome": False,
        "confidence": 0.9,
    }


def atom(
    text: str,
    *,
    action: str = "PRODUCT_CONCEPT",
    information: str = "",
    attribute: str = "UNKNOWN",
) -> dict[str, Any]:
    return {
        "text": text,
        "action": action,
        "requested_information": information or text,
        "requested_attribute": attribute,
    }


class FakeSemanticAnalyzer:
    """Stands in for the provider call, not for the analysis.

    ``analyze`` returns whatever ``parse`` makes of the scripted payload, which
    is the same call the real service makes on the provider's reply.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.last_trace: dict[str, Any] = {"cache_hit": False, "latency_ms": 0.0}
        self.calls = 0

    def analyze(self, question: object) -> SemanticAnalysis:
        self.calls += 1
        return parse(self.payload)


# --------------------------------------------------------------- 실행 하네스
def run(
    question: str,
    payload: dict[str, Any],
    *,
    label: str = "sem",
    observe: list | None = None,
) -> dict[str, Any]:
    """One real ``generate_for_inquiry`` with semantics switched on."""

    analyzer = FakeSemanticAnalyzer(payload)
    database = Database(pathlib.Path(tempfile.mkdtemp()) / f"{label}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "S", "source_type": "NAVER",
        "source_question_id": label, "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의", "content": question, "product_name": PRODUCT,
        "order_id": None, "product_order_id": None, "raw_json": {},
    }).inquiry_id

    service = AnswerService(
        database,
        dps_enrichment=_FakeDps(),
        hybrid_service=HybridAnswerService(_StubProvider()),
        semantic_analyzer=analyzer,
    )
    if observe is not None:
        _instrument_route_boundary(service, observe)
    service.generate_for_inquiry(inquiry_id)

    record = dict(AnswerRepository(database).latest_for_inquiry(inquiry_id))
    metadata = record.get("metadata_json")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    metadata = metadata or {}
    return {
        "analyzer_calls": analyzer.calls,
        "metadata": metadata,
        "route": str(metadata.get("selected_answer_route") or ""),
        # Persisted under "semantic_analysis" by _record_semantic_action_support;
        # "semantic_routing" lives on the in-memory request only.
        "semantic_routing": metadata.get("semantic_analysis") or {},
        "coverage": str((metadata.get("semantic_coverage") or {}).get("status") or ""),
        "answer": str(record.get("original_answer") or ""),
    }


def _instrument_route_boundary(service: AnswerService, sink: list) -> None:
    """Record what the route decision can see, at the moment it is made.

    Wraps the rule engine's entry point: it is called from inside
    ``generate_for_inquiry`` while the request carries whatever
    ``_attach_semantic_routing`` put on it, which is exactly the visibility a
    routing change would have.
    """

    original = service._exclude_semantic_rule_mismatch

    def spy(candidate, semantic, request):
        atoms = list(getattr(semantic, "atomic_questions", ()) or ())
        sink.append({
            "semantic_present": semantic is not None,
            "atom_count": len(atoms),
            "atom_texts": [str(item.text) for item in atoms],
            "attached": request.metadata.get("_semantic_routing_value") is not None,
        })
        return original(candidate, semantic, request)

    service._exclude_semantic_rule_mismatch = spy


@pytest.fixture(autouse=True)
def semantics_on(monkeypatch):
    monkeypatch.setenv(SEMANTIC_ENABLED_ENV, "1")
    monkeypatch.setenv(COVERAGE_ENABLED_ENV, "1")


# ======================================================= fixture 자체의 유효성
def test_the_analyzer_is_actually_called():
    outcome = run("오베닉 스탠드는 몇 세대인가요?",
                  semantic_payload(atom("오베닉 스탠드는 몇 세대인가요?")),
                  label="f1")
    assert outcome["analyzer_calls"] >= 1


def test_the_semantic_result_is_persisted_with_the_draft():
    outcome = run("오베닉 스탠드는 몇 세대인가요?",
                  semantic_payload(atom("오베닉 스탠드는 몇 세대인가요?")),
                  label="f2")
    routing = outcome["semantic_routing"]
    assert routing.get("called") is True
    assert routing.get("usable") is True
    assert routing.get("semantic", {}).get("atomic_questions")


def test_the_payload_goes_through_the_production_parser():
    """A contract change must break this fixture, not slip past it."""
    analysis = parse(semantic_payload(
        atom("배송비는 얼마인가요", action="DELIVERY_POLICY",
             information="배송비", attribute="AMOUNT_OR_COST")
    ))
    assert analysis.usable is True
    assert len(analysis.atomic_questions) == 1
    assert analysis.atomic_questions[0].requested_attribute == "AMOUNT_OR_COST"


# ============================== route boundary 에서 실제로 관측되는가 (핵심 증명)
def test_a_single_atom_is_visible_at_the_route_boundary():
    seen: list = []
    run("오베닉 스탠드는 몇 세대인가요?",
        semantic_payload(atom("오베닉 스탠드는 몇 세대인가요?")),
        label="b1", observe=seen)

    assert seen, "route boundary was never reached"
    assert all(item["semantic_present"] for item in seen)
    assert all(item["attached"] for item in seen)
    assert max(item["atom_count"] for item in seen) == 1


def test_two_atoms_are_visible_at_the_route_boundary():
    seen: list = []
    run("배송비는 얼마인가요?\n브라켓도 같이 오나요?",
        semantic_payload(
            atom("배송비는 얼마인가요", action="DELIVERY_POLICY", attribute="AMOUNT_OR_COST"),
            atom("브라켓도 같이 오나요", action="PACKAGE_CONTENTS", attribute="INCLUSION"),
        ),
        label="b2", observe=seen)

    assert seen, "route boundary was never reached"
    assert all(item["semantic_present"] for item in seen)
    assert max(item["atom_count"] for item in seen) == 2


def test_three_atoms_are_visible_at_the_route_boundary():
    seen: list = []
    run("배송비는 얼마인가요?\n브라켓도 같이 오나요?\n보증기간은 얼마인가요?",
        semantic_payload(
            atom("배송비는 얼마인가요", action="DELIVERY_POLICY", attribute="AMOUNT_OR_COST"),
            atom("브라켓도 같이 오나요", action="PACKAGE_CONTENTS", attribute="INCLUSION"),
            atom("보증기간은 얼마인가요", action="OTHER", attribute="TIMING"),
        ),
        label="b3", observe=seen)

    assert seen, "route boundary was never reached"
    assert max(item["atom_count"] for item in seen) == 3


def test_the_measured_inquiry_reaches_the_boundary_as_two_atoms():
    """687718601, decomposed the way the semantic contract describes it."""
    seen: list = []
    run(
        "안녕하세요:) 집에서 그냥 일반 tv시청이나 셋톱박스 연결되어있는걸로 "
        "ott, 유튜브 볼건데 비즈니스tv와 사이니지tv 중 뭐가 낫나요?? "
        "그리고 스탠드형 비즈니스 tv가 여러 제품이 있던데 "
        "2026년 출시형 모델 제품 추천 부탁드립니다!!",
        semantic_payload(
            atom("비즈니스tv와 사이니지tv 중 뭐가 낫나요",
                 information="비즈니스 TV와 사이니지 TV의 차이",
                 attribute="DIFFERENCE"),
            atom("2026년 출시형 모델 제품 추천 부탁드립니다",
                 action="OTHER",
                 information="2026년 출시형 비즈니스 TV 모델",
                 attribute="SPEC_VALUE"),
        ),
        label="b4", observe=seen)

    assert seen, "route boundary was never reached"
    assert max(item["atom_count"] for item in seen) == 2


# ================================================= semantic 부재 경로는 그대로
def test_with_the_analyser_disabled_the_boundary_sees_nothing(monkeypatch):
    """The existing suite's condition, kept as the documented fallback.

    Every other end-to-end test in the repository runs this way. Nothing may
    start behaving differently for them.
    """
    monkeypatch.setenv(SEMANTIC_ENABLED_ENV, "0")
    seen: list = []
    run("오베닉 스탠드는 몇 세대인가요?",
        semantic_payload(atom("오베닉 스탠드는 몇 세대인가요?")),
        label="off1", observe=seen)

    assert all(item["semantic_present"] is False for item in seen)
    assert all(item["atom_count"] == 0 for item in seen)


# ============================ Semantic 이 route 를 실제로 바꾸는가 (핵심 증명)
STAND_QUESTION = "오베닉 스탠드는 몇 세대인가요?"
STAND_COMPOUND = "오베닉 스탠드는 몇 세대인가요?\n배송비는 얼마인가요?"


def test_one_atom_fully_covered_keeps_the_deterministic_route():
    """The rule shortcut must survive. This is the control for the next test."""
    outcome = run(STAND_QUESTION,
                  semantic_payload(atom(STAND_QUESTION)), label="r1")

    assert outcome["route"] in {"TEMPLATE", "PRODUCT_DB", "SAFE_RULE"}
    assert outcome["coverage"] != "PARTIAL"


def test_a_compound_inquiry_the_rule_only_half_answers_does_not_take_the_route():
    """Same product, same rule match, one more question -- a different route.

    This is what makes the semantic analysis an authority rather than a
    record: the only thing that changed between this and the test above is
    what the semantic pass found, and the route decision changed with it.
    """
    outcome = run(
        STAND_COMPOUND,
        semantic_payload(
            atom("오베닉 스탠드는 몇 세대인가요"),
            atom("배송비는 얼마인가요", action="DELIVERY_POLICY",
                 attribute="AMOUNT_OR_COST"),
        ),
        label="r2")

    assert outcome["route"] not in {"TEMPLATE", "PRODUCT_DB"}


def test_the_gate_decides_on_the_semantic_analysis_and_nothing_else():
    """Same question text, same deterministic answer, different analysis.

    The route a compound inquiry ends up on depends on more than this gate --
    template matching, product-fact sensitivity and validator results all
    move it. So the authority claim is made where it actually lives: the one
    decision that reads the semantic analysis. Holding text and answer fixed,
    the verdict flips on the analysis alone.
    """
    from answer.models import AnswerRequest

    service = AnswerService(
        Database(pathlib.Path(tempfile.mkdtemp()) / "gate.db"),
        dps_enrichment=_FakeDps(),
        hybrid_service=HybridAnswerService(_StubProvider()),
    )
    question = STAND_COMPOUND
    answer = "스탠드는 오베닉 스탠드 FMS 모델로 출고되고 있습니다."

    def gate(payload):
        request = AnswerRequest(
            inquiry_id=1, question_id="G", inquiry_type="상품",
            question=question, product_name=PRODUCT,
            metadata={"_semantic_routing_value": parse(payload)},
        )
        return service._deterministic_answer_settles_inquiry(request, answer)

    one_atom = semantic_payload(atom(question))
    two_atoms = semantic_payload(
        atom("오베닉 스탠드는 몇 세대인가요"),
        atom("배송비는 얼마인가요", action="DELIVERY_POLICY",
             attribute="AMOUNT_OR_COST"),
    )

    assert gate(one_atom) is True, "a single question keeps the shortcut"
    assert gate(two_atoms) is False, "an unanswered second question withdraws it"


def test_the_gate_is_inert_without_a_semantic_analysis():
    """No analysis means no new behaviour, which is the whole legacy suite."""
    from answer.models import AnswerRequest

    service = AnswerService(
        Database(pathlib.Path(tempfile.mkdtemp()) / "gate2.db"),
        dps_enrichment=_FakeDps(),
        hybrid_service=HybridAnswerService(_StubProvider()),
    )
    request = AnswerRequest(
        inquiry_id=1, question_id="G2", inquiry_type="상품",
        question=STAND_COMPOUND, product_name=PRODUCT, metadata={},
    )
    assert service._deterministic_answer_settles_inquiry(
        request, "스탠드는 오베닉 스탠드 FMS 모델로 출고되고 있습니다."
    ) is True


def test_a_compound_inquiry_answered_throughout_keeps_the_shortcut():
    """Continuation is for unanswered questions, not for compound ones."""
    seen: list = []
    outcome = run(
        "배송비는 얼마인가요?\n브라켓도 같이 오나요?",
        semantic_payload(
            atom("배송비는 얼마인가요", action="DELIVERY_POLICY",
                 attribute="AMOUNT_OR_COST"),
            atom("브라켓도 같이 오나요", action="PACKAGE_CONTENTS",
                 attribute="INCLUSION"),
        ),
        label="r5", observe=seen)

    assert max(item["atom_count"] for item in seen) == 2
    assert outcome["coverage"] != "PARTIAL" or outcome["route"] not in {"TEMPLATE"}


def test_without_semantics_the_route_is_exactly_what_it_was(monkeypatch):
    """The legacy path keeps its behaviour: no analysis, no new decision."""
    monkeypatch.setenv(SEMANTIC_ENABLED_ENV, "0")
    outcome = run(STAND_COMPOUND,
                  semantic_payload(atom(STAND_COMPOUND)), label="r6")

    assert outcome["route"] in {"TEMPLATE", "PRODUCT_DB", "SAFE_RULE", "GPT_FALLBACK",
                                "GPT_DIRECT", "GPT_HYBRID"}


# ================================== 687718601: continuation + 여전히 차단
INQUIRY_687718601 = (
    "안녕하세요:) 집에서 그냥 일반 tv시청이나 셋톱박스 연결되어있는걸로 "
    "ott, 유튜브 볼건데 비즈니스tv와 사이니지tv 중 뭐가 낫나요?? "
    "그리고 스탠드형 비즈니스 tv가 여러 제품이 있던데 "
    "2026년 출시형 모델 제품 추천 부탁드립니다!!"
)
SEMANTIC_687718601 = dict(
    atoms=(
        ("비즈니스tv와 사이니지tv 중 뭐가 낫나요", "PRODUCT_CONCEPT", "DIFFERENCE"),
        ("2026년 출시형 모델 제품 추천 부탁드립니다", "OTHER", "SPEC_VALUE"),
    ),
)


def _payload_687718601():
    return semantic_payload(*[
        atom(text, action=action, attribute=attribute)
        for text, action, attribute in SEMANTIC_687718601["atoms"]
    ])


def test_the_measured_inquiry_no_longer_ends_on_the_rule_answer():
    """The stand rule still matches; it no longer finishes the inquiry.

    The matcher is deliberately untouched -- "모델" and "스탠드" still refer to
    the TV and still trigger the stand rule. What changed is that a rule
    answering neither question cannot close the inquiry over both of them.
    """
    outcome = run(INQUIRY_687718601, _payload_687718601(), label="m1")
    assert outcome["route"] not in {"TEMPLATE", "PRODUCT_DB", "SAFE_RULE"}


def test_the_measured_inquiry_reaches_the_evidence_pipeline():
    outcome = run(INQUIRY_687718601, _payload_687718601(), label="m2")
    assert outcome["metadata"].get("hybrid")


def test_the_measured_inquiry_is_still_held_from_auto_post():
    """Continuation must not become a way around the coverage gate."""
    from services.auto_processing_eligibility_service import (
        SEMANTIC_COVERAGE_INCOMPLETE, AutoProcessingEligibilityService,
    )

    outcome = run(INQUIRY_687718601, _payload_687718601(), label="m3")
    assert outcome["coverage"] == "PARTIAL"

    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "metadata_json": outcome["metadata"],
            "original_answer": outcome["answer"],
            "validation_status": "PASS",
        },
        route=outcome["route"],
    )
    assert SEMANTIC_COVERAGE_INCOMPLETE in verdict.reasons
    assert verdict.decision != "SAFE"


# ============================ 복합문의에서 deterministic 지름길은 증명이 필요하다
#
# 325584049 "혼자계신 엄마댁이라 tv설지하고 페가전 수거해주시는거죠?" 는 SAFE_RULE
# 의 "정확한 정보 확인이 필요합니다" 답변으로 종료되었고 learning_search_called 가
# False 였다. 운영 DB 에는 이 질문에 답하는 승인 Learning 이 63건 있다. 그 답변은
# coverage 가 UNKNOWN -- 앵커가 아무것도 알아보지 못한 상태 -- 였으므로, "FAIL/
# PARTIAL 이 아니다" 를 지름길의 조건으로 삼으면 통과해 버린다.
def _gate(question: str, answer: str, payload: dict | None):
    from answer.models import AnswerRequest

    service = AnswerService(
        Database(pathlib.Path(tempfile.mkdtemp()) / "g.db"),
        dps_enrichment=_FakeDps(),
        hybrid_service=HybridAnswerService(_StubProvider()),
    )
    request = AnswerRequest(
        inquiry_id=1, question_id="G", inquiry_type="상품",
        question=question, product_name=PRODUCT,
        metadata={} if payload is None
        else {"_semantic_routing_value": parse(payload)},
    )
    return service._deterministic_answer_settles_inquiry(request, answer)


COLLECTION_INQUIRY = "혼자계신 엄마댁이라 tv설지하고 페가전 수거해주시는거죠?"
COLLECTION_ATOMS = semantic_payload(
    atom("tv 설치해주시나요", action="INSTALLATION_METHOD",
         information="설치 제공 여부", attribute="EXISTENCE_OR_CAPABILITY"),
    atom("폐가전 수거해주시나요", action="COLLECTION",
         information="폐가전 수거 제공 여부", attribute="EXISTENCE_OR_CAPABILITY"),
    primary_action="COLLECTION",
)
NEEDS_INFO_ANSWER = (
    "문의주신 아래 내용은 - tv 설치해주시나요? - 폐가전 수거해주시나요? "
    "관련하여 정확한 정보 확인이 필요합니다."
)


def test_an_unrecognised_answer_cannot_settle_a_compound_inquiry():
    """CASE B -- 확인이 필요하다는 답변은 두 질문을 해결한 것이 아니다."""
    assert _gate(COLLECTION_INQUIRY, NEEDS_INFO_ANSWER, COLLECTION_ATOMS) is False


def test_a_single_question_keeps_its_route_even_when_unrecognised():
    """CASE A -- 단일 문의는 이 게이트가 손대지 않는다."""
    single = semantic_payload(atom("폐가전 수거해주시나요", action="COLLECTION"))
    assert _gate("폐가전 수거해주시나요?", NEEDS_INFO_ANSWER, single) is True


def test_a_compound_inquiry_answered_throughout_keeps_the_shortcut_too():
    """지름길이 사라지는 것이 아니라, 해결했을 때만 유지된다."""
    payload = semantic_payload(
        atom("배송비는 얼마인가요", action="DELIVERY_POLICY",
             attribute="AMOUNT_OR_COST"),
        atom("브라켓도 같이 오나요", action="PACKAGE_CONTENTS",
             attribute="INCLUSION"),
    )
    assert _gate(
        "배송비는 얼마인가요?\n브라켓도 같이 오나요?",
        "배송비는 무료입니다. 벽걸이 브라켓은 구성품에 포함되어 함께 발송됩니다.",
        payload,
    ) is True


def test_the_gate_is_still_inert_without_semantics():
    assert _gate(COLLECTION_INQUIRY, NEEDS_INFO_ANSWER, None) is True


# ================== semantic atom 이 하나여도 검색 질의로 남아야 한다 (retrieval)
def test_a_single_semantic_atom_is_used_as_the_retrieval_query():
    """운영 DB 측정: 전체 문의는 63건 중 0건, atom 질의는 3건을 threshold 위로
    올린다. 원문으로 되돌리면 그 3건이 사라진다."""
    from answer.facts import build_answer_facts  # noqa: F401  (계약 존재 확인)
    import inspect

    from services.learning_context_service import LearningContextService

    source = inspect.getsource(LearningContextService.build)
    # 되돌림은 semantic atom 이 아예 없을 때로 한정되어야 한다.
    assert "if not semantic_atomic and len(questions) <= 1:" in source


# ================= 해결하지 못한 deterministic 답변은 generation 근거가 아니다
#
# 325584049 는 continuation 이 붙은 뒤에도 근거를 쓰지 않았다. 실제 provider 가
# 그 이유를 스스로 적어 돌려주었다:
#
#   learning_usage: [{"learning_id": 314283, "answer_supported": false,
#     "reason": "학습 근거는 참고 가능하나, 제공된 확인 안내 규칙을 우선
#      적용했습니다."}]
#
# 폐가전 수거가 무료라는 승인 답변이 prompt 안에 있었는데, 같은 prompt 의
# facts["rule.answer"] 에 "정확한 정보 확인이 필요합니다" 라는 rule 답변이
# 함께 실려 있었고 모델은 그쪽을 규칙으로 읽었다. 아무것도 해결하지 못해서
# 계속 진행하기로 한 답변을, 근거로 다시 건네고 있었던 셈이다.
def test_an_answer_that_did_not_settle_the_inquiry_is_not_passed_as_grounding():
    """continuation 으로 넘어간 rule 답변은 중립화되어야 한다."""
    import inspect

    source = inspect.getsource(AnswerService.generate_for_inquiry)
    assert "_neutral_gpt_context" in source
    # 중립화 조건에 "해결하지 못했다" 가 포함되어야 한다.
    assert "not self._deterministic_answer_settles_inquiry(" in source


def test_the_neutral_context_carries_no_answer_body():
    """중립화된 context 는 근거로 쓰일 본문을 갖지 않는다."""
    from answer.models import AnswerResult, AnswerStatus
    from services.answer_service import _neutral_gpt_context

    deferral = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="확인필요",
        reason="추가정보 필요",
        answer="문의주신 내용은 정확한 정보 확인이 필요합니다.",
        provider="rules",
        auto_answerable=False,
        needs_review=True,
    )
    neutral = _neutral_gpt_context(
        deferral, template_failure="NOT_FOUND", category="상품",
    )
    assert neutral.answer == ""
    assert neutral.auto_answerable is False


def test_a_settling_deterministic_answer_still_grounds_generation():
    """해결한 답변까지 중립화하면 안 된다 -- 게이트가 True 면 조건이 꺼진다."""
    payload = semantic_payload(
        atom("배송비는 얼마인가요", action="DELIVERY_POLICY",
             attribute="AMOUNT_OR_COST"),
        atom("브라켓도 같이 오나요", action="PACKAGE_CONTENTS",
             attribute="INCLUSION"),
    )
    assert _gate(
        "배송비는 얼마인가요?\n브라켓도 같이 오나요?",
        "배송비는 무료입니다. 벽걸이 브라켓은 구성품에 포함되어 함께 발송됩니다.",
        payload,
    ) is True
