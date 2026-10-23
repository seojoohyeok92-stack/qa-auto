"""Does the answer still say what the product fact said?

``used_product_facts`` is GPT ②'s report of the catalogue rows it answered
from, and the publishing gate reads it as evidence. Checking that each name
was delivered closed one hole; checking the value closes the other. Measured
end to end, every one of these was GENERATED and auto-answerable at some point
in this work:

* ``color_bit_depth = 10bit (8bit + FRC)`` cited, answer "12비트 … 지원";
* ``freesync_spec = FreeSync Premium Pro`` cited, answer "FreeSync를 지원하지
  않습니다";
* ``usb_c_present = USB C타입`` cited, answer "USB-C 단자는 지원하지 않습니다";
* ``stand_spacing = 674.9mm`` cited, answer "674.9cm입니다" -- the number
  right and the unit out by a factor of ten;
* ``color_bit_depth = 10bit (8bit + FRC)`` cited, answer "12bit (8bit + FRC)"
  -- a secondary number of the value standing in for the one that was changed;
* ``ott_apps_spec`` listing 쿠팡플레이 cited, answer "쿠팡플레이는 지원하지
  않습니다".

The reading here is by the *shape of the value*, not by a pattern per
incident, because 2-c adds more compound and list values:

``NUMERIC``
    One measurement. Number and unit are one claim: 674.9mm is not 674.9cm,
    and 240Hz is not 240cd. A unit the catalogue stores in ``unit`` rather
    than in the text counts as part of the value.

``COMPOUND``
    Several numbers in one value, "10bit (8bit + FRC)". The leading
    measurement is the assertion; the others describe how it is reached, so a
    secondary number cannot stand in for it. Where the numbers are
    independent -- a width, a height and a depth in one string -- which one
    the customer asked is not decidable here, so an answer that does not
    carry the leading one is left for staff. Those sub-values have fields of
    their own, and that is what should be cited.

``LIST``
    Items that are all true at once. The answer never repeats the list; what
    it must get right is the member it talks about. A member the value lists
    may not be denied, and a member of the same family that the value does
    *not* list may not be affirmed.

``LADDER``
    One tier of a certification. "FreeSync Premium" is a different claim from
    "FreeSync Premium Pro".

``POLARITY``
    The value is the yes or the no. The attribute's own question wording says
    what the subject is, and the answer may not say the opposite of it.

A hedge is read per sentence, not per answer: "FreeSync Premium만 지원합니다.
정확한 다른 사양은 확인이 필요합니다." asserts the first sentence, and a
later disclaimer does not withdraw it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from services.spec_attributes import (
    LADDER_FIELDS, LIST_FIELDS, SECTION_RULES, SPEC_ATTRIBUTES, SPEC_UNITS,
)

NUMERIC = "NUMERIC"
COMPOUND = "COMPOUND"
LIST = "LIST"
LADDER = "LADDER"
POLARITY = "POLARITY"
TEXT = "TEXT"
NEGATIVE = "NEGATIVE"

# Units that are the same unit written differently. A number means a different
# thing under a different class, and nothing here converts between classes:
# 674.9mm and 67.49cm are the same length, and an answer that writes the
# second has not stated the value, because the catalogue wrote the first.
_UNIT_CLASSES: tuple[frozenset[str], ...] = (
    frozenset({"mm"}),
    frozenset({"cm"}),
    frozenset({"m"}),
    frozenset({"kg"}),
    frozenset({"g"}),
    frozenset({"hz"}),
    frozenset({"cd", "nit", "니트", "cdm2", "cd/m2", "cd/㎡", "cd㎡"}),
    frozenset({"bit", "비트"}),
    frozenset({"inch", "인치"}),
    frozenset({"ms"}),
    frozenset({"w"}),
    frozenset({"개", "ea"}),
    frozenset({"%"}),
    frozenset({"mah"}),
    frozenset({"r"}),
)
_UNIT_TOKENS = sorted(
    (token for family in _UNIT_CLASSES for token in family),
    key=len, reverse=True,
)
# A Korean particle attaches straight to the unit ("674.9mm입니다"), so a
# unit may be followed by Hangul; what it may not be followed by is another
# letter or digit, which would make it a different token. A number with no
# unit is only read as one when nothing unit-like follows it.
_MEASURE = re.compile(
    r"(?P<number>\d+(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?:(?P<unit>"
    + "|".join(re.escape(token) for token in _UNIT_TOKENS)
    + r")(?![0-9A-Za-z])|(?![0-9A-Za-z]))",
    re.IGNORECASE,
)
_NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"
#: A measurement, its tolerance in brackets, then the unit both share.
#: "105.0 (±5.0) mm", "105.0(±5.0)mm", "-3°(±2°)" -- the unit after the
#: bracket belongs to the number before it.
_TOLERANCE_UNIT = re.compile(
    r"(?P<lead>" + _NUMBER + r")\s*"
    r"\(\s*(?:±|\+/-|\+-|\+/‑)\s*(?P<tolerance>" + _NUMBER + r")\s*"
    r"(?:" + "|".join(re.escape(token) for token in _UNIT_TOKENS) + r")?\s*\)"
    r"\s*(?P<unit>"
    + "|".join(re.escape(token) for token in _UNIT_TOKENS)
    + r")(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_POLARITY_VALUES = frozenset({
    "있음", "없음", "지원", "미지원", "가능", "불가", "불가능", "y", "n",
    "o", "x", "yes", "no", "true", "false", "해당", "해당없음", "포함",
})
#: The value itself says the product does not have the thing.
_NEGATIVE_MARKERS = ("미지원", "없음", "불가", "지원하지", "제외", "안됨")
_NEGATION = (
    "지원하지않", "지원되지않", "지원안", "미지원", "없습니다", "없음",
    "불가능", "불가합니다", "제공하지않", "되지않습니다", "아닙니다",
    "안됩니다", "지원x",
)
_AFFIRMATION = (
    "지원합니다", "지원됩니다", "지원하며", "지원하고", "가능합니다",
    "있습니다", "제공합니다", "됩니다", "지원",
)
_HEDGE = (
    "확인이필요", "확인후", "안내드리겠", "확인해드리겠", "정확한사양은",
    "담당자", "문의주시면", "확인중", "확인이어렵",
)
#: A text value longer than this is a sentence or a list, and asking an answer
#: to repeat it verbatim would fail a correct reply. Values of this length
#: carry their claim in a tier or a number instead, which is read above.
_MAX_QUOTABLE = 24
_MEMBER_SPLIT = re.compile(r"\s*[,/·]\s*|\s+및\s+")
_LIST_TAIL = re.compile(r"\s*등등?\)?$|\s*기타$")


def _compact(text: object) -> str:
    return re.sub(r"[\s\-_()\[\],·]", "", str(text or "")).lower()


# A full stop between digits is a decimal point. Splitting on it cut
# "674.9mm입니다" into "674" and "9mm입니다", and the sentence that stated the
# value then read as a sentence stating 9mm -- which the conflict check called
# a contradiction of the value it had just quoted.
# A clause connective ends a claim. "일반 밝기는 1,000cd이고 최대 밝기는
# 450cd입니다" is two claims in one sentence, and read whole it looked as if
# both brightnesses were properly attached; Korean marks the join, so the
# join is where the claim ends.
_SENTENCE_SPLIT = re.compile(
    r"(?<!\d)[.!?]+(?!\d)|\n+|(?<=니다)\s+"
    r"|(?<=이고)\s*|(?<=이며)\s*|(?<=하며)\s*|(?<=지만)\s*|(?<=으며)\s*")


def _sentences(answer: object) -> list[str]:
    return [
        part.strip()
        for part in _SENTENCE_SPLIT.split(str(answer or ""))
        if part.strip()
    ]


def _unit_class(unit: object) -> str:
    # The same unit is written several ways across the two sources: the
    # collected knowledge stores "cd/m²" where the registry says "cd/m2",
    # and the superscript made it an unknown class of its own -- so a
    # brightness in cd/m² did not match the same brightness in cd, and the
    # answer that stated it correctly was reported as not stating it.
    token = (
        _compact(unit)
        .replace("/", "")
        .replace("²", "2")
        .replace("㎡", "m2")
        .replace("³", "3")
    )
    if not token:
        return ""
    for family in _UNIT_CLASSES:
        if token in {member.replace("/", "") for member in family}:
            return sorted(family)[0]
    return token


def _tolerance_units(body: str) -> dict[int, str]:
    """Where a parenthesised tolerance stands between a number and its unit.

    "105.0(±5.0)mm" is one millimetre measurement with its tolerance, and
    writing the tolerance in between does not leave 105.0 unitless -- but
    the measurement pattern reads a unit only where it touches the number,
    so that is exactly what happened: the stated height was found as a bare
    "105.0" and an answer carrying it did not count as stating "105.0mm".
    Measured on 162413382, where the record holds "105.0 (±5.0) mm" and the
    draft wrote "105.0(±5.0)mm".

    The unit is given to the tolerance's number as well, which is what makes
    a *wrong* tolerance still fail: "105.0(±10.0)mm" then states 10.0 as a
    length the value does not have, and the conflict check reports it.

    Returns the offset of each such number against the unit it belongs to.
    """

    found: dict[int, str] = {}
    for match in _TOLERANCE_UNIT.finditer(body):
        unit = _unit_class(match.group("unit"))
        if not unit:
            continue
        found[match.start("lead")] = unit
        found[match.start("tolerance")] = unit
    return found


def _measures(text: object) -> list[tuple[str, str]]:
    """``(number, unit class)`` for each measurement stated in the text.

    The unit is the one written against the number; "" when there is none,
    which is a number whose unit the text left open -- except where a
    tolerance stands between the two, which :func:`_tolerance_units` reads.
    """

    body = str(text or "")
    tolerances = _tolerance_units(body)
    found: list[tuple[str, str]] = []
    for match in _MEASURE.finditer(body):
        number = match.group("number").replace(",", "")
        unit = _unit_class(match.group("unit"))
        if not unit:
            unit = tolerances.get(match.start("number"), "")
        # "2개월" is a duration, not two of something: 개 is a counter and
        # 개월 is the word for a month. Read as the counter, a delivery
        # sentence looked like a statement about an HDMI port count.
        if unit == "ea" and body[match.end():match.end() + 1] == "월":
            unit = ""
        found.append((number, unit))
    return found


def _value_measures(value: str, unit: object) -> list[tuple[str, str]]:
    """The value's measurements, with the unit the catalogue knows applied.

    Two gaps are filled. A dimension string states the unit once at the end
    ("1147.1 X 536.4 X 240.0mm"), so a bare number inherits the unit stated
    later in the same value. And a value may be a bare number whose unit is
    stored in the fact's ``unit`` field ("450" with unit "cd/m2").
    """

    measures = _measures(value)
    if not measures:
        return []
    stated = next((item[1] for item in measures if item[1]), "")
    fallback = stated or _unit_class(unit)
    return [(number, found or fallback) for number, found in measures]


@dataclass(frozen=True)
class FactClaim:
    """One cited fact, read as the claim the answer has to keep."""

    field_key: str
    value: str
    unit: str
    kind: str
    measures: tuple[tuple[str, str], ...]
    members: tuple[str, ...]
    subject_terms: tuple[str, ...]


# The words a section rule recognises for a field, taken from the rule
# patterns themselves. ``speaker_present`` has no question wording in the
# registry -- it comes from a catalogue column -- but the section reader knows
# the catalogue writes it as "스피커", and that is the word an answer about it
# uses too. Derived rather than listed, so a rule added in 2-c brings its own
# wording with it.
_WORD_IN_PATTERN = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9.\-]{2,}")
_PATTERN_IGNORED = frozenset({
    "mini", "micro", "else", "none", "true", "false", "spec",
})
#: Words for *having* a thing rather than for the thing had.
#:
#: A section rule spells its match out in full, so the words read back out of
#: one include its bare verb: ``^(OTT지원)$`` yields "OTT" and "지원" alike.
#: A bare verb names no attribute -- it is in every support sentence about
#: every attribute -- so it cannot be what makes a sentence about one.
_GENERIC_SUBJECTS = frozenset({
    "지원", "가능", "사용", "불가", "자체", "기능", "제공", "해당", "support",
})


def _rule_words() -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = {}
    for rules in SECTION_RULES.values():
        for rule in rules:
            if not rule.field:
                continue
            words = [
                word for word in _WORD_IN_PATTERN.findall(rule.pattern.pattern)
                if word.lower() not in _PATTERN_IGNORED
            ]
            if words:
                found.setdefault(rule.field, []).extend(words)
    return {field: tuple(dict.fromkeys(words))
            for field, words in found.items()}


_RULE_WORDS = _rule_words()


def _excluded_terms(field_key: str) -> tuple[str, ...]:
    """Wording that takes a sentence *out* of this attribute.

    The registry already states this for the question side: ``not_when`` is
    how "스탠드 색상" stops being a question about the body colour, and how a
    condition stops being a question about the conditionless value. The
    answer side needs it for the same reason and for the same words.

    Measured: ``power_consumption_labelled`` holds what the source stated
    with no condition ("25W" and nothing else), its own wording is "소비전력",
    and "일반 소비전력" contains it -- so a sentence that named a condition
    the source never stated counted as a sentence about the attribute that
    was cited, and the conditionless figure grounded a conditioned claim.
    The same shape protects ``color`` from a sentence about the stand's
    colour.
    """

    return tuple(dict.fromkeys(
        term for item in SPEC_ATTRIBUTES if item.field_key == field_key
        for term in item.not_when
    ))


def _names_the_attribute(sentence: str, field_key: str) -> bool:
    """Whether this sentence is about this attribute, by its own wording."""

    if any(_states_term(sentence, term)
           for term in _excluded_terms(field_key)):
        return False
    terms = _question_terms(field_key)
    return not terms or any(_states_term(sentence, term) for term in terms)


def _question_terms(field_key: str) -> tuple[str, ...]:
    """Only the wording a customer uses for this attribute.

    Curated, and specific to the attribute: ``hdmi_version_spec`` is asked as
    "HDMI 버전", never as "HDMI" alone. That distinction matters wherever
    there is no delivered value to compare against, because then the wording
    is the only thing deciding whether a sentence is about this attribute at
    all -- and the words a *rule* recognises are far broader ("HDMI", and the
    bare verb "지원" from the pattern ``^(OTT지원)$``), which read every
    support sentence as a claim about every port and about OTT.
    """

    own = [
        term for item in SPEC_ATTRIBUTES if item.field_key == field_key
        for term in item.question_terms
    ]
    if own:
        # The attribute's own wording, and only that. ``brightness_peak``
        # also appears in ``brightness_typical_cd_m2.also_fields`` -- which
        # is how a brightness question asks for both conditions -- and
        # reading those borrowed terms as the peak's own made "일반 밝기는
        # 1,000cd입니다" look like a sentence about the peak.
        return tuple(dict.fromkeys(own))
    borrowed = [
        term for item in SPEC_ATTRIBUTES
        if field_key in item.also_fields or field_key in {
            nested for _label, nested in item.nested}
        for term in item.question_terms
    ]
    if borrowed:
        return tuple(dict.fromkeys(borrowed))
    # The same attribute under the other vocabulary's spelling:
    # ``refresh_rate`` is what the collected knowledge calls
    # ``refresh_rate_spec``, and only the latter carries the wording -- so
    # without this, "주사율은 1,000cd입니다" named no attribute this could
    # recognise.
    stem = _stem(field_key)
    return tuple(dict.fromkeys(
        term for item in SPEC_ATTRIBUTES
        if stem and _stem(item.field_key) == stem
        for term in item.question_terms
    ))


def _catalogue_labels(field_key: str) -> tuple[str, ...]:
    """What the catalogue's own specification calls this attribute.

    The registry keeps two wording lists per attribute and they answer two
    different questions. ``question_terms`` is how a customer asks for it,
    and is deliberately empty for a ``supply_only`` attribute: the question
    side must not route to one from wording alone. ``spec_labels`` is what
    the specification calls it, which is the question the answer side asks --
    what is this attribute named in a sentence.

    Reading only the first left three attributes with no name at all.
    ``wifi_present``, ``bluetooth_present`` and ``stand_type_spec`` are all
    ``supply_only``, so ``_polarity_findings`` had no term to compare and
    reported ``VALUE_CANNOT_BE_VERIFIED`` for every answer that stated one.

    Same three branches as :func:`_question_terms`, for the same reasons: the
    attribute's own labels, then those of an attribute that names this field
    in ``also_fields`` or ``nested`` -- which is how the stored ``speaker``,
    ``wifi`` and ``bluetooth`` reach the registry's ``*_present`` spelling --
    then the same stem, which is what links ``stand_type`` to
    ``stand_type_spec``.
    """

    own = [
        term for item in SPEC_ATTRIBUTES if item.field_key == field_key
        for term in item.spec_labels
    ]
    if own:
        return tuple(dict.fromkeys(own))
    borrowed = [
        term for item in SPEC_ATTRIBUTES
        if field_key in item.also_fields or field_key in {
            nested for _label, nested in item.nested}
        for term in item.spec_labels
    ]
    if borrowed:
        return tuple(dict.fromkeys(borrowed))
    stem = _stem(field_key)
    return tuple(dict.fromkeys(
        term for item in SPEC_ATTRIBUTES
        if stem and _stem(item.field_key) == stem
        for term in item.spec_labels))


def _subject_terms(field_key: str) -> tuple[str, ...]:
    """What this attribute is called, in a question or in the catalogue.

    Wider than :func:`_question_terms`: it adds the catalogue's own labels
    and the words the section rules recognise, which is what lets a delivered
    value be found in a sentence that uses the catalogue's wording rather
    than the customer's.

    Those rule words carry the bare verb of having the thing, and a verb is
    not a subject. ``ott_support_spec`` was called "지원" among other things,
    which made "미러링 기능을 지원합니다" a sentence about OTT: the answer to
    692741190 then read as contradicting ``ott_support_spec = 미지원`` while
    its own OTT sentence agreed with it, because polarity settles on the
    first sentence it finds and the mirroring one came first. The same
    pairing is in 692590688, which was not withheld and auto-posted.

    The filter applies to both derived lists. :func:`_question_terms` is
    curated per attribute and holds none of these; of the 164 fields this
    module can recognise, two carry such a word, and neither is left without
    a term that names it, so nothing becomes unrecognisable by this.
    """

    return tuple(dict.fromkeys((
        *_question_terms(field_key),
        *(word for word in _catalogue_labels(field_key)
          if word.lower() not in _GENERIC_SUBJECTS),
        *(word for word in _RULE_WORDS.get(field_key, ())
          if word.lower() not in _GENERIC_SUBJECTS),
    )))


def _negated_subject(value: str) -> str:
    """What a negative value is about, with its negation removed."""

    # Punctuation left behind by the removal is not part of the subject:
    # "없음. 고객 자가 설치모델" left ". 고객 자가 설치모델", whose leading
    # stop made the subject unmatchable against any sentence.
    return re.sub(
        "|".join(_NEGATIVE_MARKERS), " ", value).strip(" ().,/-·:;")


def _members(value: str) -> tuple[str, ...]:
    parts = [
        _LIST_TAIL.sub("", part).strip()
        for part in _MEMBER_SPLIT.split(value)
    ]
    return tuple(part for part in parts if part)


def classify_fact(field_key: str, value: object, unit: object = None) -> FactClaim:
    """Read a delivered fact as the shape of claim it makes."""

    text = str(value or "").strip()
    unit_text = str(unit or "") or str(SPEC_UNITS.get(field_key) or "")
    measures = tuple(_value_measures(text, unit_text))
    subject = _subject_terms(field_key)
    compact = _compact(text)
    if compact in _POLARITY_VALUES:
        # The whole value is a yes or a no, including a bare "없음". The
        # subject is not in the value, so it is read from the registry's own
        # question wording rather than by stripping the negation.
        kind = POLARITY
    elif any(marker in text for marker in _NEGATIVE_MARKERS):
        kind = NEGATIVE
    elif field_key in LIST_FIELDS and len(_members(text)) > 1:
        kind = LIST
    elif field_key in LADDER_FIELDS:
        kind = LADDER
    elif len(measures) > 1:
        kind = COMPOUND
    elif measures:
        kind = NUMERIC
    else:
        kind = TEXT
    members = _members(text)
    if kind == NEGATIVE:
        # What the row denies, so a sentence about it counts as a sentence
        # about this fact: "터치 미지원" is about 터치, and the answer that
        # contradicts it says "터치를 지원합니다" without repeating the value.
        denied = _negated_subject(text)
        members = tuple(filter(None, (denied,))) or members
    return FactClaim(
        field_key=str(field_key), value=text, unit=unit_text, kind=kind,
        measures=measures, members=members, subject_terms=subject,
    )


def _states_measure(text: str, number: str, unit_class: str) -> bool:
    """Whether the text states this number under the value's unit.

    A measurement is a number *and* a unit. "스탠드 다리 사이 간격은 674.9
    입니다" leaves out what makes 674.9mm a length, and read as the value it
    would equally be centimetres -- which is the error this whole check
    exists for. So a value whose unit the catalogue knows requires the answer
    to state a compatible one; only a genuinely unitless value (a contrast
    ratio, a resolution) may be answered as a bare number.
    """

    for found_number, found_unit in _measures(text):
        if found_number != number:
            continue
        if not unit_class:
            return True
        if found_unit == unit_class:
            return True
    return False


def _conflicting_measure(
    text: str, measures: Sequence[tuple[str, str]],
    known: Iterable[tuple[str, str]] = (),
) -> tuple[str, str] | None:
    """A measurement in the text that this value's unit contradicts.

    ``known`` is every measurement the cited facts state. A sentence may
    carry two of them -- "일반 밝기는 450cd이고 최대 밝기는 1,000cd입니다" --
    and the other attribute's figure is not a contradiction of this one.
    """

    allowed = {number for number, _unit in measures}
    classes = {unit for _number, unit in measures if unit}
    others = {(number, unit) for number, unit in known}
    for number, unit in _measures(text):
        if not unit or unit not in classes or number in allowed:
            continue
        if (number, unit) in others:
            continue
        return (number, unit)
    return None


def _hedged(sentence: str) -> bool:
    return any(marker in _compact(sentence) for marker in _HEDGE)


def _negated(sentence: str) -> bool:
    return any(marker in _compact(sentence) for marker in _NEGATION)


def _affirmed(sentence: str) -> bool:
    compact = _compact(sentence)
    return (not _negated(sentence)) and any(
        marker in compact for marker in _AFFIRMATION)


def _spaced(text: object) -> str:
    """Lowercased, with separators turned into single spaces.

    Keeps word boundaries where compaction destroys them: "G-Sync
    compatible" becomes "g sync compatible", so the term "g-sync" matches it
    and the term "sync" does not match "freesync".
    """

    return " ".join(
        re.sub(r"[-_/()\[\],·]+", " ", str(text or "").lower()).split())


def _states_term(text: str, term: str) -> bool:
    """Whether the text uses this wording for an attribute.

    A Latin term is matched on word boundaries and a Korean one as a
    substring, because Korean attaches its particles directly. Without the
    boundary, "Sync" -- the word a section rule uses for Adaptive Sync --
    matched inside "FreeSync Premium Pro", and an answer about one
    certification read as a claim about the other.
    """

    token = str(term or "").strip()
    if not token:
        return False
    if token.isascii():
        spaced = _spaced(token)
        if not spaced:
            return False
        pattern = re.compile(
            r"(?<![0-9a-z])" + re.escape(spaced) + r"(?![0-9a-z])")
        return bool(pattern.search(_spaced(text)))
    return _compact(token) in _compact(text)


def _mentions(sentence: str, claim: FactClaim) -> bool:
    """Whether this sentence is talking about the attribute at all."""

    compact = _compact(sentence)
    if claim.value and _compact(claim.value) in compact:
        return True
    # A sentence-long value is never repeated whole, so its opening words are
    # what a sentence about it shares: "스탠드형(고정형) / 벽걸이 …" is about
    # 스탠드형.
    head = _compact(re.split(r"[\s/(,]", claim.value.strip())[0])
    if len(head) >= 2 and head in compact:
        return True
    if any(_states_term(sentence, term) for term in claim.subject_terms):
        return True
    if any(_compact(member) in compact for member in claim.members):
        return True
    classes = {unit for _number, unit in claim.measures if unit}
    return any(unit in classes for _number, unit in _measures(sentence))


def _relevant_sentences(answer: str, claim: FactClaim) -> list[str]:
    return [item for item in _sentences(answer) if _mentions(item, claim)]


def _asserts_anything(answer: str) -> bool:
    """Whether any unhedged sentence states something as fact."""

    for sentence in _sentences(answer):
        if _hedged(sentence):
            continue
        if _measures(sentence) or any(
            marker in _compact(sentence)
            for marker in (*_AFFIRMATION, *_NEGATION, "입니다")
        ):
            return True
    return False


def _finding(claim: FactClaim, reason: str, detail: str = "") -> dict[str, str]:
    found = {
        "field_key": claim.field_key,
        "delivered_value": claim.value,
        "value_shape": claim.kind,
        "reason": reason,
    }
    if detail:
        found["detail"] = detail
    return found


def _acceptable_measures(
    claim: FactClaim,
) -> tuple[tuple[str, str], ...]:
    """Which of the value's measurements the answer may state.

    One measurement, one claim -- except where a value states the same
    quantity twice in different units. "123.8cm(49인치)" is one screen size
    written both ways, and an answer that gives either has given the value.
    Where the measurements share a unit class they are not alternatives:
    "10bit (8bit + FRC)" is a 10-bit panel described by how it gets there, and
    the 8 cannot stand in for the 10, which is how "12bit (8bit + FRC)"
    passed. The same holds for a width, a height and a depth in one string.
    """

    classes = {unit for _number, unit in claim.measures}
    if len(claim.measures) > 1 and len(classes) == len(claim.measures):
        return claim.measures
    return claim.measures[:1]


def _attached_to_another_attribute(
    answer: str, claim: FactClaim, siblings: Mapping[str, Sequence[str]],
) -> bool:
    """Whether the value appears only beside some *other* attribute's name.

    The value check asks whether the figure is in the answer; this asks what
    the answer says it is. ``brightness_peak`` 1,000cd cited and answered
    "일반 밝기는 1,000cd입니다" states the number and attributes it to the
    typical brightness -- and with both brightnesses cited, nothing else
    would have caught it.

    Silent where it cannot tell: an attribute with no question wording, or an
    answer that names no attribute at all ("674.9mm입니다"), is left alone.
    """

    own = [term for term in _question_terms(claim.field_key) if term]
    if not own:
        return False
    acceptable = _acceptable_measures(claim)
    if claim.measures:
        carrying = [
            sentence for sentence in _sentences(answer)
            if any(_states_measure(sentence, number, unit)
                   for number, unit in acceptable)
        ]
    else:
        carrying = [
            sentence for sentence in _sentences(answer)
            if _compact(claim.value) in _compact(sentence)
        ]
    if not carrying:
        return False
    # A sentence that names a condition this attribute does not carry is not
    # a sentence about it, however much of its wording it repeats.
    if any(_names_the_attribute(sentence, claim.field_key)
           for sentence in carrying):
        return False
    for sentence in carrying:
        for field, terms in siblings.items():
            if same_attribute(field, claim.field_key):
                continue
            if any(_states_term(sentence, term) for term in terms if term):
                return True
        # Nothing delivered claims this sentence, and the registry says this
        # wording is not this attribute's. The figure is being attributed to
        # a condition the source never stated -- a bare "25W" answered as
        # "일반 소비전력은 25W입니다" -- which is the same error as attributing
        # it to a sibling, except that here there is no sibling to attribute
        # it to, so the sibling loop above could never see it.
        if any(_states_term(sentence, term)
               for term in _excluded_terms(claim.field_key)):
            return True
    return False


def _numeric_findings(
    answer: str, claim: FactClaim, definite: Sequence[str],
    siblings: Mapping[str, Sequence[str]] | None = None,
    known_measures: Iterable[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    """The measurement has to survive, with its unit."""

    acceptable = _acceptable_measures(claim)
    principal_number, principal_unit = acceptable[0]
    stated = any(
        _states_measure(answer, number, unit) for number, unit in acceptable)
    if stated and _attached_to_another_attribute(
            answer, claim, siblings or {}):
        return [_finding(
            claim, "VALUE_ATTACHED_TO_ANOTHER_ATTRIBUTE",
            f"{principal_number}{principal_unit or ''}",
        )]
    if not stated:
        if not definite and not _asserts_anything(answer):
            return []
        return [_finding(
            claim, "VALUE_NOT_STATED_IN_ANSWER",
            f"{principal_number}{principal_unit or ''}",
        )]
    for sentence in definite:
        conflict = _conflicting_measure(
            sentence, claim.measures, known_measures)
        if conflict:
            return [_finding(
                claim, "ANSWER_STATES_A_DIFFERENT_VALUE",
                f"{conflict[0]}{conflict[1]}",
            )]
        if any(_states_measure(sentence, number, unit)
               for number, unit in acceptable) and _negated(sentence):
            return [_finding(claim, "ANSWER_NEGATES_THE_VALUE")]
    return []


# What a Korean sentence says is supported, and what a question asks about.
# Both read the words in front of the support verb and drop the particle, so
# no list of member names is needed anywhere: "애플TV를 지원합니다" names
# 애플TV and "애플TV 되나요?" asks about 애플TV, whatever 애플TV happens to be.
_SUPPORT_VERB = re.compile(
    r"(?:지원|제공|가능|사용)(?:합니다|됩니다|하며|하고|해요|한가요|하나요|"
    r"되나요|가능한가요|할\s*수\s*있)?|되나요|있나요|있습니까|되는가요|"
    r"있을까요|되나요"
)
_PARTICLE = re.compile(
    r"(?:은|는|이|가|을|를|도|만|과|와|랑|이랑|의|에서|에|으로|로)$"
)
_OBJECT_SPLIT = re.compile(r"\s*(?:,|/|·|그리고|및|와|과)\s*")
# Words that are never the item being asked about: they are how the question
# is phrased, not what it is about.
_PHRASING = frozenset({
    "이", "그", "저", "제품", "상품", "모델", "기능", "여부", "단자", "포트",
    "앱", "어플", "어플리케이션", "애플리케이션", "서비스", "모두", "전부",
    "다", "혹시", "지금", "현재", "해당", "본", "이번", "정말", "진짜",
})


def _objects_before_support(text: object) -> tuple[str, ...]:
    """The things a sentence or question says are supported.

    Takes the words in front of each support verb, drops the Korean particle
    and splits a conjunction, so "넷플릭스와 유튜브를 지원합니다" names both.
    """

    found: list[str] = []
    body = str(text or "")
    for match in _SUPPORT_VERB.finditer(body):
        head = body[:match.start()].strip()
        if not head:
            continue
        # The clause this verb belongs to, not the whole sentence.
        # A decimal point is not a clause boundary: splitting on it left
        # "2를" of "HDMI 2.2를", which named nothing.
        head = re.split(
            r"(?<!\d)[,.!?](?!\d)|\s+(?:그리고|하지만|다만)\s+", head)[-1]
        for part in _OBJECT_SPLIT.split(head):
            token = _PARTICLE.sub("", part.strip()).strip()
            if not token:
                continue
            # The item is the last word in front of the verb, not the whole
            # clause: "이 제품은 OTT를 지원합니다" is about OTT. Where that
            # word is too short to name anything ("HDMI 2"), the two last
            # words are taken together.
            words = [
                _PARTICLE.sub("", word).strip() for word in token.split()
            ]
            words = [word for word in words if word]
            if not words:
                continue
            candidate = words[-1]
            if len(candidate) < 2 and len(words) > 1:
                candidate = " ".join(words[-2:])
            if len(candidate) < 2 or candidate in _PHRASING:
                continue
            found.append(candidate)
    return tuple(dict.fromkeys(found))


def _names_the_field(token: str, claim: FactClaim) -> bool:
    """Whether this token names the attribute rather than one of its items.

    "OTT를 지원합니다" against an app list is a statement about the list, not
    about a member missing from it. The attribute's own name is what decides,
    so nothing has to be enumerated: ``ott_apps_spec`` yields ott/apps/spec.
    """

    compact = _compact(token)
    if not compact:
        return False
    # Only the field's own name. The registry's question wording cannot be
    # used here: for a list, those terms *are* item names ("넷플릭스",
    # "라프텔"), so consulting them would excuse exactly the items this is
    # meant to catch.
    parts = [part for part in re.split(r"[_\s]+", claim.field_key) if part]
    # The attribute's name, however the question or the answer wraps it:
    # GPT ①'s "애플TV 등 OTT 앱 지원 여부" yields the candidate "OTT 앱",
    # which names the list rather than an item missing from it.
    return any(
        len(part) >= 3 and (
            compact in {part.lower(), part.lower() + "s"}
            or part.lower() in compact)
        for part in parts
    )


def _listed(claim: FactClaim, token: str) -> bool:
    """Whether the delivered list contains this item."""

    compact = _compact(token)
    if not compact:
        return False
    return any(
        compact == _compact(member)
        or compact in _compact(member)
        or _compact(member) in compact
        for member in claim.members
    )


def _list_findings(
    answer: str, claim: FactClaim, definite: Sequence[str],
    asked: Sequence[str] = (),
) -> list[dict[str, str]]:
    """The item the answer talks about has to match the membership.

    Three readings, none of which needs a vocabulary of item names:

    * an item the value lists, denied -- "쿠팡플레이는 지원하지 않습니다"
      against a list containing it;
    * an item the answer says is supported that the value does not list --
      read from the answer's own grammar, so 애플TV is caught exactly as
      라프텔 is, without either being registered anywhere;
    * an item the *customer* asked about that the value does not list, where
      the answer affirms support without naming it ("지원합니다").
    """

    findings: list[dict[str, str]] = []
    for sentence in definite:
        compact = _compact(sentence)
        for member in claim.members:
            if _compact(member) in compact and _negated(sentence):
                findings.append(_finding(
                    claim, "LIST_MEMBER_DENIED", member))
        if _negated(sentence):
            # A denial that names nothing is still a denial of what was
            # asked: "쿠팡플레이 되나요?" answered "지원하지 않습니다." about a
            # list that contains it.
            for token in asked:
                if _listed(claim, token):
                    findings.append(_finding(
                        claim, "LIST_MEMBER_DENIED", token))
            continue
        if not _affirmed(sentence):
            continue
        for token in _objects_before_support(sentence):
            if _listed(claim, token) or _names_the_field(token, claim):
                continue
            findings.append(_finding(
                claim, "LIST_MEMBER_NOT_DELIVERED", token))
        # The asked item only has to be judged when the answer does not say
        # which item it means. "디즈니플러스 되나요?" answered with
        # "디즈니+를 지원합니다" has named a listed item, and the two
        # spellings are the customer's and the catalogue's.
        if any(_compact(member) in compact for member in claim.members):
            continue
        for token in asked:
            if _listed(claim, token) or _names_the_field(token, claim):
                continue
            findings.append(_finding(
                claim, "LIST_MEMBER_NOT_DELIVERED", token))
    # One finding per reason, with the fullest spelling of the item. The
    # question and the list can write one name differently -- a Korean
    # particle stripper leaves "쿠팡플레" where the list says "쿠팡플레이" --
    # and that is one problem reported once, not two.
    best: dict[str, dict[str, str]] = {}
    for item in findings:
        current = best.get(item["reason"])
        if current is None or len(item.get("detail", "")) > len(
                current.get("detail", "")):
            best[item["reason"]] = item
    return list(best.values())


# One colour, spelled two ways. The catalogue keeps its own spelling -- for
# LS32DM501EKXKR ``stand_color`` is stored as "White" -- and the answer writes
# it in Korean. On inquiry 11238 the fact was delivered, cited, recorded USED
# and cleared by the validator, and then failed only this string comparison:
# "White" was not found in "모니터와 스탠드 모두 화이트 구성입니다", so the
# grounding reported VALUE_NOT_STATED_IN_ANSWER and auto-post was refused as
# PRODUCT_FACT_NOT_VERIFIED.
#
# A closed list, and only for colour. This is not a similarity relaxation: a
# value outside these groups is compared character for character exactly as
# before, two different groups never match each other, and the answer still
# has to contain one of the spellings -- an answer that does not name the
# colour stays unverified.
_COLOUR_SPELLINGS: tuple[frozenset[str], ...] = (
    frozenset({"white", "화이트", "흰색", "백색"}),
    frozenset({"black", "블랙", "검정", "검정색", "검은색"}),
    frozenset({"silver", "실버", "은색"}),
    frozenset({"gray", "grey", "그레이", "회색"}),
    frozenset({"beige", "베이지"}),
    frozenset({"ivory", "아이보리"}),
    frozenset({"navy", "네이비", "남색"}),
    frozenset({"blue", "블루", "파란색"}),
    frozenset({"green", "그린", "초록", "초록색"}),
    frozenset({"pink", "핑크", "분홍", "분홍색"}),
    frozenset({"red", "레드", "빨간색"}),
    frozenset({"gold", "골드", "금색"}),
)
# Read from the field, not only the value: a colour group may only speak for a
# field that states a colour, so no other attribute can borrow it.
_COLOUR_FIELD = re.compile(r"(?:^|_)colou?r(?:$|_)")


def _colour_spellings(claim: "FactClaim") -> frozenset[str]:
    """Spellings that mean this claim's colour, or empty when it is not one."""

    if not _COLOUR_FIELD.search(str(claim.field_key or "").lower()):
        return frozenset()
    value = _compact(claim.value)
    for group in _COLOUR_SPELLINGS:
        if value in group:
            return group
    return frozenset()


