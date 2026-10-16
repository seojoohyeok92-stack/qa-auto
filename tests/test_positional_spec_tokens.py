"""A positional spec token is claimed only when its own shape settles it.

"80cm(32인치) / 3840 x 2160(4K UHD) / 60Hz / VA / 와이드(16:9) / 평면 /
 4ms(GTG) / 400cd / 3,000:1 / HDR10+ / 스피커" is an enumeration: most of it
means what it means because of where it sits. This step claims only the subset
whose unit or shape says the attribute outright, and the tests below are
mostly about what it must *refuse* -- a bare number, a year, a wattage, a port
count, a certification number, a panel type, an aspect ratio.

Measured before the change, on the catalog of 2026-09-27: 32,539 positional
tokens across 1,487 models, of which 89 were structured.
"""
from __future__ import annotations

import pytest

from answer.text_utils import split_subquestions
from services.product_knowledge_service import ProductKnowledgeService
from services.spec_attributes import (
    POSITIONAL_FIELD_KEYS,
    classify_positional,
    facts_from_positional_spec,
)

MONITOR = ("80cm(32인치) / 3840 x 2160(4K UHD) / 60Hz / VA / 와이드(16:9) / 평면 "
           "/ 4ms(GTG) / 400cd / 3,000:1 / HDR10+ / 스피커 / USB PD 지원 "
           "/ Wi-Fi(무선) / 블루투스 / 6.7kg / 베사홀: 100 x 100mm")


@pytest.fixture(scope="module")
def service() -> ProductKnowledgeService:
    return ProductKnowledgeService()


def lookup(service, question, model, name=None):
    return service.facts_for_inquiry(
        product_id=None, questions=split_subquestions(question),
        question=question, model_code=model,
        product_name=name or f"삼성 모니터 {model}", option_name="",
        include_all_catalog_fields=False,
    )


def fact(result, field_key):
    found = [item for item in result.safe_facts if item.field_key == field_key]
    return found[0] if found else None


# -- what is claimed --------------------------------------------------------


def test_each_self_describing_token_is_read_as_its_own_attribute():
    safe, ambiguous, _unmapped = classify_positional(MONITOR)
    assert ambiguous == ()
    assert safe["refresh_rate"][0] == "60Hz"
    assert safe["weight_catalog"][0] == "6.7kg"
    assert safe["brightness_typical_cd_m2"][0] == "400cd"
    assert safe["response_time_ms"][0] == "4ms(GTG)"
    assert safe["contrast_ratio"][0] == "3,000:1"
    assert safe["screen_size"][0] == "80cm(32인치)"
    assert safe["resolution"][0] == "3840 x 2160(4K UHD)"
    assert safe["vesa_mm"][0] == "베사홀: 100 x 100mm"


def test_the_source_token_travels_with_the_value():
    """Provenance: a shape-recognised value must be traceable to its text."""

    safe, _ambiguous, _unmapped = classify_positional(MONITOR)
    for _field, (value, _unit, token) in safe.items():
        assert token
        assert value == token


def test_one_token_stating_one_measurement_twice_is_one_fact():
    """"80cm(32인치)" is cm and inches for the same screen, not two facts."""

    safe, _ambiguous, _unmapped = classify_positional("80cm(32인치)")
    assert list(safe) == ["screen_size"]
    assert safe["screen_size"][0] == "80cm(32인치)"


def test_a_resolution_keeps_its_marketing_label_with_the_pixels():
    safe, _a, _u = classify_positional("3840 x 2160(4K UHD)")
    assert safe["resolution"][0] == "3840 x 2160(4K UHD)"


# -- what must be refused ---------------------------------------------------


