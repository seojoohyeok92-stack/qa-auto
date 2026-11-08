"""Which product a lookup is about, for two shapes the resolution missed.

694966530: "25년형과 26년형 차이가 뭔가요" on listing 10914735269, whose options
are 24년형 LH43BED, 25년형 LH43BEF and 26년형 LH43BEH. The question names no
model code, so the lookup fell back to the listing's API identity -- the
24년형 -- and the draft told a customer holding a 26년형 that their product
was released in 2024년 9월. The options say which model each year is.

11316: "LS43FM701 호환되나요" on the VI-201-S stand's page. Naming the display
made it the only target, so the stand's own limits (17~35인치, 2~10kg, VESA
75/100) never reached the prompt and a Learning about another product
answered instead. Compatibility is a property of the pair.

Both run on the shipped catalogue and without a provider.
"""

from __future__ import annotations

from collections import Counter

import pytest

from answer.models import AnswerRequest
from repositories.product_catalog_repository import ProductCatalogRepository
from services.hybrid_answer_service import HybridAnswerService
from services.product_fact_guard import extract_model_code
from services.product_knowledge_service import (
    ACCESSORY_WITH_NAMED_DEVICE,
    INQUIRY_EXPLICIT_MODEL,
    LISTING_FALLBACK,
    LISTING_OPTION_MODEL,
    MULTI_MODEL_COMPARISON,
    ProductKnowledgeService,
)

YEAR_LISTING = "10914735269"
YEAR_TITLE = "삼성 107.9cm(43인치) 4K UHD LH43B 스마트 비즈니스TV 1등급 스탠드형"
STAND_LISTING = "10470584846"
STAND_TITLE = "3세대 삼탠바이미 이동식 TV 거치대 삼성 M5 M7 M8 LG 룸앤티비 스마트 모니터"
BATTERY_LISTING = "10358299339"
BATTERY_TITLE = (
    "삼탠배터리 VIPB01W 무탠바이미 무선 삼탠바이미 만들기 결합식 배터리"
    " 이동식거치대 삼성 M5 80.1cm TV모니터 스탠드"
)


@pytest.fixture(scope="module")
def service() -> ProductKnowledgeService:
    return ProductKnowledgeService(ProductCatalogRepository())


def _lookup(service, product_id, title, question, *, include_all=True):
    return service.facts_for_inquiry(
        product_id=product_id, question=question,
        model_code=extract_model_code(title), product_name=title,
        include_all_catalog_fields=include_all,
    )


def _models(result) -> Counter:
    return Counter(str(fact.model_code) for fact in result.safe_facts)


def _identity_block(result, question, title) -> dict:
    request = AnswerRequest(question=question, product_name=title)
    request.metadata["product_knowledge"] = result
    context = HybridAnswerService._product_facts_context(request)
    return context.get("product_identity") or {}


# --- 694966530: model years named through the listing's options -------------

def test_694966530_compares_the_25_and_26_records_not_the_24(service):
    question = (
        "25년형과 26년형 차이가 뭔가요 26년형을 두대 구매했는데 소비자들"
        " 구매보니까 거의 25'년혀믈 구매하던대요 차이점을 자세히 설명 부탁드립니다"
    )
    result = _lookup(service, YEAR_LISTING, YEAR_TITLE, question)

    assert result.target_resolution_reason == LISTING_OPTION_MODEL
    assert result.resolved_pk_targets == ("43BEF", "43BEH")
    models = _models(result)
    assert not any("43BED" in model for model in models)
    assert any("43BEF" in model for model in models)
    assert any("43BEH" in model for model in models)


def test_the_26_release_is_not_reported_as_the_24_release(service):
    result = _lookup(
        service, YEAR_LISTING, YEAR_TITLE, "26년형 출시일이 언제인가요",
        include_all=False,
    )

    releases = {
        (fact.model_code, str(fact.value)) for fact in result.safe_facts
        if fact.field_key.startswith("release")
    }
    assert releases
    assert all("43BEH" in model for model, _value in releases)
    assert not any("2024" in value for _model, value in releases)


def test_the_listings_own_year_keeps_the_listing_lookup(service):
    plain = _lookup(service, YEAR_LISTING, YEAR_TITLE, "벽걸이 가능한가요")
    own_year = _lookup(service, YEAR_LISTING, YEAR_TITLE, "24년형인가요")

    assert own_year.target_resolution_reason == LISTING_FALLBACK
    assert own_year.resolved_pk_targets == plain.resolved_pk_targets
    assert _models(own_year) == _models(plain)


def test_the_listings_own_year_beside_another_is_looked_up_as_the_listing(service):
    result = _lookup(service, YEAR_LISTING, YEAR_TITLE, "24년형이랑 26년형 차이")

    assert result.target_resolution_reason == LISTING_OPTION_MODEL
    assert result.resolved_pk_targets == ("43BED", "43BEH")


def test_a_year_this_listing_does_not_sell_changes_nothing(service):
    result = _lookup(service, YEAR_LISTING, YEAR_TITLE, "27년형 있나요")

    assert result.target_resolution_reason == LISTING_FALLBACK
    assert result.resolved_pk_targets == ("43BED",)


def test_a_year_two_options_share_is_not_settled_by_picking_one(service):
    # 11363535046 sells both LH43BEHH and LH50BEHH as 26년형.
    result = service.facts_for_inquiry(
        product_id="11363535046", question="26년형 출시일",
        include_all_catalog_fields=True,
    )

    assert result.target_resolution_reason == LISTING_FALLBACK


