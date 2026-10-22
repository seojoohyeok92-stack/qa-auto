"""10월 21~22일 미답변 감사에서 재현된 두 결함.

  공통 제목   쿠팡은 모든 상품문의의 제목을 "Coupang 상품문의" 로 보낸다. 그
              문자열에는 고객이 말한 것이 하나도 없는데 generic title 로 인식되지
              않아 본문 앞에 남았고, GPT ① 이 atomic_questions 를 돌려주지 않은
              문의에서는 fallback splitter 가 그것을 독립된 하위 질문으로 만들어
              Learning 을 "Coupang 상품문의" 로 검색했다 (문의 11229, 11234).

  범용 주제   ``classify_topics`` 는 주제를 하나도 알아보지 못하면 ["OTHER"] 를
              저장한다. 파괴적 충돌 제거는 "동일한 명시적 주제 하나" 를 요구하면서
              {"OTHER"} == {"OTHER"} 를 그 조건으로 통과시켰고, 같은 모델 scope 의
              미분류 답변들이 서로 전부 "같은 주제" 가 됐다. 문의 11196 에서 26쌍이
              그렇게 묶였다 -- "PC모니터로 이용은 가능하시나 원할하진않습니다" 와
              "네이버 로그인이 필요합니다" 가 한 쌍이다. 두 문장은 서로 다른 질문의
              답이고, 극성 휴리스틱은 같은 질문의 답이라는 전제로만 쓸 수 있다.
"""
from __future__ import annotations

import pytest

from answer.source_adapter import (
    _combined_inquiry_text,
    _is_generic_inquiry_title,
)
from answer.text_utils import split_subquestions
from services.learning_compatibility_service import GENERIC_TOPICS
from services.similar_answer_service import _resolve_latest_valid_conflicts

# 운영 DB 에 기록된 실제 제목·본문 (data/서버pc_data_26.10.22, 읽기 전용 확인)
COUPANG_TITLE = "Coupang 상품문의"
INQUIRY_11229 = "기스사진여기요"
INQUIRY_11234 = (
    "전원을 키면 리모코볼륨을 올리거나 아님다른체널 돌리거나 등등하면 "
    "전원이꺼져서 사용할수없습니다 리모콘 조작하는 체널만 누르면 전원이 "
    "꺼지고 볼륨올려도 전원이 꺼집니다"
)


# ---------------------------------------------------------------------------
# 1. 공통 제목
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    COUPANG_TITLE,
    "COUPANG 상품문의",
    "coupang상품문의",
    "쿠팡 상품문의",
    "Coupang 상품 문의",
    "네이버 상품문의",
    # 접두어가 없던 기존 형태. 계속 generic 이어야 한다.
    "상품문의",
    "상품 문의",
    "제품문의",
    "배송문의",
    "설치 관련 문의",
    "일반 상품 질문",
])
def test_01_marketplace_prefixed_common_titles_are_generic(title):
    """마켓 이름이 붙은 공통 제목도 고객이 말한 것을 담고 있지 않다."""

    assert _is_generic_inquiry_title(title) is True


@pytest.mark.parametrize("title", [
    "쿠팡 50인치 설치문의",
    "Coupang 벽걸이 설치 가능 문의",
    "배송날짜 지정 문의",
    "DP케이블 문의",
    "상품문의입니다만 색상이",
    "쿠팡",
    "문의",
])
def test_02_titles_that_say_something_are_kept(title):
    """조건·모델·부품·장소를 담은 제목은 문의의 일부로 남는다."""

    assert _is_generic_inquiry_title(title) is False


@pytest.mark.parametrize("content", [INQUIRY_11229, INQUIRY_11234])
def test_03_common_title_never_becomes_a_subquestion(content):
    """11229 / 11234 재현: 공통 제목이 하위 질문으로 생성되지 않는다."""

    combined = _combined_inquiry_text(COUPANG_TITLE, content)
    assert not combined.startswith(COUPANG_TITLE)
    parts = [str(item) for item in split_subquestions(combined)]
    assert COUPANG_TITLE not in parts
    assert len(parts) == 1


def test_04_naver_title_deduplication_is_preserved():
    """네이버의 '제목이 본문 첫 줄' 중복 제거 동작은 그대로다."""

    body = "스피커 내장인가요?\n설치비는 얼마인가요?"
    assert _combined_inquiry_text("스피커 내장인가요?", body) == body
    # 본문이 담고 있지 않은 제목은 계속 앞에 붙는다.
    combined = _combined_inquiry_text("무타공 설치 문의", body)
    assert combined.startswith("무타공 설치 문의")


# ---------------------------------------------------------------------------
# 2. 범용 주제 충돌 오판
# ---------------------------------------------------------------------------

def learning(
    learning_id: int,
    answer: str,
    topics: list[str],
    *,
    model: str = "LH50BEHHLGFXKR",
    source: str = "APPROVED_EDITED",
    effective_at: str | None = None,
):
    metadata: dict[str, object] = {"learning_topics": topics}
    if effective_at:
        metadata["effective_at"] = effective_at
    return {
        "id": learning_id,
        "learning_source": source,
        "model_code": model,
        "store_code": "COUPANG_OJE_NS",
        "question_normalized": "q",
        "final_answer": answer,
        "metadata_json": metadata,
    }


def resolve(rows, *, priority=None):
    ranked = [(1.0 - index * 0.01, row) for index, row in enumerate(rows)]
    return _resolve_latest_valid_conflicts(
        ranked, priority_for=priority or (lambda _item: 0))


