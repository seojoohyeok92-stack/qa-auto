"""Eighteen approved attributes the registry had no word for.

The 26.10.21 reviewer approved 468 Product Knowledge candidates across 22
models. Forty-two of them named a field this module knew no wording for, so
``_polarity_findings`` had nothing to compare and failed closed: an answer
stating exactly what the record states came back
``VALUE_CANNOT_BE_VERIFIED``, which is the ``speaker = 없음`` shape the pilot
hit. Approving a true fact would therefore have made the gate stricter, not
looser.

Each label registered here is the one the stored provenance already uses,
read off the evidence rather than invented -- "USB 포트 잠금 있음", "HDMI-CEC
있음", "SmartThings 허브 있음", "모바일 무선 연결 Mobile to TV, Sound
Mirroring". ``question_terms`` stays empty and ``supply_only`` stays true, so
nothing about question routing changes; this is only about recognising the
attribute in an answer, which is what ``_catalogue_labels`` reads
``spec_labels`` for.

Two names, one attribute: ``vxt_player`` and ``vxt_player_support`` carry the
same evidence label and ``_stem`` does not strip ``_support``, so the shorter
spelling is reached through ``also_fields`` rather than by widening the
suffix list -- widening it would also change ``same_attribute``, which is the
check that keeps ``hdmi_present`` from covering ``hdmi_version_spec``.

Measured after registration: 39 of the 42 approved rows now ground cleanly on
an answer the record licenses. The remaining three are ``mobile_wireless``
and ``remote_control_setting`` rows whose value is a list of three features
longer than ``_MAX_QUOTABLE``; ``_text_findings`` sends a value that cannot be
checked by quoting to staff on purpose, and that rule is left alone.
"""

from __future__ import annotations

import pytest

import services.product_fact_grounding as grounding
from services.product_fact_grounding import unsupported_fact_claims
from services.spec_attributes import SPEC_ATTRIBUTES, fields_for_spec_question

#: field -> (the label the stored evidence uses, a value the record holds)
REGISTERED = {
    "business_tv_app": ("Business TV App", "있음"),
    "contrast_enhancer": ("명암비 강화 기술", "있음"),
    "core_sync": ("Core Sync", "있음"),
    "factory_calibration_report": ("Factory 캘리브레이션 레포트", "있음"),
    "factory_tuning": ("Factory 튜닝", "있음"),
    "hdmi_cec": ("HDMI-CEC", "있음"),
    "hdr10_plus": ("HDR10+", "있음"),
    "mobile_wireless": ("모바일 무선 연결", "Mobile to TV, Sound Mirroring"),
    "pantone_validated": ("Pantone Validated", "있음"),
    "remote_control_setting": ("리모컨 반응 속도 조정", "Slow Button Repeat"),
    "smartthings_hub": ("SmartThings 허브", "있음"),
    "tco_certified": ("TCO Certified", "있음"),
    "tv_plus": ("TV Plus", "있음"),
    "ul_glare_free": ("UL Glare Free", "있음"),
    "usb_c_to_a_gender_cable_included": (
        "USB Type-C to Type-A 젠더 케이블", "있음"),
    "usb_port_lock": ("USB 포트 잠금", "있음"),
    "vxt_player": ("VXT Player Support", "있음"),
    "vxt_player_support": ("VXT Player Support", "있음"),
}
GENERIC_SENTENCES = (
    "지원합니다.",
    "사용 가능합니다.",
    "해당 기능을 지원합니다.",
    "제공됩니다.",
    "이용 가능합니다.",
)


def reasons(answer: str, field_key: str, value: str):
    return sorted(
        str(finding.get("reason"))
        for finding in unsupported_fact_claims(answer, {field_key: value})
        if finding.get("field_key") == field_key
    )


@pytest.fixture
def unregistered(monkeypatch):
    """The module before this change: the catalogue's labels unread."""

    monkeypatch.setattr(grounding, "_catalogue_labels", lambda field: ())


# --- every registered attribute is now recognisable --------------------

@pytest.mark.parametrize("field", sorted(REGISTERED))
def test_the_attribute_has_a_name(field) -> None:
    label, _value = REGISTERED[field]
    terms = grounding._subject_terms(field)
    assert terms, field
    assert label in terms, (field, terms)


@pytest.mark.parametrize("field", sorted(REGISTERED))
def test_an_answer_that_states_the_record_is_accepted(field) -> None:
    label, value = REGISTERED[field]
    answer = ("%s는 없습니다." % label if value == "없음"
              else "%s를 지원합니다." % label if value == "있음"
              else "%s %s." % (label, value))
    assert reasons(answer, field, value) == []


@pytest.mark.parametrize("field", sorted(REGISTERED))
def test_it_was_unverifiable_before(unregistered, field) -> None:
    """Without the label there is nothing to compare, and it fails closed."""

    label, value = REGISTERED[field]
    if value != "있음":
        pytest.skip("only the yes/no values take the fail-closed branch")
    assert reasons("%s를 지원합니다." % label, field, value) == [
        "VALUE_CANNOT_BE_VERIFIED"]


