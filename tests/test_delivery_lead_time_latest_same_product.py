"""배송기간 문의: 같은 상품·조건의 최신 승인 Learning 하나만 근거로 쓴다.

10901/10953 (LH43B 43인치) 은 같은 리스팅의 "구매일로부터 약 1-2주" 가 후보
아래쪽에 있고 다른 상품·다른 시기의 "3일", "3주" 가 위 다섯 자리를 차지해
일반 배송기간 근거를 찾지 못하고 보류됐다. 11586 은 제주도 문의에 대한
"+1~2일 추가소요" (319103) 가, 10984 는 "섬 지역의 경우 약 2주" (319049) 가
일반 고객의 배송기간으로 쓰일 수 있었다.
"""
from __future__ import annotations

import pytest

from services.learning_evidence_policy import asks_dated_arrival
from services.similar_answer_service import _latest_same_product_lead_time


def _row(row_id, answer, *, match="EXACT_PRODUCT", created="2026-08-20T00:00:00Z",
         verified=None, question="배송 문의"):
    metadata = {"verified_at": verified} if verified else {}
    return (0.5, {
        "id": row_id, "final_answer": answer, "created_at": created,
        "question_original_masked": question, "question_normalized": question,
        "metadata_json": metadata, "compatibility": {"product_match": match},
    })


OLD = _row(1, "구매일로부터 약 2~3주 소요됩니다.", created="2026-08-06T00:00:00Z")
NEW = _row(2, "구매일로부터 약 1~2주 정도 소요됩니다.", created="2026-08-27T00:00:00Z")
OTHER_PRODUCT = _row(3, "배송은 영업일 기준 2~3일 정도 소요됩니다.", match="MISMATCH",
                     created="2026-10-12T00:00:00Z")
JEJU = _row(4, "제주도 배송 가능합니다. 일반적인 배송보다 +1~2일 추가소요될 수 있습니다.",
            created="2026-09-28T00:00:00Z", question="제주도 배송 가능한거죠?")
ISLAND = _row(5, "섬 지역의 경우 설치까지 약 2주 정도 소요될 수 있습니다.",
              created="2026-09-21T00:00:00Z")
INCHEON = _row(6, "인천 지역 설치의 경우, 배송/설치까지 1~2주 소요될 수 있습니다.",
               created="2026-09-30T00:00:00Z")
UNRELATED = _row(7, "벽걸이 브라켓은 별도 구매 없이 포함됩니다.")
SAME_MODEL = _row(8, "구매일로부터 약 1~2주 소요되는 상품입니다.", match="EXACT_MODEL",
                  created="2026-10-01T00:00:00Z")

Q = "오늘 주문하면 언제 받을 수 있나요?"


def _ids(ranked):
    return [item["id"] for _score, item in ranked]


def test_the_most_recently_approved_same_product_row_is_the_only_lead_time():
    ranked, choice = _latest_same_product_lead_time(
        [OTHER_PRODUCT, OLD, UNRELATED, NEW], question=Q)

    assert _ids(ranked) == [2, 7]
    assert choice["selected_learning_id"] == 2
    assert sorted(choice["removed_lead_time_learning_ids"]) == [1, 3]


def test_approval_date_outranks_creation_date():
    approved_late = _row(9, "구매일로부터 약 3주 소요됩니다.",
                         created="2026-08-01T00:00:00Z", verified="2026-09-15T00:00:00Z")

    ranked, _choice = _latest_same_product_lead_time([NEW, approved_late], question=Q)

    assert _ids(ranked) == [9]


def test_the_same_listing_comes_before_the_same_model():
    ranked, _choice = _latest_same_product_lead_time([SAME_MODEL, NEW], question=Q)

    assert _ids(ranked) == [2]


def test_the_same_model_is_used_when_the_listing_has_none():
    ranked, _choice = _latest_same_product_lead_time([OTHER_PRODUCT, SAME_MODEL], question=Q)

    assert _ids(ranked) == [8]


@pytest.mark.parametrize("remote", [JEJU, ISLAND])
def test_a_remote_area_lead_time_is_not_an_ordinary_customers(remote):
    ranked, choice = _latest_same_product_lead_time([remote, NEW], question=Q)

    assert _ids(ranked) == [2]


def test_a_remote_area_customer_gets_the_remote_lead_time():
    ranked, _choice = _latest_same_product_lead_time(
        [NEW, JEJU], question="제주도인데 주문하면 언제 받을 수 있나요?")

    assert _ids(ranked) == [4]


def test_another_regions_lead_time_is_not_used():
    ranked, _choice = _latest_same_product_lead_time(
        [INCHEON, NEW], question="오늘 주문하면 경기도에 언제 배송되나요?")

    assert _ids(ranked) == [2]


def test_a_region_lead_time_stands_when_the_customer_names_none():
    ranked, _choice = _latest_same_product_lead_time([NEW, INCHEON], question=Q)

    assert _ids(ranked) == [6]


def test_no_same_product_row_changes_nothing_and_the_hold_stands():
    pool = [OTHER_PRODUCT, UNRELATED]

    ranked, choice = _latest_same_product_lead_time(pool, question=Q)

    assert ranked == pool and choice is None


def test_only_other_condition_rows_leave_and_nothing_is_chosen():
    ranked, choice = _latest_same_product_lead_time(
        [JEJU, OTHER_PRODUCT, UNRELATED], question=Q)

    assert _ids(ranked) == [3, 7]
    assert choice["selected_learning_id"] is None


@pytest.mark.parametrize("question", [
    "오늘 주문하면 내일 오나요?",
    "월요일에 주문하면 수요일에 받을 수 있나요?",
    "이번 주 안에 받을 수 있나요?",
    "10월 23일 금요일에 상품을 받고싶은데 언제쯤 주문하면 맞출 수 있을까요?",  # 10952
    "토요일에 맞춰 받으려면 언제 주문해야 하나요?",
    "내일까지 배송바랍니다 꼭이요",  # 11479
])
def test_a_dated_arrival_question_is_a_lead_time_question(question):
    assert asks_dated_arrival(question)


@pytest.mark.parametrize("question", [
    "배송일을 10월 20일 이후로 지정할 수 있나요?",
    "설치일을 다음 주로 변경해 주세요",
    "벽걸이 설치도 해주시나요?",
])
def test_choosing_a_day_or_a_non_date_question_is_not(question):
    assert not asks_dated_arrival(question)


def test_the_prompt_forbids_back_calculating_and_keeps_the_basis_wording():
    import answer.prompt_builder as prompt_builder
    from pathlib import Path

    text = Path(prompt_builder.__file__).read_text(encoding="utf-8")
    assert "역산해 날짜로 말하지 않는다" in text
    assert "'출고일 기준'·'영업일 기준'" in text
    assert "'내일까지 배송 바랍니다'" in text
