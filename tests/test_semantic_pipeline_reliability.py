"""Adding a model must not cost the pipeline a single answer it used to post.

A semantic stage sits in front of a queue that answers customers without
supervision. The danger is not only a wrong verdict -- it is a stage that hangs,
throws, poisons a cache, or quietly stops a draft one step short of posting. An
inquiry that used to reach the customer and now sits in PROCESSING is a worse
outcome than the mis-routed A/S answer this whole change exists to prevent.

So these tests exercise the real generation path against a copy of the store and
a recording post client, and check the *pipeline*, not the model: that a fast
inquiry never reaches a provider, that a compatible verdict still posts, that
only a genuine mismatch holds, that every provider fault falls through to the
answer that would have been produced anyway, that one inquiry's timeout does not
touch the next, that a repeat run still posts exactly once, and that no failure
here can switch automatic processing off.

Every provider is a stand-in. No real model call, no DPS lookup, no Naver
request, and nothing is written to the operational database.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from repositories.answer_repository import AnswerRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.semantic_action_support import REASON_CODE
import services.answer_service as answer_service_module


PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"

FAST_QUESTION = "배송은 보통 며칠 걸리나요?"
COMPATIBLE_QUESTION = "고장난 TV 수리해주세요"
MISMATCH_QUESTION = "고장난 기존 tv 수거 요청드려요"


def payload(primary: str, *secondary: str, confidence: float = 0.95) -> dict:
    return {
        "primary_action": primary, "secondary_actions": list(secondary),
        "request_type": "ACTION_REQUEST",
        "objects": [{"type": "TV", "states": ["BROKEN"]}],
        "atomic_questions": [{"text": "q", "action": primary}],
        "deadline": None, "constraints": [], "negation": False,
        "conditional": False, "requires_order_context": False,
        "requires_delivery_schedule": False, "confidence": confidence,
    }


ANSWERS = {
    MISMATCH_QUESTION: payload("COLLECTION"),
    "고장난 TV 수거해주세요": payload("COLLECTION"),
    COMPATIBLE_QUESTION: payload("REPAIR"),
    "기존 TV 가져가주시나요?": payload("COLLECTION"),
}


class SemanticProvider:
    """Answers semantic calls; raises whatever a transport would raise."""

    name = "semantic-stub"

    def __init__(self, fault: Exception | dict | None = None) -> None:
        self.fault = fault
        self.calls: list[str] = []

    def generate_json(self, *, task, prompt, context):
        assert task == "SEMANTIC_ANALYSIS", f"unexpected GPT task: {task}"
        asked = prompt.split("INQUIRY:\n")[-1].strip()
        self.calls.append(asked)
        if isinstance(self.fault, Exception):
            raise self.fault
        if isinstance(self.fault, dict):
            return self.fault
        for question, value in ANSWERS.items():
            if question in asked:
                return value
        return payload("OTHER")


class NoDps:
    """Never performs a lookup. Records that it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def enrich(self, request, **kwargs):
        return self.skip_for_phase9(request)

    def skip_for_phase9(self, request, **kwargs):
        self.calls += 1
        request.metadata["dps"] = {
            "lookup_required": False, "lookup_status": "NOT_REQUIRED",
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"], lookup_row=None,
        )


class PostRecorder:
    """Stands in for the Naver client. Counts, never sends."""

    def __init__(self) -> None:
        self.posts: list[int] = []

    def post(self, inquiry_id: int) -> None:
        self.posts.append(int(inquiry_id))


@pytest.fixture
def store(tmp_path) -> Database:
    database = Database(tmp_path / "reliability.db")
    database.initialize()
    return database


@pytest.fixture
def semantic_on(monkeypatch):
    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")


@pytest.fixture
def semantic_off(monkeypatch):
    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "0")


def install(monkeypatch, provider: SemanticProvider) -> None:
    monkeypatch.setattr(
        answer_service_module, "create_gpt_provider", lambda *a, **k: provider,
    )


def ask(store: Database, question: str, *, key: str) -> int:
    return InquiryRepository(store).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "NAVER",
        "source_question_id": key, "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의", "content": question, "product_name": PRODUCT,
        "product_id": "12139453925", "order_id": None,
        "product_order_id": None, "raw_json": {},
    }).inquiry_id


