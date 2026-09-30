"""A stored answer about another product is not this product's specification.

Two failures set the shape of this. 690027174 asked where a 27-inch monitor is
made and was given four Learning rows about a speaker, a monitor arm, a
resolution and a screen size -- every one of them carrying ``eligible=false``,
every one of them attached anyway. And a VESA question about 43BEH, whose record
holds no VESA fact of its own, was answered "200 x 200mm, M8 23-25mm" from
L247879: a row from a different listing whose own model code was null.

The cause was not missing machinery. ``PRODUCT_FACT_ACTIONS`` already told the
compatibility service that the customer had asked for a property of one product,
and the service already returned ``eligible=false``. Production ran with
``hard_conflicts_only=True``, which keeps an ineligible row so GPT ② can read it
with its origin labelled -- and that leniency, measured and right for the
questions it was built for, was being applied to specifications too.

So the leniency stops at specification questions, and nothing else changes. The
distinction matters because the other direction was measured as well: 688218182
asked who installs a wall mount and what it costs, and the store's own answer to
that question was once deleted for naming a different listing. Those rows stay.
"""

from __future__ import annotations

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.learning_compatibility_service import (
    GENERIC_TOPICS, classify_topics,
)
from services.learning_context_service import PRODUCT_FACT_ACTIONS, _why_selected
from services.product_fact_guard import classify_product_fact
from services.similar_answer_service import IDENTITY_REJECT_REASONS
from services.similar_answer_service import SimilarAnswerService

SPEC_QUESTION = "이 제품 베사홀 규격이 어떻게 되나요?"
ORIGIN_QUESTION = "27인치 모니터의 생산지가 어디인가요?"
INSTALL_QUESTION = "벽걸이 추가하면 설치비가 포함되나요? 삼성기사님이 설치해주시나요?"


@pytest.fixture
def store(tmp_path):
    """Two rows: one about another product's VESA, one about installation cost.

    Both name a different listing. Which of them may be used depends entirely on
    what the customer asked, which is the property under test.
    """

    import json

    database = Database(tmp_path / "learning.db")
    database.initialize()
    rows = [
        {
            "source_key": "other-listing-vesa",
            "learning_source": "APPROVED_UNEDITED",
            "question_original_masked": "벽걸이 규격이 어떻게 되나요?",
            "question_normalized": "상품 문의 벽걸이 규격이 어떻게 되나요",
            "store_code": "OJE_PLUS", "inquiry_type": "PRODUCT_INQUIRY",
            "intent": "상품",
            "final_answer": "베사규격 200X200 나사규격 M8 23-25mm 입니다",
            "rating": 5, "active": 1, "validity_active": 1,
            "validity_type": "PERMANENT",
            "product_name": "삼성 107.9cm(43인치) 4K UHD LH43B 스마트 비즈니스TV",
            "model_code": "",
            "metadata_json": json.dumps(
                {"human_verified": True, "product_scope": "MODEL",
                 "learning_signal_type": "POSITIVE",
                 "source_product_id": "99999999"}, ensure_ascii=False),
        },
        {
            "source_key": "other-listing-install-cost",
            "learning_source": "APPROVED_UNEDITED",
            "question_original_masked": "벽걸이추가만하면 설치비랑 다 포함되나요?",
            "question_normalized": "상품 문의 벽걸이추가만하면 설치비랑다포함되는건가요",
            "store_code": "OJE_PLUS", "inquiry_type": "PRODUCT_INQUIRY",
            "intent": "상품",
            "final_answer": (
                "벽걸이 추가하시게 될 경우 벽걸이용 브라켓이 함께 출고되며, "
                "설치비는 청구되지 않습니다. 삼성전자 기사님께서 설치까지 해드립니다."),
            "rating": 5, "active": 1, "validity_active": 1,
            "validity_type": "PERMANENT",
            "product_name": "삼성 125.7cm(50인치) 비즈니스TV",
            "model_code": "",
            "metadata_json": json.dumps(
                {"human_verified": True, "product_scope": "POLICY",
                 "learning_signal_type": "POSITIVE",
                 "source_product_id": "88888888"}, ensure_ascii=False),
        },
    ]
    with database.transaction() as connection:
        columns = {c[1] for c in connection.execute(
            "PRAGMA table_info(learning_examples)")}
        for row in rows:
            usable = {k: v for k, v in row.items() if k in columns}
            connection.execute(
                f"INSERT INTO learning_examples ({','.join(usable)})"
                f" VALUES ({','.join('?' for _ in usable)})",
                list(usable.values()))
    return database


