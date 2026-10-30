"""공통 Learning 은 운영자가 고른 범위 안에서만 공통이다.

26.10.30 서버 사본에서 확인한 결함. 공통 Learning 의 우선권 세 가지 --
``operator_common_policy`` 플래그, 그 플래그를 읽는 GPT ② 지시("카탈로그보다
앞선다"), ``fact_conflict_winner`` 의 LEARNING_WINS -- 가 모두
``is_common_learning`` 하나만 읽었다. "현재 모델" 로 저장한 행도 사양이 아닌
질문에서는 같은 카테고리의 다른 모델에 POLICY_COMPATIBLE 로, 다른 카테고리에
CATEGORY_UNCERTAIN 으로 전달되며 그 플래그를 달고 있었다. 운영 중인 공통
Learning 4 건은 모두 "전 모델 공통 정책" 이라 실제로 드러나지는 않았다.

고정하는 것:

* "현재 모델" / "동일 제품군" 공통 Learning 은 같은 상품·같은 모델코드·같은
  상품명에서만 남고, 그 밖에서는 검색에서 제거된다. 제품군은 양쪽에 기록된
  경우에만 인정된다 -- 저장된 행 중 제품군을 가진 행은 없다.
* "전 모델 공통 정책" 은 그대로다.
* 공통이 아닌 일반 Learning 의 적용 범위 처리는 바뀌지 않는다.
* 11411 / 11412 에서 갈린 것은 GPT ② 의 판단이고, 코드가 붙이는 라벨은 두
  실행에서 같다.
"""
from __future__ import annotations

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)
from services.similar_answer_service import SimilarAnswerService, _evidence_origin
from tests.test_cross_model_policy_scope import (
    MONITOR,
    STORE_NAVER,
    TV_43,
    TV_50,
    _insert,
)


STAIR_QUESTION = "계단으로 올라오셔야 하는데 배송비가 붙나요?"
STAIR_ANSWER = (
    "계단 배송에 따른 추가 비용은 현장 상황에 따라 달라질 수 있습니다. "
    "정확한 부과 여부와 금액은 배송기사님과 협의해 주세요."
)
# 같은 모델(LH50BEH)의 다른 판매 페이지.
TV_50_OTHER_LISTING = "삼성 125.7cm(50인치) 비즈니스TV LH50BEHHLGFXKR 스탠드형 기사설치"


@pytest.fixture()
def repository(tmp_path) -> LearningRepository:
    database = Database(tmp_path / "common_scope.db")
    database.initialize()
    return LearningRepository(database)


def _common(repository: LearningRepository, *, scope: str, key: str = "k1") -> dict:
    """TV_50 판매 페이지(p-k1)에서 승인한 원본으로 공통 Learning 을 만든다."""

    origin_id = _insert(
        repository.database, question=STAIR_QUESTION, answer=STAIR_ANSWER,
        product_name=TV_50, key=key,
    )
    return repository.add_common_learning(
        origin_id, question=STAIR_QUESTION, answer=STAIR_ANSWER,
        product_scope=scope, market_applicability="COMMON", actor="qa-operator",
    )


def _search(
    repository: LearningRepository, *, product_name: str, product_id: str | None = None,
) -> list[dict]:
    # 운영 검색과 같은 모드: 약한 거부는 남겨 GPT ② 가 읽게 한다.
    return SimilarAnswerService(repository).search(
        STAIR_QUESTION, store_code=STORE_NAVER, product_name=product_name,
        product_id=product_id, inquiry_type="PRODUCT_INQUIRY", limit=8,
        hard_conflicts_only=True,
    )


def _common_rows(found: list[dict], created: dict) -> list[dict]:
    return [item for item in found if int(item["id"]) == int(created["id"])]


def _approved_payload(
    repository: LearningRepository, *, product_name: str, product_id: str | None = None,
) -> list[dict]:
    context = SimilarAnswerService(repository).context(
        STAIR_QUESTION, store_code=STORE_NAVER, product_name=product_name,
        product_id=product_id, inquiry_type="PRODUCT_INQUIRY",
        hard_conflicts_only=True,
    )
    return list(context.get("similar_approved_answers") or [])


# ===========================================================================
# 현재 모델 / 동일 제품군: 범위 밖에서는 제거된다
# ===========================================================================

