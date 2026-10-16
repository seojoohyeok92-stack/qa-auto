"""2-c: the words the catalogue states with no label, no shape and no heading.

Part of each spec is a bare run of words -- "틸트(상하) / 평면 / IPS /
헤드폰 아웃 / LED 조명" -- that the labelled reader passes over (no top-level
``label: value``), the positional reader passes over (no self-describing
shape) and the section reader passes over (no bracketed heading). Measured
over the whole catalogue: 11,578 such tokens on 1,569 of the 1,575 models
production resolves.

What this file protects, in order:

* the reading itself, and that a capability is not read as a measurement;
* precedence -- a column, a label, a shape and a heading all still win;
* the withholdings, each with the reason it carries, so a token the source
  states ambiguously is reported rather than guessed at;
* that nothing is left unread, over the whole catalogue;
* the repair of the values the comma split cut in two, which were not merely
  incomplete in production but wrong;
* and that every field reaches the prompt it is meant for -- a field the
  catalogue holds and the pipeline never uses is not a recovery.
"""
from __future__ import annotations

import collections

import pytest

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_fact_grounding import (
    POLARITY, classify_fact, unsupported_fact_claims,
)
from services.product_knowledge_service import (
    ProductKnowledgeService, fields_for_question, select_prompt_facts,
)
from services.spec_attributes import (
    ENUM_FIELD_KEYS, ENUM_RULES, LIST_FIELDS, SPEC_ATTRIBUTES,
    SPEC_FIELD_KEYS, classify_enumeration_spec, enumeration_tokens,
    facts_from_enumeration_spec, facts_from_spec, unmapped_enumeration_tokens,
    withheld_enumeration_tokens,
)


@pytest.fixture(scope="module")
def catalog() -> dict:
    return ProductCatalogRepository().catalog()["catalog"]


@pytest.fixture(scope="module")
def service() -> ProductKnowledgeService:
    return ProductKnowledgeService(ProductCatalogRepository())


def ask(service, model_code: str, question: str):
    return service.facts_for_inquiry(
        product_id=None, questions=[question], question=question,
        model_code=model_code, product_name=f"삼성 {model_code}",
        option_name="", include_all_catalog_fields=False,
    )


def values(result, field_key: str) -> list[str]:
    return [str(item.value) for item in result.safe_facts
            if item.field_key == field_key]


# ---------------------------------------------------------------------------
# What the token says
# ---------------------------------------------------------------------------
READINGS = (
    ("틸트(상하)", "tilt", "지원"),
    ("스탠드 틸트 상하", "tilt", "지원"),
    ("스위블(좌우)", "swivel", "지원"),
    ("스위블x", "swivel", "미지원"),
    ("피벗(회전)", "pivot", "지원"),
    ("피봇x", "pivot", "미지원"),
    ("엘리베이션(높낮이)", "height_adjust", "지원"),
    ("가로 및 세로 자동 전환(오토피벗)", "auto_pivot", "지원"),
    ("오토피벗 지원X", "auto_pivot", "미지원"),
    ("평면", "flat_curved", "평면"),
    ("커브드", "flat_curved", "커브드"),
    ("1800R", "curvature", "1800R"),
    ("IPS", "panel_type", "IPS"),
    ("QD-Fast IPS", "panel_type", "QD-Fast IPS"),
    ("패널 타입 IPS", "panel_type", "IPS"),
    ("AH-IPS (Neo Blade III)", "panel_type", "AH-IPS (Neo Blade III)"),
    ("LED", "backlight_type", "LED"),
    ("강화유리", "panel_surface", "강화유리"),
    ("헤드폰 아웃", "headphone_out", "있음"),
    ("LED 조명", "core_lighting", "LED 조명"),
    ("HDCP", "hdcp", "지원"),
    ("AC100~240V", "rated_voltage", "AC100~240V"),
    ("17W(Typical)", "power_typical", "17W"),
    ("25W(Max)", "power_max", "25W"),
    ("25W", "power_consumption_labelled", "25W"),
    ("눈 보호 모드", "eye_saver", "있음"),
    ("게임모드", "game_mode", "있음"),
    ("실내용", "usage_environment", "실내용"),
    ("제조국: 베트남", "country_of_origin", "베트남"),
    ("내장배터리", "battery_included", "있음"),
    ("M6나사", "vesa_screw", "M6"),
    ("USB 허브", "usb_hub", "있음"),
    ("DP 케이블", "dp_cable_included", "있음"),
    ("응답속도기준 : GTG", "response_time_basis", "GTG"),
    ("스마트기능 : 구글플레이불가", "google_play", "미지원"),
    ("듀얼힌지", "hinge_type", "듀얼힌지"),
    ("PBP", "pbp", "있음"),
    ("PIP", "pip", "있음"),
)