@pytest.mark.parametrize("token", [
    # positional enumerations whose meaning comes from their neighbours
    "VA", "IPS", "Fast IPS", "평면", "커브드", "1500R", "와이드(16:9)",
    "울트라와이드(21:9)", "FreeSync", "FreeSync Premium", "G-Sync compatible",
    "Adaptive Sync", "헤드폰 아웃", "스피커", "DP", "DP 1.4", "D-SUB",
    "HDR10", "HDR10+", "USB C타입", "USB PD 지원", "Wi-Fi(무선)", "블루투스",
    "틸트(상하)", "엘리베이션(높낮이)", "스위블(좌우)", "피벗(회전)",
    # bracketed sections, which are 2-b
    "[색상영역] sRGB: 99%", "[게임특화] FreeSync", "[단자정보] HDMI 2.0",
    # numbers that must not be mistaken for a measurement
    "2023년형", "24년형", "38W", "74W", "120W", "2개", "1개", "4개",
    "SU10349-23002", "LS32DM501EKXKR", "10914735269", "1등급",
    "16:9", "21:9", "400", "1,000", "100 x 100mm", "4K UHD",
    "8bit + FRC", "99%", "최대65W", "1.5m",
])
def test_a_token_whose_shape_does_not_settle_it_is_refused(token):
    safe, ambiguous, unmapped = classify_positional(token)
    assert safe == {}, (token, safe)
    assert ambiguous == ()
    assert unmapped == (token,)


def test_an_aspect_ratio_is_not_read_as_a_contrast_ratio():
    """Both are "N:M". A contrast's second term is 1; an aspect's is not."""

    assert classify_positional("16:9")[0] == {}
    assert classify_positional("1,000:1")[0]["contrast_ratio"][0] == "1,000:1"


def test_a_vesa_hole_is_not_read_as_a_resolution():
    """"100 x 100mm" has a unit; "1920 x 1080" does not."""

    assert "resolution" not in classify_positional("100 x 100mm")[0]
    assert classify_positional("1920 x 1080")[0]["resolution"][0] == (
        "1920 x 1080")


def test_two_different_values_for_one_attribute_are_both_withheld():
    """Choosing between them is a review decision, not a parse."""

    safe, _a, _u = classify_positional("60Hz / 144Hz")
    assert "refresh_rate" not in safe


# -- precedence -------------------------------------------------------------


def test_a_dedicated_column_outranks_a_recognised_token():
    """The column is the better evidence, so the token must not duplicate it."""

    supplied = facts_from_positional_spec(
        MONITOR, already_supplied={"refresh_rate", "resolution"})
    assert "refresh_rate" not in supplied
    assert "resolution" not in supplied
    assert "brightness_typical_cd_m2" in supplied


def test_an_explicit_label_outranks_a_recognised_token():
    """1-a's labelled parser wins; this only fills what it left empty."""

    supplied = facts_from_positional_spec(
        MONITOR, already_supplied={"vesa_mm"})
    assert "vesa_mm" not in supplied


def test_no_positional_field_is_a_newly_invented_name():
    """2-a reuses the names the router and the stored facts already use."""

    assert POSITIONAL_FIELD_KEYS == {
        "brightness_typical_cd_m2", "response_time_ms", "contrast_ratio",
        "vesa_mm", "refresh_rate", "weight_catalog", "screen_size",
        "resolution",
    }


# -- through the production service -----------------------------------------


@pytest.mark.parametrize("model, question, field_key", [
    ("S32BM80P", "주사율 몇 Hz인가요?", "refresh_rate"),
    ("S32BM80P", "무게가 몇 kg인가요?", "weight_catalog"),
    ("S32BM80P", "밝기 몇 니트인가요?", "brightness_typical_cd_m2"),
    ("S32BM80P", "응답속도 얼마인가요?", "response_time_ms"),
    ("S32BM80P", "명암비가 어떻게 되나요?", "contrast_ratio"),
    ("S32BM80P", "화면 크기가 몇 인치인가요?", "screen_size"),
    ("S32BM80P", "해상도가 어떻게 되나요?", "resolution"),
    ("G7 G70B S32BG700", "베사 규격이 어떻게 되나요?", "vesa_mm"),
    ("G7 G70B S32BG700", "응답속도 얼마인가요?", "response_time_ms"),
    ("TFG32F24V2", "명암비가 어떻게 되나요?", "contrast_ratio"),
])
def test_the_question_reaches_the_token_through_the_service(
    service, model, question, field_key,
):
    result = lookup(service, question, model)
    found = fact(result, field_key)
    assert found is not None, [f.field_key for f in result.safe_facts]
    assert str(found.value).strip()
    # Only the asked-about attribute, not the whole enumeration.
    assert len(result.safe_facts) <= 3, [f.field_key for f in result.safe_facts]


