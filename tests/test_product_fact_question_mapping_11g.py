"""Phase 11-G: the customer phrasings that reach a Product Fact, and the ones
that must not.

Every mapping added here was grounded in a fact the shipped database actually
holds, and every one is pinned with the question it must NOT answer. The pairs
matter more than the positives: a keyword that pulls the wrong field is worse
than one that pulls nothing, because SAFE_UNKNOWN is a correct answer and a
wrong fact is not.

Fixture-level only -- no database, no network, no provider.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from answer.text_utils import is_missing_item_report
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


def requested(question: str) -> set[str]:
    return set(fields_for_question(question)[0])


# ===================================================== 소비전력 vs 전원 케이블 ==
@pytest.mark.parametrize("question", [
    "소비전력이 얼마인가요?",
    "소비 전력 알려주세요",
    "전력 얼마나 써요?",
    "전기 얼마나 먹나요?",
    "전기 많이 먹나요?",
    "전기요금 많이 나오나요?",
])
def test_power_draw_phrasings_reach_consumption(question) -> None:
    assert "power_consumption_typical_w" in requested(question)


@pytest.mark.parametrize("question", [
    "전원 케이블 포함인가요?",
    "전원선 들어있나요?",
    "파워 케이블 주나요?",
])
def test_a_cable_question_is_not_a_consumption_question(question) -> None:
    """How much electricity it uses and which cable is in the box are two
    questions. "전기" was deliberately not keyed on its own for this reason."""

    fields = requested(question)
    assert "power_cable_included" in fields
    assert "power_consumption_typical_w" not in fields
    assert "power_consumption_max_w" not in fields


# ============================================================ 리모컨 ==========
@pytest.mark.parametrize("question", [
    "리모컨 포함인가요?",
    "리모컨 들어있나요?",
    "리모콘 포함되나요?",
    "리모컨 동봉되나요?",
    "리모컨도 주나요?",
    "리모컨 같이 오나요?",
])
def test_remote_inclusion_phrasings_reach_the_included_fact(question) -> None:
    assert requested(question) == {"remote_control_included"}


@pytest.mark.parametrize("question", [
    "리모컨이 안 왔어요",
    "리모컨이 누락됐어요",
])
def test_a_missing_remote_is_not_an_inclusion_question(question) -> None:
    """The report and the question share a word and nothing else. The report is
    refused upstream; here we only pin that it never asks for a fact."""

    assert requested(question) == set()
    assert is_missing_item_report(question) is True


def test_a_remote_model_question_does_not_get_the_display_model() -> None:
    """The right field for the wrong subject.

    "리모컨 모델명이 뭐예요?" contains 모델명, so the topic map offers the
    model fields -- but they name the television, not its remote. The component
    subject gate is what stops it, which is why the model fields belong to the
    identity set.
    """

    fields = requested("리모컨 모델명이 뭐예요?")
    assert "model_name" in fields          # the map does request it...
    assert asks_about_a_bundled_component("리모컨 모델명이 뭐예요?") is True


# =========================================================== OTT / 서비스 =====
def test_a_named_service_is_not_the_category() -> None:
    """"OTT 지원" may not be read as "YouTube 지원", so they key different
    fields and neither borrows the other's."""

    youtube = requested("유튜브 되나요?")
    assert youtube == {"youtube_supported"}
    assert "ott_supported" not in youtube

    ott = requested("OTT 볼 수 있나요?")
    assert "ott_supported" in ott
    assert "youtube_supported" not in ott


def test_netflix_only_reaches_the_field_that_could_name_it() -> None:
    fields = requested("넷플릭스 되나요?")
    assert fields == {"ott_supported_services"}
    assert "ott_supported" not in fields


def test_tv_plus_is_its_own_service() -> None:
    assert requested("TV플러스 되나요?") == {"tv_plus"}


# ============================================================== 설치 =========
@pytest.mark.parametrize("question", [
    "설치 방법 알려주세요",
    "설치는 어떻게 하나요?",
    "어떻게 설치하나요?",
    "자가설치인가요?",
    "기사님이 설치해주시나요?",
])
def test_how_it_is_installed_is_a_product_fact(question) -> None:
    assert "installation_method" in requested(question)


