"""Golden scenarios for what may and may not ground a factual claim.

Two families of rule are pinned here.

``LG-*`` covers the evidence policy itself: which retrieved text is allowed to
prove a product claim. Every one of these was reachable in production -- the
operational store holds 521 style-only rows, 47 rows carrying an internal
redaction token, and approved rows whose body defers rather than states.

``SA-*`` covers what staff approval means. Approval and editing are different
acts: an operator who reads a generated answer, changes nothing and approves
it has approved it just as much as one who rewrote it first. Editing is
provenance, never a precondition for evidence -- while approval on its own is
also not sufficient, because an approved answer that says "가능할 것으로
보입니다" is still a person declining to commit.
"""
from __future__ import annotations

from typing import Any

import pytest

from services.hybrid_answer_service import HybridAnswerService
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)
from services.learning_evidence_policy import (
    contamination_reason,
    evaluate,
    hedge_reason,
    is_hedged,
    usable_as_factual_evidence,
)
from services.product_knowledge_service import ProductFact, ProductKnowledgeResult


MODEL_A = "LH43BEFHLGFXKR"
MODEL_B = "LH50BEFHLGFXKR"
PRODUCT_A = "삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEFHLGFXKR 스탠드형"
PRODUCT_B = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
PID_A = "13239109816"
PID_B = "13239109999"

STAND_QUESTION = "기본 스탠드 다리 분리 가능한가요?"
STAND_ANSWER = "네, 기본 스탠드는 탈부착 가능합니다."

# The company template every generated answer is wrapped in. Its closing
# contains "확인 후", which is why hedge detection must read the body.
WRAPPED = (
    "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
    "{body}\n\n"
    "안내드린 내용이 문의하신 내용과 다른 경우,\n"
    "네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다.\n\n"
    "감사합니다."
)


def wrapped(body: str) -> str:
    return WRAPPED.format(body=body)


def fact(field_key: str, value: Any, unit: str | None = None) -> ProductFact:
    return ProductFact(
        product_id=PID_A, listing_id=f"listing_{PID_A}", model_code=MODEL_A,
        field_key=field_key, value=value, raw_value=value, unit=unit,
        scope="PRODUCT", scope_key=PID_A, component_scope="BASE",
        volatility="STABLE", verification_status="VERIFIED",
        resolution_status="RESOLVED", lifecycle_status="ACTIVE",
        canonical_fact_id=f"fact-{field_key}", value_id=f"value-{field_key}",
        safe_for_answer=True,
    )


def knowledge(*facts: ProductFact) -> ProductKnowledgeResult:
    return ProductKnowledgeResult(
        product_id=PID_A, listing_id=f"listing_{PID_A}", matched=True,
        requested_fields=tuple(item.field_key for item in facts),
        safe_facts=facts,
    )


def learning_item(
    *,
    answer: str = STAND_ANSWER,
    question: str = STAND_QUESTION,
    approved: bool = True,
    product_match: str = "EXACT_PRODUCT",
    answer_support: float = 0.9,
    learning_id: int = 1,
) -> dict[str, Any]:
    return {
        "learning_example_id": learning_id,
        "authority": "APPROVED" if approved else "AUTO",
        "answer": answer,
        "question": question,
        "answer_support": answer_support,
        "matched_subquestion": question,
        "compatibility": {"product_match": product_match},
    }


def decide(
    items: list[dict[str, Any]],
    *,
    facts: tuple[ProductFact, ...] = (),
    question: str = STAND_QUESTION,
    status: str = "ANSWERABLE",
) -> Any:
    return evaluate(
        learning_context={
            "similar_approved_answers": items,
            "subquestion_evidence": [
                {
                    "subquestion": question,
                    "status": status,
                    "evidence_coverage": "SUPPORTED",
                    "source": "ACTIVE_POSITIVE_LEARNING",
                }
            ],
        },
        safe_facts=facts,
    )


