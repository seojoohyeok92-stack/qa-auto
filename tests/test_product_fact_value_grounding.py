"""A cited product fact has to survive into the answer, value and all.

``used_product_facts`` is GPT ②'s report of the catalogue rows it answered
from, and the publishing gate reads it as evidence. Two holes were measured
end to end, both GENERATED and auto-answerable at the time:

* a name that was never delivered (``invented_pivot_support``), closed by the
  delivered-set check in the gate;
* a delivered name beside a value the answer changed -- and the first version
  of this rule still let three of those through, because it matched loose
  anchors: ``674.9mm`` answered as "674.9cm" (unit out by ten), ``10bit
  (8bit + FRC)`` answered as "12bit (8bit + FRC)" (a secondary number of the
  value standing in for the one that was changed), and an OTT list containing
  쿠팡플레이 answered "쿠팡플레이는 지원하지 않습니다" (a long list value that
  skipped the check entirely).

So the reading is by the shape of the value -- NUMERIC, COMPOUND, LIST,
LADDER, POLARITY, NEGATIVE -- and this file pins each shape, in both
directions. It also pins where the rule must stay quiet: a wholly hedged
answer asserts nothing, a long dimension string is not something an answer
repeats, and a row that says the product lacks something is agreed with by an
answer that says so.
"""
from __future__ import annotations

import pytest

from services.product_fact_grounding import (
    COMPOUND, LADDER, LIST, NEGATIVE, NUMERIC, POLARITY, classify_fact,
    uncited_product_claims, unsupported_fact_claims,
)

OTT = "넷플릭스, 디즈니+, 웨이브, 티빙, 유튜브, 쿠팡플레이, 아마존, 왓챠 등등"
DIMENSIONS = "(가로x높이x깊이) : 1147.1 X 536.4 X 240.0mm"


def reasons(answer: str, facts: dict) -> list[str]:
    return [item["reason"] for item in unsupported_fact_claims(answer, facts)]


# -- the value is read as a shape -------------------------------------------


@pytest.mark.parametrize("field, value, unit, kind", [
    ("stand_spacing", "674.9mm", "mm", NUMERIC),
    ("brightness_typical", "450", "cd/m2", NUMERIC),
    ("color_bit_depth", "10bit (8bit + FRC)", None, COMPOUND),
    ("dimensions_with_stand", DIMENSIONS, "mm", COMPOUND),
    ("ott_apps_spec", OTT, None, LIST),
    ("hdmi_version_spec", "HDMI 2.0, HDMI 2.1", None, LIST),
    ("freesync_spec", "FreeSync Premium Pro", None, LADDER),
    ("pivot_spec", "있음", None, POLARITY),
    ("touch_support_spec", "터치 미지원", None, NEGATIVE),
])
def test_a_delivered_value_is_classified_by_its_shape(field, value, unit, kind):
    assert classify_fact(field, value, unit).kind == kind


# -- NUMERIC: the number and its unit are one claim --------------------------


@pytest.mark.parametrize("answer, value, unit", [
    ("스탠드 다리 사이 간격은 674.9cm입니다.", "674.9mm", "mm"),
    ("주사율은 240cd입니다.", "240Hz", "Hz"),
    ("최대 밝기는 1000Hz입니다.", "1,000cd", "cd/m2"),
    ("주사율은 100Hz입니다.", "240Hz", "Hz"),
    ("밝기는 450Hz입니다.", "450", "cd/m2"),
])
def test_the_same_number_under_another_unit_is_not_the_value(
    answer, value, unit,
):
    assert reasons(answer, {"field": (value, unit)}) == [
        "VALUE_NOT_STATED_IN_ANSWER"]


@pytest.mark.parametrize("answer, value, unit", [
    ("스탠드 다리 사이 간격은 674.9mm입니다.", "674.9mm", "mm"),
    ("스탠드 다리 사이 간격은 674.9mm로 80cm 이내입니다.", "674.9mm", "mm"),
    ("주사율은 240Hz입니다.", "240Hz", "Hz"),
    ("최대 밝기는 1000cd입니다.", "1,000cd", "cd/m2"),
    ("최대 밝기는 1,000cd입니다.", "1,000cd", "cd/m2"),
    ("밝기는 450cd입니다.", "450", "cd/m2"),
    ("밝기는 450니트입니다.", "450", "cd/m2"),
])
def test_the_number_with_its_own_unit_keeps_the_value(answer, value, unit):
    assert reasons(answer, {"field": (value, unit)}) == []


