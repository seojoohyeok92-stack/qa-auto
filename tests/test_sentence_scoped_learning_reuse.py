"""One order sentence no longer deletes the whole Learning row.

``ORDER_SPECIFIC`` is a search over an entire stored answer, so a product fact
lost its place in the candidate pool because of a shipping line written beside
it.  Measured over the 1,919 rows that clear the repository gate in the 26.10.4
snapshot: it removes 91 rows, and 71 of them contain sentences the detector
never fired on.  L319062 is the shape -- "터치 기능은 지원하지 않는 제품 입니다"
was deleted for "...같은 날 발송을 진행해드리고 있어" two sentences later.

The temporary axis is deliberately untouched, and one of the tests below says
why: a date and the sentence it bounds are one statement, and the store's
holiday banner splits them across a line break.

So a mixed row is kept and the offending sentences are withheld.  That is the
half that makes it safe rather than merely permissive: what travels onward is
the reusable text, so no customer's order fact becomes evidence either way.

What must not change is the rest, and most of this file is about that.  A row
whose reusable half says nothing stays blocked; so does a row that is inactive,
policy-risky, time-bound, token-bearing, or whose withheld sentence carries the
measured value another gate needs to see.  And nothing at all happens to a row
that was not already being deleted -- one test is that invariant, because it is
the reason the wrong-product, CURRENT_* and attribute gates cannot be affected
by any of this.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from typing import Any

import pytest

from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from services.historical_learning_quality_service import (
    DATA_UNSAFE_REASONS,
    HistoricalLearningQualityService,
    PERIOD_BOUNDARY,
    PRODUCT_SCOPED_CONCEPTS,
    SENTENCE_SCOPED_REASONS,
    is_data_unsafe,
    split_reusable_answer,
    unsafe_sentence_reason,
)
from services.learning_context_service import LearningContextService
from services.learning_evidence_policy import _claim_sentences
from services.semantic_analysis import (
    AtomicQuestion,
    PRODUCT_SPEC,
    SemanticAnalysis,
)

_key = itertools.count(1)

PRODUCT = "삼성 삼탠바이미 32인치(80cm) M5 스마트 모니터 IPTV+2in1 이동식 거치대"
TOUCH_FACT = "터치 기능은 지원하지 않는 제품 입니다."
SHIPPING_LINE = (
    "모니터 및 스탠드가 각각의 송장으로 발송되며 같은 날 발송을 진행해드리고 "
    "있어 일반적으로는 같이 도착하시나"
)
CLOSING_LINE = "택배사 사정에 따라 변동될 수 있는 점 양해 부탁 드립니다."

#: L319062's shape, sanitised. Newline-separated, as the stored row is: the
#: shipping line ends on "도착하시나" with no stop, so a space-joined version
#: would merge it with the closing and the fixture would not be the shape
#: being tested.
TOUCH_MIXED = "\n".join([TOUCH_FACT, SHIPPING_LINE, CLOSING_LINE])

#: An unrelated product fact beside the same shipping line. Nothing about the
#: first sentence is about one order, and it must survive the one that is.
STAND_FACT = "거치대는 기본 포함되어 있습니다."
STAND_MIXED = "\n".join(
    [STAND_FACT, "모니터와 스탠드는 같은 날 발송을 진행해드리고 있습니다."]
)

#: The same shape, but the fact is a port count -- which ``CONCEPT_PATTERNS``
#: has no entry for. See the test that records this as a known limit.
HDMI_FACT = "HDMI 포트는 3개가 있습니다."
HDMI_MIXED = "\n".join(
    [HDMI_FACT, "모니터와 스탠드는 같은 날 발송을 진행해드리고 있습니다."]
)

#: One customer's order, and nothing else. L7 and L206727 are this shape.
ORDER_ONLY = "고객님께서 주문하신 날짜는 26.07.03으로 확인되고 있습니다."

#: L211's shape: that same order fact, plus a sentence that asks the customer
#: to check something. The second sentence names nothing in the concept table,
#: so the row is an order-specific row with a polite line attached.
ORDER_PLUS_REQUEST = "\n".join(
    [ORDER_ONLY, "구매처를 네이버로 기재해주신게 맞는지 확인 부탁드립니다."]
)

#: Every sentence is bound to a window. There is no reusable half to keep.
HOLIDAY_ONLY = "8/3~8/4 하계 휴가 기간 동안 고객센터는 일부만 운영합니다."

#: Mixed, but the withheld sentence is the one stating the measured value, and
#: ``search`` hardens an identity rejection only for a row that states one.
SPEC_IN_WITHHELD = "\n".join([
    "기존 브라켓 재사용 여부는 현장 설치 기사님이 확인해주실 수 있습니다.",
    "고객님께서 주문하신 제품의 VESA 규격은 200x200mm 로 확인됩니다.",
])

#: Mixed, but the answer carries a redaction token, which ``search`` drops
#: whatever its validity.
TOKEN_MIXED = "\n".join([
    TOUCH_FACT,
    "고객님께서 주문하신 건은 판매자고객센터)<masked-phone>로 연락 주시면 확인 "
    "도와드리겠습니다.",
])


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "sentence-scope.db")
    value.initialize()
    return value


def _learning(
    database: Database,
    answer: str,
    *,
    question: str = "터치도 되는지 궁금합니다",
    human_verified: bool = True,
    product: str = PRODUCT,
    model_code: str = "",
    validity_type: str = "PERMANENT",
    valid_from: str | None = None,
    valid_until: str | None = None,
    policy_risk: str = "NONE",
    active: int = 1,
) -> int:
    metadata: dict[str, Any] = {
        "learning_signal_type": "POSITIVE",
        "human_verified": human_verified,
        "answer_provenance": "NAVER_POSTED",
        "product_scope": "MODEL",
        "learning_topics": ["PRODUCT_SPEC_FEATURE"],
    }
    if policy_risk != "NONE":
        metadata["policy_risk"] = policy_risk
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                model_code, final_answer, seller_answer, posted, rating,
                edit_ratio, quality_score, style_only, version, metadata_json,
                active, usage_count, created_at, updated_at, validity_type,
                valid_from, valid_until, validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (f"ss-{next(_key)}", "SELLER_ANSWER", question, question, "OJE_PLUS",
             "PRODUCT_INQUIRY", product, model_code, answer, answer, 1, 5, 0.0,
             1.0, 0, 1, json.dumps(metadata, ensure_ascii=False), active, 0,
             "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z", validity_type,
             valid_from, valid_until, 1),
        )
        return int(cursor.lastrowid)


def _service(database: Database) -> LearningContextService:
    service = LearningContextService(database, hard_conflicts_only=True)

    class _Sink:
        def __getattr__(self, name):
            return lambda *a, **k: None

    service.logs = _Sink()
    service.provenance = _Sink()
    service.feedback_signal_provenance = _Sink()
    service._semantic_ranks = lambda *a, **k: {}
    return service


def _row(database: Database, row_id: int) -> dict[str, Any]:
    """One pool row, exactly as the repository hands it to the gate."""

    rows = LearningRepository(database).candidates(
        store_code="OJE_PLUS", market="NAVER", limit=2000)
    return next(item for item in rows if int(item["id"]) == row_id)


def _assess(service: LearningContextService, row: dict[str, Any]):
    metadata = row.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    return service.historical.quality_policy.assess(
        question=str(row.get("question_original_masked") or ""),
        answer=str(row.get("final_answer") or ""),
        stored_quality=float(row.get("quality_score") or 0),
        policy_risk=str(metadata.get("policy_risk") or "NONE"),
        active=bool(row.get("active")),
        structured_temporary_valid=(
            str(row.get("validity_type") or "PERMANENT").upper() == "TEMPORARY"),
    )


def _rescue(database: Database, row_id: int):
    """``(eligibility, row)`` after the gate, or None when nothing changed."""

    service = _service(database)
    row = _row(database, row_id)
    return service._sentence_scoped_rescue(_assess(service, row), row)


def _blocked_today(database: Database, row_id: int) -> bool:
    service = _service(database)
    return is_data_unsafe(_assess(service, _row(database, row_id)))


# ----------------------------------------------------------------------
# 1, 2, 5: a product fact survives the order sentence beside it
# ----------------------------------------------------------------------


def test_a_product_fact_survives_the_order_sentence_beside_it(database) -> None:
    """CASES 1 and 5: L319062's shape is no longer deleted whole."""

    row_id = _learning(database, TOUCH_MIXED)
    assert _blocked_today(database, row_id), "fixture must be blocked before the fix"
    rescue = _rescue(database, row_id)
    assert rescue is not None
    eligibility, rescued = rescue
    assert not is_data_unsafe(eligibility)
    assert "SENTENCE_SCOPED_REUSE" in eligibility.reasons
    assert TOUCH_FACT in rescued["final_answer"]