def _search(store, question, *, action, queries, product_name):
    """Retrieve the way production does, from GPT ①'s action label.

    The flags are *computed* here, by the same two expressions
    ``LearningContextService`` uses, rather than passed in. An earlier version
    of this file handed ``product_fact_sensitive`` in by hand, and because the
    pipeline derives it as ``question_guard.sensitive OR action``, the test
    asserted a property the running code did not have: on 688218182 the guard
    said sensitive for an installation-cost question and every Learning row was
    removed, while this file went on passing.
    """

    guard = classify_product_fact(
        question, inquiry_type="PRODUCT_INQUIRY", product_name=product_name)
    product_fact_sensitive = guard.sensitive or action in PRODUCT_FACT_ACTIONS
    identity_enforced = action in PRODUCT_FACT_ACTIONS

    service = SimilarAnswerService(LearningRepository(store))
    selected = service.search(
        question, store_code="OJE_PLUS", product_name=product_name,
        inquiry_type="PRODUCT_INQUIRY", limit=5, hard_conflicts_only=True,
        product_fact_sensitive=product_fact_sensitive,
        identity_enforced=identity_enforced,
        semantic_goal={
            "customer_goal": action, "requested_information": question,
            "atomic_question": question, "all_atomic_questions": [],
            "retrieval_queries": queries,
            "order_evidence_required": False, "schedule_scoped": False,
        },
    )
    return selected, dict(service.last_trace or {})


TARGET = "삼성 107.9cm(43인치) UHD 4K 비즈니스 TV LH43BEHHLGFXKR 스탠드형"


def test_another_listings_spec_never_becomes_this_products_spec(store):
    """The 43BEH case. The row reads like an answer and is not one for this TV."""

    selected, trace = _search(
        store, SPEC_QUESTION, action="PRODUCT_SPEC",
        queries=["제품의 VESA 홀 규격에 대한 안내"], product_name=TARGET)

    assert selected == [], [item["final_answer"][:40] for item in selected]
    reasons = trace.get("rejection_counts") or {}
    assert any(key.startswith("FACTUAL_IDENTITY_MISMATCH")
               or key == "TOPIC_SCOPE_MISMATCH" for key in reasons), reasons


def test_the_same_row_is_kept_for_an_operational_question(store):
    """Identity mismatch alone never removes a row.

    688218182 is why: the store's own answer about who installs a wall mount and
    what it costs was being deleted for naming a different listing, and GPT ②
    was handed 해피콜 and 주문취소 instead.
    """

    selected, _trace = _search(
        store, INSTALL_QUESTION, action="INSTALLATION_METHOD",
        queries=["벽걸이 추가 시 설치비가 포함되는지에 대한 안내"],
        product_name="삼성 125.7cm(50인치) UHD 4K 비즈니스TV LH50BEFHLGFXKR 스탠드형")

    answers = " ".join(str(item["final_answer"]) for item in selected)
    assert "설치비는 청구되지 않습니다" in answers, [
        item["final_answer"][:40] for item in selected]


def test_a_spec_question_blocks_a_row_about_another_model(store):
    """What this gate guarantees: identity, not subject matter.

    The VESA row names a different model, and for a specification question that
    is enough to remove it. The reasons that mean "a different product" are
    ``IDENTITY_REJECT_REASONS``; everything else stays a ranking signal.
    """

    selected, trace = _search(
        store, SPEC_QUESTION, action="PRODUCT_SPEC",
        queries=["제품의 VESA 홀 규격에 대한 안내"], product_name=TARGET)

    assert selected == []
    blocked = [key for key in (trace.get("rejection_counts") or {})
               if key.startswith("FACTUAL_IDENTITY_MISMATCH")]
    assert blocked, trace.get("rejection_counts")
    for key in blocked:
        assert key.split(":", 1)[1] in IDENTITY_REJECT_REASONS, key


def test_a_category_difference_is_left_for_the_model_to_weigh(store):
    """Deferred to attribute-based selection, on purpose.

    690027174 asked a 27-inch monitor's origin and was offered a 43-inch TV's
    VESA size. The row is wrong, but what makes it wrong is that it answers a
    different *attribute* -- and the verdict available here is
    ``PRODUCT_CATEGORY_MISMATCH``, which fires on brand or category alone. That
    reason was hard-enforced in the first version of this gate and removed 400
    candidates on a single inquiry, so it is a ranking signal again.

    This test records the state rather than asserting the row is gone: it is
    PHASE 2's regression case, together with the review-points, SK Broadband,
    온누리 and DP-cable rows that survive the same way.
    """

    selected, _trace = _search(
        store, ORIGIN_QUESTION, action="PRODUCT_SPEC",
        queries=["27인치 모니터의 제조국 또는 생산지에 대한 안내"],
        product_name="삼성 삼탠바이미 스마트 모니터 M5 27인치(68cm) IPTV 화이트")

    # Neither row states this monitor's origin, so none of them is grounds for
    # an origin answer -- which is a judgement about the attribute asked for,
    # not about identity, and is why this is deferred.
    for item in selected:
        assert "생산지" not in str(item["final_answer"])
        assert "원산지" not in str(item["final_answer"])


