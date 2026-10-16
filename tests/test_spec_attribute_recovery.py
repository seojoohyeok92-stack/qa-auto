"""The legacy ``spec`` text is read once, and a measurement is a number.

Anchored on a real failure: "다리 사이 간격이 80cm 이하인가요?" about
LH43BEFHLGFXKR was answered "확인된 자료가 없어 정확한 안내가 어렵습니다" while
the catalog's own spec for that model says ``스탠드 다리 사이 간격: 674.9mm``.
Two separate defects, both measured before the change:

* the question side and the supply side carried their own wording lists, and
  only the supply side knew "다리 사이", so the field was never requested;
* when it *was* requested, its value was the entire spec string, because
  presence fields and measurement fields shared one assignment.

These tests run the real ``ProductKnowledgeService`` against the real catalog,
so they fail if either defect returns.
"""
from __future__ import annotations

import pytest

from answer.text_utils import split_subquestions
from services.product_knowledge_service import (
    MEASUREMENT_FIELD_KEYS,
    NESTED_FIELD_KEYS,
    ProductKnowledgeService,
    fields_for_question,
)
from services.spec_attributes import (
    PRESENCE,
    SPEC_ATTRIBUTES,
    facts_from_spec,
    fields_for_spec_question,
    labelled_segments,
    unmapped_labels,
)

BEF = "LH43BEFHLGFXKR"
BEF_NAME = "삼성 107.9cm(43인치) 비즈니스 TV LH43BEFHLGFXKR"


@pytest.fixture(scope="module")
def service() -> ProductKnowledgeService:
    return ProductKnowledgeService()


@pytest.fixture(scope="module")
def catalog(service: ProductKnowledgeService) -> dict:
    return service.catalog_repository.catalog()["catalog"]


def lookup(service, question, *, model=BEF, name=BEF_NAME, include_all=False):
    return service.facts_for_inquiry(
        product_id=None, questions=split_subquestions(question),
        question=question, model_code=model, product_name=name,
        option_name="", include_all_catalog_fields=include_all,
    )


def fact(result, field_key):
    found = [item for item in result.safe_facts if item.field_key == field_key]
    return found[0] if found else None


# -- the question the system could not answer -------------------------------


def test_the_production_question_now_reaches_the_stored_measurement(service):
    """The whole case, end to end, in the wording the customer used."""

    result = lookup(service, "다리 사이 간격이 80cm 이하인가요?")
    assert result.resolved_pk_targets == ("43BEF",)
    spacing = fact(result, "stand_spacing")
    assert spacing is not None, [f.field_key for f in result.safe_facts]
    assert spacing.value == "674.9mm"
    assert spacing.unit == "mm"
    # 674.9mm is 67.49cm, so the number needed to settle "80cm 이하" is now
    # in the prompt. The comparison itself is the model's to make.
    assert "674.9" in result.prompt_block()


@pytest.mark.parametrize("question", [
    "다리 사이 간격이 80cm 이하인가요?",
    "다리 간격이 얼마인가요?",
    "스탠드 간격이 얼마인가요?",
    "받침대 사이 간격이 얼마인가요?",
    "스탠드 다리 간격 알려주세요",
    "다리사이 간격 알려주세요",
    "받침대 간격이 궁금합니다",
])
def test_every_wording_for_the_same_measurement_reaches_the_same_fact(
    service, question,
):
    """One vocabulary means these cannot disagree with each other again."""

    spacing = fact(lookup(service, question), "stand_spacing")
    assert spacing is not None, question
    assert spacing.value == "674.9mm"


def test_a_component_word_no_longer_withholds_the_products_own_dimension(
    service,
):
    """"스탠드 간격" names a component; the millimetres are still the product's.

    The bundled-component gate exists to withhold the listing's *identity*.
    It was withholding every catalogue fact, so this question got nothing
    while "다리 간격" -- the same question -- got the measurement.
    """

    result = lookup(service, "스탠드 간격이 얼마인가요?")
    assert fact(result, "stand_spacing") is not None
    # And the identity the gate protects is still withheld.
    assert fact(result, "brand") is None
    assert fact(result, "model_name") is None


