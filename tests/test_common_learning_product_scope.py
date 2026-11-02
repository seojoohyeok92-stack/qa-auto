"""공통 Learning "현재 판매 페이지"(PRODUCT) 범위.

"현재 모델" 은 모델코드로 판정되어, 같은 모델을 파는 다른 판매 페이지에도
적용된다. 26.10.30 사본 재생에서 확인한 사례: LH85BEHHLGFXKR 이동식 거치대
답변(13684622431)이 같은 모델의 벽걸이형 판매 페이지(13684631171) 문의에서
상위 5위로 선택되었다. 설치 옵션·배송 방식은 판매 페이지마다 다르다 --
9645661432 는 벽걸이를 기본 옵션으로, LH43B/LH50B 판매 페이지는 추가상품으로
판다.

고정하는 것:

* PRODUCT 는 승인 시 고정한 판매 페이지 하나에만 적용된다. 모델코드·상품명·
  크기가 같아도 다른 판매 페이지에는 적용되지 않고, 문의에 판매 페이지 ID 가
  없으면 적용되지 않는다.
* 판매 페이지 ID 는 마켓 안에서만 유일하므로 마켓도 범위에 포함된다.
* 같은 판매 페이지 안에서도 양쪽이 밝힌 모델·크기가 다르면 다른 옵션이다.
* 검색 점수와 전달 개수는 바뀌지 않는다. MODEL / POLICY / 일반 Learning 동작도
  그대로다.
"""
from __future__ import annotations

import json

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from services.similar_answer_service import SimilarAnswerService


STORE_NAVER = "OJE_PLUS"
STORE_COUPANG = "COUPANG_OJE"

SIGNAGE_43 = "삼성 4K UHD 스마트 사이니지 TV 1등급 티비 기사님 방문설치 107.9cm(43인치), 스탠드"
SIGNAGE_50 = "삼성 4K UHD 스마트 사이니지 TV 1등급 티비 기사님 방문설치 125.7cm(50인치), 스탠드"
LH50BED = "삼성 125.7cm(50인치) 스마트 비즈니스 TV 4K UHD LED LH50BED-H 선명한 화질 스탠드형"
LH50BED_OTHER = "삼성 50인치 비즈니스TV LH50BED-H 4K UHD 기사설치 스탠드"
LH85_STAND = "삼성 4K UHD 스마트 비즈니스 TV LH85BEHHLGFXKR 1등급 214.7cm(85인치), 스탠드"
LH85_WALL = "삼성 214.7cm(85인치) UHD 4K 1등급 비즈니스 TV LH85BEHHLGFXKR 벽걸이형"

BRACKET_Q = "기존에 사용하던 벽걸이 브라켓에 그대로 설치해 주시나요?"
BRACKET_A = (
    "기존 브라켓을 그대로 사용할 수 있는지는 방문하시는 삼성 설치 기사님이 현장에서 "
    "호환 여부를 확인해야 합니다. 벽걸이형 제품으로 선택해서 결제해 주시고, 기사님께서 "
    "기존 벽걸이암에 장착 가능하다고 확인하시면 벽걸이암 비용은 환불 가능합니다."
)
STAND_Q = "스탠드형으로 구매해도 이동식 거치대 스탠드에 거치해서 사용할 수 있나요?"
STAND_A = (
    "이동식 거치대 스탠드를 별도로 구매하셔도 사용은 가능하지만, 거치대 설치는 "
    "지원되지 않아 고객님께서 직접 설치해 주셔야 합니다. VESA 규격은 400 x 300입니다."
)
STAIR_Q = "계단으로 올라오셔야 하는데 배송비가 붙나요?"
STAIR_A = (
    "계단 배송에 따른 추가 비용은 현장 상황에 따라 달라질 수 있습니다. "
    "정확한 부과 여부와 금액은 배송기사님과 협의해 주세요."
)


