"""The delivery pipeline from order number to Auto Post, on real-shaped rows.

The failure these pin is CASE C: DPS returned SUCCESS -- in one operational
inquiry with a confirmed date of 2026-08-26 -- and the dashboard still showed
"답변 생성 버튼을 눌러 초안을 생성하세요", because no draft existed at all.

``phase9_answer_policy`` selected its delivery branches by writing out the
subtype set ``{DELIVERY_OR_INSTALLATION_SCHEDULE, LEGACY_DELIVERY_CATEGORY,
SCHEDULE_CHANGE_REQUEST}`` in each condition. That set omits
``COMPOUND_MULTI_INTENT``, which is what the classifier produces whenever a
customer asks two things at once -- "오늘 주문했는데 언제 받아볼 수 있을까요?
대략적인 배송 예정이라도 알 수 없나요?" is one such inquiry, and so were
operational rows 2638, 2662 and 2670. Those matched no branch, fell through to
the tail, and ``answer_service`` turned the unrecognised ``answer_source`` into
``AnswerGenerationError`` -- which killed draft generation outright.

The invariant asserted throughout: **a delivery inquiry whose DPS lookup
succeeded must never end with an empty Program Answer.**
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from answer.inquiry_analysis import (
    AnswerStrategy,
    InquiryAnalysis,
    InquiryType,
    OrderIdStatus,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.source_adapter import answer_request_from_inquiry
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.inquiry_analysis_service import InquiryAnalysisService
from services.phase9_answer_policy import apply_phase9_rule_policy


ORDER_NUMBER = "2026082198559811"
PRODUCT = "삼성 43인치(107.9cm) 비즈니스TV 4K UHD 1등급 LH43BEFHLGFXKR 스탠드형"

# Every answer_source answer_service.py accepts for a delivery inquiry.
# Anything else raises AnswerGenerationError and no draft is written.
ACCEPTED_SOURCES = frozenset({
    "delivery_template", "dps", "ORDER_ID_REQUEST",
    "ORDER_LOOKUP_FAILED", "SAFE_TEMPLATE",
})


def inquiry(
    question: str,
    *,
    order_id: str = "",
    source_type: str = "CUSTOMER_INQUIRY",
    title: str = "문의",
) -> dict[str, Any]:
    return {
        "id": 1,
        "source_type": source_type,
        "inquiry_type": source_type,
        "source_question_id": "dps-e2e",
        "external_inquiry_id": "dps-e2e",
        "title": title,
        "content": question,
        "product_name": PRODUCT,
        "order_id": order_id,
        "product_order_id": "",
        "raw_json": {"productId": "13239109816"},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }


def analyse(
    question: str,
    *,
    order_id: str = "",
    source_type: str = "CUSTOMER_INQUIRY",
    title: str = "문의",
) -> InquiryAnalysis:
    return InquiryAnalysisService().analyze(
        answer_request_from_inquiry(
            inquiry(
                question,
                order_id=order_id,
                source_type=source_type,
                title=title,
            )
        )
    )


def route_with_dps(
    question: str,
    *,
    order_id: str = ORDER_NUMBER,
    lookup_status: str = "SUCCESS",
    installation_date: str | None = None,
    source_type: str = "CUSTOMER_INQUIRY",
    title: str = "문의",
) -> AnswerResult:
    """Run the real delivery routing with a DPS result already in hand."""

    payload = inquiry(
        question, order_id=order_id, source_type=source_type, title=title
    )
    request = answer_request_from_inquiry(payload)
    analysis = InquiryAnalysisService().analyze(request)
    request.metadata["phase9_analysis"] = analysis.to_dict()
    request.metadata["dps"] = {
        "lookup_required": True,
        "lookup_status": lookup_status,
        "installation_date": installation_date,
        "installation_date_display": installation_date,
        "delivery_status": "구매요청",
        "installation_status": "구매요청",
        "change_request": False,
        "general_segments": [],
        "dps_segments": [question],
        "warnings": [],
    }
    base = AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="배송/설치현황",
        reason="rule fallback",
        answer="",
        provider="rules",
        auto_answerable=False,
        needs_review=True,
        matched_rule="",
        metadata={"answer_source": "rule_engine_fallback"},
    )
    return apply_phase9_rule_policy(request, base, analysis)


def source_of(result: AnswerResult) -> str:
    return str(result.metadata.get("answer_source") or "")


def route_of(result: AnswerResult) -> str:
    return str(result.metadata.get("selected_answer_route") or "")


# ==========================================================================
# The invariant: DPS SUCCESS must never yield an empty Program Answer
# ==========================================================================


# The three operational rows that produced no draft at all, title and body
# as they were written -- the title matters, because it is part of what the
# classifier reads and it is what pushed 2662 into COMPOUND_MULTI_INTENT.
OPERATIONAL_ROWS = [
    (
        "2638",
        "배송 문의드립니다",
        "오늘(8/24) 주문했는데 언제 받아볼 수 있을까요? 대략적인 배송 예정이라도 알 수 없나요?",
    ),
    ("2670", "배송문의", "언제쯤 배송이 될까요? 아직 출발도 안해서 연락드려봅니다."),
    (
        "2662",
        "배송일 문의겸 변경문의",
        "배송예정이 8.26일로 나오는데 변경지정이 가능 한가요? 아직 연락이 없었는데요",
    ),
]


@pytest.mark.parametrize(
    ("case", "title", "question"),
    OPERATIONAL_ROWS
    + [
        # Simple phrasings, which already worked and must keep working.
        ("simple-install", "문의", "언제설치가능한가요?"),
        ("simple-delivery", "문의", "배송이 언제쯤 되는지 일정 문의드립니다."),
        ("dispatch", "문의", "모니터 언제 발송되나요?"),
    ],
)
@pytest.mark.parametrize("installation_date", [None, "2026-08-28"])
def test_dps_success_always_produces_an_answer(
    case: str, title: str, question: str, installation_date: str | None
) -> None:
    """CASE C. An empty answer here is the exact production failure."""

    result = route_with_dps(
        question, title=title, installation_date=installation_date
    )

    assert result.answer.strip(), (case, installation_date)
    assert source_of(result) in ACCEPTED_SOURCES, (case, source_of(result))
    assert route_of(result), case


@pytest.mark.parametrize(
    ("case", "title", "question"),
    [row for row in OPERATIONAL_ROWS if row[0] in {"2638", "2662"}],
)
def test_compound_delivery_inquiry_is_routed_not_dropped(
    case: str, title: str, question: str
) -> None:
    """The subtype that was missing from every branch."""

    analysis = analyse(question, title=title, order_id=ORDER_NUMBER)

    assert analysis.inquiry_subtype == "COMPOUND_MULTI_INTENT", case
    # ...and the analysis object's own predicate says it is a delivery
    # question, which is what the routing now consults.
    assert analysis.delivery_question is True, case


# ==========================================================================
# DPS SUCCESS: a confirmed date is answered, an unconfirmed one is not invented
# ==========================================================================


def test_confirmed_schedule_is_answered_from_dps() -> None:
    result = route_with_dps("언제설치가능한가요?", installation_date="2026-08-28")

    assert source_of(result) == "dps"
    assert route_of(result) == "DELIVERY_WITH_INSTALLATION_DATE"
    assert "2026년 8월 28일" in result.answer


def test_dps_success_without_a_confirmed_date_invents_nothing() -> None:
    """DPS answered, but it holds no date. Saying one would be a fabrication."""

    result = route_with_dps("언제쯤 배송이 될까요?", installation_date=None)

    assert route_of(result) == "DELIVERY_DATE_UNCONFIRMED"
    assert source_of(result) == "SAFE_TEMPLATE"
    assert "등록되지 않았습니다" in result.answer
    # No calendar date of any kind.
    assert "월" not in result.answer.split("안내드린")[0].replace("문의하신", "")


@pytest.mark.parametrize(
    "title",
    [
        "문의",  # SCHEDULE_CHANGE_REQUEST
        "배송일 문의겸 변경문의",  # COMPOUND_MULTI_INTENT
    ],
)
def test_schedule_change_gets_the_change_template_even_when_compound(
    title: str,
) -> None:
    """"8/26으로 나오는데 변경 가능한가요" is a request, not a lookup.

    Asking to move the date and asking something else in the same breath is
    still asking to move the date. Keying the template on the subtype alone
    answered the compound version with "주문 조회가 필요합니다" -- a reply to a
    question the customer never asked.
    """

    result = route_with_dps(
        "배송예정이 8.26일로 나오는데 변경지정이 가능 한가요? 아직 연락이 없었는데요",
        title=title,
        installation_date="2026-08-26",
    )

    assert result.answer.strip()
    assert "일정 변경은 담당자 확인이 필요합니다" in result.answer, title
    assert "주문 조회가 필요합니다" not in result.answer, title
    assert result.needs_review is True


# ==========================================================================
# DPS failures keep their existing safe handling
# ==========================================================================


@pytest.mark.parametrize(
    "lookup_status",
    ["TIMEOUT", "AGENT_OFFLINE", "PARSE_ERROR", "AUTOMATION_ERROR",
     "NETWORK_ERROR", "FAILED"],
)
def test_dps_failure_uses_the_safe_template(lookup_status: str) -> None:
    result = route_with_dps("배송 일정 문의", lookup_status=lookup_status)

    assert route_of(result) == "DPS_LOOKUP_FAILED"
    assert source_of(result) == "SAFE_TEMPLATE"
    assert result.needs_review is True
    assert result.answer.strip()


def test_dps_not_found_uses_the_safe_template() -> None:
    result = route_with_dps("배송 일정 문의", lookup_status="NOT_FOUND")

    assert route_of(result) == "DELIVERY_ORDER_NOT_FOUND"
    assert result.needs_review is True


# ==========================================================================
# CASE A / B / D / E -- the routing decisions upstream of DPS
# ==========================================================================


@pytest.mark.parametrize(
    "source_type", ["CUSTOMER_INQUIRY", "PRODUCT_INQUIRY"]
)
def test_case_a_order_number_in_the_body_is_validated(source_type: str) -> None:
    """The inquiry type must not stop the body's order number being read."""

    analysis = analyse(
        f"주문번호 {ORDER_NUMBER}입니다. 배송이 언제쯤 되는지 일정 문의드립니다.",
        source_type=source_type,
    )

    assert analysis.order_id_status is OrderIdStatus.VALIDATED
    assert analysis.requires_order_lookup is True
    assert analysis.requires_dps_lookup is True


