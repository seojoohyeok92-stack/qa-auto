"""Phase 11-FINAL: which subject a weight or a VESA pattern belongs to.

A package listing measures the same quantities twice. The display has a
weight and a mounting pattern; so does the stand bundled beside it. The field
names keep the two apart in the database, but only the customer's wording says
which subject was asked about -- and a number that is right for the other
subject is worse than no number at all, because the customer acts on it.

Two directions, one boundary:

* "거치대가 몇 kg까지 버티나요?" may not be answered with the television's
  5.5 kg, even though the prompt would label it BASE_DEVICE;
* a plain "무게 알려주세요" may not be answered with the stand's load rating,
  which is not a weight at all but the weight it is rated to carry.

And one thing that must keep working: naming a component in order to exclude
it ("스탠드 제외하고 본체 무게") is a question about the display. Real
inquiry #2704 is exactly that sentence, and it has a correct answer.

Fixture-level plus the shipped database -- no network, no provider.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from repositories.product_fact_repository import ProductFactRepository
from services.product_knowledge_service import (
    ProductKnowledgeService,
    asks_about_a_bundled_component,
    fields_for_question,
)

REAL_DB = Path("data") / "product_facts.db"
real_db = pytest.mark.skipif(
    not REAL_DB.is_file(), reason="data/product_facts.db not present"
)

BASE_WEIGHT = {"weight_with_stand_kg", "weight_without_stand_kg",
               "package_weight_kg"}
ACCESSORY_WEIGHT = {"accessory_weight_kg", "accessory_package_weight_kg",
                    "accessory_max_load_kg"}


@pytest.fixture(name="service")
def _service() -> ProductKnowledgeService:
    return ProductKnowledgeService(ProductFactRepository(REAL_DB))


def products() -> list[str]:
    connection = sqlite3.connect(REAL_DB.resolve().as_uri() + "?mode=ro",
                                 uri=True)
    try:
        return [row[0] for row in connection.execute(
            "SELECT product_id FROM listings ORDER BY product_id")]
    finally:
        connection.close()


# ============================================== 제외 어법은 본체 질문이다 ====
@pytest.mark.parametrize("question", [
    "이 제품 스탠드 제외하고 본체 무게가 몇 kg인가요?",
    "스탠드 빼고 무게 알려주세요",
    "스탠드 없이 무게가 어떻게 되나요?",
    "받침대 미포함 무게 알려주세요",
])
def test_naming_a_part_to_exclude_it_is_not_asking_about_it(question) -> None:
    assert asks_about_a_bundled_component(question) is False


@pytest.mark.parametrize("question", [
    "거치대 최대 지원 무게가 얼마인가요?",
    "스탠드가 몇 kg까지 버티나요?",
    "거치대 VESA 범위 알려주세요",
    "리모컨 모델명이 뭐예요?",
])
def test_a_part_named_without_exclusion_still_sets_the_subject(question) -> None:
    """The Phase 11-G boundary is unchanged: only the exclusion wording is
    new, and it may not become a way around the component gate."""

    assert asks_about_a_bundled_component(question) is True


def test_one_excluded_mention_does_not_excuse_another() -> None:
    """Two parts, one excluded and one asked about. The question is still
    about a component, so the gate must stay closed."""

    assert asks_about_a_bundled_component(
        "스탠드 제외하고 리모컨 모델명 알려주세요") is True


# ================================================ 실제 DB: 두 방향 모두 ======
@real_db
def test_real_db_a_load_question_never_gets_the_display_weight(service) -> None:
    for product_id in products():
        for question in ("거치대 최대 지원 무게가 얼마인가요?",
                         "스탠드가 몇 kg까지 버티나요?"):
            offered = service.facts_for_inquiry(
                product_id=product_id, question=question).safe_field_keys()
            assert not (BASE_WEIGHT & set(offered)), (product_id, question)


@real_db
def test_real_db_a_load_question_never_gets_the_display_vesa(service) -> None:
    for product_id in products():
        offered = service.facts_for_inquiry(
            product_id=product_id,
            question="거치대 VESA 범위 알려주세요").safe_field_keys()
        assert "vesa_mm" not in offered, product_id


@real_db
def test_real_db_a_plain_weight_question_never_gets_the_stands_rating(
        service) -> None:
    """The other direction. A load rating answering "무게 알려주세요" would
    hand the customer a number that describes what the stand carries."""

    for product_id in products():
        for question in ("무게 알려주세요", "제품 무게가 얼마인가요?"):
            offered = service.facts_for_inquiry(
                product_id=product_id, question=question).safe_field_keys()
            assert not (ACCESSORY_WEIGHT & set(offered)), (product_id, question)


@real_db
def test_real_db_the_withheld_weight_is_labelled_not_denied(service) -> None:
    """Withheld means excluded with a reason, never absent without one --
    otherwise a later reader cannot tell a gated fact from an uncollected."""

    seen = set()
    for product_id in products():
        result = service.facts_for_inquiry(
            product_id=product_id, question="스탠드가 몇 kg까지 버티나요?")
        for item in result.excluded_facts:
            if item.field_key in BASE_WEIGHT:
                seen.add(item.exclusion_reason)
    assert "COMPONENT_SUBJECT_UNRESOLVED" in seen, seen


@real_db
def test_real_db_inquiry_2704_still_has_its_answer(service) -> None:
    """The sentence a customer actually sent. It asks for the weight without
    the stand, and the database holds exactly that field."""

    result = service.facts_for_inquiry(
        product_id="13239109816",
        question="이 제품 스탠드 제외하고 본체 무게가 몇 kg인가요?")
    assert "weight_without_stand_kg" in result.safe_field_keys()
    # ...and the parts it did not ask about stay out of the answer.
    assert not (ACCESSORY_WEIGHT & set(result.safe_field_keys()))


# =========================================== G2: 명백한 설치 표현만 추가 ======
@pytest.mark.parametrize("question", [
    "본인이 직접 설치하기는 어려운가요?",
    "혼자서 설치 가능한가요?",
    "혼자 설치 가능합니까?",
])
def test_clear_self_install_phrasings_reach_only_installation_facts(
        question) -> None:
    fields = set(fields_for_question(question)[0])
    assert "installation_method" in fields
    assert fields <= {"installation_method", "package_professional_installation"}


@pytest.mark.parametrize("product_id", ["12021985151", "13239109816"])
@real_db
def test_real_g2_products_have_verified_installation_evidence(
        service, product_id) -> None:
    result = service.facts_for_inquiry(
        product_id=product_id, question="혼자서 설치 가능한가요?")
    assert "installation_method" in result.safe_field_keys()
    fact = next(item for item in result.safe_facts
                if item.field_key == "installation_method")
    assert fact.value == "PROFESSIONAL_TECHNICIAN_REQUIRED"
    assert fact.verification_status == "VERIFIED"
    assert fact.provenance


@pytest.mark.parametrize("question", [
    "제 주문 언제 설치돼요?",
    "내일 배송되나요?",
    "설치 기사 언제 와요?",
])
def test_g2_self_install_mapping_does_not_capture_order_schedule(
        question) -> None:
    fields = set(fields_for_question(question)[0])
    assert "installation_method" not in fields
    assert "package_professional_installation" not in fields
