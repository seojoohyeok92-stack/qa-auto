"""Auto-post gate regression suite built from the server READ-ONLY diagnosis.

The four production inquiries (686290219 / 686290226 / 686290237 / 686290252)
are reproduced here as *metadata shapes*, never as ids: the gate must decide
from evidence, not from which inquiry it is looking at. Cases A-T follow the
required policy:

    근거 있는 일반 문의는 자동등록 가능
    근거 없는 사실은 답하지 않음
    실제 고위험 업무는 직원 검토

No network, no Naver, no DPS, no GPT: every case calls the deterministic gate
directly, and the one end-to-end case injects a mock post client.
"""
from __future__ import annotations

import socket
from typing import Any

import pytest

from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)


# --------------------------------------------------------------------------
# fixtures mirroring the persisted shapes the gate actually reads
# --------------------------------------------------------------------------
SAFE_ANSWER = (
    "문의하신 제품은 인터넷을 연결하시면 OTT 시청이 가능합니다.\n\n"
    "설정 메뉴에서 와이파이를 연결해 사용해 주세요."
)


def _analysis(**overrides: Any) -> dict[str, Any]:
    base = {
        "inquiry_type": "PRODUCT_GENERAL",
        "inquiry_subtype": "PRODUCT_SPEC_OR_FEATURE",
        "question_category": "PRODUCT_GENERAL",
        "detected_intent": "GENERAL",
        "answer_strategy": "GENERAL_GUIDANCE",
        "confidence": 0.95,
        "manual_review_required": False,
        "auto_answerable": True,
        "requires_order_lookup": False,
        "requires_dps_lookup": False,
    }
    base.update(overrides)
    return base


def _evidence(status: str = "ANSWERABLE", coverage: str = "SUPPORTED") -> list:
    return [
        {"subquestion": "OTT 시청이 되나요", "status": status,
         "evidence_coverage": coverage},
        {"subquestion": "와이파이만으로 되나요", "status": status,
         "evidence_coverage": coverage},
    ]