@pytest.mark.parametrize(
    "question",
    ["모니터 언제 발송되나요?", "배송이 언제 되나요?", "배송 일정 문의",
     "설치 예정일이 언제인가요?"],
)
def test_case_b_valid_order_number_requires_dps(question: str) -> None:
    analysis = analyse(question, order_id=ORDER_NUMBER)

    assert analysis.requires_order_lookup is True
    assert analysis.requires_dps_lookup is True
    assert analysis.answer_strategy is AnswerStrategy.DIRECT_FACT_ANSWER


@pytest.mark.parametrize(
    "question", ["언제 발송되나요?", "배송은 언제 되나요?", "언제 받을 수 있나요?"]
)
def test_case_d_no_order_number_asks_for_it_and_skips_dps(question: str) -> None:
    analysis = analyse(question)

    assert analysis.order_id_status is OrderIdStatus.MISSING
    assert analysis.requires_order_lookup is True
    assert analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID

    result = route_with_dps(question, order_id="", lookup_status="NOT_STARTED")
    assert route_of(result) == "ORDER_ID_REQUEST"
    assert source_of(result) == "ORDER_ID_REQUEST"
    assert "일반 주문번호가 필요합니다" in result.answer


@pytest.mark.parametrize(
    "question",
    ["HDMI 단자가 몇 개 있나요?", "에어플레이 지원되나요?",
     "스탠드 분리 후 다시 장착할 수 있나요?", "이 제품 화면 크기가 어떻게 되나요?"],
)
def test_case_e_product_questions_never_reach_dps(question: str) -> None:
    """Fixing delivery routing must not send product questions to DPS."""

    analysis = analyse(question, source_type="PRODUCT_INQUIRY")

    assert analysis.requires_order_lookup is False
    assert analysis.requires_dps_lookup is False


