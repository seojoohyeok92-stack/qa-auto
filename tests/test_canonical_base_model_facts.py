"""One model, one set of model-level facts, whatever the code is spelled like.

The catalogue holds several records for one display. ``S32DM501`` and
``LS32DM501EKXKR`` are the same monitor, and so are ``F27T450`` and
``LF27T450FZKXKR`` -- each record carries part of the specification. Every
record was read in isolation, so the answer depended on which spelling the
customer happened to write:

* ``S32DM501`` returned ``display_width_mm 698.4``; ``LS32DM501EKXKR``
  returned nothing for the same question.
* ``F27T450`` returned ``hdmi_port_count_spec 2개`` and
  ``elevation_range_spec 130mm``; ``LF27T450FZKXKR`` returned neither.
* ``S27DM501`` could not reach the ``power_cable_length 1.5m`` its sibling
  record states.

The fix separates two questions that had been one. :meth:`match` still decides
*which record* the customer is writing from, keeping the literal-key
precedence that keeps EDGE 27CH3 and EDGE 27CH3 화이트 apart. The record's own
``model`` field then says which base model it describes, and the records that
describe one base model may fill each other's gaps.

What must not happen is a merge of different products, so these tests pin the
limits as hard as the parity: the resolved record always wins, two records
disagreeing settle nothing, a listing's own choices (colour, name, release
month) never travel, and a package listing never states facts for the model.
"""
from __future__ import annotations

import collections

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository, canonical_model_identity,
)
from services.product_knowledge_service import (
    ProductKnowledgeService, select_prompt_facts,
)

BUNDLE_LISTING = (
    "삼성 M5 M50D 80cm 캠핑TV 스마트 IPTV 화이트에디션 접이식 휴대용 패키지"
)


@pytest.fixture(scope="module")
def repository() -> ProductCatalogRepository:
    return ProductCatalogRepository()


@pytest.fixture(scope="module")
def service(repository) -> ProductKnowledgeService:
    return ProductKnowledgeService(repository)


def ask(service, model, question):
    return service.facts_for_inquiry(
        product_id=None, questions=[question], question=question,
        model_code=model, product_name=f"삼성 {model}", option_name="",
        include_all_catalog_fields=False,
    )


def values(result, field_key):
    return [str(item.value) for item in result.safe_facts
            if item.field_key == field_key]


# -- the reported failures, as parity -----------------------------------------


@pytest.mark.parametrize("model", ["S32DM501", "LS32DM501EKXKR", "32DM501"])
def test_every_spelling_of_32dm501_answers_the_width(service, model):
    result = ask(service, model, "디스플레이 가로 길이가 얼마인가요?")
    assert result.resolved_pk_targets == ("32DM501",)
    assert values(result, "display_width_mm") == ["698.4"]


@pytest.mark.parametrize("model", ["F27T450", "LF27T450FZKXKR"])
def test_every_spelling_of_f27t450_answers_the_ports(service, model):
    result = ask(service, model, "HDMI 단자 몇 개인가요?")
    assert result.resolved_pk_targets == ("F27T450",)
    assert values(result, "hdmi_port_count_spec") == ["2개"]


@pytest.mark.parametrize("model", ["F27T450", "LF27T450FZKXKR"])
def test_every_spelling_of_f27t450_answers_the_height_adjustment(
    service, model,
):
    result = ask(service, model, "높이 조절 되나요?")
    assert values(result, "elevation_range_spec") == ["130mm"]


def test_the_cable_length_its_sibling_states_is_reachable(service):
    result = ask(service, "S27DM501", "전원 케이블 길이가 얼마인가요?")
    assert values(result, "power_cable_length") == ["1.5m"]


@pytest.mark.parametrize("model, question, field, value", [
    ("LS32DM501EKXKR", "디스플레이 가로 길이가 얼마인가요?",
     "display_width_mm", "698.4"),
    ("LF27T450FZKXKR", "HDMI 단자 몇 개인가요?", "hdmi_port_count_spec", "2개"),
])
def test_a_pooled_fact_reaches_the_prompt(service, model, question, field,
                                          value):
    result = ask(service, model, question)
    kept, _dropped, _selection = select_prompt_facts(
        tuple(result.safe_facts), requested_fields=result.requested_fields,
        topics=result.topics, question=question,
    )
    assert value in [str(item.value) for item in kept
                     if item.field_key == field]