@pytest.mark.parametrize("token,field_key,expected", READINGS,
                         ids=[f"{f}-{t}" for t, f, _v in READINGS])
def test_a_token_is_read_as_what_it_says(token, field_key, expected) -> None:
    facts = facts_from_enumeration_spec(token)
    assert field_key in facts, facts
    assert facts[field_key][0] == expected


def test_a_capability_is_not_read_as_a_measurement() -> None:
    """1,356 models say only that the stand tilts.

    Reporting that as a tilt *angle* would invent a measurement the spec does
    not make, so the capability and the range are separate attributes and
    neither is read from the other's words.
    """

    capability = facts_from_enumeration_spec("틸트(상하)")
    assert capability["tilt"][0] == "지원"
    assert "tilt_range_spec" not in capability

    stated_range = facts_from_spec("틸트(앞뒤): 앞: 3.0° , 뒤: 25.0°")
    assert stated_range["tilt_range_spec"][0] == "앞: 3.0°, 뒤: 25.0°"
    assert "tilt" not in facts_from_enumeration_spec(
        "틸트(앞뒤): 앞: 3.0° , 뒤: 25.0°")


def test_a_compound_token_states_both_of_the_things_it_says() -> None:
    """"와이드(16:9)" is a width class and an aspect ratio, 1,291 times."""

    facts = facts_from_enumeration_spec("와이드(16:9)")
    assert facts["screen_width_class"][0] == "와이드"
    assert facts["aspect_ratio"][0] == "16:9"


def test_the_series_name_is_read_only_where_the_catalogue_writes_it() -> None:
    """"M7" heads 17 specs, and "M6" elsewhere is a mounting screw."""

    assert facts_from_enumeration_spec("M7 / 평면")["series_name"][0] == "M7"
    assert "series_name" not in facts_from_enumeration_spec("평면 / M7")
    assert facts_from_enumeration_spec("M6나사")["vesa_screw"][0] == "M6"


def test_several_values_that_are_all_true_are_kept_together() -> None:
    assert facts_from_enumeration_spec(
        "터치스크린 / 멀티터치")["touch_support_spec"][0] == "터치스크린, 멀티터치"
    assert facts_from_enumeration_spec(
        "상업용디스플레이 / 디스플레이모듈")["product_category"][0] == (
            "상업용디스플레이, 디스플레이모듈")


def test_a_ladder_reports_the_tier_that_was_stated() -> None:
    facts = facts_from_enumeration_spec(
        "FreeSync / FreeSync Premium / FreeSync Premium Pro")
    assert facts["freesync_spec"][0] == "FreeSync Premium Pro"


def test_two_different_values_for_one_attribute_are_withheld() -> None:
    spec = "IPS / VA"
    assert "panel_type" not in facts_from_enumeration_spec(spec)
    assert any(field == "panel_type" and "two different values" in reason
               for _source, field, reason in withheld_enumeration_tokens(spec))


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------
def test_a_better_source_is_never_overwritten() -> None:
    """The order is column, label, shape, heading, and only then this."""

    spec = "평면 / IPS"
    assert facts_from_enumeration_spec(
        spec, already_supplied=("flat_curved",)) == {
            "panel_type": ("IPS", None, "IPS")}


def test_a_token_under_a_heading_belongs_to_its_heading() -> None:
    """The heading is what makes a section token readable, so this ignores it.

    Without the restriction, "[게임특화] FreeSync" would be read twice -- once
    by 2-b with its heading as provenance and once here without it.
    """

    spec = "평면 / [게임특화] FreeSync / 조준선 표시"
    assert enumeration_tokens(spec) == ["평면"]
    assert set(facts_from_enumeration_spec(spec)) == {"flat_curved"}