def test_validated_order_number_survives_the_delivery_routing() -> None:
    """Invariant D: a validated order number is never downgraded downstream."""

    analysis = analyse(
        f"주문번호 {ORDER_NUMBER}입니다. 배송 일정 문의", source_type="PRODUCT_INQUIRY"
    )

    assert analysis.order_id_validated is True
    result = route_with_dps(
        f"주문번호 {ORDER_NUMBER}입니다. 배송 일정 문의",
        source_type="PRODUCT_INQUIRY",
        installation_date="2026-08-28",
    )
    assert route_of(result) != "ORDER_ID_REQUEST"
    assert result.answer.strip()


def test_invalid_order_number_is_not_sent_to_dps() -> None:
    analysis = analyse("주문번호 123입니다. 배송 언제 되나요?")

    assert analysis.order_id_status is not OrderIdStatus.VALIDATED
    assert analysis.answer_strategy is AnswerStrategy.REQUEST_ORDER_ID


def test_delivery_routing_never_returns_an_unroutable_source() -> None:
    """The tail guard: a routing gap must not become a fatal error.

    ``answer_service`` raises AnswerGenerationError for any answer_source it
    does not recognise, and that raise is what left inquiries with no draft.
    The guard stays; delivery inquiries simply no longer reach it unrouted.
    """

    for lookup_status in ("SUCCESS", "PENDING", "NOT_STARTED", "UNKNOWN_STATE"):
        for date in (None, "2026-08-28"):
            for question in (
                "언제쯤 배송이 될까요? 아직 출발도 안해서 연락드려봅니다.",
                "배송 일정 문의",
                "설치 예정일 알려주세요.",
            ):
                result = route_with_dps(
                    question, lookup_status=lookup_status, installation_date=date
                )
                assert source_of(result) in ACCEPTED_SOURCES, (
                    question, lookup_status, date, source_of(result)
                )
                assert result.answer.strip()