# -- measurement vs presence ------------------------------------------------


def test_no_measurement_fact_carries_the_spec_blob(service, catalog):
    """The defect that made the stored value useless, across the catalog."""

    # An attribute that declares its value is a list of items has a longer
    # one by design -- 2-c restored "시인성 향상"'s seven accessibility
    # features, of which the comma split had been keeping one -- so the
    # ceiling that caught the blob is checked against what it was for: a
    # value that swallowed the labels after it.
    from services.spec_attributes import SPEC_ATTRIBUTES

    listed = {item.field_key for item in SPEC_ATTRIBUTES if item.list_value}
    offenders: list[tuple[str, str, int]] = []
    for key, record in catalog.items():
        if not isinstance(record, dict):
            continue
        for field_key, (value, _unit) in facts_from_spec(record.get("spec")).items():
            text = str(value)
            if field_key in listed:
                # Still never the blob: a list of items carries no label of
                # its own and is a fraction of the specification.
                if ":" in text or len(text) > len(
                        str(record.get("spec") or "")) / 3:
                    offenders.append((key, field_key, len(text)))
                continue
            if len(text) > 80:
                offenders.append((key, field_key, len(text)))
    assert offenders == []


def test_an_unreadable_measurement_produces_no_fact_at_all():
    """Silence, not a guess: a run-on segment is not a value.

    The label here *is* mapped -- "색상" is a colour label -- so this is the
    case where the attribute is known and the text is not usable. Nothing is
    promoted, and nothing is guessed from the surrounding run-on.
    """

    spec = "색상: 블랙 / LG용 등급 : 보급형출시년도 : 2023년형종류 : LEDTV화면크기 : 109.2"
    assert facts_from_spec(spec) == {}


def test_a_label_no_attribute_claims_is_reported_not_dropped():
    """The extension point: an unmodelled label stays visible."""

    labels = unmapped_labels("응답속도 평가지표: 사내 기준 A, 패널 공급사: 미공개")
    assert labels
    assert facts_from_spec("패널 공급사: 미공개") == {}


def test_presence_fields_still_use_the_matched_spec_text():
    """Only measurements changed. A presence claim is about the mention."""

    presence = {item.field_key for item in SPEC_ATTRIBUTES
                if item.kind == PRESENCE}
    assert presence & {"wifi_present", "bluetooth_present", "hdmi_present"}
    assert not (presence & MEASUREMENT_FIELD_KEYS)


# -- nested labels ----------------------------------------------------------


def test_a_nested_label_keeps_every_part_it_states(service):
    """"상단결합시: 609.7mm / 하단결합시: 634.7mm / 차이: 25mm" is three facts."""

    result = lookup(service, "스탠드 결합 높이 알려주세요", model="LH43BEDHLGFXKR",
                    name="삼성 107.9cm(43인치) 비즈니스 TV LH43BEDHLGFXKR")
    assert fact(result, "stand_mount_height_upper").value == "609.7mm"
    assert fact(result, "stand_mount_height_lower").value == "634.7mm"
    assert fact(result, "stand_mount_height_difference").value == "25mm"
    assert NESTED_FIELD_KEYS >= {
        "stand_mount_height_upper", "stand_mount_height_lower",
        "stand_mount_height_difference",
    }


# -- the two sides cannot drift again ---------------------------------------


def test_every_registry_term_is_answerable_by_the_registry_itself():
    """A question term that selects no field is a list that has drifted."""

    for item in SPEC_ATTRIBUTES:
        if item.supply_only or not item.question_terms:
            continue
        for term in item.question_terms:
            fields, _topics = fields_for_spec_question(term)
            assert item.field_key in fields, (item.field_key, term)


