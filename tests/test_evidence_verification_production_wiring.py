"""The verifier as Production actually runs it, not as a service in isolation.

The previous release shipped this gate with its consumer wired and its
producer missing. Every unit test passed, the whole suite passed, and the
verifier was never called on a single live inquiry -- because "no record" and
"nothing to check" were the same answer, and nothing asserted that generation
had produced a record at all.

So these tests refuse to import the verifier and call it directly. They run
``HybridAnswerService.generate`` with the real learning context, read the
metadata the draft actually carries, and hand that metadata to the real
``AutoProcessingEligibilityService``. If the producer is unplugged again, they
go red.
"""
from __future__ import annotations

import pytest

from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.evidence_verification_service import (
    METADATA_KEY,
    NOT_SUPPORTED,
    REASON_CODE,
    SUPPORTED,
    decision_from_metadata,
    verification_required,
)
from services.hybrid_answer_service import HybridAnswerService
from services.semantic_analysis import AtomicQuestion


# ------------------------------------------------------------------ 사용 도구
class CountingProvider(FakeGptProvider):
    """The real generation provider, with the verifier's replies scripted.

    The verifier shares the pipeline's provider, so this counts the calls the
    verifier makes without disturbing the ones generation makes.
    """

    def __init__(self, verdict: str = NOT_SUPPORTED) -> None:
        super().__init__()
        self.verdict = verdict
        self.verification_calls = 0
        self.verification_prompts: list[str] = []

    def generate_json(self, *, task, prompt, context, **kwargs):
        if task == "EVIDENCE_VERIFICATION":
            self.verification_calls += 1
            self.verification_prompts.append(prompt)
            return {
                "verdict": self.verdict,
                "why": "테스트 판정",
                "missing": "비용 부담 주체",
            }
        return super().generate_json(
            task=task, prompt=prompt, context=context, **kwargs
        )


CM18_TEXT = "사다리차가 필요하면 비용은 누가 내나요?"
CM01_TEXT = "설치 기사님 안 부르고 받아만 볼 수 있나요?"


def atom(text: str, *, information: str, attribute: str) -> AtomicQuestion:
    return AtomicQuestion(
        text=text,
        action="ANSWER",
        requested_information=information,
        requested_attribute=attribute,
    )


class Semantic:
    """The semantic value object the pipeline attaches to the request."""

    usable = True

    def __init__(self, *atoms: AtomicQuestion) -> None:
        self.atomic_questions = list(atoms)


def learning_context(
    *,
    subquestion: str,
    learning_id: int = 317203,
    question: str = "엘베없는 5층 빌라 사다리차 비용, 유상무상 궁금합니다",
    answer: str = (
        "사다리차 사용 여부는 설치 기사님께서 판단하여 안내드리며 유상으로 알고 있습니다."
    ),
    status: str = "ANSWERABLE",
) -> dict:
    """The shape ``LearningContextService.build`` returns, keys unchanged."""

    return {
        "similar_approved_answers": [
            {
                "learning_example_id": learning_id,
                "learning_source": "STAFF_EDITED",
                "question": question,
                "answer": answer,
                "matched_subquestion": subquestion,
                "relevance": 0.8,
                "answer_support": 0.7,
            }
        ],
        "seller_style_examples": [],
        "historical_cases": [],
        "subquestion_evidence": [
            {
                "subquestion": subquestion,
                "status": status,
                "source": "ACTIVE_POSITIVE_LEARNING",
                "learning_ids": [learning_id] if status == "ANSWERABLE" else [],
                "historical_case_ids": [],
                "answer_required": status == "ANSWERABLE",
                "evidence_coverage": "SUPPORTED",
            }
        ],
        "feedback_signals": {
            "verified_facts": [],
            "corrections": [],
            "good_patterns": [],
            "bad_patterns": [],
        },
    }


def request(question: str, semantic) -> AnswerRequest:
    return AnswerRequest(
        inquiry_id=1,
        question_id="EV-1",
        inquiry_type="상품",
        question=question,
        product_name="삼성 125.7cm(50인치) 비즈니스TV LH50BEFHLGFXKR 스탠드형",
        metadata={
            "source_type": "PRODUCT_INQUIRY",
            "_semantic_routing_value": semantic,
            "dps": {
                "lookup_required": False,
                "lookup_status": "NOT_REQUIRED",
                "warnings": [],
            },
        },
    )


