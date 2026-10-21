"""A verb is not a subject, and "지원" was one of an attribute's names.

``_subject_terms`` is ``_question_terms`` plus the words read back out of the
section rules' own patterns, which is what lets a catalogue spelling be found
in a sentence a customer would phrase differently.  A rule spells its match
out in full, so ``^(OTT지원)$`` contributes "OTT" and the bare verb "지원"
alike -- and ``ott_support_spec`` was therefore called ``('ott', '오티티',
'OTT', '지원', '자체', '불가', '사용')``.

Every support sentence in every answer contains one of those verbs, so every
support sentence was a sentence about OTT.  ``_polarity_findings`` settles on
the first sentence that names the subject and asserts something, and both
real drafts have the same shape: the sentence that *agrees* with
``ott_support_spec = 미지원`` is found and passed over because it is negated,
and the affirmative that decides the finding is about something else
entirely.

Measured on the 26.10.20 server snapshot, which holds two
``POLARITY_CONTRADICTED`` findings in 1,165 drafts and they are these two:

``692741190`` -- "미러링기능이 되나요? 셋탑박스가있으면 넷플 btv볼수있나요?"
    decided by "미러링 기능을 **지원**합니다", matched on the verb alone.

``692590688`` -- "sk btv 셋탑박스있어요. 연결가능하죠? 유튜브나 넷플릭스도
    가능한가요" -- decided by "넷플릭스는 Btv 셋톱박스에서 해당 기능을
    **지원**하는 경우 …", a sentence about the set-top box.  Its guard was not
    ``sensitive``, so the finding withheld nothing and the answer posted.

Of the 164 fields this module recognises wording for, two carry such a word
and neither is left without a term that names it, so the filter takes no
attribute's name away.  The curated side is untouched: no ``question_terms``
entry is a generic word, and only the rule-derived ones are filtered.

What must not change is a sentence that does name the attribute: "TV 자체에서
OTT를 지원합니다" against ``미지원`` is still a contradiction, and so is
"내장 스피커를 지원합니다" against ``speaker = 없음`` -- on both the grounding
and the Learning path, which reach the same verdict through different
vocabularies.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.product_fact_grounding as grounding
from services.learning_evidence_policy import fact_conflicts
from services.product_fact_grounding import unsupported_fact_claims

OTT_FIELD = "ott_support_spec"
NOT_SUPPORTED = "미지원"

# The two real answers, from the snapshot's drafts 1165 and 1145.
ANSWER_692741190 = (
    "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
    "미러링 기능을 지원합니다. 넷플릭스 등 OTT는 TV 자체에서는 지원하지 "
    "않지만, HDMI로 연결한 셋톱박스가 넷플릭스를 지원하면 시청할 수 "
    "있습니다. B tv도 셋톱박스를 연결해 시청하실 수 있습니다.\n\n"
    "안내드린 내용이 문의하신 내용과 다른 경우,\n네이버 톡톡으로 문의 "
    "남겨주시면 담당자가 확인 후 안내드리겠습니다.\n\n감사합니다."
)
ANSWER_692590688 = (
    "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
    "네, SK Btv 셋톱박스는 HDMI 단자에 연결해 시청하실 수 있습니다. 이 "
    "제품은 넷플릭스 등 OTT 앱을 자체 지원하지 않지만, 와이파이 연결 시 "
    "유튜브는 이용 가능합니다. 넷플릭스는 Btv 셋톱박스에서 해당 기능을 "
    "지원하는 경우 셋톱박스를 통해 이용해 주세요.\n\n"
    "안내드린 내용이 문의하신 내용과 다른 경우,\n네이버 톡톡으로 문의 "
    "남겨주시면 담당자가 확인 후 안내드리겠습니다.\n\n감사합니다."
)


def reasons(answer: str, field_key: str, value: str, asked: str = ""):
    """The findings this answer draws for one delivered fact."""

    return sorted(
        str(finding.get("reason"))
        for finding in unsupported_fact_claims(
            answer, {field_key: value}, asked=asked)
        if finding.get("field_key") == field_key
    )


@pytest.fixture
def unfiltered(monkeypatch):
    """The module as it was: every rule word counted as a subject."""

    monkeypatch.setattr(grounding, "_GENERIC_SUBJECTS", frozenset())


# --- 1-4. what a sentence has to name to be about this attribute ----------

def test_a_mirroring_sentence_is_not_about_ott() -> None:
    """The exact sentence that decided 692741190."""

    assert reasons("미러링 기능을 지원합니다.", OTT_FIELD, NOT_SUPPORTED) == []


def test_the_mirroring_sentence_was_what_decided_it(unfiltered) -> None:
    """And it really was the verb: unfiltered, the same sentence contradicts."""

    assert reasons("미러링 기능을 지원합니다.", OTT_FIELD, NOT_SUPPORTED) == [
        "POLARITY_CONTRADICTED"]


def test_a_set_top_box_sentence_is_not_the_tvs_own_support() -> None:
    """Whose support it is decides nothing here; whose attribute does.

    The shape that decided 692590688: an affirmative about a third-party
    box, reached only through the verb.
    """

    assert reasons("HDMI 셋톱박스가 넷플릭스를 지원합니다.",
                   OTT_FIELD, NOT_SUPPORTED) == []


def test_the_tvs_own_ott_claim_still_contradicts() -> None:
    assert reasons("TV 자체에서 OTT를 지원합니다.", OTT_FIELD,
                   NOT_SUPPORTED) == ["POLARITY_CONTRADICTED"]


def test_an_answer_that_agrees_with_the_absence_is_no_conflict() -> None:
    assert reasons("TV 자체에서 OTT를 지원하지 않습니다.", OTT_FIELD,
                   NOT_SUPPORTED) == []


# --- 5. a named attribute is still a named attribute ---------------------

def test_a_speaker_claim_still_contradicts_a_verified_absence() -> None:
    """Both paths, because they recognise the attribute differently.

    ``speaker`` carries no wording this module knows, so the grounding check
    is made against ``speaker_present``, which the section rules name
    "스피커"; the Learning path reaches bare ``speaker`` through the
    attribute families instead. Neither route goes through a generic word.
    """

    claim = "내장 스피커를 지원합니다."
    assert reasons(claim, "speaker_present", "없음") == [
        "POLARITY_CONTRADICTED"]

    conflicts = fact_conflicts(
        [{
            "learning_example_id": 1, "answer": claim, "authority": "APPROVED",
            "matched_subquestion": "스피커가 있나요?",
            "compatibility": {"product_match": "EXACT_MODEL"},
        }],
        [SimpleNamespace(field_key="speaker", value="없음")],
    )
    assert [item["field_key"] for item in conflicts] == ["speaker"]


# --- the two real drafts, replayed --------------------------------------

def test_692741190_loses_the_polarity_finding_and_nothing_else() -> None:
    """The other two findings are untouched, and are meant to be.

    ``VALUE_NOT_STATED_IN_ANSWER`` is a separate question -- whether a
    delivered fact the answer never states should count as a used one -- and
    this change deliberately leaves it alone, so the draft stays
    ``REVIEW_REQUIRED`` on ``PRODUCT_FACT_NOT_VERIFIED``.
    """

    facts = {
        "hdmi_count": "3개",
        "mobile_connect_spec": "Mobile to TV, Sound Mirroring",
        OTT_FIELD: NOT_SUPPORTED,
    }
    found = {
        str(item.get("field_key")): str(item.get("reason"))
        for item in unsupported_fact_claims(
            ANSWER_692741190, facts,
            asked="미러링기능이 되나요?  셋탑박스가있으면 넷플 btv볼수있나요?")
    }
    assert OTT_FIELD not in found
    assert found == {
        "hdmi_count": "VALUE_NOT_STATED_IN_ANSWER",
        "mobile_connect_spec": "VALUE_NOT_STATED_IN_ANSWER",
    }


def test_692741190_was_contradicted_before(unfiltered) -> None:
    """The stored finding is reproduced exactly by the unfiltered module."""

    found = {
        str(item.get("field_key")): str(item.get("reason"))
        for item in unsupported_fact_claims(
            ANSWER_692741190,
            {"hdmi_count": "3개",
             "mobile_connect_spec": "Mobile to TV, Sound Mirroring",
             OTT_FIELD: NOT_SUPPORTED},
            asked="미러링기능이 되나요?  셋탑박스가있으면 넷플 btv볼수있나요?")
    }
    assert found[OTT_FIELD] == "POLARITY_CONTRADICTED"


def test_692590688_loses_the_polarity_finding() -> None:
    """The draft that posted with the false finding recorded against it."""

    facts = {"hdmi_count": "3개", OTT_FIELD: NOT_SUPPORTED}
    asked = ("sk btv 셋탑박스있어요. 연결가능하죠?\n유튜브나 넷플릭스도 "
             "가능한가요")
    found = {
        str(item.get("field_key")): str(item.get("reason"))
        for item in unsupported_fact_claims(ANSWER_692590688, facts,
                                            asked=asked)
    }
    assert OTT_FIELD not in found
    assert found == {"hdmi_count": "VALUE_NOT_STATED_IN_ANSWER"}


def test_692590688_was_contradicted_before(unfiltered) -> None:
    found = {
        str(item.get("field_key")): str(item.get("reason"))
        for item in unsupported_fact_claims(
            ANSWER_692590688, {"hdmi_count": "3개", OTT_FIELD: NOT_SUPPORTED},
            asked="sk btv 셋탑박스있어요. 연결가능하죠?")
    }
    assert found[OTT_FIELD] == "POLARITY_CONTRADICTED"


def test_the_answers_own_ott_sentence_agreed_all_along() -> None:
    """Why these were false: the attribute's own sentence says 미지원 too.

    Both answers state the absence the record states, which is what makes
    the finding a misreading rather than a disagreement.
    """

    claim = grounding.classify_fact(OTT_FIELD, NOT_SUPPORTED)
    for answer in (ANSWER_692741190, ANSWER_692590688):
        about = [
            sentence for sentence in grounding._sentences(answer)
            if any(grounding._states_term(sentence, term)
                   for term in claim.subject_terms)
        ]
        assert about, "no sentence names the attribute"
        assert all(grounding._negated(sentence) for sentence in about)


# --- the rule, not the field --------------------------------------------

def test_the_filter_is_not_keyed_on_one_field() -> None:
    """``samsung_tv_plus_spec`` is the other field that carried one."""

    assert reasons("무선 연결을 사용할 수 있습니다.",
                   "samsung_tv_plus_spec", NOT_SUPPORTED) == []
    assert reasons("삼성TV플러스를 지원합니다.",
                   "samsung_tv_plus_spec", NOT_SUPPORTED) == [
        "POLARITY_CONTRADICTED"]
    assert OTT_FIELD not in grounding._GENERIC_SUBJECTS


def test_only_the_rule_derived_words_are_filtered() -> None:
    """No curated wording is a generic word, so none is being discarded."""

    for field in sorted(set(grounding._REGISTRY_FIELDS)):
        offending = [
            term for term in grounding._question_terms(field)
            if term.lower() in grounding._GENERIC_SUBJECTS
        ]
        assert offending == [], field


def test_no_generic_word_remains_any_attributes_subject() -> None:
    for field in sorted(set(grounding._REGISTRY_FIELDS)):
        for term in grounding._subject_terms(field):
            assert term.lower() not in grounding._GENERIC_SUBJECTS, field


def test_no_attribute_loses_every_name_it_has(unfiltered) -> None:
    """The filter removes words, never an attribute's recognisability.

    Losing the last subject term is not a quiet pass: it routes the
    attribute to ``VALUE_CANNOT_BE_VERIFIED`` instead, so a field that fell
    off this list would start blocking drafts rather than stop. None does.
    """

    fields = sorted(set(grounding._REGISTRY_FIELDS))
    before = {field for field in fields if not grounding._subject_terms(field)}

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(grounding, "_GENERIC_SUBJECTS", frozenset({
            "지원", "가능", "사용", "불가", "자체", "기능", "제공", "해당",
            "support",
        }))
        after = {
            field for field in fields if not grounding._subject_terms(field)}
        # Which fields the filter actually narrows: everything the three
        # sources offer, against what survives. ``_catalogue_labels`` joined
        # those sources later; the filter applies to it too.
        narrowed = {
            field for field in fields
            if set(grounding._subject_terms(field)) != set(
                grounding._question_terms(field))
            | set(grounding._catalogue_labels(field))
            | set(grounding._RULE_WORDS.get(field, ()))
        }

    assert after == before, sorted(after - before)
    # Exactly the two fields the snapshot measurement found.
    assert narrowed == {OTT_FIELD, "samsung_tv_plus_spec"}


def test_ott_keeps_the_words_that_name_it() -> None:
    assert grounding._subject_terms(OTT_FIELD) == ("ott", "오티티", "OTT")