def test_the_service_router_exposes_the_registry_fields():
    """``fields_for_question`` is the production entry point for the same list."""

    fields, _topics = fields_for_question("다리 사이 간격이 80cm 이하인가요?")
    assert "stand_spacing" in fields


# -- other representative models -------------------------------------------


@pytest.mark.parametrize("model, name, question, field_key", [
    ("S49CG954E", "삼성 오디세이 S49CG954E", "스탠드 포함 무게 알려주세요",
     "weight_with_stand"),
    ("S49CG954E", "삼성 오디세이 S49CG954E", "스탠드 포함 크기 알려주세요",
     "dimensions_with_stand"),
    ("F24T450", "삼성 모니터 F24T450", "Display Port 몇 개인가요?",
     "display_port_count"),
    ("F24T450", "삼성 모니터 F24T450", "픽셀피치 알려주세요", "pixel_pitch"),
])
def test_representative_labelled_models_reach_their_measurement(
    service, model, name, question, field_key,
):
    result = lookup(service, question, model=model, name=name)
    found = fact(result, field_key)
    assert found is not None, [f.field_key for f in result.safe_facts]
    assert str(found.value).strip()
    assert len(str(found.value)) <= 80


def test_no_measurement_is_attributed_to_another_model(service, catalog):
    """A recovered value belongs to the model whose spec stated it."""

    for key, model in (("LH43BEFH", BEF), ("LH43BEDH", "LH43BEDHLGFXKR")):
        own = facts_from_spec(catalog[key].get("spec"))
        result = lookup(service, "다리 사이 간격이 얼마인가요?", model=model,
                        name=f"삼성 {model}")
        spacing = fact(result, "stand_spacing")
        assert spacing is not None
        assert spacing.value == own["stand_spacing"][0]


def test_every_recovered_measurement_carries_its_unit_in_the_value():
    """The prompt renders ``value`` and not ``unit``, so the value must say it.

    A ``unit:`` line was tried and withdrawn: one more line per fact costs
    prompt budget, and on a whole-record offer that budget decides which forty
    facts survive -- ``test_merged_product_knowledge_runtime`` lost
    ``additional_cost``. A measurement this module extracts therefore keeps its
    unit inside the value, so nothing downstream has to add one.
    """

    from services.spec_attributes import SPEC_UNITS

    spec = ("스탠드 다리 사이 간격: 674.9mm, 제품 무게: 7.1 kg, "
            "포장크기(가로x높이x깊이): 1081 x 665 x 147 mm")
    recovered = facts_from_spec(spec)
    assert recovered
    for field_key, (value, unit) in recovered.items():
        if field_key not in SPEC_UNITS:
            continue
        assert unit
        assert unit.lower() in str(value).lower(), (field_key, value, unit)


def test_labelled_segments_does_not_split_inside_a_ratio():
    """"1,000:1" and "16:9" are values, not labels."""

    pairs = dict(labelled_segments("명암비: 1,000:1, 화면비율: 16:9"))
    assert pairs.get("명암비") == "1,000:1"
    assert pairs.get("화면비율") == "16:9"


# -- colour, kept apart by subject -----------------------------------------


@pytest.mark.parametrize("question", [
    "제품 색상이 어떻게 되나요?", "색상이 뭐예요?", "색깔이 뭔가요?",
    "컬러 알려주세요", "무슨 색인가요?", "어떤 색 있나요?",
    "색이 뭐예요?", "몇 가지 색으로 나오나요?",
])
def test_a_general_colour_question_reaches_the_body_colour_family(question):
    """The record writes one body colour under four names; ask for all four.

    Measured before this: the whole-corpus audit found 69 colour assertions
    that the service returned and no wording could select, because "색상"
    routed to nothing at all.
    """

    fields, _topics = fields_for_question(question)
    assert {"color", "front_color", "rear_color", "color_options"} <= set(fields)