# --- the gate must not fire where topic inference is unreliable ------------

@pytest.mark.parametrize("question", [
    "쓰던 티비 가져가 주시나요?",
    "누가 와서 달아주시는 건가요?",
    "고장나면 어디로 연락하나요?",
    "제가 직접 조립해야 하나요?",
])
def test_colloquial_operational_questions_are_not_topic_gated(question):
    """These carry no detectable topic, and their answers do.

    Applied to every question the topic rule took benchmark top-3 recall from
    88% to 83%, and every case it broke was one of these. A specification
    question does not have the problem: GPT ① has already named the property.
    """

    asked = {t for t in classify_topics(question) if t not in GENERIC_TOPICS}
    assert asked == set(), (question, asked)


def test_the_gate_only_applies_to_product_fact_actions():
    """The set is GPT ①'s own labelling, not a second classifier."""

    assert PRODUCT_FACT_ACTIONS == frozenset(
        {"PRODUCT_SPEC", "PRODUCT_CONCEPT", "PACKAGE_CONTENTS"})


# --- metadata says what happened -------------------------------------------

def test_why_selected_reports_the_verdict_not_a_constant():
    """It used to be one hardcoded string naming checks the row never faced.

    On 690027174 the trace said four rows passed
    "..._AND_PRODUCT_TOPIC_COMPATIBILITY" while each carried eligible=false.
    """

    eligible = _why_selected({
        "compatibility": {"eligible": True, "topic_match": "MATCH"},
        "answer_support_reason": "SEMANTIC_QUESTION_MATCH"})
    mismatched = _why_selected({
        "compatibility": {"eligible": False, "reject_reason": "MODEL_MISMATCH"}})

    assert "COMPATIBILITY_ELIGIBLE" in eligible
    assert "TOPIC_MATCH" in eligible
    assert eligible != mismatched
    assert "MODEL_MISMATCH" in mismatched
    assert "IDENTITY_UNVERIFIED" in mismatched
    # The old constant must not come back.
    for value in (eligible, mismatched):
        assert "ACTIVE_VALIDITY_AND_RELEVANCE_THRESHOLD" not in value


def test_why_selected_falls_back_without_inventing_a_check():
    assert _why_selected({}) == "RANKED_BY_RELEVANCE"


# --- the narrowed gate, reason by reason -----------------------------------
#
# Both halves of the trigger are computed the way production computes them:
# ``identity_enforced`` from GPT ①'s action alone, ``product_fact_sensitive``
# from that OR the keyword guard. The pairs below are the six the narrowing was
# specified against.

def _decide(current, candidate, answer, *, scope, action, question):
    """The compatibility verdict and what the narrowed gate does with it."""

    from services.learning_compatibility_service import (
        LearningCompatibilityService, extract_product_identity,
    )

    decision = LearningCompatibilityService().evaluate(
        current_question=question,
        current_product=extract_product_identity(product_name=current),
        candidate_question=question,
        candidate_answer=answer,
        candidate_product=extract_product_identity(product_name=candidate),
        candidate_metadata={"product_scope": scope,
                            "source_product_id": "99999999"},
        query_is_product_fact=(
            classify_product_fact(
                question, inquiry_type="PRODUCT_INQUIRY",
                product_name=current).sensitive
            or action in PRODUCT_FACT_ACTIONS),
    )
    enforced = action in PRODUCT_FACT_ACTIONS
    hardened = (
        enforced
        and str(decision.reject_reason or "") in IDENTITY_REJECT_REASONS
    )
    return decision, hardened


VESA_ANSWER = "베사규격 200X200 나사규격 M8 23-25mm 입니다"
SPEC_Q = "이 제품 베사홀 규격이 어떻게 되나요?"

_43BEH = "삼성 107.9cm(43인치) UHD 4K 비즈니스 TV LH43BEHHLGFXKR 스탠드형"
_LH43B = "삼성 107.9cm(43인치) 4K UHD LH43B 스마트 비즈니스TV"
_50BEF = "삼성 125.7cm(50인치) UHD 4K 비즈니스TV LH50BEFHLGFXKR 스탠드형"
_50ANY = "삼성 125.7cm(50인치) 비즈니스TV"
_27MON = "삼성 삼탠바이미 스마트 모니터 M5 27인치(68cm) IPTV 화이트"


