"""One attribute under two names, and one measurement under three spellings.

Found replaying Coupang inquiry 162413382 on LS27HG400EKXKR after the
26.10.21 review approved its stand facts. The draft was right about all four
-- pivot, tilt, height and speaker -- and grounding reported three findings
anyway:

``height_adjust`` → ``VALUE_NOT_STATED_IN_ANSWER``
    The record holds "105.0 (±5.0) mm" and the draft wrote
    "105.0(±5.0)mm". A unit is read only where it touches its number, so the
    bracketed tolerance in between left the stated height unitless and the
    answer did not count as stating it.

``elevation_range_spec`` → ``ATTRIBUTE_WITHOUT_EVIDENCE``
``tilt_range_spec`` → ``PRODUCT_CLAIM_WITHOUT_CITATION``
    The same two attributes under the other store's spelling. The
    catalogue's spec text is the only place ``elevation_range_spec`` and
    ``tilt_range_spec`` are written, and the collected knowledge is the only
    place ``height_adjust`` and ``tilt`` are: measured over the record, 17
    models hold ``height_adjust``, 21 hold ``tilt``, and **no** model holds
    both names of either pair. Their question wording is identical for the
    height pair and shared for the tilt pair, and the values agree in
    meaning -- "-3 ~ +25" against "-3°(±2°) ~ 25°(±2°)", "120mm" against
    "120.0(±5.0)".

Neither is fixed by relaxing a general rule. ``same_attribute`` is
deliberately spelling-only, and ``also_fields`` cannot serve as the identity
relation because it is a *routing* relation and is wider on purpose: it
declares ``color -> rear_color`` and ``ott_support_spec -> ott_apps_spec``,
two of the five pairings ``same_attribute`` was narrowed to reject. So
identity is stated per pair in ``SpecAttribute.same_as``, and the negative
controls below pin that nothing else joined.

The tolerance fix gives the shared unit to the tolerance's number as well,
which is what keeps a *wrong* tolerance failing: "105.0(±10.0)mm" then
states 10.0 as a length the value does not have, and the conflict check
reports it.
"""

from __future__ import annotations

import pytest

import services.product_fact_grounding as grounding
from services.product_fact_grounding import (
    same_attribute, unsupported_attribute_claims, unsupported_fact_claims,
)
from services.spec_attributes import SPEC_ATTRIBUTES

HEIGHT_VALUE = "105.0 (±5.0) mm"
HEIGHT_UNIT = "mm"


def reasons(answer: str, field_key: str, value, unit=None):
    entry = (value, unit) if unit else value
    return sorted(
        str(finding.get("reason"))
        for finding in unsupported_fact_claims(answer, {field_key: entry})
        if finding.get("field_key") == field_key
    )


# --- 1. the two pairs are one attribute each ---------------------------

@pytest.mark.parametrize("stored,catalogue", [
    ("tilt", "tilt_range_spec"),
    ("height_adjust", "elevation_range_spec"),
])
def test_the_pair_is_one_attribute_in_both_directions(stored, catalogue):
    assert same_attribute(stored, catalogue) is True
    assert same_attribute(catalogue, stored) is True


@pytest.mark.parametrize("stored,catalogue", [
    ("tilt", "tilt_range_spec"),
    ("height_adjust", "elevation_range_spec"),
])
def test_the_identity_is_declared_not_derived(stored, catalogue):
    """Stated on the registry entry, and the spelling rule still rejects it.

    If ``_stem`` were doing this, ``_range`` would have had to join the
    suffix list -- and that list also feeds ``same_attribute`` for every
    other field.
    """

    item = next(i for i in SPEC_ATTRIBUTES if i.field_key == catalogue)
    assert stored in item.same_as
    assert grounding._stem(stored) != grounding._stem(catalogue)
    assert "_range" not in grounding._DESCRIPTOR_SUFFIXES
    assert "_present" not in grounding._DESCRIPTOR_SUFFIXES
    assert "_support" not in grounding._DESCRIPTOR_SUFFIXES


def test_a_citation_of_one_covers_a_claim_about_the_other():
    """What the two findings on 162413382 were.

    ``unsupported_attribute_claims`` skips a field an equivalent delivered
    fact already speaks to, and that is the skip these pairs now take.
    """

    answer = ("높낮이는 105.0(±5.0)mm까지 조절 가능하고, 화면 상하 각도는 "
              "-3°(±2°)~25°(±2°)로 조절됩니다.")
    findings = unsupported_attribute_claims(
        answer,
        delivered_fields=("height_adjust", "tilt"),
        requested_fields=("elevation_range_spec", "tilt_range_spec",
                          "height_adjust", "tilt"),
        asked="높낮이?각도?",
    )
    flagged = {str(item.get("field_key")) for item in findings}
    assert "elevation_range_spec" not in flagged, sorted(flagged)
    assert "tilt_range_spec" not in flagged, sorted(flagged)


# --- 2. nothing else became the same attribute -------------------------