def test_a_model_code_still_wins_over_the_year(service):
    result = _lookup(service, YEAR_LISTING, YEAR_TITLE, "26년형 LH43BEF 출시일")

    assert result.target_resolution_reason == INQUIRY_EXPLICIT_MODEL
    assert result.resolved_pk_targets == ("43BEF",)


def test_the_prompt_says_the_years_came_from_the_options_and_records_are_uneven(service):
    question = "25년형과 26년형 차이가 뭔가요"
    block = _identity_block(
        _lookup(service, YEAR_LISTING, YEAR_TITLE, question), question, YEAR_TITLE)

    assert block["listing_model"] == "43BED"
    assert block["answer_target_models"] == ["43BEF", "43BEH"]
    usage = str(block["usage"])
    assert "옵션" in usage
    assert "차이로 단정하지" in usage


def test_a_single_year_prompt_has_no_comparison_note(service):
    question = "26년형 출시일이 언제인가요"
    block = _identity_block(
        _lookup(service, YEAR_LISTING, YEAR_TITLE, question), question, YEAR_TITLE)

    assert block["answer_target_models"] == ["43BEH"]
    assert "차이로 단정하지" not in str(block["usage"])


def test_an_explicit_comparison_also_warns_about_uneven_records(service):
    question = "LH43BEF와 LH43BEH 차이가 뭔가요"
    result = _lookup(service, YEAR_LISTING, YEAR_TITLE, question)

    assert result.target_resolution_reason == MULTI_MODEL_COMPARISON
    assert "차이로 단정하지" in str(
        _identity_block(result, question, YEAR_TITLE)["usage"])


# --- 11316: an accessory's page and the device it is meant for --------------

def test_11316_carries_the_stand_beside_the_monitor(service):
    result = _lookup(service, STAND_LISTING, STAND_TITLE, "LS43FM701 호환되나요")

    assert result.target_resolution_reason == ACCESSORY_WITH_NAMED_DEVICE
    assert result.resolved_pk_targets == ("VI-201-S", "43FM701")
    stand = {
        fact.field_key: str(fact.value) for fact in result.safe_facts
        if fact.model_code == "VI-201-S"
    }
    monitor = {
        fact.field_key: str(fact.value) for fact in result.safe_facts
        if fact.model_code == "S43FM701"
    }
    assert stand["recommended_screen_size"] == "17~35인치 TV"
    assert stand["recommended_load"] == "2~10"
    assert "100X100" in stand["vesa"]
    assert monitor["screen_size"] == "43인치"
    assert monitor["vesa_mm"] == "200x200"
    assert monitor["weight_catalog"] == "8.5kg"


def test_the_stand_record_is_the_one_the_listing_alone_reaches(service):
    alone = _lookup(service, STAND_LISTING, STAND_TITLE, "몇 인치까지 되나요")
    paired = _lookup(service, STAND_LISTING, STAND_TITLE, "LS43FM701 호환되나요")

    def stand_facts(result):
        return {
            (fact.field_key, str(fact.value)) for fact in result.safe_facts
            if fact.model_code == "VI-201-S"
        }

    assert stand_facts(alone)
    assert stand_facts(alone) == stand_facts(paired)


def test_the_compatibility_prompt_does_not_settle_on_vesa_alone(service):
    question = "LS43FM701 호환되나요"
    block = _identity_block(
        _lookup(service, STAND_LISTING, STAND_TITLE, question),
        question, STAND_TITLE)

    assert block["listing_model"] == "VI-201-S"
    assert block["answer_target_models"] == ["VI-201-S", "43FM701"]
    usage = str(block["usage"])
    assert "VESA 가 맞아도" in usage
    assert "권장 인치" in usage
    assert "과거 답변" in usage


def test_the_battery_page_keeps_the_battery_beside_the_monitor(service):
    result = _lookup(
        service, BATTERY_LISTING, BATTERY_TITLE,
        "LS32FM700M-2MS 제품과 호환이 될까요")

    assert result.target_resolution_reason == ACCESSORY_WITH_NAMED_DEVICE
    assert result.resolved_pk_targets[0] == "VIPB01W"
    assert "32FM700" in result.resolved_pk_targets


def test_an_accessory_page_without_a_named_device_is_unchanged(service):
    result = _lookup(service, STAND_LISTING, STAND_TITLE, "몇 인치까지 되나요")

    assert result.target_resolution_reason == LISTING_FALLBACK


def test_a_display_page_naming_another_display_is_unchanged(service):
    result = _lookup(service, YEAR_LISTING, YEAR_TITLE, "LH43BEH 무게가 얼마예요?")

    assert result.target_resolution_reason == INQUIRY_EXPLICIT_MODEL
    assert result.resolved_pk_targets == ("43BEH",)


def test_only_catalogued_accessories_count():
    repository = ProductCatalogRepository()

    assert repository.accessory_model_for_listing(
        product_name=STAND_TITLE) == "VI-201-S"
    assert repository.accessory_model_for_listing(
        model_code="VIPB01W") == "VIPB01W"
    assert repository.accessory_model_for_listing(
        product_name=YEAR_TITLE, model_code="LH43B") is None
    assert repository.accessory_model_for_listing() is None