@pytest.mark.parametrize("answer, value, unit", [
    ("스탠드 다리 사이 간격은 674.9입니다.", "674.9mm", "mm"),
    ("최대 밝기는 1000입니다.", "1,000cd", "cd/m2"),
    # The catalogue stores the unit beside the value rather than inside it,
    # and it is still part of the claim.
    ("밝기는 450입니다.", "450", "cd/m2"),
])
def test_a_bare_number_does_not_state_a_measurement(answer, value, unit):
    """674.9 is not 674.9mm: read as the value it is equally centimetres."""

    assert reasons(answer, {"field": (value, unit)}) == [
        "VALUE_NOT_STATED_IN_ANSWER"]


@pytest.mark.parametrize("answer, value", [
    ("명암비는 2,500:1입니다.", "2,500:1"),
    ("해상도는 5120 x 1440입니다.", "5120 x 1440(DQHD)"),
])
def test_a_genuinely_unitless_value_may_be_answered_as_a_bare_number(
    answer, value,
):
    assert reasons(answer, {"field": (value, None)}) == []


@pytest.mark.parametrize("answer, reason", [
    ("화면 크기는 49인치입니다.", None),
    ("화면 크기는 123.8cm입니다.", None),
    ("화면 크기는 55인치입니다.", "VALUE_NOT_STATED_IN_ANSWER"),
])
def test_one_quantity_written_in_two_units_accepts_either(answer, reason):
    """"123.8cm(49인치)" is one screen size, not two measurements."""

    found = reasons(answer, {"screen_size": ("123.8cm(49인치)", None)})
    assert found == ([reason] if reason else [])


def test_any_notation_of_the_same_measurement_counts():
    """A record can state one measurement three ways; one of them is enough."""

    delivered = [("240Hz", "Hz"), ("Max 240", "Hz"), ("240", "Hz")]
    assert reasons("주사율은 240Hz입니다.", {"refresh_rate": delivered}) == []
    assert reasons("주사율은 165Hz입니다.", {"refresh_rate": delivered}) == [
        "VALUE_NOT_STATED_IN_ANSWER"]


def test_a_quoted_value_that_is_then_negated_is_reported():
    assert reasons("240Hz는 지원하지 않습니다.", {"refresh_rate": ("240Hz", "Hz")}) == [
        "ANSWER_NEGATES_THE_VALUE"]


# -- COMPOUND: the leading measurement is the assertion ----------------------


def test_a_secondary_number_cannot_stand_in_for_the_leading_one():
    """"12bit (8bit + FRC)" repeats the 8 and changes the 10."""

    assert reasons(
        "이 제품은 12bit (8bit + FRC) 색심도를 지원합니다.",
        {"color_bit_depth": "10bit (8bit + FRC)"},
    ) == ["VALUE_NOT_STATED_IN_ANSWER"]


@pytest.mark.parametrize("answer", [
    "이 제품은 10bit (8bit + FRC) 색심도를 지원합니다.",
    "10비트 색심도를 지원합니다.",
    "8bit + FRC 방식으로 10bit 색을 표현합니다.",
])
def test_the_leading_measurement_keeps_the_compound_value(answer):
    assert reasons(answer, {"color_bit_depth": "10bit (8bit + FRC)"}) == []


def test_an_independent_compound_needs_its_leading_measurement():
    """Which sub-value was asked is not decidable here, so it fails closed.

    Those sub-values have fields of their own -- ``display_width_mm``,
    ``display_height_mm`` -- and citing one of those is how an answer about
    the height is grounded.
    """

    facts = {"dimensions_with_stand": (DIMENSIONS, "mm")}
    assert reasons("크기는 가로 1147.1mm입니다.", facts) == []
    assert reasons("크기는 가로 900mm입니다.", facts) == [
        "VALUE_NOT_STATED_IN_ANSWER"]


# -- LIST: the member decides ------------------------------------------------


@pytest.mark.parametrize("answer, reason", [
    ("쿠팡플레이를 지원합니다.", None),
    ("넷플릭스와 유튜브를 지원합니다.", None),
    ("쿠팡플레이는 지원하지 않습니다.", "LIST_MEMBER_DENIED"),
    ("왓챠는 지원되지 않습니다.", "LIST_MEMBER_DENIED"),
    ("라프텔을 지원합니다.", "LIST_MEMBER_NOT_DELIVERED"),
])
def test_a_list_is_judged_by_the_member_the_answer_names(answer, reason):
    found = reasons(answer, {"ott_apps_spec": OTT})
    assert found == ([reason] if reason else [])


@pytest.mark.parametrize("answer, reason", [
    ("HDMI 2.0과 HDMI 2.1을 모두 지원합니다.", None),
    ("HDMI 2.1은 지원하지 않습니다.", "LIST_MEMBER_DENIED"),
])
def test_the_same_rule_applies_to_a_port_revision_list(answer, reason):
    found = reasons(answer, {"hdmi_version_spec": "HDMI 2.0, HDMI 2.1"})
    assert found == ([reason] if reason else [])