def _text_findings(
    answer: str, claim: FactClaim, definite: Sequence[str],
) -> list[dict[str, str]]:
    """A tier or a short text value has to appear, and stay affirmed."""

    compact_value = _compact(claim.value)
    if not compact_value:
        return []
    if len(compact_value) > _MAX_QUOTABLE:
        # A sentence-long value cannot be checked by quoting, and an answer
        # that asserts something about it while citing it is then taking
        # authority from a value nothing verified. It goes to staff instead
        # of counting as grounded.
        if definite:
            return [_finding(
                claim, "VALUE_CANNOT_BE_VERIFIED", claim.value[:60])]
        return []
    if compact_value not in _compact(answer):
        # The same colour under its other spelling counts as stated, and the
        # spelling the answer used becomes the one the negation check below
        # looks for -- "화이트가 아닙니다" still negates "White".
        spoken = next(
            (
                spelling for spelling in sorted(_colour_spellings(claim))
                if spelling in _compact(answer)
            ),
            "",
        )
        if not spoken:
            if not definite and not _asserts_anything(answer):
                return []
            return [_finding(claim, "VALUE_NOT_STATED_IN_ANSWER", claim.value)]
        compact_value = spoken
    for sentence in definite:
        if compact_value in _compact(sentence) and _negated(sentence):
            return [_finding(claim, "ANSWER_NEGATES_THE_VALUE")]
    return []