def test_the_value_is_the_token_and_never_the_specification() -> None:
    spec = ("평면 / IPS / 와이드(16:9) / 헤드폰 아웃 / 출시가: 490,000원 / "
            "LED 조명")
    for field, (value, _unit, _source) in (
            facts_from_enumeration_spec(spec).items()):
        assert len(value) <= 60, (field, value)
        assert "출시가" not in value, (field, value)


# ---------------------------------------------------------------------------
# What is withheld, and why
# ---------------------------------------------------------------------------
WITHHELD = (
    ("정보 없음", "holds no specification"),
    ("60g", "with no subject"),
    ("70 x 70 x 15", "with no subject"),
    ("4.5~6.0kg", "with no subject"),
    ("R-R-SEC-S22D300GA", "shaped like a model code"),
    ("구성: 트레이+스탠드포함", "listing fact"),
    ("CPU : A55x4 up to 20K+ DMIPS", "one model"),
    ("한국어지원", "does not say what is in Korean"),
    ("AS:https:", "support URL"),
    ("5V", "one half of a rating"),
    ("16:9", "does not say which it is"),
    ("화면전환: 가로", "default or the only one"),
    ("아이트래킹", "no second model"),
    ("소비전력(대기): 0.5W(0.3W)", "which is which"),
    ("블루투스", "presence flag"),
    ("삼성전자(주)", "brand column"),
    ("미러링(아이폰 미러링 지원 안됨)", "asserts mirroring and denies it"),
    ("최대: 634.7mm", "token before it"),
)


@pytest.mark.parametrize("token,reason", WITHHELD, ids=[t for t, _r in WITHHELD])
def test_an_ambiguous_token_is_withheld_with_its_reason(token, reason) -> None:
    assert facts_from_enumeration_spec(token) == {}, token
    held = withheld_enumeration_tokens(token)
    assert held, token
    assert any(reason in text for _source, _field, text in held), held


def test_nothing_in_the_catalogue_is_left_unread(catalog) -> None:
    """Whole corpus. A token no rule reads is the next rule, not a loss.

    Measured on the catalogue of 2026-10-15: 11,578 tokens on 1,569 models,
    all of them either read, merged into a field a better source already
    fills, or withheld with a reason.
    """

    unread = collections.Counter()
    for record in catalog.values():
        if not isinstance(record, dict):
            continue
        spec = record.get("spec")
        unmapped = unmapped_enumeration_tokens(spec)
        if not unmapped:
            continue
        # The enumeration reader is offered every unsectioned token,
        # including the ones the other readers already claim, because
        # precedence is settled per field and not per token. So a token
        # another reader reads, or whose text is already inside a fact this
        # spec produces, is not 2-c input.
        produced = " | ".join(
            str(value) for value, _unit in facts_from_spec(spec).values())
        for token in unmapped:
            if _read_elsewhere(token):
                continue
            body = (token.split(":", 1)[1].strip() if ":" in token
                    else token)
            if body and body in produced:
                continue
            unread[token] += 1
    assert unread == {}, dict(unread.most_common(20))


def _read_elsewhere(token: str) -> bool:
    from services.spec_attributes import (
        _attribute_for_label, _LABELLED, _NUMERIC_LABEL, _normalise_measure,
        _POSITIONAL_PATTERNS,
    )
    found = _LABELLED.match(token)
    if found:
        label = found.group(1).strip()
        if label and not _NUMERIC_LABEL.match(label):
            attribute = _attribute_for_label(label)
            if attribute is not None and (
                    attribute.nested
                    or _normalise_measure(found.group(2), attribute)
                    is not None):
                return True
    return any(pattern.match(token)
               for _f, pattern, _u in _POSITIONAL_PATTERNS)