def test_the_order_sentence_is_withheld_from_what_travels_on(database) -> None:
    """CASE 2: the sentence the detector fired on is not offered as evidence.

    Withheld rather than labelled. ``final_answer`` is what scoring, duplicate
    collapse and the prompt all read, so substituting it here is what keeps the
    sentence out of all three without a second mechanism.
    """

    row_id = _learning(database, TOUCH_MIXED)
    _eligibility, rescued = _rescue(database, row_id)
    assert SHIPPING_LINE not in rescued["final_answer"]
    assert rescued["withheld_answer_sentences"] == [SHIPPING_LINE]
    # And the row the database holds is untouched: this is a projection.
    assert SHIPPING_LINE in _row(database, row_id)["final_answer"]


def test_a_caveat_left_behind_by_the_withheld_sentence_goes_too(database) -> None:
    """CASE A: the continuation the withheld sentence was qualified by.

    L319062's shipping line ends on "같이 도착하시나" and the next sentence is
    the caveat it hands off to -- "택배사 사정에 따라 변동될 수 있는 점 양해 부탁
    드립니다". No detector objects to that sentence, and on its own in front of
    a question about touch input it states nothing. It asserts nothing because
    it is an apology, which is what ``APOLOGY`` already recognises.

    So the projection is the product fact and nothing else.
    """

    row_id = _learning(database, TOUCH_MIXED)
    _eligibility, rescued = _rescue(database, row_id)
    assert rescued["final_answer"] == TOUCH_FACT
    assert CLOSING_LINE not in rescued["final_answer"]
    split = split_reusable_answer(_claim_sentences(TOUCH_MIXED))
    assert split.withheld == (SHIPPING_LINE,)
    assert split.non_claim == (CLOSING_LINE,)


