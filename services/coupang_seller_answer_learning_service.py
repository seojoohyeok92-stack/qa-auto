"""Capture a Coupang seller's own reply into Learning, when a person asks.

An answered Coupang inquiry is read-only here: the reply was written by a
person in Wing, this system never posted it, and nothing on that screen
generates, edits, approves or registers an answer.  What was missing was a way
to keep a good reply -- an operator could read one and had no way to tell the
Learning corpus about it.

That is the same action Naver already has.  There, an operator reads the
answer the marketplace actually shows and keeps it through
``LearningService.capture_verified_posted_answer``.  This is that path with a
different source for the text, so it uses the Coupang twin,
``capture_verified_marketplace_answer``, and adds no policy of its own: the
shared builder, the negative-signal guard, the human-verified upsert and the
positive signal all belong to the Naver path.

In particular there is no quality threshold.  The 0.55 score belongs to
unattended Historical promotion, which decides for itself which of thousands of
backfilled rows may become Learning; here a person has read this one answer and
pressed a button, and that is the decision.  Historical promotion keeps its
gate untouched.

Two things stop a duplicate.  A repeat click rebuilds the same Learning row --
its ``source_key`` is derived from the question and the answer -- so the upsert
updates in place.  And an inquiry the historical backfill already promoted is
recognised by that path's own key, read without writing anything, so the 742
are never copied.

Nothing here calls Coupang.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.historical_case_service import HistoricalCaseService
from services.learning_service import LearningService
from services.market_policy import COUPANG, account_of_store, market_of


# Why this inquiry has no capture action, in the operator's words.
UNAVAILABLE_REASONS: dict[str, str] = {
    "NOT_COUPANG": "쿠팡 문의가 아닙니다.",
    "NOT_ANSWERED": "판매자 답변이 완료된 문의가 아닙니다.",
    "NO_SELLER_ANSWER": "저장된 판매자 답변 본문이 없습니다.",
    "MULTI_COMMENT_REVIEW": (
        "댓글이 여러 개라 어느 것이 판매자 답변인지 확정할 수 없습니다."
    ),
    "ANSWER_NOT_REUSABLE": (
        "기간이 지난 정책·배송 표현이 있어 Learning에 반영할 수 없습니다."
    ),
}


@dataclass(frozen=True)
class SellerAnswerLearningStatus:
    """What the screen may offer for this inquiry, and why."""

    available: bool
    captured: bool
    learning_example_id: int | None = None
    reason: str | None = None

    @property
    def message(self) -> str:
        return UNAVAILABLE_REASONS.get(str(self.reason or ""), "")


class CoupangSellerAnswerLearningService:
    """The one write this read-only screen may perform, on an explicit click."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.learning = LearningRepository(database)
        self.learning_service = LearningService(database)
        self.normalizer = CoupangInquiryNormalizer()

    # -- reading -----------------------------------------------------------

    def status(self, inquiry: dict[str, Any]) -> SellerAnswerLearningStatus:
        """Whether this inquiry's seller answer may be, or already is, kept.

        Read-only: the Learning row is built but not saved, only to read the
        key it would be stored under.
        """

        prepared = self._prepare(inquiry)
        if isinstance(prepared, str):
            return SellerAnswerLearningStatus(
                available=False, captured=False, reason=prepared
            )
        answer, _provenance = prepared
        example = self.learning_service.marketplace_answer_example(
            inquiry=inquiry, answer=answer
        )
        if example is None:
            return SellerAnswerLearningStatus(
                available=False, captured=False, reason="ANSWER_NOT_REUSABLE",
            )
        existing = self._existing(inquiry, example)
        if existing is not None:
            return SellerAnswerLearningStatus(
                available=True, captured=True,
                learning_example_id=int(existing["id"]),
            )
        return SellerAnswerLearningStatus(available=True, captured=False)

    def status_for_inquiry(self, inquiry_id: int) -> SellerAnswerLearningStatus:
        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        return self.status(inquiry)

    # -- writing -----------------------------------------------------------

    def capture(self, inquiry_id: int, *, actor: str = "관리자") -> dict[str, Any]:
        """Keep this seller answer as Learning; return the row either way."""

        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        prepared = self._prepare(inquiry)
        if isinstance(prepared, str):
            raise ValueError(
                UNAVAILABLE_REASONS.get(prepared, "Learning에 반영할 수 없습니다.")
            )
        answer, provenance = prepared
        example = self.learning_service.marketplace_answer_example(
            inquiry=inquiry, answer=answer
        )
        if example is None:
            raise ValueError(UNAVAILABLE_REASONS["ANSWER_NOT_REUSABLE"])
        promoted = self._historical_learning(inquiry)
        if promoted is not None:
            # Already in the corpus under Historical promotion's own key.
            # Writing the same answer again under this path's key would be a
            # second copy of one reply.
            return promoted
        saved = self.learning_service.capture_verified_marketplace_answer(
            inquiry_id=int(inquiry_id),
            answer=answer,
            actor=str(actor or "관리자"),
            provenance=provenance,
        )
        if saved is None:
            raise ValueError(UNAVAILABLE_REASONS["ANSWER_NOT_REUSABLE"])
        return saved

    # -- internals ---------------------------------------------------------

    def _existing(
        self, inquiry: dict[str, Any], example: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The Learning row this answer already has, by either path's key."""

        return (
            self.learning.get_by_source_key(str(example["source_key"]))
            or self._historical_learning(inquiry)
        )

    def _historical_learning(
        self, inquiry: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The row the historical backfill already promoted, if it did.

        Read-only.  ``prepare_case`` is a pure derivation plus one count
        query; nothing is written to Historical from here, and its promotion
        gate is never consulted -- only the key it would have produced, so the
        742 are recognised rather than copied.
        """

        case = self._historical_case(inquiry)
        if case is None:
            return None
        return self.learning.get_by_source_key(
            HistoricalCaseService._digest(
                "HISTORICAL_PROMOTED", case["fingerprint"]
            )
        )

    def _historical_case(self, inquiry: dict[str, Any]) -> dict[str, Any] | None:
        raw = inquiry.get("raw_json")
        raw = raw if isinstance(raw, dict) else {}
        account_code = self._account_code(inquiry)
        normalized = self.normalizer.online(raw, account_code=account_code or None)
        if not normalized.seller_answer:
            return None
        # The stored payload is privacy-masked, and a nine-digit Coupang
        # inquiryId inside it comes back ``<masked-...>``.  ``prepare_case``
        # puts the external id into its digest unmasked, so re-deriving it
        # from the payload would hash a different string than the backfill
        # hashed from the live response -- and none of the 742 would be
        # recognised.  The inquiry's own columns kept the real value.
        external_id = str(
            inquiry.get("external_inquiry_id")
            or inquiry.get("source_question_id")
            or normalized.external_inquiry_id
            or ""
        )
        candidate = dict(normalized.to_work_item())
        candidate.update({
            "external_inquiry_id": external_id,
            "source_question_id": external_id,
            "inquiry_id": external_id,
            "local_inquiry_id": inquiry.get("id"),
            "seller_answer": normalized.seller_answer,
            "source_answered": True,
            "raw_payload": normalized.raw_payload,
            "historical_metadata": {"candidate_only": True, "market": COUPANG},
        })
        return HistoricalCaseService(self.database).prepare_case(
            candidate,
            source_reference=(
                f"COUPANG_ONLINE_API:{account_code}:{external_id}"
            ),
        )

    @staticmethod
    def _account_code(inquiry: dict[str, Any]) -> str:
        metadata = inquiry.get("source_metadata_json")
        metadata = metadata if isinstance(metadata, dict) else {}
        return str(
            metadata.get("account_code")
            or account_of_store(inquiry.get("store_code"))
            or ""
        ).strip()

    def _prepare(
        self, inquiry: dict[str, Any]
    ) -> tuple[str, dict[str, Any]] | str:
        """The seller answer to keep and the provenance to keep with it.

        Returns a reason code instead when there is nothing to keep.
        """

        store_code = inquiry.get("store_code")
        if market_of(store_code) != COUPANG:
            return "NOT_COUPANG"
        if not inquiry.get("source_answered"):
            return "NOT_ANSWERED"
        raw = inquiry.get("raw_json")
        raw = raw if isinstance(raw, dict) else {}
        account_code = self._account_code(inquiry)
        normalized = self.normalizer.online(raw, account_code=account_code or None)
        # Which of several comments is the seller's is not decided here, for
        # the same reason the historical import does not decide it.
        if normalized.answer_selection_status == "MULTI_COMMENT_REVIEW":
            return "MULTI_COMMENT_REVIEW"
        if not normalized.seller_answer:
            return "NO_SELLER_ANSWER"
        return normalized.seller_answer, self._provenance(
            inquiry, normalized, account_code, raw,
        )

    def _provenance(
        self,
        inquiry: dict[str, Any],
        normalized: Any,
        account_code: str,
        raw: dict[str, Any],
    ) -> dict[str, Any]:
        """What the Learning row records about where this answer came from.

        ``store_code`` is deliberately not among it: the shared builder drops
        that column for a Coupang origin so both seller accounts read one
        corpus, and the market filter, not a store column, is what keeps it
        away from Naver.  The account and the listing ids stay here instead.
        """

        provenance: dict[str, Any] = {
            "source_origin_detail": "COUPANG_SELLER_ANSWER_MANUAL_CAPTURE",
            "captured_from_inquiry_id": inquiry.get("id"),
            "account_code": account_code or None,
            "origin_store_code": inquiry.get("store_code"),
            "source_question_id": inquiry.get("source_question_id"),
            "external_inquiry_id": (
                inquiry.get("external_inquiry_id")
                or inquiry.get("source_question_id")
            ),
            "seller_product_id": raw.get("sellerProductId"),
            "vendor_item_id": raw.get("vendorItemId"),
            "product_id": inquiry.get("product_id") or raw.get("productId"),
            "source_created_at": normalized.source_created_at,
            "seller_answer_selection": normalized.answer_selection_status,
            "inquiry_comment_id": normalized.inquiry_comment_id,
            "seller_answer_provenance": "MARKETPLACE_SELLER_ANSWER",
        }
        model = self._confirmed_model(inquiry)
        if model is not None:
            # The catalogue representative is the scope, because that is what
            # the answer path asks Product Knowledge for; the operator's
            # CONFIRMED value is kept beside it and its mapping row is not
            # touched.  Without a CONFIRMED mapping there is no scope at all --
            # guessing one would file this answer under another product.
            provenance.update({
                "canonical_model": model["model_code"],
                "confirmed_canonical_model": model["canonical_model"],
                "model_identity_source": "COUPANG_CONFIRMED_MAPPING",
            })
        return provenance

    def _confirmed_model(self, inquiry: dict[str, Any]) -> dict[str, Any] | None:
        """The operator-confirmed model for this option, or nothing.

        Imported here rather than at module scope: the answer service owns this
        rule and is a heavy import that this screen does not otherwise need.
        """

        from repositories.product_catalog_repository import ProductCatalogRepository
        from services.answer_service import coupang_confirmed_model

        return coupang_confirmed_model(
            self.database, inquiry, ProductCatalogRepository()
        )