# ==========================================================================
# End to end, on a real database: inquiry -> AnswerService -> persisted draft
# -> Validator -> Eligibility.
#
# The routing tests above prove the branch is selected. These prove the draft
# actually reaches the table -- which is the thing the dashboard reads, and
# the thing that was missing in production while DPS said SUCCESS.
# ==========================================================================


class _StubDps:
    """Stands in for the DPS agent so no lookup leaves the machine."""

    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata
        self.calls: list[str] = []

    def enrich(self, request, **kwargs):
        self.calls.append(request.order_id)
        request.metadata["dps"] = dict(self.metadata)
        request.metadata["dps"].setdefault("cache_used", True)
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=True),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
        request.metadata["dps"] = {
            "lookup_required": False,
            "lookup_status": "NOT_REQUIRED",
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )


class _ForbiddenHybrid:
    def generate(self, request, rule_result):
        raise AssertionError("GPT must not be called for a delivery schedule")


def dps_success(installation_date: str | None) -> dict[str, Any]:
    return {
        "lookup_required": True,
        "lookup_status": "SUCCESS",
        "installation_date": installation_date,
        "installation_date_display": installation_date,
        "delivery_status": "구매요청",
        "installation_status": "구매요청",
        "change_request": False,
        "general_segments": [],
        "dps_segments": [],
        "warnings": [],
    }


@pytest.fixture
def database(tmp_path):
    value = Database(tmp_path / "delivery_e2e.db")
    value.initialize()
    return value


def store_inquiry(
    database,
    source_id: str,
    *,
    question: str,
    title: str = "문의",
    order_id: str | None = ORDER_NUMBER,
    product_order_id: str | None = None,
    inquiry_type: str = "배송",
) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S",
            "source_type": "NAVER",
            "source_question_id": source_id,
            "inquiry_type": inquiry_type,
            "title": title,
            "content": question,
            "product_name": PRODUCT,
            "order_id": order_id,
            "product_order_id": product_order_id,
            "raw_json": {},
        }
    ).inquiry_id


def generate(database, inquiry_id: int, dps_metadata: dict[str, Any]):
    return AnswerService(
        database,
        dps_enrichment=_StubDps(dps_metadata),
        hybrid_service=_ForbiddenHybrid(),
    ).generate_for_inquiry(inquiry_id)