def test_a_pooled_fact_says_which_record_stated_it(service):
    """Attributed to the resolved model, with the donor in the provenance."""

    result = ask(service, "LS32DM501EKXKR", "디스플레이 가로 길이가 얼마인가요?")
    fact = next(item for item in result.safe_facts
                if item.field_key == "display_width_mm")
    assert fact.model_code == "LS32DM501EKXKR"
    assert fact.provenance and fact.provenance[0] == {
        "source": "CANONICAL_BASE_MODEL_RECORD",
        "base_model_identity": "32DM501",
        "stated_by_record": "S32DM501",
        "resolved_record": "LS32DM501EKXKR",
    }


# -- the limits ---------------------------------------------------------------


def test_the_resolved_record_is_never_overwritten(service):
    """F27T450 says 5ms, its sibling says 5ms(GTG). Each keeps its own."""

    assert values(ask(service, "F27T450", "응답속도 얼마인가요?"),
                  "response_time_ms") == ["5ms"]
    assert values(ask(service, "LF27T450FZKXKR", "응답속도 얼마인가요?"),
                  "response_time_ms") == ["5ms(GTG)"]


def test_two_donors_disagreeing_settle_nothing(tmp_path):
    """The rule, on a catalogue written to exercise it.

    Today's catalogue has no such disagreement among the fields that may be
    pooled -- the whole-corpus audit below reports zero -- so the behaviour is
    pinned on a three-record catalogue of one base model instead: two donors
    state a different brightness, one states a response time, and the record
    being answered states neither.
    """

    import json

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "MODEL_CATALOG": {
            "S32XX900": {"model": "S32XX900", "spec": "80cm(32인치) / 240Hz"},
            "LS32XX900AKXKR": {
                "model": "S32XX900",
                "spec": "80cm(32인치) / 240Hz / 350cd / 1ms(GTG)",
            },
            "G9 S32XX900": {
                "model": "S32XX900", "spec": "80cm(32인치) / 240Hz / 400cd",
            },
        },
        "MODEL_ALIASES": {},
    }, ensure_ascii=False), encoding="utf-8")
    service = ProductKnowledgeService(
        catalog_repository=ProductCatalogRepository(path))

    pooled, conflicts = service._canonical_base_model_facts(
        product_id="p", model_key="S32XX900", base_identity="32XX900",
        fields=("brightness_typical_cd_m2", "response_time_ms"),
        already_stated=(),
    )
    # The one both donors do not contradict travels; the contested one does
    # not, and says why.
    assert [item.field_key for item in pooled] == ["response_time_ms"]
    assert [(item.field_key, item.exclusion_reason, str(item.value))
            for item in conflicts] == [
        ("brightness_typical_cd_m2", "CANONICAL_BASE_MODEL_CONFLICT",
         "350cd/400cd"),
    ]
    result = service.facts_for_inquiry(
        product_id=None, question="밝기 몇 니트인가요?", model_code="S32XX900",
        product_name="삼성 S32XX900", option_name="",
    )
    assert values(result, "brightness_typical_cd_m2") == []
    assert [item.exclusion_reason for item in result.excluded_facts
            if item.field_key == "brightness_typical_cd_m2"] == [
        "CANONICAL_BASE_MODEL_CONFLICT"]


@pytest.mark.parametrize("field", [
    "color", "color_options", "front_color", "rear_color", "stand_color",
    "stand_color_spec", "accessory_color", "model_name", "release_ym_spec",
    "brand",
])
def test_a_listing_scoped_field_never_travels(service, field):
    assert field in ProductKnowledgeService._LISTING_SCOPED_FIELDS
    assert field not in ProductKnowledgeService._POOLABLE_FIELDS