# -- LADDER: the tier is the claim -------------------------------------------


@pytest.mark.parametrize("answer, reason", [
    ("FreeSync Premium Pro를 지원합니다.", None),
    ("FreeSync Premium만 지원합니다.", "VALUE_NOT_STATED_IN_ANSWER"),
    ("이 제품은 FreeSync를 지원하지 않습니다.", "VALUE_NOT_STATED_IN_ANSWER"),
    ("FreeSync Premium Pro는 지원하지 않습니다.", "ANSWER_NEGATES_THE_VALUE"),
])
def test_a_lower_tier_is_a_different_claim(answer, reason):
    found = reasons(answer, {"freesync_spec": "FreeSync Premium Pro"})
    assert found == ([reason] if reason else [])


# -- POLARITY and NEGATIVE ---------------------------------------------------


@pytest.mark.parametrize("value, answer, reason", [
    ("있음", "피벗을 지원합니다.", None),
    ("있음", "피벗은 지원하지 않습니다.", "POLARITY_CONTRADICTED"),
    ("없음", "피벗은 지원하지 않습니다.", None),
    ("없음", "피벗을 지원합니다.", "POLARITY_CONTRADICTED"),
])
def test_a_polarity_value_is_checked_against_the_answers_polarity(
    value, answer, reason,
):
    found = reasons(answer, {"pivot_spec": value})
    assert found == ([reason] if reason else [])


@pytest.mark.parametrize("answer, reason", [
    ("터치는 지원하지 않습니다.", None),
    ("터치를 지원합니다.", "ANSWER_CONTRADICTS_A_NEGATIVE_VALUE"),
])
def test_a_negative_value_may_not_be_turned_into_a_yes(answer, reason):
    found = reasons(answer, {"touch_support_spec": "터치 미지원"})
    assert found == ([reason] if reason else [])


def test_a_short_text_value_has_to_appear():
    assert reasons("USB C타입 단자를 지원합니다.", {"usb_c_present": "USB C타입"}) == []
    assert reasons("USB-C 단자는 지원하지 않습니다.",
                   {"usb_c_present": "USB C타입"}) == [
        "VALUE_NOT_STATED_IN_ANSWER"]
    assert reasons("HDMI 단자가 있습니다.", {"hdmi_present": "HDMI"}) == []
    assert reasons("HDMI 단자는 없습니다.", {"hdmi_present": "HDMI"}) == [
        "ANSWER_NEGATES_THE_VALUE"]


# -- the hedge is per sentence ----------------------------------------------


def test_a_hedge_in_another_sentence_does_not_excuse_a_wrong_claim():
    """This is the laundering path: assert, then disclaim."""

    assert reasons(
        "FreeSync Premium만 지원합니다. 정확한 다른 사양은 확인이 필요합니다.",
        {"freesync_spec": "FreeSync Premium Pro"},
    ) == ["VALUE_NOT_STATED_IN_ANSWER"]


def test_an_answer_that_only_hedges_states_nothing_to_contradict():
    assert reasons(
        "정확한 사양은 확인이 필요하여 담당자가 안내드리겠습니다.",
        {"color_bit_depth": "10bit (8bit + FRC)"},
    ) == []


def test_each_failing_fact_is_named_with_its_value_and_shape():
    found = unsupported_fact_claims(
        "12비트이고 FreeSync는 지원하지 않습니다.",
        {"color_bit_depth": "10bit (8bit + FRC)",
         "freesync_spec": "FreeSync Premium Pro"},
    )
    assert [(item["field_key"], item["delivered_value"], item["value_shape"])
            for item in found] == [
        ("color_bit_depth", "10bit (8bit + FRC)", COMPOUND),
        ("freesync_spec", "FreeSync Premium Pro", LADDER),
    ]


def test_an_empty_answer_or_no_cited_facts_is_not_a_finding():
    assert unsupported_fact_claims("", {"f": "10bit"}) == []
    assert unsupported_fact_claims("10비트입니다.", {}) == []
    assert unsupported_fact_claims("10비트입니다.", {"f": ""}) == []

# -- a list member nobody registered ----------------------------------------
#
# The first version of the list rule compared the answer against the
# attribute's registered question wording, so 라프텔 was caught only because
# it happened to be in the registry and 애플TV was not caught at all. The
# item now comes from the grammar of the answer and of the question, so no
# family needs its members enumerated in advance -- which matters because 2-c
# adds more list families.


