"""Phase 2-A: a Coupang inquiry reaches AnswerService through its mapping.

Two things are under test, and they are the two that decide whether an
answer can be wrong rather than merely absent.

**Identity.** A Coupang listing title is not an identity. Before this, a
listing with no confirmed mapping still produced a model code -- pulled out
of the Coupang title and handed to the Naver catalogue -- so an unconfirmed
guess could put another product's specification in the reply. Outside Naver
the confirmed mapping is now the only identity, and a question that turns on
which model this is gets no draft at all until the mapping is confirmed.
A question that does not turn on the model is answered as before: gating
those would withhold answers the mapping has no bearing on.

**Posting.** Coupang must not have a write path. That is asserted against
the production policy itself, not against a test double, because a double
proves only what the test arranged.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from answer.exceptions import AutoAnswerProhibitedError
from repositories.coupang_product_mapping_repository import (
    AUTO_ALIAS, AUTO_EXACT, MANUAL_CONFIRMED, MANUAL_REQUIRED, UNRESOLVED,
)
from services.answer_service import (
    AnswerService, coupang_confirmed_model, coupang_mapping_resolution,
)
from services.market_policy import (
    AUTOMATIC_POST_MARKETS, COUPANG, MANUAL_POST_MARKETS, NAVER,
    is_store_automatic_post_enabled, is_store_dps_enabled,
    is_store_manual_post_enabled, is_store_answer_generation_enabled,
)

COUPANG_STORE = "COUPANG_OJE_NS"
NAVER_STORE = "NAVER_OJE"
ACCOUNT = "OJE_NS"
VENDOR_ITEM = "88112233"


def inquiry_row(**overrides):
    row = {
        "store_code": COUPANG_STORE,
        "source_type": "COUPANG_INQUIRY",
        "source_question_id": "160959847",
        "raw_json": {"vendorItemId": VENDOR_ITEM, "sellerProductId": "77001"},
        "source_metadata_json": {"account_code": ACCOUNT},
        "product_name": "삼성전자 32인치 모니터 S32DM501 커브드 FHD",
    }
    row.update(overrides)
    return row


class FakeMappingRepository:
    """Stands in for the mapping table, one row deep."""

    row: dict | None = None

    def __init__(self, database=None) -> None:  # noqa: D107 - shape only
        self.database = database

    def get(self, *, account_code, vendor_item_id):
        if self.row is None:
            return None
        if (str(account_code) != ACCOUNT
                or str(vendor_item_id) != VENDOR_ITEM):
            return None
        return dict(self.row)

    def get_confirmed(self, *, account_code, vendor_item_id):
        row = self.get(account_code=account_code, vendor_item_id=vendor_item_id)
        return row if row and row.get("mapping_status") == "CONFIRMED" else None


@pytest.fixture
def mapping(monkeypatch):
    """Point both identity helpers at one in-memory mapping row."""

    import services.answer_service as answer_service

    repository = FakeMappingRepository
    repository.row = None
    monkeypatch.setattr(
        answer_service, "CoupangProductMappingRepository", repository)

    def set_row(*, resolution, confirmed, canonical="S32DM501",
                evidence_class=None):
        repository.row = {
            "account_code": ACCOUNT, "vendor_item_id": VENDOR_ITEM,
            "canonical_model": canonical if confirmed else None,
            "resolution": resolution,
            "model_evidence_class": evidence_class,
            "mapping_status": "CONFIRMED" if confirmed else "NEEDS_REVIEW",
        }

    set_row.clear = lambda: setattr(repository, "row", None)
    return set_row


def request_for(service, row, *, question="이 모니터 해상도가 어떻게 되나요?",
                actions=("PRODUCT_SPEC",)):
    """An AnswerRequest as production builds it, understanding included.

    ``actions`` are GPT ①'s per-atom labels, carried on
    ``gpt_understanding`` exactly as ``_attach_semantic_routing`` leaves
    them.  The gate reads these; it used to read Product Knowledge's
    ``inquiry_target_models``, which on an unconfirmed mapping is always
    empty, so the fixtures that injected one were testing a condition real
    data cannot produce.
    """

    request = SimpleNamespace(
        store_code=row["store_code"], product_name=row["product_name"],
        question=question, metadata={},
    )
    service._attach_market_identity(request, row)
    request.metadata["gpt_understanding"] = {
        "usable": True,
        "questions": [
            {"text": question, "action": action, "requested_information": ""}
            for action in actions
        ],
    }
    return request


#: What Product Knowledge really returns for an unconfirmed mapping: no
#: target models at all.  Passed so the gate is seen not to use it.
NO_PK_TARGETS = SimpleNamespace(inquiry_target_models=())


@pytest.fixture
def service():
    """AnswerService with only what the identity path touches.

    The identity helpers reach the mapping repository and, optionally, the
    catalogue; nothing else on the service is involved, so building the whole
    thing would test the fixture rather than the gate.
    """

    built = AnswerService.__new__(AnswerService)
    built.database = None
    built.product_knowledge = SimpleNamespace(catalog_repository=None)
    return built


# --- 1-3. a confirmed mapping is the canonical identity -------------------

@pytest.mark.parametrize(
    "resolution", [AUTO_EXACT, AUTO_ALIAS, MANUAL_CONFIRMED])
def test_a_confirmed_mapping_becomes_the_lookup_identity(
    service, mapping, resolution
) -> None:
    """AUTO_EXACT, AUTO_ALIAS and MANUAL_CONFIRMED all name the model."""

    mapping(resolution=resolution, confirmed=True)
    request = request_for(service, inquiry_row())

    assert request.metadata["canonical_model"] == "S32DM501"
    assert request.metadata["model_code"] == "S32DM501"
    assert request.metadata["model_identity_source"] == (
        "COUPANG_CONFIRMED_MAPPING")
    assert request.metadata["mapping_resolution"] == resolution


# --- 6-7. an unconfirmed mapping answers no model-specific question -------

@pytest.mark.parametrize("resolution,evidence", [
    (MANUAL_REQUIRED, None),
    (UNRESOLVED, "NO_MODEL_EVIDENCE"),
    (MANUAL_REQUIRED, "AMBIGUOUS"),
])
def test_a_model_question_is_refused_until_the_mapping_is_confirmed(
    service, mapping, resolution, evidence
) -> None:
    mapping(resolution=resolution, confirmed=False, evidence_class=evidence)
    request = request_for(service, inquiry_row())

    with pytest.raises(AutoAnswerProhibitedError) as raised:
        service._require_confirmed_identity_for_model_question(
            request, NO_PK_TARGETS)

    assert raised.value.policy_reason == "COUPANG_MODEL_MAPPING_UNCONFIRMED"
    # The refusal says where the mapping stopped, so the block is legible
    # rather than a bare policy name.
    assert request.metadata["blocked_mapping_resolution"] == resolution
    assert request.metadata["blocked_identity_actions"] == ["PRODUCT_SPEC"]


def test_a_missing_mapping_row_is_refused_the_same_way(
    service, mapping
) -> None:
    """No row at all is the same risk as an unfinished one."""

    request = request_for(service, inquiry_row())
    assert request.metadata.get("model_identity_source") is None

    with pytest.raises(AutoAnswerProhibitedError):
        service._require_confirmed_identity_for_model_question(
            request, NO_PK_TARGETS)
    assert request.metadata["blocked_mapping_resolution"] == "NONE"


# --- 9. a question that does not turn on the model is not gated -----------

def test_a_question_without_a_model_is_still_answered(
    service, mapping
) -> None:
    """Seller policy and general questions do not need the mapping."""

    mapping(resolution=MANUAL_REQUIRED, confirmed=False)
    request = request_for(
        service, inquiry_row(), question="배송은 얼마나 걸리나요?",
        actions=("DELIVERY_POLICY",))

    # The question asks for no product property, so nothing is withheld.
    service._require_confirmed_identity_for_model_question(
        request, NO_PK_TARGETS)


def test_a_confirmed_mapping_is_never_gated(service, mapping) -> None:
    mapping(resolution=AUTO_EXACT, confirmed=True)
    request = request_for(service, inquiry_row())

    service._require_confirmed_identity_for_model_question(
        request, NO_PK_TARGETS)


# --- 8. the listing title is never an identity ----------------------------

def test_the_listing_title_is_not_used_as_a_lookup_key(
    service, mapping
) -> None:
    """The Coupang title carries S32DM501; it must not become the key.

    The source is read rather than executed because the fallback lives in
    one expression inside the generation path: what matters is that the
    branch exists and that Naver keeps its own behaviour.
    """

    mapping(resolution=UNRESOLVED, confirmed=False)
    request = request_for(service, inquiry_row())
    assert "S32DM501" in request.product_name
    assert not request.metadata.get("model_code")

    whole = inspect.getsource(type(service))
    marker = "extract_model_code(request.product_name)"
    pieces = whole.split(marker)
    assert len(pieces) > 1, "the Naver fallback is gone entirely"
    # The fallback still exists -- Naver depends on it -- but every use of it
    # now sits behind the market check.
    for before in pieces[:-1]:
        assert "_non_naver_market(request.store_code) is not None" in \
            before[-400:], "title fallback reachable without a market check"


# --- 11. Coupang never reaches DPS ----------------------------------------

def test_coupang_has_no_dps(mapping) -> None:
    assert is_store_dps_enabled(COUPANG_STORE) is False
    assert is_store_dps_enabled(NAVER_STORE) is True


# --- 13-15. Coupang has no write path, Naver keeps its own ----------------

def test_only_a_person_may_register_a_coupang_answer() -> None:
    """Asserted against the policy itself, not against a double.

    Phase 3-B opened the manual permission on an explicit decision. The
    unattended one is a separate decision and was not taken: the queue must
    not register replies nobody looked at.
    """

    assert COUPANG in MANUAL_POST_MARKETS
    assert COUPANG not in AUTOMATIC_POST_MARKETS
    assert is_store_manual_post_enabled(COUPANG_STORE) is True
    assert is_store_automatic_post_enabled(COUPANG_STORE) is False


def test_coupang_registration_is_still_not_unattended() -> None:
    """The queue's own scope, which this decision did not widen."""

    from repositories.auto_post_repository import AutoPostRepository  # noqa: F401
    from services.market_policy import post_enabled_store_codes

    assert post_enabled_store_codes(
        [NAVER_STORE, COUPANG_STORE]) == [NAVER_STORE]


