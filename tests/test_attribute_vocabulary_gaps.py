"""Four attributes had no name, so an answer about them could not be checked.

``_subject_terms`` is documented as "what this attribute is called, in a
question or in the catalogue", and it read the question half from
``question_terms`` and the catalogue half from the section rules' regex
source. The registry's own catalogue-label list, ``spec_labels``, it never
read -- and that is the only place three attributes are named at all:

``bluetooth_present``, ``wifi_present`` and ``stand_type_spec`` are
``supply_only``, which the registry defines as "the question side must not
infer this attribute from wording alone". Their ``question_terms`` are empty
by design, on the question side. The answer side asks a different question --
what is this attribute called in a sentence -- and ``spec_labels`` answers it:
"블루투스", "WI-FI"/"와이파이"/"무선랜", "스탠드 형태".

A fourth name had nothing anywhere. Product Knowledge stores the speaker as
``speaker`` -- 64 rows, "없음" / "있음" / "2.0채널" -- while the section rule
maps the spec token to ``speaker_present``, and ``_stem`` does not strip
``_present``, so the two spellings never met. Found on the Coupang pilot of
LS27HG400EKXKR: once ``speaker = 없음`` was approved and GPT ② used it, the
answer "내장 스피커는 없습니다" -- which is exactly what the record says --
came back ``VALUE_CANNOT_BE_VERIFIED``, because ``_polarity_findings`` had no
term to compare and fails closed. That added ``PRODUCT_FACT_NOT_VERIFIED`` to
a draft that had not carried it, so approving a true fact made the gate
stricter.

Measured before the change: 73 of 168 field names gain a term, none is left
without one, and across the 26.10.20 snapshot's drafts re-grounded on the
facts GPT ② reported using, **no** finding appears or disappears. On the spec
side, all 1,593 spec blobs emit exactly the same 979 facts, because no legacy
spec label is "스피커" or "내장 스피커".

What must not come back is the fault fixed in ``_GENERIC_SUBJECTS``: a bare
"지원합니다" naming no attribute must not match any of these. The filter is
applied to the new list too, and the generic cases below pin it.
"""

from __future__ import annotations

import pytest

import services.product_fact_grounding as grounding
from services.product_fact_grounding import unsupported_fact_claims
from services.spec_attributes import SPEC_ATTRIBUTES, fields_for_spec_question

#: The name Product Knowledge stores, beside the registry's own spelling.
PAIRS = (
    ("speaker", "speaker_present"),
    ("bluetooth", "bluetooth_present"),
    ("wifi", "wifi_present"),
    ("stand_type", "stand_type_spec"),
)
GENERIC_SENTENCES = (
    "지원합니다.",
    "사용 가능합니다.",
    "해당 기능을 지원합니다.",
    "제공됩니다.",
)


def reasons(answer: str, field_key: str, value: str):
    return sorted(
        str(finding.get("reason"))
        for finding in unsupported_fact_claims(answer, {field_key: value})
        if finding.get("field_key") == field_key
    )


@pytest.fixture
def unnamed(monkeypatch):
    """The module as it was: the catalogue's labels unread."""

    monkeypatch.setattr(grounding, "_catalogue_labels", lambda field: ())


# --- 1-2. the pilot's own case ------------------------------------------

@pytest.mark.parametrize("field", ["speaker", "speaker_present"])
def test_an_answer_stating_the_absence_is_checkable(field) -> None:
    """LS27HG400EKXKR: the record says 없음 and the answer says so too."""

    assert reasons("내장 스피커는 없습니다.", field, "없음") == []


def test_that_answer_was_unverifiable_before(unnamed) -> None:
    assert reasons("내장 스피커는 없습니다.", "speaker", "없음") == [
        "VALUE_CANNOT_BE_VERIFIED"]


@pytest.mark.parametrize("field", ["speaker", "speaker_present"])
def test_a_speaker_claim_still_contradicts_the_absence(field) -> None:
    """Naming the attribute still decides against it, which is the point."""

    assert reasons("내장 스피커를 지원합니다.", field, "없음") == [
        "POLARITY_CONTRADICTED"]


# --- 3-5. the other three ----------------------------------------------

@pytest.mark.parametrize("field", ["bluetooth_present", "bluetooth"])
def test_a_bluetooth_claim_is_checked_against_the_record(field) -> None:
    assert reasons("블루투스를 지원합니다.", field, "있음") == []
    assert reasons("블루투스를 지원하지 않습니다.", field, "있음") == [
        "POLARITY_CONTRADICTED"]