def rule() -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED,
        category="상품",
        reason="RULE",
        answer="안내드립니다.",
        provider="rules",
        auto_answerable=True,
        needs_review=False,
    )


def generate(*, question: str, semantic, context, provider):
    """Run the real Production generation path."""

    return HybridAnswerService(
        provider, learning_context_provider=lambda *a, **k: dict(context),
    ).generate(request(question, semantic), rule())


# ============================================ 1. Production 호출이 실제로 일어나는가
def test_a_learning_factual_inquiry_actually_calls_the_verifier():
    """The assertion the previous release did not have."""
    provider = CountingProvider()
    semantic = Semantic(
        atom(CM18_TEXT, information="사다리차 비용 부담 주체", attribute="ACTOR")
    )
    generate(
        question=CM18_TEXT,
        semantic=semantic,
        provider=provider,
        context=learning_context(subquestion=CM18_TEXT),
    )
    assert provider.verification_calls > 0


def test_the_verdict_reaches_the_draft_metadata():
    provider = CountingProvider()
    semantic = Semantic(
        atom(CM18_TEXT, information="사다리차 비용 부담 주체", attribute="ACTOR")
    )
    outcome = generate(
        question=CM18_TEXT,
        semantic=semantic,
        provider=provider,
        context=learning_context(subquestion=CM18_TEXT),
    )
    payload = outcome.result.metadata.get(METADATA_KEY)
    assert isinstance(payload, dict)
    assert payload["holds_auto_post"] is True


def test_the_asked_property_reaches_the_verifier_prompt():
    """requested_attribute is generated *and* used, not just recorded."""
    provider = CountingProvider()
    semantic = Semantic(
        atom(CM18_TEXT, information="사다리차 비용 부담 주체", attribute="ACTOR")
    )
    generate(
        question=CM18_TEXT,
        semantic=semantic,
        provider=provider,
        context=learning_context(subquestion=CM18_TEXT),
    )
    assert any("ACTOR" in text for text in provider.verification_prompts)
    assert any(
        "사다리차 비용 부담 주체" in text
        for text in provider.verification_prompts
    )


def test_the_verifier_reads_the_candidate_generation_was_given():
    """Not a re-retrieved set -- the same stored answer, verbatim."""
    provider = CountingProvider()
    semantic = Semantic(
        atom(CM18_TEXT, information="사다리차 비용 부담 주체", attribute="ACTOR")
    )
    generate(
        question=CM18_TEXT,
        semantic=semantic,
        provider=provider,
        context=learning_context(subquestion=CM18_TEXT),
    )
    assert any(
        "유상으로 알고 있습니다" in text
        for text in provider.verification_prompts
    )


# ================================================ 2. eligibility 가 실제로 소비하는가
def test_the_gate_consumes_the_recorded_verdict_and_holds():
    """One chain: generation records, the real gate reads, the answer holds."""
    provider = CountingProvider()
    semantic = Semantic(
        atom(
            CM01_TEXT,
            information="설치 없이 배송만 받을 수 있는지",
            attribute="PERMISSION_OR_OPTION",
        )
    )
    outcome = generate(
        question=CM01_TEXT,
        semantic=semantic,
        provider=provider,
        context=learning_context(
            subquestion=CM01_TEXT,
            learning_id=154681,
            question="설치해주시는 상품이라 주문했습니다",
            answer="삼성 설치 기사님께서 배송 후 설치까지 진행해드리는 제품 입니다.",
        ),
    )
    hold, why = decision_from_metadata(
        outcome.result.metadata, route="GPT_HYBRID"
    )
    assert hold is True
    assert why.startswith("NO_USABLE_EVIDENCE")


def test_a_verified_candidate_does_not_hold():
    """SUPPORTED evidence must still auto-answer. One evidence is enough."""
    provider = CountingProvider(verdict=SUPPORTED)
    question = "오토 피벗이 되는 모델인가요?"
    semantic = Semantic(
        atom(
            question,
            information="오토 피벗 지원 여부",
            attribute="EXISTENCE_OR_CAPABILITY",
        )
    )
    outcome = generate(
        question=question,
        semantic=semantic,
        provider=provider,
        context=learning_context(
            subquestion=question,
            learning_id=158169,
            question="오토피벗 지원하나요",
            answer="이동형 거치대 포함제품이며 오토피봇 가능한 제품입니다.",
        ),
    )
    assert provider.verification_calls == 1
    payload = outcome.result.metadata[METADATA_KEY]
    assert payload["holds_auto_post"] is False
    hold, _why = decision_from_metadata(
        outcome.result.metadata, route="GPT_HYBRID"
    )
    assert hold is False


