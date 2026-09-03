"""상품 사양은 그 사양을 물은 질문에만 근거가 된다.

COHORT_1 실데이터 40건을 상품 경로로 통과시켜 확인한 것.

inquiry 1505 "구매하면 기존 벽걸이 티비 제거는 무상인가요?" 는 카탈로그에서
VESA 600x400 을 근거로 받았고, ``supports_question`` 이 True 를 돌려주어
민감 상품사실 hold 까지 풀릴 수 있는 상태였다. 고객이 물은 것은 *기존에 쓰던*
티비를 떼어가는 비용이고, VESA 는 *파는 제품*의 구멍 규격이다. 서로 다른
물건에 대한 사실이다.

두 자리가 겹쳐서 생겼다.

* 토픽 선택이 단어의 존재만 봤다. '벽걸이' 가 문장에 있으면 VESA 질문으로
  읽혔다. 이 파일에는 이미 ``EXCLUSION_MARKERS`` -- 고객이 무언가를 질문에서
  *빼려고* 부른 이름 -- 라는 판단이 있었는데, 부품 범위에만 쓰이고 토픽
  선택에는 쓰이지 않았다.
* ``supports_question`` 이 "주장을 하나도 식별하지 못함" 을 "모든 주장이
  충족됨" 으로 돌려줬다(``return self.has_safe_facts``). 카탈로그에 값이
  하나라도 있으면 무슨 질문이든 뒷받침된 것이 되었다.

아래는 그 두 자리의 불변식이고, 진짜 사양 질문이 계속 답해지는지를 같은 수의
대조군으로 함께 고정한다. 실제 상품 JSON 은 건드리지 않는다 -- 여기 카탈로그는
이 테스트만의 것이다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_knowledge_service import (
    ProductKnowledgeService,
    fields_for_question,
)


CATALOG = {
    "MODEL_CATALOG": {
        "LH85TESTH": {
            "model": "LH85TESTH", "brand": "삼성", "size_inch": "85인치",
            "resolution": "3840 x 2160(4K UHD)", "hz": "60Hz",
            "vesa": "600x400", "weight": "43.2kg", "speaker": True,
            "spec": "패널:VA패널 / HDMI 3개 / USB 2개 / RF 단자 / "
                    "스탠드 다리 사이 간격: 1200mm",
        },
    },
    "MODEL_ALIASES": {"BE85TEST": "LH85TESTH"},
}
PRODUCT = "삼성 214.7cm(85인치) 4K UHD BE85TEST 스마트 비즈니스TV"


@pytest.fixture
def service(tmp_path: Path) -> ProductKnowledgeService:
    path = tmp_path / "model_data_with_color.json"
    path.write_text(json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8")
    return ProductKnowledgeService(
        catalog_repository=ProductCatalogRepository(path)
    )


def ask(service: ProductKnowledgeService, question: str):
    return service.facts_for_inquiry(
        product_id="listing-1", questions=[question], question=question,
        model_code=None, product_name=PRODUCT, option_name=None,
    )


# 고객이 파는 제품의 사양을 실제로 물은 질문. 계속 답해져야 한다.
REAL_SPEC_QUESTIONS = (
    ("이 티비 베사 규격이 어떻게 되나요?", "vesa_mm"),
    ("vesa 사이즈 알려주세요", "vesa_mm"),
    ("화면이 몇 인치인가요?", "screen_size"),
    ("해상도가 4k 맞나요?", "resolution"),
    ("주사율 몇 hz 인가요?", "refresh_rate"),
    ("무게가 몇 kg 인가요?", "weight_catalog"),
)

# 사양 단어가 들어 있지만 그 사양을 묻는 질문이 아닌 것.
NOT_A_SPEC_QUESTION = (
    "구매하면 기존 벽걸이 티비 제거는 무상인가요?",
    "기존 벽걸이 브라켓 제거도 같이 해주시나요?",
    "스탠드 제외하고 배송해주실 수 있나요?",
    "설치기사님이 언제 방문하시나요?",
    "배송은 며칠 걸리나요?",
    "환불하려면 어떻게 하나요?",
)


# ==========================================================================
# 1. 다른 물건을 가리킨 단어는 이 제품의 토픽을 열지 않는다
# ==========================================================================


def test_a_mount_named_only_to_have_it_removed_is_not_a_vesa_question(
    service: ProductKnowledgeService,
) -> None:
    """inquiry 1505 그 자체. '벽걸이' 는 떼어갈 물건의 이름이었다."""

    question = "구매하면 기존 벽걸이 티비 제거는 무상인가요?"
    fields, topics = fields_for_question(question)

    assert topics == ()
    assert fields == ()
    assert ask(service, question).supports_question(question) is False


def test_the_same_word_asked_about_this_product_still_opens_the_topic(
    service: ProductKnowledgeService,
) -> None:
    """대조군 -- '제거' 가 없으면 '벽걸이' 는 그대로 VESA 토픽이다."""

    question = "벽걸이 설치하려는데 이 제품 베사 규격 알려주세요"
    fields, topics = fields_for_question(question)

    assert "vesa_mm" in fields
    knowledge = ask(service, question)
    assert knowledge.supports_question(question) is True
    assert [f.value for f in knowledge.safe_facts if f.field_key == "vesa_mm"] == [
        "600x400"
    ]


# ==========================================================================
# 2. 식별하지 못한 주장은 충족된 주장이 아니다
# ==========================================================================


@pytest.mark.parametrize("question", NOT_A_SPEC_QUESTION)
def test_a_catalogued_value_does_not_vouch_for_an_unasked_question(
    service: ProductKnowledgeService, question: str,
) -> None:
    assert ask(service, question).supports_question(question) is False


@pytest.mark.parametrize("question,field", REAL_SPEC_QUESTIONS)
def test_a_real_specification_question_is_still_answered(
    service: ProductKnowledgeService, question: str, field: str,
) -> None:
    knowledge = ask(service, question)

    assert knowledge.matched is True
    assert field in knowledge.safe_field_keys(), sorted(knowledge.safe_field_keys())
    assert knowledge.supports_question(question) is True


# ==========================================================================
# 3. 모델을 확정하지 못하면 이웃 모델의 사양을 빌려오지 않는다
# ==========================================================================


def test_an_unidentified_model_yields_no_facts(
    service: ProductKnowledgeService,
) -> None:
    """카탈로그에 없는 상품명이면 UNKNOWN 이지, 가장 비슷한 모델이 아니다."""

    result = service.facts_for_inquiry(
        product_id="listing-2", questions=["베사 규격이 어떻게 되나요?"],
        question="베사 규격이 어떻게 되나요?", model_code=None,
        product_name="삼성 4K UHD 스마트 사이니지 TV 기사님 방문설치 125.7cm(50인치)",
        option_name=None,
    )

    assert result.matched is False
    assert result.safe_facts == ()
    assert result.unavailable_reason in {
        "PRODUCT_CATALOG_MODEL_NOT_FOUND", "PRODUCT_CATALOG_AMBIGUOUS",
    }


def test_two_aliases_pointing_at_different_models_stay_unresolved(
    tmp_path: Path,
) -> None:
    """실제 카탈로그에서 관측된 모양: 크기만 적힌 alias 와 전체 상품명 alias 가
    서로 다른 모델을 가리킨다. 더 그럴듯한 쪽을 고르지 않고 멈춘다."""

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "MODEL_CATALOG": {
            "LH50AAAH": {"model": "LH50AAAH", "vesa": "200x200"},
            "LH50BBBH": {"model": "LH50BBBH", "vesa": "400x400"},
        },
        "MODEL_ALIASES": {
            "삼성 125.7cm(50인치)": "LH50AAAH",
            "삼성 125.7cm(50인치) 방문설치 스탠드": "LH50BBBH",
        },
    }, ensure_ascii=False), encoding="utf-8")
    service = ProductKnowledgeService(
        catalog_repository=ProductCatalogRepository(path)
    )

    result = service.facts_for_inquiry(
        product_id="listing-3", questions=["베사 규격 알려주세요"],
        question="베사 규격 알려주세요", model_code=None,
        product_name="삼성 125.7cm(50인치) 방문설치 스탠드", option_name=None,
    )

    assert result.matched is False
    assert result.unavailable_reason == "PRODUCT_CATALOG_AMBIGUOUS"
    assert result.safe_facts == ()


def test_a_field_the_catalog_does_not_carry_stays_unknown(
    service: ProductKnowledgeService,
) -> None:
    """OTT 지원 여부는 JSON 에 없다. 없는 것은 '아니오' 가 아니라 모르는 것이다."""

    question = "넷플릭스 되나요?"
    knowledge = ask(service, question)

    assert "ott_supported_services" in knowledge.requested_fields
    assert knowledge.safe_facts == ()
    assert knowledge.supports_question(question) is False


# ==========================================================================
# 4. 운영 경로는 JSON 카탈로그이고 product_facts.db 가 아니다
# ==========================================================================


def test_the_production_default_never_constructs_the_retired_facts_db() -> None:
    from repositories.product_catalog_repository import (
        DEFAULT_PRODUCT_CATALOG_PATH,
    )

    service = ProductKnowledgeService()

    assert service.repository is None
    assert service.catalog_repository.path == DEFAULT_PRODUCT_CATALOG_PATH.resolve()
    assert service.catalog_repository.path.name == "model_data_with_color.json"


def test_no_product_facts_database_is_opened_on_the_default_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """운영 기본 경로로 상품 조회를 해도 product_facts.db 는 열리지 않는다."""

    import sqlite3

    opened: list[str] = []
    real_connect = sqlite3.connect

    def watching(database, *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", watching)

    ProductKnowledgeService().facts_for_inquiry(
        product_id="listing-4", questions=["베사 규격 알려주세요"],
        question="베사 규격 알려주세요", model_code=None,
        product_name="삼성 214.7cm(85인치) 4K UHD BE85F 스마트 비즈니스TV",
        option_name=None,
    )

    assert [p for p in opened if "product_facts" in p.lower()] == []