def grounding_corpus(**context: Any) -> str:
    return HybridAnswerService._evidence_texts(context)


# ==========================================================================
# LG-01 .. LG-15
# ==========================================================================


def test_lg01_same_product_staff_approved_definite_is_factual() -> None:
    """Same product_id, approved, on point, definite, undisputed."""

    verdict = decide([learning_item()])

    assert verdict.usable is True
    assert verdict.reason == "APPROVED_LEARNING_SUPPORTED"


def test_lg02_same_model_different_listing_is_factual() -> None:
    """A different listing of the same proven model is the same product."""

    verdict = decide([learning_item(product_match="EXACT_MODEL")])

    assert verdict.usable is True


def test_lg03_other_model_learning_is_never_factual() -> None:
    compatibility = LearningCompatibilityService().evaluate(
        current_question=STAND_QUESTION,
        current_product=extract_product_identity(
            product_id=PID_A, product_name=PRODUCT_A
        ),
        candidate_question=STAND_QUESTION,
        candidate_answer=STAND_ANSWER,
        candidate_product=extract_product_identity(
            product_id=PID_B, product_name=PRODUCT_B
        ),
        authority="APPROVED",
    )

    assert compatibility.eligible is False
    assert compatibility.reject_reason in {
        "PRODUCT_VARIANT_MISMATCH", "MODEL_MISMATCH",
    }
    # And even if such an item were handed to the policy directly, the
    # product_match verdict it carries keeps it out of evidence.
    assert decide([learning_item(product_match="POLICY_COMPATIBLE")]).usable is False


def test_lg04_style_only_never_grounds_a_factual_claim() -> None:
    """The prompt says these are not facts; the validator must agree.

    Before this, the deterministic grounding corpus admitted style-only text,
    so an unreviewed harvested answer could prove a claim two other layers
    had already refused.
    """

    corpus = grounding_corpus(
        seller_style_examples=[
            {"answer": "HDMI 단자는 3개입니다.", "question": "HDMI 몇 개인가요?"}
        ]
    )

    assert "3개" not in corpus
    assert usable_as_factual_evidence({"style_only": 1, "answer": "HDMI 단자는 3개입니다."}) is False


def test_lg04b_approved_learning_still_grounds() -> None:
    """Narrowing the corpus must not empty it."""

    corpus = grounding_corpus(
        similar_approved_answers=[
            {"answer": STAND_ANSWER, "question": STAND_QUESTION}
        ]
    )

    assert "탈부착" in corpus


def test_lg05_hedged_answer_never_grounds_a_definite_claim() -> None:
    assert is_hedged("사용 가능할 것으로 보입니다.") is True
    assert decide([learning_item(answer="사용 가능할 것으로 보입니다.")]).usable is False
    corpus = grounding_corpus(
        similar_approved_answers=[{"answer": "지원 가능할 것으로 보입니다."}]
    )
    assert corpus == ""


def test_lg06_redaction_token_learning_is_excluded_everywhere() -> None:
    contaminated = "문의는 <masked-phone>로 연락 주세요."

    assert contamination_reason(contaminated) == "<masked-phone>"
    assert usable_as_factual_evidence({"answer": contaminated}) is False
    assert grounding_corpus(similar_approved_answers=[{"answer": contaminated}]) == ""


def test_lg06b_contaminated_candidate_is_dropped_from_retrieval() -> None:
    """It must not reach the prompt either -- that is how it reached a customer."""

    from services.similar_answer_service import SimilarAnswerService

    class _Repo:
        def candidates(self, **_: Any) -> list[dict[str, Any]]:
            return [
                {
                    "id": 1, "learning_source": "SELLER_ANSWER",
                    "question_original_masked": STAND_QUESTION,
                    "question_normalized": STAND_QUESTION,
                    "final_answer": "문의는 <masked-phone>로 연락 주세요.",
                    "rating": 5, "style_only": 0, "created_at": "2026-01-01",
                    "metadata_json": {}, "inquiry_type": "PRODUCT_INQUIRY",
                    "model_code": None, "product_name": PRODUCT_A,
                }
            ]

        def candidate_diagnostics(self, **_: Any) -> dict[str, int]:
            return {
                "active_candidates": 1, "filtered_by_validity": 0,
                "revoked": 0, "negative_excluded": 0,
            }

    service = SimilarAnswerService(_Repo())
    selected = service.search(STAND_QUESTION, product_name=PRODUCT_A)

    assert selected == []
    counts = service.last_trace["rejection_counts"]
    assert counts["REDACTION_TOKEN_CONTAMINATED"] == 1