def test_an_unfinished_clause_is_not_kept(database) -> None:
    """A fragment states nothing, whatever it mentions.

    Withholding a sentence out of the middle of a chain leaves its neighbours
    dangling: L154689's apology came out and left "...수취인과 연락이 되지 않을
    경우" with its consequence gone, which then ran straight into the next
    sentence. ``_COMPLETE_STATEMENT`` is the test for whether a sentence
    finishes, and an unfinished one is not evidence.
    """

    answer = "\n".join([
        "고객님께서 주문하신 건은 확인되었습니다.",
        "잘못된 연락처 또는 부재중으로 인해 연락이 되지 않을 경우",
        "납기일이 하루씩 미뤄지실 수 있으신 점 양해 부탁 드립니다.",
        "셋탑박스의 경우 택배로 배송되는 제품이며 직접 설치를 진행해주셔야 합니다.",
    ])
    split = split_reusable_answer(_claim_sentences(answer))
    assert "연락이 되지 않을 경우" in " ".join(split.non_claim)
    assert split.reusable_text == (
        "셋탑박스의 경우 택배로 배송되는 제품이며 직접 설치를 진행해주셔야 합니다."
    )


def test_an_operational_apology_is_not_a_product_fact(database) -> None:
    """CASE B: a size token does not promote a delay apology to a product fact.

    L160147 read "85인치 제품의 경우 설치가 많이 지연되고 있는 점 죄송합니다",
    which names ``PRODUCT_OPTION`` only because 인치 is in that pattern. It
    reports an operational state and apologises for it; it states no property
    of the product. With the apology recognised there is nothing reusable left,
    so the row stays blocked.
    """

    answer = "\n".join([
        "주문하신 상품의 설치예정일은 8/28입니다.",
        "85인치 제품의 경우 설치가 많이 지연되고 있는 점 죄송합니다.",
    ])
    row_id = _learning(database, answer, question="설치 언제 되나요")
    assert _blocked_today(database, row_id)
    split = split_reusable_answer(_claim_sentences(answer))
    assert split.withheld, "the order sentence is recognised"
    assert len(split.non_claim) == 1, "and the apology is not a claim"
    assert split.reusable_text == ""
    assert split.reusable_concepts == ()
    assert _rescue(database, row_id) is None