@pytest.mark.parametrize(("case", "title", "question"), OPERATIONAL_ROWS)
@pytest.mark.parametrize("installation_date", [None, "2026-08-28"])
def test_program_answer_is_never_empty_after_dps_success(
    database, case: str, title: str, question: str, installation_date: str | None
) -> None:
    """The mandated assertion.

    ``dps_lookup_status == SUCCESS`` on an ordinary delivery inquiry and no
    draft -- or a draft whose Program Answer is an empty string -- is the
    production failure, and this test must fail if it comes back.
    """

    inquiry_id = store_inquiry(
        database, f"E2E-{case}-{installation_date}", question=question, title=title
    )

    outcome = generate(database, inquiry_id, dps_success(installation_date))

    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    assert draft is not None, f"{case}: DPS SUCCESS but no draft was written"
    program_answer = str(draft.get("original_answer") or "")
    assert program_answer.strip(), f"{case}: Program Answer is empty"
    assert outcome.result.answer.strip(), case


def test_confirmed_date_reaches_the_draft_and_clears_eligibility(database) -> None:
    inquiry_id = store_inquiry(
        database, "E2E-CONFIRMED", question="언제설치가능한가요?"
    )

    generate(database, inquiry_id, dps_success("2026-08-28"))

    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    assert draft is not None
    assert "2026년 8월 28일" in draft["original_answer"]

    inquiry = InquiryRepository(database).get(inquiry_id)
    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=draft, route="DELIVERY_WITH_INSTALLATION_DATE"
    )
    assert eligibility.decision == "SAFE"


def test_unconfirmed_date_is_held_for_review_not_auto_posted(database) -> None:
    """No date from DPS must never become an auto-posted guess."""

    inquiry_id = store_inquiry(
        database, "E2E-UNCONFIRMED", question="언제쯤 배송이 될까요?"
    )

    generate(database, inquiry_id, dps_success(None))

    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    assert draft is not None
    assert draft["original_answer"].strip()

    inquiry = InquiryRepository(database).get(inquiry_id)
    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=draft, route="DELIVERY_DATE_UNCONFIRMED"
    )
    assert eligibility.decision != "SAFE"


@pytest.mark.parametrize("lookup_status", ["TIMEOUT", "AGENT_OFFLINE", "FAILED"])
def test_dps_failure_still_writes_a_draft_and_blocks_auto_post(
    database, lookup_status: str
) -> None:
    inquiry_id = store_inquiry(
        database, f"E2E-{lookup_status}", question="배송 일정 문의드립니다."
    )

    metadata = dps_success(None)
    metadata["lookup_status"] = lookup_status

    generate(database, inquiry_id, metadata)

    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    assert draft is not None
    assert draft["original_answer"].strip()

    inquiry = InquiryRepository(database).get(inquiry_id)
    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=draft, route="DPS_LOOKUP_FAILED"
    )
    assert eligibility.decision != "SAFE"


def test_no_order_number_asks_for_it_without_touching_dps(database) -> None:
    """CASE D, end to end. The DPS agent must not be called at all."""

    inquiry_id = store_inquiry(
        database, "E2E-NO-ORDER", question="언제 발송되나요?", order_id=None
    )
    stub = _StubDps(dps_success("2026-08-28"))

    AnswerService(
        database, dps_enrichment=stub, hybrid_service=_ForbiddenHybrid()
    ).generate_for_inquiry(inquiry_id)

    assert stub.calls == []
    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    assert draft is not None
    assert "일반 주문번호가 필요합니다" in draft["original_answer"]


def test_product_order_id_alone_is_not_treated_as_an_order_number(database) -> None:
    """A product order id is not the number DPS takes."""

    inquiry_id = store_inquiry(
        database,
        "E2E-PRODUCT-ORDER-ID",
        question="배송이 언제 되나요?",
        order_id=None,
        product_order_id="2026082198559811",
    )
    stub = _StubDps(dps_success("2026-08-28"))

    AnswerService(
        database, dps_enrichment=stub, hybrid_service=_ForbiddenHybrid()
    ).generate_for_inquiry(inquiry_id)

    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    assert draft is not None
    assert draft["original_answer"].strip()


def test_already_answered_inquiry_is_blocked_by_idempotency(database) -> None:
    inquiry_id = store_inquiry(
        database, "E2E-ANSWERED", question="언제설치가능한가요?"
    )
    generate(database, inquiry_id, dps_success("2026-08-28"))
    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)

    inquiry = dict(InquiryRepository(database).get(inquiry_id))
    inquiry["source_answered"] = 1

    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=draft, route="DELIVERY_WITH_INSTALLATION_DATE"
    )
    assert eligibility.decision == "BLOCKED"
    assert eligibility.stage == "IDEMPOTENCY"
