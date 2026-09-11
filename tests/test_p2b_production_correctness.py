"""The four boundaries P2-B had to fix, each pinned where it actually lives.

Every case here was measured on real production data first (the 688393xxx
inquiries, and a read-only pass over product_facts.db); the numbers quoted in
the docstrings are from those measurements. No inquiry id or customer wording is
a condition in production code, and none is one here either -- these tests
describe behaviour, and the ids only say where the behaviour was observed.
"""
from __future__ import annotations

import pytest

from services.auto_processing_eligibility_service import (
    POLICY_STAFF_ONLY_ACTIONS,
    RETURN_OR_DAMAGE_POLICY_REVIEW,
    SOFT_REASONS,
    AutoProcessingEligibilityService,
)
from services.learning_context_service import (
    _RETRIEVAL_DEPTH_PER_ATOM,
    _retrieval_depth,
    interleave_by_rank,
)
from services.product_knowledge_service import ProductKnowledgeService


# ===========================================================================
# 1. Learning candidate budget
# ===========================================================================

def _row(learning_id: int, relevance: float) -> dict:
    return {"learning_example_id": learning_id, "relevance": relevance}


def test_decomposing_an_inquiry_never_reduces_what_each_part_may_use():
    """The depth used to halve when the inquiry was split.

    5 alone, 3 once decomposed -- so breaking a question into its parts, which
    exists so each part can be answered properly, gave each part less to answer
    from. On 688393243 the store's own answer to the installation question
    ranked 5th of the 630 candidates that cleared that atom's relevance floor,
    and a depth of 3 could not reach it.
    """

    depths = {count: _retrieval_depth(count) for count in (1, 2, 3, 4, 6, 9)}
    assert set(depths.values()) == {_RETRIEVAL_DEPTH_PER_ATOM}, depths
    assert _RETRIEVAL_DEPTH_PER_ATOM >= 5, (
        "the measured answer sat at rank 5; a shallower read cannot see it"
    )


def test_every_sub_question_is_heard_before_depth_is_spent_anywhere():
    """Interleaved by rank, so no sub-question is silenced by another."""

    groups = [
        [_row(1, 0.91), _row(2, 0.88), _row(3, 0.80)],
        [_row(4, 0.42), _row(5, 0.40)],
        [_row(6, 0.11)],
    ]
    order = [item["learning_example_id"] for item in interleave_by_rank(
        groups, id_key="learning_example_id",
    )]
    assert order[:3] == [1, 4, 6], order
    assert set(order) == {1, 2, 3, 4, 5, 6}


def test_the_narrowest_sub_question_survives_a_prompt_that_has_to_shrink():
    """The budget drops from the tail, so position is what protects a row.

    Ranked globally the third sub-question's only candidate (0.11) sorted last
    and was the first thing a shrinking prompt lost, however little else that
    sub-question had.
    """

    groups = [
        [_row(1, 0.91), _row(2, 0.88), _row(3, 0.80)],
        [_row(4, 0.42), _row(5, 0.40)],
        [_row(6, 0.11)],
    ]
    globally_ranked = sorted(
        (item for group in groups for item in group),
        key=lambda item: item["relevance"], reverse=True,
    )
    assert [item["learning_example_id"] for item in globally_ranked[:3]] == [1, 2, 3]

    kept = [
        item["learning_example_id"]
        for item in interleave_by_rank(groups, id_key="learning_example_id")[:3]
    ]
    assert 6 in kept, kept


def test_the_union_is_not_capped_and_loses_no_candidate():
    """Nine candidates used to become eight. Delivery is the budget's call."""

    groups = [[_row(i, 1.0 - i / 100) for i in range(n, n + 3)]
              for n in (1, 10, 20)]
    merged = interleave_by_rank(groups, id_key="learning_example_id")
    assert len(merged) == 9
    expected = {item["learning_example_id"] for group in groups for item in group}
    assert {item["learning_example_id"] for item in merged} == expected


def test_a_duplicate_is_kept_once_at_its_best_score():
    """Technical dedup is allowed; losing the row is not."""

    groups = [[_row(7, 0.20)], [_row(7, 0.95), _row(8, 0.30)]]
    merged = interleave_by_rank(groups, id_key="learning_example_id")
    ids = [item["learning_example_id"] for item in merged]
    assert ids.count(7) == 1
    assert {item["learning_example_id"]: item["relevance"] for item in merged}[7] \
        == pytest.approx(0.95)
    assert 8 in ids


