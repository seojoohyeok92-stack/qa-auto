"""What a Coupang answer records about the identity it was built on.

Found by replaying fourteen real Coupang inquiries through
``generate_for_inquiry``: the model reached Product Knowledge -- nine of them
resolved ``identity_status`` EXACT and retrieved between 6 and 134 verified
facts -- while nothing anywhere said which identity that was.  Of the 542
drafts in the 26.10.12 snapshot that carry product facts, 0 contained
``model_identity_source``, ``mapping_resolution``, ``canonical_model`` or
``blocked_mapping_resolution``.

Phase 2-A wrote all four onto ``request.metadata``, with a comment saying the
trace and the dashboard could then show the mapping state.  ``request`` is a
local that dies with the call, so none of it was ever persisted: the keys were
written and dropped.  A blocked inquiry was worse off still -- it leaves no
draft at all, so the only record was the policy name, without which model was
refused or how far its mapping had got.

Both halves are pinned here, and so is the distinction that made the empty
string worth keeping: 232 of the snapshot's mapping rows were backfilled with
``mapping_status`` CONFIRMED and no ``resolution`` at all, and every one of
the ten unanswered Coupang inquiries maps to one of them.  "No resolution
recorded" and "no mapping" are different states and the draft has to be able
to tell them apart.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.exceptions import AutoAnswerProhibitedError
from repositories.coupang_product_mapping_repository import (
    AUTO_EXACT, MANUAL_REQUIRED,
)
from services.answer_service import AnswerService

COUPANG_STORE = "COUPANG_OJE_NS"
NAVER_STORE = "OJE_PLUS"
ACCOUNT = "OJE_NS"
VENDOR_ITEM = "91454239547"


def inquiry_row(**overrides):
    row = {
        "store_code": COUPANG_STORE,
        "source_type": "COUPANG_ONLINE_INQUIRY",
        "source_question_id": "160959847",
        "raw_json": {"vendorItemId": VENDOR_ITEM},
        "source_metadata_json": {"account_code": ACCOUNT},
        "product_name": "사이니지 BED / 모음전 / 메인코드",
    }
    row.update(overrides)
    return row


class FakeMappingRepository:
    row: dict | None = None

    def __init__(self, database=None) -> None:
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
    import services.answer_service as answer_service

    repository = FakeMappingRepository
    repository.row = None
    monkeypatch.setattr(
        answer_service, "CoupangProductMappingRepository", repository)

    def set_row(*, resolution, confirmed, canonical="LH43BEDHLGFXKR"):
        repository.row = {
            "account_code": ACCOUNT, "vendor_item_id": VENDOR_ITEM,
            "canonical_model": canonical if confirmed else None,
            "resolution": resolution,
            "mapping_status": "CONFIRMED" if confirmed else "NEEDS_REVIEW",
        }

    return set_row


@pytest.fixture
def service():
    built = AnswerService.__new__(AnswerService)
    built.database = None
    built.product_knowledge = SimpleNamespace(catalog_repository=None)
    return built


def request_for(service, row, *, actions=("PACKAGE_CONTENTS",)):
    question = "기본 구성품에 리모컨이 포함되어 있나요?"
    request = SimpleNamespace(
        store_code=row["store_code"], product_name=row["product_name"],
        question=question, metadata={},
    )
    service._attach_market_identity(request, row)
    request.metadata["gpt_understanding"] = {
        "usable": True,
        "questions": [{"text": question, "action": action}
                      for action in actions],
    }
    return request


# --- the identity a draft is built on is recorded on the draft ------------

def _recorded(request) -> dict:
    """What the generation path now copies onto ``result.metadata``.

    The copy itself sits inline in ``generate_for_inquiry`` next to
    ``production_decision_trace``; this applies the same rule to the request
    so the contract is tested without building the whole pipeline.
    """

    if not request.metadata.get("market"):
        return {}
    return {
        key: request.metadata.get(key)
        for key in ("market", "model_identity_source", "model_code",
                    "canonical_model", "mapping_resolution")
    }


def test_a_confirmed_identity_is_recorded_whole(service, mapping) -> None:
    mapping(resolution=AUTO_EXACT, confirmed=True)
    identity = _recorded(request_for(service, inquiry_row()))

    assert identity == {
        "market": "COUPANG",
        "model_identity_source": "COUPANG_CONFIRMED_MAPPING",
        "model_code": "LH43BEDHLGFXKR",
        "canonical_model": "LH43BEDHLGFXKR",
        "mapping_resolution": AUTO_EXACT,
    }


def test_a_backfilled_confirmation_keeps_its_empty_resolution(
    service, mapping
) -> None:
    """The real confirmed population: CONFIRMED with no resolution recorded.

    All ten unanswered Coupang inquiries in the snapshot are this shape, so a
    record that dropped the empty string would describe none of them.
    """

    mapping(resolution=None, confirmed=True)
    identity = _recorded(request_for(service, inquiry_row()))

    assert identity["model_identity_source"] == "COUPANG_CONFIRMED_MAPPING"
    assert identity["canonical_model"] == "LH43BEDHLGFXKR"
    assert identity["mapping_resolution"] == ""
    assert "mapping_resolution" in identity, "the key itself is the record"


def test_an_unconfirmed_mapping_records_how_far_it_got(
    service, mapping
) -> None:
    mapping(resolution=MANUAL_REQUIRED, confirmed=False)
    identity = _recorded(request_for(service, inquiry_row()))

    assert identity["model_identity_source"] is None
    assert identity["canonical_model"] is None
    assert identity["mapping_resolution"] == MANUAL_REQUIRED


def test_a_naver_inquiry_records_nothing(service, mapping) -> None:
    """There is no market identity to record, and none is invented."""

    mapping(resolution=AUTO_EXACT, confirmed=True)
    identity = _recorded(request_for(service, inquiry_row(
        store_code=NAVER_STORE)))
    assert identity == {}


def test_the_generation_path_copies_the_identity_onto_the_result() -> None:
    """The copy exists and reads the request, not something reconstructed."""

    import inspect

    source = inspect.getsource(AnswerService.generate_for_inquiry)
    assert 'result.metadata["market_identity"]' in source
    marker = source.index('result.metadata["market_identity"]')
    nearby = source[marker:marker + 400]
    for key in ("model_identity_source", "canonical_model",
                "mapping_resolution"):
        assert key in nearby
    assert 'request.metadata.get("market")' in source[marker - 300:marker]


# --- a block leaves no draft, so it carries its own diagnosis -------------

def test_the_block_carries_the_mapping_state_on_the_error(
    service, mapping
) -> None:
    """``policy_reason`` alone could not say which model, or how far.

    The handler writes ``policy_details`` into the skipped workflow step, so
    this is the only route by which a blocked inquiry's diagnosis survives.
    """

    mapping(resolution=MANUAL_REQUIRED, confirmed=False)
    request = request_for(service, inquiry_row())

    with pytest.raises(AutoAnswerProhibitedError) as raised:
        service._require_confirmed_identity_for_model_question(request)

    assert raised.value.policy_reason == "COUPANG_MODEL_MAPPING_UNCONFIRMED"
    assert raised.value.policy_details == {
        "blocked_mapping_resolution": MANUAL_REQUIRED,
        "blocked_identity_actions": ["PACKAGE_CONTENTS"],
    }


def test_a_missing_mapping_row_is_reported_as_none_not_blank(
    service, mapping
) -> None:
    request = request_for(service, inquiry_row())

    with pytest.raises(AutoAnswerProhibitedError) as raised:
        service._require_confirmed_identity_for_model_question(request)

    assert raised.value.policy_details["blocked_mapping_resolution"] == "NONE"


def test_the_handler_records_whatever_the_block_carried() -> None:
    """The details reach the workflow step rather than stopping at the raise."""

    import inspect

    source = inspect.getsource(AnswerService.generate_for_inquiry)
    assert 'getattr(error, "policy_details", None)' in source
    marker = source.index('getattr(error, "policy_details", None)')
    assert "policy_blocked" in source[marker - 300:marker]