def _polarity_findings(
    answer: str, claim: FactClaim, definite: Sequence[str],
) -> list[dict[str, str]]:
    """The value is the yes; an answer may not make it a no.

    A yes or a no is only checkable against an answer if something says what
    the subject *is*. 33 fields have no wording anywhere -- ``hdmi_cec``,
    ``universal_guide``, ``bixby`` and the like arrive from the collected
    knowledge as a bare "있음" -- and for those this cannot compare anything,
    so it reports that rather than passing. A citation that cannot be checked
    does not become authority to post.
    """

    if not claim.subject_terms:
        # Only where the answer states a yes or a no of its own. An answer
        # about delivery cites nothing of this attribute and asserts nothing
        # about it, so there is nothing to withhold.
        if any(_affirmed(sentence) or _negated(sentence)
               for sentence in _sentences(answer)
               if not _hedged(sentence)):
            return [_finding(
                claim, "VALUE_CANNOT_BE_VERIFIED", claim.value[:60])]
        return []
    positive = _compact(claim.value) not in {
        "없음", "미지원", "불가", "불가능", "n", "x", "no", "false",
        "해당없음",
    }
    for sentence in definite:
        if not any(_states_term(sentence, term)
                   for term in claim.subject_terms):
            continue
        if positive and _negated(sentence):
            return [_finding(claim, "POLARITY_CONTRADICTED", claim.value)]
        if not positive and _affirmed(sentence):
            return [_finding(claim, "POLARITY_CONTRADICTED", claim.value)]
    return []


