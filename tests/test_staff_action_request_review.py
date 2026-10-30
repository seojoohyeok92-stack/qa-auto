"""직원이 실제로 해야 하는 일을 요청한 문의는 자동등록하지 않는다.

문의 11407 (쿠팡 OJE_NS, 2026-10-29) -- "답변 잘 받았습니다.앞의 내용 담당
기사님께 전달 부탁드릴게요 / 제가 지금이후 전화통화 불가능합니다" -- 에
"앞서 말씀해 주신 내용과 현재 전화 통화가 불가능하신 점을 담당 기사님께 전달
요청하겠습니다." 가 자동등록됐다. 아무것도 전달되지 않았고, 마켓에는 답변
완료로 표시돼 미답변 위주로 일하는 직원의 목록에서 빠졌다.

GPT ① 은 요청을 정확히 읽었다 (action OTHER, requested_attribute
ACTION_EXECUTION). 막지 못한 것은 최종 판정이었다: ``_promises_an_action`` 은
답변도 조치를 약속해야 발동하는데 그 동사 목록에 "전달" 이 없었고, 고객 요청
자체를 보는 관문은 없었다. 기록된 판정은 SAFE / AUTO_POST_ALLOWED, soft
INTENT_CONFIDENCE_LOW 뿐이었다.

두 안전장치를 따로 둔다.

* 요청: GPT ① 이 ACTION_EXECUTION 으로 읽은 문항이 하나라도 있으면 보류
  (STAFF_ACTION_REQUEST_REVIEW). 답변 문구와 무관하다.
* 답변: 제3자에게 전달하겠다는 약속은 GPT ① 의 판단과 무관하게 보류
  (ACTION_COMMITMENT_REVIEW).

외부 호출은 없다. 실제 사례는 서버 사본에서 판정 입력만 잘라 둔
``tests/fixtures/staff_action_request_11407.json`` 이고, 마켓 등록은 기록용
더블, 카카오 대기열은 ``tmp_path`` 파일이다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services import auto_processing_eligibility_service as policy
from services.auto_processing_eligibility_service import (
    ACTION_COMMITMENT_REVIEW,
    SOFT_REASONS,
    STAFF_ACTION_REQUEST_REVIEW,
    AutoProcessingEligibilityService,
    _promises_a_handoff,
    _promises_an_action,
    _requests_staff_action,
)

FIXTURE = Path(__file__).parent / "fixtures" / "staff_action_request_11407.json"


def _real_case() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _evaluate_real_case(case: dict[str, Any] | None = None):
    case = case or _real_case()
    inquiry = dict(case["inquiry"], source_answered=0, post_status="NOT_POSTED")
    draft = dict(case["draft"], posted=0,
                 original_answer=case["draft"]["final_answer"])
    route = draft["metadata_json"]["selected_answer_route"]
    return AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=draft, route=route,
    )


def _questions(case: dict[str, Any]) -> list[dict[str, Any]]:
    return case["draft"]["metadata_json"]["semantic_routing"]["understanding"][
        "questions"
    ]


# ===========================================================================
# 1. 실제 사고 사례
# ===========================================================================

def test_the_fixture_is_the_recorded_inquiry() -> None:
    case = _real_case()
    assert case["inquiry"]["store_code"] == "COUPANG_OJE_NS"
    assert "담당 기사님께 전달 부탁" in case["inquiry"]["content"]
    assert _questions(case) == [{
        "action": "OTHER",
        "requested_attribute": "ACTION_EXECUTION",
        "requested_information": "앞서 전달된 내용을 담당 기사님에게 전달하는 조치",
        "text": "앞의 내용을 담당 기사님께 전달해 주세요.",
    }]
    assert "전달 요청하겠습니다" in case["draft"]["final_answer"]


def test_without_the_two_checks_the_real_draft_is_safe_as_recorded(
    monkeypatch,
) -> None:
    """수정 전 경로의 재현: 기록된 AUTO_POST_DECISION 과 같은 판정이 나온다."""

    monkeypatch.setattr(policy, "_requests_staff_action", lambda _q: False)
    monkeypatch.setattr(policy, "_promises_a_handoff", lambda _a: False)

    verdict = _evaluate_real_case()

    recorded = _real_case()["recorded_auto_post_decision"]
    assert verdict.decision == recorded["eligibility"] == "SAFE"
    assert list(verdict.reasons) == recorded["blocking_reason_codes"] == []
    assert list(verdict.soft_reasons) == recorded["soft_reason_codes"]


def test_the_existing_commitment_check_missed_it() -> None:
    """요청은 ACTION_EXECUTION 이었지만 "전달" 은 약속 동사가 아니었다."""

    case = _real_case()
    assert _promises_an_action(
        _questions(case), answer=case["draft"]["final_answer"],
    ) is False


def test_the_real_draft_is_now_held_for_staff() -> None:
    verdict = _evaluate_real_case()

    assert verdict.decision == "REVIEW_REQUIRED"
    assert verdict.safe is False
    assert STAFF_ACTION_REQUEST_REVIEW in verdict.reasons
    assert ACTION_COMMITMENT_REVIEW in verdict.reasons
    # 신뢰도는 여전히 참고값이다. 보류 사유가 그것을 대신하지 않는다.
    assert verdict.soft_reasons == ("INTENT_CONFIDENCE_LOW",)


def test_the_request_alone_holds_the_real_case() -> None:
    """답변을 중립 문구로 바꿔도 고객 요청만으로 보류된다."""

    case = _real_case()
    case["draft"]["final_answer"] = (
        "말씀해 주신 내용 잘 확인했습니다. 감사합니다."
    )

    verdict = _evaluate_real_case(case)

    assert verdict.decision == "REVIEW_REQUIRED"
    assert STAFF_ACTION_REQUEST_REVIEW in verdict.reasons
    assert ACTION_COMMITMENT_REVIEW not in verdict.reasons


def test_the_answer_alone_holds_the_real_case() -> None:
    """GPT ① 이 요청을 놓쳤어도 답변의 전달 약속만으로 보류된다."""

    case = _real_case()
    for question in _questions(case):
        question["requested_attribute"] = "METHOD_OR_PROCEDURE"

    verdict = _evaluate_real_case(case)

    assert verdict.decision == "REVIEW_REQUIRED"
    assert STAFF_ACTION_REQUEST_REVIEW not in verdict.reasons
    assert ACTION_COMMITMENT_REVIEW in verdict.reasons


def test_both_holds_are_hard() -> None:
    assert STAFF_ACTION_REQUEST_REVIEW not in SOFT_REASONS
    assert ACTION_COMMITMENT_REVIEW not in SOFT_REASONS
    assert STAFF_ACTION_REQUEST_REVIEW == "STAFF_ACTION_REQUEST_REVIEW"


# ===========================================================================
# 2. 판정 단위: 직원 조치 요청 / 일반 안내
# ===========================================================================

NEUTRAL_ANSWER = "문의 주신 내용 잘 확인했습니다."


def _verdict(
    questions: list[tuple[str, str, str]],
    *,
    answer: str = NEUTRAL_ANSWER,
    store_code: str = "COUPANG_OJE_NS",
    route: str = "GPT_FALLBACK",
):
    """근거·Validator 가 모두 통과한 draft. 남는 변수는 문항 이해뿐이다."""

    metadata = {
        "selected_answer_route": route,
        "semantic_routing": {
            "understanding": {
                "usable": True,
                "questions": [
                    {"text": text, "action": action,
                     "requested_attribute": attribute}
                    for action, attribute, text in questions
                ],
            },
        },
        "hybrid": {
            "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
            "subquestion_evidence": [
                {"subquestion": text, "status": "CANDIDATE",
                 "source": "ACTIVE_POSITIVE_LEARNING",
                 "learning_ids": [500 + index], "historical_case_ids": []}
                for index, (_a, _b, text) in enumerate(questions, start=1)
            ],
            "draft": {
                "learning_usage": [], "requires_review": False,
                "missing_information": [], "unresolved": [],
                "can_auto_post": True, "used_learning_ids": [501],
            },
            "self_review": {"requires_review": False},
        },
    }
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED",
                 "store_code": store_code},
        draft={"metadata_json": metadata, "original_answer": answer,
               "validation_status": "PASS"},
        route=route,
    )


STAFF_ACTION_REQUESTS = [
    ("OTHER", "설치 기사님께 문자로 연락해 달라고 전달해 주세요."),
    ("OTHER", "제가 전화를 받을 수 없으니 기사님께 문자로 연락 달라고 해주세요."),
    ("OTHER", "담당자분이 저에게 전화 좀 해 주세요."),
    ("SCHEDULE_CHANGE", "설치일을 10월 10일로 변경해 주세요."),
    ("SCHEDULE_CHANGE", "배송지를 803호에서 802호로 수정해 주세요."),
    ("OTHER", "수령인을 어머니 이름으로 바꿔 주세요."),
    ("CANCEL_RETURN", "주문을 취소해 주세요."),
    ("CANCEL_RETURN", "반품 처리해 주세요."),
    ("DAMAGE_REPORT", "작동 불량 리모컨을 교환해 주세요."),
    ("CANCEL_RETURN", "환불해 주세요."),
    ("OTHER", "재고 확인해 보시고 회신 부탁드립니다."),
]


@pytest.mark.parametrize("action, text", STAFF_ACTION_REQUESTS)
def test_a_request_to_act_is_held_whatever_the_answer_says(action, text) -> None:
    verdict = _verdict([(action, "ACTION_EXECUTION", text)])

    assert verdict.decision == "REVIEW_REQUIRED", verdict
    assert STAFF_ACTION_REQUEST_REVIEW in verdict.reasons


def test_a_compound_inquiry_with_one_request_to_act_is_held() -> None:
    verdict = _verdict([
        ("INSTALLATION_SCHEDULE", "TIMING", "설치 전에 기사님이 전화 주시나요?"),
        ("OTHER", "ACTION_EXECUTION",
         "제가 전화를 못 받으니 기사님께 문자로 연락 달라고 전달해 주세요."),
    ], answer="설치 전 기사님께서 방문 일정을 안내해 드립니다.")

    assert verdict.decision == "REVIEW_REQUIRED"
    assert STAFF_ACTION_REQUEST_REVIEW in verdict.reasons


GENERAL_GUIDANCE = [
    ("INSTALLATION_METHOD", "METHOD_OR_PROCEDURE",
     "설치는 어떻게 진행되나요?",
     "기사님이 방문하여 설치해 드립니다."),
    ("INSTALLATION_SCHEDULE", "TIMING",
     "설치기사님은 보통 언제 연락하시나요?",
     "설치 기사님께서 방문 전날 연락드립니다."),
    ("INSTALLATION_SCHEDULE", "EXISTENCE_OR_CAPABILITY",
     "설치 전날 연락이 오나요?",
     "네, 설치 전 기사님께서 연락드리겠습니다."),
    ("DELIVERY_POLICY", "TIMING",
     "배송은 보통 얼마나 걸리나요?",
     "주문 후 보통 3~5일 정도 소요됩니다."),
    ("SCHEDULE_CHANGE", "EXISTENCE_OR_CAPABILITY",
     "설치일 변경은 가능한가요?",
     "설치 기사님 해피콜 때 일정 변경을 요청해 주시면 됩니다."),
    ("CANCEL_RETURN", "METHOD_OR_PROCEDURE",
     "반품 신청은 어떻게 하나요?",
     "마이페이지 주문내역에서 반품 신청을 하실 수 있습니다."),
    ("SCHEDULE_CHANGE", "METHOD_OR_PROCEDURE",
     "배송지 변경 방법을 알려주세요.",
     "발송 전이라면 주문 상세에서 배송지를 변경하실 수 있습니다."),
    ("INSTALLATION_SCHEDULE", "LOCATION_OR_CONTACT",
     "기사님 연락처를 어디서 확인하나요?",
     "설치 전 기사님께서 직접 연락드리며, 문자로도 안내됩니다."),
    ("PRODUCT_SPEC", "SPEC_VALUE",
     "화면 크기가 몇 인치인가요?",
     "문의하신 모델은 43인치입니다."),
]


@pytest.mark.parametrize("action, attribute, text, answer", GENERAL_GUIDANCE)
def test_general_guidance_is_not_mistaken_for_a_request(
    action, attribute, text, answer,
) -> None:
    """정보·방법 문의는 기존 판정 그대로다. 반품·교환은 원래의 정책 보류만 받는다."""

    verdict = _verdict([(action, attribute, text)], answer=answer)

    assert STAFF_ACTION_REQUEST_REVIEW not in verdict.reasons
    assert ACTION_COMMITMENT_REVIEW not in verdict.reasons
    if action == "CANCEL_RETURN":
        assert verdict.reasons == ("RETURN_OR_DAMAGE_POLICY_REVIEW",)
    else:
        assert verdict.decision == "SAFE", verdict.reasons


def test_the_two_example_inquiries_in_the_policy_are_told_apart() -> None:
    general = _verdict([
        ("INSTALLATION_SCHEDULE", "EXISTENCE_OR_CAPABILITY",
         "설치 전에 기사님이 전화 주시나요?"),
        ("INSTALLATION_SCHEDULE", "TIMING", "대략 언제 연락이 오나요?"),
    ], answer="설치 기사님께서 방문 전날 연락드립니다.")
    staff = _verdict([
        ("OTHER", "ACTION_EXECUTION",
         "기사님께 문자로 연락해 달라고 전달해 주세요."),
    ], answer="설치 기사님께서 방문 전날 연락드립니다.")

    assert general.decision == "SAFE", general.reasons
    assert staff.decision == "REVIEW_REQUIRED"


@pytest.mark.parametrize("route", ["PRODUCT_DB", "TEMPLATE", "GPT_FALLBACK"])
def test_product_knowledge_and_learning_routes_keep_passing(route) -> None:
    verdict = _verdict(
        [("PRODUCT_SPEC", "SPEC_VALUE", "스피커가 내장되어 있나요?")],
        answer="네, 스피커가 내장되어 있습니다.", route=route,
    )
    assert verdict.decision == "SAFE", verdict.reasons


@pytest.mark.parametrize("route", ["PRODUCT_DB", "TEMPLATE", "GPT_FALLBACK"])
def test_the_request_holds_on_every_route(route) -> None:
    verdict = _verdict(
        [("SCHEDULE_CHANGE", "ACTION_EXECUTION", "설치일을 바꿔 주세요.")],
        route=route,
    )
    assert STAFF_ACTION_REQUEST_REVIEW in verdict.reasons


@pytest.mark.parametrize("store_code", ["OJE_PLUS", "COUPANG_OJE_NS", "COUPANG_OJ"])
def test_both_markets_share_the_rule(store_code) -> None:
    held = _verdict(
        [("OTHER", "ACTION_EXECUTION", "기사님께 전달해 주세요.")],
        store_code=store_code,
    )
    passed = _verdict(
        [("DELIVERY_POLICY", "TIMING", "배송은 얼마나 걸리나요?")],
        answer="보통 3~5일 소요됩니다.", store_code=store_code,
    )
    assert held.decision == "REVIEW_REQUIRED"
    assert STAFF_ACTION_REQUEST_REVIEW in held.reasons
    assert passed.decision == "SAFE", passed.reasons


# ===========================================================================
# 3. 두 검사 함수 자체
# ===========================================================================

def test_the_request_check_reads_only_the_attribute() -> None:
    assert _requests_staff_action(
        [{"action": "OTHER", "requested_attribute": "ACTION_EXECUTION"}]
    ) is True
    assert _requests_staff_action(
        [{"action": "CANCEL_RETURN", "requested_attribute": "METHOD_OR_PROCEDURE"}]
    ) is False
    # 단어가 아니라 이해 결과를 읽는다.
    assert _requests_staff_action(
        [{"text": "기사님께 전달해 주세요", "action": "OTHER"}]
    ) is False
    assert _requests_staff_action([]) is False
    assert _requests_staff_action(["문자열", None]) is False


@pytest.mark.parametrize("answer", [
    "앞서 말씀해 주신 내용과 현재 전화 통화가 불가능하신 점을 담당 기사님께 "
    "전달 요청하겠습니다.",
    "설치 기사님께 전달해 드리겠습니다.",
    "담당자에게 말씀하신 내용 전달하겠습니다.",
    "배송 기사님께 요청 사항 전달드리겠습니다.",
    "택배사에 전달하겠습니다.",
])
def test_a_promise_to_relay_is_caught(answer) -> None:
    assert _promises_a_handoff(answer) is True


@pytest.mark.parametrize("answer", [
    "설치 기사님께서 방문 전날 연락드립니다.",
    "네, 설치 전 기사님께서 연락드리겠습니다.",
    "기사님께 전달된 일정은 주문 상세에서 확인하실 수 있습니다.",
    "고객님께 안내 전달드립니다.",
    "설치 기사님 해피콜 때 일정 변경을 요청해 주시면 됩니다.",
    "안내드린 내용이 문의하신 내용과 다른 경우,\n네이버 톡톡으로 문의 남겨주시면 "
    "담당자가 확인 후 안내드리겠습니다.",
    "",
])
def test_information_is_not_a_promise_to_relay(answer) -> None:
    assert _promises_a_handoff(answer) is False


# ===========================================================================
# 4. 자동등록 파이프라인: 네이버·쿠팡, 검토 대기, 카카오 보류 알림
# ===========================================================================

from tests.test_coupang_automatic_pipeline import (  # noqa: E402
    RecordingConfirmation,
    RecordingPoster,
    coupang_inquiry,
    coupang_posting_opened,  # noqa: F401 - fixture
    naver_inquiry,
    run,
)
from tests.test_kakao_post_outcome_notifications import (  # noqa: E402
    HOLD_TITLE,
    events,
    outbox,  # noqa: F401 - fixture
)


@pytest.fixture
def database(tmp_path):
    from repositories.database import Database

    value = Database(tmp_path / "staff_action.db")
    value.initialize()
    return value


def _draft_with_understanding(
    database, inquiry_id: int, *, questions: list[dict[str, str]], answer: str,
) -> dict[str, Any]:
    from answer.models import AnswerResult, AnswerStatus
    from repositories.answer_repository import AnswerRepository

    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="상품", reason="테스트",
            answer=answer, provider="rules", auto_answerable=True,
            needs_review=False,
            # A Product Knowledge route: GPT ① understanding is persisted and
            # GPT ② is not required, so the only open variable is the request.
            metadata={
                "selected_answer_route": "PRODUCT_DB",
                "generation_mode": "PRODUCT_DB",
                "requires_manual_review": False,
                "validator_result": {"status": "PASS", "passed": True},
                "semantic_routing": {"understanding": {
                    "usable": True, "questions": questions,
                }},
            },
        ),
    )


REAL_QUESTION = {
    "action": "OTHER", "requested_attribute": "ACTION_EXECUTION",
    "text": "앞의 내용을 담당 기사님께 전달해 주세요.",
}
REAL_ANSWER = (
    "앞서 말씀해 주신 내용과 현재 전화 통화가 불가능하신 점을 담당 기사님께 "
    "전달 요청하겠습니다."
)
INFO_QUESTION = {
    "action": "PRODUCT_SPEC", "requested_attribute": "EXISTENCE_OR_CAPABILITY",
    "text": "스피커 내장되어 있나요?",
}
INFO_ANSWER = "네, 스피커가 내장되어 있습니다."


def _review_logs(database, inquiry_id: int) -> list[dict[str, Any]]:
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT event_code, details_json FROM activity_logs "
            "WHERE inquiry_id=? AND event_code IN "
            "('AUTO_PROCESSING_REVIEW_REQUIRED','AUTO_POST_DECISION')",
            (inquiry_id,),
        ).fetchall()
    return [
        {"event_code": row["event_code"],
         "details": json.loads(row["details_json"] or "{}")}
        for row in rows
    ]


def test_a_coupang_request_to_relay_is_never_posted_and_waits_for_staff(
    coupang_posting_opened, database, outbox,  # noqa: F811
) -> None:
    from repositories.inquiry_repository import InquiryRepository
    from services.dashboard_operations_service import _reason_bucket

    inquiry_id = coupang_inquiry(database, inquiry_id="17200001")
    _draft_with_understanding(
        database, inquiry_id, questions=[REAL_QUESTION], answer=REAL_ANSWER,
    )
    poster = RecordingPoster()

    outcome = run(database, poster, RecordingConfirmation())

    # 최종 등록 호출이 일어나지 않는다.
    assert poster.calls == []
    assert outcome.succeeded_count == 0
    assert outcome.skipped_count == 1
    # 마켓에는 답변 완료로 표시되지 않고 검토 대기로 남는다.
    row = InquiryRepository(database).get(inquiry_id)
    assert str(row["post_status"]).upper() == "NOT_POSTED"
    assert not row["source_answered"]
    logged = _review_logs(database, inquiry_id)
    review = [item for item in logged
              if item["event_code"] == "AUTO_PROCESSING_REVIEW_REQUIRED"]
    assert review, logged
    assert review[0]["details"]["reasons"] == [STAFF_ACTION_REQUEST_REVIEW,
                                               ACTION_COMMITMENT_REVIEW]
    # 대시보드 검토 대기 사유
    assert _reason_bucket(STAFF_ACTION_REQUEST_REVIEW) == "직원 조치 요청"
    # 카카오 보류 알림: 사유는 가고, 미등록 초안은 가지 않는다.
    sent = events(outbox)
    assert len(sent) == 1, sent
    message = sent[0]["message"]
    assert sent[0]["title"] == HOLD_TITLE
    assert "직원 조치 요청" in message
    assert "답변: -" in message
    assert "전달 요청하겠습니다" not in message
    assert "STAFF_ACTION_REQUEST_REVIEW" not in message


def test_a_naver_request_to_act_is_held_the_same_way(database, outbox) -> None:  # noqa: F811
    inquiry_id = naver_inquiry(database, external_id="N-STAFF-1")
    _draft_with_understanding(
        database, inquiry_id,
        questions=[{"action": "SCHEDULE_CHANGE",
                    "requested_attribute": "ACTION_EXECUTION",
                    "text": "설치일을 10월 10일로 변경해 주세요."}],
        answer="설치일 변경 요청 확인했습니다.",
    )
    poster = RecordingPoster()

    outcome = run(database, poster, RecordingConfirmation())

    assert poster.calls == []
    assert outcome.skipped_count == 1
    review = [item for item in _review_logs(database, inquiry_id)
              if item["event_code"] == "AUTO_PROCESSING_REVIEW_REQUIRED"]
    assert review[0]["details"]["reasons"] == [STAFF_ACTION_REQUEST_REVIEW]
    sent = events(outbox)
    assert len(sent) == 1, sent
    assert "직원 조치 요청" in sent[0]["message"]
    assert "답변: -" in sent[0]["message"]


def test_general_inquiries_in_both_markets_still_post(
    coupang_posting_opened, database,  # noqa: F811
) -> None:
    coupang_id = coupang_inquiry(database, inquiry_id="17200002")
    naver_id = naver_inquiry(database, external_id="N-STAFF-2")
    for inquiry_id in (coupang_id, naver_id):
        _draft_with_understanding(
            database, inquiry_id, questions=[INFO_QUESTION], answer=INFO_ANSWER,
        )
    poster, confirmation = RecordingPoster(), RecordingConfirmation()

    outcome = run(database, poster, confirmation)

    assert outcome.succeeded_count == 2
    assert sorted(call["inquiry_id"] for call in poster.calls) == sorted(
        [coupang_id, naver_id]
    )


def test_the_hold_reason_reads_as_an_action_for_staff() -> None:
    from answer.hold_reasons import (
        REASON_LABELS,
        STAFF_HEADLINES,
        STAFF_REASON_LABELS,
    )

    for code in (STAFF_ACTION_REQUEST_REVIEW, ACTION_COMMITMENT_REVIEW):
        assert code in REASON_LABELS
        assert code in STAFF_REASON_LABELS
        assert code in STAFF_HEADLINES
    assert STAFF_REASON_LABELS[STAFF_ACTION_REQUEST_REVIEW] == "직원 조치 요청"
