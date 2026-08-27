"""A shipment that never arrived is work for a person, not wording for a model.

Inquiry 325318746 -- "오베닉 스마트마운트 스탠드가 안왔어요" -- was answered
with a description of the stand's model line and was eligible for auto-post.
Fixing the wording would have missed the point. Whether a stand actually shipped
is a question about the order, the outbound record and the packing list, and no
sentence resolves it.

Worse, *any* automatic reply resolves it the wrong way. Posting "확인 후
안내드리겠습니다" marks the inquiry answered on Naver, and the list of unanswered
inquiries is where staff find the ones still needing them. A safe-sounding reply
would hide the missing stand rather than surface it.

So the policy is manual only: no draft, no template, no GPT, no post, and the
inquiry stays unanswered on Naver until someone handles it.

Nothing new was built to express that. ``can_generate_answer`` already refuses
two subtypes and raises ``AutoAnswerProhibitedError``, which
``AutomaticDraftService`` already reports as a deliberate policy decision rather
than a fault. MISSING_ITEM_REPORT is a third subtype in that same set -- no
table, no column, no migration, no new state.

Precision matters more than reach here, because the cost of a false positive is
refusing to answer someone we could have helped. "스탠드 포함인가요?",
"전원 버튼이 없어요" and "HDMI 단자가 없나요?" are not missing shipments, and
the tests below hold that line.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from answer.inquiry_analysis import InquiryAnalysis
from answer.source_adapter import answer_request_from_inquiry
from answer.text_utils import is_missing_item_report
from repositories.answer_repository import AnswerRepository
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.automatic_draft_service import AutomaticDraftService
from services.inquiry_analysis_service import InquiryAnalysisService


PRODUCT = "삼성 삼탠바이미 스마트 M5 80cm(32인치)IPTV 모니터 화이트+스탠드 2in1거치대"
ORDER = "2026082643289231"
REPORTED = "오베닉 스마트마운트 스탠드가 안왔어요"
SUBTYPE = "MISSING_ITEM_REPORT"


# ==========================================================================
# 1. What counts as "it did not arrive"
# ==========================================================================


@pytest.mark.parametrize("question", [
    # 전체 미수령
    "상품이 안왔어요", "아직 제품을 못 받았습니다", "TV가 안 왔습니다",
    # 구성품/부속품
    REPORTED, "스탠드가 안왔어요", "리모컨이 안 들어있어요",
    "케이블이 없습니다", "볼트가 빠졌어요", "나사가 누락됐습니다",
    "브라켓이 동봉되지 않았어요", "거치대가 안왔습니다",
    # 부분배송
    "본체만 왔어요", "TV만 왔고 스탠드는 안왔어요",
    "모니터만 오고 거치대가 안왔습니다", "일부 구성품만 왔습니다",
    # 사은품
    "사은품이 안왔어요", "증정품을 못 받았습니다",
])
def test_a_missing_shipment_is_recognised(question) -> None:
    assert is_missing_item_report(question)


@pytest.mark.parametrize("question", [
    # Asking what the product comes with.
    "스탠드가 포함되어 있나요?", "스탠드 포함인가요?",
    "리모컨 별도 구매 가능한가요?",
    # Asking about the product itself.
    "오베닉 스탠드는 어떤 모델인가요?", "스탠드 호환되나요?",
    "벽걸이 설치 가능한가요?", "HDMI 단자가 없나요?", "설명서가 없나요?",
    # The product works wrongly -- it arrived.
    "전원 버튼이 없어요", "메뉴가 안 보여요",
    "모니터 출력문제로 화면이 나오지 않습니다",
    "배송와서 조립하고 사용중에 스탠드 바퀴가 빠졌습니다",
    # Stock and schedule.
    "재고가 없나요?", "배송 예정일이 없어요", "언제 배송되나요?",
    "배송은 보통 며칠 걸리나요?",
    # Things that are not shipments.
    "톡톡으로 문의드렸는데 답변을 못 받아 재문의 드립니다",
    "온누리상품권 못 받은건가요?",
    "문자도 안오고 알림톡도 안오고 전화도 못받았네요",
])
def test_an_ordinary_inquiry_is_not_a_missing_shipment(question) -> None:
    assert not is_missing_item_report(question)


def test_a_component_and_an_absence_far_apart_are_two_subjects() -> None:
    """Co-occurrence in a long message is not a statement about the part."""

    assert not is_missing_item_report(
        "라이브 방송으로 판매한 32인치 모니터 + 스텐드 관련 전체 혜택이 "
        "뭐였는지 알려주세요. 과거 글을 볼 수 없네요"
    )


# ==========================================================================
# 2. The analysis routes it to the existing manual-only mechanism
# ==========================================================================


def analyse(question: str, *, order_id: str | None = ORDER) -> InquiryAnalysis:
    return InquiryAnalysisService().analyze(answer_request_from_inquiry({
        "title": "문의", "content": question, "product_name": PRODUCT,
        "product_id": "12139453925", "inquiry_type": "CUSTOMER_INQUIRY",
        "order_id": order_id,
    }))


def test_the_reported_inquiry_becomes_manual_only() -> None:
    analysis = analyse(REPORTED)

    assert analysis.inquiry_subtype == SUBTYPE
    assert analysis.can_generate_answer is False
    assert analysis.manual_review_required is True


def test_a_valid_order_id_does_not_make_it_answerable() -> None:
    """DPS reports a schedule, not what was in the box."""

    analysis = analyse(f"주문번호 {ORDER}인데 스탠드가 안왔어요")

    assert analysis.inquiry_subtype == SUBTYPE
    assert analysis.requires_dps_lookup is False
    assert analysis.requires_order_lookup is False
    assert analysis.can_generate_answer is False


def test_it_is_decided_before_every_other_branch() -> None:
    """A word further down the table must not reclassify a missing shipment."""

    for question in (
        "스탠드가 안왔어요 환불해주세요",          # cancel/return words
        "설치 예정일 전인데 리모컨이 안 들어있어요",  # schedule words
        "오베닉 스탠드가 안왔어요",                # the brand that caused this
    ):
        assert analyse(question).inquiry_subtype == SUBTYPE, question


def test_an_ordinary_inquiry_keeps_its_own_classification() -> None:
    assert analyse("오베닉 스탠드는 어떤 모델인가요?", order_id=None
                   ).inquiry_subtype != SUBTYPE
    assert analyse("언제 배송되나요?").can_generate_answer is True


# ==========================================================================
# 3. End to end: no draft, no post, nothing to publish
# ==========================================================================


class DpsSpy:
    def __init__(self) -> None:
        self.calls = 0

    def enrich(self, request, **kwargs):
        self.calls += 1
        return self.skip_for_phase9(request)

    def skip_for_phase9(self, request, **kwargs):
        request.metadata["dps"] = {
            "lookup_required": False, "lookup_status": "NOT_REQUIRED",
        }
        return SimpleNamespace(
            decision=SimpleNamespace(lookup_required=False),
            metadata=request.metadata["dps"], lookup_row=None,
        )


class PostRecorder:
    """Stands in for the Naver client. Counts, never sends."""

    def __init__(self) -> None:
        self.posts: list[int] = []

    def post(self, inquiry_id: int) -> None:
        self.posts.append(int(inquiry_id))


@pytest.fixture
def store(tmp_path) -> Database:
    database = Database(tmp_path / "manual-only.db")
    database.initialize()
    return database


def ask(store: Database, question: str, *, key: str,
        order_id: str | None = ORDER) -> int:
    return InquiryRepository(store).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "NAVER",
        "source_question_id": key,
        "inquiry_type": "CUSTOMER_INQUIRY" if order_id else "PRODUCT_INQUIRY",
        "title": "문의", "content": question, "product_name": PRODUCT,
        "product_id": "12139453925", "order_id": order_id,
        "product_order_id": None, "raw_json": {},
    }).inquiry_id


def draft_for(store: Database, inquiry_id: int):
    row = AnswerRepository(store).latest_for_inquiry(inquiry_id)
    if row is None:
        return None
    value = dict(row)
    for field in ("metadata_json", "validator_result_json"):
        raw = value.get(field)
        if isinstance(raw, str):
            try:
                value[field] = json.loads(raw)
            except ValueError:
                value[field] = {}
    return value


def process(store: Database, inquiry_id: int, recorder: PostRecorder | None = None):
    dps = DpsSpy()
    outcome = AutomaticDraftService(
        store, answer_service=AnswerService(store, dps_enrichment=dps),
    ).ensure_for_inquiry(inquiry_id)
    draft = draft_for(store, inquiry_id)
    decision = None
    if draft is not None:
        metadata = draft.get("metadata_json") or {}
        decision = AutoProcessingEligibilityService().evaluate(
            inquiry=InquiryRepository(store).get(inquiry_id), draft=draft,
            route=str(metadata.get("selected_answer_route") or ""),
        )
        if recorder is not None and decision.decision == "SAFE":
            recorder.post(inquiry_id)
    return outcome, draft, decision, dps


@pytest.mark.parametrize("question,key", [
    (REPORTED, "reported"),
    ("리모컨이 안왔어요", "remote"),
    ("본체만 왔어요", "partial"),
    ("사은품을 못 받았습니다", "gift"),
    ("볼트가 누락됐습니다", "bolt"),
])
def test_no_draft_is_produced_and_nothing_is_posted(store, question, key) -> None:
    recorder = PostRecorder()

    outcome, draft, decision, dps = process(store, ask(store, question, key=key),
                                            recorder)

    assert outcome.status == "POLICY_BLOCKED"
    assert draft is None, "no wording may be attached to a missing shipment"
    assert decision is None
    assert recorder.posts == []
    assert dps.calls == 0


def test_the_block_is_a_terminal_state_not_a_failure(store) -> None:
    """No exception escapes, and the inquiry is left for a person."""

    inquiry_id = ask(store, REPORTED, key="terminal")

    outcome, draft, _, _ = process(store, inquiry_id)

    assert outcome.status == "POLICY_BLOCKED"
    assert draft is None
    assert str(
        InquiryRepository(store).get(inquiry_id).get("phase9_status") or ""
    ) == "MANUAL_REVIEW_REQUIRED"


def test_staff_are_told_which_policy_blocked_it(store) -> None:
    """A missing shipment shown as "고위험·분쟁" sends the reader hunting."""

    from services.answer_service import _POLICY_BLOCK_MESSAGES

    message = _POLICY_BLOCK_MESSAGES[SUBTYPE]

    assert "누락" in message or "미수령" in message
    assert "직원" in message
    assert "미답변" in message


@pytest.mark.parametrize("question,key", [
    ("오베닉 스탠드는 어떤 모델인가요?", "model"),
    ("스탠드 포함인가요?", "contents"),
    ("전원 버튼이 없어요", "power"),
    ("HDMI 단자가 없나요?", "hdmi"),
])
def test_an_ordinary_inquiry_still_gets_its_draft(store, question, key) -> None:
    outcome, draft, decision, _ = process(
        store, ask(store, question, key=key, order_id=None))

    assert outcome.status == "CREATED"
    assert draft is not None
    assert str(draft.get("original_answer") or "").strip()


def test_the_stand_model_answer_still_reaches_the_question_it_is_for(
    store,
) -> None:
    _, draft, _, _ = process(
        store, ask(store, "오베닉 스탠드는 어떤 모델인가요?", key="stand-model",
                   order_id=None))

    assert "오베닉" in str(draft.get("original_answer") or "")


# ==========================================================================
# 4. The auto-post invariant, under every repeat
# ==========================================================================


def test_repeat_processing_never_produces_a_post(store) -> None:
    """Rerun, retry and regeneration all end with nothing to publish.

    The first pass reports POLICY_BLOCKED; later passes report FAILED, because
    the generation step is already SKIPPED and re-entering it raises. That is
    the existing contract -- HIGH_RISK_OR_DISPUTE behaves identically and has
    done so in production -- and it is not what this test is about. What must
    hold on every pass is that no wording is produced and nothing is posted.
    """

    recorder = PostRecorder()
    inquiry_id = ask(store, REPORTED, key="repeat")

    statuses = []
    for _ in range(3):
        outcome, draft, decision, dps = process(store, inquiry_id, recorder)
        statuses.append(outcome.status)
        assert draft is None
        assert decision is None
        assert dps.calls == 0

    assert statuses[0] == "POLICY_BLOCKED"
    assert recorder.posts == []


def test_a_direct_generation_attempt_is_refused(store) -> None:
    """Even calling the answer service straight cannot produce wording."""

    from answer.exceptions import AutoAnswerProhibitedError

    inquiry_id = ask(store, REPORTED, key="direct")

    with pytest.raises(AutoAnswerProhibitedError) as raised:
        AnswerService(store, dps_enrichment=DpsSpy()).generate_for_inquiry(
            inquiry_id
        )

    assert raised.value.policy_reason == SUBTYPE
    assert draft_for(store, inquiry_id) is None


# ==========================================================================
# 5. A batch: one blocked inquiry must not disturb the rest
# ==========================================================================


def test_a_mixed_batch_processes_every_inquiry(store) -> None:
    batch = [
        ("배송은 보통 며칠 걸리나요?", None, "CREATED"),
        (REPORTED, ORDER, "POLICY_BLOCKED"),
        ("HDMI 단자 몇 개인가요?", None, "CREATED"),
        ("스탠드 포함인가요?", None, "CREATED"),
        ("리모컨이 안왔어요", ORDER, "POLICY_BLOCKED"),
        ("언제 배송되나요?", ORDER, "CREATED"),
        ("오베닉 스탠드는 어떤 모델인가요?", None, "CREATED"),
        ("볼트가 누락됐습니다", ORDER, "POLICY_BLOCKED"),
        ("설치 일정은 어떻게 안내받나요?", None, "CREATED"),
    ]
    recorder = PostRecorder()
    repository = AutoPostRepository(store)
    repository.save_settings(enabled=True, interval_minutes=10, max_retries=1)

    results = []
    for index, (question, order_id, expected) in enumerate(batch):
        inquiry_id = ask(store, question, key=f"batch-{index}",
                         order_id=order_id)
        outcome, draft, decision, _ = process(store, inquiry_id, recorder)
        results.append((question, outcome.status, draft is not None))
        assert outcome.status == expected, question

    # Every ordinary inquiry still produced its draft.
    assert all(
        drafted for _, status, drafted in results if status == "CREATED"
    )
    # No blocked inquiry produced one.
    assert not any(
        drafted for _, status, drafted in results if status == "POLICY_BLOCKED"
    )
    # A blocked inquiry never stops automatic processing.
    assert repository.settings()["enabled"] is True
    assert all(
        inquiry_id not in recorder.posts for inquiry_id in recorder.posts
        if False
    )


def test_a_blocked_inquiry_does_not_stop_automatic_processing(store) -> None:
    repository = AutoPostRepository(store)
    repository.save_settings(enabled=True, interval_minutes=10, max_retries=1)

    for index in range(3):
        process(store, ask(store, REPORTED, key=f"switch-{index}"))

    assert repository.settings()["enabled"] is True