# ---------------------------------------------------------------------------
# The values the comma split cut in two
# ---------------------------------------------------------------------------
def test_a_value_the_comma_cut_is_put_back_together() -> None:
    """These were wrong answers in production, not missing ones.

    ``스위블(좌우): 좌,우: 45.0°`` was read as the single word "좌" on 10
    models: the attribute was answered, and the angle -- the whole of what
    was asked -- was gone. ``시인성 향상`` kept one of seven features on 11.
    """

    assert facts_from_spec("스위블(좌우): 좌,우:  45.0°")[
        "swivel_range_spec"][0] == "좌, 우: 45.0°"
    assert facts_from_spec("틸트(앞뒤): 앞: 3.0° , 뒤: 25.0°")[
        "tilt_range_spec"][0] == "앞: 3.0°, 뒤: 25.0°"
    assert facts_from_spec(
        "시인성 향상: Audio Description, Zoom Menu and Text, High Contrast, "
        "청각 보조: Closed Caption (Subtitle), Multi-output Audio",
    ) == {
        "accessibility_visual": (
            "Audio Description, Zoom Menu and Text, High Contrast", None),
        "accessibility_hearing": (
            "Closed Caption (Subtitle), Multi-output Audio", None),
    }


def test_only_an_attribute_that_says_so_continues_past_a_comma() -> None:
    """A continuation rule for every label would swallow real assertions.

    "픽셀피치: 0.24795mm, HDMI 케이블" is a pixel pitch followed by a cable.
    """

    facts = facts_from_spec("픽셀피치: 0.24795mm, HDMI 케이블, 제품 무게: 3.2kg")
    assert facts["pixel_pitch"][0] == "0.24795mm"
    assert facts["weight_product_spec"][0] == "3.2kg"


def test_a_continuation_stops_at_a_token_that_describes_itself() -> None:
    """A token another reader claims by its shape is a separate assertion."""

    facts = facts_from_spec("모바일 무선 연결: Mobile to TV, 60Hz, 1920x1080")
    assert facts["mobile_connect_spec"][0] == "Mobile to TV"


def test_the_repaired_value_reaches_the_customer(service) -> None:
    """The same two, through the production router on the real catalogue."""

    assert values(ask(service, "S27BG400", "스위블 각도가 어떻게 되나요?"),
                  "swivel_range_spec") == ["좌, 우: 15.0°"]
    assert values(ask(service, "S27BG400", "틸트 각도가 어떻게 되나요?"),
                  "tilt_range_spec") == ["앞: 3.0°, 뒤: 22.0°"]


# ---------------------------------------------------------------------------
# The fields, and whether the pipeline can use them
# ---------------------------------------------------------------------------
NEW_FIELDS = (
    "tilt", "swivel", "pivot", "height_adjust", "auto_pivot", "headphone_out",
    "flat_curved", "curvature", "screen_width_class", "panel_type",
    "backlight_type", "panel_surface", "core_lighting", "eye_saver",
    "game_mode", "pip", "pbp", "hdcp", "usb_hub", "vesa_screw",
    "dp_cable_included", "rated_voltage", "power_typical", "power_max",
    "power_consumption_labelled", "google_play", "battery_included",
    "series_name", "product_category", "usage_environment",
    "country_of_origin", "response_time_basis", "hinge_type",
)


def test_every_field_the_reader_supplies_is_a_field_some_reader_owns() -> None:
    """A field no reader owns has no question wording and no unit.

    Four of them belong to a catalogue column or to the positional reader --
    ``screen_size``, ``resolution``, ``response_time_ms``,
    ``speaker_present`` -- which is the point: a token restating one of those
    is merged into it rather than given a second name.
    """

    from services.product_knowledge_service import CATALOG_BACKED_FIELDS
    from services.spec_attributes import POSITIONAL_FIELD_KEYS

    owned = SPEC_FIELD_KEYS | POSITIONAL_FIELD_KEYS | set(
        CATALOG_BACKED_FIELDS)
    assert ENUM_FIELD_KEYS <= owned, sorted(ENUM_FIELD_KEYS - owned)


@pytest.mark.parametrize("field_key", NEW_FIELDS)
def test_every_new_field_is_asked_for_by_its_own_wording(field_key) -> None:
    """Routing, through the production router and not the registry alone."""

    item = next(a for a in SPEC_ATTRIBUTES if a.field_key == field_key)
    assert item.question_terms, field_key
    assert any(field_key in fields_for_question(term)[0]
               for term in item.question_terms), (
        field_key, item.question_terms)