# ======================================== 3. Verifier 를 부르지 않아야 하는 경로
def test_an_inquiry_with_no_stored_grounds_costs_no_call():
    provider = CountingProvider()
    semantic = Semantic(
        atom(CM18_TEXT, information="사다리차 비용 부담 주체", attribute="ACTOR")
    )
    outcome = generate(
        question=CM18_TEXT,
        semantic=semantic,
        provider=provider,
        context=learning_context(
            subquestion=CM18_TEXT, status="NO_RELIABLE_SOURCE"
        ),
    )
    assert provider.verification_calls == 0
    assert METADATA_KEY not in outcome.result.metadata


def test_no_semantic_analysis_costs_no_call():
    """Legacy callers with no semantic pass keep their behaviour exactly."""
    provider = CountingProvider()
    outcome = generate(
        question=CM18_TEXT,
        semantic=None,
        provider=provider,
        context=learning_context(subquestion=CM18_TEXT),
    )
    assert provider.verification_calls == 0
    assert METADATA_KEY not in outcome.result.metadata


@pytest.mark.parametrize("route", ["TEMPLATE", "SAFE_RULE", "PRODUCT_DB"])
def test_a_deterministic_route_is_never_held_by_this_gate(route):
    """Template/RULE/Catalog answered from sources settled before this gate."""
    metadata = {
        "hybrid": {
            "subquestion_evidence": [
                {
                    "subquestion": CM18_TEXT,
                    "status": "ANSWERABLE",
                    "learning_ids": [317203],
                    "historical_case_ids": [],
                }
            ]
        }
    }
    assert verification_required(metadata, route=route) is False
    assert decision_from_metadata(metadata, route=route)[0] is False


# ==================================== 4. producer 누락 재발 방지 (fail-closed)
def test_a_required_verification_with_no_record_holds_the_answer():
    """The invariant the last release lacked.

    Remove the producer and this goes red: the route needs verifying, nothing
    was written down, and the answer must not publish on silence.
    """
    metadata = {
        "hybrid": {
            "subquestion_evidence": [
                {
                    "subquestion": CM18_TEXT,
                    "status": "ANSWERABLE",
                    "learning_ids": [317203],
                    "historical_case_ids": [],
                }
            ]
        }
    }
    assert verification_required(metadata, route="GPT_HYBRID") is True
    hold, why = decision_from_metadata(metadata, route="GPT_HYBRID")
    assert hold is True
    assert why == "VERIFICATION_REQUIRED_BUT_NOT_RECORDED"


def test_an_inquiry_that_never_needed_verifying_is_untouched():
    """Fail-closed must not become fail-closed-everywhere."""
    metadata = {
        "hybrid": {
            "subquestion_evidence": [
                {
                    "subquestion": CM18_TEXT,
                    "status": "NO_RELIABLE_SOURCE",
                    "learning_ids": [],
                    "historical_case_ids": [],
                }
            ]
        }
    }
    assert verification_required(metadata, route="GPT_HYBRID") is False
    hold, why = decision_from_metadata(metadata, route="GPT_HYBRID")
    assert hold is False
    assert why == "VERIFICATION_NOT_REQUIRED"


def test_a_draft_predating_this_gate_is_untouched():
    """No hybrid record at all -- nothing to require, nothing to hold."""
    assert decision_from_metadata({}, route="GPT_HYBRID")[0] is False


def test_the_real_eligibility_service_appends_the_reason():
    """The gate the pipeline actually calls, not a reimplementation."""
    metadata = {
        "hybrid": {
            "subquestion_evidence": [
                {
                    "subquestion": CM18_TEXT,
                    "status": "ANSWERABLE",
                    "learning_ids": [317203],
                    "historical_case_ids": [],
                }
            ]
        }
    }
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry={"inquiry_id": 1, "question": CM18_TEXT},
        draft={
            "metadata_json": metadata,
            "original_answer": "사다리차 비용은 유상으로 알고 있습니다.",
        },
        route="GPT_HYBRID",
    )
    assert REASON_CODE in verdict.reasons