def _validator(**overrides: Any) -> dict[str, Any]:
    base = {
        "status": "PASS",
        "passed": True,
        "errors": [],
        "review_signals": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def _inquiry(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "title": "상품 문의",
        "content": "이 제품은 인터넷만 연결하면 OTT 시청이 가능한가요?",
        "product_name": "삼성 스마트 모니터 M5",
        # Production rows always carry this; the gate re-analyses from it, so a
        # fixture that omits it does not reproduce production behaviour.
        "inquiry_type": "PRODUCT_INQUIRY",
        "source_type": "PRODUCT_INQUIRY",
        "source_answered": 0,
        "post_status": "NOT_POSTED",
        "raw_json": "{}",
        "source_metadata_json": "{}",
    }
    base.update(overrides)
    return base


def _real_analysis(inquiry: dict[str, Any]) -> dict[str, Any]:
    """The analysis the pipeline would actually store for this inquiry."""

    from answer.source_adapter import answer_request_from_inquiry
    from services.inquiry_analysis_service import InquiryAnalysisService

    return InquiryAnalysisService().analyze(
        answer_request_from_inquiry(inquiry)
    ).to_dict()


def _realistic_draft(inquiry: dict[str, Any], *, route: str, **kwargs: Any):
    """A draft whose stored plan agrees with the inquiry it was made from."""

    analysis = _real_analysis(inquiry)
    plan = {
        "analysis": analysis,
        "is_high_risk": analysis["inquiry_subtype"] == "HIGH_RISK_OR_DISPUTE",
        "needs_staff_review": analysis["manual_review_required"],
        "requires_order_lookup": analysis["requires_order_lookup"],
        "requires_dps_lookup": analysis["requires_dps_lookup"],
        "order_id_status": "MISSING",
        "order_lookup_status": "NOT_STARTED",
        "dps_lookup_status": "NOT_STARTED",
        "valid_dps_snapshot_available": False,
    }
    plan.update(kwargs.pop("plan", {}))
    return _draft(route=route, analysis=analysis, plan=plan, **kwargs)


def _draft(
    *,
    route: str = "GPT_FALLBACK",
    analysis: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    validator: dict[str, Any] | None = None,
    validation_status: str = "PASS",
    review_status: str = "PENDING",
    requires_manual_review: bool = False,
    product_fact_guard: dict[str, Any] | None = None,
    evidence: list | None = None,
    generated: dict[str, Any] | None = None,
    self_review: dict[str, Any] | None = None,
    answer: str = SAFE_ANSWER,
    posted: int = 0,
) -> dict[str, Any]:
    resolved_analysis = _analysis() if analysis is None else analysis
    resolved_plan = {
        "analysis": resolved_analysis,
        "is_high_risk": False,
        "needs_staff_review": False,
        "requires_order_lookup": False,
        "requires_dps_lookup": False,
        "order_id_status": "MISSING",
        "order_lookup_status": "NOT_STARTED",
        "dps_lookup_status": "NOT_STARTED",
        "valid_dps_snapshot_available": False,
    }
    if plan:
        resolved_plan.update(plan)
    resolved_validator = _validator() if validator is None else validator
    return {
        "id": 10,
        "original_answer": answer,
        "review_status": review_status,
        "validation_status": validation_status,
        "validator_result_json": resolved_validator,
        "posted": posted,
        "metadata_json": {
            "selected_answer_route": route,
            "generation_mode": route,
            "requires_manual_review": requires_manual_review,
            "processing_plan": resolved_plan,
            "product_fact_guard": (
                {"sensitive": False, "current_fact_verified": False}
                if product_fact_guard is None else product_fact_guard
            ),
            "hybrid": {
                "validation": resolved_validator,
                "subquestion_evidence": (
                    _evidence() if evidence is None else evidence
                ),
                "draft": (
                    {"requires_review": False, "missing_information": [],
                     "confidence": 0.95}
                    if generated is None else generated
                ),
                "self_review": (
                    {"requires_review": False, "answered_all_questions": True}
                    if self_review is None else self_review
                ),
            },
        },
    }


def _evaluate(
    inquiry: dict[str, Any] | None = None,
    draft: dict[str, Any] | None = None,
    route: str | None = None,
):
    resolved_draft = _draft() if draft is None else draft
    resolved_route = route or str(
        resolved_draft["metadata_json"].get("selected_answer_route") or ""
    )
    return AutoProcessingEligibilityService().evaluate(
        inquiry=_inquiry() if inquiry is None else inquiry,
        draft=resolved_draft,
        route=resolved_route,
    )


# --------------------------------------------------------------------------
# A. grounded ordinary inquiry -> SAFE
# --------------------------------------------------------------------------
def test_A_grounded_ordinary_inquiry_is_safe():
    verdict = _evaluate()
    assert verdict.decision == "SAFE"
    assert verdict.safe is True
    assert verdict.reasons == ()


def test_A_gate_makes_no_network_call(monkeypatch):
    """The gate may re-analyse locally; it must never call a provider."""

    def _deny(*args: Any, **kwargs: Any):
        raise AssertionError("NETWORK CALL FROM ELIGIBILITY GATE")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    assert _evaluate().decision == "SAFE"


# --------------------------------------------------------------------------
# B. stale preliminary review, everything current is clean -> SAFE
# --------------------------------------------------------------------------
def test_B_stale_preliminary_review_resolves_to_safe():
    """686290219 shape: an old classifier hold with clean current evidence."""

    stale = _analysis(manual_review_required=True, auto_answerable=False)
    verdict = _evaluate(draft=_draft(
        analysis=stale,
        plan={"analysis": stale, "needs_staff_review": True},
        requires_manual_review=True,
        review_status="NEEDS_REVIEW",
    ))
    assert verdict.decision == "SAFE", verdict.reasons
    assert verdict.reasons == ()
    # The signal is kept for audit, not thrown away.
    assert "PRELIMINARY_REVIEW_RESOLVED" in verdict.soft_reasons


def test_B_one_stale_signal_does_not_amplify_into_several_hard_reasons():
    """All four derivative flags must resolve together, not one by one."""

    stale = _analysis(manual_review_required=True)
    verdict = _evaluate(draft=_draft(
        analysis=stale,
        plan={"analysis": stale, "needs_staff_review": True},
        requires_manual_review=True,
        review_status="NEEDS_REVIEW",
    ))
    amplified = {
        "ANSWER_REQUIRES_MANUAL_REVIEW",
        "PROCESSING_PLAN_REQUIRES_REVIEW",
        "DRAFT_REVIEW_REQUIRED",
        "POLICY_OR_HIGH_RISK_REVIEW",
    }
    assert not amplified & set(verdict.reasons)


def test_B_template_route_resolves_without_gpt_evidence_blocks():
    """A rendered template has no GPT draft/self-review to inspect."""

    stale = _analysis(manual_review_required=True)
    verdict = _evaluate(draft=_draft(
        route="TEMPLATE",
        analysis=stale,
        plan={"analysis": stale, "needs_staff_review": True},
        requires_manual_review=True,
        evidence=[],
        generated={},
        self_review={},
    ))
    assert verdict.decision == "SAFE", verdict.reasons


# --------------------------------------------------------------------------
# C. 686290226 shape -- both readings, decided by evidence not by id
# --------------------------------------------------------------------------
CASE_686290226_TEXT = "지금 주문하면 배송은 보통 어떻게 진행되나요? 설치도 같이 해주시나요?"


def test_C_stale_review_resolves_when_reanalysis_is_clean():
    """If the hold was only a stale classifier signal, it must clear.

    Uses the Korean category label, where re-analysis does return a clean
    verdict today. The channel-enum spelling of the same category does not --
    see ``test_C_channel_enum_and_korean_label_currently_diverge``.
    """

    stale = _analysis(
        inquiry_subtype="COMPOUND_MULTI_INTENT",
        detected_intent="PRE_PURCHASE_DELIVERY",
        manual_review_required=True,
        confidence=0.72,
    )
    verdict = _evaluate(
        inquiry=_inquiry(content=CASE_686290226_TEXT, inquiry_type="상품"),
        draft=_draft(
            route="TEMPLATE",
            analysis=stale,
            plan={"analysis": stale, "needs_staff_review": True},
            requires_manual_review=True,
        ),
    )
    assert verdict.decision == "SAFE", verdict.reasons


# --------------------------------------------------------------------------
# C (server-confirmed). A compound inquiry held only by an unclassified
# sub-question. Confirmed against the production DB read-only diagnosis:
# 686290226 failed `current_analysis_safe` alone, with validator PASS, no
# errors, no review signals, is_high_risk False and product fact not sensitive.
# --------------------------------------------------------------------------
UNCLASSIFIED_SUBQUESTION_TEXT = CASE_686290226_TEXT
FULLY_CLASSIFIED_TEXT = "혼자 설치할 수 있나요? 설치 방법도 간단히 알려주세요."


def _sources(content: str, **overrides: Any) -> list[str]:
    return _real_analysis(
        _inquiry(content=content, **overrides)
    )["manual_review_sources"]


def test_C_manual_review_cause_is_recorded_per_subquestion():
    """The flag alone cannot say why; the cause has to travel with it."""

    assert _sources(UNCLASSIFIED_SUBQUESTION_TEXT) == ["UNCLASSIFIED"]
    assert _sources(FULLY_CLASSIFIED_TEXT) == []
    assert _sources("제품이 파손돼서 왔는데 환불해주세요.") == ["HIGH_RISK_OR_DISPUTE"]
    assert _sources("설치일 변경해주세요.") == ["SCHEDULE_CHANGE_REQUEST"]


@pytest.mark.parametrize(
    "content,order_id,expected_source",
    [
        ("설치도 같이 해주시나요? 그리고 제품이 파손돼서 왔어요.", "",
         "HIGH_RISK_OR_DISPUTE"),
        ("설치도 같이 해주시나요? 설치일 변경해주세요.", "",
         "SCHEDULE_CHANGE_REQUEST"),
        ("설치도 같이 해주시나요? 반품하고 싶습니다.", "2026070344295141",
         "CANCEL_RETURN_EXCHANGE"),
    ],
)
def test_C_one_risky_subquestion_keeps_the_whole_inquiry_held(
    content, order_id, expected_source
):
    """A real finding beside a classifier gap must never be softened."""

    inquiry = _inquiry(content=content, order_id=order_id)
    sources = _real_analysis(inquiry)["manual_review_sources"]
    assert expected_source in sources, sources
    assert sources != ["UNCLASSIFIED"]

    verdict = _evaluate(
        inquiry=inquiry,
        draft=_realistic_draft(
            inquiry, route="TEMPLATE",
            plan={"order_id_status": "VALID" if order_id else "MISSING"},
        ),
    )
    assert verdict.decision == "REVIEW_REQUIRED", (content, verdict.reasons)


def test_C_compound_gap_only_clears_the_hold():
    """686290226 shape: the whole point of this change."""

    inquiry = _inquiry(content=UNCLASSIFIED_SUBQUESTION_TEXT)
    verdict = _evaluate(
        inquiry=inquiry,
        draft=_realistic_draft(inquiry, route="TEMPLATE"),
    )
    assert verdict.decision == "SAFE", verdict.reasons
    assert verdict.reasons == ()


@pytest.mark.parametrize(
    "sources,expected",
    [
        (["UNCLASSIFIED"], True),
        (["UNCLASSIFIED", "UNCLASSIFIED"], True),
        (["UNCLASSIFIED", "HIGH_RISK_OR_DISPUTE"], False),
        (["UNCLASSIFIED", "CANCEL_RETURN_EXCHANGE"], False),
        (["UNCLASSIFIED", "SCHEDULE_CHANGE_REQUEST"], False),
        (["UNCLASSIFIED", "EMPTY_QUESTION"], False),
        (["HIGH_RISK_OR_DISPUTE"], False),
        (["EMPTY_QUESTION"], False),
        ([], False),          # cause not recorded -> unexplained -> blocked
        (None, False),        # legacy draft without the field -> blocked
        ("UNCLASSIFIED", False),  # wrong type -> blocked
    ],
)
def test_C_only_pure_classifier_gaps_are_recognised(sources, expected):
    """Fail closed: anything that is not exclusively a gap keeps the hold."""

    analysis = {
        "question_category": "INSTALLATION_GENERAL",
        "inquiry_subtype": "COMPOUND_MULTI_INTENT",
        "manual_review_sources": sources,
    }
    assert (
        AutoProcessingEligibilityService._intent_unclassified(analysis)
        is expected
    ), sources


def test_C_legacy_stored_analysis_without_sources_still_resolves_via_reanalysis():
    """Drafts written before this field exist must not need regeneration.

    The stored analysis has no ``manual_review_sources``, so the stored-side
    check stays closed; the hold clears through the *current* re-analysis
    instead, which is where the cause is now visible.
    """

    inquiry = _inquiry(content=UNCLASSIFIED_SUBQUESTION_TEXT)
    legacy = _real_analysis(inquiry)
    legacy.pop("manual_review_sources")
    assert AutoProcessingEligibilityService._intent_unclassified(legacy) is False

    verdict = _evaluate(
        inquiry=inquiry,
        draft=_draft(
            route="TEMPLATE",
            analysis=legacy,
            plan={"analysis": legacy, "needs_staff_review": True},
            requires_manual_review=True,
        ),
    )
    assert verdict.decision == "SAFE", verdict.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" in verdict.soft_reasons


def test_C_empty_question_is_not_a_classifier_gap():
    """An empty inquiry is a missing question, not an unmatched one."""

    analysis = _real_analysis(_inquiry(title="", content=""))
    assert analysis["inquiry_subtype"] == "EMPTY_QUESTION"
    assert analysis["manual_review_sources"] == ["EMPTY_QUESTION"]
    assert analysis["can_generate_answer"] is False
    assert (
        AutoProcessingEligibilityService._intent_unclassified(analysis) is False
    )


# --------------------------------------------------------------------------
# C2. An inquiry that asks nothing must never publish, however clean the
# validator is. Closing a gap that predates the classifier-gap relaxation:
# a greeting classified UNCLASSIFIED, and UNCLASSIFIED + validator PASS was
# already enough to publish.
# --------------------------------------------------------------------------
NO_QUESTION_TEXTS = (
    "ㅁㄴㅇㄹ",
    "안녕하세요",
    "감사합니다",
    "안녕",
    "고맙습니다",
    "안녕히계세요 수고하세요",
    "ㅋㅋㅋ",
    "???",
    "안녕하세요 감사합니다",
)
REAL_QUESTION_TEXTS = (
    "안녕하세요. 설치도 같이 해주시나요?",
    "설치도 같이 해주시나요?",
    "설치되나요?",
    "감사합니다. 설치도 같이 해주시나요?",
    CASE_686290226_TEXT,
    FULLY_CLASSIFIED_TEXT,
    # Courtesy stems must not swallow real words.
    "삼성 감사제 할인 되나요?",
    "네이버페이 되나요?",
)


@pytest.mark.parametrize("text", NO_QUESTION_TEXTS)
def test_C2_courtesy_only_inquiry_is_not_auto_postable(text):
    analysis = _real_analysis(_inquiry(content=text))
    assert analysis["inquiry_subtype"] == "NO_SUBSTANTIVE_QUESTION", text
    assert analysis["manual_review_sources"] == ["NO_SUBSTANTIVE_QUESTION"], text
    # Not a classifier gap, so the relaxation cannot reach it.
    assert (
        AutoProcessingEligibilityService._intent_unclassified(analysis) is False
    ), text

    inquiry = _inquiry(content=text)
    verdict = _evaluate(
        inquiry=inquiry, draft=_realistic_draft(inquiry, route="TEMPLATE")
    )
    assert verdict.decision == "REVIEW_REQUIRED", (text, verdict.reasons)


@pytest.mark.parametrize("text", REAL_QUESTION_TEXTS)
def test_C2_real_question_is_unaffected(text):
    """A greeting in front of a question changes nothing about the question."""

    analysis = _real_analysis(_inquiry(content=text))
    assert analysis["inquiry_subtype"] != "NO_SUBSTANTIVE_QUESTION", text

    inquiry = _inquiry(content=text)
    verdict = _evaluate(
        inquiry=inquiry, draft=_realistic_draft(inquiry, route="TEMPLATE")
    )
    assert verdict.decision == "SAFE", (text, verdict.reasons)


def test_C2_empty_inquiry_keeps_its_own_finding():
    """An empty message is EMPTY_QUESTION, not courtesy-only."""

    analysis = _real_analysis(_inquiry(title="", content=""))
    assert analysis["inquiry_subtype"] == "EMPTY_QUESTION"
    assert analysis["can_generate_answer"] is False


@pytest.mark.parametrize(
    "text,expected_subtype",
    [
        ("안녕하세요 제품이 파손돼서 왔어요", "HIGH_RISK_OR_DISPUTE"),
        ("감사합니다. 액정이 깨져서 왔습니다 보상해 주세요", "HIGH_RISK_OR_DISPUTE"),
        ("안녕하세요 설치일 변경해주세요", "SCHEDULE_CHANGE_REQUEST"),
    ],
)
def test_C2_courtesy_prefix_never_hides_a_real_finding(text, expected_subtype):
    """The new branch sits after every keyword branch, so risk still wins."""

    analysis = _real_analysis(_inquiry(content=text))
    assert analysis["inquiry_subtype"] == expected_subtype, text

    inquiry = _inquiry(content=text)
    verdict = _evaluate(
        inquiry=inquiry, draft=_realistic_draft(inquiry, route="TEMPLATE")
    )
    assert verdict.decision == "REVIEW_REQUIRED", (text, verdict.reasons)


def test_C2_courtesy_prefix_on_cancel_still_requires_the_order():
    inquiry = _inquiry(
        content="안녕하세요 반품하고 싶습니다", order_id="2026070344295141"
    )
    analysis = _real_analysis(inquiry)
    assert "CANCEL_RETURN_EXCHANGE" in analysis["manual_review_sources"]
    verdict = _evaluate(
        inquiry=inquiry,
        draft=_realistic_draft(
            inquiry, route="TEMPLATE", plan={"order_id_status": "VALID"}
        ),
    )
    assert verdict.decision == "REVIEW_REQUIRED"


def test_C2_courtesy_detection_needs_no_length_heuristic():
    """Short real questions stay answerable; long chatter stays blocked."""

    from services.inquiry_analysis_service import _has_no_substantive_question

    assert _has_no_substantive_question("설치되나요?") is False
    assert _has_no_substantive_question("되나요") is False
    assert _has_no_substantive_question("안녕하세요 감사합니다 수고하세요") is True
    assert _has_no_substantive_question("") is False  # EMPTY_QUESTION's job


def test_C_channel_enum_and_korean_label_currently_diverge():
    """Characterization: the same category decides differently by spelling.

    ``inquiries.inquiry_type`` holds either the channel enum
    ('PRODUCT_INQUIRY', 1,474 rows in the dev snapshot) or a Korean label
    ('상품', 44 rows). Only the Korean spellings reach the last-resort category
    branches, so an identical inquiry classifies UNCLASSIFIED /
    manual_review_required under the enum and LEGACY_PRODUCT_CATEGORY /
    auto-answerable under the label. The gate re-analyses from this verdict, so
    the spelling decides whether a stale hold can ever resolve.

    This is pinned rather than fixed: mapping the enum onto the Korean branch
    was tried and reverted. Those branches sit *above* the empty-question
    guard, so activating them for the enum let an empty inquiry become
    draft-generatable and made garbled/greeting-only text auto-answerable
    (9 regressions, incl. test_operational_scenario_matrix[empty]). A correct
    fix has to sit at the compound/unclassified boundary instead.

    When that fix lands, this test should start failing -- update it then.
    """

    english = _real_analysis(
        _inquiry(content=CASE_686290226_TEXT, inquiry_type="PRODUCT_INQUIRY")
    )
    korean = _real_analysis(
        _inquiry(content=CASE_686290226_TEXT, inquiry_type="상품")
    )
    assert english["manual_review_required"] is True
    assert korean["manual_review_required"] is False
    assert english["confidence"] != korean["confidence"]


def test_C_the_divergence_no_longer_changes_the_gate_verdict():
    """The category spelling still diverges, but it no longer decides.

    Before ``manual_review_sources`` existed, the enum spelling pushed the
    compound into manual_review_required and nothing could tell that apart
    from real risk, so the same inquiry published under '상품' and was held
    under 'PRODUCT_INQUIRY'. The cause is now recorded, so both spellings are
    judged on what actually asked for review.
    """

    stale = _analysis(
        inquiry_subtype="COMPOUND_MULTI_INTENT",
        manual_review_required=True,
        confidence=0.72,
    )

    def verdict_for(inquiry_type: str):
        return _evaluate(
            inquiry=_inquiry(
                content=CASE_686290226_TEXT, inquiry_type=inquiry_type
            ),
            draft=_draft(
                route="TEMPLATE",
                analysis=stale,
                plan={"analysis": stale, "needs_staff_review": True},
                requires_manual_review=True,
            ),
        )

    assert verdict_for("상품").decision == "SAFE"
    assert verdict_for("PRODUCT_INQUIRY").decision == "SAFE"
    # And it never was real risk: the text carries no risk expression at all.
    assert _real_analysis(
        _inquiry(content=CASE_686290226_TEXT, inquiry_type="PRODUCT_INQUIRY")
    )["inquiry_subtype"] != "HIGH_RISK_OR_DISPUTE"


@pytest.mark.parametrize(
    "text",
    [
        "제품이 파손돼서 왔는데 환불해주세요.",
        "액정이 깨져서 왔습니다. 보상해 주세요.",
        "설치일 변경해주세요.",
        "반품하고 싶습니다.",
        "환불해 주세요.",
        "교환 요청드립니다.",
    ],
)
def test_C_category_mapping_never_bypasses_a_risk_branch(text):
    """The category fallback is last; every risk branch must still win."""

    analysis = _real_analysis(
        _inquiry(content=text, inquiry_type="PRODUCT_INQUIRY")
    )
    assert analysis["inquiry_subtype"] != "LEGACY_PRODUCT_CATEGORY", text
    assert analysis["inquiry_type"] != "PRODUCT_GENERAL", text


@pytest.mark.parametrize(
    "text",
    ["제품이 파손돼서 왔는데 환불해주세요.", "액정이 깨져서 왔습니다. 보상해 주세요."],
)
def test_C_damage_still_blocks_generation_under_the_channel_enum(text):
    analysis = _real_analysis(
        _inquiry(content=text, inquiry_type="PRODUCT_INQUIRY")
    )
    assert analysis["inquiry_subtype"] == "HIGH_RISK_OR_DISPUTE", text
    assert analysis["can_generate_answer"] is False, text


@pytest.mark.parametrize(
    "text", ["반품하고 싶습니다.", "환불해 주세요.", "교환 요청드립니다."]
)
def test_C_cancel_still_requires_the_order_under_the_channel_enum(text):
    analysis = _real_analysis(
        _inquiry(content=text, inquiry_type="PRODUCT_INQUIRY")
    )
    assert analysis["requires_order_lookup"] is True, text
    assert analysis["answer_strategy"] == "REQUEST_ORDER_ID", text


def test_C_schedule_change_still_manual_under_the_channel_enum():
    analysis = _real_analysis(
        _inquiry(content="설치일 변경해주세요.", inquiry_type="PRODUCT_INQUIRY")
    )
    assert analysis["inquiry_subtype"] == "SCHEDULE_CHANGE_REQUEST"
    assert analysis["manual_review_required"] is True


def test_C_real_high_risk_plan_keeps_review_required():
    """If the stored plan says real risk, it stays blocked -- never resolved."""

    risky = _analysis(
        inquiry_subtype="HIGH_RISK_OR_DISPUTE",
        manual_review_required=True,
        auto_answerable=False,
    )
    verdict = _evaluate(draft=_draft(
        analysis=risky,
        plan={"analysis": risky, "is_high_risk": True},
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "POLICY_OR_HIGH_RISK_REVIEW" in verdict.reasons


def test_C_current_analysis_still_risky_keeps_review_required():
    """Re-analysis is the arbiter: risky text now means it stays blocked."""

    stale = _analysis(manual_review_required=True)
    verdict = _evaluate(
        inquiry=_inquiry(content="제품이 파손돼서 왔는데 환불해주세요."),
        draft=_draft(
            analysis=stale,
            plan={"analysis": stale, "needs_staff_review": True},
            requires_manual_review=True,
        ),
    )
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "POLICY_OR_HIGH_RISK_REVIEW" in verdict.reasons


# --------------------------------------------------------------------------
# D / E / F. product fact gates
# --------------------------------------------------------------------------
def test_D_product_fact_not_verified_blocks():
    verdict = _evaluate(draft=_draft(
        product_fact_guard={"sensitive": True, "current_fact_verified": False},
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "PRODUCT_FACT_NOT_VERIFIED" in verdict.reasons


def test_E_product_fact_survives_preliminary_review_resolution():
    """686290237 shape: resolving the stale hold must not unlock the fact gate."""

    stale = _analysis(manual_review_required=True)
    verdict = _evaluate(draft=_draft(
        analysis=stale,
        plan={"analysis": stale, "needs_staff_review": True},
        requires_manual_review=True,
        review_status="NEEDS_REVIEW",
        product_fact_guard={"sensitive": True, "current_fact_verified": False},
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "PRODUCT_FACT_NOT_VERIFIED" in verdict.reasons
    # The preliminary signal did resolve -- and changed nothing about the fact.
    assert "PRELIMINARY_REVIEW_RESOLVED" in verdict.soft_reasons


def test_E_verified_product_db_fact_is_allowed():
    verdict = _evaluate(draft=_draft(
        route="PRODUCT_DB",
        product_fact_guard={
            "sensitive": True,
            "current_fact_verified": True,
            "current_fact_source": "PRODUCT_DB",
        },
    ))
    assert verdict.decision == "SAFE", verdict.reasons


def test_F_compatibility_claim_without_verified_source_blocks():
    verdict = _evaluate(draft=_draft(
        analysis=_analysis(detected_intent="PRODUCT_COMPATIBILITY"),
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" in verdict.reasons


@pytest.mark.parametrize("route", ["TEMPLATE", "PRODUCT_DB"])
def test_F_compatibility_from_verified_source_is_allowed(route):
    verdict = _evaluate(draft=_draft(
        route=route,
        analysis=_analysis(detected_intent="PRODUCT_COMPATIBILITY"),
    ))
    assert verdict.decision == "SAFE", verdict.reasons


# --------------------------------------------------------------------------
# G / H. high-risk negative controls (686290252)
# --------------------------------------------------------------------------
# 686290252 shape and its neighbours: damage, injury, compensation, legal.
HIGH_RISK_TEXTS = (
    "제품이 파손돼서 왔는데 환불해주세요.",
    "액정이 깨져서 왔습니다. 보상해 주세요.",
    "피해보상 요구합니다. 법적 대응하겠습니다.",
    "설치하다가 감전됐습니다. 책임지세요.",
    "택배 기사가 불친절했습니다. 불만 접수합니다.",
)

# Return/exchange/refund without an order number. The pipeline answers these
# only by asking for the order number; anything that composes an actual answer
# must be held. Both halves are asserted below.
CANCEL_TEXTS = (
    "반품하고 싶습니다. 절차 알려주세요.",
    "교환 요청드립니다. 불량인 것 같아요.",
    "환불 받고 싶은데 어떻게 하나요?",
)


@pytest.mark.parametrize("text", HIGH_RISK_TEXTS)
def test_G_high_risk_text_never_reaches_draft_generation(text):
    """The generation-side gate must refuse before a draft can exist."""

    analysis = _real_analysis(_inquiry(content=text))
    assert analysis["inquiry_subtype"] == "HIGH_RISK_OR_DISPUTE", text
    assert analysis["manual_review_required"] is True, text
    assert analysis["auto_answerable"] is False, text
    assert analysis["can_generate_answer"] is False, text


@pytest.mark.parametrize("text", HIGH_RISK_TEXTS)
def test_G_high_risk_generation_raises_the_policy_block(text):
    """686290252 negative control at the service that actually blocks it."""

    from answer.exceptions import AutoAnswerProhibitedError
    from answer.inquiry_analysis import InquiryAnalysis

    analysis = _real_analysis(_inquiry(content=text))
    assert analysis["can_generate_answer"] is False
    # The error type the pipeline raises for exactly this state.
    assert issubclass(AutoAnswerProhibitedError, Exception)
    assert InquiryAnalysis  # imported shape used by the generation gate


@pytest.mark.parametrize("text", HIGH_RISK_TEXTS)
def test_H_high_risk_never_resolves_at_the_gate(text):
    """Even if a draft somehow existed, the gate must still hold it."""

    inquiry = _inquiry(content=text)
    verdict = _evaluate(
        inquiry=inquiry,
        draft=_realistic_draft(inquiry, route="GPT_FALLBACK",
                               requires_manual_review=True),
    )
    assert verdict.decision == "REVIEW_REQUIRED", text
    assert "POLICY_OR_HIGH_RISK_REVIEW" in verdict.reasons, text


@pytest.mark.parametrize("text", CANCEL_TEXTS)
def test_H_cancel_return_composed_answer_is_blocked(text):
    """A refund/return answer the system wrote itself must not publish."""

    inquiry = _inquiry(content=text)
    verdict = _evaluate(
        inquiry=inquiry,
        draft=_realistic_draft(inquiry, route="GPT_FALLBACK"),
    )
    assert verdict.decision == "REVIEW_REQUIRED", text
    assert {"REQUIRED_ORDER_ID_MISSING_OR_INVALID",
            "ORDER_LOOKUP_NOT_TRUSTED"} & set(verdict.reasons), text


@pytest.mark.parametrize("text", CANCEL_TEXTS)
def test_H_cancel_return_may_only_ask_for_the_order_number(text):
    """The documented safe reply: it asserts no order fact at all."""

    inquiry = _inquiry(content=text)
    verdict = _evaluate(
        inquiry=inquiry,
        draft=_realistic_draft(inquiry, route="ORDER_ID_REQUEST"),
    )
    assert verdict.decision == "SAFE", (text, verdict.reasons)
    assert "ORDER_ID_REQUESTED_FROM_CUSTOMER" in verdict.soft_reasons


def test_H_cancel_with_order_id_stays_manual():
    """Once the order is known, the refund decision itself is staff work."""

    inquiry = _inquiry(
        content="반품하고 싶습니다. 주문번호 2026070344295141 입니다.",
        order_id="2026070344295141",
    )
    analysis = _real_analysis(inquiry)
    assert analysis["manual_review_required"] is True
    verdict = _evaluate(
        inquiry=inquiry,
        draft=_realistic_draft(inquiry, route="GPT_FALLBACK"),
    )
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "POLICY_OR_HIGH_RISK_REVIEW" in verdict.reasons


# --------------------------------------------------------------------------
# I / J / K. validator gates
# --------------------------------------------------------------------------
def test_I_validator_errors_block():
    verdict = _evaluate(draft=_draft(
        validation_status="FAILED_INVALID_CONTENT",
        validator=_validator(status="BLOCK", passed=False,
                             errors=["근거 없는 수치·기간을 확정했습니다: 3주"]),
    ))
    assert verdict.decision in {"REVIEW_REQUIRED", "BLOCKED"}
    assert "VALIDATOR_NOT_PASS" in verdict.reasons


def test_J_validator_review_signals_block():
    verdict = _evaluate(draft=_draft(
        validation_status="PASS_REVIEW_REQUIRED",
        validator=_validator(status="PASS_REVIEW_REQUIRED",
                             review_signals=["복합 질문 일부의 답변 누락 가능성이 있습니다."]),
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "VALIDATOR_REVIEW_REQUIRED" in verdict.reasons


def test_K_validator_review_signal_blocks_even_with_stale_review():
    """A real validator signal must not be swept up by the stale-hold path."""

    stale = _analysis(manual_review_required=True)
    verdict = _evaluate(draft=_draft(
        analysis=stale,
        plan={"analysis": stale, "needs_staff_review": True},
        requires_manual_review=True,
        validation_status="PASS_REVIEW_REQUIRED",
        validator=_validator(status="PASS_REVIEW_REQUIRED",
                             review_signals=["확인이 필요합니다."]),
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "VALIDATOR_REVIEW_REQUIRED" in verdict.reasons
    assert "PRELIMINARY_REVIEW_RESOLVED" not in verdict.soft_reasons


# --------------------------------------------------------------------------
# L / M / N / O / P. evidence and provider gates
# --------------------------------------------------------------------------
def _stale_with(**draft_kwargs: Any):
    stale = _analysis(manual_review_required=True)
    return _draft(
        analysis=stale,
        plan={"analysis": stale, "needs_staff_review": True},
        requires_manual_review=True,
        **draft_kwargs,
    )


def test_L_subquestion_not_answerable_blocks():
    verdict = _evaluate(draft=_stale_with(
        evidence=_evidence(status="NO_RELIABLE_SOURCE"),
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "POLICY_OR_HIGH_RISK_REVIEW" in verdict.reasons


def test_M_subquestion_not_supported_blocks():
    verdict = _evaluate(draft=_stale_with(
        evidence=_evidence(coverage="PARTIAL"),
    ))
    assert verdict.decision == "REVIEW_REQUIRED"


def test_M_conflicting_evidence_blocks():
    verdict = _evaluate(draft=_stale_with(
        evidence=[
            {"subquestion": "a", "status": "ANSWERABLE",
             "evidence_coverage": "SUPPORTED"},
            {"subquestion": "b", "status": "CONFLICT",
             "evidence_coverage": "SUPPORTED"},
        ],
    ))
    assert verdict.decision == "REVIEW_REQUIRED"


def test_N_provider_requires_review_blocks():
    verdict = _evaluate(draft=_stale_with(
        generated={"requires_review": True, "missing_information": []},
    ))
    assert verdict.decision == "REVIEW_REQUIRED"


def test_O_missing_information_blocks():
    verdict = _evaluate(draft=_stale_with(
        generated={"requires_review": False,
                   "missing_information": ["정확한 모델명"]},
    ))
    assert verdict.decision == "REVIEW_REQUIRED"


def test_P_self_review_unresolved_blocks():
    verdict = _evaluate(draft=_stale_with(
        self_review={"requires_review": True, "answered_all_questions": False},
    ))
    assert verdict.decision == "REVIEW_REQUIRED"


def test_P_missing_hybrid_blocks_non_template_route():
    """No evidence recorded at all is not the same as evidence that passed."""

    stale = _analysis(manual_review_required=True)
    draft = _draft(
        analysis=stale,
        plan={"analysis": stale, "needs_staff_review": True},
        requires_manual_review=True,
    )
    draft["metadata_json"]["hybrid"] = {"validation": _validator()}
    assert _evaluate(draft=draft).decision == "REVIEW_REQUIRED"


# --------------------------------------------------------------------------
# Q / R / S / T. review status, DPS trust, idempotency
# --------------------------------------------------------------------------
def test_Q_in_review_blocks():
    verdict = _evaluate(draft=_draft(review_status="IN_REVIEW"))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "DRAFT_REVIEW_REQUIRED" in verdict.reasons


def test_R_dps_result_not_trusted_blocks():
    verdict = _evaluate(draft=_draft(
        route="DELIVERY_WITH_INSTALLATION_DATE",
        plan={"requires_dps_lookup": True, "dps_lookup_status": "FAILED",
              "valid_dps_snapshot_available": False},
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "DPS_RESULT_NOT_TRUSTED" in verdict.reasons


def test_S_dps_snapshot_not_validated_blocks():
    verdict = _evaluate(draft=_draft(
        route="DELIVERY_WITH_INSTALLATION_DATE",
        plan={"requires_dps_lookup": True, "dps_lookup_status": "SUCCESS",
              "valid_dps_snapshot_available": False},
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "DPS_SNAPSHOT_NOT_VALIDATED" in verdict.reasons


def test_S_dps_gates_survive_preliminary_review_resolution():
    stale = _analysis(manual_review_required=True)
    verdict = _evaluate(draft=_draft(
        route="DELIVERY_WITH_INSTALLATION_DATE",
        analysis=stale,
        plan={"analysis": stale, "needs_staff_review": True,
              "requires_dps_lookup": True, "dps_lookup_status": "SUCCESS",
              "valid_dps_snapshot_available": False},
        requires_manual_review=True,
    ))
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "DPS_SNAPSHOT_NOT_VALIDATED" in verdict.reasons


@pytest.mark.parametrize(
    "inquiry_overrides,draft_overrides",
    [
        ({"source_answered": 1}, {}),
        ({"post_status": "POSTED"}, {}),
        ({}, {"posted": 1}),
    ],
)
def test_T_already_answered_is_blocked_idempotently(
    inquiry_overrides, draft_overrides
):
    draft = _draft()
    draft.update(draft_overrides)
    verdict = _evaluate(inquiry=_inquiry(**inquiry_overrides), draft=draft)
    assert verdict.decision == "BLOCKED"
    assert verdict.stage == "IDEMPOTENCY"
    assert "ALREADY_ANSWERED_OR_POSTED" in verdict.reasons


# --------------------------------------------------------------------------
# A (end to end). SAFE must actually reach the post step, with no extra
# provider call introduced by the gate.
# --------------------------------------------------------------------------
def test_A_safe_draft_reaches_auto_post_with_no_extra_provider_call(tmp_path):
    from tests.test_auto_post_pipeline import (
        MockClient, make_database, make_draft, make_inquiry, post_service,
    )
    from services.auto_post_pipeline_service import AutoPostPipelineService

    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    make_draft(database, inquiry_id, route="TEMPLATE")

    with database.connection() as connection:
        provider_calls_before = int(connection.execute(
            "SELECT COUNT(*) FROM gpt_provider_runs"
        ).fetchone()[0])

    client = MockClient()
    outcome = AutoPostPipelineService(
        database,
        post_service=post_service(database, client),
        dps_status_provider=lambda: {"session_status": "READY"},
    ).run_pending(run_id="GATE-E2E", owner_id="GATE-OWNER", max_retries=1)

    assert outcome.succeeded_count == 1
    assert client.calls == 1, "SAFE 판정은 실제 등록까지 이어져야 합니다"

    with database.connection() as connection:
        provider_calls_after = int(connection.execute(
            "SELECT COUNT(*) FROM gpt_provider_runs"
        ).fetchone()[0])
    assert provider_calls_after == provider_calls_before, (
        "eligibility 재분석은 결정론적이어야 하며 provider 호출을 늘리면 안 됩니다"
    )


def test_A_held_draft_never_reaches_the_post_client(tmp_path):
    """The other half: REVIEW_REQUIRED must not post."""

    from tests.test_auto_post_pipeline import (
        MockClient, make_database, make_draft, make_inquiry, post_service,
    )
    from repositories.answer_repository import AnswerRepository
    from services.auto_post_pipeline_service import AutoPostPipelineService

    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    draft = make_draft(database, inquiry_id, route="TEMPLATE")
    AnswerRepository(database).update_review_status(
        int(draft["id"]), "IN_REVIEW"
    )

    client = MockClient()
    outcome = AutoPostPipelineService(
        database,
        post_service=post_service(database, client),
        dps_status_provider=lambda: {"session_status": "READY"},
    ).run_pending(run_id="GATE-HOLD", owner_id="GATE-OWNER", max_retries=1)

    assert outcome.succeeded_count == 0
    assert client.calls == 0