def test_an_independent_general_policy_is_still_rescued(database) -> None:
    """CASE D: a standing policy sentence survives the order sentence beside it.

    L319131's shape. The policy is what the next customer asking the same
    question needs, and it is a complete statement that asserts something, so
    nothing here removes it.
    """

    answer = "\n".join([
        "구매하신 제품은 자체적으로 판매중인 스탠드와의 패키지 제품입니다.",
        "삼성 감사 페스티벌 신청 시에는 모니터 모델코드만 입력해주시면 됩니다.",
    ])
    row_id = _learning(database, answer, question="패키지상품코드가 뭔가요")
    assert _blocked_today(database, row_id)
    _eligibility, rescued = _rescue(database, row_id)
    assert rescued["final_answer"] == (
        "삼성 감사 페스티벌 신청 시에는 모니터 모델코드만 입력해주시면 됩니다."
    )


def test_delivery_wording_alone_does_not_kill_an_unrelated_product_fact(
    database,
) -> None:
    """CASE 6: a contents fact is not an order fact because it sits beside one."""

    row_id = _learning(database, STAND_MIXED, question="거치대도 같이 오나요")
    assert _blocked_today(database, row_id)
    _eligibility, rescued = _rescue(database, row_id)
    assert rescued["final_answer"] == STAND_FACT


def test_a_fact_the_concept_table_cannot_name_is_not_rescued(database) -> None:
    """A recorded limit, not a wish: ports are not in ``CONCEPT_PATTERNS``.

    The reusable half has to name something in that table, and the table has
    eleven entries built for historical intake -- no ports among them. So "HDMI
    포트는 3개가 있습니다" beside a shipping line stays blocked.

    Widening the test to ``attribute_families`` was measured and rejected: of
    the 63 rows held back for naming no concept, 15 do name an attribute
    family, and reading them they are one customer's handling notes --
    "금일 기사님께서 방문 예정입니다", "취소 확인되었습니다". The looser
    vocabulary brings those back, which is the opposite of the point. This
    stays blocked until the concept table itself grows.
    """

    row_id = _learning(database, HDMI_MIXED, question="HDMI 포트 몇 개인가요")
    assert _blocked_today(database, row_id)
    split = split_reusable_answer(_claim_sentences(HDMI_MIXED))
    assert split.withheld
    assert split.reusable_text == HDMI_FACT
    assert split.reusable_concepts == ()
    assert _rescue(database, row_id) is None


def test_the_restored_row_reaches_the_candidate_pool(database) -> None:
    """The consumer: ``build`` hands the reusable text to retrieval.

    The column and the helper are not the contract -- what the pool contains
    is. Checked through the real ``build`` so the trace is the production one.
    """

    kept = _learning(database, TOUCH_MIXED)
    blocked = _learning(database, ORDER_ONLY, question="주문 날짜 확인해주세요")
    question = "터치스크린 기능도 있나요?"
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "NAVER",
        "source_question_id": f"ss-i-{next(_key)}",
        "inquiry_type": "PRODUCT_INQUIRY", "title": "상품 문의",
        "content": question, "product_name": PRODUCT, "product_id": "p1",
        "raw_json": {},
    }).inquiry_id
    semantic = SemanticAnalysis(
        primary_action=PRODUCT_SPEC,
        atomic_questions=(AtomicQuestion(
            text=question, action=PRODUCT_SPEC,
            requested_information="터치스크린 기능 지원 여부",
            requested_attribute="EXISTENCE_OR_CAPABILITY"),),
        purchase_state="UNKNOWN", confidence=0.95, source="GPT")
    context = _service(database).build(
        AnswerFacts(inquiry={"inquiry_id": inquiry_id, "question": question},
                    product={"name": PRODUCT}, order={}),
        IntentResult("PRODUCT", (question,), Emotion.NORMAL, "NORMAL", 0.9,
                     False, ""),
        semantic_analysis=semantic,
    )
    delivered = {
        int(item["learning_example_id"]): str(item.get("answer") or "")
        for item in context["similar_approved_answers"]
    }
    assert kept in delivered
    assert TOUCH_FACT in delivered[kept]
    assert SHIPPING_LINE not in delivered[kept]
    assert blocked not in delivered


# ----------------------------------------------------------------------
# 3, 4: a row that is only a past order stays blocked
# ----------------------------------------------------------------------