def test_a_product_spec_question_hardens_a_model_mismatch():
    """A. The 43BEH case, by its reason rather than by its listing."""

    decision, hardened = _decide(
        _43BEH, _LH43B, VESA_ANSWER, scope="MODEL",
        action="PRODUCT_SPEC", question=SPEC_Q)

    assert decision.reject_reason == "MODEL_MISMATCH"
    assert hardened is True


def test_a_product_spec_question_hardens_a_variant_mismatch():
    """B. A different size of the same thing is a different product."""

    decision, hardened = _decide(
        _50BEF, _43BEH, VESA_ANSWER, scope="MODEL",
        action="PRODUCT_SPEC", question=SPEC_Q)

    assert decision.reject_reason == "PRODUCT_VARIANT_MISMATCH"
    assert hardened is True


def test_a_product_spec_question_hardens_insufficient_identity():
    """C. No identity established is not the same as a match."""

    decision, hardened = _decide(
        _50BEF, _50ANY, VESA_ANSWER, scope="POLICY",
        action="PRODUCT_SPEC", question=SPEC_Q)

    assert decision.reject_reason == "INSUFFICIENT_PRODUCT_IDENTITY"
    assert hardened is True


def test_a_category_verdict_is_never_hardened():
    """D. Brand or category alone is not a statement about this product."""

    decision, hardened = _decide(
        _27MON, _LH43B, VESA_ANSWER, scope="MODEL",
        action="PRODUCT_SPEC", question=SPEC_Q)

    assert decision.reject_reason == "PRODUCT_CATEGORY_MISMATCH"
    assert hardened is False


def test_an_operational_question_never_hardens_an_identity_verdict():
    """E. 688218182: the same row, the same verdict, a different question."""

    decision, hardened = _decide(
        _50BEF, _50ANY,
        "벽걸이용 브라켓이 함께 출고되며, 설치비는 청구되지 않습니다.",
        scope="POLICY", action="INSTALLATION_METHOD",
        question="벽걸이 추가하면 설치비가 포함되나요?")

    assert decision.reject_reason in IDENTITY_REJECT_REASONS
    assert hardened is False


def test_a_delivery_question_never_hardens():
    """F. A policy or schedule question is not a specification question.

    Two separate things keep this row, and both are asserted because either one
    failing would be a regression. The action is not a product-fact action, so
    identity is never enforced; and the compatibility service reads a delivery
    answer written for another listing as policy-compatible, so there is no
    rejection to harden in the first place.
    """

    decision, hardened = _decide(
        _50BEF, _50ANY, "지역에 따라 2~3주 소요됩니다.", scope="POLICY",
        action="DELIVERY_POLICY",
        question="이 제품 지금 주문하면 배송 얼마나 걸릴까요?")

    assert "DELIVERY_POLICY" not in PRODUCT_FACT_ACTIONS
    assert hardened is False
    assert decision.eligible is True
    assert decision.reject_reason is None
    assert decision.product_match == "POLICY_COMPATIBLE"


def test_an_identity_verdict_on_a_delivery_question_still_never_hardens():
    """The other half of F: the gate is off even when identity does differ.

    The row above is compatible, so it alone cannot show that the action is
    what disables enforcement. This pair produces a real identity verdict and
    is still kept, because the customer did not ask for a specification.
    """

    decision, hardened = _decide(
        _50BEF, _43BEH, VESA_ANSWER, scope="MODEL",
        action="DELIVERY_POLICY",
        question="이 제품 지금 주문하면 배송 얼마나 걸릴까요?")

    assert decision.reject_reason in IDENTITY_REJECT_REASONS
    assert hardened is False


def test_the_keyword_guard_alone_never_enforces_identity():
    """The whole narrowing, stated once.

    The guard calls an installation-cost question product-fact sensitive
    because it names the product. That may tighten scoring; it may not delete
    evidence.
    """

    question = "이 제품 벽걸이로 설치하려는데 추가 비용이 있나요?"
    guard = classify_product_fact(
        question, inquiry_type="PRODUCT_INQUIRY", product_name=_50BEF)

    assert guard.sensitive is True
    assert "INSTALLATION_METHOD" not in PRODUCT_FACT_ACTIONS


def test_the_hard_enforced_reasons_are_exactly_the_identity_ones():
    assert IDENTITY_REJECT_REASONS == frozenset({
        "MODEL_MISMATCH", "PRODUCT_VARIANT_MISMATCH",
        "INSUFFICIENT_PRODUCT_IDENTITY"})
    assert "PRODUCT_CATEGORY_MISMATCH" not in IDENTITY_REJECT_REASONS
    assert "TOPIC_MISMATCH" not in IDENTITY_REJECT_REASONS
    assert "TOPIC_PARTIAL_COVERAGE" not in IDENTITY_REJECT_REASONS
