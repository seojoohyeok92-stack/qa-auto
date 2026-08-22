"""Compound inquiry 686058300: the partial answer must survive validation.

Production reproduction (inquiry_id 2592, registered 2026-08-23T04:41:32).
The six-part inquiry produced no customer answer at all. The stored record
showed provider=safe_rule / route=REVIEW_REQUIRED_SAFE_DRAFT after these
validator errors:

    존재하지 않는 Fact를 사용했습니다: analysis.requires_order_id
    존재하지 않는 Fact를 사용했습니다: analysis.private_post_required
    GPT 자체 검토를 통과하지 못했습니다.
    GPT 자체 검토에서 사실 불일치를 확인했습니다.

Three defects combined:

  1. ``analysis.*`` is a virtual namespace understood by FactSelectionService
     and advertised to the provider as allowed_fact_paths, but the validator
     resolved fact paths with ``AnswerFacts.get_fact`` alone. The pipeline
     told the provider to cite those paths and then rejected the draft for
     citing them -- an error no corrective regeneration could ever fix.
  2. ``_analyze_compound`` dropped UNCLASSIFIED sub-questions outright, so the
     "카드 할인도 되나요?" part vanished together with its review requirement
     and the inquiry was recorded as five questions, not six.
  3. ``not review.passed`` was treated as a hard error even when the only
     deficiency the self-review reported was incomplete coverage -- which is
     precisely what the pipeline asks the provider to do on a compound
     inquiry whose other parts have no evidence.

Drafting a partial answer and publishing it are separate decisions: this
inquiry must still produce a draft while never becoming auto-postable.

Fakes only -- no provider, no network, no posting.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from answer.fact_selection import (
    ANALYSIS_FACT_KEYS,
    FactSelectionService,
    resolve_fact,
)
from answer.facts import build_answer_facts
from answer.answer_validator import AnswerValidator
from answer.hybrid_models import (
    DraftResult,
    Emotion,
    IntentResult,
    SelfReviewResult,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from answer.text_utils import split_subquestions
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The exact customer text of inquiry 686058300.
SIX_PART = (
    "A/S는 삼성서비스센터에서 하나요?\n"
    "설치는 기사님이 해주시나요?\n"
    "집에 있는 브라켓과 호환되나요?\n"
    "설치예정일은 언제인가요?\n"
    "카드 할인도 되나요?\n"
    "배송 중 파손되면 어떻게 하나요?"
)

ANALYSIS = InquiryAnalysisService()
ELIGIBILITY = AutoProcessingEligibilityService()
VALIDATOR = AnswerValidator()

# A partial answer of the shape the policy asks for: the two grounded
# sub-questions answered, the four unsupported ones deferred without
# inventing anything.
PARTIAL_ANSWER = (
    "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
    "설치는 전문 기사가 방문하여 진행합니다.\n"
    "브라켓 호환 여부와 카드 혜택은 확인 후 안내드리겠습니다.\n"
    "설치예정일은 주문번호를 알려주시면 확인해 드리겠습니다.\n"
    "배송 중 파손은 담당 직원이 확인 후 안내드리겠습니다."
)


def request(question: str = SIX_PART) -> AnswerRequest:
    # inquiry_type must stay PRODUCT_INQUIRY: the legacy type feeds the
    # classifier, and only this value reproduces the production analysis
    # (a "상품" legacy type labels the card sub-question
    # LEGACY_PRODUCT_CATEGORY instead of UNCLASSIFIED, which hides the defect).
    return AnswerRequest(
        inquiry_id=2592,
        question_id="686058300",
        inquiry_type="PRODUCT_INQUIRY",
        question=question,
        product_name="삼성 50인치 TV",
        metadata={
            "source_type": "PRODUCT_INQUIRY",
            "dps": {
                "lookup_required": False,
                "lookup_status": "NOT_REQUIRED",
                "warnings": [],
            },
        },
    )


def rule(*, needs_review: bool = True) -> AnswerResult:
    return AnswerResult(
        status=(
            AnswerStatus.NEEDS_REVIEW
            if needs_review
            else AnswerStatus.GENERATED
        ),
        category="설치/AS",
        reason="Rule",
        answer="설치와 A/S 관련 안내를 드립니다.",
        provider="rules",
        auto_answerable=not needs_review,
        needs_review=needs_review,
        matched_rule="설치/AS",
    )


def _pieces(question: str = SIX_PART):
    """Facts, analysis and selected facts for one inquiry."""

    req = request(question)
    analysis = ANALYSIS.analyze(req)
    facts = build_answer_facts(req, rule())
    selected = FactSelectionService().select(facts, analysis)
    return facts, analysis, selected


def _validate(
    answer: str,
    used_facts: tuple[str, ...],
    *,
    review: SelfReviewResult | None = None,
    question: str = SIX_PART,
):
    facts, analysis, selected = _pieces(question)
    intent = IntentResult(
        category="설치/AS",
        questions=split_subquestions(question),
        emotion=Emotion.NORMAL,
        urgency="NORMAL",
        confidence=0.9,
        requires_review=True,
        reason="복합문의",
    )
    draft = DraftResult(answer=answer, confidence=0.8, used_facts=used_facts)
    return VALIDATOR.validate(
        facts,
        intent,
        draft,
        review or passing_review(),
        analysis=analysis,
        selected_facts=selected,
    )


def passing_review(**overrides) -> SelfReviewResult:
    values = {
        "passed": True,
        "answered_all_questions": True,
        "has_speculation": False,
        "facts_consistent": True,
        "requires_review": False,
        "reason": "검토 완료",
        "warnings": (),
    }
    values.update(overrides)
    return SelfReviewResult(**values)


def evaluate(question: str, *, route: str = "GPT_DIRECT"):
    analysis = ANALYSIS.analyze(request(question))
    return ELIGIBILITY.evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": PARTIAL_ANSWER,
            "validation_status": "PASSED",
            "validator_result_json": None,
            "review_status": "",
            "metadata_json": {
                "processing_plan": {"analysis": analysis.to_dict()}
            },
            "posted": False,
            "id": 1,
        },
        route=route,
    )


class CountingPostService:
    """Stand-in for the Naver posting service; must never be called."""

    def __init__(self) -> None:
        self.calls = 0

    def post(self, *args, **kwargs):  # pragma: no cover - must not run
        self.calls += 1
        raise AssertionError("POST must not be called for a blocked inquiry")


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "compound-686058300.db")
    value.initialize()
    return value


def _stored_inquiry(database: Database, question: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "686058300",
            "external_inquiry_id": "686058300",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "복합문의",
            "content": question,
            "product_name": "삼성 50인치 TV",
            "registered_at": "2026-08-23T04:41:32+09:00",
            "raw_json": {},
        }
    ).inquiry_id


# ------------------------------------------------------------ CASE A

def test_case_a_six_questions_are_decomposed_and_counted_as_six() -> None:
    assert len(split_subquestions(SIX_PART)) == 6
    analysis = ANALYSIS.analyze(request())
    assert "6개 질문" in analysis.reasons[0]


def test_case_a_card_benefit_subquestion_is_not_silently_dropped() -> None:
    """The dropped part was "카드 할인도 되나요" -- UNCLASSIFIED yet
    manual_review_required, so dropping it discarded a review requirement."""

    card = ANALYSIS._analyze_single(
        dataclasses.replace(request(), question="카드 할인도 되나요")
    )
    assert card.inquiry_subtype == "UNCLASSIFIED"
    assert card.manual_review_required is True

    # A compound of one safe part plus the card part must keep the review
    # requirement the card part carries.
    analysis = ANALYSIS.analyze(
        request("A/S는 삼성서비스센터에서 하나요?\n카드 할인도 되나요?")
    )
    assert analysis.manual_review_required is True


def test_inert_filler_fragment_is_still_dropped() -> None:
    """The UNCLASSIFIED filter still does its original job."""

    analysis = ANALYSIS.analyze(
        request("A/S는 삼성서비스센터에서 하나요?\n설치는 기사님이 해주시나요?")
    )
    assert analysis.manual_review_required is False


# ------------------------------------------------------------ CASE B

def test_case_b_every_selected_fact_key_resolves() -> None:
    facts, analysis, selected = _pieces()
    assert selected.keys, "the inquiry must select at least one fact"
    # Every key handed to the provider as an allowed path must resolve for the
    # validator too -- that equivalence is what broke in production.
    for key in selected.keys:
        assert (
            resolve_fact(facts, key, analysis=analysis) is not None
        ), f"selected fact key does not resolve: {key}"
    # Specifically, no analysis.* key the analyser asked for may be a dead end.
    for key in analysis.selected_fact_keys:
        if key in ANALYSIS_FACT_KEYS:
            assert resolve_fact(facts, key, analysis=analysis) is not None


def test_case_b_analysis_namespace_has_a_single_source_of_truth() -> None:
    """Every analysis.* key the analyser can emit must have a resolver.

    Guards against the two halves drifting apart again: a new key added to
    the analyser without a resolver would silently become a validator error.
    """

    source = (
        PROJECT_ROOT / "services" / "inquiry_analysis_service.py"
    ).read_text(encoding="utf-8")
    emitted = set(re.findall(r'"(analysis\.[a-z_]+)"', source))
    assert emitted, "expected the analyser to emit analysis.* fact keys"
    assert emitted <= ANALYSIS_FACT_KEYS, (
        "analysis.* keys without a resolver: "
        f"{sorted(emitted - ANALYSIS_FACT_KEYS)}"
    )


# ------------------------------------------------------- CASE C and D

@pytest.mark.parametrize(
    "path",
    ["analysis.requires_order_id", "analysis.private_post_required",
     "analysis.order_id_status"],
)
def test_case_c_d_analysis_facts_are_not_reported_as_nonexistent(
    path: str,
) -> None:
    result = _validate(PARTIAL_ANSWER, (path,))
    assert not [
        error for error in result.errors if "존재하지 않는 Fact" in error
    ], result.errors


def test_a_genuinely_unknown_fact_is_still_rejected() -> None:
    result = _validate(PARTIAL_ANSWER, ("analysis.not_a_real_key",))
    assert any("존재하지 않는 Fact" in error for error in result.errors)


# ------------------------------------------------------- CASE E, F, N

def test_case_e_f_n_partial_answer_survives_unanswerable_subquestions() -> None:
    """A/S and the general installation answer must not be thrown away
    because compatibility, the schedule, the card benefit and the damage
    question cannot be answered."""

    review = passing_review(passed=False, answered_all_questions=False)
    result = _validate(
        PARTIAL_ANSWER,
        ("analysis.requires_order_id", "analysis.private_post_required"),
        review=review,
    )
    assert result.passed is True, result.errors
    assert any("하위 질문 미답변" in signal for signal in result.review_signals)


def test_self_review_failure_with_a_real_finding_still_blocks() -> None:
    for overrides in (
        {"passed": False, "facts_consistent": False},
        {"passed": False, "has_speculation": True},
        # No identifiable cause: stay conservative and block.
        {"passed": False, "answered_all_questions": True},
    ):
        result = _validate(
            PARTIAL_ANSWER, (), review=passing_review(**overrides)
        )
        assert result.passed is False, overrides


# ------------------------------------------------------------ CASE G

def test_case_g_compatibility_speculation_is_still_blocked() -> None:
    result = _validate("기존 브라켓과 호환될 것 같습니다.", ())
    assert result.passed is False
    assert any("추측" in error for error in result.errors)


def test_case_g_compound_keeps_compatibility_review_reason() -> None:
    analysis = ANALYSIS.analyze(request())
    assert analysis.detected_intent == "PRODUCT_COMPATIBILITY"
    assert analysis.manual_review_required is True


# ------------------------------------------------------------ CASE H

def test_case_h_no_installation_date_without_order_and_dps() -> None:
    analysis = ANALYSIS.analyze(request())
    assert analysis.requires_order_id is True
    assert analysis.requires_dps_lookup is True
    facts, _, _ = _pieces()
    assert not facts.installation.get("date")
    result = _validate("설치예정일은 2026-09-01입니다.", ())
    assert result.passed is False, result.errors


# ------------------------------------------------------- CASE I and J

@pytest.mark.parametrize(
    "question",
    ["카드 할인도 되나요?", "배송 중 파손되면 어떻게 하나요?"],
)
def test_case_i_j_card_and_damage_require_review(question: str) -> None:
    single = ANALYSIS._analyze_single(
        dataclasses.replace(request(), question=question.rstrip("?"))
    )
    assert single.manual_review_required is True


# ------------------------------------------------------- CASE K and L

def test_case_k_six_part_inquiry_still_produces_a_draft() -> None:
    analysis = ANALYSIS.analyze(request())
    assert analysis.can_generate_answer is True


def test_case_l_six_part_inquiry_is_not_eligible_for_auto_post() -> None:
    result = evaluate(SIX_PART)
    assert result.safe is False
    assert result.reasons


def test_case_l_six_part_inquiry_never_reaches_post(
    database: Database,
) -> None:
    """Pipeline level, with a fake post service: nothing is published."""

    inquiry_id = _stored_inquiry(database, SIX_PART)
    posts = CountingPostService()
    pipeline = AutoPostPipelineService(database, post_service=posts)

    outcome = pipeline.run_pending(
        run_id="RUN-686058300",
        owner_id="OWNER-686058300",
        max_retries=1,
        inquiry_ids=[inquiry_id],
    )

    assert posts.calls == 0
    assert outcome.succeeded_count == 0


# ------------------------------------------------------------ CASE M

def test_case_m_all_safe_compound_stays_auto_postable() -> None:
    question = "A/S는 어디서 받나요? 설치는 기사님이 해주시나요?"
    analysis = ANALYSIS.analyze(request(question))
    assert analysis.can_generate_answer is True
    assert analysis.manual_review_required is False
    assert evaluate(question).safe is True


# ------------------------------------------------------------ CASE N

def test_case_n_end_to_end_partial_answer_is_not_replaced() -> None:
    """The hybrid service must keep the provider's partial answer instead of
    falling back to the generic "everything needs checking" draft."""

    provider = FakeGptProvider(
        responses={
            "DRAFT": {
                "answer": PARTIAL_ANSWER,
                "confidence": 0.8,
                "used_facts": [
                    "analysis.requires_order_id",
                    "analysis.private_post_required",
                ],
                "missing_information": ["브라켓 호환 정보"],
                "requires_review": True,
                "warnings": [],
            },
            "SELF_REVIEW": {
                "passed": False,
                "answered_all_questions": False,
                "has_speculation": False,
                "facts_consistent": True,
                "requires_review": True,
                "reason": "근거가 없는 하위 질문은 확인 안내로 처리했습니다.",
                "warnings": [],
            },
        }
    )
    # The hybrid service only builds an analysis from this metadata key; the
    # production pipeline populates it before calling in.
    req = request()
    req.metadata["phase9_analysis"] = ANALYSIS.analyze(req).to_dict()
    outcome = HybridAnswerService(provider).generate(req, rule())

    assert outcome.fallback_used is False, (
        outcome.validation.errors if outcome.validation else None
    )
    assert outcome.result.answer == PARTIAL_ANSWER
    assert "A/S는 삼성전자 서비스센터" in outcome.result.answer
    assert "설치는 전문 기사가 방문" in outcome.result.answer
    # Drafted, but held for staff.
    assert outcome.result.needs_review is True
    assert outcome.result.auto_answerable is False


# ------------------------------------------------------------ CASE O

def test_case_o_provider_failure_still_falls_back_to_the_safe_draft() -> None:
    provider = FakeGptProvider(fail_tasks={"DRAFT"})
    outcome = HybridAnswerService(provider).generate(request(), rule())
    assert outcome.fallback_used is True
    assert outcome.result.answer
    hybrid = outcome.result.metadata["hybrid"]
    assert hybrid["fallback_used"] is True
    assert hybrid["fallback_reason"]