def test_an_answer_that_is_only_one_customers_order_stays_blocked(database) -> None:
    """CASE 3: nothing reusable is left, so nothing is reconsidered."""

    row_id = _learning(database, ORDER_ONLY, question="주문 날짜 확인해주세요")
    assert _blocked_today(database, row_id)
    assert _rescue(database, row_id) is None


def test_human_verification_does_not_make_a_past_order_fact_evidence(
    database,
) -> None:
    """CASE 4: approval is about who wrote it, not about whether it is reusable.

    L211's shape. A second sentence exists and no detector fires on it, so a
    rule that asked only "is anything left" would bring this row back carrying
    the order date's neighbour. The reusable half names nothing in the concept
    table, which is what stops it.
    """

    row_id = _learning(database, ORDER_PLUS_REQUEST, human_verified=True,
                       question="구매일자 보완 요청을 받았습니다")
    assert _blocked_today(database, row_id)
    split = split_reusable_answer(_claim_sentences(ORDER_PLUS_REQUEST))
    assert split.withheld, "the order sentence must be recognised"
    assert split.reusable_text, "a sentence does remain"
    assert split.reusable_concepts == (), "but it names nothing reusable"
    assert split.mixed is False
    assert _rescue(database, row_id) is None


# ----------------------------------------------------------------------
# 7: the temporary / expired axis is untouched
# ----------------------------------------------------------------------


def test_a_time_bound_answer_is_never_reconsidered(database) -> None:
    """CASE 7: the temporary axis is left exactly as it was.

    Splitting these was tried and measured, and the results were wrong. The
    store's holiday banner reads "★ [8/3~8/4] 하계 휴가 기간 동안" on one line
    and "고객센터는 일부만 운영합니다" on the next, so the window and the claim
    it qualifies land in different sentences -- what survived was a
    restricted-hours notice with its dates stripped off (L36, L40, L41, L45).
    Others kept a stale dated claim no single-sentence test catches ("7월 중
    접수하신 분들에 대하여 금주 중 발송될 것으로", L30).

    A date and the sentence it bounds are one statement. So
    ``TEMPORARY_WITHOUT_STRUCTURED_VALIDITY`` is not sentence-scoped, and a
    row carrying it stays blocked however reusable the rest looks.
    """

    assert "TEMPORARY_WITHOUT_STRUCTURED_VALIDITY" not in SENTENCE_SCOPED_REASONS

    # The banner shape, on two lines, as the store writes it.
    banner = "\n".join([
        "★ [8/3~8/4] 하계 휴가 기간 동안",
        "고객센터는 일부만 운영합니다.",
        TOUCH_FACT,
    ])
    row_id = _learning(database, banner, question="고객센터 운영하나요")
    service = _service(database)
    row = _row(database, row_id)
    eligibility = _assess(service, row)
    assert eligibility.status == "TEMPORARY_OR_EXPIRED"
    assert service._sentence_scoped_rescue(eligibility, row) is None

    # And the single-sentence case, for the same reason.
    only = _learning(database, HOLIDAY_ONLY, question="고객센터 운영하나요")
    assert _blocked_today(database, only)
    assert _rescue(database, only) is None


def test_a_dated_sentence_inside_an_order_row_is_still_withheld(database) -> None:
    """``TIME_BOUND`` stays a sentence-level marker even so.

    The row is reconsidered because it is order-specific, and the dated
    sentence inside it is withheld along with the order one. The split may
    remove more than the row was blocked for; it may never remove less.
    """

    answer = "\n".join([
        TOUCH_FACT,
        "고객님께서 주문하신 건은 확인되었습니다.",
        "8/3~8/4 하계 휴가 기간 동안은 처리가 지연됩니다.",
    ])
    assert unsafe_sentence_reason(
        "8/3~8/4 하계 휴가 기간 동안은 처리가 지연됩니다.") == "TIME_BOUND"
    split = split_reusable_answer(_claim_sentences(answer))
    assert len(split.withheld) == 2
    assert set(split.withheld_reasons) == {"ORDER_SPECIFIC", "TIME_BOUND"}
    assert split.reusable_text == TOUCH_FACT