def _negative_findings(
    answer: str, claim: FactClaim, definite: Sequence[str],
) -> list[dict[str, str]]:
    """The row says the product does not have it; the answer may not add it."""

    subject = _compact(_negated_subject(claim.value))
    if not subject or len(subject) > _MAX_QUOTABLE:
        return []
    for sentence in definite:
        compact = _compact(sentence)
        if subject not in compact:
            continue
        if _negated(sentence):
            return []
        if _affirmed(sentence):
            return [_finding(
                claim, "ANSWER_CONTRADICTS_A_NEGATIVE_VALUE", claim.value)]
    return []


def _normalise(entry: Any) -> list[tuple[str, str]]:
    """``[(value, unit)]`` from whatever shape the caller holds a fact in."""

    if entry is None:
        return []
    if isinstance(entry, Mapping):
        return [(str(entry.get("value") or ""), str(entry.get("unit") or ""))]
    if isinstance(entry, tuple) and len(entry) == 2:
        return [(str(entry[0] or ""), str(entry[1] or ""))]
    if isinstance(entry, (list, tuple, set, frozenset)):
        out: list[tuple[str, str]] = []
        for item in entry:
            out.extend(_normalise(item))
        return out
    return [(str(entry), "")]


def unsupported_fact_claims(
    answer: object, facts: Mapping[str, Any],
    *, asked: object = "", siblings: Iterable[str] = (),
) -> list[dict[str, str]]:
    """Cited facts whose value the answer does not keep.

    ``facts`` maps a field key GPT ② said it used to the value the prompt
    delivered for it -- a scalar, a ``(value, unit)`` pair, a mapping with
    those keys, or a list of several when the record states the same field in
    more than one notation ("240Hz", "Max 240", "240"). Several notations are
    alternatives: stating any one of them keeps the fact.

    ``asked`` is what the customer asked, with GPT ①'s reading of it. It
    matters for a list: "애플TV 되나요?" names the item the answer is about
    even when the answer only says "지원합니다", and the list either contains
    it or does not.

    Returns one finding per failing field, naming the field, the delivered
    value, the shape it was read as and which rule it broke.
    """

    body = str(answer or "")
    if not body.strip():
        return []
    asked_items = _objects_before_support(asked)
    # What else the prompt delivered, so a value said of another attribute is
    # recognisable as said of another attribute.
    sibling_terms = {
        str(field): _question_terms(str(field))
        for field in (*facts, *siblings)
    }
    cited_measures = {
        measure
        for measures in measures_of(facts).values()
        for measure in measures
    }
    findings: list[dict[str, str]] = []
    for field_key, entry in facts.items():
        alternatives = [
            classify_fact(field_key, value, unit)
            for value, unit in _normalise(entry)
            if str(value or "").strip()
        ]
        if not alternatives:
            continue
        per_alternative: list[list[dict[str, str]]] = []
        for claim in alternatives:
            relevant = _relevant_sentences(body, claim)
            if claim.kind == LIST and asked_items:
                # "지원합니다" names nothing, and with the asked item in hand
                # that is still an answer about this list.
                relevant = [
                    sentence for sentence in _sentences(body)
                    if sentence in relevant or _affirmed(sentence)
                    or _negated(sentence)
                ]
            definite = [
                sentence for sentence in relevant if not _hedged(sentence)
            ]
            if claim.kind == NEGATIVE:
                found = _negative_findings(body, claim, definite)
            elif claim.kind == LIST:
                found = _list_findings(
                    body, claim, definite, asked=asked_items)
            elif claim.kind in {NUMERIC, COMPOUND}:
                found = _numeric_findings(
                    body, claim, definite, siblings=sibling_terms,
                    known_measures=cited_measures)
            elif claim.kind == POLARITY:
                found = _polarity_findings(body, claim, definite)
            else:
                found = _text_findings(body, claim, definite)
            per_alternative.append(found)
        # One notation surviving is the fact surviving; the finding reported
        # when none does is the first one, so the log names a single reason.
        if all(per_alternative) and per_alternative:
            findings.extend(per_alternative[0])
    return findings