def test_a_caller_with_a_real_allowance_still_gets_one():
    """Style references are not evidence and keep their fixed allowance."""

    groups = [[_row(1, 0.9), _row(2, 0.8)], [_row(3, 0.7), _row(4, 0.6)]]
    assert len(interleave_by_rank(
        groups, id_key="learning_example_id", limit=3,
    )) == 3


# ===========================================================================
# 2. Product model identity
# ===========================================================================

def _exclusion(expected: str | None, row_model: str | None) -> str | None:
    """Only the model-scope question: everything else is made to pass."""

    return ProductKnowledgeService._exclusion_reason(
        row={
            "lifecycle_status": "ACTIVE",
            "verification_status": "VERIFIED",
            "resolution_status": "SINGLE_SOURCE",
            "volatility": "STABLE",
            "field": "screen_size",
            "selected_value_id": "v1",
        },
        value={"inch": 50},
        provenance=({"lifecycle_status": "ACTIVE", "source_status": "VERIFIED"},),
        expected_model=expected,
        row_model=row_model,
        collection_status="COLLECTION_SUCCESS",
    )


MISMATCH = "MODEL_SCOPE_MISMATCH"


def test_the_same_model_written_out_in_full_is_the_same_model():
    """The measured failure: 2,884 of 5,261 verified facts were refused.

    The listing title gives a code; the label stored beside the fact gives a
    sentence that contains that code. Compared whole, 50 of 94 products lost
    every verified fact they had.
    """

    assert _exclusion(
        "LH50BEHHLGFXKR",
        "2026 LED 4K BE50H-H 125.7CM(50인치) (LH50BEHHLGFXKR) 스탠드",
    ) is None


def test_a_different_model_is_still_refused():
    assert _exclusion("LH50BEHHLGFXKR", "LH55BEHHLGFXKR") == MISMATCH


def test_a_different_model_inside_a_descriptive_label_is_still_refused():
    assert _exclusion(
        "LH50BEHHLGFXKR",
        "2026 LED 4K BE55H-H 138.7CM(55인치) (LH55BEHHLGFXKR) 스탠드",
    ) == MISMATCH


def test_a_suffix_that_survives_extraction_stays_a_mismatch():
    """Deliberately conservative: nothing here can prove a suffix is regional."""

    assert _exclusion("LS32DM501", "LS32DM501EKXKR") == MISMATCH


def test_no_expected_model_means_the_question_is_not_asked():
    """A title that names no model -- the 688393xxx product -- is not evidence
    of a different one, and product_id identity already holds."""

    assert _exclusion(None, "삼성 50인치 비즈니스TV + 무빙 이동식 스탠드") is None
    assert _exclusion("", "LH50BEHHLGFXKR") is None


def test_no_row_model_means_the_question_is_not_asked():
    assert _exclusion("LH50BEHHLGFXKR", None) is None
    assert _exclusion("LH50BEHHLGFXKR", "") is None


def test_a_label_with_no_extractable_code_is_not_read_as_a_model_claim():
    """A marketing name is not a competing identity; the rows were fetched
    ``WHERE cfl.product_id = ?``, which is the stronger identity."""

    assert _exclusion(
        "LH50BEHHLGFXKR", "삼성전자 2024 LED 4K 125.7CM 비즈니스TV",
    ) is None
    assert _exclusion("LH50BEHHLGFXKR", "12022024182") is None


def test_a_prefix_is_not_treated_as_a_match():
    """Substring logic was rejected on purpose, so this must not pass.

    ``LH50BE`` is a prefix of ``LH50BEHHLGFXKR`` and both extract cleanly as
    codes. Equality is the rule, so the prefix is still a mismatch.
    """

    assert _exclusion("LH50BE", "LH50BEHHLGFXKR") == MISMATCH
    assert _exclusion("LH50BEHHLGFXKR", "LH50BE") == MISMATCH


@pytest.mark.parametrize("status,expected_reason", [
    ("NEEDS_REVIEW", "VERIFICATION_NEEDS_REVIEW"),
    ("CONFLICT", "VERIFICATION_CONFLICT"),
])
def test_an_unverified_fact_is_still_refused_whatever_the_model_says(
    status, expected_reason,
):
    """Canonicalisation must not become a way past the VERIFIED requirement."""

    assert ProductKnowledgeService._exclusion_reason(
        row={
            "lifecycle_status": "ACTIVE", "verification_status": status,
            "resolution_status": "SINGLE_SOURCE", "volatility": "STABLE",
            "field": "screen_size", "selected_value_id": "v1",
        },
        value={"inch": 50},
        provenance=({"lifecycle_status": "ACTIVE", "source_status": "VERIFIED"},),
        expected_model="LH50BEHHLGFXKR",
        row_model="2026 LED 4K (LH50BEHHLGFXKR) 스탠드",
        collection_status="COLLECTION_SUCCESS",
    ) == expected_reason