def test_lg07_conflicting_approved_learning_blocks() -> None:
    verdict = decide([
        learning_item(answer="기본 스탠드는 탈부착 가능합니다.", learning_id=1),
        learning_item(answer="기본 스탠드는 탈부착이 불가능합니다.", learning_id=2),
    ])

    assert verdict.usable is False
    assert verdict.conflict is True
    assert verdict.reason == "APPROVED_LEARNING_CONFLICT"


def test_lg08_product_fact_agreeing_with_learning_is_usable() -> None:
    verdict = decide(
        [learning_item(answer="HDMI 단자는 3개입니다.", question="HDMI 단자가 몇 개인가요?")],
        facts=(fact("hdmi_port_count", 3, "개"),),
        question="HDMI 단자가 몇 개인가요?",
    )

    assert verdict.conflict is False
    assert verdict.usable is True


def test_lg09_product_fact_contradicting_learning_blocks() -> None:
    verdict = decide(
        [learning_item(answer="HDMI 단자는 2개입니다.", question="HDMI 단자가 몇 개인가요?")],
        facts=(fact("hdmi_port_count", 3, "개"),),
        question="HDMI 단자가 몇 개인가요?",
    )

    assert verdict.usable is False
    assert verdict.conflict is True
    assert verdict.reason == "PRODUCT_FACT_VS_LEARNING_CONFLICT"


def test_lg10_retrieved_but_unrelated_learning_is_not_evidence() -> None:
    """Retrieval finding something is not the same as it answering this."""

    verdict = decide([learning_item(answer_support=0.1)])

    assert verdict.usable is False
    assert verdict.reason == "NO_QUALIFYING_APPROVED_LEARNING"


def test_lg11_same_model_stand_learning_grounds_without_product_fact() -> None:
    """The Product DB has no stand_detachable row; approval is the evidence."""

    verdict = decide([learning_item()], facts=())

    assert verdict.usable is True
    assert 1 in verdict.learning_ids


def test_lg12_other_size_stand_learning_is_rejected_by_compatibility() -> None:
    compatibility = LearningCompatibilityService().evaluate(
        current_question=STAND_QUESTION,
        current_product=extract_product_identity(product_name=PRODUCT_A),
        candidate_question=STAND_QUESTION,
        candidate_answer=STAND_ANSWER,
        candidate_product=extract_product_identity(product_name=PRODUCT_B),
        authority="APPROVED",
    )

    assert compatibility.eligible is False


def test_lg13_conflicting_airplay_learning_blocks() -> None:
    verdict = decide(
        [
            learning_item(
                answer="에어플레이를 지원합니다.",
                question="에어플레이 지원되나요?", learning_id=1,
            ),
            learning_item(
                answer="에어플레이는 지원하지 않습니다.",
                question="에어플레이 지원되나요?", learning_id=2,
            ),
        ],
        question="에어플레이 지원되나요?",
    )

    assert verdict.usable is False
    assert verdict.conflict is True


