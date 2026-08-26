"""Phase 1 semantic coverage: measure the gap, change nothing.

The gap is measured, not assumed. When the shipping block handed
``install_existing_order_answer`` to 84 questions it did not answer, the
existing validator passed **84 of 84** -- it checks whether an answer is safe,
not whether it is about the same subject as the question.

This suite pins two things:

* the evaluator's verdicts on the operational answers that exposed the gap,
  on the safe-limitation and operational-request wordings that must *not* be
  read as evasions, and on the ordinary answers whose false-positive rate is
  the whole point of running Phase 1 in observation mode;
* the soft-gate invariant -- with the evaluator off and on, the same inquiry
  must produce the same validator verdict, the same requires_review, the same
  eligibility decision and the same auto-post outcome. Only telemetry differs.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.semantic_coverage_service import (
    ENABLED_ENV,
    FAIL,
    PARTIAL,
    PASS,
    UNKNOWN,
    SemanticCoverageService,
    is_enabled,
    topics_of,
)


PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
ORDER_NUMBER = "2026082198559811"

NOTICE_ANSWER = (
    "설치 예정일 관련 알림톡은 설치일 전날 수취인의 카카오톡으로 발송됩니다.\n\n"
    "확인이 안 되시는 경우 02-706-2678로 문의해 주세요.\n"
    "상담 가능 시간: 영업일 오전 10시 ~ 오후 5시"
)
NEW_ORDER_ANSWER = (
    "방문 설치 상품은 결제 확인 후 설치 기사님 일정에 맞춰 배송·설치가 진행됩니다.\n\n"
    "설치 일정 관련 알림톡은 결제 후 수취인의 카카오톡으로 발송되며, "
    "안내에 따라 일정을 확인하고 조율하실 수 있습니다."
)
AS_ANSWER = (
    "제품 사용 중 고장이나 불량이 의심되는 경우 삼성전자 서비스센터를 통해 "
    "점검 및 A/S를 받으실 수 있습니다."
)
ORDER_ID_REQUEST_ANSWER = (
    "배송 또는 설치 일정을 확인하려면 네이버 구매내역에 표시된 일반 주문번호가 "
    "필요합니다. 네이버 앱 또는 웹에서 확인해 주세요."
)


def evaluate(question: str, answer: str, route: str = ""):
    return SemanticCoverageService().evaluate(
        question=question, answer=answer, route=route
    )


# ==========================================================================
# 1. The wrong answers that exposed the gap  (spec section 14)
# ==========================================================================


@pytest.mark.parametrize(
    ("case", "question"),
    [
        ("보증기간", "보증기간이 얼마나 되나요?"),
        ("캐시백", "캐시백 받을 수 있나요?"),
        ("파손", "배송 중 깨진 것 같은데 어떻게 하나요?"),
        ("주말배송", "토요일에도 배송되나요?"),
        ("배송기간", "구매 후 배송까지 며칠 정도 걸리나요?"),
        ("A/S", "A/S 기간이 얼마나 되나요?"),
        ("포인트", "지금 올려도 네이버포인트 혜택을 받을 수 있나요?"),
        ("배송지역", "제주도 배송 가능한가요?"),
    ],
)
def test_notice_template_fails_coverage_for_unrelated_questions(
    case: str, question: str
) -> None:
    result = evaluate(question, NOTICE_ANSWER)

    assert result.status == FAIL, f"{case}: {result.status}"
    assert result.covered == 0, case
    assert result.uncovered >= 1, case


@pytest.mark.parametrize(
    "question",
    [
        "설치일 알림톡 언제 오나요?",
        "설치 예정일 문자는 언제 오나요?",
        "기사님 방문 전에 연락 오나요?",
        "설치 알림톡은 언제 발송되나요?",
        "배송 안내 문자는 언제 오나요?",
    ],
)
def test_notice_template_passes_coverage_for_notice_questions(
    question: str,
) -> None:
    result = evaluate(question, NOTICE_ANSWER)

    assert result.status == PASS, question
    assert result.covered == result.total


def test_business_hours_are_not_read_as_a_delivery_duration() -> None:
    """The notice template ends with "상담 가능 시간: 영업일 오전 10시".

    Counting that as a duration would make the template answer "며칠 걸리나
    요?" again -- the exact failure this evaluator exists to see.
    """

    assert "DELIVERY_DURATION" not in topics_of(NOTICE_ANSWER)


# ==========================================================================
# 2. Compound questions  (spec section 15)
# ==========================================================================


def test_compound_answered_in_part_is_partial() -> None:
    result = evaluate(
        "토요일에도 배송되나요?\n주문하면 며칠 걸리나요?",
        "주문 후 배송까지는 보통 2~3일 정도 소요됩니다.",
    )

    assert result.status == PARTIAL
    assert result.total == 2
    assert result.covered == 1
    assert result.uncovered == 1


def test_compound_answered_in_full_is_pass() -> None:
    result = evaluate(
        "토요일에도 배송되나요?\n주문하면 며칠 걸리나요?",
        "토요일 배송은 지역에 따라 다르며, 주문 후 배송까지는 보통 2~3일 소요됩니다.",
    )

    assert result.status == PASS
    assert result.covered == 2


def test_wall_mount_answer_does_not_cover_the_bracket_question() -> None:
    """Two questions, not one installation topic.

    Folding 브라켓 into the installation topic let an answer about wall
    mounting alone count as covering "브라켓도 따로 구매해야 하나요?".
    """

    result = evaluate(
        "벽걸이 가능한가요?\n브라켓도 따로 구매해야 하나요?",
        "해당 제품은 벽걸이 설치가 가능합니다.",
    )

    assert result.status == PARTIAL
    assert result.covered == 1


def test_both_installation_questions_answered_is_pass() -> None:
    result = evaluate(
        "벽걸이 가능한가요?\n브라켓도 따로 구매해야 하나요?",
        "벽걸이 설치가 가능하며, 브라켓은 별도 구매가 필요합니다.",
    )

    assert result.status == PASS
    assert result.covered == 2


def test_safe_limitation_on_the_second_question_still_covers_it() -> None:
    """Admitting a limit is a response; only silence is not."""

    result = evaluate(
        "벽걸이 가능한가요?\n브라켓도 따로 구매해야 하나요?",
        "벽걸이 설치가 가능합니다. 브라켓 규격 정보가 확인되지 않아 정확한 "
        "호환 여부는 확인이 필요합니다.",
    )

    assert result.status == PASS
    assert result.covered == 2


# ==========================================================================
# 3. What must not be judged by word overlap  (spec section 6)
# ==========================================================================


def test_shared_words_without_an_answer_do_not_pass() -> None:
    """"배송 관련해서 확인해보겠습니다" shares the most distinctive word."""

    result = evaluate("언제 배송되나요?", "배송 관련해서 확인해보겠습니다.")

    assert result.status != PASS


def test_different_words_that_do_answer_are_covered() -> None:
    result = evaluate(
        "주말에도 받을 수 있나요?",
        "토요일 및 공휴일 배송 가능 여부는 주문별 일정에 따라 확인이 필요합니다.",
    )

    assert result.status == PASS


# ==========================================================================
# 4. Safe limitation and operational requests  (spec sections 9, 10)
# ==========================================================================


def test_whole_question_missed_is_fail() -> None:
    result = evaluate("캐시백 가능한가요?", "배송 일정은 주문 후 안내됩니다.")

    assert result.status == FAIL


def test_admitting_a_limit_is_covered() -> None:
    result = evaluate(
        "이 브라켓 정확히 호환되나요?",
        "브라켓 규격 정보가 확인되지 않아 정확한 호환 여부는 확답하기 "
        "어렵습니다. 구매 전 규격 확인이 필요합니다.",
    )

    assert result.status == PASS


def test_referring_an_action_to_staff_is_covered() -> None:
    """Coverage and auto-post safety are separate questions.

    The eligibility gate still holds this for a person; that is not this
    evaluator's business, and saying "a person will handle it" *is* a response
    to "please change it".
    """

    result = evaluate(
        "토요일로 설치일 변경해주세요.", "설치 일정 변경은 담당자 확인이 필요합니다."
    )

    assert result.status == PASS


def test_a_phone_number_alone_covers_nothing() -> None:
    """Otherwise a referral sentence would whitewash every wrong answer."""

    result = evaluate(
        "보증기간이 얼마나 되나요?",
        "자세한 내용은 02-706-2678로 문의해 주세요.",
    )

    assert result.status != PASS


def test_asking_for_the_order_number_answers_an_order_question() -> None:
    result = evaluate(
        "언제 배송되나요?", ORDER_ID_REQUEST_ANSWER, route="ORDER_ID_REQUEST"
    )

    assert result.status == PASS


# ==========================================================================
# 5. Ordinary answers must not be flagged  (spec section 16)
# ==========================================================================


@pytest.mark.parametrize(
    ("case", "question", "answer", "route"),
    [
        ("제품 크기", "제품 크기가 어떻게 되나요?", "가로 1110.8mm, 세로 643.8mm입니다.", ""),
        ("HDMI", "HDMI 단자가 몇 개인가요?", "HDMI 단자는 3개입니다.", ""),
        ("벽걸이", "벽걸이 설치 가능한가요?", "해당 제품은 벽걸이 설치가 가능합니다.", ""),
        ("배송기간", "주문하면 보통 며칠 걸리나요?", NEW_ORDER_ANSWER, "TEMPLATE"),
        ("주문번호 요청", "언제 배송되나요?", ORDER_ID_REQUEST_ANSWER, "ORDER_ID_REQUEST"),
        (
            "DPS 확정일",
            "언제설치가능한가요?",
            "확인 결과 설치 예정일은 2026년 8월 28일입니다.",
            "DELIVERY_WITH_INSTALLATION_DATE",
        ),
        ("설치 알림톡", "설치 알림톡은 언제 발송되나요?", NOTICE_ANSWER, "TEMPLATE"),
        ("A/S 안내", "A/S 기간이 얼마나 되나요?", AS_ANSWER, "TEMPLATE"),
        (
            "주문 취소 직원확인",
            "주문 취소해주세요.",
            "주문 취소·환불은 담당자 확인이 필요합니다. 확인 후 안내드리겠습니다.",
            "",
        ),
    ],
)
def test_ordinary_answers_are_not_flagged(
    case: str, question: str, answer: str, route: str
) -> None:
    result = evaluate(question, answer, route=route)

    assert result.status == PASS, f"{case}: {result.status} / {result.reason}"


# ==========================================================================
# 6. UNKNOWN beats a forced verdict
# ==========================================================================


def test_unrecognised_answer_subject_is_unknown_not_fail() -> None:
    result = evaluate("이 제품 어떤가요?", "네, 확인해보겠습니다.")

    assert result.status == UNKNOWN


def test_empty_answer_is_unknown() -> None:
    assert evaluate("보증기간이 얼마나 되나요?", "").status == UNKNOWN


def test_empty_question_is_unknown() -> None:
    assert evaluate("", NOTICE_ANSWER).status == UNKNOWN


def test_result_serialises_the_fields_telemetry_needs() -> None:
    payload = evaluate(
        "토요일에도 배송되나요?\n주문하면 며칠 걸리나요?",
        "주문 후 배송까지는 보통 2~3일 정도 소요됩니다.",
    ).to_dict()

    for key in (
        "status", "reason", "total_subquestions", "covered_subquestions",
        "uncovered_subquestions", "unknown_subquestions", "score",
        "subquestions", "phase",
    ):
        assert key in payload
    assert payload["phase"] == "SOFT_OBSERVATION_ONLY"
    assert json.dumps(payload, ensure_ascii=False)
    assert [item["status"] for item in payload["subquestions"]]


# ==========================================================================
# 7. The soft-gate invariant  (spec sections 11, 21)
# ==========================================================================


class _StubProvider:
    name = "openai"

    def generate_json(self, *, task, prompt, context):
        if task == "UNDERSTANDING":
            return {
                "category": "GENERAL", "questions": ["q"], "urgency": "NORMAL",
                "emotion": "NORMAL", "confidence": 0.95,
                "requires_review": False, "reason": "stub",
            }
        if task == "DRAFT":
            return {
                "answer": "해당 제품은 벽걸이 설치가 가능합니다.",
                "confidence": 0.9, "used_facts": [],
                "missing_information": [], "requires_review": False,
                "warnings": [], "learning_usage": [], "historical_usage": [],
                "feedback_signal_usage": [],
                "subquestion_results": [
                    {"subquestion": "q", "answered": True,
                     "status": "ANSWERABLE"},
                ],
            }
        if task == "SELF_REVIEW":
            return {
                "passed": True, "answered_all_questions": True,
                "has_speculation": False, "facts_consistent": True,
                "requires_review": False, "reason": "stub", "warnings": [],
            }
        raise AssertionError(task)


class _FakeDps:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def enrich(self, request, **kwargs):
        self.calls.append(request.order_id)
        request.metadata["dps"] = {
            "lookup_required": True, "lookup_status": "SUCCESS",
            "installation_date": "2026-08-28",
            "installation_date_display": "2026-08-28",
            "delivery_status": "구매요청", "installation_status": "구매요청",
            "change_request": False, "general_segments": [],
            "dps_segments": [], "warnings": [], "cache_used": True,
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=True),
            metadata=request.metadata["dps"], lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
        request.metadata["dps"] = {
            "lookup_required": False, "lookup_status": "NOT_REQUIRED",
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"], lookup_row=None,
        )


# Questions chosen so the set spans a coverage FAIL, a PASS and a delivery
# route: if the evaluator could move a decision anywhere, it would show here.
INVARIANT_CASES = [
    ("coverage-fail", "보증기간이 얼마나 되나요?", None),
    ("coverage-pass", "설치일 알림톡 언제 오나요?", None),
    ("weekend", "토요일에도 배송되나요?", None),
    ("compound", "벽걸이 가능한가요?\n브라켓도 따로 구매해야 하나요?", None),
    ("order-missing", "언제 배송되나요?", None),
    ("dps", "언제설치가능한가요?", ORDER_NUMBER),
    ("cancel", "주문 취소해주세요.", ORDER_NUMBER),
]


def _run(tmp_path, label: str, question: str, order_id: str | None):
    from services.hybrid_answer_service import HybridAnswerService

    database = Database(tmp_path / f"{label}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S", "source_type": "NAVER",
            "source_question_id": f"sc-{label}",
            "inquiry_type": "CUSTOMER_INQUIRY" if order_id else "PRODUCT_INQUIRY",
            "title": "문의", "content": question, "product_name": PRODUCT,
            "order_id": order_id, "product_order_id": None, "raw_json": {},
        }
    ).inquiry_id
    error: str | None = None
    try:
        AnswerService(
            database,
            dps_enrichment=_FakeDps(),
            hybrid_service=HybridAnswerService(_StubProvider()),
        ).generate_for_inquiry(inquiry_id)
    except Exception as exc:
        error = type(exc).__name__

    draft = AnswerRepository(database).latest_for_inquiry(inquiry_id)
    if draft is None:
        return {"error": error, "draft": None}, None

    record = dict(draft)
    for key in ("metadata_json", "validator_result_json"):
        raw = record.get(key)
        if isinstance(raw, str):
            try:
                record[key] = json.loads(raw)
            except ValueError:
                record[key] = {}
    metadata = record.get("metadata_json") or {}
    plan = metadata.get("processing_plan") or {}
    eligibility = AutoProcessingEligibilityService().evaluate(
        inquiry=InquiryRepository(database).get(inquiry_id),
        draft=record,
        route=str(metadata.get("selected_answer_route") or ""),
    )
    decisions = {
        "error": error,
        "answer": record.get("original_answer"),
        "validation_status": record.get("validation_status"),
        "review_status": record.get("review_status"),
        "program_status": record.get("program_status"),
        "requires_review": plan.get("needs_staff_review"),
        "selected_answer_route": metadata.get("selected_answer_route"),
        "eligibility_decision": eligibility.decision,
        "eligibility_stage": eligibility.stage,
        "eligibility_reasons": tuple(eligibility.reasons),
        "auto_post": eligibility.decision == "SAFE",
    }
    return decisions, metadata.get("semantic_coverage")


@pytest.mark.parametrize(("label", "question", "order_id"), INVARIANT_CASES)
def test_soft_gate_changes_no_production_decision(
    tmp_path, monkeypatch, label: str, question: str, order_id: str | None
) -> None:
    """The Phase 1 invariant, proven per case rather than asserted."""

    monkeypatch.setenv(ENABLED_ENV, "0")
    assert is_enabled() is False
    off_decisions, off_coverage = _run(
        tmp_path / "off", label, question, order_id
    )

    monkeypatch.setenv(ENABLED_ENV, "1")
    assert is_enabled() is True
    on_decisions, on_coverage = _run(
        tmp_path / "on", label, question, order_id
    )

    assert off_decisions == on_decisions, (
        f"{label}: semantic coverage altered a production decision"
    )
    assert off_coverage is None, f"{label}: telemetry written while disabled"
    if on_decisions.get("draft") is not None or on_decisions.get("answer"):
        assert on_coverage is not None, f"{label}: telemetry not recorded"


def test_soft_gate_records_telemetry_without_touching_eligibility(
    tmp_path, monkeypatch
) -> None:
    """A coverage FAIL on an otherwise clean answer stays auto-postable."""

    monkeypatch.setenv(ENABLED_ENV, "1")
    decisions, coverage = _run(
        tmp_path / "fail", "warranty", "보증기간이 얼마나 되나요?", None
    )

    assert coverage is not None
    assert coverage["phase"] == "SOFT_OBSERVATION_ONLY"
    # Whatever the coverage verdict, it did not become a blocking reason.
    for reason in decisions["eligibility_reasons"]:
        assert "SEMANTIC" not in reason
        assert "COVERAGE" not in reason


def test_eligibility_never_reads_the_coverage_key() -> None:
    """Structural guard: the gate must not learn to read this key by accident."""

    import inspect

    source = inspect.getsource(AutoProcessingEligibilityService)

    assert "semantic_coverage" not in source
    assert "SEMANTIC_COVERAGE" not in source


def test_no_external_call_is_made(monkeypatch) -> None:
    """The evaluator is deterministic: no provider, no network."""

    import socket

    def _blocked(*args: Any, **kwargs: Any):
        raise AssertionError("semantic coverage attempted a network call")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    result = evaluate("보증기간이 얼마나 되나요?", NOTICE_ANSWER)

    assert result.status == FAIL


# ==========================================================================
# 8. The two precision rules the Phase 1 audit produced
# ==========================================================================


def test_topics_are_read_per_sentence_not_per_answer() -> None:
    """An answer often answers first and mentions the notice afterwards.

    Suppressing the schedule topic across the whole text because a later
    sentence mentions the 알림톡 was the largest single source of false
    positives in the audit: it made the new-order body stop answering
    "언제 받을 수 있나요?".
    """

    topics = topics_of(NEW_ORDER_ANSWER)

    assert "NOTIFICATION" in topics
    assert "INSTALLATION_SCHEDULE" in topics

    # The notice template is still only about the notice: there its schedule
    # mention sits inside the same sentence as the 알림톡.
    notice_topics = topics_of(NOTICE_ANSWER)
    assert "NOTIFICATION" in notice_topics
    assert "INSTALLATION_SCHEDULE" not in notice_topics
    assert "DELIVERY_SCHEDULE" not in notice_topics


def test_new_order_body_answers_when_will_i_get_it() -> None:
    result = evaluate("오늘 주문하면 언제 받을 수 있나요?", NEW_ORDER_ANSWER)

    assert result.status == PASS


def test_bracket_answer_covers_a_mounting_question_but_not_the_reverse() -> None:
    """The responsive edge is deliberately one-way."""

    covered = evaluate(
        "기존 스탠드와 호환되나요?",
        "50인치 이상 제품은 모델별 무게와 베사 규격 확인이 필요합니다.",
    )
    assert covered.status == PASS

    still_uncovered = evaluate(
        "벽걸이 가능한가요?\n브라켓도 따로 구매해야 하나요?",
        "해당 제품은 벽걸이 설치가 가능합니다.",
    )
    assert still_uncovered.status == PARTIAL