def test_structured_temporary_validity_is_still_honoured(database) -> None:
    """A TEMPORARY row inside its window was never blocked here, and is not now."""

    row_id = _learning(
        database, "행사 기간 중 사은품이 제공됩니다.", validity_type="TEMPORARY",
        valid_from="2026-01-01T00:00:00Z", valid_until="2099-01-01T00:00:00Z",
        question="사은품 있나요")
    service = _service(database)
    row = _row(database, row_id)
    eligibility = _assess(service, row)
    assert "TEMPORARY_WITHOUT_STRUCTURED_VALIDITY" not in eligibility.reasons
    assert service._sentence_scoped_rescue(eligibility, row) is None


# ----------------------------------------------------------------------
# the vetoes that keep other gates' populations identical
# ----------------------------------------------------------------------


def test_a_token_bearing_answer_is_not_reconsidered(database) -> None:
    """``search`` drops token-bearing rows; which rows it drops must not move.

    Sixteen of the 263 carry a redaction token and eight hold it inside a
    sentence this would withhold. Withholding it would quietly take those rows
    out of that gate's reach, so the row is left blocked here instead.
    """

    row_id = _learning(database, TOKEN_MIXED)
    assert _blocked_today(database, row_id)
    assert _rescue(database, row_id) is None


def test_the_reusable_half_must_be_about_the_product(database) -> None:
    """Removing a sentence does not make its neighbours generic.

    L197's order sentence came out and left "3~5영업일 내로 환불 진행되실
    예정입니다", which is still one customer's refund. L11610's left "주문 시
    확인하신 도착일자에 맞춰 배송됩니다" without the clause that scoped it to
    n배송 products, so a narrow claim read as a universal one.

    Keying on any concept at all admits 51 rows and both of those; keying on
    ``PRODUCT_SCOPED_CONCEPTS`` admits 6 and neither.
    """

    refund = "\n".join([
        "고객님께서 주문하신 건은 확인되었습니다.",
        "3~5영업일 내로 환불 진행되실 예정입니다.",
    ])
    row_id = _learning(database, refund, question="환불 언제 되나요")
    assert _blocked_today(database, row_id)
    split = split_reusable_answer(_claim_sentences(refund))
    assert split.withheld, "the order sentence is recognised"
    assert split.reusable_concepts, "and the remainder does name a concept"
    assert not set(split.reusable_concepts) & PRODUCT_SCOPED_CONCEPTS
    assert _rescue(database, row_id) is None


def test_a_period_marker_left_in_the_reusable_half_blocks_the_row(database) -> None:
    """A claim about how things stand right now is not reusable either.

    L169255's reusable half was "블랙모델의 경우 현재 품절로 ... 약 2주정도
    소요될 예정입니다" -- a product-scoped sentence that was true the week it
    was written. ``PERIOD_BOUNDARY`` is the existing pattern for that wording
    and it is applied to what survives, not only to what is withheld.
    """

    stale = "\n".join([
        "고객님께서 주문하신 건은 순차 출고 예정입니다.",
        "블랙 모델의 경우 현재 품절로 약 2주 정도 소요될 예정입니다.",
    ])
    row_id = _learning(database, stale, question="블랙 언제 오나요")
    assert _blocked_today(database, row_id)
    split = split_reusable_answer(_claim_sentences(stale))
    assert set(split.reusable_concepts) & PRODUCT_SCOPED_CONCEPTS
    assert PERIOD_BOUNDARY.search(split.reusable_text)
    assert _rescue(database, row_id) is None


def test_a_withheld_sentence_may_not_carry_the_measured_value(database) -> None:
    """``search`` hardens an identity rejection only for a stated specification.

    If the figure is in the withheld sentence, dropping it would stop that
    hardening from firing -- wrong-product leakage by omission -- so the row
    stays blocked.
    """

    row_id = _learning(database, SPEC_IN_WITHHELD, question="기존 브라켓 호환되나요")
    assert _blocked_today(database, row_id)
    split = split_reusable_answer(_claim_sentences(SPEC_IN_WITHHELD))
    assert split.mixed, "without the veto this row would have been kept"
    assert _rescue(database, row_id) is None


def test_a_policy_risk_row_is_not_reconsidered(database) -> None:
    """Policy risk is a fact about the row, and splitting sentences is no answer.

    Two separate things stop it, and only one of them is the reason list.
    ``policy_risk`` is passed into the re-assessment too, so the reusable half
    comes back POLICY_RISK as well -- which is why mutating the reason check
    alone does not surface here. The next test aims at that check directly.
    """

    row_id = _learning(database, TOUCH_MIXED, policy_risk="HIGH")
    service = _service(database)
    row = _row(database, row_id)
    eligibility = _assess(service, row)
    assert eligibility.status == "POLICY_RISK"
    assert service._sentence_scoped_rescue(eligibility, row) is None