def test_naver_posting_is_unchanged() -> None:
    assert NAVER in MANUAL_POST_MARKETS
    assert NAVER in AUTOMATIC_POST_MARKETS
    assert is_store_manual_post_enabled(NAVER_STORE) is True
    assert is_store_automatic_post_enabled(NAVER_STORE) is True


def test_coupang_is_still_collected_and_answered() -> None:
    """Closing the write path must not close generation or display."""

    assert is_store_answer_generation_enabled(COUPANG_STORE) is True


def test_the_manual_post_panel_still_refuses_before_any_network_call(
    monkeypatch,
) -> None:
    """The permission is open; every other precondition still has to hold.

    This inquiry has no draft, so the resolution refuses on local state --
    and refuses before a client is built, which is what keeps a panel from
    sending anything while it works out whether it may.
    """

    from services.coupang_post_service import CoupangPostService

    service = CoupangPostService.__new__(CoupangPostService)
    readable = inquiry_row(post_status=None, source_answered=False)
    readable["raw_json"] = {
        "inquiryId": "160959847", "vendorItemId": VENDOR_ITEM,
        "commentDtoList": [],
    }
    service.inquiries = SimpleNamespace(get=lambda _id: readable)
    service.answers = SimpleNamespace(
        active_for_inquiry=lambda _id: None,
        latest_for_inquiry=lambda _id: None)

    blocked = service._resolve(1, retry_requested=False)
    assert isinstance(blocked, tuple), "preflight let a Coupang post through"
    assert blocked[0] == "LOCAL_STATE_MISSING"


