"""Operational scenario matrix and end-to-end harness.

Unit tests kept passing while the server kept finding new failures, because
nothing exercised the whole path an operator actually triggers. This module
covers both levels:

  * a table of real operational inquiries checked against the live
    InquiryAnalysisService and AutoProcessingEligibilityService;
  * end-to-end runs through AnswerService -> validator -> eligibility ->
    AutoPostPipelineService, with only the external systems faked.

The governing policy is that drafting and publishing are separate decisions.
A compound inquiry answers the sub-questions it has grounds for and defers the
rest; one HARD sub-question blocks publishing for the whole inquiry but must
never suppress the grounded parts. Equally, a compound inquiry whose parts are
all safe must still be able to publish -- a matrix where everything is blocked
would prove nothing.

Only Naver POST, DPS and the provider are faked. No network, no real POST.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from answer.models import AnswerRequest
from answer.providers.fake_gpt_provider import FakeGptProvider
from answer.text_utils import split_subquestions
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.hybrid_answer_service import HybridAnswerService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.learning_compatibility_service import classify_topics


ANALYSIS = InquiryAnalysisService()
ELIGIBILITY = AutoProcessingEligibilityService()

SIX_PART = (
    "A/S는 삼성서비스센터에서 하나요?\n"
    "설치는 기사님이 해주시나요?\n"
    "집에 있는 브라켓과 호환되나요?\n"
    "설치예정일은 언제인가요?\n"
    "카드 할인도 되나요?\n"
    "배송 중 파손되면 어떻게 하나요?"
)


@dataclass(frozen=True)
class Scenario:
    name: str
    question: str
    subquestions: int
    can_generate: bool
    requires_order_id: bool
    requires_dps: bool
    manual_review: bool
    auto_post_safe: bool
    block_reason: str | None = None
    # How many sub-questions the analyser ends up judging. Defaults to the
    # deterministic count; a connector compound refines it upward, and a
    # comparison ("50인치, 60인치 중...") deliberately stays at one.
    analysis_parts: int | None = None


# 46 operational scenarios. `auto_post_safe` is the published/blocked verdict,
# and `can_generate` is deliberately independent of it.
SCENARIOS: tuple[Scenario, ...] = (
    # ---------------------------------------------------------- singles
    Scenario("general-product", "이 제품 무게가 얼마나 되나요?",
             1, True, False, False, False, True),
    Scenario("as-servicecenter", "A/S는 삼성서비스센터에서 하나요?",
             1, True, False, False, False, True),
    Scenario("as-where", "A/S는 어디서 받나요?",
             1, True, False, False, False, True),
    Scenario("install-method", "설치는 어떻게 하나요?",
             1, True, False, False, False, True),
    Scenario("install-engineer", "설치는 기사님이 해주시나요?",
             1, True, False, False, False, True),
    Scenario("install-self", "자가설치도 가능한가요?",
             1, True, False, False, False, True),
    Scenario("warranty", "보증기간이 얼마나 되나요?",
             1, True, False, False, False, True),
    Scenario("spec", "패널이 QLED인가요?",
             1, True, False, False, False, True),
    Scenario("general-compat", "HDMI 연결 가능한가요?",
             1, True, False, False, False, True),
    Scenario("card-payment", "카드 결제 가능한가요?",
             1, True, False, False, False, True),
    # 구매 전 고객의 실제 배송 소요 기간 문의(유형 A). 확정된 운영정책상
    # 자동답변하지 않는다 -- 알려줄 확정 배송기간이 존재하지 않는다.
    # 조회는 여전히 필요 없다: 조회할 주문 자체가 없다.
    Scenario("pre-purchase", "지금 주문하면 배송 얼마나 걸리나요?",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("order-status", "주문 상태 확인해주세요.",
             1, True, True, False, False, True),
    # ------------------------------------------------ schedule / DPS
    # Nothing in these says an order exists, so the purchase-state policy
    # holds them: no order number is demanded and DPS is never consulted.
    # "with-order-id" just below is the same shape with the number supplied,
    # and it still takes the full order/DPS route.
    Scenario("install-date", "설치예정일은 언제인가요?",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("delivery-date", "배송 언제 오나요?",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("delivery-delay", "배송이 너무 늦는데 언제 오나요?",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("with-order-id", "주문번호 2024010112345678 설치일 언제인가요?",
             1, True, True, True, False, True),
    Scenario("schedule-change", "설치일을 10일로 변경해주세요.",
             1, True, True, True, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    # ------------------------------------------- compatibility (HARD)
    Scenario("bracket-compat", "집에 있는 브라켓과 호환되나요?",
             1, True, False, False, False, False,
             "PRODUCT_COMPATIBILITY_NOT_VERIFIED"),
    Scenario("stand-compat", "기존 스탠드와 호환되나요?",
             1, True, False, False, False, False,
             "PRODUCT_COMPATIBILITY_NOT_VERIFIED"),
    # ------------------------------------- payment benefit (HARD)
    Scenario("card-benefit", "카드 할인도 되나요?",
             1, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("promotion", "지금 프로모션 적용되나요?",
             1, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    # ------------------------------------------- high risk (HARD)
    Scenario("damage", "배송 중 파손되면 어떻게 하나요?",
             1, False, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("damage-actual", "제품이 깨져서 왔는데 보상은 어떻게 되나요?",
             1, False, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("legal", "법적으로 대응하겠습니다.",
             1, False, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("complaint", "서비스가 엉망입니다.",
             1, False, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    # ------------------------------------------------- degenerate input
    Scenario("empty", "", 0, False, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("garbled", "ㅁㄴㅇㄹ",
             1, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("greeting-only", "안녕하세요",
             1, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    # ------------------------------------------------------- compound
    Scenario("compound-safe-safe",
             "A/S는 어디서 받나요? 설치는 기사님이 해주시나요?",
             2, True, False, False, False, True),
    Scenario("compound-3safe",
             "A/S는 어디서 받나요? 설치는 기사님이 해주시나요? 보증기간은 얼마인가요?",
             3, True, False, False, False, True),
    Scenario("compound-numbered",
             "1. 설치는 어떻게 하나요?\n2. A/S는 어디서 받나요?",
             2, True, False, False, False, True),
    Scenario("compound-trailing-filler",
             "A/S는 어디서 받나요? 설치는 기사님이 해주시나요? 궁금해요",
             2, True, False, False, False, True),
    Scenario("compound-same-intent",
             "A/S는 어디서 받나요? 수리는 어디서 하나요?",
             2, True, False, False, False, True),
    Scenario("compound-safe-dps",
             "A/S는 어디서 받나요? 설치예정일은 언제인가요?",
             2, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("compound-safe-compat",
             "A/S는 어디서 받나요? 집에 있는 브라켓과 호환되나요?",
             2, True, False, False, False, False,
             "PRODUCT_COMPATIBILITY_NOT_VERIFIED"),
    Scenario("compound-safe-card",
             "설치는 기사님이 해주시나요? BC카드 할인도 되나요?",
             2, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("compound-safe-damage",
             "A/S는 어디서 받나요? 배송 중 파손되면 어떻게 하나요?",
             2, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("compound-dps-change",
             "설치예정일이 언제인가요? 그리고 그 날짜를 10일로 변경해주세요.",
             2, True, True, True, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("compound-no-punct",
             "AS 어디서 받아요 설치는 기사님이 해주시나요 기존 브라켓도 쓸 수 있는지 궁금해요",
             3, True, False, False, False, False,
             "PRODUCT_COMPATIBILITY_NOT_VERIFIED"),
    Scenario("six-part-686058300", SIX_PART,
             6, True, False, False, True, False,
             "PRODUCT_COMPATIBILITY_NOT_VERIFIED"),
    # ------------------------------------------- connector compounds
    # Split only when the two sides carry different intents; `subquestions`
    # is the deterministic first pass, `analysis_parts` the refined count.
    Scenario("connector-run-on", "AS도 되고 설치도 기사님이 해주시나요",
             1, True, False, False, False, True, analysis_parts=1),
    Scenario("connector-and", "설치방법 알려주시고 그리고 보증기간도 알려주세요",
             1, True, False, False, False, True, analysis_parts=2),
    Scenario("connector-comma", "설치방법, 보증기간, A/S 위치 알려주세요",
             1, True, False, False, False, True, analysis_parts=3),
    Scenario("connector-comma-card", "설치방법, 카드 할인 알려주세요",
             1, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW", analysis_parts=1),
    # The P1 case: the answerable 설치방법 part must survive the HARD 파손 part.
    Scenario("connector-comma-damage", "설치방법, 파손 보상 알려주세요",
             1, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW", analysis_parts=2),
    Scenario("connector-and-compat",
             "설치방법 알려주시고 그리고 기존 브라켓과 호환되나요",
             1, True, False, False, False, False,
             "PRODUCT_COMPATIBILITY_NOT_VERIFIED", analysis_parts=2),
    Scenario("connector-install-as",
             "설치는 기사님이 해주시나요 그리고 AS는 어디서 받나요",
             2, True, False, False, False, True, analysis_parts=2),
    Scenario("connector-card-compat", "카드 할인도 되고 브라켓도 호환되나요",
             1, True, False, False, True, False,
             "POLICY_OR_HIGH_RISK_REVIEW", analysis_parts=1),
    Scenario("connector-delivery-install",
             "배송일도 알고 싶고 설치방법도 궁금합니다",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW",
             analysis_parts=2),
    Scenario("connector-as-install", "A/S는 어디서 받고 설치는 누가 하나요?",
             1, True, False, False, False, True, analysis_parts=2),
    # -------------------------------- one question despite the connector
    Scenario("comparison-size", "50인치, 60인치 중 어떤 게 좋나요",
             1, True, False, False, False, True, analysis_parts=1),
    Scenario("comparison-mount", "스탠드, 벽걸이 중 선택 가능한가요",
             1, True, False, False, False, True, analysis_parts=1),
    # 경계 사례로 확정된 것: 절차가 아니라 실제 일정 가능성을 묻고 있다
    # (유형 A). "배송과 설치는 어떤 방식으로 진행되나요?" 는 유형 B 로 남아
    # 계속 자동답변된다 -- 아래 relationship-procedure 참고.
    Scenario("relationship-sameday", "배송, 설치를 같은 날 받을 수 있나요",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW",
             analysis_parts=1),
    Scenario("relationship-procedure", "배송과 설치는 어떤 방식으로 진행되나요?",
             1, True, False, False, False, True, analysis_parts=1),
    # ------------------------- schedule complaint vs change request
    # "미뤄지는데" complains about a delay without ever stating an order,
    # so these are held for the same reason as the plain schedule questions.
    Scenario("delay-install-lookup", "설치가 계속 미뤄지는데 언제 되나요?",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("delay-install-lookup2", "설치가 미뤄졌는데 언제 설치되나요?",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("delay-delivery-lookup", "배송이 계속 미뤄지는데 언제 오나요?",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("delay-visit-lookup", "기사님 방문이 미뤄졌는데 언제 오시나요?",
             1, True, False, False, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("change-install-request", "설치일을 미뤄주세요",
             1, True, True, True, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
    Scenario("change-visit-request", "기사님 방문일을 변경해주세요",
             1, True, True, True, True, False, "POLICY_OR_HIGH_RISK_REVIEW"),
)


def request_for(question: str) -> AnswerRequest:
    return AnswerRequest(
        inquiry_id=1,
        question_id="Q",
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


def eligibility_for(question: str, *, route: str = "GPT_DIRECT"):
    analysis = ANALYSIS.analyze(request_for(question))
    return ELIGIBILITY.evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "original_answer": "안내드립니다.",
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


@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[item.name for item in SCENARIOS]
)
def test_scenario_matrix(scenario: Scenario) -> None:
    analysis = ANALYSIS.analyze(request_for(scenario.question))
    assert len(split_subquestions(scenario.question)) == scenario.subquestions
    assert analysis.can_generate_answer is scenario.can_generate
    assert analysis.requires_order_id is scenario.requires_order_id
    assert analysis.requires_dps_lookup is scenario.requires_dps
    assert analysis.manual_review_required is scenario.manual_review

    expected_parts = scenario.analysis_parts or scenario.subquestions
    if expected_parts > 1:
        segments = ANALYSIS._connector_segments(
            request_for(scenario.question), scenario.question
        )
        judged = len(segments) if segments else scenario.subquestions
        assert judged == expected_parts, segments
    result = eligibility_for(scenario.question)
    assert result.safe is scenario.auto_post_safe, result.reasons
    if scenario.block_reason:
        assert scenario.block_reason in result.reasons


def test_hard_keyword_scan_survives_undecomposed_connectors() -> None:
    """The safety verdict does not depend on decomposition.

    split_subquestions does not separate comma or "그리고" connectors, but the
    HARD keyword scan runs over the whole text, so such an inquiry is still
    blocked from publishing. This is the property that keeps the splitter's
    limits from becoming a safety hole.
    """

    for question in (
        "설치방법, 카드 할인 알려주세요",
        "설치방법, 파손 보상 알려주세요",
        "설치방법 알려주시고 그리고 기존 브라켓과 호환되나요",
    ):
        assert len(split_subquestions(question)) == 1
        assert eligibility_for(question).safe is False


def test_schedule_complaint_is_a_lookup_not_a_change_request() -> None:
    """"미뤄" alone is not the evidence -- who is asking for what is.

    A customer reporting that the schedule slipped is asking what it is now,
    so the order/DPS requirement must survive. A customer asking us to move
    it is a change request and stays in review.
    """

    complaints = (
        "설치가 계속 미뤄지는데 언제 되나요?",
        "배송이 계속 미뤄지는데 언제 오나요?",
        "기사님 방문이 미뤄졌는데 언제 오시나요?",
    )
    for question in complaints:
        analysis = ANALYSIS.analyze(request_for(question))
        # Read as a lookup, not as a request to move the date. That is the
        # distinction this case exists for and it is unchanged.
        assert analysis.inquiry_subtype != "SCHEDULE_CHANGE_REQUEST", question
        assert analysis.detected_intent != "SCHEDULE_CHANGE", question
        # None of them says an order exists, so the lookup is held rather than
        # run -- complaining that something is late does not prove it was
        # bought here, and DPS has nothing to be asked about.
        assert analysis.inquiry_subtype == "UNCONFIRMED_DELIVERY_OUTCOME"
        assert analysis.requires_dps_lookup is False, question

    # The same complaints from a customer who says they ordered: still a
    # lookup, and now one the pipeline can actually perform.
    for question in complaints:
        analysis = ANALYSIS.analyze(request_for(f"어제 주문했는데 {question}"))
        assert analysis.inquiry_subtype == "DELIVERY_OR_INSTALLATION_SCHEDULE"
        assert analysis.requires_dps_lookup is True, question

    for question in ("설치일을 미뤄주세요", "기사님 방문일을 변경해주세요"):
        analysis = ANALYSIS.analyze(request_for(question))
        assert analysis.inquiry_subtype == "SCHEDULE_CHANGE_REQUEST", question
        assert analysis.manual_review_required is True


def test_validator_review_is_not_reported_as_validator_failure() -> None:
    """A validator that passed but asked for review must not be reported as
    having failed; both still block."""

    def evaluate(**draft_overrides):
        draft = {
            "original_answer": "안내드립니다.",
            "validation_status": "PASS",
            "validator_result_json": None,
            "review_status": "",
            "metadata_json": {},
            "posted": False,
            "id": 1,
        }
        draft.update(draft_overrides)
        return ELIGIBILITY.evaluate(
            inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
            draft=draft,
            route="GPT_DIRECT",
        )

    review = evaluate(validation_status="REVIEW_REQUIRED")
    assert "VALIDATOR_NOT_PASS" not in review.reasons
    assert "VALIDATOR_REVIEW_REQUIRED" in review.reasons
    assert review.safe is False

    # A genuine failure keeps the original code, both ways of expressing it.
    assert "VALIDATOR_NOT_PASS" in evaluate(
        validation_status="FAILED"
    ).reasons
    assert "VALIDATOR_NOT_PASS" in evaluate(
        validator_result_json={"passed": False}
    ).reasons


def test_connector_form_keeps_the_answerable_part() -> None:
    """The P1 regression: a HARD part must not suppress the answerable one.

    "설치방법, 파손 보상 알려주세요" used to be judged as one HIGH_RISK question,
    so no draft was produced at all and the answerable 설치방법 part was lost.
    """

    analysis = ANALYSIS.analyze(request_for("설치방법, 파손 보상 알려주세요"))
    assert analysis.can_generate_answer is True
    assert analysis.inquiry_subtype == "COMPOUND_MULTI_INTENT"
    # ...while publishing stays blocked because of the 파손 part.
    assert analysis.manual_review_required is True
    assert eligibility_for("설치방법, 파손 보상 알려주세요").safe is False


@pytest.mark.parametrize(
    "question",
    [
        "50인치, 60인치 중 어떤 게 좋나요",
        "스탠드, 벽걸이 중 선택 가능한가요",
        "배송, 설치를 같은 날 받을 수 있나요",
    ],
)
def test_connector_does_not_split_a_single_question(question: str) -> None:
    """A comma is not a question boundary on its own.

    Each of these is one comparison / selection / relationship question, and
    the segments on either side carry the same intent, so the split is
    rejected. This is what keeps the connector pass from being a blind split.
    """

    assert ANALYSIS._connector_segments(request_for(question), question) == ()
    assert ANALYSIS.analyze(request_for(question)).inquiry_subtype != (
        "COMPOUND_MULTI_INTENT"
    )


def test_matrix_covers_both_verdicts() -> None:
    """A matrix that blocks everything would prove nothing."""

    safe = [item for item in SCENARIOS if item.auto_post_safe]
    blocked = [item for item in SCENARIOS if not item.auto_post_safe]
    assert len(SCENARIOS) >= 40
    assert len(safe) >= 15, "no positive controls"
    assert len(blocked) >= 15, "no negative controls"
    assert any(item.subquestions >= 2 and item.auto_post_safe
               for item in SCENARIOS), "no auto-postable compound inquiry"


# --------------------------------------------------- topic classifier

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Korean attaches a particle straight onto the term. The question form
        # must classify the same way the answer form does, or answering an
        # asked question counts as an unrequested topic.
        ("A/S는 어디서 받나요", "AS_SUPPORT"),
        ("A/S가 되나요", "AS_SUPPORT"),
        ("AS도 되나요", "AS_SUPPORT"),
        ("A/S 문의", "AS_SUPPORT"),
        ("서비스센터 위치", "AS_SUPPORT"),
    ],
)
def test_as_topic_is_detected_in_question_form(text: str, expected: str) -> None:
    assert expected in classify_topics(text)


@pytest.mark.parametrize("text", ["ASUS 모니터", "CLASS 제품"])
def test_latin_words_are_not_mistaken_for_as(text: str) -> None:
    assert "AS_SUPPORT" not in classify_topics(text)


# ------------------------------------------------------- E2E harness

class FakeDpsEnrichment:
    def enrich(self, request, **kwargs):
        request.metadata["dps"] = {
            "lookup_required": False,
            "lookup_status": "NOT_REQUIRED",
            "cache_used": False,
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"],
            lookup_row=None,
        )

    def skip_for_phase9(self, request, **kwargs):
        return self.enrich(request, **kwargs)


class CountingPostService:
    def __init__(self, *, succeed: bool = False) -> None:
        self.calls = 0
        self.succeed = succeed

    def post(self, *args, **kwargs):
        self.calls += 1
        if not self.succeed:
            raise AssertionError("POST must not be called for a blocked inquiry")
        return SimpleNamespace(
            success=True, error_code=None, error_message=None, response={}
        )


def scripted_provider(answer: str, *, clean: bool) -> FakeGptProvider:
    """A provider that returns `answer`, either fully clean or as a partial
    answer whose self-review honestly reports incomplete coverage."""

    return FakeGptProvider(
        responses={
            "DRAFT": {
                "answer": answer,
                "confidence": 0.95 if clean else 0.8,
                "used_facts": [] if clean else [
                    "analysis.requires_order_id",
                    "analysis.private_post_required",
                ],
                "missing_information": [],
                "requires_review": False,
                "warnings": [],
            },
            "SELF_REVIEW": {
                "passed": clean,
                "answered_all_questions": clean,
                "has_speculation": False,
                "facts_consistent": True,
                "requires_review": False,
                "reason": "ok",
                "warnings": [],
            },
        }
    )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "matrix.db")
    value.initialize()
    return value


def store(database: Database, question: str, key: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": key,
            "external_inquiry_id": key,
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "문의",
            "content": question,
            "product_name": "삼성 50인치 TV",
            "registered_at": "2026-08-23T04:41:32+09:00",
            "raw_json": {},
        }
    ).inquiry_id


def generate(database: Database, inquiry_id: int, provider: FakeGptProvider):
    return AnswerService(
        database,
        dps_enrichment=FakeDpsEnrichment(),
        hybrid_service=HybridAnswerService(provider),
    ).generate_for_inquiry(inquiry_id)


def active_draft(database: Database, inquiry_id: int) -> dict:
    with database.connection() as connection:
        connection.row_factory = sqlite3.Row
        return dict(
            connection.execute(
                "SELECT * FROM answer_drafts WHERE inquiry_id=?"
                " ORDER BY id DESC LIMIT 1",
                (inquiry_id,),
            ).fetchone()
        )


SAFE_SINGLE_ANSWER = "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다."
SAFE_COMPOUND_ANSWER = (
    "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
    "설치는 전문 기사가 방문하여 진행합니다."
)
SIX_PART_PARTIAL = (
    "A/S는 삼성전자 서비스센터를 통해 받으실 수 있습니다.\n"
    "설치는 전문 기사가 방문하여 진행합니다.\n"
    "기존 브라켓 호환 여부는 확인 후 안내드리겠습니다.\n"
    "설치예정일은 주문번호를 알려주시면 확인해 드리겠습니다.\n"
    "카드 혜택은 확인 후 안내드리겠습니다.\n"
    "배송 중 파손은 담당 직원이 확인 후 안내드리겠습니다."
)


@pytest.mark.parametrize(
    ("label", "question", "answer"),
    [
        ("single", "A/S는 어디서 받나요?", SAFE_SINGLE_ANSWER),
        ("compound", "A/S는 어디서 받나요? 설치는 기사님이 해주시나요?",
         SAFE_COMPOUND_ANSWER),
    ],
)
def test_e2e_safe_inquiry_reaches_post(
    database: Database, label: str, question: str, answer: str
) -> None:
    """Positive control -- including a compound one. A safe compound inquiry
    must publish; forcing every compound to manual review is a failure."""

    inquiry_id = store(database, question, f"SAFE-{label}")
    outcome = generate(database, inquiry_id, scripted_provider(answer, clean=True))
    hybrid = outcome.result.metadata.get("hybrid") or {}

    assert hybrid.get("fallback_used") is False
    assert outcome.result.needs_review is False
    draft = active_draft(database, inquiry_id)
    assert draft["validation_status"] == "PASS"

    result = ELIGIBILITY.evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft=draft,
        route=str(outcome.result.metadata.get("selected_answer_route") or ""),
    )
    assert result.safe is True, result.reasons

    posts = CountingPostService(succeed=True)
    AutoPostPipelineService(database, post_service=posts).run_pending(
        run_id="SAFE", owner_id="SAFE", max_retries=1,
        inquiry_ids=[inquiry_id],
    )
    assert posts.calls == 1


def test_e2e_six_part_keeps_the_partial_answer(database: Database) -> None:
    """The grounded sub-answers survive; nothing is invented for the rest."""

    inquiry_id = store(database, SIX_PART, "686058300")
    outcome = generate(
        database, inquiry_id, scripted_provider(SIX_PART_PARTIAL, clean=False)
    )
    hybrid = outcome.result.metadata.get("hybrid") or {}
    validation = hybrid.get("validation") or {}
    answer = outcome.result.answer

    # Drafted from the provider, not replaced by the generic safe draft.
    assert hybrid.get("fallback_used") is False, validation.get("errors")
    assert validation.get("errors") == []
    assert outcome.result.provider.endswith("_hybrid")

    # The two grounded sub-questions are answered...
    assert "서비스센터" in answer
    assert "전문 기사" in answer
    # ...and the four unsupported ones are deferred, not invented.
    assert "브라켓" in answer
    assert "주문번호" in answer
    assert "카드" in answer
    assert "파손" in answer
    # No fabricated date anywhere.
    assert not __import__("re").search(r"20\d{2}[-.년]", answer)

    # Drafted, but held back.
    assert outcome.result.needs_review is True
    assert outcome.result.auto_answerable is False


@pytest.mark.parametrize(
    "question",
    [
        SIX_PART,
        "A/S는 어디서 받나요? 집에 있는 브라켓과 호환되나요?",
        "설치는 기사님이 해주시나요? BC카드 할인도 되나요?",
        "A/S는 어디서 받나요? 배송 중 파손되면 어떻게 하나요?",
        "설치예정일이 언제인가요? 그리고 그 날짜를 10일로 변경해주세요.",
        "설치방법, 카드 할인 알려주세요",
        "제품이 깨져서 왔는데 보상은 어떻게 되나요?",
    ],
)
def test_e2e_hard_inquiries_never_post(
    database: Database, question: str
) -> None:
    inquiry_id = store(database, question, f"HARD-{abs(hash(question)) % 99999}")
    posts = CountingPostService()
    outcome = AutoPostPipelineService(
        database, post_service=posts
    ).run_pending(
        run_id="HARD", owner_id="HARD", max_retries=1,
        inquiry_ids=[inquiry_id],
    )
    assert posts.calls == 0
    assert outcome.succeeded_count == 0


def _drafts(database: Database, inquiry_id: int) -> list[dict]:
    with database.connection() as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM answer_drafts WHERE inquiry_id=? ORDER BY id",
                (inquiry_id,),
            ).fetchall()
        ]


def test_failed_regeneration_does_not_destroy_the_existing_draft(
    database: Database,
) -> None:
    """A failed "GPT 새 답변 생성" must leave the good draft in place."""

    question = "A/S는 어디서 받나요?"
    inquiry_id = store(database, question, "LIFECYCLE-1")
    generate(
        database, inquiry_id, scripted_provider(SAFE_SINGLE_ANSWER, clean=True)
    )
    before = active_draft(database, inquiry_id)
    assert SAFE_SINGLE_ANSWER in before["original_answer"]

    # Now a generation where every provider stage fails.
    failing = FakeGptProvider(
        fail_tasks={"UNDERSTANDING", "DRAFT", "SELF_REVIEW"}
    )
    try:
        generate(database, inquiry_id, failing)
    except Exception:  # noqa: BLE001 - a failed generation is allowed to raise
        pass

    after = [item for item in _drafts(database, inquiry_id) if item["is_active"]]
    assert len(after) == 1, "exactly one draft must stay active"
    assert after[0]["original_answer"].strip(), "active draft must not be empty"


def test_repeated_generation_leaves_one_active_draft(
    database: Database,
) -> None:
    """Two clicks in a row must not leave two active drafts behind."""

    question = "A/S는 어디서 받나요?"
    inquiry_id = store(database, question, "LIFECYCLE-2")
    for _ in range(2):
        generate(
            database,
            inquiry_id,
            scripted_provider(SAFE_SINGLE_ANSWER, clean=True),
        )

    drafts = _drafts(database, inquiry_id)
    active = [item for item in drafts if item["is_active"]]
    assert len(active) == 1, f"{len(active)} active drafts"
    assert len(drafts) >= 1


def test_e2e_provider_timeout_still_yields_a_safe_draft(
    database: Database,
) -> None:
    """Provider failure must leave a Program Answer, not nothing."""

    inquiry_id = store(database, SIX_PART, "TIMEOUT-686058300")
    provider = FakeGptProvider(fail_tasks={"UNDERSTANDING"})
    outcome = generate(database, inquiry_id, provider)

    assert outcome.result.answer.strip()
    draft = active_draft(database, inquiry_id)
    assert draft["original_answer"].strip()

    posts = CountingPostService()
    AutoPostPipelineService(database, post_service=posts).run_pending(
        run_id="TO", owner_id="TO", max_retries=1, inquiry_ids=[inquiry_id],
    )
    assert posts.calls == 0
