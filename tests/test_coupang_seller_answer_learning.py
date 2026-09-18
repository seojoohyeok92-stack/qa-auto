"""Keeping a Coupang seller's own reply as Learning, when a person asks.

No answer is generated, edited or registered: the reply is already at the
marketplace and nothing is posted back.  The one decision offered is the Naver
one -- 승인 makes this reply Human Verified Positive Learning, 승인 취소
deactivates it -- entered through the same ApprovalService, so it carries no
quality threshold of its own and an inquiry the historical backfill already
promoted produces no second row.
"""

from __future__ import annotations

from typing import Any

import pytest

from repositories.coupang_product_mapping_repository import (
    CONFIRMED,
    MANUAL,
    CoupangProductMappingRepository,
)
from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services import market_policy
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.coupang_seller_answer_learning_service import (
    CoupangSellerAnswerLearningService,
)
from services.historical_case_service import HistoricalCaseService
from services.inquiry_sync_service import normalize_work_item

SPID = "15654321531"
VENDOR_ITEM = "93128932886"
SELLER_ANSWER = (
    "스탠드형 방문설치는 주문 시 설치 옵션을 선택하시면 기사님이 방문해 설치해 드립니다."
)


# --- fixtures ------------------------------------------------------------------

@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "seller_learning.db")
    value.initialize()
    return value


def _payload(
    *, inquiry_id: str, content: str, comments: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    return {
        "inquiryId": inquiry_id,
        "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM,
        "productId": "PRD-1",
        "content": content,
        "inquiryAt": "2026-09-17T10:00:00+09:00",
        "orderIds": [],
        "commentDtoList": comments or [],
    }


def coupang_inquiry(
    database: Database,
    *,
    account: str = "OJE_NS",
    inquiry_id: str = "170000001",
    content: str = "방문설치는 어떻게 신청하나요?",
    answer: str | None = SELLER_ANSWER,
    comments: list[dict[str, Any]] | None = None,
) -> int:
    if comments is None:
        comments = (
            [{
                "inquiryCommentId": "c1",
                "inquiryId": inquiry_id,
                "content": answer,
                "inquiryCommentAt": "2026-09-17T11:00:00+09:00",
            }]
            if answer
            else []
        )
    payload = _payload(inquiry_id=inquiry_id, content=content, comments=comments)
    ready = normalize_work_item(
        CoupangInquiryNormalizer().online(payload, account_code=account).to_work_item()
    )
    row_id = InquiryRepository(database).upsert_work_item(ready).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def naver_inquiry(database: Database) -> int:
    row_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "N-1", "inquiry_type": "상품",
        "title": "상품 문의", "content": "스피커 있나요?",
        "product_name": "삼성 스마트모니터 M5", "option_name": "32인치",
        "source_answered": True, "raw_json": {},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def map_option(database: Database, *, account: str = "OJE_NS",
               model: str = "LH43BEHHLGFXKR") -> None:
    CoupangProductMappingRepository(database).upsert(
        account_code=account, vendor_item_id=VENDOR_ITEM, seller_product_id=SPID,
        canonical_model=model, mapping_source=MANUAL, mapping_status=CONFIRMED,
    )


def service(database: Database) -> CoupangSellerAnswerLearningService:
    return CoupangSellerAnswerLearningService(database)


def approve(database: Database, inquiry_id: int, **kwargs) -> dict[str, Any]:
    """Approve exactly where Naver approves its marketplace answer."""

    return ApprovalService(database).approve_posted_answer(
        inquiry_id=inquiry_id, actor="관리자", **kwargs
    )


def cancel(database: Database, inquiry_id: int, reason: str = "오안내"):
    return ApprovalService(database).cancel_approval_with_learning(
        inquiry_id=inquiry_id, draft_id=None, reason=reason, actor="관리자",
    )


def inquiry_of(database: Database, inquiry_id: int) -> dict[str, Any]:
    row = InquiryRepository(database).get(inquiry_id)
    assert row is not None
    return row


def learning_count(database: Database) -> int:
    with database.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM learning_examples"
        ).fetchone()[0]