def test_only_the_declared_text_scoped_finding_may_be_reconsidered(database) -> None:
    """A data-unsafe finding this gate does not understand must block the row.

    ``DATA_UNSAFE_REASONS`` is a list that will grow, and a reason added to it
    later has no reason to travel through ``assess`` on a shortened answer the
    way ``policy_risk`` does. So the gate names the one finding a sentence
    split can answer and refuses everything else, which is checked here by
    handing it a finding that is not on that list.
    """

    assert "INACTIVE_OR_EMPTY" not in SENTENCE_SCOPED_REASONS
    assert "POLICY_RISK" not in SENTENCE_SCOPED_REASONS
    assert SENTENCE_SCOPED_REASONS < DATA_UNSAFE_REASONS

    row_id = _learning(database, TOUCH_MIXED)
    service = _service(database)
    row = _row(database, row_id)
    eligibility = _assess(service, row)
    # The row as it stands is rescued: that is the control for what follows.
    assert service._sentence_scoped_rescue(eligibility, row) is not None

    other_reason = next(iter(DATA_UNSAFE_REASONS - SENTENCE_SCOPED_REASONS))
    with_other = dataclasses.replace(
        eligibility, reasons=tuple(eligibility.reasons) + (other_reason,)
    )
    assert is_data_unsafe(with_other)
    assert service._sentence_scoped_rescue(with_other, row) is None


# ----------------------------------------------------------------------
# 8, 9, 10: the reason the other gates cannot be affected
# ----------------------------------------------------------------------


def test_nothing_happens_to_a_row_that_was_not_already_blocked(database) -> None:
    """CASES 8, 9 and 10, as the invariant that makes them true.

    The wrong-product factual boundary, the CURRENT_* delivery and installation
    guards and the ATTRIBUTE_MATCH/MISMATCH behaviour all act on rows that
    reach the search. This gate only ever reconsiders a row it was already
    deleting, so none of those rows can change -- which is a stronger statement
    than re-asserting each of them here, and it is checked over the shapes they
    act on.
    """

    rows = {
        "wrong_product_spec": _learning(
            database, "해당 모델의 VESA 규격은 200x200mm 입니다.",
            question="베사 규격 알려주세요"),
        "current_order_delivery": _learning(
            database, "택배배송 상품은 보통 1~2영업일 내 도착합니다.",
            question="배송 얼마나 걸려요"),
        "attribute_bearing": _learning(
            database, "블루투스 연결을 지원합니다.", question="블루투스 되나요"),
        "plain_policy": _learning(
            database, "설치비는 별도로 청구되지 않습니다.", question="설치비 있나요"),
    }
    service = _service(database)
    for label, row_id in rows.items():
        row = _row(database, row_id)
        eligibility = _assess(service, row)
        if is_data_unsafe(eligibility):
            continue
        assert service._sentence_scoped_rescue(eligibility, row) is None, label
        assert _row(database, row_id)["final_answer"] == row["final_answer"], label


def test_a_sentence_reason_is_one_the_whole_answer_search_would_have_found(
    database,
) -> None:
    """The split loosens nothing: it asks the same three questions, per sentence.

    Every sentence the splitter marks unsafe is a sentence the whole-answer
    detectors match on their own, so no new vocabulary enters the system and a
    row can never be withheld for a reason ``assess`` would not recognise.
    """

    quality = HistoricalLearningQualityService()
    for answer in (TOUCH_MIXED, ORDER_PLUS_REQUEST, HDMI_MIXED, HOLIDAY_ONLY):
        split = split_reusable_answer(_claim_sentences(answer))
        for sentence in split.withheld:
            assert unsafe_sentence_reason(sentence) is not None
            # and the whole-answer verdict agrees that something is wrong here
            assert quality.assess(
                question="x", answer=sentence, stored_quality=1.0,
                active=True, structured_temporary_valid=False,
            ).status in {"ORDER_SPECIFIC", "TEMPORARY_OR_EXPIRED"}
        for sentence in _claim_sentences(split.reusable_text):
            assert unsafe_sentence_reason(sentence) is None