# 11196 의 실제 충돌 쌍. 서로 다른 질문의 답이고, 둘 다 주제가 OTHER 다.
PC_MONITOR = "안녕하세요 고객님 pc모니터로 이용은 가능하시나, 모니터만큼 원할하진않습니다."
NAVER_LOGIN = "안녕하세요. 해당 링크의 경우 네이버 로그인이 필요합니다."


def test_05_generic_topic_alone_is_not_the_same_subject():
    """11196 재현: OTHER 만 같은 두 답변은 충돌로 묶이지 않는다."""

    kept, conflicts = resolve([
        learning(1, PC_MONITOR, ["OTHER"]),
        learning(2, NAVER_LOGIN, ["OTHER"]),
    ])
    assert conflicts == []
    assert sorted(row["id"] for _score, row in kept) == [1, 2]


@pytest.mark.parametrize("topic", sorted(GENERIC_TOPICS - {"OTHER"}))
def test_06_the_other_generic_topics_still_conflict(topic):
    """GENERAL_POLICY·PRODUCT_SPEC 은 주제를 가리킨다 -- 실제 충돌은 보류된다.

    GENERIC_TOPICS 전체를 빼면 이 둘의 상반된 정책·사양도 통과한다. 제외는
    ``OTHER`` 하나로 한정한다.
    """

    kept, conflicts = resolve([
        learning(1, "설치 가능합니다.", [topic]),
        learning(2, "설치는 불가능합니다.", [topic]),
    ])
    assert len(conflicts) == 1
    assert kept == []


def test_07_a_real_conflict_on_an_explicit_topic_still_blocks():
    """명시적 전문 주제·동일 모델의 반대 주장은 그대로 양쪽 보류된다."""

    kept, conflicts = resolve([
        learning(1, "벽걸이 설치 가능합니다.", ["STAND_BRACKET_VESA"]),
        learning(2, "벽걸이 설치는 불가능합니다.", ["STAND_BRACKET_VESA"]),
    ])
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "UNRESOLVED_NO_RELIABLE_TIME"
    assert conflicts[0]["winner_learning_id"] is None
    assert kept == []


def test_08_a_conflict_needs_the_same_topic_and_scope():
    """주제가 다르거나 모델이 다르면 종전처럼 충돌이 아니다."""

    _kept, other_topic = resolve([
        learning(1, "벽걸이 설치 가능합니다.", ["STAND_BRACKET_VESA"]),
        learning(2, "HDMI 연결은 불가합니다.", ["PORT_CONNECTIVITY"]),
    ])
    assert other_topic == []
    _kept, other_model = resolve([
        learning(1, "벽걸이 설치 가능합니다.", ["STAND_BRACKET_VESA"]),
        learning(2, "벽걸이 설치는 불가능합니다.", ["STAND_BRACKET_VESA"],
                 model="LH43BEHHLGFXKR"),
    ])
    assert other_model == []


def test_09_higher_authority_still_resolves():
    """권한이 높은 쪽이 이기는 기존 정책은 유지된다."""

    kept, conflicts = resolve(
        [
            learning(1, "벽걸이 설치 가능합니다.", ["STAND_BRACKET_VESA"]),
            learning(2, "벽걸이 설치는 불가능합니다.", ["STAND_BRACKET_VESA"]),
        ],
        priority=lambda item: 2 if int(item["id"]) == 1 else 1,
    )
    assert conflicts[0]["reason"] == "HIGHER_AUTHORITY"
    assert conflicts[0]["winner_learning_id"] == 1
    assert [row["id"] for _score, row in kept] == [1]


def test_10_newer_effective_approval_still_resolves():
    """양쪽에 신뢰 가능한 시각이 있으면 최신이 이기는 정책도 유지된다."""

    kept, conflicts = resolve([
        learning(1, "벽걸이 설치 가능합니다.", ["STAND_BRACKET_VESA"],
                 effective_at="2026-10-01T00:00:00Z"),
        learning(2, "벽걸이 설치는 불가능합니다.", ["STAND_BRACKET_VESA"],
                 effective_at="2026-09-01T00:00:00Z"),
    ])
    assert conflicts[0]["reason"] == "NEWER_EFFECTIVE_APPROVAL"
    assert conflicts[0]["winner_learning_id"] == 1
    assert [row["id"] for _score, row in kept] == [1]


@pytest.mark.parametrize("topic", ["GENERAL_POLICY", "PRODUCT_SPEC"])
def test_06b_real_policy_and_spec_conflicts_are_held(topic):
    """상반된 정책·사양 쌍은 각각 양쪽 보류된다."""

    pairs = {
        "GENERAL_POLICY": ("반품 가능합니다.", "반품은 불가합니다."),
        "PRODUCT_SPEC": ("스피커가 내장 가능합니다.", "스피커는 내장 불가합니다."),
    }[topic]
    kept, conflicts = resolve([
        learning(1, pairs[0], [topic]),
        learning(2, pairs[1], [topic]),
    ])
    assert len(conflicts) == 1
    assert conflicts[0]["topics"] == [topic]
    assert kept == []


def test_11_relevance_order_does_not_decide_a_conflict():
    """relevance 는 충돌 판정에 관여하지 않는다 -- 기준을 바꾸지 않았다."""

    rows = [
        learning(1, "벽걸이 설치 가능합니다.", ["STAND_BRACKET_VESA"]),
        learning(2, "벽걸이 설치는 불가능합니다.", ["STAND_BRACKET_VESA"]),
    ]
    forward, conflicts_forward = resolve(rows)
    backward, conflicts_backward = resolve(list(reversed(rows)))
    assert len(conflicts_forward) == len(conflicts_backward) == 1
    assert forward == backward == []