def draft_count(database: Database) -> int:
    with database.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM answer_drafts"
        ).fetchone()[0]


# --- A. the action is offered ---------------------------------------------------

def test_an_answered_coupang_inquiry_offers_the_capture(database) -> None:
    inquiry_id = coupang_inquiry(database)
    status = service(database).status(inquiry_of(database, inquiry_id))

    assert status.available is True
    assert status.captured is False
    assert status.learning_example_id is None


def test_an_unanswered_coupang_inquiry_offers_nothing(database) -> None:
    """H: the action belongs to an answer that exists, not to a pending one."""

    inquiry_id = coupang_inquiry(database, answer=None)
    status = service(database).status(inquiry_of(database, inquiry_id))

    assert status.available is False
    assert status.captured is False
    assert status.reason == "NOT_ANSWERED"


def test_an_ambiguous_comment_list_is_not_guessed_at(database) -> None:
    """Which of several comments is the seller's is not decided here."""

    inquiry_id = coupang_inquiry(database, comments=[
        {"inquiryCommentId": "c1", "content": "고객 추가 문의입니다.",
         "inquiryCommentAt": "2026-09-17T11:00:00+09:00"},
        {"inquiryCommentId": "c2", "content": SELLER_ANSWER,
         "inquiryCommentAt": "2026-09-17T12:00:00+09:00"},
    ])
    status = service(database).status(inquiry_of(database, inquiry_id))

    assert status.available is False
    assert status.reason == "MULTI_COMMENT_REVIEW"


def test_a_naver_inquiry_has_no_marketplace_seller_capture(database) -> None:
    """I: the Naver posted-answer flow is untouched by this action."""

    inquiry_id = naver_inquiry(database)
    status = service(database).status(inquiry_of(database, inquiry_id))

    assert status.available is False
    assert status.reason == "NOT_COUPANG"


# --- B. what one capture writes -------------------------------------------------

def test_capturing_writes_one_coupang_only_learning_row(database) -> None:
    inquiry_id = coupang_inquiry(database)
    before = learning_count(database)

    saved = approve(database, inquiry_id)

    assert learning_count(database) == before + 1
    metadata = saved["metadata_json"]
    assert metadata["origin_market"] == "COUPANG"
    assert metadata["market_applicability"] == "COUPANG_ONLY"
    assert metadata["shared_cross_market_learning"] is False
    # Account-independent on purpose: both seller accounts read one corpus.
    assert saved["store_code"] is None
    assert SELLER_ANSWER in str(saved["final_answer"])


def test_the_captured_row_keeps_the_marketplace_provenance(database) -> None:
    inquiry_id = coupang_inquiry(database)
    saved = approve(database, inquiry_id)

    metadata = saved["metadata_json"]
    provenance = metadata["market_provenance"]
    assert provenance["account_code"] == "OJE_NS"
    assert provenance["origin_store_code"] == "COUPANG_OJE_NS"
    assert provenance["external_inquiry_id"] == "170000001"
    assert provenance["source_question_id"] == "170000001"
    assert provenance["seller_product_id"] == SPID
    assert provenance["vendor_item_id"] == VENDOR_ITEM
    # The Coupang inquiry carries the seller product id as its product id.
    assert provenance["product_id"] == SPID
    assert provenance["source_created_at"]
    assert provenance["seller_answer_provenance"] == "MARKETPLACE_SELLER_ANSWER"
    assert provenance["source_origin_detail"] == (
        "COUPANG_SELLER_ANSWER_MANUAL_CAPTURE"
    )
    assert provenance["captured_from_inquiry_id"] == inquiry_id
    # Saved by the same manual-positive path Naver uses, and marked as one.
    assert metadata["human_verified"] is True
    assert metadata["verified_by"] == "관리자"
    assert metadata["facts_authority"] == "HUMAN_VERIFIED_MARKETPLACE_ANSWER"
    assert metadata["learning_signal_type"] == "POSITIVE"
    assert saved["learning_source"] == "SELLER_ANSWER"
    # A person read it, so it is a reference rather than a style sample.
    assert saved["style_only"] is False


