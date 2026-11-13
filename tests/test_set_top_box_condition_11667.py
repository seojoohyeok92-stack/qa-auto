"""기존 셋톱박스를 기사님이 연결해 주는지 묻는 질문에 신규 회선 Learning을 쓰지 않는다.

11667 "SK유선 셋탑박스 사용중인데 기사님이오셔서 연결해주시나요?" 에 108663
("기사님 오셔서 설치만 하면 되나요? 아님 유선방송 기사님도 불러야 하나요?" ->
"방송사 기사님 방문이 필요할 수 있으니") 이 근거로 붙어 직원 답변("셋탑박스
사용중이면 연결까지 해드립니다") 과 반대인 답변이 SAFE 가 됐다. 11030 은 같은
오류가 실제로 게시됐다.

질문이 셋톱박스와 기사를 함께 말할 때만, 질문에 셋톱박스가 없는 Learning 을
뺀다. 셋톱박스 OTT·RF 단자 질문이나 셋톱박스 없는 설치 질문은 그대로다.
"""
from __future__ import annotations

import json

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.similar_answer_service import SimilarAnswerService

SIGNAGE = "삼성 4K UHD 스마트 사이니지 TV 1등급 티비 기사님 방문설치 107.9cm(43인치), 스탠드"

NEW_LINE = "new-cable-line"          # 108663
EXISTING_BOX = "existing-set-top"    # 115140
BOX_SHIPPING = "set-top-shipping"    # 154689
RF_PORT = "rf-port"                  # 318219 류
GENERIC_VISIT = "generic-visit"      # 154549


def _row(key, question, answer):
    return {
        "source_key": key, "learning_source": "APPROVED_UNEDITED",
        "question_original_masked": question,
        "question_normalized": f"상품 문의 {question}",
        "store_code": "OJE_PLUS", "inquiry_type": "PRODUCT_INQUIRY",
        "intent": "상품", "final_answer": answer, "rating": 5, "active": 1,
        "validity_active": 1, "validity_type": "PERMANENT",
        "product_name": SIGNAGE, "model_code": "",
        "metadata_json": json.dumps(
            {"human_verified": True, "learning_signal_type": "POSITIVE"},
            ensure_ascii=False),
    }


ROWS = {
    NEW_LINE: _row(
        NEW_LINE,
        "부모님 작은방에 설치하려는데요 기사님 오셔서 설치만 하심되나요? 아님 유선방송 기사님도 불러야 하나요?",
        "해당 상품은 삼성 기사님이 방문하여 설치하는 상품입니다. 유선방송(케이블TV) 연결이"
        " 필요한 경우는 별도로 해당 방송사 기사님 방문이 필요할 수 있으니 참고 부탁드립니다."),
    EXISTING_BOX: _row(
        EXISTING_BOX,
        "기존 TV와 연결해서 쓰던 KT 셋탑박스가 있는데 기사님께서 오시면 따로 연결해서 셋팅해주시는지 궁금합니다.",
        "해당 상품은 기사님이 직접 방문하여 설치해 드리며, 기존에 사용하시던 KT 셋탑박스도"
        " 함께 연결해 드립니다."),
    BOX_SHIPPING: _row(
        BOX_SHIPPING,
        "TV 벽걸이 설치시 셋톱박스가 같이배송 설치돼는지 궁금합니다.",
        "셋탑박스의 경우 택배로 배송되는 제품이며 직접 설치를 진행해주셔야 합니다."),
    RF_PORT: _row(
        RF_PORT,
        "동축케이블 연결로는 시청 못하나요?",
        "해당모델은 동축케이블 있는 모델로 동축케이블 연결 시 tv시청 가능합니다."),
    GENERIC_VISIT: _row(
        GENERIC_VISIT,
        "설치는 삼성 기사님이 오시는 것인가요 협력 업체 기사님이 오시는 건가요?",
        "해당 상품은 삼성 기사님이 방문하여 설치하는 상품입니다."),
}