def test_a_stand_colour_question_does_not_answer_with_the_body_colour():
    """Different subject, different field. The component owns its own colour."""

    fields, _topics = fields_for_question("스탠드 색상은 뭔가요?")
    assert "stand_color" in fields
    assert "color" not in fields
    assert "front_color" not in fields


@pytest.mark.parametrize("question", [
    "sRGB 색영역 몇 %인가요?", "DCI-P3 커버리지는요?", "NTSC 색재현율 알려주세요",
])
def test_a_colour_gamut_question_is_not_a_body_colour_question(question):
    """Coverage of a colour space is not "what colour is it"."""

    fields, _topics = fields_for_question(question)
    assert "color" not in fields
    assert any(key.startswith("color_gamut") for key in fields), fields


def test_every_model_asserting_a_colour_returns_one_when_asked(service, catalog):
    """Whole-corpus, not a sample: all 71 models that state a colour."""

    question = "제품 색상이 어떻게 되나요?"
    targets = [(key, record) for key, record in catalog.items()
               if isinstance(record, dict)
               and record.get("color") not in (None, "", False)]
    assert len(targets) >= 70
    missing = []
    for key, record in targets:
        model = str(record.get("model") or key)
        result = lookup(service, question, model=model, name=f"삼성 {model}")
        if not [item for item in result.safe_facts
                if "color" in item.field_key]:
            missing.append(key)
    assert missing == []


# -- run-on values, caught by auditing the whole corpus --------------------


def test_a_slash_joined_run_on_is_not_promoted_as_a_value():
    """Found by audit, not by a sample: 4 of 12 models disagreed.

    ``스탠드 형태: 플랫형 / 밝기 300 / 패널:VA패널 / 소비전력 225 / 214.7 cm``
    carries one colon and 41 characters, so the first guard (two colons, 80
    characters) let it through and ``stand_type_spec`` was promoted with four
    unrelated specifications inside it. Auditing every model that asserts the
    label against what the service returns is what surfaced it.
    """

    spec = ("스탠드 형태: 플랫형 / 밝기 300 / 패널:VA패널 / 소비전력 225 "
            "/ 214.7 cm")
    assert "stand_type_spec" not in facts_from_spec(spec)


@pytest.mark.parametrize("spec, field_key, value", [
    ("HDMI ARC: eARC/ARC", "hdmi_arc_spec", "eARC/ARC"),
    ("제품 사용 시간: 8/7", "daily_usage_hours_spec", "8/7"),
])
def test_a_slash_inside_one_value_is_still_a_value(spec, field_key, value):
    """The guard must not take the legitimate two-part values with it."""

    assert facts_from_spec(spec)[field_key][0] == value


def test_no_recovered_value_is_a_run_on_anywhere_in_the_catalog(catalog):
    """Whole-corpus, every label, every model."""

    offenders = []
    for key, record in catalog.items():
        if not isinstance(record, dict):
            continue
        for field_key, (value, _unit) in facts_from_spec(record.get("spec")).items():
            parts = [p.strip() for p in str(value).split("/") if p.strip()]
            if len(parts) >= 3 or (len(parts) == 2
                                   and any(":" in p for p in parts)):
                offenders.append((key, field_key, value))
    assert offenders == []


def test_the_spec_and_the_record_may_disagree_and_neither_is_promoted(service):
    """A conflict stays a conflict: ``stand_type_spec`` has no wording.

    Three models state ``스탠드 형태: FLAT FEET`` in the spec while the stored
    Product Knowledge says ``BASIC FEET``. Deciding between them is a review
    decision, so the spec reading is supplied but not selectable, and a
    question about the stand gets the record's answer alone rather than two
    contradictory ones.
    """

    fields, _topics = fields_for_question("스탠드 형태가 어떻게 되나요?")
    assert "stand_type" in fields
    assert "stand_type_spec" not in fields

    result = lookup(service, "스탠드 형태가 어떻게 되나요?")
    values = {str(item.value) for item in result.safe_facts
              if item.field_key.startswith("stand_type")}
    assert len(values) == 1, values