@pytest.mark.parametrize("scope", ["MODEL", "PRODUCT_FAMILY"])
@pytest.mark.parametrize(
    "label, product_name",
    [("같은 카테고리의 다른 모델", TV_43), ("다른 카테고리", MONITOR)],
)
def test_a_model_scoped_common_learning_does_not_reach_another_model(
    repository, scope, label, product_name,
):
    created = _common(repository, scope=scope)

    found = _search(repository, product_name=product_name)

    assert not _common_rows(found, created), label
    payload = _approved_payload(repository, product_name=product_name)
    assert not any(item.get("operator_common_policy") for item in payload), label


@pytest.mark.parametrize("scope", ["MODEL", "PRODUCT_FAMILY"])
@pytest.mark.parametrize(
    "label, product_name, product_id",
    [
        ("같은 판매 페이지", TV_50, "p-k1"),
        ("같은 모델의 다른 판매 페이지", TV_50_OTHER_LISTING, "p-other"),
    ],
)
def test_a_model_scoped_common_learning_stays_on_its_own_model(
    repository, scope, label, product_name, product_id,
):
    created = _common(repository, scope=scope)

    found = _common_rows(
        _search(repository, product_name=product_name, product_id=product_id),
        created,
    )

    assert found, label
    assert found[0]["compatibility"]["eligible"] is True, label
    payload = [
        item for item in _approved_payload(
            repository, product_name=product_name, product_id=product_id,
        )
        if item.get("learning_example_id") == int(created["id"])
    ]
    assert payload and payload[0]["operator_common_policy"] is True, label


def test_a_product_family_counts_only_when_both_sides_record_it():
    """제품군은 추론하지 않는다. 양쪽에 같은 값이 기록된 경우에만 범위 안이다."""

    service = LearningCompatibilityService()
    metadata = {
        "common_learning": True, "product_scope": "PRODUCT_FAMILY",
        "product_family": "BUSINESS_TV",
    }
    candidate = extract_product_identity(product_name=TV_50, metadata=metadata)

    def decide(current_metadata: dict) -> object:
        return service.evaluate(
            current_question=STAIR_QUESTION,
            current_product=extract_product_identity(
                product_name=TV_43, metadata=current_metadata,
            ),
            candidate_question=STAIR_QUESTION, candidate_answer=STAIR_ANSWER,
            candidate_product=candidate, candidate_metadata=metadata,
            authority="APPROVED",
        )

    recorded = decide({"product_family": "BUSINESS_TV"})
    unknown = decide({})

    assert recorded.eligible is True
    assert recorded.hard_reject is False
    assert unknown.eligible is False
    assert unknown.hard_reject is True
    assert unknown.reject_reason == "COMMON_LEARNING_OUT_OF_SCOPE"


# ===========================================================================
# 전 모델 공통 정책 / 일반 Learning: 바뀌지 않는다
# ===========================================================================

@pytest.mark.parametrize(
    "label, product_name", [("다른 TV 모델", TV_43), ("모니터", MONITOR)],
)
def test_a_policy_common_learning_still_reaches_every_model(
    repository, label, product_name,
):
    created = _common(repository, scope="POLICY")

    found = _common_rows(_search(repository, product_name=product_name), created)

    assert found, label
    assert found[0]["compatibility"]["product_match"] == "POLICY_COMPATIBLE", label
    payload = [
        item for item in _approved_payload(repository, product_name=product_name)
        if item.get("learning_example_id") == int(created["id"])
    ]
    assert payload and payload[0]["operator_common_policy"] is True, label
    assert payload[0]["evidence_origin"]["identity"] == "POLICY_OR_GENERAL", label


@pytest.mark.parametrize("scope", ["MODEL", "PRODUCT_FAMILY"])
def test_an_ordinary_model_scoped_learning_is_unchanged(repository, scope):
    """운영자 공통 승인이 없는 행은 기존 규칙 그대로 다른 모델에도 전달된다."""

    origin_id = _insert(
        repository.database, question=STAIR_QUESTION, answer=STAIR_ANSWER,
        product_name=TV_50, scope=scope,
    )

    found = [
        item for item in _search(repository, product_name=TV_43)
        if int(item["id"]) == origin_id
    ]

    assert found
    assert found[0]["compatibility"]["hard_reject"] is False
    assert found[0]["compatibility"]["reject_reason"] != "COMMON_LEARNING_OUT_OF_SCOPE"