def test_a_brightness_question_does_not_pull_the_response_time(service):
    result = lookup(service, "밝기 몇 니트인가요?", "S32BM80P")
    keys = {item.field_key for item in result.safe_facts}
    assert "brightness_typical_cd_m2" in keys
    assert "response_time_ms" not in keys
    assert "contrast_ratio" not in keys


def test_no_recognised_token_becomes_a_blob(service):
    """A parse failure must leave nothing behind, never the spec text."""

    catalog = service.catalog_repository.catalog()["catalog"]
    for key, record in catalog.items():
        if not isinstance(record, dict):
            continue
        safe, _a, _u = classify_positional(record.get("spec"))
        for field_key, (value, _unit, _token) in safe.items():
            assert len(str(value)) <= 60, (key, field_key, value)


# -- found by auditing the whole corpus, not by a sample -------------------


@pytest.mark.parametrize("token, value", [
    ("1,000cd", "1,000cd"), ("1,300cd", "1,300cd"),
    ("1,400cd", "1,400cd"), ("1,600cd", "1,600cd"),
])
def test_a_thousands_separator_is_part_of_the_brightness(token, value):
    """27 tokens went nowhere because the pattern allowed only bare digits.

    Found by auditing every positional token in the catalog against what the
    patterns claim -- 1,000cd appears 21 times -- not by reading a model.
    """

    safe, ambiguous, _unmapped = classify_positional(token)
    assert ambiguous == ()
    assert safe["brightness_typical_cd_m2"][0] == value


def test_a_bare_thousands_number_is_still_not_a_measurement():
    """The separator is allowed; the unit is still required."""

    assert classify_positional("1,000")[0] == {}
    assert classify_positional("400")[0] == {}


def test_no_comma_thousands_measurement_is_left_unclaimed(service):
    """Whole-corpus: every ``N,NNNunit`` token is read by something."""

    import re

    shape = re.compile(r"^\d{1,3}(?:,\d{3})+\s*(?:cd|nit|니트|hz|kg)$", re.I)
    catalog = service.catalog_repository.catalog()["catalog"]
    leftover = [
        (key, token)
        for key, record in catalog.items() if isinstance(record, dict)
        for token in classify_positional(record.get("spec"))[2]
        if shape.match(token)
    ]
    assert leftover == []


def test_the_comma_delimited_dialect_is_read_too():
    """47 models enumerate with commas instead of slashes.

    "블랙, 2023년 11월, 123.8cm(49인치), 5120x1440(DQHD), 240Hz, VA,
     화면비율: 32:9, 1ms(GTG), 450cd, 2,500:1" -- the slash split produced one
    segment, and the labelled parser only reads segments carrying a label, so
    these self-describing tokens were read by nothing at all.
    """

    spec = ("블랙,  2023년 11월,  123.8cm(49인치),  5120x1440(DQHD),  240Hz,  "
            "VA, 화면비율: 32:9,  커브드,  1ms(GTG),  450cd,  2,500:1")
    safe, ambiguous, _unmapped = classify_positional(spec)
    assert ambiguous == ()
    assert safe["refresh_rate"][0] == "240Hz"
    assert safe["response_time_ms"][0] == "1ms(GTG)"
    assert safe["brightness_typical_cd_m2"][0] == "450cd"
    assert safe["contrast_ratio"][0] == "2,500:1"
    assert safe["screen_size"][0] == "123.8cm(49인치)"


def test_a_purely_labelled_spec_gains_no_positional_claims():
    """Widening the split must not let the labelled dialect be re-read."""

    labelled = ("25년형 / 패널:VA패널 / 107.9 cm, 제품크기(가로x높이x깊이): "
                "967.5 x 561.4 x 59.7 mm, 스탠드 다리 사이 간격: 674.9mm")
    safe, ambiguous, _unmapped = classify_positional(labelled)
    assert ambiguous == ()
    assert "stand_spacing" not in safe
    assert "dimensions_product_spec" not in safe