@pytest.mark.parametrize("field", [
    "hdmi_present", "usb_present", "bluetooth_present", "wifi_present",
    "ethernet_present", "rf_terminal",
])
def test_a_connection_the_model_has_does_travel(service, field):
    """Whether the panel has a USB port is a fact about the model.

    These were excluded while a presence value was the whole spec string --
    importing one would have quoted another record's entire specification.
    The value is the matched token now ("HDMI", "USB", "Wi-Fi"), so the
    exclusion has no reason left, and F27T450 and LF27T450FZKXKR -- one model
    -- answered "USB 포트 있나요?" differently until this changed.
    """

    assert field in ProductKnowledgeService._POOLABLE_FIELDS
    assert field not in ProductKnowledgeService._LISTING_SCOPED_FIELDS


@pytest.mark.parametrize("model", ["F27T450", "LF27T450FZKXKR"])
@pytest.mark.parametrize("question", ["USB 포트 있나요?", "USB 단자 있나요?"])
def test_both_spellings_answer_the_usb_question_the_same(
    service, model, question,
):
    result = ask(service, model, question)
    assert result.resolved_pk_targets == ("F27T450",)
    assert values(result, "usb_present") == ["USB"]


def test_a_package_listing_never_states_facts_for_the_model(repository):
    """It is the model plus accessories; its spec can describe the extras."""

    assert BUNDLE_LISTING in repository.catalog()["catalog"]
    assert repository.base_model_identity(BUNDLE_LISTING) == "32DM501"
    donors = [key for key, _record in
              repository.base_model_records("S32DM501")]
    assert BUNDLE_LISTING not in donors
    # It may still be answered from the model's own records.
    assert "S32DM501" in [
        key for key, _record in repository.base_model_records(BUNDLE_LISTING)
    ]


def test_a_record_with_no_canonical_identity_is_in_no_group(repository):
    """Which is what keeps the EDGE and XL+ records to themselves."""

    for key in ("EDGE 27CH3", "EDGE 27CH3 화이트", "XL2586X+",
                "EDGE 24 커브드144 게이밍 블랙"):
        assert repository.base_model_identity(key) is None
        assert repository.base_model_records(key) == ()


@pytest.mark.parametrize("model, question, field, value", [
    ("EDGE 27CH3", "주사율 몇 Hz인가요?", "refresh_rate", "165Hz"),
    ("EDGE 27CH3 화이트", "주사율 몇 Hz인가요?", "refresh_rate", "144Hz"),
    ("EDGE 27CH3", "무게가 몇 kg인가요?", "weight_catalog", "5.56kg"),
    ("EDGE 27CH3 화이트", "무게가 몇 kg인가요?", "weight_catalog", "7.2kg"),
])
def test_the_edge_records_keep_their_own_specification(service, model,
                                                       question, field, value):
    assert values(ask(service, model, question), field) == [value]


@pytest.mark.parametrize("left, right", [
    ("DM500", "DM501"), ("43BED", "43BEH"), ("S32FM701", "S32FM703"),
    ("S32DM501", "S27DM501"), ("S32DM501", "S32DM503"),
])
def test_different_models_are_still_different(repository, left, right):
    """Size, generation and a differing identifier all stay in the identity.

    ``S49CG954``/``S49CG954E`` is deliberately absent: those two are one
    model, settled from the evidence recorded below, and are the only pair
    this work merged.
    """

    aliases = repository.catalog().get("aliases")
    assert (canonical_model_identity(left, aliases=aliases)
            != canonical_model_identity(right, aliases=aliases))


@pytest.mark.parametrize("left, right", [
    ("S32FM701", "S32FM703"), ("S32DM501", "S27DM501"),
    ("S32DM501", "S32DM503"), ("S49CG954", "S49DG930"),
])
def test_two_models_never_share_a_base_identity(repository, left, right):
    """Two catalogue keys that are two products keep two base identities."""

    assert (repository.base_model_identity(left)
            != repository.base_model_identity(right))
    assert right not in [key for key, _record
                         in repository.base_model_records(left)]