# --- C/D. it never writes a second row ------------------------------------------

def test_pressing_the_button_twice_writes_one_row(database) -> None:
    inquiry_id = coupang_inquiry(database)
    first = approve(database, inquiry_id)
    after_first = learning_count(database)

    second = approve(database, inquiry_id)

    assert learning_count(database) == after_first
    assert int(second["id"]) == int(first["id"])
    status = service(database).status(inquiry_of(database, inquiry_id))
    assert status.captured is True
    assert status.learning_example_id == int(first["id"])


def test_an_answer_the_historical_backfill_already_promoted_is_not_duplicated(
    database,
) -> None:
    """D: the 742 are matched on their own key, so none of them is re-created.

    The case is built and promoted here exactly as the backfill built and
    promoted it, without this service.  The screen must then already read as
    captured, and pressing the button must return that row.
    """

    inquiry_id = coupang_inquiry(database)
    cases = HistoricalCaseService(database)
    # From the live API response, exactly as the backfill saw it.  The stored
    # payload is privacy-masked, so deriving the key from it instead is how a
    # second copy of an already-promoted answer gets written.
    normalized = CoupangInquiryNormalizer().online(
        _payload(
            inquiry_id="170000001",
            content="방문설치는 어떻게 신청하나요?",
            comments=[{
                "inquiryCommentId": "c1", "inquiryId": "170000001",
                "content": SELLER_ANSWER,
                "inquiryCommentAt": "2026-09-17T11:00:00+09:00",
            }],
        ),
        account_code="OJE_NS",
    )
    candidate = dict(normalized.to_work_item())
    candidate.update({
        "local_inquiry_id": inquiry_id,
        "seller_answer": normalized.seller_answer,
        "source_answered": True,
        "source_updated_at": normalized.answer_created_at,
        "raw_payload": normalized.raw_payload,
        "historical_metadata": {
            "candidate_only": True, "market": "COUPANG",
            "market_applicability": "COUPANG_ONLY", "origin_market": "COUPANG",
            "shared_cross_market_learning": False, "account_code": "OJE_NS",
        },
    })
    case = cases.prepare_case(
        candidate,
        source_reference=f"COUPANG_ONLINE_API:OJE_NS:{normalized.external_inquiry_id}",
    )
    stored, _ = HistoricalCaseRepository(database).upsert(case)
    HistoricalCaseRepository(database).set_learning_enabled(
        int(stored["id"]), True, actor="관리자"
    )
    promoted = cases.promote(int(stored["id"]), actor="관리자")
    baseline = learning_count(database)

    status = service(database).status(inquiry_of(database, inquiry_id))
    assert status.captured is True
    assert status.learning_example_id == int(promoted["id"])

    again = approve(database, inquiry_id)
    assert int(again["id"]) == int(promoted["id"])
    assert learning_count(database) == baseline


# --- E/F. who may read it -------------------------------------------------------

def _candidate_ids(database: Database, store_code: str) -> set[int]:
    return {
        int(row["id"])
        for row in LearningRepository(database).candidates(store_code=store_code)
    }


def test_both_coupang_accounts_read_one_corpus(database) -> None:
    """E: captured under OJE_NS, retrievable for OJE_PLUS."""

    inquiry_id = coupang_inquiry(database, account="OJE_NS")
    saved = approve(database, inquiry_id)

    assert int(saved["id"]) in _candidate_ids(database, "COUPANG_OJE_NS")
    assert int(saved["id"]) in _candidate_ids(database, "COUPANG_OJE_PLUS")


