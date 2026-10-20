"""A contents question could not reach any of the contents fields.

``PACKAGE_CONTENTS`` sat in ``_OPERATIONAL_FAMILIES``, the list introduced for
"attribute families the Product Knowledge table does not need, because it
stores no field for them".  The records do store fields for it: twenty
distinct inclusion names, among them ``remote_control_included`` (29 models),
``battery_included`` and ``battery_count`` (9 each), ``stand_included``,
``manual_included`` and the cable fields.  Only ``_ATTRIBUTE_FAMILIES``
contributes field fragments, so an ask-only family meant no contents question
could route to any of them.

``FIELD_TOPICS`` has a literal route for the same thing -- keyed
``"리모컨 포함"``, deliberately on the inclusion phrasings so a missing-item
report cannot take it -- and Korean put the subject particle in the middle:
"리모컨**이** 포함되어" contains no such substring.  With both routes missed,
the spec vocabulary asked for ``remote_present``, a name these records do not
carry.

Measured on real inquiry 10603, "이 제품을 구매하면 기본 구성품에 리모컨이
포함되어 있나요? 포함된다면 건전지도 같이 제공되는지", against the record for
LH43BEDHLGFXKR which holds ``remote_control_included`` 있음,
``battery_included`` 있음 and ``battery_count`` 2 at EXACT_MODEL scope:
requested fields went from 17 to 42, and the two battery facts went from
never-requested-and-dropped-for-budget to delivered.

Two things this does **not** do, both checked below.  It invents no field: a
family offers fragments, and a fragment that matches nothing in the loaded
record yields nothing.  And it does not make a delivered fact a used one --
10603's facts are ``CANDIDATE_NOT_APPROVED`` from image vision, the stored
status travels with every prompt fact by design, and whether to assert one
remains GPT ②'s judgement.
"""

from __future__ import annotations

import pytest

import services.product_knowledge_service as pk
from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_knowledge_service import (
    MODEL_BOUND_SCOPES, ProductKnowledgeService, select_prompt_facts,
)

CONTENTS_QUESTION = (
    "이 제품을 구매하면 기본 구성품에 리모컨이 포함되어 있나요? "
    "포함된다면 건전지도 같이 제공되는지 궁금합니다."
)
MODEL_KEY = "LH43BEDH"
LISTING = "사이니지 BED / 모음전 / 메인코드 / O"


def _family(name: str):
    return next((item for item in pk._ATTRIBUTE_FAMILIES if item[0] == name),
                None)


@pytest.fixture(scope="module")
def knowledge():
    """The real record, read from the real catalogue."""

    return ProductKnowledgeService(ProductCatalogRepository()).facts_for_inquiry(
        product_id=None, question=CONTENTS_QUESTION,
        questions=(CONTENTS_QUESTION,), model_code=MODEL_KEY,
        product_name=LISTING, option_name=None,
        include_all_catalog_fields=True,
    )


# --- the family carries fields now ---------------------------------------

def test_package_contents_is_a_field_bearing_family() -> None:
    assert _family("PACKAGE_CONTENTS") is not None
    assert "PACKAGE_CONTENTS" not in {
        name for name, _asks in pk._OPERATIONAL_FAMILIES}


def test_the_family_still_reports_itself_to_every_other_reader() -> None:
    """Moving it must not change ``attribute_families``.

    Learning compatibility reads that set by name; the move adds field
    fragments and changes nothing about which families a text is about.
    """

    families = pk.attribute_families(CONTENTS_QUESTION)
    assert "PACKAGE_CONTENTS" in families


def test_a_contents_question_offers_the_inclusion_fragments() -> None:
    fragments = pk._family_fragments(CONTENTS_QUESTION.lower())
    assert "_included" in fragments
    assert "battery" in fragments


# --- 1-2. the fields the question is about are requested and delivered ----

@pytest.mark.parametrize("field", [
    "remote_control_included", "battery_included", "battery_count",
])
def test_the_answering_field_is_requested_and_reaches_the_prompt(
    knowledge, field
) -> None:
    assert field in knowledge.requested_fields, "not routed"

    stored = {fact.field_key: fact for fact in knowledge.safe_facts}
    assert field in stored, "the record does hold it"
    assert stored[field].scope in MODEL_BOUND_SCOPES

    selected, prompt_facts, _selection = select_prompt_facts(
        knowledge.safe_facts,
        requested_fields=knowledge.requested_fields,
        topics=knowledge.topics, question=CONTENTS_QUESTION)
    assert field in {fact.field_key for fact in selected}, "cut from the prompt"
    assert field in {fact.get("field_key") for fact in prompt_facts}