# -- whole corpus -------------------------------------------------------------


def test_every_group_is_grounded_in_the_records_own_model_field(repository):
    """Measured: 1,598 records, 1,415 base identities, 13 groups of 2 or more.

    The grouping evidence is never a guessed suffix: each member's own
    ``model`` field canonicalises to the group's identity, or the member's key
    does when the record states no model.
    """

    loaded = repository.catalog()
    catalog, aliases = loaded["catalog"], loaded.get("aliases")
    index = loaded["base_models"]
    groups = {identity: keys
              for identity, keys in index["keys_of_identity"].items()
              if len(keys) > 1}
    assert len(catalog) == 1598
    assert len(index["identity_of_key"]) == 1435
    assert len(index["keys_of_identity"]) == 1415
    assert len(groups) == 13
    assert sum(len(keys) for keys in groups.values()) == 33
    for identity, keys in groups.items():
        for key in keys:
            record = catalog[key]
            stated = str(record.get("model") or "")
            assert identity in {
                canonical_model_identity(stated, aliases=aliases),
                canonical_model_identity(key, aliases=aliases),
            }, (identity, key, stated)


def test_no_group_member_answers_with_another_models_fact(repository, service):
    """Pooling fills gaps; it never attributes a fact to another model."""

    index = repository.catalog()["base_models"]
    groups = [keys for keys in index["keys_of_identity"].values()
              if len(keys) > 1]
    question = "HDMI 단자 몇 개인가요?"
    for keys in groups:
        for key in keys:
            record = repository.catalog()["catalog"][key]
            model_code = str(record.get("model") or key)
            resolved = repository.match(
                product_name=f"삼성 {model_code}", option_name="",
                model_code=model_code,
            )
            if not resolved.model_key:
                continue
            result = ask(service, model_code, question)
            foreign = {
                str(item.model_code) for item in result.safe_facts
                if item.scope == "MODEL_CATALOG"
                and str(item.model_code) != str(resolved.model_key)
            }
            assert not foreign, (key, foreign)


def test_pooling_touches_only_the_models_that_have_a_sibling(repository):
    """Every other record in the catalogue is read exactly as before."""

    index = repository.catalog()["base_models"]
    with_siblings = {
        key for keys in index["keys_of_identity"].values() if len(keys) > 1
        for key in keys
    }
    sampled = collections.Counter()
    for key in repository.catalog()["catalog"]:
        donors = repository.base_model_records(key)
        sampled["with_donors" if donors else "alone"] += 1
        if donors:
            assert key in with_siblings, key
    assert sampled["with_donors"] <= len(with_siblings)

# -- S49CG954: one model, five spellings --------------------------------------
#
# The catalogue holds three records for the Odyssey OLED G9 -- ``S49CG954``,
# ``G9 S49CG954E`` and ``G95C S49CG954E`` -- and the identity of the trailing
# E was unsettled: ``LS49CG954EKXKR`` and ``S49CG954`` canonicalised to
# ``49CG954`` while ``S49CG954E`` canonicalised to ``49CG954E``, so the same
# monitor had two base models and the G9/G95C listings answered with neither.
#
# The evidence that settles it is internal, not a suffix rule:
#
# * one API product id, 9701738909, carries collected Product Knowledge under
#   *both* ``LS49CG954EKXKR`` (146 rows) and ``S49CG954E`` (11 rows);
# * the catalogue's alias table already recorded
#   ``LS49CG954EKXKR -> S49CG954``;
# * the records agree on every material measurement -- 240Hz, 2,500:1,
#   1ms(GTG), 123.8cm(49인치), DQHD 5120x1440 -- and the one apparent
#   difference (450cd against 1,000cd) is the typical and the HDR peak of one
#   panel, which 2-a already settled from the same API row.
#
# So one alias was added, ``S49CG954E -> S49CG954``, and nothing else: no
# suffix-stripping rule, and no other model touched.

S49_SPELLINGS = ("S49CG954", "S49CG954E", "LS49CG954EKXKR",
                 "G9 S49CG954E", "G95C S49CG954E")