def test_naver_never_retrieves_a_captured_coupang_answer(database) -> None:
    """F: a Coupang marketplace reply is not evidence for a Naver customer."""

    inquiry_id = coupang_inquiry(database)
    saved = approve(database, inquiry_id)

    assert int(saved["id"]) not in _candidate_ids(database, "OJE_PLUS")
    from repositories.learning_repository import is_market_applicable

    assert is_market_applicable(saved["metadata_json"], "NAVER") is False
    assert is_market_applicable(saved["metadata_json"], "COUPANG") is True


def test_legacy_metadata_free_learning_stays_out_of_coupang(database) -> None:
    """I: the existing isolation in the other direction is unchanged."""

    from repositories.learning_repository import is_market_applicable

    assert is_market_applicable({}, "NAVER") is True
    assert is_market_applicable({}, "COUPANG") is False


# --- G. nothing else was opened -------------------------------------------------

def test_capturing_opens_no_answer_edit_approval_or_registration(database) -> None:
    inquiry_id = coupang_inquiry(database)
    before = inquiry_of(database, inquiry_id)

    approve(database, inquiry_id)

    row = inquiry_of(database, inquiry_id)
    # Nothing about the inquiry itself moves: the marketplace already answered
    # it, and keeping that answer is not this system answering it.
    assert draft_count(database) == 0
    for field in (
        "post_status", "answer_status", "workflow_status", "approval_status",
        "posted_draft_id", "posted_at", "source_answered",
    ):
        assert row[field] == before[field]
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM naver_post_attempts"
        ).fetchone()[0] == 0
    # The read-only market policy itself is untouched by this feature.
    assert market_policy.is_store_post_enabled("COUPANG_OJE_NS") is False
    assert market_policy.is_store_dps_enabled("COUPANG_OJE_NS") is False
    assert market_policy.is_kakao_market_enabled("COUPANG") is False
    assert market_policy.is_store_automatic_generation_enabled(
        "COUPANG_OJE_NS"
    ) is False


# --- model scope ----------------------------------------------------------------

def test_a_confirmed_mapping_scopes_the_row_and_keeps_its_raw_value(
    database,
) -> None:
    """The 43-inch equivalence applies; the operator's mapping is not rewritten."""

    map_option(database, model="LH43BEHHLGFXKR")
    inquiry_id = coupang_inquiry(database)

    saved = approve(database, inquiry_id)

    metadata = saved["metadata_json"]
    provenance = metadata["market_provenance"]
    assert provenance["confirmed_canonical_model"] == "LH43BEHHLGFXKR"
    assert provenance["model_identity_source"] == "COUPANG_CONFIRMED_MAPPING"
    assert provenance["canonical_model"] == "LH43BEDH"
    # The scope the retrieval side compares on, not only a provenance note.
    assert saved["model_code"] == "LH43BEDH"
    assert metadata["product_identity"]["model_code"] == "LH43BEDH"
    assert saved["model_code"] == "LH43BEDH"
    with database.connection() as connection:
        row = connection.execute(
            "SELECT canonical_model, mapping_status FROM coupang_product_mappings"
        ).fetchone()
    assert row["canonical_model"] == "LH43BEHHLGFXKR"
    assert row["mapping_status"] == CONFIRMED


def test_without_a_confirmed_mapping_no_model_is_invented(database) -> None:
    inquiry_id = coupang_inquiry(database)
    saved = approve(database, inquiry_id)

    # The shared builder masks an absent model to an empty string, exactly
    # as it does for a Naver row whose draft named none.
    assert not saved["model_code"]
    provenance = saved["metadata_json"].get("market_provenance") or {}
    assert "canonical_model" not in provenance
    assert "confirmed_canonical_model" not in provenance
    assert "model_identity_source" not in provenance


