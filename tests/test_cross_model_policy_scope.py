"""운영자가 추가한 공통 Learning 이 다른 모델까지 닿는가.

세 가지를 고정한다.

**도달.** "행사카드는 신용 체크카드 동일하게 적용 받을 수 있나요?" 는 제품이
아니라 결제 조건을 묻는다. 운영자가 그 부분을 공통 Learning 으로 추가하면, TV
에서 추가한 것이 모니터 문의에서도 근거가 되어야 한다. 바뀐 런타임 지점은 한
곳뿐이다 -- 카테고리가 다르면 ``CATEGORY_UNCERTAIN`` 으로 내려 GPT ② 프롬프트에
``UNKNOWN_IDENTITY`` 로 투영되던 라벨이, 승인된 행에서만 내려가지 않는다.

**승격 금지.** ``product_scope`` 는 승인이 아니다. 새 Learning 은 모두
``LearningService._build`` 의 어휘 분류기에서 그 값을 받고, 26.10.22 스냅숏의
활성 2,074 행 중 **559 행이 이미 POLICY** 였다 -- 아무도 검토하지 않은 채로.
그래서 공통 승인은 별도 키이며, 원본 행은 그 키를 얻을 수 없다.

**부분 승인.** 원본 한 행은 고객 문의 전체다. "동봉되는 코드 길이가 어떻게
되나요? 화면 터치 되나요?" 처럼 적용 범위가 다른 질문이 섞여 있으면 통째로
공통이 될 수 없고, 운영자가 고른 부분만 별도 행으로 저장된다. 이 구조가 이전의
어휘 기반 차단을 대체한다 -- 그 차단은 운영 스냅숏에서 부수적인 부품 명사로
166 행을 과차단하면서도 주제가 OTHER 뿐인 25 행("다리사이 간격" -> "812.6mm")을
놓쳤고, 낱말 목록을 늘리는 것은 두 오류를 맞바꾸는 일이었다.

실제 신용카드와 체크카드의 혜택이 같은지는 이 파일이 판단하지 않는다. 운영자가
저장한 본문의 조건이 GPT ② 앞까지 그대로 전달되는지만 검사한다.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import (
    APPLICATION_SCOPES,
    LearningRepository,
    is_common_learning,
    is_operator_approved_policy,
)
from services.similar_answer_service import SimilarAnswerService
from tests.test_v4_evidence_generalization import (
    _EvidenceReadingStub,
    _run,
)


STORE_NAVER = "OJE_PLUS"
STORE_COUPANG = "COUPANG_OJE"

POLICY_QUESTION = "행사카드는 신용 체크카드 동일하게 적용 받을 수 있나요?"
# 조건이 본문에 남아 있는 답변. 카드 종류와 제외 대상이 모두 적혀 있다.
POLICY_ANSWER = (
    "행사 카드 할인은 신용카드와 정상 승인된 체크카드 모두 동일하게 적용됩니다. "
    "다만 상품권·기프트카드 결제와 법인카드는 행사 대상에서 제외됩니다."
)

# 적용 범위가 다른 두 질문이 한 행에 있는 원본.
COMPOSITE_QUESTION = "동봉되는 코드 길이가 어떻게 되나요? 화면 터치 되나요?"
COMPOSITE_ANSWER = (
    "동봉되는 전원 케이블은 1.5m이며, 이 비즈니스 TV 라인업은 터치 기능을 "
    "지원하지 않습니다."
)
TOUCH_QUESTION = "화면 터치 되나요?"
TOUCH_ANSWER = "이 비즈니스 TV 라인업은 화면 터치 기능을 지원하지 않습니다."

TV_50 = "삼성 4K UHD 스마트 비즈니스 TV LH50BEHHLGFXKR 125.7cm(50인치), 스탠드"
TV_43 = "삼성 4K UHD 스마트 비즈니스 TV LH43BEDHLGFXKR 108cm(43인치), 스탠드"
MONITOR = "삼성 모니터 LS32DM501EKXKR 80cm(32인치)"

SPEC_QUESTION = "해상도가 어떻게 되나요?"
SPEC_ANSWER = "이 모델의 해상도는 3840x2160입니다."


def _insert(
    database: Database,
    *,
    question: str,
    answer: str,
    product_name: str = TV_50,
    store_code: str = STORE_NAVER,
    scope: str | None = "MODEL",
    key: str = "k1",
) -> int:
    """승인된 원본 Learning 과 그 출처 문의를 임시 DB 에 넣는다."""

    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": store_code,
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": f"pol-{key}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "상품문의",
        "content": question,
        "product_name": product_name,
        "product_id": f"p-{key}",
        "order_id": None,
        "product_order_id": None,
        "raw_json": {},
    }).inquiry_id
    metadata: dict[str, object] = {
        "facts_authority": "APPROVED_REFERENCE",
        "learning_signal_type": "POSITIVE",
        "answer_provenance": "APPROVED_EDITED",
    }
    if scope is not None:
        metadata["product_scope"] = scope
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
                product_name, answer, json.dumps(metadata, ensure_ascii=False),
            ),
        )
        return int(cursor.lastrowid)


@pytest.fixture()
def repository(tmp_path) -> LearningRepository:
    database = Database(tmp_path / "policy_scope.db")
    database.initialize()
    return LearningRepository(database)


def _origin_and_common(
    repository: LearningRepository,
    *,
    question: str = COMPOSITE_QUESTION,
    answer: str = COMPOSITE_ANSWER,
    common_question: str | None = POLICY_QUESTION,
    common_answer: str | None = POLICY_ANSWER,
    product_name: str = TV_50,
    store_code: str = STORE_NAVER,
    scope: str = "POLICY",
    market: str = "COMMON",
    key: str = "k1",
    **validity,
) -> tuple[int, dict]:
    """원본을 넣고, 그 일부를 공통 Learning 으로 추가한다."""

    origin_id = _insert(
        repository.database, question=question, answer=answer,
        product_name=product_name, store_code=store_code, key=key,
    )
    created = repository.add_common_learning(
        origin_id,
        question=common_question or question,
        answer=common_answer or answer,
        product_scope=scope,
        market_applicability=market,
        actor="qa-operator",
        **validity,
    )
    return origin_id, created


def _found(
    repository: LearningRepository,
    *,
    product_name: str,
    store_code: str = STORE_NAVER,
    question: str = POLICY_QUESTION,
) -> list[dict]:
    return SimilarAnswerService(repository).search(
        question,
        store_code=store_code,
        product_name=product_name,
        inquiry_type="PRODUCT_INQUIRY",
        limit=5,
    )


def _identity(item: dict) -> str:
    compatibility = item.get("compatibility")
    compatibility = compatibility if isinstance(compatibility, dict) else {}
    return str(compatibility.get("product_match") or "")


# ===========================================================================
# 1. 기존 Learning 승인 동작 불변 / 원본은 통째로 공통이 될 수 없다
# ===========================================================================

@pytest.mark.parametrize(
    "label, question, answer",
    [
        ("복합 문의", COMPOSITE_QUESTION, COMPOSITE_ANSWER),
        ("순수 사양", SPEC_QUESTION, SPEC_ANSWER),
        ("순수 정책", POLICY_QUESTION, POLICY_ANSWER),
    ],
)
def test_an_original_row_can_never_be_approved_as_common(
    repository, label, question, answer,
):
    """내용이 무엇이든 원본 행 자체는 공통이 되지 않는다.

    낱말을 읽지 않는다는 것이 요점이다. 순수 정책 문의조차 원본 행으로는
    거부되고, 운영자가 고른 부분을 별도 행으로 추가하는 경로만 열려 있다.
    """

    origin_id = _insert(repository.database, question=question, answer=answer)

    with pytest.raises(ValueError) as caught:
        repository.update_application_scope(
            origin_id, product_scope="POLICY",
            market_applicability="COMMON", actor="qa-operator",
        )

    assert "공통 Learning 추가" in str(caught.value), label
    metadata = repository.get(origin_id)["metadata_json"]
    assert metadata["product_scope"] == "MODEL"
    assert not is_operator_approved_policy(metadata)
    assert not is_common_learning(metadata)


@pytest.mark.parametrize("scope", ["MODEL", "PRODUCT_FAMILY"])
def test_an_original_row_keeps_its_model_scoped_editing(repository, scope):
    """기존 적용 범위 편집은 그대로 동작한다."""

    origin_id = _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
    )

    row = repository.update_application_scope(
        origin_id, product_scope=scope,
        market_applicability="NAVER_ONLY", actor="qa-operator",
    )

    assert row["metadata_json"]["product_scope"] == scope
    assert row["metadata_json"]["market_applicability"] == "NAVER_ONLY"


# ===========================================================================
# 2, 3, 4. 과거/신규 승인 Learning 에서 공통 Learning 추가
# ===========================================================================

def test_a_previously_approved_learning_can_contribute_a_common_learning(
    repository,
):
    """과거에 승인된 Learning -- 생성 시각이 오래된 행 -- 에서도 추가된다."""

    origin_id, created = _origin_and_common(repository)

    assert created["id"] != origin_id
    assert is_common_learning(created["metadata_json"])
    assert created["metadata_json"]["common_learning_origin_id"] == origin_id
    assert created["metadata_json"]["common_learning_approved_by"] == "qa-operator"
    assert created["metadata_json"]["common_learning_approved_at"]


def test_only_the_selected_question_of_a_composite_inquiry_is_stored(
    repository,
):
    """복합 문의에서 질문 하나만 골라 저장할 수 있다."""

    origin_id, created = _origin_and_common(
        repository, question=COMPOSITE_QUESTION, answer=COMPOSITE_ANSWER,
        common_question=TOUCH_QUESTION, common_answer=TOUCH_ANSWER,
    )

    assert created["question_original_masked"] == TOUCH_QUESTION
    assert created["final_answer"] == TOUCH_ANSWER
    # 고르지 않은 쪽 -- 케이블 길이 -- 은 공통 행에 들어가지 않는다.
    assert "1.5m" not in created["final_answer"]
    assert "코드 길이" not in created["question_original_masked"]


def test_the_origin_row_is_untouched_by_the_addition(repository):
    origin_id, created = _origin_and_common(
        repository, question=COMPOSITE_QUESTION, answer=COMPOSITE_ANSWER,
        common_question=TOUCH_QUESTION, common_answer=TOUCH_ANSWER,
    )

    origin = repository.get(origin_id)

    assert origin["question_original_masked"] == COMPOSITE_QUESTION
    assert origin["final_answer"] == COMPOSITE_ANSWER
    assert origin["metadata_json"]["product_scope"] == "MODEL"
    assert "common_learning" not in origin["metadata_json"]
    assert "cross_model_policy" not in origin["metadata_json"]


# ===========================================================================
# 5, 6. 운영자가 검증한 공통 사양과 일반 판매 정책 모두 승인된다
# ===========================================================================

@pytest.mark.parametrize(
    "label, question, answer",
    [
        # 터치 미지원처럼 운영자가 확인한 공통 사양.
        ("공통 사양", TOUCH_QUESTION, TOUCH_ANSWER),
        # 카드 혜택처럼 일반 판매 정책.
        ("판매 정책", POLICY_QUESTION, POLICY_ANSWER),
    ],
)
def test_a_verified_common_specification_and_a_sales_policy_both_qualify(
    repository, label, question, answer,
):
    """제품 사양이라는 이유만으로 공통 승인이 막히지 않는다."""

    _, created = _origin_and_common(
        repository, question=COMPOSITE_QUESTION, answer=COMPOSITE_ANSWER,
        common_question=question, common_answer=answer,
    )

    assert is_operator_approved_policy(created["metadata_json"]), label


# ===========================================================================
# 7. 다른 모델에서 검색된다
# ===========================================================================

@pytest.mark.parametrize(
    "label, product_name",
    [
        ("원본과 같은 모델", TV_50),
        ("같은 제품군 다른 모델", TV_43),
        ("다른 제품군", MONITOR),
    ],
)
def test_a_common_learning_is_evidence_for_another_model(
    repository, label, product_name,
):
    _origin_and_common(repository)

    found = _found(repository, product_name=product_name)

    assert found, f"{label}: 공통 Learning 이 검색되지 않았다"
    assert _identity(found[0]) != "CATEGORY_UNCERTAIN", label


def test_the_approval_is_what_crosses_a_product_family_not_the_scope(repository):
    """``product_scope=POLICY`` 만으로는 제품군을 넘지 않는다.

    어휘 분류기가 쓴 POLICY 는 운영 corpus 에 이미 559 행 있다.
    """

    _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
        scope="POLICY",
    )

    found = _found(repository, product_name=MONITOR)

    assert found, "검색 자체는 기존대로 유지된다"
    assert _identity(found[0]) == "CATEGORY_UNCERTAIN", (
        "승인 키가 없는 POLICY 행이 교차 제품군에서 승격됐다"
    )


def test_a_common_learning_can_never_outrank_a_product_fact(repository):
    """공통 승인이 Product Knowledge 충돌 우선권을 만들지 않는다.

    Learning 이 카탈로그 사실을 이길 수 있는 조건은
    ``learning_evidence_policy._precedence`` 의 ``EXACT_MODEL`` 하나이고,
    교차 모델로 넘어간 공통 Learning 은 ``POLICY_COMPATIBLE`` 이다.
    """

    from services.learning_evidence_policy import EXACT_PRODUCT_MATCHES

    _origin_and_common(repository)

    for product_name in (TV_43, MONITOR):
        for item in _found(repository, product_name=product_name):
            assert _identity(item) not in EXACT_PRODUCT_MATCHES, (
                product_name, _identity(item),
            )


# ===========================================================================
# 8. 원본과 공통 Learning 의 적용 범위는 독립이다
# ===========================================================================

def test_the_two_rows_carry_independent_application_ranges(repository):
    origin_id, created = _origin_and_common(
        repository, scope="POLICY", market="COMMON",
    )

    repository.update_application_scope(
        origin_id, product_scope="MODEL",
        market_applicability="NAVER_ONLY", actor="qa-operator",
    )

    origin = repository.get(origin_id)["metadata_json"]
    common = repository.get(int(created["id"]))["metadata_json"]

    assert origin["product_scope"] == "MODEL"
    assert origin["market_applicability"] == "NAVER_ONLY"
    assert common["product_scope"] == "POLICY"
    assert common["market_applicability"] == "COMMON"
    assert is_operator_approved_policy(common)
    assert not is_operator_approved_policy(origin)


def test_a_common_learning_may_be_narrowed_back_to_one_model(repository):
    """공통 행은 범위를 좁힐 수 있고, 좁히면 교차 승인이 회수된다."""

    _, created = _origin_and_common(repository)
    learning_id = int(created["id"])
    assert _identity(_found(repository, product_name=MONITOR)[0]) != (
        "CATEGORY_UNCERTAIN"
    )

    repository.update_application_scope(
        learning_id, product_scope="MODEL",
        market_applicability="COMMON", actor="qa-operator",
    )
    metadata = repository.get(learning_id)["metadata_json"]

    assert "cross_model_policy" not in metadata
    # 공통 Learning 이라는 사실 자체는 남아야 관리 목록에서 사라지지 않는다.
    assert is_common_learning(metadata)
    # "현재 모델" 로 좁힌 공통 행은 다른 모델에 닿지 않는다. 전에는
    # CATEGORY_UNCERTAIN 으로 남아 공통 우선권 표시를 단 채 전달됐다 --
    # tests/test_common_learning_scope_range.py 참조.
    assert not [
        item for item in _found(repository, product_name=MONITOR)
        if int(item["id"]) == learning_id
    ]


# ===========================================================================
# 9. 네이버 / 쿠팡 / 공통
# ===========================================================================

@pytest.mark.parametrize(
    "applicability, naver_expected, coupang_expected",
    [
        ("NAVER_ONLY", True, False),
        ("COUPANG_ONLY", False, True),
        ("COMMON", True, True),
    ],
)
def test_market_scope_is_honoured_for_a_common_learning(
    repository, applicability, naver_expected, coupang_expected,
):
    _origin_and_common(repository, market=applicability)

    naver = _found(repository, product_name=MONITOR, store_code=STORE_NAVER)
    coupang = _found(repository, product_name=MONITOR, store_code=STORE_COUPANG)

    assert bool(naver) is naver_expected, f"{applicability} / 네이버"
    assert bool(coupang) is coupang_expected, f"{applicability} / 쿠팡"


def test_a_market_change_is_reflected_in_retrieval(repository):
    _, created = _origin_and_common(repository, market="COMMON")

    repository.update_application_scope(
        int(created["id"]), product_scope="POLICY",
        market_applicability="NAVER_ONLY", actor="qa-operator",
    )

    assert _found(repository, product_name=MONITOR, store_code=STORE_NAVER)
    assert not _found(
        repository, product_name=MONITOR, store_code=STORE_COUPANG
    )


# ===========================================================================
# 10. 기간성
# ===========================================================================

@pytest.mark.parametrize(
    "valid_from, valid_until, expected",
    [
        ("2026-10-01", "2026-12-31", True),
        ("2026-01-01", "2026-06-30", False),
        ("2026-12-01", "2026-12-31", False),
    ],
)
def test_a_temporary_common_learning_applies_only_inside_its_window(
    repository, valid_from, valid_until, expected,
):
    _origin_and_common(
        repository, validity_type="TEMPORARY", event_name="10월 카드 행사",
        valid_from=valid_from, valid_until=valid_until,
    )

    assert bool(_found(repository, product_name=MONITOR)) is expected


def test_a_period_may_be_changed_after_registration(repository):
    _, created = _origin_and_common(repository)
    learning_id = int(created["id"])
    assert _found(repository, product_name=MONITOR)

    row = repository.get(learning_id)
    repository.update_validity(
        learning_id, validity_type="TEMPORARY", event_name="종료된 행사",
        valid_from="2026-01-01", valid_until="2026-06-30",
        validity_active=True, validity_note="", condition={},
        expected_updated_at=str(row["updated_at"]),
    )

    assert not _found(repository, product_name=MONITOR)


# ===========================================================================
# 11. 중복 등록 방지
# ===========================================================================

def test_the_same_text_and_range_from_one_origin_is_refused(repository):
    origin_id, created = _origin_and_common(repository)

    with pytest.raises(ValueError) as caught:
        repository.add_common_learning(
            origin_id, question=POLICY_QUESTION, answer=POLICY_ANSWER,
            product_scope="POLICY", market_applicability="COMMON",
            actor="qa-operator",
        )

    assert str(created["id"]) in str(caught.value)
    assert len(repository.common_learnings()) == 1


@pytest.mark.parametrize(
    "scope, market",
    [("POLICY", "NAVER_ONLY"), ("PRODUCT_FAMILY", "COMMON")],
)
def test_a_different_range_is_not_a_duplicate(repository, scope, market):
    """같은 문장을 다른 범위로 승인하는 것은 별개의 결정이다."""

    origin_id, _ = _origin_and_common(repository)

    second = repository.add_common_learning(
        origin_id, question=POLICY_QUESTION, answer=POLICY_ANSWER,
        product_scope=scope, market_applicability=market, actor="qa-operator",
    )

    assert second["metadata_json"]["product_scope"] == scope
    assert len(repository.common_learnings()) == 2


@pytest.mark.parametrize(
    "question, answer",
    [("", POLICY_ANSWER), (POLICY_QUESTION, ""), ("   ", "   ")],
)
def test_empty_text_is_refused(repository, question, answer):
    origin_id = _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
    )

    with pytest.raises(ValueError):
        repository.add_common_learning(
            origin_id, question=question, answer=answer,
            product_scope="POLICY", market_applicability="COMMON",
            actor="qa-operator",
        )


@pytest.mark.parametrize("scope", ["GLOBAL", "CATEGORY", "VARIANT", ""])
def test_only_the_three_offered_targets_are_accepted(repository, scope):
    origin_id = _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
    )

    assert scope not in APPLICATION_SCOPES
    with pytest.raises(ValueError):
        repository.add_common_learning(
            origin_id, question=POLICY_QUESTION, answer=POLICY_ANSWER,
            product_scope=scope, market_applicability="COMMON",
            actor="qa-operator",
        )


@pytest.mark.parametrize("market", ["NAVER", "ALL", "", "COUPANG"])
def test_an_unknown_market_is_refused(repository, market):
    origin_id = _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
    )

    with pytest.raises(ValueError):
        repository.add_common_learning(
            origin_id, question=POLICY_QUESTION, answer=POLICY_ANSWER,
            product_scope="POLICY", market_applicability=market,
            actor="qa-operator",
        )


def test_a_missing_origin_is_reported_as_missing(repository):
    with pytest.raises(LookupError):
        repository.add_common_learning(
            999, question=POLICY_QUESTION, answer=POLICY_ANSWER,
            product_scope="POLICY", market_applicability="COMMON",
            actor="qa-operator",
        )


# ===========================================================================
# 관리 목록
# ===========================================================================

def test_the_management_list_holds_only_operator_added_rows(repository):
    """어휘 분류로 POLICY 가 된 행은 목록에 없다."""

    _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
        scope="POLICY", key="inferred",
    )
    _, created = _origin_and_common(repository, key="added")

    listed = repository.common_learnings()

    assert [int(row["id"]) for row in listed] == [int(created["id"])]
    assert all(is_common_learning(_metadata(row)) for row in listed)


def _metadata(row: dict) -> dict:
    value = row.get("metadata_json")
    return value if isinstance(value, dict) else {}


def test_a_disabled_common_learning_stays_in_the_list(repository):
    """사용 중지는 삭제가 아니다 -- 목록에 남아 다시 사용할 수 있어야 한다."""

    _, created = _origin_and_common(repository)
    learning_id = int(created["id"])
    row = repository.get(learning_id)
    repository.update_validity(
        learning_id, validity_type="PERMANENT", validity_active=False,
        condition={}, expected_updated_at=str(row["updated_at"]),
    )

    assert [int(item["id"]) for item in repository.common_learnings()] == [
        learning_id
    ]
    assert not repository.common_learnings(include_inactive=False)


def test_stopping_and_resuming_a_common_learning_moves_retrieval(repository):
    _, created = _origin_and_common(repository)
    learning_id = int(created["id"])
    assert _found(repository, product_name=MONITOR)

    row = repository.get(learning_id)
    repository.update_validity(
        learning_id, validity_type="PERMANENT", validity_active=False,
        condition={}, expected_updated_at=str(row["updated_at"]),
    )
    assert not _found(repository, product_name=MONITOR), "중지 후에도 검색됐다"

    row = repository.get(learning_id)
    repository.update_validity(
        learning_id, validity_type="PERMANENT", validity_active=True,
        condition={}, expected_updated_at=str(row["updated_at"]),
    )
    assert _found(repository, product_name=MONITOR), "다시 사용 후 검색되지 않았다"


def test_resuming_keeps_the_market_scope_it_was_given(repository):
    """다시 사용 시에도 현재 설정된 마켓 조건이 적용된다."""

    _, created = _origin_and_common(repository, market="NAVER_ONLY")
    learning_id = int(created["id"])
    for active in (False, True):
        row = repository.get(learning_id)
        repository.update_validity(
            learning_id, validity_type="PERMANENT", validity_active=active,
            condition={}, expected_updated_at=str(row["updated_at"]),
        )

    assert _found(repository, product_name=MONITOR, store_code=STORE_NAVER)
    assert not _found(
        repository, product_name=MONITOR, store_code=STORE_COUPANG
    )


# ===========================================================================
# 질문·답변 수정
# ===========================================================================

def test_editing_a_common_learning_changes_what_retrieval_returns(repository):
    _, created = _origin_and_common(repository)
    learning_id = int(created["id"])

    repository.update_common_learning_text(
        learning_id,
        question="행사 카드 혜택이 체크카드에도 적용되나요?",
        answer="행사 카드 혜택은 체크카드에도 동일 적용되며 법인카드는 제외됩니다.",
        expected_updated_at=str(created["updated_at"]),
    )

    found = _found(
        repository, product_name=MONITOR,
        question="행사 카드 혜택이 체크카드에도 적용되나요?",
    )
    assert found
    assert "법인카드는 제외" in str(found[0].get("final_answer") or "")


def test_an_original_row_cannot_be_edited_through_the_common_path(repository):
    origin_id = _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
    )

    with pytest.raises(ValueError):
        repository.update_common_learning_text(
            origin_id, question="바꾼 질문", answer="바꾼 답변",
        )

    assert repository.get(origin_id)["final_answer"] == POLICY_ANSWER


def test_a_stale_edit_is_refused(repository):
    _, created = _origin_and_common(repository)

    with pytest.raises(RuntimeError):
        repository.update_common_learning_text(
            int(created["id"]), question="질문", answer="답변",
            expected_updated_at="2020-01-01T00:00:00.000Z",
        )


def test_a_scope_change_from_a_stale_list_is_refused(repository):
    origin_id = _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
    )

    with pytest.raises(RuntimeError):
        repository.update_application_scope(
            origin_id, product_scope="PRODUCT_FAMILY",
            market_applicability="COMMON", actor="qa-operator",
            expected_updated_at="2020-01-01T00:00:00.000Z",
        )


# ===========================================================================
# 조건 보존
# ===========================================================================

def test_the_payment_conditions_survive_into_the_retrieved_evidence(repository):
    """카드 종류와 제외 대상이 근거 본문에 남아야 한다.

    신용카드와 체크카드가 실제로 같은 혜택을 받는지는 이 테스트가 정하지
    않는다. 운영자가 저장한 본문의 조건이 지워지지 않는지만 검사한다.
    """

    _origin_and_common(repository)

    found = _found(repository, product_name=MONITOR)

    assert found
    answer = str(found[0].get("final_answer") or "")
    for condition in ("신용카드", "체크카드", "상품권", "법인카드", "제외"):
        assert condition in answer, condition


# ===========================================================================
# 재수집
# ===========================================================================

def _recapture(repository: LearningRepository, *, key: str, inquiry_id: int):
    """같은 원본 답변을 다시 수집하는 경로. ``source_key`` 가 같다."""

    return repository.upsert({
        "source_key": f"src-{key}",
        "inquiry_id": inquiry_id,
        "learning_source": "APPROVED_EDITED",
        "question_original_masked": POLICY_QUESTION,
        "question_normalized": POLICY_QUESTION,
        "product_name": TV_50,
        "final_answer": POLICY_ANSWER,
        "style_only": False,
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.9,
        "version": 1,
        "style_features_json": {},
        # 재수집은 어휘 분류기가 방금 추론한 scope 를 가져온다.
        "metadata_json": {
            "facts_authority": "APPROVED_REFERENCE",
            "learning_signal_type": "POSITIVE",
            "answer_provenance": "APPROVED_EDITED",
            "product_scope": "MODEL",
        },
        "active": True,
    })


def test_a_recapture_does_not_withdraw_a_common_approval(repository):
    """재수집이 운영자의 공통 승인 범위를 되돌리지 않는다.

    ``metadata_json`` 은 upsert 의 assignment 목록에 있으므로 통째로
    교체된다. 승인 키만 이월된다.
    """

    origin_id, created = _origin_and_common(
        repository, key="recap", market="NAVER_ONLY",
    )
    learning_id = int(created["id"])
    # 공통 행 자신의 source_key 로 다시 수집되는 경우.
    repository.upsert({
        **{
            key: created[key] for key in (
                "source_key", "inquiry_id", "learning_source",
                "question_original_masked", "question_normalized",
                "product_name", "final_answer", "rating", "version",
            )
        },
        "style_only": False,
        "edit_ratio": 0.0,
        "quality_score": 0.9,
        "style_features_json": {},
        "metadata_json": {
            "facts_authority": "APPROVED_REFERENCE",
            "learning_signal_type": "POSITIVE",
            "answer_provenance": "APPROVED_EDITED",
            "product_scope": "MODEL",
        },
        "active": True,
    })

    metadata = repository.get(learning_id)["metadata_json"]
    assert metadata["product_scope"] == "POLICY", "승인한 적용 대상이 사라졌다"
    assert metadata["market_applicability"] == "NAVER_ONLY"
    assert metadata["cross_model_policy"] is True
    assert is_common_learning(metadata)
    assert metadata["common_learning_origin_id"] == origin_id


def test_a_recapture_of_an_unapproved_row_keeps_todays_behaviour(repository):
    """승인이 없으면 이월도 없다 -- 추론 scope 는 그대로 교체된다."""

    origin_id = _insert(
        repository.database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
        scope="POLICY", key="plainrecap",
    )
    row = repository.get(origin_id)
    assert row["metadata_json"]["product_scope"] == "POLICY"

    _recapture(repository, key="plainrecap", inquiry_id=int(row["inquiry_id"]))
    metadata = repository.get(origin_id)["metadata_json"]

    assert metadata["product_scope"] == "MODEL", (
        "승인 없는 행의 재수집 동작이 바뀌었다"
    )
    assert "cross_model_policy" not in metadata
    assert not is_common_learning(metadata)


# ===========================================================================
# 12. GPT ① -> Learning Retrieval -> GPT ② Prompt Context
# ===========================================================================

def _policy_atom():
    return {
        "text": POLICY_QUESTION,
        "action": "OTHER",
        "requested_information": "행사 카드 적용 조건",
        "requested_attribute": "PERMISSION_OR_OPTION",
    }


def _learning_block(prompt) -> list[dict]:
    """GPT ② 가 받는 Learning 근거 목록.

    키 이름은 추측하지 않는다. 프롬프트를 실제로 덤프해 확인한 이름이며,
    아래 ``test_the_prompt_block_is_where_this_contract_lives`` 가 블록이
    비어 있지 않다는 것을 따로 고정한다 -- 비어 있으면 신원 라벨에 대한
    단정이 전부 공허해지기 때문이다.
    """

    payload = (prompt.get("input") or {}) if isinstance(prompt, dict) else {}
    block = payload.get("similar_approved_answers")
    return [item for item in (block or []) if isinstance(item, dict)]


def _item_with(prompt, needle: str) -> dict | None:
    """프롬프트에 실린 근거 중 그 문장을 담은 것.

    블록에는 원본 복합 행도 함께 실릴 수 있다 -- 그것은 정상이며 이 파일이
    다투는 대상이 아니다. 고정하려는 것은 공통 행이 어떤 신원 라벨로
    실리는가이므로, 본문으로 그 한 행을 집어 확인한다.
    """

    for item in _learning_block(prompt):
        if needle in str(item.get("answer") or ""):
            return item
    return None


def _origin_field(item: dict | None, key: str) -> str:
    origin = (item or {}).get("evidence_origin")
    origin = origin if isinstance(origin, dict) else {}
    return str(origin.get(key) or "")


def _seed_then_run(tmp_path, monkeypatch, *, name, approve):
    """``_run`` 이 쓸 임시 DB 를 먼저 만들고 Learning 을 넣은 뒤 생성한다.

    ``_run`` 은 ``tmp_path/{name}.db`` 를 열고 ``initialize()`` 를 호출하는데
    그것은 idempotent 하므로, 같은 경로를 미리 준비해 두면 생성 경로가 그
    Learning 을 보게 된다. 한 테스트에서 답변 생성은 한 번만 일어난다.
    """

    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    repository = LearningRepository(database)
    if approve:
        _origin_and_common(repository, key=name)
    else:
        _insert(
            database, question=POLICY_QUESTION, answer=POLICY_ANSWER,
            scope="POLICY", key=name,
        )
    stub = _EvidenceReadingStub()
    return _run(
        tmp_path, monkeypatch, name=name,
        question=POLICY_QUESTION, product_name=MONITOR,
        atoms=[_policy_atom()], stub=stub,
    )


def test_a_common_learning_reaches_the_gpt2_prompt_for_another_product(
    tmp_path, monkeypatch,
):
    """근거 검색만 확인하고 끝내지 않는다 -- 실제 생성 경로의 프롬프트를 읽는다."""

    result = _seed_then_run(
        tmp_path, monkeypatch, name="common_cross", approve=True,
    )

    assert result.prompt is not None, (
        f"DRAFT 프롬프트가 만들어지지 않았다: {result.error!r}"
    )
    body = result.raw_prompt
    assert "정상 승인된 체크카드" in body, (
        "공통 Learning 이 GPT ② 프롬프트에 전달되지 않았다"
    )
    for condition in ("상품권", "법인카드"):
        assert condition in body, condition
    # 전달만으로는 부족하다 -- UNKNOWN_IDENTITY 면 쓰지 말라는 뜻으로 읽힌다.
    item = _item_with(result.prompt, "정상 승인된 체크카드")
    assert item is not None, _learning_block(result.prompt)
    assert _origin_field(item, "identity") == "POLICY_OR_GENERAL"
    assert _origin_field(item, "reason") == "OPERATOR_APPROVED_CROSS_MODEL_POLICY"


def test_an_unapproved_policy_row_is_not_promoted_in_the_prompt(
    tmp_path, monkeypatch,
):
    result = _seed_then_run(
        tmp_path, monkeypatch, name="common_plain", approve=False,
    )

    assert result.prompt is not None, f"{result.error!r}"
    assert "정상 승인된 체크카드" in result.raw_prompt
    item = _item_with(result.prompt, "정상 승인된 체크카드")
    assert item is not None, _learning_block(result.prompt)
    assert _origin_field(item, "identity") == "UNKNOWN_IDENTITY"
    assert _origin_field(item, "reason") == "POLICY_CATEGORY_DIFFERS_SOFT_PENALTY"


def test_the_prompt_block_is_where_this_contract_lives(tmp_path, monkeypatch):
    """위 두 테스트가 공허해지지 않게 블록 자체를 고정한다.

    ``similar_approved_answers`` 가 비면 신원 라벨 단정이 전부 참이 되어
    버린다. 실제로 그 일이 한 번 있었고 -- 처음 쓴 ``_learning_block`` 은
    존재하지 않는 키를 찾아 늘 빈 목록을 돌려줬다 -- 그래서 이 테스트가 있다.
    """

    result = _seed_then_run(
        tmp_path, monkeypatch, name="common_block", approve=True,
    )

    assert _learning_block(result.prompt), "Learning 근거 블록이 비어 있다"
    item = _item_with(result.prompt, "정상 승인된 체크카드")
    assert item is not None, _learning_block(result.prompt)
    assert isinstance(item.get("evidence_origin"), dict)
    assert _origin_field(item, "source_category") == "TV"
    assert _origin_field(item, "current_category") == "MONITOR"


def test_a_disabled_common_learning_does_not_reach_the_prompt(
    tmp_path, monkeypatch,
):
    """사용 중지한 공통 Learning 은 자동답변 근거로 전달되지 않는다."""

    database = Database(tmp_path / "common_off.db")
    database.initialize()
    repository = LearningRepository(database)
    _, created = _origin_and_common(repository, key="common_off")
    row = repository.get(int(created["id"]))
    repository.update_validity(
        int(created["id"]), validity_type="PERMANENT", validity_active=False,
        condition={}, expected_updated_at=str(row["updated_at"]),
    )

    stub = _EvidenceReadingStub()
    result = _run(
        tmp_path, monkeypatch, name="common_off",
        question=POLICY_QUESTION, product_name=MONITOR,
        atoms=[_policy_atom()], stub=stub,
    )

    assert result.prompt is not None, f"{result.error!r}"
    # 원본 복합 행은 그대로 후보로 남는다. 사라져야 하는 것은 중지한 공통 행뿐이다.
    assert _item_with(result.prompt, "정상 승인된 체크카드") is None, (
        "중지한 공통 Learning 이 프롬프트에 남아 있다"
    )
    assert "정상 승인된 체크카드" not in result.raw_prompt


# ===========================================================================
# 운영 corpus 에 대한 회귀 -- 승인 키가 붙은 행은 아직 없다
# ===========================================================================

OPERATIONAL_DB = pathlib.Path("data/서버pc_data_26.10.22/oje_automation.db")


@pytest.mark.skipif(
    not OPERATIONAL_DB.exists(), reason="운영 DB 복사본이 이 PC 에 없다"
)
def test_no_existing_row_is_already_treated_as_a_common_learning():
    """이번 변경이 기존 corpus 의 검색 결과를 바꾸지 않는다는 증거.

    승인 키를 가진 행이 하나도 없으면, 바뀐 분기는 운영 corpus 의 어느 행에도
    적용되지 않는다. 559 개의 추론 POLICY 행은 그대로
    ``CATEGORY_UNCERTAIN`` 을 유지한다.
    """

    connection = sqlite3.connect(
        f"file:{OPERATIONAL_DB.as_posix()}?mode=ro", uri=True
    )
    try:
        approved = connection.execute(
            """
            SELECT COUNT(*) FROM learning_examples
            WHERE json_extract(metadata_json, '$.cross_model_policy') IS NOT NULL
               OR json_extract(metadata_json, '$.common_learning') IS NOT NULL
            """
        ).fetchone()[0]
        inferred_policy = connection.execute(
            """
            SELECT COUNT(*) FROM learning_examples
            WHERE active=1
              AND json_extract(metadata_json, '$.product_scope')='POLICY'
            """
        ).fetchone()[0]
    finally:
        connection.close()

    assert approved == 0, (
        "운영 corpus 에 이미 승인 키가 있다. 영향 범위를 다시 계산해야 한다."
    )
    assert inferred_policy > 0, "추론 POLICY 행이 있다는 전제가 깨졌다"