@pytest.fixture()
def repository(tmp_path) -> LearningRepository:
    database = Database(tmp_path / "product_scope.db")
    database.initialize()
    return LearningRepository(database)


def _inquiry(
    database: Database, *, key: str, product_id: object, product_name: str,
    question: str, store_code: str = STORE_NAVER, option_name: str | None = None,
) -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": store_code,
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": f"ps-{key}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "상품문의",
        "content": question,
        "product_name": product_name,
        "product_id": product_id,
        "option_name": option_name,
        "order_id": None,
        "product_order_id": None,
        "raw_json": {},
    }).inquiry_id


def _origin(
    repository: LearningRepository, *, key: str = "o1", product_id: object = "9645661432",
    product_name: str = SIGNAGE_43, question: str = BRACKET_Q, answer: str = BRACKET_A,
    store_code: str = STORE_NAVER, option_name: str | None = None,
    metadata: dict | None = None,
) -> int:
    """승인된 원본 Learning 과 그 출처 문의."""

    database = repository.database
    inquiry_id = _inquiry(
        database, key=key, product_id=product_id, product_name=product_name,
        question=question, store_code=store_code, option_name=option_name,
    )
    stored = {
        "facts_authority": "APPROVED_REFERENCE",
        "learning_signal_type": "POSITIVE",
        "answer_provenance": "APPROVED_EDITED",
        "human_verified": True,
        **(metadata or {}),
    }
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO learning_examples(
                source_key, inquiry_id, learning_source,
                question_original_masked, question_normalized,
                store_code, inquiry_type, product_name, final_answer,
                style_only, rating, edit_ratio, quality_score, version,
                style_features_json, metadata_json, active, validity_type,
                validity_active, created_at
            ) VALUES (?,?,'APPROVED_EDITED',?,?,?,'PRODUCT_INQUIRY',?,?,
                      0,5,0.0,0.9,1,'{}',?,1,'PERMANENT',1,
                      '2026-10-01T00:00:00.000Z')
            """,
            (
                f"src-{key}", inquiry_id, question, question, store_code,
                product_name, answer, json.dumps(stored, ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid)


def _common(
    repository: LearningRepository, origin_id: int, *, scope: str = "PRODUCT",
    market: str = "NAVER_ONLY", question: str = BRACKET_Q, answer: str = BRACKET_A,
) -> dict:
    return repository.add_common_learning(
        origin_id, question=question, answer=answer, product_scope=scope,
        market_applicability=market, actor="qa-operator",
    )


def _search(
    repository: LearningRepository, *, question: str = BRACKET_Q,
    product_name: str = SIGNAGE_43, product_id: object = "9645661432",
    store_code: str = STORE_NAVER, option_name: str | None = None,
) -> list[dict]:
    # 운영 검색과 같은 모드: 약한 거부는 남겨 GPT ② 가 읽게 한다.
    return SimilarAnswerService(repository).search(
        question, store_code=store_code, product_name=product_name,
        product_id=product_id, option_name=option_name,
        inquiry_type="PRODUCT_INQUIRY", limit=8, hard_conflicts_only=True,
    )


def _row(found: list[dict], created: dict) -> dict | None:
    return next((item for item in found if int(item["id"]) == int(created["id"])), None)


def _payload(
    repository: LearningRepository, *, question: str = BRACKET_Q,
    product_name: str = SIGNAGE_43, product_id: object = "9645661432",
) -> list[dict]:
    context = SimilarAnswerService(repository).context(
        question, store_code=STORE_NAVER, product_name=product_name,
        product_id=product_id, inquiry_type="PRODUCT_INQUIRY",
        hard_conflicts_only=True,
    )
    return list(context.get("similar_approved_answers") or [])


# ===========================================================================
# 저장: 판매 페이지와 마켓을 승인 시점에 고정한다
# ===========================================================================

def test_product_scope_pins_the_origin_listing_and_its_market(repository):
    created = _common(repository, _origin(repository))

    metadata = created["metadata_json"]
    assert metadata["product_scope"] == "PRODUCT"
    assert metadata["common_learning_product_id"] == "9645661432"
    assert metadata["common_learning_product_market"] == "NAVER"
    assert metadata["market_applicability"] == "NAVER_ONLY"
    assert metadata["common_learning"] is True
    assert "cross_model_policy" not in metadata


@pytest.mark.parametrize("market", ["COMMON", "COUPANG_ONLY"])
def test_product_scope_refuses_any_market_but_the_listings_own(repository, market):
    origin_id = _origin(repository)

    with pytest.raises(ValueError, match="마켓"):
        _common(repository, origin_id, market=market)


@pytest.mark.parametrize(
    "product_id", [None, "", "<masked-product-order-id>"],
)
def test_product_scope_needs_a_listing_id_on_the_origin(repository, product_id):
    origin_id = _origin(repository, product_id=product_id)

    with pytest.raises(ValueError, match="판매 페이지"):
        _common(repository, origin_id)
    assert repository.product_scope_target(learning_id=origin_id) is None


def test_editor_preview_shows_what_will_be_pinned(repository):
    origin_id = _origin(repository, product_id="10914781557", product_name=LH50BED)

    target = repository.product_scope_target(learning_id=origin_id)

    assert target["product_id"] == "10914781557"
    assert target["market"] == "NAVER"
    assert target["product_name"] == LH50BED


def test_direct_approval_pins_the_inquirys_listing(repository):
    inquiry_id = _inquiry(
        repository.database, key="d1", product_id="13684622431",
        product_name=LH85_STAND, question=STAND_Q,
    )

    target = repository.product_scope_target(inquiry_id=inquiry_id)
    created = repository.add_common_learning_for_inquiry(
        inquiry_id, question=STAND_Q, answer=STAND_A, product_scope="PRODUCT",
        market_applicability="NAVER_ONLY", actor="qa-operator",
    )

    assert target["product_id"] == "13684622431"
    assert target["model_code"]
    assert created["metadata_json"]["common_learning_product_id"] == "13684622431"


def test_a_coupang_listing_is_pinned_to_coupang(repository):
    origin_id = _origin(
        repository, product_id="12401719630", store_code=STORE_COUPANG,
        metadata={"origin_market": "COUPANG"},
    )

    created = _common(repository, origin_id, market="COUPANG_ONLY")

    assert created["metadata_json"]["common_learning_product_market"] == "COUPANG"


# ===========================================================================
# 적용 범위 변경: 고정된 판매 페이지는 유지만 되고 새로 부여되지 않는다
# ===========================================================================

def test_scope_editor_keeps_a_pinned_listing_on_its_market(repository):
    created = _common(repository, _origin(repository))

    kept = repository.update_application_scope(
        int(created["id"]), product_scope="PRODUCT",
        market_applicability="NAVER_ONLY", actor="qa-operator",
    )
    assert kept["metadata_json"]["common_learning_product_id"] == "9645661432"

    with pytest.raises(ValueError, match="마켓"):
        repository.update_application_scope(
            int(created["id"]), product_scope="PRODUCT",
            market_applicability="COMMON", actor="qa-operator",
        )


def test_widening_a_pinned_row_releases_the_listing(repository):
    created = _common(repository, _origin(repository))

    widened = repository.update_application_scope(
        int(created["id"]), product_scope="MODEL",
        market_applicability="NAVER_ONLY", actor="qa-operator",
    )

    assert widened["metadata_json"]["product_scope"] == "MODEL"
    assert "common_learning_product_id" not in widened["metadata_json"]
    with pytest.raises(ValueError, match="판매 페이지"):
        repository.update_application_scope(
            int(created["id"]), product_scope="PRODUCT",
            market_applicability="NAVER_ONLY", actor="qa-operator",
        )


def test_an_original_row_cannot_be_given_product_scope(repository):
    origin_id = _origin(repository)

    with pytest.raises(ValueError, match="판매 페이지"):
        repository.update_application_scope(
            origin_id, product_scope="PRODUCT",
            market_applicability="NAVER_ONLY", actor="qa-operator",
        )


# ===========================================================================
# 검색: 고정된 판매 페이지에만 적용된다 (요구 1~9)
# ===========================================================================

def test_1_same_listing_applies_and_reaches_gpt2(repository):
    created = _common(repository, _origin(repository))

    row = _row(_search(repository), created)
    payload = [
        item for item in _payload(repository)
        if int(item["learning_example_id"]) == int(created["id"])
    ]

    assert row is not None
    assert row["compatibility"]["eligible"] is True
    assert row["compatibility"]["product_match"] == "EXACT_PRODUCT"
    assert payload and payload[0]["operator_common_policy"] is True
    assert payload[0]["evidence_origin"]["identity"] == "SAME_PRODUCT"


@pytest.mark.parametrize(
    "label, product_name, product_id",
    [
        ("2. 다른 판매 페이지(다른 크기 사이니지)", SIGNAGE_50, "9645702227"),
        ("4. 같은 상품명, 다른 판매 페이지", SIGNAGE_43, "9999999999"),
        ("5. 판매 페이지 ID 없음", SIGNAGE_43, None),
        ("5. 판매 페이지 ID 공백", SIGNAGE_43, "  "),
    ],
)
def test_other_or_missing_listing_is_out_of_scope(
    repository, label, product_name, product_id,
):
    created = _common(repository, _origin(repository))

    found = _search(repository, product_name=product_name, product_id=product_id)

    assert _row(found, created) is None, label
    assert not any(
        int(item["learning_example_id"]) == int(created["id"])
        for item in _payload(repository, product_name=product_name, product_id=product_id)
    ), label


def test_3_same_model_on_another_listing_is_out_of_scope_where_model_is_not(repository):
    product = _common(
        repository,
        _origin(repository, key="p", product_id="10914781557", product_name=LH50BED),
    )
    model = _common(
        repository,
        _origin(repository, key="m", product_id="10914781557", product_name=LH50BED),
        scope="MODEL",
    )

    found = _search(repository, product_name=LH50BED_OTHER, product_id="11363682002")

    assert _row(found, product) is None
    assert _row(found, model) is not None, "MODEL 범위는 같은 모델의 다른 판매 페이지에 그대로 적용"


@pytest.mark.parametrize("product_id", [" 9645661432 ", 9645661432])
def test_6_listing_id_is_compared_after_normalisation(repository, product_id):
    created = _common(repository, _origin(repository))

    assert _row(_search(repository, product_id=product_id), created) is not None


def test_7_the_same_number_in_the_other_market_is_out_of_scope(repository):
    created = _common(repository, _origin(repository))

    found = _search(repository, store_code=STORE_COUPANG)

    assert _row(found, created) is None


@pytest.mark.parametrize(
    "label, option_name",
    [
        ("같은 판매 페이지의 다른 모델 옵션", "26년형_LH50BEH UHDTV_무료배송설치"),
        ("같은 판매 페이지의 다른 크기 옵션", "65인치"),
    ],
)
def test_8_another_option_of_the_same_listing_is_out_of_scope(
    repository, label, option_name,
):
    created = _common(
        repository,
        _origin(
            repository, product_id="10914781557", product_name=LH50BED,
            option_name="24년형_LH50BED UHDTV_무료배송설치",
        ),
    )

    found = _search(
        repository, product_name=LH50BED, product_id="10914781557",
        option_name=option_name,
    )
    same_option = _search(
        repository, product_name=LH50BED, product_id="10914781557",
        option_name="24년형_LH50BED UHDTV_무료배송설치",
    )

    assert _row(found, created) is None, label
    assert _row(same_option, created) is not None


def test_9_an_inactive_product_scoped_learning_is_not_retrieved(repository):
    created = _common(repository, _origin(repository))
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE learning_examples SET active=0 WHERE id=?", (int(created["id"]),)
        )

    assert _row(_search(repository), created) is None


# ===========================================================================
# 기존 범위는 그대로 (요구 10~12)
# ===========================================================================

def test_10_policy_still_reaches_every_listing(repository):
    created = _common(
        repository,
        _origin(repository, question=STAIR_Q, answer=STAIR_A),
        scope="POLICY", market="COMMON", question=STAIR_Q, answer=STAIR_A,
    )

    for product_name, product_id in (
        (SIGNAGE_43, "9645661432"), (LH50BED, "10914781557"), (SIGNAGE_43, None),
    ):
        row = _row(
            _search(repository, question=STAIR_Q, product_name=product_name,
                    product_id=product_id),
            created,
        )
        assert row is not None, product_id


def test_11_model_scope_still_follows_the_model(repository):
    created = _common(
        repository,
        _origin(repository, product_id="13684622431", product_name=LH85_STAND,
                question=STAND_Q, answer=STAND_A),
        scope="MODEL", question=STAND_Q, answer=STAND_A,
    )

    found = _search(repository, question=STAND_Q, product_name=LH85_WALL,
                    product_id="13684631171")

    assert _row(found, created) is not None


def test_12_an_ordinary_learning_is_judged_as_before(repository):
    origin_id = _origin(repository, product_id="13684622431", product_name=LH85_STAND,
                        question=STAND_Q, answer=STAND_A, metadata={"product_scope": "MODEL"})

    found = _search(repository, question=STAND_Q, product_name=LH85_WALL,
                    product_id="13684631171")

    row = next(item for item in found if int(item["id"]) == origin_id)
    assert row["compatibility"]["product_match"] == "EXACT_MODEL"


# ===========================================================================
# 실제 사례: 같은 모델의 다른 판매 페이지로 번지지 않는다
# ===========================================================================

def test_case_c_mobile_stand_stays_off_the_wall_mount_listing(repository):
    """10571: 318221 의 '직접 설치, VESA 400x300' 이 13684631171 로 가지 않는다."""

    created = _common(
        repository,
        _origin(repository, product_id="13684622431", product_name=LH85_STAND,
                question=STAND_Q, answer=STAND_A),
        question=STAND_Q, answer=STAND_A,
    )

    own = _search(repository, question=STAND_Q, product_name=LH85_STAND,
                  product_id="13684622431")
    wall = _search(repository, question=STAND_Q, product_name=LH85_WALL,
                   product_id="13684631171")

    assert _row(own, created) is not None
    assert _row(wall, created) is None


# ===========================================================================
# 점수와 전달 개수는 바뀌지 않는다
# ===========================================================================

def test_product_scope_adds_no_score_over_model_scope(tmp_path):
    """같은 원본·같은 판매 페이지에서 PRODUCT 와 MODEL 은 같은 점수를 받는다."""

    relevance = {}
    for scope in ("PRODUCT", "MODEL"):
        database = Database(tmp_path / f"{scope}.db")
        database.initialize()
        repository = LearningRepository(database)
        created = _common(repository, _origin(repository), scope=scope)
        row = _row(_search(repository), created)
        relevance[scope] = (
            row["relevance"], row["answer_support"],
            row["compatibility"]["score_adjustment"],
        )

    assert relevance["PRODUCT"] == relevance["MODEL"]


def test_delivery_limit_is_unchanged(repository):
    for index in range(8):
        _common(
            repository,
            _origin(repository, key=f"n{index}"),
            question=f"{BRACKET_Q} ({index})",
        )

    found = SimilarAnswerService(repository).search(
        BRACKET_Q, store_code=STORE_NAVER, product_name=SIGNAGE_43,
        product_id="9645661432", inquiry_type="PRODUCT_INQUIRY", limit=5,
        hard_conflicts_only=True,
    )

    assert len(found) <= 5