# ===========================================================================
# 11411 / 11412: 코드가 붙이는 라벨은 두 실행에서 같다
# ===========================================================================

# 26.10.30 서버 사본의 Learning 18572 와 두 문의의 상품. 두 실행에서 GPT ① 이
# 하위 질문을 조금 다르게 적었고, 그 두 문장을 그대로 쓴다.
L18572_PRODUCT = "삼성 삼탠바이미 32인치(80cm) M5 스마트 모니터 IPTV+2in1 이동식 거치대"
L18572_QUESTION = (
    "상품 문의\n다세대 주택인데 엘리베이터를 사용하면 사용료가 5만원 내야해서 "
    "3층인데 계단으로 배달 가능한가요?"
)
L18572_ANSWER = (
    "고객님, 다세대 주택의 엘리베이터 사용료 관련 안내 감사합니다. 보통 계단 "
    "배송이 가능하나, 계단 배송 시 추가비용이나 상황에 따라 차이가 있을 수 "
    "있으니 배송 시 함께 안내되는 안내문이나 배송기사님과의 협의를 권장드립니다."
)
ASKED_PRODUCT = "삼성 4K UHD 스마트 비즈니스 TV LH50BEHHLGFXKR 1등급 125.7cm(50인치), 스탠드"
SUBQUESTION_11411 = "2층 주택으로 계단 운반이 필요한 경우 배송비가 추가되는지 궁금합니다."
SUBQUESTION_11412 = "2층 주택으로 계단 배송 시 추가 배송비가 부과되는지 문의합니다."


def _label_for(subquestion: str, metadata: dict) -> tuple[str, str, str]:
    decision = LearningCompatibilityService().evaluate(
        current_question=subquestion,
        current_product=extract_product_identity(
            product_id="13684610101", product_name=ASKED_PRODUCT,
        ),
        candidate_question=L18572_QUESTION, candidate_answer=L18572_ANSWER,
        candidate_product=extract_product_identity(
            product_id="9775146473", product_name=L18572_PRODUCT, metadata=metadata,
        ),
        candidate_metadata=metadata, authority="APPROVED",
    )
    return (
        decision.product_match,
        decision.product_match_reason,
        _evidence_origin(decision)["identity"],
    )


def test_the_two_runs_received_the_same_label_for_learning_18572():
    metadata = {
        "human_verified": True, "learning_signal_type": "POSITIVE",
        "facts_authority": "HUMAN_VERIFIED_NAVER_POSTED",
    }

    first = _label_for(SUBQUESTION_11411, metadata)
    second = _label_for(SUBQUESTION_11412, metadata)

    assert first == second == (
        "CATEGORY_UNCERTAIN", "POLICY_CATEGORY_DIFFERS_SOFT_PENALTY",
        "UNKNOWN_IDENTITY",
    )


def test_the_operator_common_learning_319370_reaches_other_categories_as_policy(
    repository,
):
    """운영자가 18572 의 내용을 "전 모델 공통 정책" 으로 분리한 형태(319370).

    같은 질문이 다른 카테고리 상품에서 들어오면 UNKNOWN_IDENTITY 가 아니라
    POLICY_OR_GENERAL 로, 공통 우선권 표시와 함께 전달된다.
    """

    created = _common(repository, scope="POLICY")

    payload = [
        item for item in _approved_payload(repository, product_name=MONITOR)
        if item.get("learning_example_id") == int(created["id"])
    ]

    assert payload
    assert payload[0]["evidence_origin"]["identity"] == "POLICY_OR_GENERAL"
    assert (
        payload[0]["evidence_origin"]["reason"]
        == "OPERATOR_APPROVED_CROSS_MODEL_POLICY"
    )
    assert payload[0]["operator_common_policy"] is True


# ===========================================================================
# 판매 페이지·상품명이 같아도 모델이 다르면 범위 밖이다
# ===========================================================================

# 한 판매 페이지에 여러 모델이 옵션으로 걸린 상품명. 모델코드는 상품명이 아니라
# 고객이 고른 옵션에서 나온다.
MULTI_MODEL_NAME = "삼성 비즈니스TV 스탠드형 43/50인치"