@pytest.mark.parametrize("field", ["wifi_present", "wifi"])
@pytest.mark.parametrize("wording", ["Wi-Fi", "와이파이", "무선랜"])
def test_a_wifi_absence_is_checked_in_any_of_its_spellings(
    field, wording
) -> None:
    assert reasons("%s는 지원하지 않습니다." % wording, field, "없음") == []
    assert reasons("%s를 지원합니다." % wording, field, "없음") == [
        "POLARITY_CONTRADICTED"]


@pytest.mark.parametrize("field", ["stand_type_spec", "stand_type"])
def test_a_stand_type_sentence_names_the_attribute(field) -> None:
    """A real value from the record: ``stand_type`` holds "BASIC FEET".

    The claim is text, not a yes or a no, so what is checked is that the
    attribute is recognised and the stated value is the record's. An answer
    that names the attribute and restates the value draws nothing.
    """

    assert reasons("스탠드 형태는 BASIC FEET입니다.", field, "BASIC FEET") == []
    assert grounding._states_term("스탠드 형태는 BASIC FEET입니다.", "스탠드 형태")


def test_a_stand_type_sentence_reaches_the_attribute_only_now(unnamed) -> None:
    assert not [
        term for term in grounding._subject_terms("stand_type")
    ], "the gap this closes"


# --- 6. a generic sentence names nothing --------------------------------

@pytest.mark.parametrize("sentence", GENERIC_SENTENCES)
@pytest.mark.parametrize("field,value", [
    ("speaker", "없음"), ("speaker_present", "없음"),
    ("bluetooth", "있음"), ("bluetooth_present", "있음"),
    ("wifi", "있음"), ("wifi_present", "있음"),
])
def test_a_generic_support_sentence_decides_nothing(
    sentence, field, value
) -> None:
    """The fault ``_GENERIC_SUBJECTS`` fixed must not return by this route."""

    assert reasons(sentence, field, value) == []


def test_no_gained_term_is_a_generic_word() -> None:
    for field in sorted(set(grounding._REGISTRY_FIELDS)):
        for term in grounding._catalogue_labels(field):
            if term.lower() in grounding._GENERIC_SUBJECTS:
                pytest.fail("%s offers the generic %r" % (field, term))
        for term in grounding._subject_terms(field):
            assert term.lower() not in grounding._GENERIC_SUBJECTS, field


# --- what the registry now says, and what it still does not -------------

@pytest.mark.parametrize("stored,registry", PAIRS)
def test_both_spellings_resolve_to_the_same_wording(stored, registry) -> None:
    terms = grounding._subject_terms(stored)
    assert terms, stored
    assert terms == grounding._subject_terms(registry)


def test_every_attribute_this_module_knows_has_a_name() -> None:
    missing = [
        field for field in sorted(set(grounding._REGISTRY_FIELDS))
        if not grounding._subject_terms(field)
    ]
    assert missing == []


def test_the_question_side_is_untouched() -> None:
    """``supply_only`` is a rule about questions, and it still holds.

    The three entries keep their empty ``question_terms``, so no wording
    routes a question to them; this change is only about recognising the
    attribute in an answer.
    """

    for name in ("bluetooth_present", "wifi_present", "stand_type_spec",
                 "speaker_present"):
        item = next(i for i in SPEC_ATTRIBUTES if i.field_key == name)
        assert item.question_terms == (), name
        assert item.supply_only is True, name
    for question in ("스피커 있나요?", "블루투스 되나요?", "와이파이 지원하나요?",
                     "스탠드 형태가 어떻게 되나요?"):
        assert fields_for_spec_question(question) == ((), ()), question


def test_the_speaker_entry_adds_no_fact_on_the_spec_side() -> None:
    """Its labels are not labels the legacy spec text writes.

    The spec reader maps a label to an attribute, so a new entry could start
    emitting facts from the free-text spec. Neither of these labels appears
    there: the token that does -- "스피커" on its own line -- is read by the
    section rules, which already mapped it here.
    """

    from services.spec_attributes import facts_from_spec

    spec = "스피커 출력: 20W / 블루투스: 있음 / 와이파이: 있음"
    emitted = facts_from_spec(spec)
    assert "speaker_present" not in emitted