def test_lg14_verified_hdmi_fact_answers_its_own_question() -> None:
    class _Request:
        metadata = {"product_knowledge": knowledge(fact("hdmi_port_count", 3, "개"))}

    item = {
        "subquestion": "이 제품 HDMI 단자가 몇 개 있나요?",
        "status": "NO_RELIABLE_SOURCE",
        "evidence_coverage": "UNSUPPORTED",
        "source": None,
        "answer_required": False,
    }
    HybridAnswerService._apply_product_fact_evidence(
        _Request(), {"subquestion_evidence": [item]}
    )

    assert item["status"] == "ANSWERABLE"
    assert item["source"] == "VERIFIED_PRODUCT_FACT"
    assert item["product_fact_fields"] == ["hdmi_port_count"]


def test_lg15_unrelated_verified_fact_does_not_answer_the_question() -> None:
    """Screen size is verified; it says nothing about AirPlay."""

    class _Request:
        metadata = {
            "product_knowledge": knowledge(
                fact("screen_size", {"inch": 43}),
                fact("wifi_standard", "802.11ac"),
            )
        }

    item = {
        "subquestion": "이 모니터 아이폰 에어플레이 지원되나요?",
        "status": "NO_RELIABLE_SOURCE",
        "evidence_coverage": "UNSUPPORTED",
        "source": None,
        "answer_required": False,
    }
    HybridAnswerService._apply_product_fact_evidence(
        _Request(), {"subquestion_evidence": [item]}
    )

    assert item["status"] == "NO_RELIABLE_SOURCE"


# ==========================================================================
# SA-01 .. SA-08 -- what staff approval means
# ==========================================================================


def approved_learning_row(*, edited: bool, answer: str = STAND_ANSWER) -> dict[str, Any]:
    """A row shaped as ``LearningService.capture_approved`` writes it."""

    return {
        "learning_source": "APPROVED_EDITED" if edited else "APPROVED_UNEDITED",
        "style_only": 0,
        "active": 1,
        "final_answer": wrapped(answer),
        "metadata_json": {"human_verified": True, "facts_authority": "APPROVED_REFERENCE"},
    }


def authority_of(row: dict[str, Any]) -> str:
    """The authority retrieval derives, copied from SimilarAnswerService."""

    metadata = row.get("metadata_json") or {}
    return "APPROVED" if metadata.get("human_verified") is True else "AUTO"


@pytest.mark.parametrize("edited", [False, True])
def test_sa01_sa02_approval_is_what_counts_not_editing(edited: bool) -> None:
    """SA-01 / SA-02: unedited approval and edited approval are both approval."""

    row = approved_learning_row(edited=edited)

    assert authority_of(row) == "APPROVED"
    assert row["style_only"] == 0
    verdict = decide([learning_item(answer=row["final_answer"])])
    assert verdict.usable is True


def test_sa03_approved_but_other_model_is_not_factual() -> None:
    """SA-03: approval does not travel across models."""

    assert decide([
        learning_item(product_match="POLICY_COMPATIBLE")
    ]).usable is False


def test_sa04_approved_but_hedged_is_not_factual() -> None:
    """SA-04: approval is not a substitute for the writer committing."""

    row = approved_learning_row(edited=False, answer="사용 가능할 것으로 보입니다.")

    assert authority_of(row) == "APPROVED"          # approval is real
    assert is_hedged(row["final_answer"]) is True    # but the claim is not
    assert decide([learning_item(answer=row["final_answer"])]).usable is False


def test_sa05_approved_but_contradicting_a_verified_fact_blocks() -> None:
    verdict = decide(
        [learning_item(answer=wrapped("HDMI 단자는 2개입니다."),
                       question="HDMI 단자가 몇 개인가요?")],
        facts=(fact("hdmi_port_count", 3, "개"),),
        question="HDMI 단자가 몇 개인가요?",
    )

    assert verdict.usable is False
    assert verdict.conflict is True


def test_sa06_auto_harvested_naver_answer_is_not_staff_approved() -> None:
    """SA-06: synchronising an answer is not a person approving it."""

    harvested = {
        "learning_source": "SELLER_ANSWER",
        "style_only": 1,
        "metadata_json": {"facts_authority": "STYLE_ONLY"},
    }

    assert authority_of(harvested) == "AUTO"
    assert decide([learning_item(approved=False)]).usable is False


