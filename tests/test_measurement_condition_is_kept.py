"""A reading without its condition is not the same reading.

Two data-integrity findings, both settled from the catalogue's own API-sourced
Product Knowledge rather than from string similarity:

* ``S32FM701``'s record named itself ``S32FM703``. The collected Product
  Knowledge holds 138+ rows for ``LS32FM701UKXKR`` under one product id and
  none for 703, its ``item_name`` says 701, and the neighbouring entries
  (S32FM700/702/703) each name themselves. The record's model field was the
  error; the two models stay separate.

* ``S49CG954``'s spec says 1,000cd where the API says ``brightness_typical``
  450, and the same spec carries ``VESA HDR 1000``. Those are one model read
  under two conditions -- typical and HDR peak -- not a conflict, and they
  belong in two fields.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import replace

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository, canonical_model_identity,
)
from services.product_knowledge_service import (
    ProductFact, ProductKnowledgeService,
)
from services.spec_attributes import classify_positional, facts_from_spec

CATALOG_PATH = (pathlib.Path(__file__).resolve().parents[1]
                / "data" / "model_data_with_color.json")


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


# -- S32FM701 names itself ---------------------------------------------------


def test_the_record_names_its_own_model():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    entries = catalog["MODEL_CATALOG"]
    for key in ("S32FM700", "S32FM701", "S32FM702", "S32FM703"):
        assert entries[key]["model"] == key, key


def test_both_models_still_exist_and_keep_their_own_identity(repository):
    """701 != 703. The correction renamed nothing and merged nothing."""

    aliases = repository.catalog().get("aliases")
    assert repository.match(model_code="S32FM701").model_key == "S32FM701"
    assert repository.match(model_code="S32FM703").model_key == "S32FM703"
    assert (canonical_model_identity("S32FM701", aliases=aliases)
            != canonical_model_identity("S32FM703", aliases=aliases))


@pytest.mark.parametrize("model, identity", [
    ("S32FM701", "32FM701"), ("S32FM703", "32FM703"),
])
def test_each_inquiry_targets_its_own_model(service, model, identity):
    result = ask(service, model, "해상도가 어떻게 되나요?")
    assert result.resolved_pk_targets == (identity,)
    foreign = [i.model_code for i in result.safe_facts
               if i.scope == "MODEL_CATALOG" and str(i.model_code) != model]
    assert foreign == []


def test_the_api_collected_facts_now_reach_701(service):
    """They were filtered out while the record claimed to be 703."""

    result = ask(service, "S32FM701", "해상도가 어떻게 되나요?")
    sources = {str(i.model_code) for i in result.safe_facts}
    assert "LS32FM701UKXKR" in sources


# -- S49CG954 keeps each condition ------------------------------------------


@pytest.mark.parametrize("model", [
    "S49CG954", "S49CG954E", "LS49CG954EKXKR",
])
def test_every_spelling_reaches_the_same_product(service, model):
    """No alias was added; the existing steps already converge."""

    assert ask(service, model, "밝기 몇 니트인가요?").resolved_pk_targets == (
        ("49CG954",))


@pytest.mark.parametrize("model", [
    "S49CG954", "S49CG954E", "LS49CG954EKXKR",
])
def test_typical_and_peak_brightness_are_separate_fields(service, model):
    result = ask(service, model, "밝기 몇 니트인가요?")
    assert values(result, "brightness_typical") == ["450"]
    assert values(result, "brightness_peak") == ["1,000cd"]
    # And the condition-losing field no longer carries the peak.
    assert values(result, "brightness_typical_cd_m2") == []


@pytest.mark.parametrize("question", [
    "HDR 최대 밝기 얼마인가요?", "피크 밝기 알려주세요", "최대 휘도는요?",
])
def test_a_peak_question_reaches_the_peak(service, question):
    result = ask(service, "S49CG954", question)
    assert values(result, "brightness_peak") == ["1,000cd"]


@pytest.mark.parametrize("question", [
    "스탠드 포함 무게 알려주세요", "스탠드 제외 무게 알려주세요",
])
def test_both_weight_conditions_are_available(service, question):
    result = ask(service, "S49CG954", question)
    assert values(result, "weight_with_stand") == ["15.6"]
    assert values(result, "weight_without_stand") == ["10.6"]


def test_no_single_field_carries_two_conditions(service):
    """The requirement, stated directly: one field, one condition."""

    result = ask(service, "S49CG954", "밝기 몇 니트인가요?")
    by_field: dict[str, set] = {}
    for item in result.safe_facts:
        by_field.setdefault(item.field_key, set()).add(str(item.value))
    assert all(len(v) == 1 for v in by_field.values()), by_field


def _spec_brightness(repository) -> dict[str, str]:
    """Every catalog record whose spec yields a bare brightness token."""

    out = {}
    for key, record in repository.catalog()["catalog"].items():
        if not isinstance(record, dict):
            continue
        spec = record.get("spec")
        got = {f: v for f, (v, _u) in facts_from_spec(spec).items()}
        safe, _ambiguous, _unmapped = classify_positional(spec)
        for field, (value, _unit, _token) in safe.items():
            got.setdefault(field, value)
        if "brightness_typical_cd_m2" in got:
            out[key] = got["brightness_typical_cd_m2"]
    return out


def _stored_brightness(repository) -> dict[str, str]:
    """Every API-collected brightness, by the identity it is filed under."""

    knowledge = repository.product_knowledge() or {}
    aliases = repository.catalog().get("aliases")
    out: dict[str, str] = {}
    for _section, rows in ProductKnowledgeService(
            repository)._runtime_fact_sections(knowledge):
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("field") or "") not in {
                    "brightness_typical", "brightness"}:
                continue
            identity = canonical_model_identity(
                row.get("model_code"), aliases=aliases)
            if identity:
                out.setdefault(str(identity), str(row.get("value")))
    return out


def test_only_a_contradicted_reading_is_relabelled(repository):
    """Whole-corpus, not a sample: every record the relabel can reach.

    The helper returns early unless the spec yielded a brightness token and
    the collected knowledge holds one too, so this enumerates both sides for
    the entire catalogue and checks every overlap. Measured: 1,462 records
    yield a spec token and 23 identities carry a collected reading; they
    overlap on 12, 11 agree and keep the field they have, and S49CG954 alone
    disagrees.
    """

    digits = re.compile(r"\d+")
    aliases = repository.catalog().get("aliases")
    spec_side = _spec_brightness(repository)
    stored_side = _stored_brightness(repository)

    overlap, disagreeing = [], []
    for key, token in spec_side.items():
        identity = str(canonical_model_identity(key, aliases=aliases) or "")
        stored = stored_side.get(identity)
        if stored is None:
            continue
        overlap.append(key)
        mine = "".join(digits.findall(token))
        theirs = "".join(digits.findall(stored))
        if mine and theirs and mine != theirs:
            disagreeing.append(key)

    assert overlap, "the join found nothing; the check would prove nothing"
    assert disagreeing == ["S49CG954"], disagreeing


def _catalog_fact(value: str) -> ProductFact:
    """A catalogue brightness fact, shaped as ``_catalog_facts`` emits one."""

    return ProductFact(
        product_id="p", listing_id="l", model_code="M",
        field_key="brightness_typical_cd_m2", value=value, raw_value=value,
        unit="cd/m2", scope="MODEL_CATALOG", scope_key="M",
        component_scope="MAIN_PRODUCT", volatility="STABLE",
        verification_status="VERIFIED", resolution_status="RESOLVED",
        lifecycle_status="ACTIVE",
        canonical_fact_id="p:brightness_typical_cd_m2", value_id=None,
        subject="MAIN_PRODUCT",
    )


@pytest.mark.parametrize("stored, token, spec, expected", [
    # Agreeing: left alone, the condition is already right.
    ("450", "450cd", "240Hz / 450cd / HDR10+ / VESA HDR 1000",
     "brightness_typical_cd_m2"),
    # Contradicted, and the spec names the tier equal to the token.
    ("450", "1,000cd", "240Hz / 1,000cd / HDR10+ / VESA HDR 1000",
     "brightness_peak"),
    # Contradicted with nothing corroborating a condition: withheld, never
    # relabelled on a guess and never reported as the ordinary brightness.
    ("450", "1,000cd", "240Hz / 1,000cd / HDR10+", None),
    ("450", "1,000cd", "240Hz / 1,000cd / VESA HDR 400", None),
])
def test_the_condition_must_be_corroborated(stored, token, spec, expected):
    catalog_fact = _catalog_fact(token)
    integrated = [replace(
        catalog_fact, field_key="brightness_typical", value=stored,
        scope="PRODUCT_KNOWLEDGE")]
    kept = ProductKnowledgeService._peak_brightness_relabel(
        [catalog_fact], integrated, {"spec": spec})
    assert [item.field_key for item in kept] == (
        [expected] if expected else [])


def test_withholding_needs_both_sides():
    """With no stored reading to contradict it, the token stays as it is."""

    catalog_fact = _catalog_fact("1,000cd")
    kept = ProductKnowledgeService._peak_brightness_relabel(
        [catalog_fact], [], {"spec": "240Hz / 1,000cd / HDR10+"})
    assert [item.field_key for item in kept] == [
        "brightness_typical_cd_m2"]