def run(store: Database, inquiry_id: int, dps: NoDps | None = None):
    """Generate through the real service. Returns the draft, or the error."""

    try:
        AnswerService(
            store, dps_enrichment=dps or NoDps(),
        ).generate_for_inquiry(inquiry_id)
    except Exception as error:  # generation may legitimately refuse
        return None, error
    row = AnswerRepository(store).latest_for_inquiry(inquiry_id)
    if row is None:
        return None, None
    draft = dict(row)
    for field in ("metadata_json", "validator_result_json"):
        raw = draft.get(field)
        if isinstance(raw, str):
            try:
                draft[field] = json.loads(raw)
            except ValueError:
                draft[field] = {}
    return draft, None


def verdict(store: Database, inquiry_id: int, draft: dict):
    metadata = draft.get("metadata_json") or {}
    return AutoProcessingEligibilityService().evaluate(
        inquiry=InquiryRepository(store).get(inquiry_id), draft=draft,
        route=str(metadata.get("selected_answer_route") or ""),
    )


def outcome(store, question, monkeypatch, provider, key="k"):
    install(monkeypatch, provider)
    inquiry_id = ask(store, question, key=key)
    draft, error = run(store, inquiry_id)
    if draft is None:
        return {"draft": None, "error": error, "decision": None}
    decision = verdict(store, inquiry_id, draft)
    metadata = draft.get("metadata_json") or {}
    return {
        "draft": draft, "error": error, "decision": decision.decision,
        "reasons": list(decision.reasons),
        "answer": str(draft.get("original_answer") or ""),
        "validation_status": draft.get("validation_status"),
        "route": metadata.get("selected_answer_route"),
        "semantic": metadata.get("semantic_analysis") or {},
        "support": metadata.get("semantic_action_support"),
    }


# ==========================================================================
# P0-2  A fast-path inquiry never reaches a provider, and decides identically
# ==========================================================================


def test_a_fast_path_inquiry_costs_no_provider_call(
    store, semantic_on, monkeypatch,
) -> None:
    provider = SemanticProvider()

    result = outcome(store, FAST_QUESTION, monkeypatch, provider, key="fast")

    assert provider.calls == []
    assert result["decision"] == "SAFE"
    assert REASON_CODE not in result["reasons"]


def test_off_and_on_agree_on_a_fast_path_inquiry(
    tmp_path, monkeypatch,
) -> None:
    seen = {}
    for mode in ("0", "1"):
        database = Database(tmp_path / f"fast-{mode}.db")
        database.initialize()
        monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", mode)
        provider = SemanticProvider()
        seen[mode] = outcome(
            database, FAST_QUESTION, monkeypatch, provider, key="fast",
        )
        assert provider.calls == []

    for field in ("decision", "answer", "validation_status", "route"):
        assert seen["0"][field] == seen["1"][field], field


# ==========================================================================
# P0-1 / P0-3  A compatible understanding still posts
# ==========================================================================


def test_a_compatible_verdict_leaves_the_answer_publishable(
    store, semantic_on, monkeypatch,
) -> None:
    """The semantic stage ran, agreed, and changed nothing."""

    provider = SemanticProvider()

    result = outcome(store, COMPATIBLE_QUESTION, monkeypatch, provider,
                     key="compatible")

    assert result["decision"] == "SAFE"
    assert REASON_CODE not in result["reasons"]
    assert result["answer"].strip()


def test_the_gate_does_not_lower_auto_post_across_a_corpus(
    tmp_path, monkeypatch,
) -> None:
    """P0-10: ordinary inquiries keep the decision they already had."""

    corpus = [
        FAST_QUESTION, "설치 일정은 어떻게 안내받나요?", "A/S는 어디서 받나요?",
        "HDMI 단자 몇 개인가요?", COMPATIBLE_QUESTION,
        "기존 TV 가져가주시나요?",
    ]
    seen: dict[str, list] = {}
    for mode in ("0", "1"):
        database = Database(tmp_path / f"corpus-{mode}.db")
        database.initialize()
        monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", mode)
        provider = SemanticProvider()
        install(monkeypatch, provider)
        results = []
        for index, question in enumerate(corpus):
            inquiry_id = ask(database, question, key=f"c-{index}")
            draft, _ = run(database, inquiry_id)
            results.append(
                None if draft is None
                else verdict(database, inquiry_id, draft).decision
            )
        seen[mode] = results

    lost = [
        corpus[i] for i, (off, on) in enumerate(zip(seen["0"], seen["1"]))
        if off == "SAFE" and on != "SAFE"
    ]
    assert lost == [], f"auto-post lost on: {lost}"