def _scope_decision(current, candidate, *, scope: str = "MODEL", common: bool = True):
    metadata: dict[str, object] = {
        "human_verified": True, "learning_signal_type": "POSITIVE",
        "facts_authority": "APPROVED_REFERENCE", "product_scope": scope,
    }
    if common:
        metadata["common_learning"] = True
    return LearningCompatibilityService().evaluate(
        current_question=STAIR_QUESTION, current_product=current,
        candidate_question=STAIR_QUESTION, candidate_answer=STAIR_ANSWER,
        candidate_product=candidate, candidate_metadata=metadata,
        authority="APPROVED",
    )


def _out_of_scope(decision) -> bool:
    return (
        decision.hard_reject is True
        and decision.reject_reason == "COMMON_LEARNING_OUT_OF_SCOPE"
    )


@pytest.mark.parametrize(
    "label, current_listing",
    [("같은 판매 페이지", "p-1"), ("다른 판매 페이지, 같은 상품명", "p-2")],
)
def test_a_different_option_model_is_out_of_scope(label, current_listing):
    """26.10.30 커밋 전 확인에서 찾은 반례 두 가지. 둘 다 범위 안으로 잘못 판정됐다."""

    candidate = extract_product_identity(
        product_id="p-1", product_name=MULTI_MODEL_NAME, option="LH50BEHHLGFXKR",
    )
    current = extract_product_identity(
        product_id=current_listing, product_name=MULTI_MODEL_NAME,
        option="LH43BEHHLGFXKR",
    )
    assert (current.model_code, candidate.model_code) == ("43BEH", "50BEH")

    assert _out_of_scope(_scope_decision(current, candidate)), label


def test_the_same_listing_with_a_different_model_name_is_out_of_scope(repository):
    """검색 경로에서: 원본과 같은 판매 페이지 ID 로 다른 모델을 묻는다."""

    created = _common(repository, scope="MODEL")

    assert not _common_rows(
        _search(repository, product_name=TV_43, product_id="p-k1"), created,
    )
    payload = _approved_payload(repository, product_name=TV_43, product_id="p-k1")
    assert not any(item.get("operator_common_policy") for item in payload)


@pytest.mark.parametrize(
    "label, current_kwargs",
    [
        ("같은 모델, 같은 옵션", {"option": "LH50BEHHLGFXKR"}),
        ("같은 모델의 짧은 표기", {"option": "LH50BEH"}),
        ("같은 모델의 소문자 표기", {"model_code": "lh50beh"}),
        ("상품명에 적힌 같은 모델", {"product_name": TV_50}),
    ],
)
def test_the_same_canonical_model_stays_in_scope(label, current_kwargs):
    candidate = extract_product_identity(
        product_id="p-1", product_name=MULTI_MODEL_NAME, option="LH50BEHHLGFXKR",
    )
    kwargs = {"product_id": "p-2", "product_name": MULTI_MODEL_NAME, **current_kwargs}
    current = extract_product_identity(**kwargs)
    assert current.model_code == candidate.model_code == "50BEH", label

    decision = _scope_decision(current, candidate)

    assert decision.eligible is True, label
    assert decision.hard_reject is False, label


