"""What a stored Coupang answer records about the product it was about.

A Coupang inquiry's payload carries no product name -- only a sellerProductId
-- and the name the dashboard shows is attached when the row is read for
display.  The storage paths read the row without that, so a Historical case,
the Learning row promoted from it, and a Human Verified Coupang answer all
recorded no product at all, while the card beside them named one.

These pin that the storage paths now read the row the same way the screen
does, and that nothing is invented when the catalogue genuinely has no name.
Nothing here calls Coupang: the catalogue rows are fixtures in a temp
database.
"""

from __future__ import annotations

from typing import Any

import pytest

from repositories.coupang_product_catalog_repository import (
    CoupangProductCatalogRepository,
)
from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from services.coupang_historical_inquiry_backfill_service import (
    CoupangHistoricalInquiryBackfillService,
)
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.historical_case_service import HistoricalCaseService
from services.inquiry_sync_service import normalize_work_item
from services.learning_service import LearningService

SPID = "15654321531"
VENDOR_ITEM = "93128932886"
PRODUCT_NAME = "삼성 스마트모니터 M5 M50D 32인치 화이트"
OPTION_NAME = "스탠드형 방문설치 32인치"
QUESTION = "이 제품 스피커 내장되어 있나요?"
SELLER_ANSWER = "네, 스피커가 내장되어 있습니다."
CANONICAL_MODEL = "32DM501"


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "coupang_names.db")
    value.initialize()
    return value


def seed_mapping(database: Database, *, account: str = "OJE_NS") -> None:
    """The CONFIRMED option mapping the backfill requires before it stores."""

    from repositories.coupang_product_mapping_repository import (
        CONFIRMED, MANUAL, CoupangProductMappingRepository,
    )

    CoupangProductMappingRepository(database).upsert(
        account_code=account, vendor_item_id=VENDOR_ITEM,
        seller_product_id=SPID, canonical_model=CANONICAL_MODEL,
        mapping_source=MANUAL, mapping_status=CONFIRMED,
    )


def seed_catalog(
    database: Database,
    *,
    account: str = "OJE_NS",
    product_name: str = PRODUCT_NAME,
    option_name: str = OPTION_NAME,
) -> None:
    catalog = CoupangProductCatalogRepository(database)
    catalog.upsert_product(
        account_code=account,
        data={
            "sellerProductId": SPID, "status": "APPROVED",
            "sellerProductName": product_name,
        },
        sync_token="t",
    )
    catalog.upsert_option(
        account_code=account, seller_product_id=SPID,
        item={"vendorItemId": VENDOR_ITEM, "itemName": option_name},
    )
    # A sync marks a listing active; the backfill only stores answers for a
    # model still being sold, so the fixture has to say it is.
    catalog.set_product_active(
        account_code=account, seller_product_id=SPID, is_active=True,
    )


def payload(inquiry_id: str = "160959847", *, answered: bool = True) -> dict[str, Any]:
    return {
        "inquiryId": inquiry_id, "sellerProductId": SPID,
        "vendorItemId": VENDOR_ITEM, "content": QUESTION,
        "inquiryAt": "2026-09-17T10:00:00+09:00", "orderIds": [],
        "commentDtoList": [{
            "inquiryCommentId": "c1", "inquiryId": inquiry_id,
            "content": SELLER_ANSWER,
            "inquiryCommentAt": "2026-09-17T11:00:00+09:00",
        }] if answered else [],
    }


def coupang_inquiry(
    database: Database, *, account: str = "OJE_NS",
    inquiry_id: str = "160959847", answered: bool = True,
) -> int:
    ready = normalize_work_item(
        CoupangInquiryNormalizer()
        .online(payload(inquiry_id, answered=answered), account_code=account)
        .to_work_item()
    )
    row_id = InquiryRepository(database).upsert_work_item(ready).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


class OneItemBackfill(CoupangHistoricalInquiryBackfillService):
    """The real backfill, handed one payload instead of a Coupang page."""

    def run_one(self, account: str, item: dict[str, Any]):
        from services.coupang_historical_inquiry_backfill_service import (
            CoupangHistoricalBackfillResult,
        )

        result = CoupangHistoricalBackfillResult(account_code=account)
        self._save_item(account, item, result)
        return result