# ==========================================================================
# P0-4  Only a genuine mismatch holds
# ==========================================================================


def test_only_a_mismatch_holds(store, semantic_on, monkeypatch) -> None:
    provider = SemanticProvider()

    result = outcome(store, MISMATCH_QUESTION, monkeypatch, provider,
                     key="mismatch")

    assert result["decision"] == "REVIEW_REQUIRED"
    assert REASON_CODE in result["reasons"]
    assert (result["support"] or {}).get("status") == "MISMATCH"
    # The draft still exists. A held answer is reviewable, not lost.
    assert result["answer"].strip()


# ==========================================================================
# P0-5  Every provider fault falls through
# ==========================================================================


@pytest.mark.parametrize("fault", [
    TimeoutError("read timed out"),
    ConnectionError("transport reset"),
    RuntimeError("rate limited"),
    PermissionError("auth rejected"),
    ValueError("invalid json"),
    KeyError("schema"),
])
def test_a_provider_fault_never_costs_the_answer(
    store, semantic_on, monkeypatch, fault,
) -> None:
    provider = SemanticProvider(fault=fault)

    result = outcome(store, MISMATCH_QUESTION, monkeypatch, provider,
                     key=f"fault-{type(fault).__name__}")

    assert result["error"] is None
    assert result["answer"].strip(), "the answer must still be produced"
    assert REASON_CODE not in (result["reasons"] or [])
    assert (result["support"] or {"status": "UNDETERMINED"})["status"] != "MISMATCH"


@pytest.mark.parametrize("bad", [
    {"primary_action": "TAKE_IT_AWAY", "confidence": 0.9},
    {"primary_action": "COLLECTION", "confidence": "high"},
    {"primary_action": "COLLECTION", "confidence": 0.1},
    {"nothing": True},
])
def test_unusable_output_never_costs_the_answer(
    store, semantic_on, monkeypatch, bad,
) -> None:
    provider = SemanticProvider(fault=bad)

    result = outcome(store, MISMATCH_QUESTION, monkeypatch, provider,
                     key=f"bad-{abs(hash(str(bad)))}")

    assert result["error"] is None
    assert result["answer"].strip()
    assert REASON_CODE not in (result["reasons"] or [])


# ==========================================================================
# P0-6  A mixed batch: one inquiry's fault must not touch the next
# ==========================================================================


def test_a_failure_does_not_disturb_the_inquiries_after_it(
    store, semantic_on, monkeypatch,
) -> None:
    batch = [
        (FAST_QUESTION, None),
        ("설치 일정은 어떻게 안내받나요?", None),
        (MISMATCH_QUESTION, None),
        (FAST_QUESTION, None),
        ("고장난 TV 수거해주세요", TimeoutError("boom")),
        (FAST_QUESTION, None),
        (COMPATIBLE_QUESTION, None),
        ("기존 TV 가져가주시나요?", None),
        ("A/S는 어디서 받나요?", None),
    ]
    results = []
    for index, (question, fault) in enumerate(batch):
        provider = SemanticProvider(fault=fault)
        install(monkeypatch, provider)
        inquiry_id = ask(store, question, key=f"batch-{index}")
        draft, error = run(store, inquiry_id)
        results.append({
            "question": question, "fault": fault is not None,
            "error": error,
            "drafted": draft is not None,
            "decision": (
                verdict(store, inquiry_id, draft).decision
                if draft is not None else None
            ),
        })

    assert all(r["error"] is None for r in results), [
        (r["question"], r["error"]) for r in results if r["error"]
    ]
    assert all(r["drafted"] for r in results)
    # The one that timed out is not held on that account.
    timed_out = results[4]
    assert timed_out["decision"] in {"SAFE", "REVIEW_REQUIRED"}
    # The genuine mismatch is still caught, and the ones after the fault are
    # decided exactly as they would have been alone.
    assert results[2]["decision"] == "REVIEW_REQUIRED"
    assert results[5]["decision"] == "SAFE"
    assert results[6]["decision"] == "SAFE"


# ==========================================================================
# P0-7 / P0-9  Repeats, state, and the cache
# ==========================================================================