def _asserts_about(claim: FactClaim, sentence: str) -> bool:
    """Whether this sentence makes a claim about this attribute.

    Stricter than :func:`_mentions`: a shared unit does not count. "2개월 내
    배송" states a quantity in the same unit as "HDMI 2개" and says nothing
    about HDMI, and this question -- did the answer make a product claim at
    all -- must not be answered by a coincidence of units.
    """

    compact = _compact(sentence)
    if not compact:
        return False
    # A measurement-only value ("2개", "240Hz") is not evidence that the
    # sentence is about this attribute: the same figure appears in sentences
    # about anything. The attribute's own wording decides those.
    if claim.value and not claim.measures and (
            _compact(claim.value) in compact):
        return True
    if any(_states_term(sentence, term) for term in claim.subject_terms):
        return True
    if claim.measures and any(
        _states_measure(sentence, number, unit)
        for number, unit in _acceptable_measures(claim)
    ) and any(_states_term(sentence, term) for term in claim.subject_terms):
        return True
    # Members are only items for a list (and the denied subject for a
    # negative value). For anything else they are just the value split up,
    # and "2개" of "HDMI 2개" matched "2개월" in a delivery sentence.
    if claim.kind not in {LIST, NEGATIVE}:
        return False
    return any(
        _compact(member) in compact for member in claim.members
        if len(_compact(member)) >= 2
    )


