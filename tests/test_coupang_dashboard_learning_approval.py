"""What the Coupang Dashboard tells an operator, against what it did.

Three things disagreed with the pipeline they were describing, and only one
of them was ever a wrong write.  None of them was.

**The approval.**  Approving a Coupang seller reply as Human Verified
Positive Learning reported "네이버 실제 등록 답변을 Positive Learning으로
승인했습니다. 네이버 재등록은 수행하지 않았습니다." -- a market the inquiry
was not from, and a re-registration that cannot happen on a market with no
post path.  The save itself was already right, which is why it is pinned
first here: the row is written with ``origin_market`` COUPANG,
``COUPANG_ONLY`` applicability and no store, and is not applicable to Naver.
The sentence was the only thing that was wrong.

**The read-only notice.**  It listed 승인 among the things a 조회 전용
inquiry does not run, while the Learning 승인 button sat further down the
same screen.  Read-only means no answer may be written, generated or
registered; it never meant the Learning decision was unavailable.

**The trace.**  ``Auto Post: SUCCESS`` was read off ``source_answered``,
which on Coupang means a person replied in Wing.  On the 26.10.12 server
snapshot that reported a successful auto-post for 11 of the 16 Coupang
drafts, every one of them ``post_status`` NOT_POSTED and ``posted`` 0.

And one thing that is working as intended rather than broken: a Coupang
order/delivery question gets no draft at all, so its order step is SKIPPED
by decision and not by omission.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from answer.exceptions import AutoAnswerProhibitedError
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import (
    LearningRepository, is_market_applicable, origin_market_of,
)
from services.approval_service import ApprovalService
from services.coupang_seller_answer_learning_service import (
    CoupangSellerAnswerLearningService, marketplace_seller_answer,
)
from ui.answer_status_presenter import build_decision_trace
from ui.review_workspace import (
    COUPANG_SELLER_VIEW, NAVER_POSTED_VIEW, _posted_answer_approved_notice,
    _read_only_notice,
)

COUPANG_STORE = "COUPANG_OJE_NS"
NAVER_STORE = "OJE_PLUS"
SELLER_REPLY = "안녕하세요 고객님. 해당 제품 소비전력은 1800W입니다. 감사합니다."


def _raw(*contents: str) -> dict:
    """A Coupang online-inquiry payload with the comments it really carries.

    The comment shape is the one every stored comment has: all 7,688 of them
    across 7,439 Coupang inquiries on the 26.10.12 snapshot carry exactly
    ``content``, ``inquiryCommentAt``, ``inquiryCommentId`` and ``inquiryId``
    -- and no author or role field, in ``raw_json`` or in the untouched
    ``source_payload`` beside it.
    """

    return {
        "inquiryId": "160959847",
        "content": "이 제품 소비전력이 어떻게 되나요?",
        "sellerProductId": "15252727782",
        "vendorItemId": "91454239559",
        "inquiryAt": "2026-10-10T10:00:00+09:00",
        "orderIds": [],
        "commentDtoList": [
            {
                "inquiryId": "160959847",
                "inquiryCommentId": str(137117766 + index),
                "inquiryCommentAt": f"2026-10-10T11:0{index}:00+09:00",
                "content": content,
            }
            for index, content in enumerate(contents)
        ],
    }


@pytest.fixture
def database(tmp_path) -> Database:
    built = Database(tmp_path / "coupang-dashboard.db")
    built.initialize()
    return built


def _coupang_inquiry(database: Database, raw: dict) -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": COUPANG_STORE,
        "source_type": "COUPANG_ONLINE_INQUIRY",
        "source_question_id": raw["inquiryId"],
        "external_inquiry_id": raw["inquiryId"],
        "title": "Coupang 상품문의",
        "content": raw["content"],
        "registered_at": "2026-10-10T10:00:00+09:00",
        "source_answered": 1,
        "raw_json": raw,
        "source_metadata_json": {"account_code": "OJE_NS"},
    }).inquiry_id


# --- 1-2. the save was never the defect ------------------------------------

def test_an_approved_coupang_reply_is_stored_as_coupang_learning(
    database,
) -> None:
    """Checked before the wording, because a wrong row is the worse fault."""

    inquiry_id = _coupang_inquiry(database, _raw(SELLER_REPLY))

    saved = ApprovalService(database).approve_posted_answer(
        inquiry_id=inquiry_id, actor="관리자",
        positive_reason="CONTENT_ACCURATE",
    )

    row = LearningRepository(database).get(int(saved["id"]))
    metadata = row.get("metadata_json")
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    assert origin_market_of(metadata) == "COUPANG"
    assert metadata["origin_market"] == "COUPANG"
    assert metadata["market_applicability"] == "COUPANG_ONLY"
    # Both seller accounts read one corpus, so the row carries the account in
    # provenance rather than a store column.
    assert row["store_code"] is None
    assert metadata["market_provenance"]["account_code"] == "OJE_NS"
    assert metadata["market_provenance"]["seller_answer_provenance"] == (
        "MARKETPLACE_SELLER_ANSWER")


def test_the_approved_row_is_not_naver_evidence(database) -> None:
    """Coupang -> Naver stays closed; this step does not change that."""

    inquiry_id = _coupang_inquiry(database, _raw(SELLER_REPLY))
    saved = ApprovalService(database).approve_posted_answer(
        inquiry_id=inquiry_id, actor="관리자")

    metadata = LearningRepository(database).get(
        int(saved["id"]))["metadata_json"]
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    assert is_market_applicable(metadata, "COUPANG") is True
    assert is_market_applicable(metadata, "NAVER") is False


# --- 3-4. the notice names the market the inquiry came from ----------------

def test_approving_a_coupang_reply_never_reports_naver() -> None:
    notice = _posted_answer_approved_notice({
        "store_code": COUPANG_STORE, "source_answered": 1,
        "seller_answer": SELLER_REPLY,
    })

    assert notice == (
        f"{COUPANG_SELLER_VIEW}을 Positive Learning으로 승인했습니다. "
        "쿠팡에 답변을 등록하지 않았습니다."
    )
    assert "네이버" not in notice
    # "재등록" would describe a step this market does not have.
    assert "재등록" not in notice
    assert COUPANG_STORE not in notice


def test_the_naver_notice_is_unchanged() -> None:
    assert _posted_answer_approved_notice({
        "store_code": NAVER_STORE, "source_answered": 1,
    }) == (
        f"{NAVER_POSTED_VIEW}을 Positive Learning으로 승인했습니다. "
        "네이버 재등록은 수행하지 않았습니다."
    )


# --- 5. 조회 전용 and the Learning 승인 are different questions ------------

def test_the_read_only_notice_does_not_deny_the_learning_approval() -> None:
    """The button is right below it, so the notice may not contradict it."""

    answered = {
        "store_code": COUPANG_STORE, "source_answered": 1,
        "seller_answer": SELLER_REPLY,
    }
    notice = _read_only_notice(answered)

    assert "조회 전용" in notice
    assert "답변 생성·수정·쿠팡 등록은 실행하지 않고" in notice
    assert "승인·등록은 실행하지 않고" not in notice
    assert "Human Verified Positive Learning으로 사용할지에 대한 승인" in notice


def test_an_inquiry_with_no_seller_reply_still_denies_approval() -> None:
    """Nothing to approve, so the original wording is the accurate one."""

    notice = _read_only_notice({
        "store_code": COUPANG_STORE, "source_answered": 0,
    })
    assert "승인·등록은 실행하지 않고" in notice
    assert "Positive Learning" not in notice


# --- 6. several comments are reviewed, never guessed between ---------------

def test_several_comments_are_not_chosen_between(database) -> None:
    """No author field exists, so neither order nor wording decides it.

    278 of the 281 multi-comment Coupang inquiries on the snapshot carry two
    comments and three carry three; none of them repeats the customer's own
    question, so there is nothing in the payload that marks one as the
    seller's.  The approval is refused rather than resolved by guesswork, and
    refusing writes nothing.
    """

    raw = _raw(SELLER_REPLY, "https://form.naver.com/response/YBZAca1E4aoa")
    inquiry_id = _coupang_inquiry(database, raw)
    inquiry = InquiryRepository(database).get(inquiry_id)

    assert marketplace_seller_answer(inquiry) == ""
    status = CoupangSellerAnswerLearningService(database).status(inquiry)
    assert status.available is False
    assert status.reason == "MULTI_COMMENT_REVIEW"
    assert status.message == (
        "댓글이 여러 개라 어느 것이 판매자 답변인지 확정할 수 없습니다.")

    with pytest.raises(ValueError, match="댓글이 여러 개라"):
        ApprovalService(database).approve_posted_answer(
            inquiry_id=inquiry_id, actor="관리자")
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 0


def test_one_comment_is_the_answer_and_is_approved(database) -> None:
    inquiry_id = _coupang_inquiry(database, _raw(SELLER_REPLY))
    inquiry = InquiryRepository(database).get(inquiry_id)

    assert marketplace_seller_answer(inquiry) == SELLER_REPLY
    status = CoupangSellerAnswerLearningService(database).status(inquiry)
    assert (status.available, status.captured) == (True, False)


# --- 7-9. Auto Post says what this system did, not what Wing holds --------

def _draft() -> dict:
    return {
        "id": 1,
        "metadata_json": {
            "hybrid": {
                "subquestion_evidence": [{"status": "CANDIDATE"}],
                "draft": {"used_product_facts": ["power_consumption"]},
                "retrieval": {"learning": {"candidate_count": 5}},
            },
            "semantic_routing": {"understanding": {"usable": True}},
        },
    }


def test_a_seller_reply_in_wing_is_not_a_successful_auto_post() -> None:
    trace = build_decision_trace(
        inquiry={
            "id": 1, "store_code": COUPANG_STORE,
            "post_status": "NOT_POSTED", "source_answered": 1,
        },
        draft=_draft(),
    )
    assert trace.auto_post == "NOT_SUPPORTED"


def test_a_coupang_inquiry_this_system_did_post_keeps_success() -> None:
    """``post_status`` is this system's own column and still decides alone.

    Three of the sixteen drafts on the snapshot really were posted, during
    the window before Coupang registration was closed; their trace is not
    rewritten.
    """

    trace = build_decision_trace(
        inquiry={
            "id": 1, "store_code": COUPANG_STORE,
            "post_status": "POSTED", "source_answered": 1,
        },
        draft=_draft(),
    )
    assert trace.auto_post == "SUCCESS"


@pytest.mark.parametrize("post_status,source_answered,expected", [
    pytest.param("POSTED", 0, "SUCCESS", id="posted"),
    pytest.param("NOT_POSTED", 1, "SUCCESS", id="answered-at-naver"),
    # The answered branch is checked before POST_FAILED, as it always was.
    pytest.param("POST_FAILED", 1, "SUCCESS", id="answered-beats-failed"),
    pytest.param("POST_FAILED", 0, "FAILED", id="failed"),
])
def test_naver_keeps_every_auto_post_verdict_it_had(
    post_status, source_answered, expected
) -> None:
    trace = build_decision_trace(
        inquiry={
            "id": 1, "store_code": NAVER_STORE,
            "post_status": post_status, "source_answered": source_answered,
        },
        draft=_draft(),
    )
    assert trace.auto_post == expected


def test_an_inquiry_naming_no_store_keeps_the_naver_default() -> None:
    """Legacy rows predate the store column; they are Naver's."""

    trace = build_decision_trace(
        inquiry={"id": 1, "post_status": "NOT_POSTED", "source_answered": 1},
        draft=_draft(),
    )
    assert trace.auto_post == "SUCCESS"


# --- 10. the order step is SKIPPED by decision ----------------------------

def test_a_coupang_order_question_gets_no_draft_at_all() -> None:
    """So SKIPPED is the decision, not a step that quietly did nothing.

    Order lookup and DPS are Naver order semantics.  The generation path
    refuses a Coupang inquiry that needs either, before a draft exists, which
    is why all 16 stored Coupang drafts carry ``requires_order_lookup``
    false.
    """

    from services.answer_service import AnswerService

    service = AnswerService.__new__(AnswerService)
    service.inquiries = SimpleNamespace(
        get=lambda _id: {"id": 1, "store_code": COUPANG_STORE})

    with pytest.raises(AutoAnswerProhibitedError) as raised:
        service.enrich_dps_for_inquiry(1)

    assert raised.value.policy_reason == "MARKET_ORDER_DPS_NOT_SUPPORTED"
