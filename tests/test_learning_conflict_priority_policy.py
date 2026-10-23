"""Learning / Product Knowledge 충돌 우선순위 정책이 코드에 남아 있는지 고정한다.

확정 정책

  Learning ↔ Learning   같은 모델·속성·조건의 실제 충돌은 더 최근에 사람이
                        승인한 쪽이 이긴다. 승인 시각이 같거나 확인할 수 없어
                        승자를 정할 수 없으면 양쪽을 보류한다. 모델·속성·조건이
                        다르면 최신성만으로 한쪽을 지우지 않는다.

  Learning ↔ PK         실제 값이 충돌하면 어느 쪽이 낡았는지 코드가 알 수
                        없으므로 사람이 결정한다 (``fact_conflicts``).
                        서로 다른 속성·측정 조건은 충돌이 아니다.

운영 DB(2026-10-22 복사본) 감사에서 10936·11162 두 건이 PK ↔ Learning
CONFLICT 로 기록돼 자동등록됐다. 그 충돌은 속성이 어긋난 오탐이었고 --
"무게가 얼마나 되나요" 의 ``weight_catalog=28.4kg`` 가 틸트 각도를 말한 답변과,
``speaker_channels=2CH`` 가 베사홀 치수를 말한 답변과 묶였다 -- 현재 코드에서는
재현되지 않는다. 당시 기록된 PK ↔ Learning 충돌 81쌍을 런타임 조립 필드
(``authority``/``compatibility``, ``_qualifying`` 이 요구한다)까지 갖춰 다시
판정하면 49쌍은 더 이상 충돌이 아니고 29쌍은 여전히 충돌이다. 이 파일은 앞의
비재발과, 뒤의 실제 충돌이 계속 사람에게 가는 것을 함께 고정한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services.learning_evidence_policy import (
    APPROVAL_PRECEDENCE_WINDOW,
    LEARNING_WINS,
    PRODUCT_FACT_WINS,
    approval_time,
    fact_conflict_winner,
    fact_conflicts,
    _fields_the_text_is_about,
    _is_about_field,
    _measurements_conflict,
)
from services.hybrid_answer_service import _exact_model_for_listing
from services.similar_answer_service import (
    _effective_time,
    _has_human_conflict_authority,
    _resolve_latest_valid_conflicts,
)

MODEL = "LH50BEHHLGFXKR"
OTHER_MODEL = "LH43BEHHLGFXKR"


def learning(
    learning_id: int,
    answer: str,
    topics: list[str],
    *,
    model: str = MODEL,
    source: str = "APPROVED_EDITED",
    verified_at: str | None = None,
    human_verified: bool | None = None,
):
    # 실제 APPROVED_EDITED 행의 모양. ``classify_provenance`` 는 human_verified
    # 비트와 answer_provenance 를 함께 읽으므로, 둘 없이 source 만 주면
    # UNKNOWN_PROVENANCE 가 되어 건별 승인으로 인정되지 않는다.
    metadata: dict[str, object] = {
        "learning_topics": topics,
        "human_verified": True,
        "answer_provenance": (
            "STAFF_EDITED" if source == "APPROVED_EDITED"
            else "PROGRAM_GENERATED" if source == "APPROVED_UNEDITED"
            else "NAVER_POSTED"
        ),
    }
    if verified_at:
        metadata["verified_at"] = verified_at
    if human_verified is not None:
        metadata["human_verified"] = human_verified
    return {
        "id": learning_id,
        "learning_example_id": learning_id,
        "learning_source": source,
        "model_code": model,
        "store_code": "COUPANG_OJE_NS",
        "question_normalized": "q",
        "matched_subquestion": "q",
        "answer": answer,
        "final_answer": answer,
        "metadata_json": metadata,
        # ``_qualifying`` 이 요구하는 런타임 조립 필드. 저장된 learning_examples
        # 행에는 없고 검색 단계가 붙인다 -- 이것을 빼면 모든 후보가 걸러져
        # fact_conflicts 가 늘 0건을 돌려준다.
        "authority": "APPROVED",
        "compatibility": {"product_match": "EXACT_MODEL"},
    }


def resolve(rows, *, priority=None):
    ranked = [(1.0 - index * 0.01, row) for index, row in enumerate(rows)]
    return _resolve_latest_valid_conflicts(
        ranked, priority_for=priority or (lambda _item: 0))


YES = "벽걸이 설치 가능합니다."
NO = "벽걸이 설치는 불가능합니다."
TOPIC = ["STAND_BRACKET_VESA"]


# ---------------------------------------------------------------------------
# Learning ↔ Learning
# ---------------------------------------------------------------------------

def test_01_the_more_recently_approved_learning_wins():
    """더 최근에 사람이 승인한 Learning 이 이긴다."""

    kept, conflicts = resolve([
        learning(1, YES, TOPIC, verified_at="2026-10-01T00:00:00Z"),
        learning(2, NO, TOPIC, verified_at="2026-08-01T00:00:00Z"),
    ])
    assert conflicts[0]["reason"] == "NEWER_EFFECTIVE_APPROVAL"
    assert conflicts[0]["winner_learning_id"] == 1
    assert [row["id"] for _score, row in kept] == [1]


def test_02_higher_authority_outranks_time():
    """권한이 높은 쪽이 시간보다 먼저 판단된다 (기존 정책)."""

    kept, conflicts = resolve(
        [
            learning(1, YES, TOPIC, verified_at="2026-08-01T00:00:00Z"),
            learning(2, NO, TOPIC, verified_at="2026-10-01T00:00:00Z"),
        ],
        priority=lambda item: 2 if int(item["id"]) == 1 else 1,
    )
    assert conflicts[0]["reason"] == "HIGHER_AUTHORITY"
    assert conflicts[0]["winner_learning_id"] == 1
    assert [row["id"] for _score, row in kept] == [1]


@pytest.mark.parametrize("left_at, right_at", [
    # 승인 시각이 같다
    ("2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"),
    # 한쪽만 있다
    ("2026-10-01T00:00:00Z", None),
    # 둘 다 없다 -- 운영 데이터의 21개 전문 충돌쌍이 이 모양이다
    (None, None),
])
def test_03_no_decidable_time_keeps_the_hold(left_at, right_at):
    """승자를 확정할 수 없으면 양쪽 보류가 유지된다."""

    kept, conflicts = resolve([
        learning(1, YES, TOPIC, verified_at=left_at),
        learning(2, NO, TOPIC, verified_at=right_at),
    ])
    assert conflicts[0]["reason"] == "UNRESOLVED_NO_RELIABLE_TIME"
    assert conflicts[0]["winner_learning_id"] is None
    assert kept == []


@pytest.mark.parametrize("left, right", [
    # 주제가 다르다
    (("STAND_BRACKET_VESA",), ("PORT_CONNECTIVITY",)),
    # 범용 라벨만 같다 -- 주제를 가리키지 않는다
    (("OTHER",), ("OTHER",)),
])
def test_04_a_different_subject_is_never_removed_by_recency(left, right):
    """모델·속성·조건이 다르면 최신성만으로 한쪽을 지우지 않는다."""

    kept, conflicts = resolve([
        learning(1, YES, list(left), verified_at="2026-10-01T00:00:00Z"),
        learning(2, NO, list(right), verified_at="2026-08-01T00:00:00Z"),
    ])
    assert conflicts == []
    assert len(kept) == 2


def test_05_a_different_model_is_not_a_conflict():
    """다른 모델의 상반된 답변은 충돌이 아니다."""

    kept, conflicts = resolve([
        learning(1, YES, TOPIC, verified_at="2026-10-01T00:00:00Z"),
        learning(2, NO, TOPIC, model=OTHER_MODEL,
                 verified_at="2026-08-01T00:00:00Z"),
    ])
    assert conflicts == []
    assert len(kept) == 2


def test_06_recency_is_read_from_the_approval_time_only():
    """최신성은 승인 시각에서만 읽는다 -- 사용으로 바뀌는 값이 아니다.

    ``_effective_time`` 이 보는 키는 effective_at / approved_at / verified_at /
    valid_from 이고, ``updated_at`` 은 들어 있지 않다. 운영 DB 의 활성 Learning
    2,074건 중 승인 시각을 가진 것은 705건이며, 그 705건이 그대로
    ``_effective_time`` 이 값을 돌려주는 집합이다.
    """

    with_time = learning(1, YES, TOPIC, verified_at="2026-10-01T00:00:00Z")
    without = learning(2, NO, TOPIC)
    without["updated_at"] = "2026-10-20T00:00:00Z"
    assert _effective_time(with_time) is not None
    assert _effective_time(without) is None


def test_07_only_a_per_item_approval_carries_conflict_authority():
    """건별 승인만 충돌 판정 권한을 갖는다 -- 일괄 승격은 갖지 않는다.

    운영 DB 활성 2,074건 중 ``human_verified`` 비트는 1,449건에 있는데 건별
    승인은 13건뿐이다. 비트를 읽으면 일괄 verified 624건과 과거 승격 743건이
    사람이 검토한 행을 지울 수 있었다.
    """

    assert _has_human_conflict_authority(
        learning(1, YES, TOPIC, source="APPROVED_EDITED"))
    assert _has_human_conflict_authority(
        learning(2, YES, TOPIC, source="APPROVED_UNEDITED"))
    # 일괄 verified 된 네이버 등록 답변 (SELLER_ANSWER_VERIFIED)
    assert not _has_human_conflict_authority(
        learning(3, YES, TOPIC, human_verified=True, source="SELLER_ANSWER"))
    # 과거 사례 승격
    promoted = learning(4, YES, TOPIC, source="APPROVED_EDITED")
    promoted["metadata_json"]["source_origin"] = "HISTORICAL_PROMOTED"
    assert not _has_human_conflict_authority(promoted)
    # 승인 기록이 아예 없는 수집분
    plain = learning(5, YES, TOPIC, source="SELLER_ANSWER")
    plain["metadata_json"]["human_verified"] = False
    assert not _has_human_conflict_authority(plain)


# ---------------------------------------------------------------------------
# Learning ↔ Product Knowledge
# ---------------------------------------------------------------------------

def fact(field_key, value):
    return SimpleNamespace(field_key=field_key, value=value)


# 운영 DB 에 기록된 실제 쌍. 셋 다 속성이 어긋난 오탐이었다.
FALSE_POSITIVES = [
    ("10936 무게 ↔ 틸트 각도", "weight_catalog", "28.4kg",
     "안녕하세요 고객님 약 10도가량씩 조절이 되는 모델이나 대형인치이다보니 "
     "움직임이 크진않다고 생각해주시면 되겠습니다."),
    ("11162 스피커채널 ↔ 베사홀", "speaker_channels", "2CH",
     "안녕하세요 블루투스 기능 지원되며 스피커 연결도 가능합니다. "
     "베사홀 사이즈는 200x200 mm 입니다."),
    ("11162 스피커유무 ↔ 앱 설치", "speaker_present", "True",
     "안녕하세요 오제앤에스 입니다. 문의 주신 구글 캐스트가 앱을 말씀하시는 "
     "것이라면 해당 제품은 제품 자체에 추가적인 앱 설치가 불가능한 제품으로 "
     "앱의 내장 여부는 확인이 어렵습니다."),
]


@pytest.mark.parametrize("label, field_key, value, answer", FALSE_POSITIVES)
def test_08_a_fact_about_another_attribute_is_not_a_conflict(
    label, field_key, value, answer,
):
    """서로 다른 속성·측정 조건은 충돌로 간주하지 않는다 (10936·11162 비재발)."""

    item = learning(1, answer, ["OTHER"], human_verified=True,
                    source="SELLER_ANSWER")
    assert fact_conflicts([item], [fact(field_key, value)]) == ()


def test_09_a_fact_the_answer_contradicts_is_still_a_conflict():
    """같은 속성에서 값이 실제로 어긋나면 사람이 결정하도록 남긴다."""

    item = learning(
        1, "안녕하세요. 해당 제품은 내장 스피커가 없습니다.", ["AUDIO"],
        human_verified=True, source="SELLER_ANSWER")
    found = fact_conflicts([item], [fact("speaker_present", "True")])
    assert len(found) == 1
    assert found[0]["field_key"] == "speaker_present"
    assert found[0]["kind"] == "PRODUCT_FACT_VS_APPROVED_LEARNING"


def test_10_a_subquestion_scope_narrows_which_facts_apply():
    """복합문의에서 각 답변은 자기 sub-question 이 물은 사실만 상대한다."""

    item = learning(
        1, "안녕하세요. 해당 제품은 내장 스피커가 없습니다.", ["AUDIO"],
        human_verified=True, source="SELLER_ANSWER")
    # 이 sub-question 은 스피커를 묻지 않았다 -- 비교 대상이 아니다.
    assert fact_conflicts(
        [item], [fact("speaker_present", "True")],
        scope_for=lambda _question: {"resolution"},
    ) == ()


# ---------------------------------------------------------------------------
# PK ↔ Learning 3개월 우선 정책
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
WINDOW_DAYS = APPROVAL_PRECEDENCE_WINDOW.days


def candidate(days_ago, *, source="APPROVED_EDITED", model=MODEL,
              answer="내장 스피커가 없습니다.", approved=True):
    row = learning(1, answer, ["AUDIO"], model=model, source=source)
    if approved:
        row["metadata_json"]["verified_at"] = (
            NOW - timedelta(days=days_ago)).isoformat()
    else:
        row["metadata_json"].pop("verified_at", None)
    return row


@pytest.mark.parametrize("days_ago, expected", [
    (0, LEARNING_WINS),
    (WINDOW_DAYS - 1, LEARNING_WINS),
    (WINDOW_DAYS, LEARNING_WINS),          # 경계: 90일째는 이내
    (WINDOW_DAYS + 1, PRODUCT_FACT_WINS),  # 경계: 91일째는 초과
    (365, PRODUCT_FACT_WINS),
])
def test_12_the_approval_window_decides_at_its_boundary(days_ago, expected):
    """승인 후 3개월 이내면 Learning, 초과면 PK 가 이긴다."""

    assert fact_conflict_winner(
        candidate(days_ago), fact_value="True", answered_at=NOW) == expected


def test_13_time_is_measured_against_the_answer_processing_time():
    """비교 기준은 답변 처리 시각이다 -- 문의 작성일이 아니다."""

    row = candidate(200)
    # 답변을 200일 전에 처리했다면 그 승인은 당시 최신이었다.
    assert fact_conflict_winner(
        row, fact_value="True",
        answered_at=NOW - timedelta(days=200)) == LEARNING_WINS
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW) == PRODUCT_FACT_WINS


@pytest.mark.parametrize("source", ["SELLER_ANSWER", "APPROVED_UNEDITED"])
def test_14_a_bulk_promotion_leaves_the_catalogue_in_front(source):
    """건별 승인이 아니면 검증된 PK 가 앞선다 (승인 이력 불명확)."""

    row = candidate(1, source=source)
    if source == "SELLER_ANSWER":
        # 일괄 verified 된 네이버 등록 답변
        row["metadata_json"]["answer_provenance"] = "NAVER_POSTED"
        assert fact_conflict_winner(
            row, fact_value="True", answered_at=NOW) == PRODUCT_FACT_WINS
    else:
        # 건별 승인 경로이므로 창 안에서는 Learning 이 이긴다
        assert fact_conflict_winner(
            row, fact_value="True", answered_at=NOW) == LEARNING_WINS


def test_15_no_approval_time_leaves_the_catalogue_in_front():
    """승인 시각이 확인되지 않으면 PK 가 앞선다."""

    row = candidate(1, approved=False)
    assert approval_time(row) is None
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW) == PRODUCT_FACT_WINS


def test_16_an_unnamed_model_is_never_decided():
    """canonical 모델이 특정되지 않으면 승자를 정하지 않는다.

    운영 DB 에서 현재도 충돌하는 29쌍 전부가 이 경우다 -- ``model_code`` 가
    비어 있어 "동일 모델" 을 확인할 수 없고, 기존 충돌 차단이 유지된다.
    """

    assert fact_conflict_winner(
        candidate(1, model=""), fact_value="True", answered_at=NOW) is None


def test_17_an_unmatched_measurement_condition_is_never_decided():
    """측정 조건이 값에만 박혀 있으면 별개 사실일 수 있어 결정하지 않는다."""

    row = candidate(1, answer="가로 716 세로 517 입니다.")
    assert fact_conflict_winner(
        row, fact_value="스탠드포함 (가로 x 높이 x 깊이) 716.1 x 517.0",
        answered_at=NOW) is None
    # 답변이 같은 조건을 말하면 비교할 수 있다.
    same = candidate(1, answer="스탠드포함 기준 가로 716 세로 517 입니다.")
    assert fact_conflict_winner(
        same, fact_value="스탠드포함 (가로 x 높이 x 깊이) 716.1 x 517.0",
        answered_at=NOW) == LEARNING_WINS


def test_18_an_exact_model_match_is_required():
    """compatibility 가 EXACT_MODEL 이 아니면 결정하지 않는다."""

    row = candidate(1)
    row["compatibility"] = {"product_match": "EXACT_NAME"}
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW) is None


# ---------------------------------------------------------------------------
# 상품 리스팅으로 모델을 복구하는 경로
# ---------------------------------------------------------------------------

ASKED_LISTING = "13684610101"
ORIGIN_LISTING = "13684610101"
OTHER_LISTING = "9775146473"


def listing_candidate(*, origin=ORIGIN_LISTING, model="", days_ago=1,
                      source="APPROVED_EDITED", answer="내장 스피커가 없습니다."):
    """``model_code`` 가 빈 행. 운영 DB 의 29쌍이 모두 이 모양이다."""

    row = learning(1, answer, ["AUDIO"], model=model, source=source)
    row["source_product_id"] = origin
    row["metadata_json"]["verified_at"] = (
        NOW - timedelta(days=days_ago)).isoformat()
    return row


def resolver(mapping):
    """``_integrated_exact_model_for_listing`` 자리에 넣는 더블."""

    return lambda product_id: mapping.get(str(product_id or ""))


def test_19_both_listings_proving_one_model_recovers_it():
    """양쪽 리스팅이 같은 모델로 EXACT 확인되면 모델이 복구된다."""

    row = listing_candidate()
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW) is None
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW,
        asked_product_id=ASKED_LISTING,
        exact_model_for=resolver({ASKED_LISTING: MODEL,
                                  ORIGIN_LISTING: MODEL}),
    ) == LEARNING_WINS


def test_20_a_bulk_verified_row_still_leaves_the_catalogue_in_front():
    """이번 29쌍의 모양: 모델은 복구되지만 일괄 승격이므로 PK 가 이긴다."""

    row = listing_candidate(source="SELLER_ANSWER")
    row["metadata_json"]["answer_provenance"] = "NAVER_POSTED"
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW,
        asked_product_id=ASKED_LISTING,
        exact_model_for=resolver({ASKED_LISTING: MODEL,
                                  ORIGIN_LISTING: MODEL}),
    ) == PRODUCT_FACT_WINS


def test_21_a_shared_product_id_alone_does_not_prove_the_model():
    """상품 ID 가 같다는 사실만으로는 EXACT 로 보지 않는다.

    한 리스팅이 여러 옵션 모델을 팔 수 있다. 리졸버가 그 리스팅에서 모델
    하나를 증명하지 못하면(복합 식별자 등) 승자를 정하지 않는다.
    """

    row = listing_candidate()
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW,
        asked_product_id=ASKED_LISTING,
        exact_model_for=resolver({}),      # 어느 쪽도 EXACT 해결 안 됨
    ) is None


def test_22_two_listings_on_different_models_are_never_merged():
    """다른 모델로 해결되면 동일 모델이 아니므로 결정하지 않는다."""

    row = listing_candidate(origin=OTHER_LISTING)
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW,
        asked_product_id=ASKED_LISTING,
        exact_model_for=resolver({ASKED_LISTING: MODEL,
                                  OTHER_LISTING: OTHER_MODEL}),
    ) is None


def test_23_only_the_asked_side_resolving_is_not_enough():
    """한쪽만 해결되면 비교할 상대가 없다."""

    row = listing_candidate(origin=OTHER_LISTING)
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW,
        asked_product_id=ASKED_LISTING,
        exact_model_for=resolver({ASKED_LISTING: MODEL}),
    ) is None


def test_24_a_row_that_names_its_model_does_not_need_the_listing():
    """``model_code`` 가 있으면 리스팅을 묻지 않는다 -- 기존 동작."""

    row = listing_candidate(model=MODEL, origin="")
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW) == LEARNING_WINS


def test_25_the_injected_resolver_is_the_catalogues_exact_path():
    """주입되는 리졸버는 카탈로그의 EXACT 경로이며 BASE 분해가 아니다."""

    import inspect

    source = inspect.getsource(_exact_model_for_listing)
    assert "_integrated_exact_model_for_listing" in source
    assert "_listing_declared_base_model" not in source
    # 상품 ID 가 없으면 아무것도 조회하지 않는다.
    assert _exact_model_for_listing("") is None
    assert _exact_model_for_listing(None) is None


# ---------------------------------------------------------------------------
# 수치 충돌은 같은 측정값끼리만 (운영 DB 29쌍 전수 오탐)
# ---------------------------------------------------------------------------

def fact_row(answer, *, topics=None):
    return learning(1, answer, topics or ["OTHER"], source="SELLER_ANSWER")


# 운영 DB 에 기록된 실제 쌍. 전부 다른 뜻의 숫자가 비교된 것이었다.
MEASURED_FALSE_POSITIVES = [
    ("11207 USB 포트 수 ↔ 설치 기간", "usb_port_count", "1 x USB-A",
     "안녕하세요 고객님 저희 제품에는 RF단자가 있어 동축케이블(TV선) 연결도 "
     "가능합니다 섬 지역의 경우 설치까지 약 2주 정도 소요됩니다"),
    ("11205 USB 포트 수 ↔ HDMI ARC 안내", "usb_port_count", "1 x USB-A",
     "문의하신 삼성 4K UHD 스마트 사이니지 TV 43인치 모델은 HDMI ARC 단자를 "
     "지원합니다."),
    ("11228 제품 치수 ↔ 다리 간격", "dimensions_product",
     "1110.8 x 643.8 x 76.4 mm",
     "안녕하세요 고객님 50인치 모델 다리사이 간격 747mm 입니다."),
    ("11022 제품 치수 ↔ VESA 규격", "dimensions_product",
     "967.5 x 561.4 x 59.7 mm",
     "VESA 규격 확인: 구매 예정이신 삼성 43인치 비즈니스 TV의 VESA 홀 규격은 "
     "200 x 200 mm 입니다."),
    ("11022 제품 치수 ↔ 설치 기간", "dimensions_product",
     "967.5 x 561.4 x 59.7 mm",
     "안녕하세요 고객님. 43인치 TV의 경우 설치까지 2주 정도 소요되십니다."),
    ("10977 화면 크기 ↔ 다리 간격", "screen_size", "50인치",
     "해당 모델의 스탠드 다리 사이 간격은 74.7cm 입니다."),
]


@pytest.mark.parametrize("label, field_key, value, answer",
                         MEASURED_FALSE_POSITIVES)
def test_26_a_number_about_something_else_is_not_a_conflict(
    label, field_key, value, answer,
):
    """다른 뜻의 숫자는 사실 충돌이 아니다 -- 29쌍 전수 오탐의 비재발."""

    assert _measurements_conflict(answer, value) is False
    assert fact_conflicts([fact_row(answer)], [fact(field_key, value)]) == ()


@pytest.mark.parametrize("value, answer", [
    # 같은 단위, 단일 측정값, 값이 다르다 -- 진짜 충돌
    ("28.4kg", "안녕하세요. 본체 무게는 30kg입니다."),
    ("50인치", "안녕하세요. 해당 제품은 43인치입니다."),
    ("2CH", "안녕하세요. 스피커는 4CH 입니다."),
])
def test_27_a_real_measurement_disagreement_still_conflicts(value, answer):
    """같은 측정값을 다르게 말하면 그대로 충돌이다."""

    assert _measurements_conflict(answer, value) is True


@pytest.mark.parametrize("value, answer", [
    ("28.4kg", "안녕하세요. 본체 무게는 28.4kg입니다."),
    ("50인치", "안녕하세요. 50인치 제품입니다."),
])
def test_28_agreeing_measurements_are_not_a_conflict(value, answer):
    """값이 같으면 충돌이 아니다."""

    assert _measurements_conflict(answer, value) is False


def test_29_a_listed_measurement_is_never_compared():
    """나열된 측정값은 어느 항목과 대응되는지 알 수 없어 비교하지 않는다."""

    listed = "1110.8 x 643.8 x 76.4 mm"
    # 답변이 같은 단위의 다른 숫자를 말해도 비교 대상이 아니다.
    assert _measurements_conflict("깊이는 80mm 입니다.", listed) is False
    # "1 x USB-A" 는 나열이 아니다 -- x 뒤가 숫자가 아니다.
    assert _measurements_conflict("USB 포트는 2개입니다.", "2 x USB-A") is False


def test_30_a_polarity_conflict_is_untouched():
    """극성 충돌은 수치 규칙과 무관하게 그대로 차단된다."""

    answer = "안녕하세요. 해당 제품은 내장 스피커가 없습니다."
    found = fact_conflicts(
        [fact_row(answer, topics=["AUDIO"])],
        [fact("speaker_present", "True")])
    assert len(found) == 1
    assert found[0]["kind"] == "PRODUCT_FACT_VS_APPROVED_LEARNING"


# ---------------------------------------------------------------------------
# 속성 오매칭: family 안에서 형제 속성으로 번지지 않는다
# ---------------------------------------------------------------------------

CROSS_ATTRIBUTE = [
    ("RF 동축케이블 ↔ USB 포트", "usb_port_count",
     "저희 제품에는 RF단자가 있어 동축케이블(TV선) 연결도 가능합니다"),
    ("HDMI ↔ USB 포트", "usb_port_count", "HDMI ARC 단자를 지원합니다."),
    ("LAN ↔ HDMI 유무", "hdmi_present",
     "유선 LAN 포트가 있어 인터넷 연결이 가능합니다."),
    ("LAN 부정 ↔ HDMI 유무", "hdmi_present",
     "유선 LAN 포트는 지원하지 않습니다."),
]


@pytest.mark.parametrize("label, field_key, answer", CROSS_ATTRIBUTE)
def test_31_a_sibling_attribute_is_not_this_field(label, field_key, answer):
    """family fragment 가 형제 필드명의 일부라는 이유로 통과하지 않는다."""

    routed, fragments = _fields_the_text_is_about(answer)
    assert _is_about_field(field_key, routed, fragments) is False


def test_32_a_negative_about_another_attribute_is_not_a_conflict():
    """무관한 속성의 부정문은 극성 충돌이 되지 않는다.

    수치가 전혀 없어 측정값 규칙이 걸러주지 못하는 경로다. LAN 을 말한 답변이
    ``hdmi_present = True`` 와 충돌로 기록되던 것이 이 경우다.
    """

    answer = "유선 LAN 포트는 지원하지 않습니다."
    assert fact_conflicts(
        [fact_row(answer)], [fact("hdmi_present", "True")]) == ()


@pytest.mark.parametrize("field_key, answer", [
    # fragment 와 필드명이 같은 말이면 그대로 도달한다.
    ("speaker", "내장 스피커가 없습니다."),
    ("hdmi", "HDMI ARC 단자를 지원합니다."),
    # 토픽 표가 직접 라우팅하는 경우도 그대로다.
    ("speaker_present", "내장 스피커가 없습니다."),
    ("usb_port_count", "USB 포트는 2개입니다."),
])
def test_33_the_field_itself_is_still_reached(field_key, answer):
    """이 속성을 실제로 말한 답변은 계속 비교 대상이다."""

    routed, fragments = _fields_the_text_is_about(answer)
    assert _is_about_field(field_key, routed, fragments) is True


# ---------------------------------------------------------------------------
# 단위 컬럼이 있으면 진짜 수치 충돌을 놓치지 않는다
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, unit, answer, expected", [
    # PK 가 "1 x USB-A" 처럼 단위를 적지 않아도 unit 컬럼이 개수를 말해준다.
    ("1 x USB-A", "개", "USB 포트는 2개입니다.", True),
    ("1 x USB-A", "개", "USB-A 포트 1개 제공됩니다.", False),
    ("28.4", "kg", "무게는 30kg입니다.", True),
    ("28.4", "kg", "무게는 28.4kg입니다.", False),
    ("3", "개", "HDMI 단자는 2개입니다.", True),
    # 나열된 치수는 unit 이 있어도 비교하지 않는다.
    ("1110.8 x 643.8 x 76.4 mm", "mm", "다리 사이 간격 747mm 입니다.", False),
])
def test_34_the_unit_column_makes_a_bare_value_comparable(
    value, unit, answer, expected,
):
    """fact 는 단위를 자기 컬럼에 담는다 -- 값만 읽으면 충돌을 놓친다."""

    assert _measurements_conflict(answer, value, unit=unit) is expected