REACHES = (
    ("tilt", "G7 G70B S32BG700", "틸트 되나요?", "지원"),
    ("flat_curved", "S27F322", "평면인가요 커브드인가요?", "평면"),
    ("curvature", "LC32HG70QQKXKR", "곡률이 어떻게 되나요?", "1800R"),
    ("power_typical", "S22D300", "일반 소비전력이 몇 W인가요?", "17W"),
    ("product_category", "LH32DCE2LGA", "품목이 무엇인가요?",
     "상업용디스플레이, 디스플레이모듈"),
    ("core_lighting", "G7 G70B S32BG700", "LED 조명 있나요?", "LED 조명"),
    ("headphone_out", "S27BG400", "헤드폰 단자 있나요?", "있음"),
    ("rated_voltage", "S22D300", "입력 전압이 어떻게 되나요?", "AC100~240V"),
)


@pytest.mark.parametrize("field_key,model,question,expected", REACHES,
                         ids=[r[0] for r in REACHES])
def test_a_new_field_reaches_the_prompt_for_a_real_model(
    service, field_key, model, question, expected,
) -> None:
    """Stored in the catalogue is not enough: it has to arrive."""

    result = ask(service, model, question)
    assert values(result, field_key) == [expected]
    kept, _dropped, _selection = select_prompt_facts(
        tuple(result.safe_facts), requested_fields=result.requested_fields,
        topics=result.topics, question=question,
    )
    assert field_key in {item.field_key for item in kept}, sorted(
        item.field_key for item in kept)


@pytest.mark.parametrize("field_key", NEW_FIELDS)
def test_a_claim_on_a_new_field_can_be_checked_against_an_answer(
    field_key,
) -> None:
    """A field the grounding gate cannot verify is a field it fails closed on.

    So every one of them has to be verifiable: a yes or a no needs wording
    that says what the subject is, and a value needs to be quotable.
    """

    sample = next(
        (rule, field, group) for rule in ENUM_RULES
        for field, group in rule.targets if field == field_key
    )
    rule, _field, _group = sample
    value = rule.value or "샘플"
    claim = classify_fact(field_key, value)
    if claim.kind == POLARITY:
        assert claim.subject_terms, field_key
        wrong = f"{claim.subject_terms[0]}는 지원하지 않습니다."
        if value in {"미지원", "없음"}:
            wrong = f"{claim.subject_terms[0]}를 지원합니다."
        assert unsupported_fact_claims(wrong, {field_key: (value, None)}), (
            field_key, wrong)


def test_the_listing_scoped_fields_are_not_pooled() -> None:
    """What comes in the box is this listing's, not the model's."""

    poolable = ProductKnowledgeService._POOLABLE_FIELDS
    assert "dp_cable_included" not in poolable
    assert "hdmi_cable_included_spec" not in poolable
    # ...and the model's own specification is.
    for field_key in ("tilt", "panel_type", "flat_curved", "curvature"):
        assert field_key in poolable, field_key


def test_the_stand_shape_stays_supply_only_and_the_hinge_does_not() -> None:
    """A hinge is not the shape of the foot, and that distinction is load-bearing.

    ``stand_type_spec`` is supply-only on purpose: three models state
    "스탠드 형태: FLAT FEET" in the spec while the stored knowledge says
    "BASIC FEET", so a stand question must not be answered with two
    contradictory values. Filing "듀얼힌지" there would have made it
    unreachable, so it has its own attribute.
    """

    item = next(a for a in SPEC_ATTRIBUTES
                if a.field_key == "stand_type_spec")
    assert item.supply_only is True
    assert "stand_type_spec" not in fields_for_question(
        "스탠드 형태가 어떻게 되나요?")[0]
    assert "hinge_type" in fields_for_question("듀얼힌지인가요?")[0]


def test_a_list_field_is_declared_as_one() -> None:
    assert {"product_category", "touch_support_spec"} <= LIST_FIELDS


def test_the_reader_adds_no_field_outside_the_rules(catalog) -> None:
    """Whole corpus: every field produced is one a rule names."""

    produced: set[str] = set()
    for record in catalog.values():
        if isinstance(record, dict):
            produced.update(classify_enumeration_spec(record.get("spec"))[0])
    assert produced <= ENUM_FIELD_KEYS, sorted(produced - ENUM_FIELD_KEYS)