S49_EXPECTED = (
    ("밝기 몇 니트인가요?", "brightness_typical", "450"),
    ("밝기 몇 니트인가요?", "brightness_peak", "1,000cd"),
    ("스탠드 포함 무게 알려주세요", "weight_with_stand", "15.6"),
    ("스탠드 제외 무게 알려주세요", "weight_without_stand", "10.6"),
    ("HDMI 단자 몇 개인가요?", "hdmi_port_count_spec", "2개"),
    ("프리싱크 지원하나요?", "freesync_spec", "FreeSync Premium Pro"),
)


@pytest.mark.parametrize("model", S49_SPELLINGS)
@pytest.mark.parametrize("question, field, value", S49_EXPECTED)
def test_every_s49_spelling_gives_the_same_model_level_answer(
    service, model, question, field, value,
):
    result = ask(service, model, question)
    assert result.resolved_pk_targets == ("49CG954",)
    assert value in values(result, field), {
        "model": model, "field": field,
        "found": values(result, field),
    }


def test_the_s49_alias_is_the_only_one_added(repository):
    aliases = repository.catalog().get("aliases") or {}
    assert aliases.get("S49CG954E") == "S49CG954"
    assert aliases.get("LS49CG954EKXKR") == "S49CG954"
    # Nothing else in the catalogue gained an alias for this work.
    assert len(aliases) == 74


def test_the_typical_and_the_peak_stay_two_fields(service):
    for model in S49_SPELLINGS:
        result = ask(service, model, "밝기 몇 니트인가요?")
        assert values(result, "brightness_typical") == ["450"]
        assert values(result, "brightness_peak") == ["1,000cd"]


# -- an ambiguous listing whose candidates are one model ----------------------


AMBIGUOUS_TITLES = (
    "삼성 오디세이 OLED G9 S49CG954E 123.8cm(49인치)",
    "삼성 오디세이 OLED G95C S49CG954E",
)


@pytest.mark.parametrize("title", AMBIGUOUS_TITLES)
@pytest.mark.parametrize("question, field, value", S49_EXPECTED)
def test_an_ambiguous_listing_still_answers_about_the_model(
    service, title, question, field, value,
):
    """The listing is undecided; the model is not."""

    result = service.facts_for_inquiry(
        product_id=None, questions=[question], question=question,
        model_code=None, product_name=title, option_name="",
    )
    assert result.identity_status == "AMBIGUOUS_SAME_BASE_MODEL"
    assert result.resolved_pk_targets == ("49CG954",)
    assert value in values(result, field)
    assert result.candidate_models


@pytest.mark.parametrize("title", AMBIGUOUS_TITLES)
def test_an_ambiguous_listing_states_no_listing_scoped_fact(service, title):
    """Which listing it is stays unresolved, so colour stays unanswered."""

    result = service.facts_for_inquiry(
        product_id=None, questions=["제품 색상이 어떻게 되나요?"],
        question="제품 색상이 어떻게 되나요?", model_code=None,
        product_name=title, option_name="",
    )
    for field in ("color", "model_name", "release_ym_spec"):
        assert values(result, field) == []


@pytest.mark.parametrize("title", [
    "삼성 KU40T5510AFXKR 모니터",
    "삼성 F32TU870VK 모니터",
])
def test_candidates_that_are_not_one_model_still_fail_closed(service, title):
    result = service.facts_for_inquiry(
        product_id=None, questions=["HDMI 단자 몇 개인가요?"],
        question="HDMI 단자 몇 개인가요?", model_code=None,
        product_name=title, option_name="",
    )
    assert result.identity_status == "AMBIGUOUS"
    assert result.safe_facts == ()
    assert result.candidate_models


def test_a_single_candidate_is_not_treated_as_a_group(service, repository):
    """Two candidates minimum: one candidate is the existing match path."""

    assert service._one_base_model_for_candidates(
        repository.match(product_name="삼성 S49CG954", option_name="",
                         model_code="S49CG954")) is None