def seed_operated(database: Database, *, account: str = "OJE_NS") -> None:
    """One account's evidence that this model is still on sale.

    ``is_canonical_model_currently_active`` joins product, option and mapping
    within one account, so the whole triple has to be there.
    """

    seed_catalog(database, account=account)
    seed_mapping(database, account=account)


def backfill(database: Database, *, account: str = "OJE_NS", inquiry_id: str = "160959847"):
    seed_mapping(database, account=account)
    service = OneItemBackfill(database)
    result = service.run_one(account, payload(inquiry_id))
    assert not result.failed, vars(result)
    return result


def historical_case(database: Database) -> dict[str, Any]:
    cases = HistoricalCaseRepository(database).list_cases(limit=10)
    assert cases, "historical case was not created"
    return cases[0]


# --- A. the Historical case records the product ---------------------------------

def test_a_backfilled_case_records_the_catalogued_product_name(database) -> None:
    seed_catalog(database)
    backfill(database)

    case = historical_case(database)
    assert case["product_name"] == PRODUCT_NAME
    # The raw inquiry row still has none; the name came from the catalogue.
    raw = InquiryRepository(database).get(int(case["inquiry_id"]))
    assert not str(raw.get("product_name") or "").strip()


# --- B. the promoted Learning row carries it through ----------------------------

def test_the_promoted_learning_row_carries_the_product_name(database) -> None:
    seed_catalog(database)
    backfill(database)
    case = historical_case(database)
    cases = HistoricalCaseService(database)
    HistoricalCaseRepository(database).set_learning_enabled(
        int(case["id"]), True, actor="관리자",
    )

    saved = cases.promote(int(case["id"]), actor="관리자")

    assert saved["product_name"] == PRODUCT_NAME


# --- C/D. the Human Verified marketplace answer ---------------------------------

def test_a_human_verified_coupang_answer_records_the_product(database) -> None:
    seed_catalog(database)
    inquiry_id = coupang_inquiry(database)

    saved = LearningService(database).capture_verified_marketplace_answer(
        inquiry_id=inquiry_id, answer=SELLER_ANSWER, actor="관리자",
    )

    assert saved is not None
    assert saved["product_name"] == PRODUCT_NAME
    # And the identity built from it names the product too.
    identity = saved["metadata_json"]["product_identity"]
    assert identity["product_name"] == PRODUCT_NAME


def test_the_option_name_comes_from_the_same_enrichment(database) -> None:
    seed_catalog(database)
    backfill(database)

    case = historical_case(database)
    stored = InquiryRepository(database).get_by_source(
        "COUPANG_OJE_NS", "COUPANG_ONLINE_INQUIRY", "160959847",
    )
    assert stored["option_name"] == OPTION_NAME
    assert case["product_name"] == stored["product_name"]


# --- E. nothing is invented -----------------------------------------------------

def test_without_a_catalogued_name_nothing_is_invented(database) -> None:
    """No model code, no id, no option text standing in for a product name."""

    # The listing is sold -- so the answer is stored -- but the catalogue
    # holds no name for it.
    seed_catalog(database, product_name="", option_name="")
    seed_mapping(database)
    backfill(database)

    case = historical_case(database)
    assert not str(case.get("product_name") or "").strip()
    assert SPID not in str(case.get("product_name") or "")
    assert VENDOR_ITEM not in str(case.get("product_name") or "")


def test_a_human_verified_answer_without_a_name_stores_none(database) -> None:
    inquiry_id = coupang_inquiry(database)

    saved = LearningService(database).capture_verified_marketplace_answer(
        inquiry_id=inquiry_id, answer=SELLER_ANSWER, actor="관리자",
    )

    assert saved is not None
    assert not str(saved.get("product_name") or "").strip()


# --- F. Naver is untouched ------------------------------------------------------

def naver_inquiry(database: Database, *, product_name: str = "삼성 M5 32인치") -> int:
    row_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "N-1", "inquiry_type": "상품",
        "title": "상품 문의", "content": QUESTION,
        "product_name": product_name, "option_name": "32인치",
        "source_answered": True, "raw_json": {},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(row_id)
    return row_id


def test_a_naver_inquiry_is_read_exactly_as_before(database) -> None:
    inquiry_id = naver_inquiry(database)
    service = LearningService(database)

    raw = service.inquiries.get(inquiry_id)
    resolved = service._marketplace_inquiry(inquiry_id)

    # Not re-read through the marketplace path at all: the same row.
    assert resolved == raw
    assert resolved["product_name"] == "삼성 M5 32인치"