@pytest.mark.parametrize("question", [
    "설치 언제 오나요?",
    "기사님 언제 오나요?",
    "오늘 설치 예정 맞나요?",
    "설치 날짜 알려주세요",
])
def test_when_it_is_installed_is_not_a_product_fact(question) -> None:
    """The schedule belongs to the customer's own order, not to the product."""

    assert requested(question) == set()


# ========================================================= 휴대폰 연결 ========
@pytest.mark.parametrize("question", [
    "휴대폰이랑 연결돼요?",
    "핸드폰 연결할 수 있나요?",
    "스마트폰 연결 지원하나요?",
    "휴대폰 연동 되나요?",
])
def test_an_unnamed_phone_connection_offers_every_method(question) -> None:
    """The customer did not say which method, so no single one is assumed."""

    fields = requested(question)
    assert {"bluetooth_present", "screen_mirroring"} <= fields
    assert "wireless_display" in fields


@pytest.mark.parametrize("question", [
    "폰 화면 띄울 수 있어요?",
    "휴대폰 화면 미러링 돼요?",
    "핸드폰 화면 보여줄 수 있나요?",
])
def test_a_named_screen_question_reaches_mirroring(question) -> None:
    assert "screen_mirroring" in requested(question)


# ================================================= component subject ========
@pytest.mark.parametrize("question", [
    "리모컨 모델명이 뭐예요?",
    "스탠드 모델명이 뭐예요?",
    "셋톱박스 모델코드가 뭐예요?",
])
def test_a_component_never_borrows_the_listing_identity(question) -> None:
    assert asks_about_a_bundled_component(question) is True


def test_the_listings_own_model_question_is_unaffected() -> None:
    assert asks_about_a_bundled_component("모델명이 뭐예요?") is False
    assert "model_name" in requested("모델명이 뭐예요?")


# ====================================================== real shipped DB =====
@real_db
def test_real_db_component_model_question_is_withheld() -> None:
    service = ProductKnowledgeService(ProductFactRepository(REAL_DB))
    connection = sqlite3.connect(
        REAL_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        products = [row[0] for row in connection.execute(
            "SELECT product_id FROM listings ORDER BY product_id")]
    finally:
        connection.close()

    checked = 0
    for product_id in products:
        own = service.facts_for_inquiry(
            product_id=product_id, question="모델명이 뭐예요?")
        if "model_name" not in own.safe_field_keys():
            continue
        checked += 1
        component = service.facts_for_inquiry(
            product_id=product_id, question="리모컨 모델명이 뭐예요?")
        assert "model_name" not in component.safe_field_keys(), product_id
        # One field can carry several excluded rows -- superseded copies beside
        # the current one -- so the reasons are collected rather than keyed.
        reasons = {item.exclusion_reason for item in component.excluded_facts
                   if item.field_key == "model_name"}
        assert "COMPONENT_SUBJECT_UNRESOLVED" in reasons, (product_id, reasons)
    assert checked, "no product answers its own model question"


@real_db
def test_real_db_new_mappings_only_offer_verified_facts() -> None:
    """Whatever the new phrasings reach must still pass every existing gate."""

    service = ProductKnowledgeService(ProductFactRepository(REAL_DB))
    connection = sqlite3.connect(
        REAL_DB.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        products = [row[0] for row in connection.execute(
            "SELECT product_id FROM listings ORDER BY product_id")]
    finally:
        connection.close()

    questions = [
        "전기 얼마나 먹나요?", "리모컨 포함인가요?", "유튜브 되나요?",
        "설치는 어떻게 하나요?", "휴대폰이랑 연결돼요?", "TV플러스 되나요?",
        "전원 케이블 포함인가요?",
    ]
    offered = 0
    for product_id in products:
        for question in questions:
            result = service.facts_for_inquiry(
                product_id=product_id, question=question)
            for fact in result.safe_facts:
                offered += 1
                assert fact.product_id == product_id
                assert fact.verification_status == "VERIFIED"
                assert fact.resolution_status not in {"CONFLICT", "NEEDS_REVIEW"}
                assert fact.lifecycle_status == "ACTIVE"
                assert fact.provenance, (product_id, fact.field_key)
    assert offered, "the new phrasings reached nothing at all"