def test_the_key_matches_the_one_the_live_backfill_would_have_derived(
    database,
) -> None:
    """The dedupe key is derived from the same string on both paths.

    The historical backfill hashes the live API response; this service only
    ever has the stored row, whose payload is privacy-masked -- a nine-digit
    Coupang inquiryId inside it comes back ``<masked-...>``.  The external id
    goes into the key unmasked, so if this service read it from the payload
    the two paths would disagree and every already-promoted answer would be
    written a second time.
    """

    inquiry_id = coupang_inquiry(database)
    live = CoupangInquiryNormalizer().online(
        _payload(
            inquiry_id="170000001",
            content="방문설치는 어떻게 신청하나요?",
            comments=[{
                "inquiryCommentId": "c1", "inquiryId": "170000001",
                "content": SELLER_ANSWER,
                "inquiryCommentAt": "2026-09-17T11:00:00+09:00",
            }],
        ),
        account_code="OJE_NS",
    )
    live_candidate = dict(live.to_work_item())
    live_candidate.update({
        "local_inquiry_id": inquiry_id,
        "seller_answer": live.seller_answer,
        "source_answered": True,
        "raw_payload": live.raw_payload,
        "historical_metadata": {"candidate_only": True, "market": "COUPANG"},
    })
    live_case = HistoricalCaseService(database).prepare_case(
        live_candidate, source_reference="COUPANG_ONLINE_API:OJE_NS:170000001",
    )

    stored_case = service(database)._historical_case(inquiry_of(database, inquiry_id))
    assert stored_case is not None

    assert stored_case["case_key"] == live_case["case_key"]
    assert stored_case["fingerprint"] == live_case["fingerprint"]


def test_a_low_quality_answer_is_still_capturable(database) -> None:
    """A person deciding is the gate; the 0.55 score is not applied here.

    That threshold belongs to unattended Historical promotion, which chooses
    among thousands of backfilled rows on its own.  Here an operator has read
    this one answer, which is the whole point of the button.
    """

    short = "네, 가능합니다."
    inquiry_id = coupang_inquiry(database, answer=short)

    # The same text would be refused by Historical promotion's gate.
    case = service(database)._historical_case(inquiry_of(database, inquiry_id))
    assert case is not None
    assert HistoricalCaseService.promotion_block_reason(case) is not None

    status = service(database).status(inquiry_of(database, inquiry_id))
    assert status.available is True

    saved = approve(database, inquiry_id)
    assert short in str(saved["final_answer"])
    assert saved["metadata_json"]["market_applicability"] == "COUPANG_ONLY"


def test_historical_bulk_promotion_keeps_its_own_quality_gate(database) -> None:
    """The manual path must not have relaxed the unattended one."""

    from services.historical_case_service import PROMOTION_MINIMUM_QUALITY

    assert PROMOTION_MINIMUM_QUALITY == 0.55
    assert HistoricalCaseService.promotion_block_reason(
        {"quality_score": 0.54, "policy_risk": "NONE"}
    ) == "QUALITY_SCORE"
    assert HistoricalCaseService.promotion_block_reason(
        {"quality_score": 0.9, "policy_risk": "HIGH"}
    ) == "POLICY_RISK"


def test_the_captured_row_uses_the_same_builder_as_the_naver_manual_capture(
    database,
) -> None:
    """Same shared builder, same source_key rule -- only the text differs."""

    inquiry_id = coupang_inquiry(database)
    inquiry = inquiry_of(database, inquiry_id)
    from services.learning_service import LearningService

    built = LearningService(database).marketplace_answer_example(
        inquiry=inquiry, answer=SELLER_ANSWER,
    )
    saved = approve(database, inquiry_id)

    assert built is not None
    assert saved["source_key"] == built["source_key"]
    assert saved["learning_source"] == built["learning_source"] == "SELLER_ANSWER"


# --- the Naver approval / cancellation policy, reused ---------------------------

def test_approval_is_entered_where_naver_enters_it(database) -> None:
    """The same service method decides it, so it is one policy, not two."""

    inquiry_id = coupang_inquiry(database)
    saved = ApprovalService(database).approve_posted_answer(
        inquiry_id=inquiry_id,
        actor="관리자",
        positive_reason="CONTENT_ACCURATE",
        positive_note="설치 옵션 안내가 정확함",
    )

    assert saved["metadata_json"]["human_verified"] is True
    assert saved["metadata_json"]["positive_reason"] == "CONTENT_ACCURATE"
    assert saved["metadata_json"]["positive_note"] == "설치 옵션 안내가 정확함"
    assert saved["active"] is True


