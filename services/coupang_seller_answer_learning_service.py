"""Capture a Coupang seller's own reply into Learning, when a person asks.

An answered Coupang inquiry is read-only here: the reply was written by a
person in Wing, this system never posted it, and nothing on that screen
generates, edits, approves or registers an answer.  What was missing was a way
to keep a good reply -- an operator could read one and had no way to tell the
Learning corpus about it.

This adds that one action and nothing else.  It writes no new kind of row: the
reply travels the same three stages the 742 backfilled Coupang answers already
travelled --

    CoupangInquiryNormalizer  ->  HistoricalCaseService.prepare_case
                              ->  HistoricalCaseService.promote
                              ->  LearningService.capture_historical_promotion

-- so it is deduplicated against them for free.  ``prepare_case`` derives a
``case_key`` from the store, source type, external id and normalised question,
and a ``fingerprint`` from that plus the answer; promotion's Learning
``source_key`` is ``HISTORICAL_PROMOTED|<fingerprint>``.  An inquiry already
promoted by the backfill therefore produces the identical key, and the existing
Learning row is returned rather than a second one written.

The reply is re-derived from the stored payload through the normaliser rather
than from the joined text the screen shows, for the same reason: the backfill
selected a single unambiguous comment, and hashing a different string would
mean a different fingerprint and a duplicate of a row that already exists.
Nothing here calls Coupang.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.historical_case_service import HistoricalCaseService
from services.market_policy import COUPANG, account_of_store, market_of


# Why this inquiry has no capture action, in the operator's words.
UNAVAILABLE_REASONS: dict[str, str] = {
    "NOT_COUPANG": "쿠팡 문의가 아닙니다.",
    "NOT_ANSWERED": "판매자 답변이 완료된 문의가 아닙니다.",
    "NO_SELLER_ANSWER": "저장된 판매자 답변 본문이 없습니다.",
    "MULTI_COMMENT_REVIEW": (
        "댓글이 여러 개라 어느 것이 판매자 답변인지 확정할 수 없습니다."
    ),
    "QUALITY_SCORE": "품질 기준(0.55)에 미달하여 Learning에 반영할 수 없습니다.",
    "POLICY_RISK": "정책 충돌 위험이 있어 Learning에 반영할 수 없습니다.",
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
        self.cases = HistoricalCaseService(database)
        self.case_repository = HistoricalCaseRepository(database)
        self.learning = LearningRepository(database)
        self.normalizer = CoupangInquiryNormalizer()

    # -- reading -----------------------------------------------------------

    def status(self, inquiry: dict[str, Any]) -> SellerAnswerLearningStatus:
        """Whether this inquiry's seller answer may be, or already is, kept.

        Read-only.  ``prepare_case`` is a pure derivation plus one count
        query, so the screen can ask this on every render without writing.
        """

        prepared = self._prepare(inquiry)
        if isinstance(prepared, str):
            return SellerAnswerLearningStatus(
                available=False, captured=False, reason=prepared
            )
        case, _ = prepared
        existing = self._promoted_learning(case)
        if existing is not None:
            return SellerAnswerLearningStatus(
                available=True, captured=True,
                learning_example_id=int(existing["id"]),
            )
        stored = self.case_repository.get_by_case_key(case["case_key"])
        blocked = HistoricalCaseService.promotion_block_reason(stored or case)
        if blocked is not None:
            return SellerAnswerLearningStatus(
                available=False, captured=False, reason=blocked
            )
        return SellerAnswerLearningStatus(available=True, captured=False)

    def status_for_inquiry(self, inquiry_id: int) -> SellerAnswerLearningStatus:
        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        return self.status(inquiry)

    # -- writing -----------------------------------------------------------

    def capture(self, inquiry_id: int, *, actor: str = "관리자") -> dict[str, Any]:
        """Keep this seller answer as Learning; return the row either way.

        Pressing the button twice writes one row.  So does pressing it on an
        inquiry the historical backfill already promoted: the Learning
        ``source_key`` is derived from the same fingerprint, so promotion
        finds the existing row and returns it untouched.
        """

        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        prepared = self._prepare(inquiry)
        if isinstance(prepared, str):
            raise ValueError(
                UNAVAILABLE_REASONS.get(prepared, "Learning에 반영할 수 없습니다.")
            )
        case, _ = prepared
        existing = self._promoted_learning(case)
        if existing is not None:
            return existing
        stored, _outcome = self.case_repository.upsert(case)
        case_id = int(stored["id"])
        blocked = HistoricalCaseService.promotion_block_reason(stored)
        if blocked is not None:
            raise ValueError(
                UNAVAILABLE_REASONS.get(blocked, "Learning에 반영할 수 없습니다.")
            )
        # The operator reading the answer is the review decision, recorded the
        # way the bulk promotion records it rather than as a second mechanism.
        if not stored.get("active"):
            self.case_repository.set_learning_enabled(
                case_id, True, actor=str(actor or "관리자")
            )
        return self.cases.promote(case_id, actor=str(actor or "관리자"))

    # -- internals ---------------------------------------------------------

    def _promoted_learning(self, case: dict[str, Any]) -> dict[str, Any] | None:
        """The Learning row this case already produced, if it produced one.

        Keyed on the fingerprint rather than on ``promoted_learning_id``: the
        backfilled corpus is matched even when this inquiry has no historical
        row of its own yet, which is what stops the 742 being duplicated.
        """

        return self.learning.get_by_source_key(
            HistoricalCaseService._digest(
                "HISTORICAL_PROMOTED", case["fingerprint"]
            )
        )

    def _prepare(
        self, inquiry: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | str:
        """The historical case this inquiry's seller answer would become.

        Returns a reason code instead when there is nothing to keep.  Builds
        the candidate exactly as the backfill builds it, from the stored
        payload, so the derived keys are the backfill's keys.
        """

        store_code = inquiry.get("store_code")
        if market_of(store_code) != COUPANG:
            return "NOT_COUPANG"
        if not inquiry.get("source_answered"):
            return "NOT_ANSWERED"
        raw = inquiry.get("raw_json")
        raw = raw if isinstance(raw, dict) else {}
        metadata = inquiry.get("source_metadata_json")
        metadata = metadata if isinstance(metadata, dict) else {}
        account_code = str(
            metadata.get("account_code") or account_of_store(store_code) or ""
        ).strip()
        normalized = self.normalizer.online(raw, account_code=account_code or None)
        if normalized.answer_selection_status == "MULTI_COMMENT_REVIEW":
            return "MULTI_COMMENT_REVIEW"
        if not normalized.seller_answer:
            return "NO_SELLER_ANSWER"
        candidate = dict(normalized.to_work_item())
        # The stored payload is privacy-masked, and a nine-digit Coupang
        # inquiryId inside it reads as a personal number and comes back
        # ``<masked-...>``.  ``prepare_case`` puts the external id into the
        # ``case_key`` digest unmasked, so re-deriving it from the payload
        # would hash a different string than the historical backfill hashed
        # from the live response -- and every one of the 742 would be
        # duplicated.  The inquiry's own columns kept the real value; they are
        # what the digest must see.
        external_id = str(
            inquiry.get("external_inquiry_id")
            or inquiry.get("source_question_id")
            or normalized.external_inquiry_id
            or ""
        )
        candidate.update({
            "external_inquiry_id": external_id,
            "source_question_id": external_id,
            "inquiry_id": external_id,
            "local_inquiry_id": inquiry.get("id"),
            "seller_answer": normalized.seller_answer,
            "source_answered": True,
            "source_updated_at": (
                normalized.answer_created_at or normalized.source_created_at
            ),
            "raw_payload": normalized.raw_payload,
            "historical_metadata": self._metadata(
                inquiry, normalized, account_code, raw,
            ),
        })
        case = self.cases.prepare_case(
            candidate,
            source_reference=(
                f"COUPANG_ONLINE_API:{account_code}:"
                f"{normalized.external_inquiry_id}"
            ),
        )
        return case, candidate

    def _metadata(
        self,
        inquiry: dict[str, Any],
        normalized: Any,
        account_code: str,
        raw: dict[str, Any],
    ) -> dict[str, Any]:
        """Provenance the Learning row carries, and the scope it is kept under.

        ``store_code`` is deliberately absent: promotion drops it for a Coupang
        origin so both seller accounts read one corpus, and the market filter,
        not a store column, is what keeps it away from Naver.  The account and
        the listing ids stay here as provenance instead.
        """

        model = self._confirmed_model(inquiry)
        metadata: dict[str, Any] = {
            # Same shape the historical backfill writes, so a row captured
            # here and a row backfilled from the same reply are one kind.
            "candidate_only": True,
            "market": COUPANG,
            "market_applicability": "COUPANG_ONLY",
            "origin_market": COUPANG,
            "shared_cross_market_learning": False,
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
        if model is not None:
            # The catalogue representative is the scope, because that is what
            # the answer path asks Product Knowledge for; the operator's
            # CONFIRMED value is kept beside it and its mapping row is not
            # touched.  Without a CONFIRMED mapping there is no scope at all --
            # guessing one would file this answer under another product.
            metadata.update({
                "canonical_model": model["model_code"],
                "model_code": model["model_code"],
                "confirmed_canonical_model": model["canonical_model"],
                "model_identity_source": "COUPANG_CONFIRMED_MAPPING",
            })
        return metadata

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