def test_naver_learning_still_stores_its_own_product_name(database) -> None:
    inquiry_id = naver_inquiry(database)

    saved = LearningService(database).capture_seller_answer(
        inquiry_id=inquiry_id, answer="네, 스피커가 내장되어 있습니다.",
    )

    assert saved is not None
    assert saved["product_name"] == "삼성 M5 32인치"


# --- G. the dedupe key does not move --------------------------------------------

def test_enrichment_does_not_change_the_dedupe_key(database) -> None:
    """product_name is not in the digest, so the 742 cannot be duplicated."""

    cases = HistoricalCaseService(database)
    base = {
        "store_code": "COUPANG_OJE_NS", "source_type": "COUPANG_ONLINE_INQUIRY",
        "source_question_id": "160959847", "external_inquiry_id": "160959847",
        "title": "Coupang 상품문의", "content": "방문설치는 어떻게 신청하나요?",
        "seller_answer": SELLER_ANSWER, "source_answered": True,
        "historical_metadata": {"candidate_only": True, "market": "COUPANG"},
    }
    without = cases.prepare_case(dict(base), source_reference="ref")
    with_name = cases.prepare_case(
        {**base, "product_name": PRODUCT_NAME, "option_name": OPTION_NAME},
        source_reference="ref",
    )

    assert without["case_key"] == with_name["case_key"]
    assert without["fingerprint"] == with_name["fingerprint"]


def test_backfilling_the_same_inquiry_twice_creates_one_case(database) -> None:
    seed_catalog(database)

    backfill(database)
    first = HistoricalCaseRepository(database).list_cases(limit=10)
    backfill(database)
    second = HistoricalCaseRepository(database).list_cases(limit=10)

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["case_key"] == second[0]["case_key"]
    assert first[0]["fingerprint"] == second[0]["fingerprint"]


# --- H. both seller accounts ----------------------------------------------------

@pytest.mark.parametrize("account", ["OJE_NS", "OJE_PLUS"])
def test_both_coupang_accounts_record_their_own_product_name(
    database, account,
) -> None:
    name = f"{PRODUCT_NAME} · {account}"
    # Whether the model is still sold is asked account-agnostically, so one
    # account's evidence satisfies the gate; the name comes from this
    # account's own catalogue row.
    seed_operated(database)
    seed_catalog(database, account=account, product_name=name)
    backfill(database, account=account, inquiry_id="17000030")

    assert historical_case(database)["product_name"] == name


def test_one_accounts_catalogue_does_not_name_the_others_inquiry(database) -> None:
    """The enrichment is account-scoped, as the same ids repeat per account."""

    seed_operated(database, account="OJE_NS")
    backfill(database, account="OJE_PLUS", inquiry_id="17000031")

    assert not str(historical_case(database).get("product_name") or "").strip()


# --- I. no Coupang call ---------------------------------------------------------

def test_nothing_reaches_coupang(database, monkeypatch) -> None:
    import requests

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no Coupang request may be made")

    monkeypatch.setattr(requests, "request", forbidden)
    monkeypatch.setattr(requests, "get", forbidden)
    monkeypatch.setattr(requests, "post", forbidden)

    seed_catalog(database)
    backfill(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17000032")
    LearningService(database).capture_verified_marketplace_answer(
        inquiry_id=inquiry_id, answer=SELLER_ANSWER, actor="관리자",
    )

    assert historical_case(database)["product_name"] == PRODUCT_NAME


# --- existing rows are not touched ----------------------------------------------

def test_existing_rows_are_left_exactly_as_they_are(database) -> None:
    """Nothing backfills a name onto a row that was stored without one."""

    seed_catalog(database, product_name="", option_name="")
    seed_mapping(database)
    backfill(database)
    before = historical_case(database)
    assert not str(before.get("product_name") or "").strip()

    # The catalogue gains the name later; the stored row is not rewritten.
    seed_catalog(database)
    after = historical_case(database)
    assert after["product_name"] == before["product_name"]
    assert not str(after.get("product_name") or "").strip()
    assert LearningRepository(database).candidates(store_code=None) == []