def test_cancelling_approval_deactivates_the_learning(database) -> None:
    """승인 취소 deactivates it, exactly as it does for a Naver answer."""

    inquiry_id = coupang_inquiry(database)
    saved = approve(database, inquiry_id)
    assert saved["active"] is True

    outcome = cancel(database, inquiry_id, reason="안내 내용이 바뀌었습니다")

    assert outcome.learning is not None
    assert int(outcome.learning["id"]) == int(saved["id"])
    after = LearningRepository(database).get(int(saved["id"]))
    assert after["active"] is False
    assert after["metadata_json"]["human_verified"] in (0, False)
    assert after["metadata_json"]["learning_status"] == "REVOKED"
    # A deactivated row is out of retrieval for both accounts.
    assert int(saved["id"]) not in _candidate_ids(database, "COUPANG_OJE_NS")
    assert int(saved["id"]) not in _candidate_ids(database, "COUPANG_OJE_PLUS")


def test_cancelling_requires_a_reason(database) -> None:
    inquiry_id = coupang_inquiry(database)
    approve(database, inquiry_id)

    with pytest.raises(ValueError):
        cancel(database, inquiry_id, reason="   ")


def test_approving_again_after_cancellation_reactivates_one_row(database) -> None:
    """Re-approval is the same row again, never a second one."""

    inquiry_id = coupang_inquiry(database)
    first = approve(database, inquiry_id)
    cancel(database, inquiry_id, reason="확인 필요")
    before = learning_count(database)

    second = approve(database, inquiry_id)

    assert int(second["id"]) == int(first["id"])
    assert learning_count(database) == before
    assert second["active"] is True


def test_an_unanswered_coupang_inquiry_cannot_be_approved(database) -> None:
    inquiry_id = coupang_inquiry(database, answer=None)

    with pytest.raises(Exception):
        approve(database, inquiry_id)
    assert learning_count(database) == 0


def test_the_screen_finds_the_learning_it_just_approved(database) -> None:
    """Without this the 승인 취소 button could never become available."""

    from ui.review_workspace import approval_learning_trace
    from repositories.approval_repository import ApprovalRepository

    inquiry_id = coupang_inquiry(database)
    saved = approve(database, inquiry_id)

    trace = approval_learning_trace(
        database,
        inquiry_id=inquiry_id,
        draft=None,
        approval_state=ApprovalRepository(database).get_inquiry_approval(
            inquiry_id
        ),
        source_answered=True,
        seller_answer=SELLER_ANSWER,
    )
    assert trace["approval_complete"] is True
    assert trace["positive_learning_id"] == int(saved["id"])
    assert trace["positive_active"] is True
    assert trace["human_verified"] is True


def test_the_read_only_panel_offers_approval_and_nothing_else(database) -> None:
    """G: the Learning decision is the only control this panel adds."""

    from ui import review_workspace

    inquiry_id = coupang_inquiry(database)
    inquiry = inquiry_of(database, inquiry_id)
    inquiry["seller_answer"] = SELLER_ANSWER

    assert review_workspace._is_read_only_inquiry(inquiry) is True
    assert review_workspace._seller_answer_learning_approval(inquiry) is True
    # Naver keeps its own posted-answer path; this predicate is not for it.
    naver = inquiry_of(database, naver_inquiry(database))
    naver["seller_answer"] = "네이버 답변"
    assert review_workspace._seller_answer_learning_approval(naver) is False
    # Nothing was opened for writing an answer.
    assert market_policy.is_store_post_enabled("COUPANG_OJE_NS") is False
    assert market_policy.is_store_dps_enabled("COUPANG_OJE_NS") is False
    assert market_policy.is_kakao_market_enabled("COUPANG") is False
    assert market_policy.is_store_automatic_generation_enabled(
        "COUPANG_OJE_NS"
    ) is False