def test_an_unreadable_payload_refuses_rather_than_raising() -> None:
    """25 Coupang inquiries in the snapshot carry an empty ``raw_json``.

    Whether Coupang already holds a reply cannot be read from those, and not
    being able to check must not read as there being nothing to check.
    """

    from services.coupang_post_service import CoupangPostService

    service = CoupangPostService.__new__(CoupangPostService)
    service.inquiries = SimpleNamespace(
        get=lambda _id: inquiry_row(
            post_status=None, source_answered=False, raw_json={}))
    service.answers = SimpleNamespace(
        active_for_inquiry=lambda _id: None,
        latest_for_inquiry=lambda _id: None)

    blocked = service._resolve(1, retry_requested=False)
    assert isinstance(blocked, tuple)
    assert blocked[0] == "SOURCE_PAYLOAD_UNREADABLE"


# --- 10/12. identity helpers keep other products out ----------------------

def test_a_mapping_for_another_option_is_not_borrowed(
    service, mapping
) -> None:
    """The row is keyed by account + vendorItemId and nothing else."""

    mapping(resolution=AUTO_EXACT, confirmed=True)
    other = inquiry_row(raw_json={"vendorItemId": "99999999"})

    assert coupang_confirmed_model(None, other) is None
    assert coupang_mapping_resolution(None, other) == ""


def test_a_naver_inquiry_is_left_alone(service, mapping) -> None:
    mapping(resolution=AUTO_EXACT, confirmed=True)
    naver = inquiry_row(store_code=NAVER_STORE)

    assert coupang_confirmed_model(None, naver) is None
    assert coupang_mapping_resolution(None, naver) == ""
    request = request_for(service, naver)
    assert "market" not in request.metadata