@pytest.mark.parametrize("field", sorted(
    f for f, (_l, v) in REGISTERED.items() if v == "있음"))
def test_a_denial_of_a_present_feature_still_contradicts(field) -> None:
    """Recognising the attribute has to work in both directions."""

    label, _value = REGISTERED[field]
    assert reasons("%s를 지원하지 않습니다." % label, field, "있음") == [
        "POLARITY_CONTRADICTED"]


# --- a generic sentence still names nothing ----------------------------

@pytest.mark.parametrize("sentence", GENERIC_SENTENCES)
@pytest.mark.parametrize("field", sorted(
    f for f, (_l, v) in REGISTERED.items() if v == "있음"))
def test_a_generic_support_sentence_decides_nothing(sentence, field) -> None:
    assert reasons(sentence, field, "있음") == []


def test_no_registered_label_is_a_generic_word() -> None:
    for field in sorted(REGISTERED):
        for term in grounding._subject_terms(field):
            assert term.lower() not in grounding._GENERIC_SUBJECTS, field


# --- the two spellings of one attribute --------------------------------

def test_the_vxt_pair_shares_one_vocabulary() -> None:
    assert grounding._subject_terms("vxt_player") == (
        grounding._subject_terms("vxt_player_support"))


def test_the_suffix_list_was_not_widened() -> None:
    """``also_fields`` carries the pair, not ``_DESCRIPTOR_SUFFIXES``.

    Widening the suffix list would also change ``same_attribute``, which is
    what stops a presence citation covering a version claim.
    """

    assert "_support" not in grounding._DESCRIPTOR_SUFFIXES
    assert "_present" not in grounding._DESCRIPTOR_SUFFIXES
    assert grounding.same_attribute("vxt_player", "vxt_player_support") is False
    assert grounding.same_attribute("hdmi_present", "hdmi_version_spec") is False


# --- nothing else moved ------------------------------------------------

@pytest.mark.parametrize("field", sorted(REGISTERED))
def test_the_question_side_is_untouched(field) -> None:
    item = next((i for i in SPEC_ATTRIBUTES if i.field_key == field), None)
    if item is None:
        # Reached through ``also_fields``; the entry that carries it is the
        # one with the rule.
        assert field == "vxt_player"
        return
    assert item.question_terms == (), field
    assert item.supply_only is True, field


@pytest.mark.parametrize("question", [
    "USB 포트 잠금 되나요?", "HDMI-CEC 지원하나요?", "TV Plus 볼 수 있나요?",
    "SmartThings 허브 있나요?",
])
def test_no_question_routes_to_them_from_wording(question) -> None:
    assert fields_for_spec_question(question) == ((), ())


def test_the_spec_reader_emits_no_new_fact() -> None:
    """These labels are not labels the legacy free-text spec writes.

    Measured over the catalogue's 1,593 spec blobs: 71 fields and 979 facts
    before and after. The check here is the property that makes that true --
    a label only matters to the spec reader if the spec contains it.
    """

    from services.spec_attributes import facts_from_spec

    spec = ("품목 : 게이밍모니터 / 페널 : IPS / DP 포트 1, HDMI 포트 1 / "
            "틸트 -3 ~ +25 / 피봇 -92 ~ +92")
    emitted = set(facts_from_spec(spec))
    assert emitted.isdisjoint(set(REGISTERED)), sorted(
        emitted & set(REGISTERED))


def test_every_attribute_this_module_knows_has_a_name() -> None:
    missing = [
        field for field in sorted(set(grounding._REGISTRY_FIELDS))
        if not grounding._subject_terms(field)
    ]
    assert missing == []


# --- the three the vocabulary cannot help ------------------------------

@pytest.mark.parametrize("value", [
    "Mobile to TV, Sound Mirroring, Wireless TV On",
    "Slow Button Repeat, Remote Control App. for All",
])
def test_a_value_too_long_to_quote_still_goes_to_staff(value) -> None:
    """``_text_findings`` refuses a sentence-long value on purpose.

    Three of the forty-two approved rows are this shape. Registering a name
    for the attribute does not change it and must not: a value that cannot be
    checked by quoting is not grounded, whatever it is called. The rule is
    the length, not the vocabulary -- the two-item spelling of the same
    attribute is accepted.
    """

    field = ("mobile_wireless" if value.startswith("Mobile")
             else "remote_control_setting")
    label, _value = REGISTERED[field]
    assert grounding._subject_terms(field), "the attribute is named"
    assert reasons("%s %s." % (label, value), field, value) == [
        "VALUE_CANNOT_BE_VERIFIED"]
    assert len(grounding._compact(value)) > grounding._MAX_QUOTABLE


def test_the_shorter_spelling_of_the_same_attribute_is_accepted() -> None:
    assert reasons("모바일 무선 연결 Mobile to TV, Sound Mirroring.",
                   "mobile_wireless", "Mobile to TV, Sound Mirroring") == []
