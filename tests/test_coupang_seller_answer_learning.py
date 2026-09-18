"""Keeping a Coupang seller's own reply as Learning, when a person asks.

The inquiry stays read-only: nothing here generates, edits, approves or
registers an answer.  The one added action writes a Learning row, and it
writes it through the same three stages the 742 backfilled Coupang answers
travelled, so pressing the button on one of those produces no second row.
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

    saved = service(database).capture(inquiry_id, actor="관리자")

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
    saved = service(database).capture(inquiry_id, actor="관리자")

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
    # The Historical case it came from is still named, as for every promotion.
    assert metadata["source_origin"] == "HISTORICAL_PROMOTED"
    assert metadata["historical_case_id"]
    assert metadata["historical_fingerprint"]


# --- C/D. it never writes a second row ------------------------------------------

def test_pressing_the_button_twice_writes_one_row(database) -> None:
    inquiry_id = coupang_inquiry(database)
    first = service(database).capture(inquiry_id, actor="관리자")
    after_first = learning_count(database)

    second = service(database).capture(inquiry_id, actor="관리자")

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

    again = service(database).capture(inquiry_id, actor="관리자")
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
    saved = service(database).capture(inquiry_id, actor="관리자")

    assert int(saved["id"]) in _candidate_ids(database, "COUPANG_OJE_NS")
    assert int(saved["id"]) in _candidate_ids(database, "COUPANG_OJE_PLUS")


def test_naver_never_retrieves_a_captured_coupang_answer(database) -> None:
    """F: a Coupang marketplace reply is not evidence for a Naver customer."""

    inquiry_id = coupang_inquiry(database)
    saved = service(database).capture(inquiry_id, actor="관리자")

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

    service(database).capture(inquiry_id, actor="관리자")

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

    saved = service(database).capture(inquiry_id, actor="관리자")

    metadata = saved["metadata_json"]
    provenance = metadata["market_provenance"]
    assert provenance["confirmed_canonical_model"] == "LH43BEHHLGFXKR"
    assert provenance["model_identity_source"] == "COUPANG_CONFIRMED_MAPPING"
    assert metadata["canonical_model"] == "LH43BEDH"
    assert saved["model_code"] == "LH43BEDH"
    with database.connection() as connection:
        row = connection.execute(
            "SELECT canonical_model, mapping_status FROM coupang_product_mappings"
        ).fetchone()
    assert row["canonical_model"] == "LH43BEHHLGFXKR"
    assert row["mapping_status"] == CONFIRMED


def test_without_a_confirmed_mapping_no_model_is_invented(database) -> None:
    inquiry_id = coupang_inquiry(database)
    saved = service(database).capture(inquiry_id, actor="관리자")

    assert saved["model_code"] is None
    assert saved["metadata_json"].get("canonical_model") is None
    provenance = saved["metadata_json"].get("market_provenance") or {}
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

    stored_case, _ = service(database)._prepare(inquiry_of(database, inquiry_id))

    assert stored_case["case_key"] == live_case["case_key"]
    assert stored_case["fingerprint"] == live_case["fingerprint"]
