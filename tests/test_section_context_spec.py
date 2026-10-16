"""A bracketed heading is part of the statement, not decoration.

The catalogue writes a third of each positional spec under a heading:

    ... / 1ms(GTG) / 1,000cd / [색상영역] DCI-P3: 92% / [게임특화] FreeSync /
    FreeSync Premium / [단자정보] HDMI 2.1 / DP 1.4 / PD충전: 최대65W

Read without the heading, "92%" names nothing and "FreeSync Premium" is a
word. Read with it, they are a colour gamut and a certification tier. Measured
over the catalogue production resolves (1,575 models): 10,362 tokens sit under
a heading in 7 sections, and before this nothing read any of them -- the
labelled parser needs a top-level ``label: value`` and the positional reader
needs a self-describing shape.

These tests pin the parts that are easy to get wrong later: the section decides
the reading, a tier ladder is not a conflict, a list of apps is not a single
value, and everything whose subject or condition the section does not state
stays withheld.
"""
from __future__ import annotations

import collections

import pytest

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_knowledge_service import (
    ProductKnowledgeService, fields_for_question, select_prompt_facts,
)
from services.spec_attributes import (
    SECTION_FIELD_KEYS, classify_section_spec, facts_from_section_spec,
    sectioned_tokens, unmapped_section_tokens, withheld_section_tokens,
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
    return [str(i.value) for i in result.safe_facts
            if i.field_key == field_key]


# -- the heading is read -----------------------------------------------------


def test_the_section_governs_the_tokens_after_it():
    spec = ("240Hz / [색상영역] DCI-P3: 92% / sRGB: 99% / "
            "[게임특화] FreeSync / [단자정보] HDMI 2.1")
    assert sectioned_tokens(spec) == [
        ("", "240Hz"),
        ("색상영역", "DCI-P3: 92%"),
        ("색상영역", "sRGB: 99%"),
        ("게임특화", "FreeSync"),
        ("단자정보", "HDMI 2.1"),
    ]


def test_two_headings_in_one_token_are_both_read():
    """C22F392 writes an empty 게임특화 section before 단자정보."""

    spec = "FreeSync / [게임특화] [단자정보] HDMI / D-SUB"
    assert sectioned_tokens(spec) == [
        ("", "FreeSync"), ("단자정보", "HDMI"), ("단자정보", "D-SUB"),
    ]


def test_a_bracketed_list_is_not_split_into_fragments():
    """"웨이브" on its own would assert an app list the spec never made."""

    spec = "[단자정보] OTT(넷플릭스, 디즈니+, 웨이브, 왓챠 등등) / HDMI 2.0"
    assert sectioned_tokens(spec) == [
        ("단자정보", "OTT(넷플릭스, 디즈니+, 웨이브, 왓챠 등등)"),
        ("단자정보", "HDMI 2.0"),
    ]
    facts = facts_from_section_spec(spec)
    assert facts["ott_apps_spec"][0] == "넷플릭스, 디즈니+, 웨이브, 왓챠 등등"


def test_the_same_percentage_means_a_different_gamut_per_label():
    facts = facts_from_section_spec(
        "[색상영역] DCI-P3: 92% / sRGB: 92% / NTSC: 92% / Adobe RGB: 92%")
    assert {field: value for field, (value, _u, _s) in facts.items()} == {
        "color_gamut_dci_p3": "92%", "color_gamut_srgb": "92%",
        "color_gamut_ntsc": "92%", "color_gamut_adobe_rgb": "92%",
    }


def test_a_percentage_without_a_section_is_not_a_gamut():
    assert facts_from_section_spec("240Hz / 92% / VA") == {}


def test_a_section_this_module_does_not_know_supplies_nothing():
    section, token = unmapped_section_tokens("[신규섹션] 무언가: 10")[0]
    assert (section, token) == ("신규섹션", "무언가: 10")
    assert facts_from_section_spec("[신규섹션] 무언가: 10") == {}


def test_the_heading_travels_with_the_value_as_provenance():
    facts = facts_from_section_spec("[색상영역] DCI-P3: 95%")
    _value, _unit, source = facts["color_gamut_dci_p3"]
    assert source == "[색상영역] DCI-P3: 95%"


# -- conditions, subjects and counts stay apart ------------------------------


def test_a_tier_ladder_is_resolved_to_its_most_specific_tier():
    """211 models state FreeSync with its predecessors; that is one tier."""

    facts = facts_from_section_spec(
        "[게임특화] FreeSync / FreeSync Premium / FreeSync Premium Pro")
    assert facts["freesync_spec"][0] == "FreeSync Premium Pro"


def test_tiers_off_the_ladder_are_withheld_rather_than_ranked():
    spec = "[게임특화] G-Sync Ultimate / G-Sync compatible"
    assert "gsync_spec" not in facts_from_section_spec(spec)
    assert any(field == "gsync_spec"
               for _source, field, _reason in withheld_section_tokens(spec))


def test_the_three_sync_certifications_stay_separate_fields():
    facts = facts_from_section_spec(
        "[게임특화] FreeSync Premium / G-Sync compatible / Adaptive Sync")
    assert facts["freesync_spec"][0] == "FreeSync Premium"
    assert facts["gsync_spec"][0] == "G-Sync compatible"
    assert facts["adaptive_sync_spec"][0] == "Adaptive Sync"


def test_support_version_and_count_are_three_fields():
    facts = facts_from_section_spec(
        "[단자정보] HDMI 2.0 / HDMI 2.1 / HDMI(전체): 3개 / mini HDMI")
    assert facts["hdmi_version_spec"][0] == "HDMI 2.0, HDMI 2.1"
    assert facts["hdmi_port_count_spec"][0] == "3"
    assert facts["hdmi_variant_spec"][0] == "mini HDMI"


def test_a_charging_ceiling_keeps_its_condition():
    facts = facts_from_section_spec("[단자정보] PD충전: 최대65W")
    assert facts["pd_charging_spec"][0] == "최대65W"


def test_two_values_for_one_single_valued_field_are_withheld():
    spec = "[단자정보] PD충전: 최대65W / PD충전: 최대90W"
    assert "pd_charging_spec" not in facts_from_section_spec(spec)
    assert withheld_section_tokens(spec)


@pytest.mark.parametrize("spec, reason_fragment", [
    ("[단자정보] 출시가: 179,000원", "listing/promotion"),
    ("[규격] ※ 별도의 증빙을 요하는 사업자전용 상품이 포함되어 있습니다",
     "listing notice"),
    ("[규격] 무게: 6.5(7.8)kg", "which is with the"),
    ("[단자정보] PD충전: 최대60W없음", "truncated"),
    ("[단자정보] 피봇x", "negation"),
    ("[단자정보] 틸트", "without a range"),
    ("[기타] X-rite i1 Display Pro 캘리브레이터 포함", "bundle"),
    ("[기타] 색상: 블랙", "whose colour"),
    ("[화질] 게임모드", "no measurable value"),
])
def test_what_the_section_does_not_settle_is_withheld(spec, reason_fragment):
    assert facts_from_section_spec(spec) == {}
    reasons = " ".join(reason for _s, _f, reason
                       in withheld_section_tokens(spec))
    assert reason_fragment in reasons


def test_a_launch_price_never_becomes_a_product_fact(repository):
    """Whole-corpus: every 출시가 token in every section stays out."""

    promoted = []
    for key, record in repository.catalog()["catalog"].items():
        if not isinstance(record, dict):
            continue
        spec = str(record.get("spec") or "")
        if "출시가" not in spec:
            continue
        for _field, (value, _unit, source) in facts_from_section_spec(
                spec).items():
            if "출시가" in str(value) or "원" == str(value)[-1:]:
                promoted.append([key, source])
    assert promoted == []


# -- precedence --------------------------------------------------------------


def test_a_better_source_is_never_overwritten():
    """A column, a label or a shape-recognised token outranks a heading."""

    spec = "[규격] 무게: 5.7kg / 크기(가로x세로x깊이): 721x420x50mm"
    assert facts_from_section_spec(
        spec, already_supplied=("weight_product_spec",)) == {
        "dimensions_product_spec": ("721x420x50mm", "mm",
                                    "[규격] 크기(가로x세로x깊이): 721x420x50mm"),
    }


def test_a_plain_port_name_does_not_duplicate_the_presence_fact(service):
    """``hdmi_present`` already fires on the spec mentioning HDMI."""

    result = ask(service, "S32DM701", "HDMI 단자 있나요?")
    assert len(values(result, "hdmi_present")) == 1


# -- the production path -----------------------------------------------------


@pytest.mark.parametrize("field_key", sorted(SECTION_FIELD_KEYS))
def test_every_section_field_is_reachable_by_some_wording(field_key):
    """A fact nothing can ask for is not recovered, only stored."""

    from services.spec_attributes import SPEC_ATTRIBUTES

    terms = [term for item in SPEC_ATTRIBUTES
             for term in item.question_terms
             if field_key == item.field_key or field_key in item.also_fields]
    extra = {
        "color_gamut_srgb": "sRGB 커버리지 몇 퍼센트인가요?",
        "color_gamut_ntsc": "NTSC 몇 퍼센트인가요?",
        "color_gamut_dci_p3": "dci-p3 몇 퍼센트인가요?",
        "hdmi_port_count_spec": "HDMI 단자 몇 개인가요?",
        "usb_port_count_spec": "USB 단자 몇 개인가요?",
        "display_port_count": "DP 단자 몇 개인가요?",
        "rf_port_count": "RF 단자 몇 개인가요?",
        "dimensions_product_spec": "제품 크기가 어떻게 되나요?",
        "weight_product_spec": "제품 무게가 몇 kg인가요?",
        "weight_catalog": "무게가 몇 kg인가요?",
        "vesa_hole_spec": "베사홀 규격이 어떻게 되나요?",
        "hdmi_present": "HDMI 단자 있나요?",
        "usb_present": "USB 단자 있나요?",
        "rf_terminal": "RF 단자 있나요?",
        "speaker_present": "스피커 내장인가요?",
        "hdr_spec": "HDR10+ 지원하나요?",
        "pd_charging_spec": "PD충전 몇 W인가요?",
        "power_cable_length": "전원 케이블 길이가 얼마인가요?",
        "tilt_range_spec": "틸트 각도가 어떻게 되나요?",
        "pivot_spec": "피벗 되나요?",
        "mobile_connect_spec": "미러링 되나요?",
    }
    if field_key in extra:
        terms.append(extra[field_key])
    # The production router, not the registry alone: a ``supply_only``
    # presence field such as ``hdmi_present`` is reachable through the
    # service's own routing rather than through registry wording.
    assert any(field_key in fields_for_question(term)[0]
               for term in terms), field_key


@pytest.mark.parametrize("model, question, field_key, value", [
    ("S49CG954", "프리싱크 지원하나요?", "freesync_spec",
     "FreeSync Premium Pro"),
    ("S49CG954", "dci-p3 몇 퍼센트인가요?", "color_gamut_dci_p3", "92%"),
    ("S32DM701", "NTSC 몇 퍼센트인가요?", "color_gamut_ntsc", "72%"),
    ("S32DM701", "조준선 표시 기능 있나요?", "crosshair_spec", "조준선 표시"),
    ("S32DM701", "C타입 단자 있나요?", "usb_c_present", "USB C타입"),
    ("S32DM701", "PD충전 몇 W인가요?", "pd_charging_spec", "최대65W"),
    ("S32DM701", "HDMI 버전이 어떻게 되나요?", "hdmi_version_spec", "HDMI 2.0"),
])
def test_a_section_fact_reaches_the_prompt(service, model, question,
                                           field_key, value):
    result = ask(service, model, question)
    assert values(result, field_key) == [value]
    kept, _dropped, _selection = select_prompt_facts(
        tuple(result.safe_facts), requested_fields=result.requested_fields,
        topics=result.topics, question=question,
    )
    assert value in [str(item.value) for item in kept
                     if item.field_key == field_key]


def test_a_section_fact_is_attributed_to_the_resolved_model(service):
    result = ask(service, "S49CG954", "프리싱크 지원하나요?")
    foreign = [i.model_code for i in result.safe_facts
               if i.scope == "MODEL_CATALOG" and str(i.model_code)
               != "S49CG954"]
    assert foreign == []


def test_no_section_fact_carries_the_spec_blob(repository):
    """Whole-corpus: a measurement never becomes 250 characters of spec."""

    from services.spec_attributes import (
        MEASUREMENT_FIELD_KEYS, NESTED_FIELD_KEYS, PRESENCE_TOKENS,
    )

    value_fields = (MEASUREMENT_FIELD_KEYS | NESTED_FIELD_KEYS) - set(
        PRESENCE_TOKENS)
    blobs = []
    for key, record in repository.catalog()["catalog"].items():
        if not isinstance(record, dict):
            continue
        for field, (value, _unit, _source) in facts_from_section_spec(
                record.get("spec")).items():
            if field in value_fields and len(str(value)) > 120:
                blobs.append([key, field])
    assert blobs == []


def test_every_section_token_in_the_catalogue_is_accounted_for(repository):
    """Whole-corpus: promoted, withheld with a reason, or reported unmapped.

    Silence is the failure this whole step is about, so no token may fall
    through: the three outcomes must add up to the tokens seen.
    """

    totals = collections.Counter()
    for key, record in repository.catalog()["catalog"].items():
        if not isinstance(record, dict):
            continue
        spec = record.get("spec")
        tokens = [1 for section, _token in sectioned_tokens(spec) if section]
        safe, held, unknown = classify_section_spec(spec)
        totals["tokens"] += len(tokens)
        totals["fields"] += len(safe)
        totals["withheld"] += len(held)
        totals["unmapped"] += len(unknown)
        assert all(reason for _s, _f, reason in held), key
    assert totals["tokens"] > 10_000
    assert totals["unmapped"] == 0, totals