@pytest.mark.parametrize("answer, asked, reason", [
    ("애플TV를 지원합니다.", "애플TV 되나요?", "LIST_MEMBER_NOT_DELIVERED"),
    # The answer names nothing; the question says what it is about.
    ("지원합니다.", "애플TV 되나요?", "LIST_MEMBER_NOT_DELIVERED"),
    ("네, 지원합니다.", "쿠팡플레이 되나요?", None),
    ("쿠팡플레이를 지원합니다.", "쿠팡플레이 되나요?", None),
    ("쿠팡플레이는 지원하지 않습니다.", "쿠팡플레이 되나요?", "LIST_MEMBER_DENIED"),
    # The customer's spelling and the catalogue's differ; the answer names a
    # listed item, which is what the customer needed.
    ("디즈니+를 지원합니다.", "디즈니플러스 되나요?", None),
    # A statement about the attribute itself, not about a missing item.
    ("이 제품은 OTT를 지원합니다.", "OTT 되나요?", None),
])
def test_an_unlisted_item_is_caught_without_being_registered(
    answer, asked, reason,
):
    found = [
        item["reason"] for item in unsupported_fact_claims(
            answer, {"ott_apps_spec": OTT}, asked=asked)
    ]
    assert found == ([reason] if reason else [])


@pytest.mark.parametrize("answer, asked, reason", [
    ("HDMI 2.1을 지원합니다.", "HDMI 2.1 되나요?", None),
    ("HDMI 2.2를 지원합니다.", "HDMI 2.2 되나요?", "LIST_MEMBER_NOT_DELIVERED"),
])
def test_the_same_reading_works_for_another_list_family(answer, asked, reason):
    found = [
        item["reason"] for item in unsupported_fact_claims(
            answer, {"hdmi_version_spec": "HDMI 2.0, HDMI 2.1"}, asked=asked)
    ]
    assert found == ([reason] if reason else [])


def test_a_feature_list_reads_the_same_way():
    facts = {"smart_feature_spec": "미러링, 실시간번역, 음성인식"}
    assert unsupported_fact_claims(
        "미러링과 음성인식을 지원합니다.", facts, asked="미러링 되나요?") == []
    assert [item["reason"] for item in unsupported_fact_claims(
        "블루투스 키보드를 지원합니다.", facts, asked="블루투스 키보드 되나요?")] == [
        "LIST_MEMBER_NOT_DELIVERED"]


# -- a claim with no citation at all ----------------------------------------


DELIVERED = {
    "color_bit_depth": "10bit (8bit + FRC)",
    "usb_c_present": "USB C타입",
    "hdmi_port_count_spec": ("2개", "개"),
    "ott_apps_spec": OTT,
}


@pytest.mark.parametrize("answer, field", [
    ("이 제품은 12비트 색심도를 지원합니다.", "color_bit_depth"),
    ("USB-C 단자는 지원하지 않습니다.", "usb_c_present"),
    ("HDMI 단자는 2개입니다.", "hdmi_port_count_spec"),
    ("넷플릭스를 지원합니다.", "ott_apps_spec"),
    # Right or wrong is not the question here: nothing was offered as the
    # reason.
    ("이 제품은 10bit (8bit + FRC) 색심도를 지원합니다.", "color_bit_depth"),
])
def test_a_product_claim_with_no_citation_is_reported(answer, field):
    found = uncited_product_claims(answer, DELIVERED)
    assert field in [item["field_key"] for item in found], found
    assert {item["reason"] for item in found} == {
        "PRODUCT_CLAIM_WITHOUT_CITATION"}


@pytest.mark.parametrize("answer", [
    "배송은 2~3일 내에 완료되며 설치 기사님이 방문합니다.",
    "무료 배송이며 2개월 무상 보증이 적용됩니다.",
    "설치비는 별도로 발생하지 않습니다.",
    "반품은 수령 후 7일 이내에 가능합니다.",
    "정확한 사양은 확인이 필요하여 담당자가 안내드리겠습니다.",
])
def test_an_answer_about_something_else_is_not_a_product_claim(answer):
    """A shared unit is not a shared subject.

    "2개월 무상 보증" states a quantity in the same counter as "HDMI 2개" and
    says nothing about HDMI, which an earlier version of this read as a claim
    about the port count.
    """

    assert uncited_product_claims(answer, DELIVERED) == []


def test_a_long_text_value_cannot_be_verified_and_says_so():
    """No quoting is possible, so it grants nothing rather than passing."""

    value = "스탠드형(고정형) / 벽걸이 설치 시 별도 브래킷이 필요하며 설치 환경에 따라 다릅니다"
    assert [item["reason"] for item in unsupported_fact_claims(
        "스탠드형이며 벽걸이도 가능합니다.", {"stand_type_spec": value})] == [
        "VALUE_CANNOT_BE_VERIFIED"]