# Two subjects before one figure: "일반 밝기와 최대 밝기는 1,000cd입니다"
# states one measurement of two attributes. Korean joins them with 와/과/및 or
# a comma, and the figure follows, so the join is what this looks for -- not a
# list of attribute names.
_CO_SUBJECT_JOIN = re.compile(r"\s*(?:와|과|및|,)\s*")
_SUBJECT_MARK = re.compile(r"(?:은|는|이|가)\s*(?=[^가-힣]*\d)")
# A claim that reuses the previous figure instead of restating it.
_SAMENESS = ("동일", "같습니다", "같음", "마찬가지", "동급", "같고", "같으며")


def _co_subjects(sentence: str) -> int:
    """How many subjects the clause puts in front of its figure.

    One is the ordinary case. Two or more means a single measurement is being
    stated of two attributes at once, and then each of them needs its own
    citation -- which is why this counts rather than naming them.
    """

    mark = _SUBJECT_MARK.search(sentence)
    head = sentence[: mark.start()] if mark else sentence
    parts = [
        part.strip() for part in _CO_SUBJECT_JOIN.split(head)
        if len(part.strip()) >= 2
    ]
    return len(parts)


def _states_sameness(sentence: str) -> bool:
    return any(marker in _compact(sentence) for marker in _SAMENESS)