def test_unverified_provenance_is_still_refused():
    assert ProductKnowledgeService._exclusion_reason(
        row={
            "lifecycle_status": "ACTIVE", "verification_status": "VERIFIED",
            "resolution_status": "SINGLE_SOURCE", "volatility": "STABLE",
            "field": "screen_size", "selected_value_id": "v1",
        },
        value={"inch": 50},
        provenance=({"lifecycle_status": "ACTIVE", "source_status": "PENDING"},),
        expected_model="LH50BEHHLGFXKR",
        row_model="2026 LED 4K (LH50BEHHLGFXKR) 스탠드",
        collection_status="COLLECTION_SUCCESS",
    ) == "PROVENANCE_NOT_VERIFIED"


# ===========================================================================
# 3. Return / exchange / damage is never auto-posted
# ===========================================================================

def _verdict(actions, *, unresolved=(), route="GPT_FALLBACK"):
    metadata = {
        "selected_answer_route": route,
        "semantic_routing": {
            "understanding": {
                "usable": True,
                "questions": [
                    {"text": "질문 %d" % index, "action": action}
                    for index, action in enumerate(actions, start=1)
                ],
            },
        },
        "hybrid": {
            "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
            "subquestion_evidence": [
                {"subquestion": "질문 %d" % index, "status": "CANDIDATE",
                 "source": "ACTIVE_POSITIVE_LEARNING",
                 "learning_ids": [500 + index], "historical_case_ids": []}
                for index, _ in enumerate(actions, start=1)
            ],
            "draft": {
                "learning_usage": [], "requires_review": bool(unresolved),
                "missing_information": [], "unresolved": list(unresolved),
                "can_auto_post": not unresolved,
                "used_learning_ids": [501],
            },
            "self_review": {"requires_review": False},
        },
    }
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "metadata_json": metadata,
            "original_answer": "안내드립니다. 단순 변심 반품은 가능합니다.",
            "validation_status": "PASS",
        },
        route=route,
    )


def test_a_fully_resolved_return_answer_is_still_held_for_staff():
    """The policy cannot depend on the model having withheld something.

    688393266 was held because GPT ② reported both parts unresolved. Retrieve
    enough about returns and it would have resolved them, and nothing in the
    pipeline would then have stopped the automatic post.
    """

    result = _verdict(["CANCEL_RETURN", "CANCEL_RETURN"])
    assert result.decision == "REVIEW_REQUIRED"
    assert RETURN_OR_DAMAGE_POLICY_REVIEW in result.reasons


def test_a_damage_report_is_held_on_the_same_terms():
    result = _verdict(["DAMAGE_REPORT"])
    assert RETURN_OR_DAMAGE_POLICY_REVIEW in result.reasons
    assert result.decision == "REVIEW_REQUIRED"


def test_one_policy_part_of_a_compound_inquiry_is_enough_to_hold_it():
    result = _verdict(["PRODUCT_SPEC", "CANCEL_RETURN"])
    assert RETURN_OR_DAMAGE_POLICY_REVIEW in result.reasons


def test_the_hold_is_hard_and_cannot_be_relaxed_as_telemetry():
    assert RETURN_OR_DAMAGE_POLICY_REVIEW not in SOFT_REASONS


@pytest.mark.parametrize("actions", [
    ["PRODUCT_SPEC"], ["INSTALLATION_METHOD", "INSTALLATION_METHOD"],
    ["COLLECTION"], ["DELIVERY_POLICY"], ["REPAIR"], ["BENEFIT"],
])
def test_an_ordinary_inquiry_is_not_caught_by_the_policy(actions):
    """Read from the understanding, not the wording.

    The draft body in this fixture contains "반품" throughout. A keyword gate
    would hold every one of these; the understanding says what was asked.
    """

    result = _verdict(actions)
    assert RETURN_OR_DAMAGE_POLICY_REVIEW not in result.reasons
    assert result.decision == "SAFE", result.reasons


def test_the_policy_actions_are_the_understanding_stages_own_labels():
    from services.semantic_analysis import ACTIONS

    assert POLICY_STAFF_ONLY_ACTIONS <= ACTIONS


def test_a_draft_with_no_understanding_record_is_not_policy_held_by_guesswork():
    """No understanding means no basis for this particular hold. The draft is
    still held -- UNDERSTANDING_UNAVAILABLE does that -- but not by a policy
    claim nothing established."""

    metadata = {
        "selected_answer_route": "GPT_FALLBACK",
        "hybrid": {"answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                   "draft": {"unresolved": [], "can_auto_post": True}},
    }
    result = AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={"metadata_json": metadata, "original_answer": "반품 안내입니다.",
               "validation_status": "PASS"},
        route="GPT_FALLBACK",
    )
    assert RETURN_OR_DAMAGE_POLICY_REVIEW not in result.reasons