@pytest.mark.parametrize("left,right", [
    # The five the ``same_attribute`` docstring records as removed.
    ("hdmi_present", "hdmi_version_spec"),
    ("brightness_typical", "brightness_peak"),
    ("freesync_spec", "gsync_spec"),
    ("ott_support_spec", "ott_apps_spec"),
    ("color", "rear_color"),
    # Other ``also_fields`` routing pairs that are not one attribute.
    ("color", "color_options"),
    ("color", "front_color"),
    ("vrr_spec", "freesync_spec"),
    ("vrr_spec", "gsync_spec"),
    ("power_consumption_labelled", "power_max"),
    ("power_consumption_labelled", "power_typical"),
    ("pbp", "pip"),
    ("battery_included", "battery_capacity_spec"),
    ("stand_color", "accessory_color"),
    ("brightness_typical_cd_m2", "brightness_peak"),
    # Crossed pairs: each stand movement is its own attribute.
    ("swivel", "tilt"),
    ("tilt", "elevation_range_spec"),
    ("height_adjust", "tilt_range_spec"),
    ("swivel_range_spec", "tilt_range_spec"),
])
def test_these_are_not_the_same_attribute(left, right):
    assert same_attribute(left, right) is False
    assert same_attribute(right, left) is False


def test_also_fields_is_not_read_as_identity():
    """The relation that declares the hazards is not the one consulted.

    ``color.also_fields`` names ``rear_color`` and ``ott_support_spec``'s
    names ``ott_apps_spec``; if ``same_attribute`` read ``also_fields`` those
    two would be one attribute again.
    """

    declared = {
        (item.field_key, other)
        for item in SPEC_ATTRIBUTES for other in item.also_fields
    }
    assert ("color", "rear_color") in declared
    assert ("ott_support_spec", "ott_apps_spec") in declared
    for left, right in declared:
        if right in grounding._declared_same_as(left):
            continue
        if left in grounding._declared_same_as(right):
            continue
        if grounding._stem(left) == grounding._stem(right):
            continue
        assert same_attribute(left, right) is False, (left, right)


def test_only_the_two_pairs_are_declared():
    declared = {
        (item.field_key, other)
        for item in SPEC_ATTRIBUTES for other in getattr(item, "same_as", ())
    }
    assert declared == {
        ("tilt_range_spec", "tilt"),
        ("elevation_range_spec", "height_adjust"),
    }


# --- 3. a tolerance written between a number and its unit --------------

@pytest.mark.parametrize("answer", [
    "높낮이는 105.0 (±5.0) mm까지 조절됩니다.",
    "높낮이는 105.0(±5.0)mm까지 조절됩니다.",
    "높낮이는 105.0 mm (±5.0 mm)까지 조절됩니다.",
    "높낮이는 105.0mm까지 조절됩니다.",
])
def test_every_spelling_of_the_same_measurement_is_accepted(answer):
    assert reasons(answer, "height_adjust", HEIGHT_VALUE, HEIGHT_UNIT) == []


@pytest.mark.parametrize("text,expected", [
    ("105.0 (±5.0) mm", [("105.0", "mm"), ("5.0", "mm")]),
    ("105.0(±5.0)mm", [("105.0", "mm"), ("5.0", "mm")]),
    ("105.0 mm (±5.0 mm)", [("105.0", "mm"), ("5.0", "mm")]),
    ("120 mm", [("120", "mm")]),
])
def test_what_the_text_is_read_as_stating(text, expected):
    assert grounding._measures(text) == expected


def test_the_tolerance_number_carries_the_unit_too():
    """Which is what keeps a wrong tolerance failing, below."""

    assert ("10.0", "mm") in grounding._measures("105.0(±10.0)mm")


# --- 4. a different value is still a different value -------------------

@pytest.mark.parametrize("answer,reason", [
    ("높낮이는 120 mm까지 조절됩니다.", "VALUE_NOT_STATED_IN_ANSWER"),
    ("높낮이는 105 ±10 mm까지 조절됩니다.", "VALUE_NOT_STATED_IN_ANSWER"),
    ("높낮이는 120.0(±5.0)mm까지 조절됩니다.", "VALUE_NOT_STATED_IN_ANSWER"),
    ("높낮이는 10.5cm까지 조절됩니다.", "VALUE_NOT_STATED_IN_ANSWER"),
    ("높낮이는 105.0(±10.0)mm까지 조절됩니다.",
     "ANSWER_STATES_A_DIFFERENT_VALUE"),
])
def test_a_different_measurement_is_rejected(answer, reason):
    assert reasons(answer, "height_adjust", HEIGHT_VALUE, HEIGHT_UNIT) == [
        reason]


def test_no_value_is_hardcoded():
    """The rule is the notation, not this product's height.

    A different model's figure, written the same three ways, behaves the
    same -- so nothing here is keyed to 105.
    """

    value, unit = "120.0 (±5.0) mm", "mm"
    for answer in ("높낮이는 120.0(±5.0)mm까지 조절됩니다.",
                   "높낮이는 120.0 mm (±5.0 mm)까지 조절됩니다."):
        assert reasons(answer, "height_adjust", value, unit) == []
    assert reasons("높낮이는 105.0(±5.0)mm까지 조절됩니다.",
                   "height_adjust", value, unit) == [
        "VALUE_NOT_STATED_IN_ANSWER"]


def test_a_unitless_range_is_unaffected():
    """``tilt`` states degrees the unit table does not carry.

    Its grounding worked before this change and goes on working: the
    tolerance rule only assigns a unit it can read.
    """

    answer = "화면 상하 각도는 -3°(±2°)~25°(±2°)로 조절됩니다."
    assert reasons(answer, "tilt", "-3°(±2°) ~ 25°(±2°)") == []