def _figures_belong_to_a_cited_attribute(
    sentence: str, figures: Sequence[tuple[str, str]],
    grounded_by_field: Mapping[str, set[tuple[str, str]]],
) -> bool:
    """Whether every figure here is a cited attribute's, said of it alone.

    Three conditions, each for a measured failure. The figures have to be the
    cited attribute's, or a sentence that adds a figure of its own rides on
    another claim's citation. The sentence has to name that attribute, or the
    right number attaches to the wrong one. And the sentence must put only
    *that* attribute in front of the figure: "일반 밝기와 최대 밝기는
    1,000cd입니다" named the cited peak and was published with the typical
    brightness sharing its number, as was "최대 밝기와 주사율은 1,000cd입니다".
    """

    if _co_subjects(sentence) > 1:
        return False
    for field, measures in grounded_by_field.items():
        if not all(item in measures for item in figures):
            continue
        if _names_the_attribute(sentence, field):
            return True
    return False


def uncited_product_claims(
    answer: object, facts: Mapping[str, Any], *, asked: object = "",
    grounded_measures: Mapping[str, Iterable[tuple[str, str]]] | None = None,
) -> list[dict[str, str]]:
    """Definite claims about delivered facts that the answer cites nothing for.

    The value check reads the rows GPT ② said it used. A model that reports
    *no* rows was therefore not checked at all, which is how two answers that
    contradicted the catalogue were published: asked "색을 몇 비트로
    표현하나요?" with ``color_bit_depth`` in the prompt, GPT ② returned
    ``used_product_facts: []`` and "12비트 색심도를 지원합니다"; asked "C 단자
    있나요?" with ``usb_c_present`` delivered, it returned no citations and
    "USB-C 단자는 지원하지 않습니다". Both were GENERATED and
    auto-answerable.

    So this answers a different question: does the answer state something
    about an attribute the prompt carried, without citing anything? The
    sentence has to be about that attribute -- its own value, its wording or
    one of its items -- and has to assert rather than hedge. A delivery or
    policy answer mentions none of them and is not affected.

    This never says the answer is *wrong*; it says nothing was offered as its
    evidence, which for a product-specific claim is a matter for staff.
    """

    body = str(answer or "")
    if not body.strip():
        return []
    asked_text = str(asked or "")
    # A citation covers an attribute, never a sentence. One sentence can make
    # two claims -- "USB C타입 단자와 HDMI 단자를 모두 지원합니다" -- and
    # exempting the sentence because one of them was cited let the other ride
    # along uncited.
    #
    # What a citation accounts for is the figures of *its own* attribute, in a
    # sentence that is about that attribute. The figure alone is not enough:
    # with ``brightness_peak`` 1,000cd cited, "일반 밝기는 1,000cd입니다" had
    # every figure grounded and was published -- the right number attached to
    # another attribute. And a sentence that adds a figure of its own --
    # "HDMI를 지원하며 HDMI 9.9를 지원합니다" -- is making a second claim.
    grounded_by_field = {
        str(field): {(number, unit) for number, unit in measures}
        for field, measures in (grounded_measures or {}).items()
    }
    findings: list[dict[str, str]] = []
    for field_key, entry in facts.items():
        for value, unit in _normalise(entry):
            if not str(value or "").strip():
                continue
            claim = classify_fact(field_key, value, unit)
            sentences = []
            for sentence in _sentences(body):
                if _hedged(sentence) or not _asserts_about(claim, sentence):
                    continue
                figures = _measures(sentence)
                if figures and _figures_belong_to_a_cited_attribute(
                        sentence, figures, grounded_by_field):
                    continue
                if (claim.measures and not figures
                        and not _states_sameness(sentence)):
                    # A quantity is not asserted by a sentence that states no
                    # number. "USB C타입 단자와 HDMI 단자를 모두 지원합니다"
                    # is a presence claim, and reading it as a claim about
                    # ``hdmi_port_count`` held an answer that cited both
                    # presences correctly.
                    #
                    # Unless the sentence carries the figure over by
                    # reference: "최대 밝기는 1,000cd이고 일반 밝기도
                    # 동일합니다" states the typical brightness without
                    # writing a number, and it was published.
                    continue
                if (_affirmed(sentence) or _negated(sentence) or figures
                        or _states_sameness(sentence)):
                    # "일반 밝기도 동일합니다" asserts a value as plainly as a
                    # number does; it just borrows the number from the clause
                    # before it.
                    sentences.append(sentence)
            if not sentences:
                continue
            findings.append({
                "field_key": claim.field_key,
                "delivered_value": claim.value,
                "value_shape": claim.kind,
                "reason": "PRODUCT_CLAIM_WITHOUT_CITATION",
                "detail": sentences[0][:60],
            })
            break
    # The asked attribute first, so the log leads with what was asked about.
    if asked_text:
        asked_items = {_compact(item) for item in
                       _objects_before_support(asked_text)}
        findings.sort(key=lambda item: not any(
            token and token in _compact(item["delivered_value"])
            for token in asked_items))
    return findings

# How the two vocabularies spell one attribute. The catalogue reader appends
# what it measured in ("_spec", "_cd_m2", "_ratio"), the collected knowledge
# does not, so ``freesync`` and ``freesync_spec`` are one attribute and
# ``contrast`` and ``contrast_ratio`` are another. Removing the descriptor is
# enough to see that, and it says nothing about two different attributes:
# ``hdmi_present`` and ``hdmi_port_count_spec`` keep their own stems, because
# having a port and having two of them are different claims.
_DESCRIPTOR_SUFFIXES = (
    "_spec", "_cd_m2", "_ratio", "_percent", "_mm", "_kg", "_m2",
)