@pytest.mark.parametrize(
    "label, current_kwargs, candidate_kwargs, in_scope",
    [
        # 모델코드가 없는 쪽이 있으면 판매 페이지·상품명이 기존대로 범위를 정한다.
        ("양쪽 모두 모델 없음, 같은 판매 페이지",
         {"product_id": "p-1", "product_name": "삼성 비즈니스TV 스탠드형"},
         {"product_id": "p-1", "product_name": "삼성 비즈니스TV 스탠드형"}, True),
        ("문의 쪽만 모델 없음, 같은 판매 페이지",
         {"product_id": "p-1", "product_name": MULTI_MODEL_NAME},
         {"product_id": "p-1", "product_name": MULTI_MODEL_NAME,
          "option": "LH50BEHHLGFXKR"}, True),
        ("Learning 쪽만 모델 없음, 같은 상품명",
         {"product_id": "p-2", "product_name": MULTI_MODEL_NAME,
          "option": "LH43BEHHLGFXKR"},
         {"product_id": "p-1", "product_name": MULTI_MODEL_NAME}, True),
        ("양쪽 모두 모델 없음, 다른 판매 페이지·다른 상품명",
         {"product_id": "p-2", "product_name": "삼성 모니터 스탠드형"},
         {"product_id": "p-1", "product_name": "삼성 비즈니스TV 스탠드형"}, False),
        # 모델코드가 없어도 양쪽에 적힌 크기가 다르면 다른 모델이다.
        ("모델 없음, 같은 판매 페이지, 다른 크기 옵션",
         {"product_id": "p-1", "product_name": "삼성 비즈니스TV 스탠드형",
          "option": "43인치"},
         {"product_id": "p-1", "product_name": "삼성 비즈니스TV 스탠드형",
          "option": "50인치"}, False),
    ],
)
def test_a_missing_model_code_falls_back_to_listing_and_name(
    label, current_kwargs, candidate_kwargs, in_scope,
):
    decision = _scope_decision(
        extract_product_identity(**current_kwargs),
        extract_product_identity(**candidate_kwargs),
    )

    assert _out_of_scope(decision) is (not in_scope), label


def test_a_recorded_family_still_covers_a_different_model():
    """동일 제품군은 모델이 달라도 양쪽에 같은 제품군이 기록돼 있으면 범위 안이다."""

    family = {"product_family": "BUSINESS_TV"}
    candidate = extract_product_identity(
        product_id="p-1", product_name=MULTI_MODEL_NAME, option="LH50BEHHLGFXKR",
        metadata=family,
    )
    current = extract_product_identity(
        product_id="p-1", product_name=MULTI_MODEL_NAME, option="LH43BEHHLGFXKR",
        metadata=family,
    )

    assert not _out_of_scope(
        _scope_decision(current, candidate, scope="PRODUCT_FAMILY")
    )
    assert _out_of_scope(_scope_decision(current, candidate, scope="MODEL"))


def test_an_ordinary_learning_with_a_different_option_model_is_unchanged():
    candidate = extract_product_identity(
        product_id="p-1", product_name=MULTI_MODEL_NAME, option="LH50BEHHLGFXKR",
    )
    current = extract_product_identity(
        product_id="p-1", product_name=MULTI_MODEL_NAME, option="LH43BEHHLGFXKR",
    )

    decision = _scope_decision(current, candidate, common=False)

    assert decision.reject_reason != "COMMON_LEARNING_OUT_OF_SCOPE"


# ===========================================================================
# PK 우선권: 범위 안의 공통 Learning 만 카탈로그를 앞선다
# ===========================================================================

def _winner_for(row: dict) -> str | None:
    from datetime import datetime

    from services.learning_evidence_policy import fact_conflict_winner

    return fact_conflict_winner(
        row, fact_value="추가 비용 없음",
        answered_at=datetime.fromisoformat("2026-10-20T00:00:00+00:00"),
        asked_product_id=None, exact_model_for=lambda *_a, **_k: None,
    )


def test_an_in_scope_common_learning_keeps_its_precedence_over_the_catalogue(
    repository,
):
    from services.learning_evidence_policy import LEARNING_WINS

    created = _common(repository, scope="MODEL")

    payload = [
        item for item in _approved_payload(
            repository, product_name=TV_50, product_id="p-k1",
        )
        if item.get("learning_example_id") == int(created["id"])
    ]

    assert payload and payload[0]["operator_common_policy"] is True
    assert _winner_for(payload[0]) == LEARNING_WINS


@pytest.mark.parametrize(
    "label, product_name, product_id",
    [
        ("같은 판매 페이지의 다른 모델", TV_43, "p-k1"),
        ("다른 판매 페이지의 다른 모델", TV_43, None),
    ],
)
def test_another_models_common_learning_never_outranks_the_catalogue(
    repository, label, product_name, product_id,
):
    from services.learning_evidence_policy import LEARNING_WINS

    _common(repository, scope="MODEL")

    payload = _approved_payload(
        repository, product_name=product_name, product_id=product_id,
    )

    assert not any(item.get("operator_common_policy") for item in payload), label
    assert all(_winner_for(item) != LEARNING_WINS for item in payload), label