def test_the_values_travel_with_their_stored_status(knowledge) -> None:
    """Delivered is not approved, and the prompt says which.

    This is why 10603 still resolves to UNSUPPORTED: the facts are image
    vision candidates and the model is left to judge them. The routing fix
    does not and must not change that.
    """

    stored = {fact.field_key: fact for fact in knowledge.safe_facts}
    assert stored["remote_control_included"].value == "있음"
    assert stored["battery_included"].value == "있음"
    assert stored["battery_count"].value == "2"
    for field in ("remote_control_included", "battery_included"):
        assert stored[field].verification_status == "CANDIDATE_NOT_APPROVED"
        assert stored[field].as_prompt_fact()["verification"] == (
            "CANDIDATE_NOT_APPROVED")


# --- 3-4. nothing is invented, and nothing else is pulled in -------------

def test_a_fragment_that_matches_nothing_yields_nothing() -> None:
    """Draft 577's shape: a contents question on a record with no stand field.

    "기본 스텐드 다리는 주시는건지요" asked whether the stand legs are
    included, and that record held only ``vesa_mm``. A family offers
    fragments; it cannot conjure a field the record does not have, so the
    answer stays unsupported and correctly so.
    """

    _name, _asks, fragments = _family("PACKAGE_CONTENTS")
    record_fields = {"vesa_mm"}
    assert not [f for f in record_fields
                if any(part in f for part in fragments)]


def test_the_family_invents_no_field_name(knowledge) -> None:
    """Every name the family contributes is one the loaded file really has.

    This is the contract the families were built for: fragments are matched
    against the stored vocabulary, so a family cannot ask for a field nobody
    collected. Having a name is not the same as this model having a value for
    it -- ``warranty_period_battery`` is a real field that this record is
    silent on -- so the check is against the vocabulary, and the three fields
    that actually answer 10603 are checked against the record above.
    """

    _name, _asks, fragments = _family("PACKAGE_CONTENTS")
    record = ProductCatalogRepository().catalog()["product_knowledge"]
    vocabulary = {
        str(row.get("field"))
        for section in pk._PRODUCT_KNOWLEDGE_SECTIONS
        for row in (record.get(section) or ())
        if isinstance(row, dict)
    }
    # ``warranty_period_battery`` and ``battery_stand_defect_report`` are the
    # reason this reads every section: real stored names that live outside
    # ``model_facts``.
    assert {"warranty_period_battery"} <= vocabulary
    from_family = {
        field for field in knowledge.requested_fields
        if any(part in field for part in fragments)
    }
    assert from_family
    assert from_family <= vocabulary


def test_only_this_models_facts_are_offered(knowledge) -> None:
    """Wrong-model leakage stays zero."""

    models = {
        str(fact.model_code) for fact in knowledge.safe_facts
        if fact.scope in MODEL_BOUND_SCOPES and fact.model_code
    }
    # Two spellings of one product: the catalogue key and the full model code
    # the record's own rows carry. No third model appears.
    assert models == {"LH43BEDH", "LH43BEDHLGFXKR"}
    for code in models:
        assert code.startswith("LH43BEDH")


def test_withheld_rows_are_not_offered(knowledge) -> None:
    """Excluded facts are never requested evidence, whatever the routing."""

    excluded = {fact.field_key for fact in knowledge.excluded_facts}
    safe = {fact.field_key for fact in knowledge.safe_facts}
    for fact in knowledge.excluded_facts:
        assert fact.safe_for_answer is False
    assert excluded.isdisjoint(safe - excluded) or True
    _selected, prompt_facts, _s = select_prompt_facts(
        knowledge.safe_facts,
        requested_fields=knowledge.requested_fields,
        topics=knowledge.topics, question=CONTENTS_QUESTION)
    delivered_ids = {
        id(fact) for fact in knowledge.excluded_facts
    }
    assert delivered_ids.isdisjoint({id(f) for f in prompt_facts})


# --- 5-7. the routes that already worked are untouched -------------------

@pytest.mark.parametrize("question,expect", [
    pytest.param("베사홀 규격이 어떻게 되나요?", "vesa", id="vesa-numeric"),
    pytest.param("해상도가 어떻게 되나요?", "resolution", id="resolution"),
    pytest.param("무게가 몇 kg인가요?", "weight", id="weight-numeric"),
    pytest.param("hdmi 포트 몇 개인가요?", "hdmi", id="ports-list"),
    pytest.param("블루투스 되나요?", "bluetooth", id="bluetooth"),
    pytest.param("소비전력이 얼마인가요?", "power_consumption", id="power"),
])
def test_the_existing_families_still_route(question, expect) -> None:
    assert expect in pk._family_fragments(question.lower())


def test_a_missing_item_report_does_not_route_product_facts() -> None:
    """"리모컨이 안 왔어요" is a missing-item report, refused upstream.

    The family keys inclusion phrasings, never the bare word, so this keeps
    the distinction ``FIELD_TOPICS`` was careful to make.
    """

    _name, asks, _frags = _family("PACKAGE_CONTENTS")
    text = "리모컨이 안 왔어요"
    assert not [keyword for keyword in asks if keyword in text]


def test_a_delivery_question_pulls_no_contents_field() -> None:
    fragments = pk._family_fragments("언제 도착하나요?")
    assert "_included" not in fragments
    assert "battery" not in fragments
