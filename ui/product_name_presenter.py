"""The product a stored Learning or Historical row was about, for display.

A Coupang inquiry's payload carries no product name -- only a sellerProductId
-- and the name the dashboard shows is attached when the row is read for
display.  The storage paths did not do that until 1b39c35, so rows written
before it recorded no product and read as "상품: -" on screen even though the
inquiry they came from names one.

This resolves that name at read time and only at read time.  It writes
nothing: no row is updated, no column is backfilled, and a stored name always
wins over anything found here, so a row that already knows its product keeps
exactly what it stored.

Nothing is invented.  A model code, a listing id, an option name or the
inquiry text is never shown as a product name; where no name exists the caller
keeps its dash.
"""

from __future__ import annotations

from typing import Any, Mapping

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.market_policy import NAVER, market_of


def _clean(value: object) -> str:
    return str(value or "").strip()


def _metadata_product_name(metadata: object) -> str:
    """A name an earlier write already recorded in the row's own metadata."""

    if not isinstance(metadata, Mapping):
        return ""
    identity = metadata.get("product_identity")
    if isinstance(identity, Mapping):
        name = _clean(identity.get("product_name"))
        if name:
            return name
    return _clean(metadata.get("product_name"))


class ProductNameResolver:
    """Resolves display names for one render pass, reading each source once.

    A screen shows several Learning candidates at a time and many of them come
    from the same inquiry, so the marketplace read is memoised per instance.
    The memo lives as long as the render and is not a cache anything else can
    observe.
    """

    def __init__(self, database: Database) -> None:
        self.inquiries = InquiryRepository(database)
        self._by_source: dict[tuple[str, str, str], str] = {}

    def resolve(
        self,
        *,
        stored: object = None,
        metadata: object = None,
        store_code: object = None,
        source_type: object = None,
        source_question_id: object = None,
    ) -> str:
        """The name to show, or ``""`` when nothing reliable names it."""

        name = _clean(stored)
        if name:
            return name
        name = _metadata_product_name(metadata)
        if name:
            return name
        return self._marketplace_name(
            store_code=store_code,
            source_type=source_type,
            source_question_id=source_question_id,
        )

    def _marketplace_name(
        self, *, store_code: object, source_type: object, source_question_id: object,
    ) -> str:
        """The name the marketplace enrichment attaches to this inquiry.

        Only for a market whose rows store one elsewhere: a Naver row keeps
        whatever it stored and is never re-read here.  The store code carries
        the seller account, so one Coupang account's catalogue cannot name the
        other's inquiry.
        """

        store = _clean(store_code)
        source = _clean(source_type)
        question_id = _clean(source_question_id)
        if not (store and source and question_id):
            return ""
        market = market_of(store)
        if market is None or market == NAVER:
            return ""
        key = (store, source, question_id)
        if key in self._by_source:
            return self._by_source[key]
        try:
            inquiry = self.inquiries.get_by_source(store, source, question_id)
        except Exception:  # noqa: BLE001 - a display name never breaks a screen
            inquiry = None
        name = _clean((inquiry or {}).get("product_name"))
        self._by_source[key] = name
        return name

    def for_display(self, **kwargs: Any) -> str:
        """The same answer with the screen's dash when there is no name."""

        return self.resolve(**kwargs) or "-"