def _store(tmp_path, keys):
    database = Database(tmp_path / "learning.db")
    database.initialize()
    with database.transaction() as connection:
        columns = {c[1] for c in connection.execute(
            "PRAGMA table_info(learning_examples)")}
        for key in keys:
            usable = {k: v for k, v in ROWS[key].items() if k in columns}
            connection.execute(
                f"INSERT INTO learning_examples ({','.join(usable)})"
                f" VALUES ({','.join('?' for _ in usable)})",
                list(usable.values()))
    return database


def _search(database, question, goal="INSTALLATION_METHOD"):
    service = SimilarAnswerService(LearningRepository(database))
    selected = service.search(
        question, store_code="OJE_PLUS", product_name=SIGNAGE,
        inquiry_type="PRODUCT_INQUIRY", limit=8, hard_conflicts_only=True,
        semantic_goal={
            "customer_goal": goal, "requested_information": question,
            "atomic_question": question, "all_atomic_questions": [],
            "retrieval_queries": [question],
            "order_evidence_required": False, "schedule_scoped": False,
        },
    )
    keys = {
        key for key, row in ROWS.items()
        for item in selected if item["final_answer"] == row["final_answer"]
    }
    return keys, dict(service.last_trace or {})


Q_11667 = "SK유선 셋탑박스 사용중인데 기사님이오셔서 연결해주시나요?"


def test_11667_does_not_take_the_new_cable_line_answer(tmp_path):
    keys, trace = _search(_store(tmp_path, ROWS), Q_11667)

    assert NEW_LINE not in keys
    assert GENERIC_VISIT not in keys
    assert EXISTING_BOX in keys
    assert BOX_SHIPPING in keys
    assert trace["rejection_counts"]["CONTEXT_POLICY_REJECTED"] >= 2


@pytest.mark.parametrize("question", [
    "셋톱박스 연결도 설치 기사님이 해주시나요?",                      # 11030
    "스탠드를 자가설치한 후 기사님이 방문하여 셋탑박스를 설치해주는지 궁금합니다.",  # 11565
    # 11063: 기사라는 말이 없어도 같은 요청이다.
    "스카이라이프 셋톱박스를 사용 중인 설치 장소에서 TV와 셋톱박스를 연결해 주시나요?",
])
def test_other_set_top_box_by_technician_questions(tmp_path, question):
    keys, _trace = _search(_store(tmp_path, ROWS), question)

    assert NEW_LINE not in keys
    assert EXISTING_BOX in keys


def test_without_a_matching_learning_nothing_is_offered(tmp_path):
    """조건에 맞는 Learning 이 없으면 근거 없음 -> 기존 보류 정책."""

    keys, _trace = _search(_store(tmp_path, [NEW_LINE, GENERIC_VISIT]), Q_11667)

    assert keys == set()


# ------------------------------------------------- 조건이 아닌 질문은 그대로
def test_a_set_top_box_question_without_a_technician_is_unchanged(tmp_path):
    keys, _trace = _search(
        _store(tmp_path, ROWS), "셋톱박스 없이 동축케이블만으로 시청할 수 있나요?",
        goal="PRODUCT_SPEC")

    assert RF_PORT in keys


def test_a_technician_question_without_a_set_top_box_is_unchanged(tmp_path):
    keys, _trace = _search(
        _store(tmp_path, ROWS), "기사님 오셔서 설치만 하면 되나요? 유선방송 기사님도 불러야 하나요?")

    assert NEW_LINE in keys
    assert GENERIC_VISIT in keys


def test_an_ott_set_top_box_question_is_unchanged(tmp_path):
    """'셋톱박스를 연결하면 OTT 되나요' 는 기사 작업 요청이 아니다."""

    from services.similar_answer_service import _SET_TOP_BOX, _TECHNICIAN

    question = "셋톱박스를 연결하면 OTT나 유튜브 시청이 가능한가요?"
    assert _SET_TOP_BOX.search(question) and not _TECHNICIAN.search(question)