# ===========================================================================
# 4. REQUEST_ORDER_ID no longer bypasses retrieval and the answer step
# ===========================================================================

def _order_id_request_analysis() -> dict:
    from answer.inquiry_analysis import (
        AnswerStrategy, InquiryAnalysis, InquiryType, OrderIdStatus,
    )

    return InquiryAnalysis(
        inquiry_type=InquiryType.ORDER_INFO_REQUIRED,
        inquiry_subtype=None,
        answer_strategy=AnswerStrategy.REQUEST_ORDER_ID,
        order_id_status=OrderIdStatus.MISSING,
        requires_order_lookup=False,
        requires_dps_lookup=False,
        requires_order_id=True,
        order_id_present=False,
        order_id_validated=False,
        selected_fact_keys=(),
        reasons=(),
        manual_review_required=False,
        auto_answerable=False,
        confidence=0.9,
    ).to_dict()


def _hybrid_run(*, strategy_analysis: dict | None):
    """One generation through the real HybridAnswerService."""

    from answer.models import AnswerRequest, AnswerResult, AnswerStatus
    from answer.providers.fake_gpt_provider import FakeGptProvider
    from services.hybrid_answer_service import HybridAnswerService

    calls: list[str] = []

    class _Provider(FakeGptProvider):
        name = "p2b-provider"

        def generate_json(self, *, task, prompt, context):
            calls.append(str(task).upper())
            if str(task).upper() == "DRAFT":
                return {
                    "answer": (
                        "안녕하세요, 고객님.\n주문번호를 알려주시면 확인해 드리겠습니다.\n"
                        "감사합니다."
                    ),
                    "confidence": 0.9, "used_facts": [],
                    "missing_information": [], "requires_review": False,
                    "warnings": [],
                }
            return super().generate_json(task=task, prompt=prompt, context=context)

    retrieved: list[int] = []

    def learning_context(_facts, _intent, **_kwargs):
        retrieved.append(1)
        return {"similar_approved_answers": [], "subquestion_evidence": []}

    request = AnswerRequest(
        inquiry_id=1, question_id="P2B", inquiry_type="PRODUCT_INQUIRY",
        question="제가 주문한 상품 언제 배송되나요?",
        product_name="삼성 스마트모니터 M5",
        metadata={
            "source_type": "PRODUCT_INQUIRY",
            "dps": {"lookup_required": False, "lookup_status": "NOT_REQUIRED",
                    "warnings": []},
        },
    )
    if strategy_analysis is not None:
        request.metadata["phase9_analysis"] = strategy_analysis
    rule = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW, category="주문확인",
        reason="주문번호 필요", answer="일반 주문번호가 필요합니다.",
        provider="rules", auto_answerable=False, needs_review=True,
        matched_rule="주문번호요청",
    )
    outcome = HybridAnswerService(
        _Provider(),
        learning_context_provider=learning_context,
        legacy_evidence_verification=False,
    ).generate(request, rule)
    return outcome, calls, retrieved


def test_the_order_id_strategy_no_longer_skips_retrieval_and_the_model():
    """The failure-only bypass. Measured on 688393266's shape.

    With the understanding stage healthy the strategy is withdrawn upstream, so
    this branch only ever fired when nothing had understood the inquiry -- and
    then it emptied the learning context, skipped the provider entirely and
    handed out the rule body as the draft.
    """

    outcome, calls, retrieved = _hybrid_run(
        strategy_analysis=_order_id_request_analysis(),
    )
    assert retrieved, "retrieval was skipped"
    assert "DRAFT" in calls, calls
    # Whatever the validator then decides, the one thing that must not happen
    # is the deterministic rule body being handed out as the generated answer.
    assert "일반 주문번호가 필요합니다." not in str(outcome.result.answer)


def test_an_ordinary_inquiry_is_unchanged_by_the_removal():
    outcome, calls, retrieved = _hybrid_run(strategy_analysis=None)
    assert retrieved
    assert "DRAFT" in calls
    assert outcome.result.answer.strip()
    assert outcome.fallback_used is False


def test_the_strategy_is_no_longer_read_inside_semantic_generation():
    """There is no branch left for it to select."""

    import inspect

    import services.hybrid_answer_service as module

    source = inspect.getsource(module)
    assert "AnswerStrategy.REQUEST_ORDER_ID" not in source