def _stem(field_key: str) -> str:
    stem = str(field_key or "").strip().lower()
    changed = True
    while changed:
        changed = False
        for suffix in _DESCRIPTOR_SUFFIXES:
            if stem.endswith(suffix) and len(stem) > len(suffix):
                stem = stem[: -len(suffix)]
                changed = True
    return stem


def _declared_same_as(field_key: str) -> frozenset[str]:
    """The field names the registry states are this same attribute.

    Curated per pair in :attr:`SpecAttribute.same_as`; nothing here is
    derived from a name.
    """

    return frozenset(
        str(name) for item in SPEC_ATTRIBUTES
        if item.field_key == str(field_key)
        for name in getattr(item, "same_as", ())
    )


def same_attribute(field_a: str, field_b: str) -> bool:
    """Whether two field keys name the same attribute.

    A citation covers the attribute it names, not the answer. One attribute
    can arrive under two keys -- the catalogue reader appends what it measured
    in -- and then a claim about either is cited by either. Nothing else is
    the same attribute.

    Deliberately only the spelling. An earlier version also accepted an
    overlap in the registry's question wording, and that wording is shared by
    attributes that are not the same thing at all: it made
    ``hdmi_present`` cover ``hdmi_version_spec`` (so a draft citing "the set
    has HDMI" could assert "HDMI 9.9" and post), ``brightness_typical`` cover
    ``brightness_peak``, ``freesync_spec`` cover ``gsync_spec``,
    ``ott_support_spec`` cover ``ott_apps_spec`` and ``color`` cover
    ``rear_color``. Having a port, having two of them and having revision 2.1
    are three claims.

    The spelling rule misses one real case, so the registry states that case
    per pair in ``same_as``. The catalogue's spec text and the collected
    knowledge name the stand's movement differently -- ``tilt_range_spec``
    against ``tilt``, ``elevation_range_spec`` against ``height_adjust`` --
    and ``_stem`` cannot join them without stripping ``_range``, which would
    also join attributes that are not the same. ``same_as`` is read in both
    directions and is not ``also_fields``: that relation is a routing one and
    declares two of the pairings named above.
    """

    if str(field_a) == str(field_b):
        return True
    if str(field_b) in _declared_same_as(field_a):
        return True
    if str(field_a) in _declared_same_as(field_b):
        return True
    return bool(_stem(field_a)) and _stem(field_a) == _stem(field_b)


def fields_covered_by(
    cited: Iterable[str], candidates: Iterable[str],
) -> set[str]:
    """The candidate fields a set of citations actually covers."""

    cited_keys = [str(item) for item in cited]
    return {
        str(candidate) for candidate in candidates
        if any(same_attribute(candidate, key) for key in cited_keys)
    }

def _registry_fields() -> tuple[str, ...]:
    """Every field key this module can recognise wording for."""

    keys: set[str] = set(_RULE_WORDS)
    for item in SPEC_ATTRIBUTES:
        keys.add(item.field_key)
        keys.update(item.also_fields)
        keys.update(nested for _label, nested in item.nested)
    return tuple(sorted(key for key in keys if key))


_REGISTRY_FIELDS = _registry_fields()


def unsupported_attribute_claims(
    answer: object, *, requested_fields: Iterable[str],
    delivered_fields: Iterable[str], evidence_texts: Iterable[str] = (),
    asked: object = "",
) -> list[dict[str, str]]:
    """Attributes the answer settles that no delivered fact speaks to.

    The value and citation checks both start from a delivered fact, so an
    attribute the catalogue never supplied had nothing to check. Measured on
    S49CG954, asked "FreeSync와 G-Sync를 지원하나요?": ``freesync_spec`` was
    delivered and ``gsync_spec`` was not, and "FreeSync Premium Pro를
    지원하며 G-Sync도 지원합니다" was GENERATED, auto-answerable and recorded
    as verified -- the second half of the sentence resting on nothing.

    This is not "absent means unsupported": nothing here answers the
    customer. It is that a *definite* claim about a product attribute needs
    something behind it. An approved Learning or template that speaks to the
    same attribute is such a thing, which is why ``evidence_texts`` is read;
    a hedged sentence settles nothing and is left alone.

    The attributes considered are the ones routing asked for *and* the ones
    the answer itself raises, so a specification the customer never mentioned
    cannot be added on the way out either.
    """

    body = str(answer or "")
    if not body.strip():
        return []
    delivered = [str(field) for field in delivered_fields]
    asked_text = str(asked or "")
    candidates: list[str] = []
    for field in (*(str(item) for item in requested_fields),
                  *_REGISTRY_FIELDS):
        if field in candidates or not field:
            continue
        if any(same_attribute(field, item) for item in delivered):
            continue
        candidates.append(field)

    findings: list[dict[str, str]] = []
    for field in candidates:
        terms = [term for term in _question_terms(field) if term]
        if not terms:
            continue
        sentences = [
            sentence for sentence in _sentences(body)
            if any(_states_term(sentence, term) for term in terms)
            and not _hedged(sentence)
            and (_affirmed(sentence) or _negated(sentence))
        ]
        if not sentences:
            continue
        if any(
            any(_states_term(str(text), term) for term in terms)
            for text in evidence_texts if str(text or "").strip()
        ):
            # Something else the pipeline gave the model speaks to this
            # attribute; that evidence keeps its own path.
            continue
        findings.append({
            "field_key": field,
            "delivered_value": "",
            "value_shape": "NO_EVIDENCE",
            "reason": "ATTRIBUTE_WITHOUT_EVIDENCE",
            "detail": sentences[0][:60],
        })
    if asked_text:
        asked_terms = {_compact(item)
                       for item in _objects_before_support(asked_text)}
        findings.sort(key=lambda item: not any(
            token and token in _compact(item["field_key"])
            for token in asked_terms))
    return findings

def measures_of(
    facts: Mapping[str, Any],
) -> dict[str, set[tuple[str, str]]]:
    """Each field's measurements, as ``{field: {(number, unit class)}}``.

    Keyed by field on purpose: a figure is grounded only for the attribute
    that stated it.
    """

    found: dict[str, set[tuple[str, str]]] = {}
    for field_key, entry in facts.items():
        for value, unit in _normalise(entry):
            if not str(value or "").strip():
                continue
            found.setdefault(str(field_key), set()).update(
                classify_fact(field_key, value, unit).measures)
    return found

def fields_subsumed_by(
    cited: Mapping[str, Any], candidates: Mapping[str, Any],
) -> set[str]:
    """Candidates whose value a cited fact already states.

    One direction only, and for a reason. ``usb_present`` is "USB" and
    ``usb_c_present`` is "USB C타입": citing the connector states the port,
    so an answer about the C port need not cite the generic one as well. The
    reverse is not true -- citing "HDMI" does not state "HDMI 2.0", which is
    how a draft citing mere HDMI presence came to assert "HDMI 9.9" -- so the
    candidate's value must be contained in the cited one, not the other way
    round.
    """

    cited_values = [
        _compact(value)
        for entry in cited.values()
        for value, _unit in _normalise(entry)
        if str(value or "").strip()
    ]
    covered: set[str] = set()
    for field, entry in candidates.items():
        for value, _unit in _normalise(entry):
            compact = _compact(value)
            if not compact:
                continue
            if any(compact in item and compact != item
                   for item in cited_values):
                covered.add(str(field))
                break
    return covered