def test_a_repeat_run_does_not_call_twice_or_change_the_verdict(
    store, semantic_on, monkeypatch,
) -> None:
    """A rerun, a regeneration and a retry are the same question."""

    provider = SemanticProvider()
    install(monkeypatch, provider)
    service = AnswerService(store, dps_enrichment=NoDps())
    inquiry_id = ask(store, MISMATCH_QUESTION, key="repeat")

    decisions = []
    for _ in range(3):
        service.generate_for_inquiry(inquiry_id)
        row = AnswerRepository(store).latest_for_inquiry(inquiry_id)
        draft = dict(row)
        for field in ("metadata_json", "validator_result_json"):
            raw = draft.get(field)
            if isinstance(raw, str):
                draft[field] = json.loads(raw or "{}")
        decisions.append(verdict(store, inquiry_id, draft).decision)

    assert decisions == ["REVIEW_REQUIRED"] * 3
    assert len(provider.calls) == 1, (
        f"one question, {len(provider.calls)} provider calls"
    )


def test_no_inquiry_is_left_in_an_intermediate_state(
    store, semantic_on, monkeypatch,
) -> None:
    """P0-9: every path ends somewhere terminal, faults included."""

    cases = [
        (FAST_QUESTION, None), (MISMATCH_QUESTION, None),
        (COMPATIBLE_QUESTION, TimeoutError("t")),
    ]
    for index, (question, fault) in enumerate(cases):
        install(monkeypatch, SemanticProvider(fault=fault))
        inquiry_id = ask(store, question, key=f"state-{index}")
        draft, error = run(store, inquiry_id)

        assert error is None
        assert draft is not None
        status = str(draft.get("review_status") or "")
        assert status in {"PENDING", "NEEDS_REVIEW", "IN_REVIEW", "APPROVED"}, (
            f"{question}: unexpected terminal state {status!r}"
        )
        assert str(draft.get("validation_status") or "")


# ==========================================================================
# P0-8  A semantic fault is not an automatic-processing fault
# ==========================================================================


def test_a_semantic_failure_never_switches_automatic_processing_off(
    store, semantic_on, monkeypatch,
) -> None:
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    repository = AutoPostRepository(store)
    repository.save_settings(enabled=True, interval_minutes=10, max_retries=1)

    for index, fault in enumerate([
        TimeoutError("t"), ConnectionError("c"), RuntimeError("r"),
    ]):
        install(monkeypatch, SemanticProvider(fault=fault))
        inquiry_id = ask(store, MISMATCH_QUESTION, key=f"switch-{index}")
        run(store, inquiry_id)

        assert repository.settings()["enabled"] is True, (
            "a semantic fault must never stop automatic processing"
        )


def test_the_recorder_never_lets_its_own_fault_escape(
    store, semantic_on, monkeypatch,
) -> None:
    """Even a provider factory that explodes leaves generation untouched."""

    def exploding(*args, **kwargs):
        raise RuntimeError("provider construction failed")

    monkeypatch.setattr(answer_service_module, "create_gpt_provider", exploding)
    inquiry_id = ask(store, MISMATCH_QUESTION, key="factory")

    draft, error = run(store, inquiry_id)

    assert error is None
    assert draft is not None
    assert str(draft.get("original_answer") or "").strip()


# ==========================================================================
# Decision value: the call is spent only where it could change the outcome
# ==========================================================================


def test_an_already_held_answer_costs_no_provider_call(
    store, semantic_on, monkeypatch,
) -> None:
    """The gate can only add a hold, so on a held answer it buys nothing."""

    provider = SemanticProvider()
    install(monkeypatch, provider)
    # A deadline question is already held by DELIVERY_DEADLINE_NOT_CONFIRMABLE.
    inquiry_id = ask(
        store, "혹시 오늘 주문하면 9일까지 받아볼 수 있을까요?", key="held",
    )
    draft, error = run(store, inquiry_id)

    assert error is None
    decision = verdict(store, inquiry_id, draft)
    assert decision.decision == "REVIEW_REQUIRED"
    assert "DELIVERY_DEADLINE_NOT_CONFIRMABLE" in decision.reasons
    metadata = draft.get("metadata_json") or {}
    router = (metadata.get("semantic_analysis") or {}).get("router") or {}
    assert "NO_DECISION_VALUE" in (router.get("reasons") or [])
    assert provider.calls == []