def test_sa07_reviewed_naver_answer_can_be_factual() -> None:
    """SA-07: the same row, once a person actually approved it."""

    reviewed = {
        "learning_source": "SELLER_ANSWER",
        "style_only": 0,
        "metadata_json": {
            "human_verified": True,
            "facts_authority": "HUMAN_VERIFIED_NAVER_POSTED",
        },
    }

    assert authority_of(reviewed) == "APPROVED"
    assert decide([learning_item()]).usable is True


def test_sa08_editing_alone_changes_no_eligibility_verdict() -> None:
    """SA-08: two approvals identical but for the edit flag must agree."""

    unedited = approved_learning_row(edited=False)
    edited = approved_learning_row(edited=True)

    assert authority_of(unedited) == authority_of(edited)
    assert is_hedged(unedited["final_answer"]) == is_hedged(edited["final_answer"])
    assert (
        decide([learning_item(answer=unedited["final_answer"])]).usable
        is decide([learning_item(answer=edited["final_answer"])]).usable
    )


# ==========================================================================
# The company template must not make every answer look uncertain
# ==========================================================================


def test_template_closing_does_not_make_an_answer_hedged() -> None:
    """The wrapper's "담당자가 확인 후 안내드리겠습니다" is not the claim.

    Reading the whole answer found "확인 후" in every answer this system has
    ever produced, so no generated-and-approved answer could be evidence --
    including flatly definite ones.
    """

    definite = wrapped("LS27D400 모델은 스피커가 내장되어 있지 않습니다.")

    assert hedge_reason(definite) is None
    assert is_hedged(definite) is False


def test_deferral_written_in_the_body_still_reads_as_deferral() -> None:
    deferral = wrapped("주문·배송 상태 확인이 필요합니다.")

    assert is_hedged(deferral) is True


@pytest.mark.parametrize(
    "body",
    [
        "사용 가능하실 것으로 보입니다.",
        "가능할 것 같습니다.",
        "될 것으로 예상됩니다.",
        "아마 가능합니다.",
        "정확하지 않지만 지원합니다.",
        "지원 여부는 확인이 필요합니다.",
        "제조사에 문의해 보시기 바랍니다.",
        "지원하는 것으로 알고 있습니다.",
        "사용이 어려울 수 있습니다.",
        "지원 가능성이 있습니다.",
    ],
)
def test_korean_uncertainty_variants_are_detected(body: str) -> None:
    assert is_hedged(wrapped(body)) is True


@pytest.mark.parametrize(
    "body",
    [
        "기본 스탠드는 탈부착 가능합니다.",
        "HDMI 단자는 3개입니다.",
        "스탠드를 제외한 본체 무게는 6.5kg입니다.",
        "이 제품은 미러링을 지원합니다.",
        "아이폰 미러링은 지원하지 않습니다.",
        "화면 크기는 43인치입니다.",
    ],
)
def test_definite_answers_are_not_flagged_uncertain(body: str) -> None:
    assert is_hedged(wrapped(body)) is False


# ==========================================================================
# A measurement is not a model code
# ==========================================================================


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("삼성 4K UHD 사이니지TV 214cm(85인치), 스탠드", None),
        ("삼성 삼탠바이미 43인치(107cm) 4K UHD 비즈니스TV", None),
        ("삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEFHLGFXKR 스탠드형", "LH43BEFHLGFXKR"),
    ],
)
def test_dimension_tokens_are_not_model_codes(name: str, expected: str | None) -> None:
    """"214CM" is stored as a model code for two different listings.

    Both would have matched as EXPLICIT_MODEL_CODE_MATCH -- the strongest
    identity verdict there is -- letting a stand or VESA fact cross between
    them.
    """

    from services.learning_compatibility_service import _model_code

    assert _model_code(None, None, name, None) == expected
