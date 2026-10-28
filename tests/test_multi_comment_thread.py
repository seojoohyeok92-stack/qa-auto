"""문의 하나에 달린 여러 댓글을 문의 상세에서 전부 보여준다.

마켓이 실제로 주는 것만 보여준다는 것이 이 파일의 요지다.

쿠팡 ``commentDtoList`` 는 26.10.22 스냅숏의 댓글 7,780 개가 모두 네 필드만
가진다 -- ``content``, ``inquiryCommentAt``, ``inquiryCommentId``,
``inquiryId``. **작성자·역할 필드가 없다.** 그래서 시각과 순서는 읽어서 보여주고
작성자는 추측하지 않고 '확인 불가' 로 둔다. 표시 경로가 이 본문들을 빈 줄로
이어 하나의 대표 답변으로 만들던 것은 그대로 두고, 경계와 시각만 따로 복원한다.

네이버는 응답에 댓글 목록이 아예 없다. ``naver_posted_answers`` 의 복수 행은
``observe()`` 가 ``sha256("NAVER_POSTED|{inquiry_id}|{body}")`` 를 키로 쓰고 매번
그 문의의 ``is_current`` 를 모두 0 으로 돌린 뒤 하나만 1 로 세우기 때문에 생긴다
-- 즉 답변 한 칸의 본문이 관측 사이에 바뀐 이력이다. 공존하는 여러 댓글이
아니므로 댓글처럼 표시하지 않는다.
"""
from __future__ import annotations

import json

import pytest
from streamlit.testing.v1 import AppTest

from answer.answer_provenance import AnswerProvenance
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from repositories.naver_posted_answer_repository import (
    NaverPostedAnswerRepository,
)
from ui.review_workspace import (
    AUTHOR_UNKNOWN,
    comment_thread,
    naver_answer_observations,
)


COUPANG_STORE = "COUPANG_OJE"
NAVER_STORE = "OJE_PLUS"


def _comment(body: str, *, at: str | None = "2025-10-02T16:37:10+09:00",
             comment_id: str = "1") -> dict:
    """쿠팡이 실제로 주는 네 필드만 가진 댓글."""

    return {
        "content": body,
        "inquiryCommentAt": at,
        "inquiryCommentId": comment_id,
        "inquiryId": "143636712",
    }


def _coupang(comments: list[dict] | None, *, nested: bool = True) -> dict:
    payload = {"inquiryId": "143636712", "content": "거래명세서 주세요"}
    if comments is not None:
        payload["commentDtoList"] = comments
    return {"raw_json": {"source_payload": payload} if nested else payload,
            "store_code": COUPANG_STORE}


# ===========================================================================
# 1~4. 댓글 0건 / 1건 / 여러 건
# ===========================================================================

def test_no_comment_list_yields_nothing():
    assert comment_thread({"raw_json": {}, "store_code": COUPANG_STORE}) == []
    assert comment_thread({}) == []


@pytest.mark.parametrize("comments", [[], None])
def test_an_empty_comment_list_yields_nothing(comments):
    assert comment_thread(_coupang(comments)) == []


def test_a_single_comment_is_read_but_needs_no_expander():
    thread = comment_thread(_coupang([_comment("하나뿐인 답변")]))

    assert len(thread) == 1
    assert thread[0]["body"] == "하나뿐인 답변"
    # 한 건이면 대표 답변과 같으므로 화면에서 expander 를 열지 않는다.
    assert len(thread) < 2


def test_several_comments_keep_their_order_and_times():
    thread = comment_thread(_coupang([
        _comment("첫 번째 안내입니다.", at="2025-10-02T16:37:10+09:00",
                 comment_id="137117766"),
        _comment("이어서 링크를 보냅니다.", at="2025-10-02T16:37:57+09:00",
                 comment_id="137117802"),
        _comment("마지막 확인 부탁드립니다.", at="2025-10-02T16:40:00+09:00",
                 comment_id="137117900"),
    ]))

    assert [item["order"] for item in thread] == [1, 2, 3]
    assert [item["body"][:2] for item in thread] == ["첫 ", "이어", "마지"]
    assert [item["comment_id"] for item in thread] == [
        "137117766", "137117802", "137117900",
    ]
    assert thread[0]["written_at"] == "2025-10-02T16:37:10+09:00"


def test_a_customer_follow_up_keeps_its_place_in_the_thread():
    """고객 추가 질문과 판매자 답변이 번갈아 있어도 순서가 보존된다.

    누가 썼는지는 데이터에 없으므로 모두 '확인 불가' 이고, 이 테스트가
    고정하는 것은 순서와 본문이 섞이지 않는다는 것뿐이다.
    """

    thread = comment_thread(_coupang([
        _comment("배송은 2~3일 소요됩니다.", comment_id="1"),
        _comment("그럼 주말에도 배송되나요?", comment_id="2"),
        _comment("주말 배송은 불가합니다.", comment_id="3"),
    ]))

    assert [item["order"] for item in thread] == [1, 2, 3]
    assert "주말에도" in thread[1]["body"]
    assert "불가" in thread[2]["body"]
    assert {item["author"] for item in thread} == {AUTHOR_UNKNOWN}


def test_a_flat_payload_is_read_too():
    """``source_payload`` 로 감싸지지 않은 형태도 읽는다."""

    thread = comment_thread(_coupang([_comment("본문")], nested=False))

    assert len(thread) == 1


# ===========================================================================
# 5, 6. 작성자 / 시각이 없을 때
# ===========================================================================

def test_the_author_is_never_guessed():
    """쿠팡 응답에 작성자 필드가 없으므로 항상 '확인 불가' 다."""

    thread = comment_thread(_coupang([
        _comment("판매자가 쓴 것처럼 보이는 안내입니다."),
        _comment("고객이 쓴 것처럼 보이는 질문인가요?"),
    ]))

    assert [item["author"] for item in thread] == [
        AUTHOR_UNKNOWN, AUTHOR_UNKNOWN,
    ]


@pytest.mark.parametrize("at", [None, "", "   "])
def test_a_missing_time_is_left_empty_rather_than_invented(at):
    thread = comment_thread(_coupang([_comment("시각 없는 댓글", at=at)]))

    assert len(thread) == 1
    assert thread[0]["written_at"] == ""


def test_an_extra_field_is_ignored_rather_than_read_as_an_author():
    """응답에 없는 필드를 가정하지 않는다.

    작성자처럼 보이는 키가 들어오더라도 작성자로 읽지 않는다 -- 쿠팡 응답에
    그런 필드가 있다는 근거가 없기 때문이다.
    """

    comment = _comment("본문")
    comment["memberId"] = "buyer-123"
    thread = comment_thread(_coupang([comment]))

    assert thread[0]["author"] == AUTHOR_UNKNOWN


def test_blank_bodies_are_dropped_and_order_is_renumbered_from_source():
    """빈 content 는 보여줄 것이 없으므로 제외한다. 순서는 원본 색인을 쓴다."""

    thread = comment_thread(_coupang([
        _comment("첫 댓글", comment_id="1"),
        _comment("   ", comment_id="2"),
        _comment("세 번째 댓글", comment_id="3"),
    ]))

    assert [item["order"] for item in thread] == [1, 3]
    assert [item["comment_id"] for item in thread] == ["1", "3"]


def test_a_non_dict_entry_does_not_break_the_thread():
    thread = comment_thread(_coupang([_comment("정상"), "문자열", None]))

    assert len(thread) == 1


# ===========================================================================
# 7. 네이버 -- 반복 관측과 본문 변경을 댓글로 보여주지 않는다
# ===========================================================================

def _naver_inquiry(database: Database, external_id: str = "Q-1") -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": NAVER_STORE,
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external_id,
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "상품문의",
        "content": "설치 일정 알려주세요",
        "raw_json": {},
    }).inquiry_id


def _observe(database: Database, inquiry_id: int, body: str) -> None:
    NaverPostedAnswerRepository(database).observe(
        inquiry_id=inquiry_id,
        answer_body=body,
        answer_id=None,
        posted_at=None,
        author_type=None,
        source_api="PRODUCT_INQUIRY",
        metadata={},
    )


@pytest.fixture()
def database(tmp_path) -> Database:
    value = Database(tmp_path / "thread.db")
    value.initialize()
    return value


def test_repeated_observation_of_one_answer_stays_one_entry(database):
    """같은 본문을 여러 번 관측해도 한 건이다.

    ``source_key`` 가 본문 해시이므로 같은 본문은 같은 행으로 합쳐진다.
    """

    inquiry_id = _naver_inquiry(database)
    for _ in range(3):
        _observe(database, inquiry_id, "설치 예정일은 카카오톡으로 안내됩니다.")

    observations = naver_answer_observations(database, inquiry_id)

    assert len(observations) == 1
    assert observations[0]["is_current"] is True


def test_a_changed_answer_body_becomes_a_second_observation(database):
    inquiry_id = _naver_inquiry(database)
    _observe(database, inquiry_id, "처음 안내드린 답변입니다.")
    _observe(database, inquiry_id, "수정한 답변입니다.")

    observations = naver_answer_observations(database, inquiry_id)

    assert len(observations) == 2
    assert [item["order"] for item in observations] == [1, 2]
    # 마지막으로 관측된 본문 하나만 현재다.
    assert [item["is_current"] for item in observations] == [False, True]


def test_no_observation_yields_nothing(database):
    inquiry_id = _naver_inquiry(database)

    assert naver_answer_observations(database, inquiry_id) == []


def test_an_empty_body_observation_is_not_listed(database):
    inquiry_id = _naver_inquiry(database)
    NaverPostedAnswerRepository(database).observe(
        inquiry_id=inquiry_id, answer_body=None,
        answer_id=None, posted_at=None, author_type=None,
        source_api="PRODUCT_INQUIRY", metadata={},
    )

    assert naver_answer_observations(database, inquiry_id) == []


def test_a_naver_inquiry_has_no_coupang_comment_thread(database):
    """마켓별 데이터가 섞이지 않는다."""

    inquiry_id = _naver_inquiry(database)
    row = InquiryRepository(database).get(inquiry_id)

    assert comment_thread(row) == []


def test_coupang_comments_do_not_leak_into_another_inquiry(database):
    """다른 문의·스토어의 댓글이 섞이지 않는다."""

    inquiries = InquiryRepository(database)
    first = inquiries.upsert_work_item({
        "store_code": COUPANG_STORE, "source_type": "COUPANG_ONLINE_INQUIRY",
        "source_question_id": "CP-1", "inquiry_type": "PRODUCT_INQUIRY",
        "title": "Coupang 상품문의", "content": "문의 1",
        "raw_json": {"source_payload": {
            "commentDtoList": [_comment("첫 문의 답변", comment_id="11")]
        }},
    }).inquiry_id
    second = inquiries.upsert_work_item({
        "store_code": "COUPANG_OJE_NS", "source_type": "COUPANG_ONLINE_INQUIRY",
        "source_question_id": "CP-2", "inquiry_type": "PRODUCT_INQUIRY",
        "title": "Coupang 상품문의", "content": "문의 2",
        "raw_json": {"source_payload": {
            "commentDtoList": [_comment("둘째 문의 답변", comment_id="22")]
        }},
    }).inquiry_id

    first_thread = comment_thread(inquiries.get(first))
    second_thread = comment_thread(inquiries.get(second))

    assert [item["comment_id"] for item in first_thread] == ["11"]
    assert [item["comment_id"] for item in second_thread] == ["22"]
    assert first != second


# ===========================================================================
# 9, 10. 대표 답변과 화면
# ===========================================================================

def _panel(
    database: Database,
    *,
    store_code: str = COUPANG_STORE,
    source_type: str = "COUPANG_ONLINE_INQUIRY",
    external_id: str = "CP-UI",
) -> AppTest:
    """상세 화면과 같은 경로로 행을 읽어 패널을 렌더한다.

    쿠팡 댓글을 하나의 대표 답변으로 합치는 것은
    ``InquiryRepository._attach_marketplace_details`` 이고, 단일 행에서는
    ``get_by_source`` 만 그것을 적용한다. ``get(id)`` 로 읽으면
    ``seller_answer`` 가 비어 쿠팡 판매자 답변 뷰 자체가 선택되지 않고,
    그러면 이 파일의 화면 단정이 전부 공허해진다 -- 실제로 한 번 그랬다.
    """

    return AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
row=InquiryRepository(db).get_by_source(
    "{store_code}", "{source_type}", "{external_id}")
assert row is not None, "fixture row not found"
_render_answer_panel(db, row)
'''
    ).run(timeout=40)


def _coupang_inquiry(
    database: Database, comments: list[dict], *, external_id: str = "CP-UI"
) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": COUPANG_STORE,
        "source_type": "COUPANG_ONLINE_INQUIRY",
        "source_question_id": external_id,
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "Coupang 상품문의",
        "content": "거래명세서 주세요",
        # 운영 데이터는 7,509 건 모두 ``commentDtoList`` 를 최상위와
        # ``source_payload`` 양쪽에 담고 있고, 대표 답변을 합치는
        # ``_attach_marketplace_details`` 는 최상위만 읽는다. 픽스처도 같은
        # 모양으로 둔다.
        "raw_json": {
            "inquiryId": "143636712",
            "commentDtoList": comments,
            "source_payload": {
                "inquiryId": "143636712", "commentDtoList": comments,
            },
        },
    }).inquiry_id
    # 답변 패널은 workflow 단계와 draft 가 있어야 답변 뷰를 그린다.
    WorkflowRepository(database).initialize_steps(inquiry_id)
    AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="GENERAL", reason="test",
            answer="확인 후 안내드리겠습니다.", provider="rules",
            auto_answerable=True, needs_review=False,
        ),
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_answered=1 WHERE id=?", (inquiry_id,)
        )
    return inquiry_id


def _expanders(app: AppTest) -> list[str]:
    return [str(getattr(item, "label", "")) for item in app.expander]


def test_a_single_comment_adds_no_expander(database):
    inquiry_id = _coupang_inquiry(database, [_comment("하나뿐인 답변")])

    app = _panel(database)

    assert not app.exception
    assert not any("전체 답글" in label for label in _expanders(app))


def test_several_comments_add_the_expander(database):
    inquiry_id = _coupang_inquiry(database, [
        _comment("첫 안내", comment_id="1"),
        _comment("이어지는 링크", comment_id="2"),
    ])

    app = _panel(database)

    assert not app.exception
    labels = _expanders(app)
    assert any("전체 답글 보기 (2건)" in label for label in labels), labels
    body = app.get("text")
    texts = [str(item.value) for item in body]
    assert any("첫 안내" in text for text in texts), texts
    assert any("이어지는 링크" in text for text in texts), texts


def test_the_representative_answer_is_unchanged(database):
    """대표 답변 표시는 기존 동작 그대로다 -- 합쳐진 본문이 그대로 보인다."""

    _coupang_inquiry(database, [
        _comment("첫 안내", comment_id="1"),
        _comment("이어지는 링크", comment_id="2"),
    ])
    row = InquiryRepository(database).get_by_source(
        COUPANG_STORE, "COUPANG_ONLINE_INQUIRY", "CP-UI",
    )

    # 표시 경로의 합치기는 건드리지 않았다.
    expected = "첫 안내" + chr(10) * 2 + "이어지는 링크"
    assert row.get("seller_answer") == expected


# ===========================================================================
# 11, 12. Learning 승인과 자동등록에 영향 없음
# ===========================================================================

def test_reading_the_thread_writes_nothing(database):
    inquiry_id = _coupang_inquiry(database, [
        _comment("첫 안내", comment_id="1"), _comment("둘째", comment_id="2"),
    ])
    columns = (
        "workflow_status", "post_status", "source_answered", "raw_json",
    )
    with database.connection() as connection:
        before = dict(connection.execute(
            f"SELECT {', '.join(columns)} FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone())

    comment_thread(InquiryRepository(database).get(inquiry_id))
    naver_answer_observations(database, inquiry_id)
    _panel(database)

    with database.connection() as connection:
        after = dict(connection.execute(
            f"SELECT {', '.join(columns)} FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone())
    assert after == before


def test_the_thread_creates_no_learning(database):
    """고객 추가 질문이나 과거 답변이 Learning 으로 올라가지 않는다."""

    inquiry_id = _coupang_inquiry(database, [
        _comment("판매자 안내", comment_id="1"),
        _comment("고객 추가 질문인가요?", comment_id="2"),
    ])

    comment_thread(InquiryRepository(database).get(inquiry_id))
    _panel(database)

    assert LearningRepository(database).for_inquiry(inquiry_id) == []
    assert LearningRepository(database).common_learnings() == []


def test_the_multi_comment_sync_guard_is_unchanged():
    """다중 댓글은 동기화 단계에서 대표 답변으로 선택되지 않는다.

    이 보호가 기존에 있었고, 이번 변경은 표시만 추가했으므로 그대로여야 한다.
    """

    from services.coupang_inquiry_normalizer import _single_comment_answer

    answer, _at, _id, status = _single_comment_answer([
        _comment("첫 댓글"), _comment("둘째 댓글"),
    ])
    assert answer is None
    assert status == "MULTI_COMMENT_REVIEW"

    answer, _at, _id, status = _single_comment_answer([_comment("단일 댓글")])
    assert answer == "단일 댓글"
    assert status == "SINGLE_COMMENT"


def test_naver_observations_do_not_change_the_current_answer(database):
    """관측 이력을 읽어도 ``current()`` 가 가리키는 대표 답변은 그대로다."""

    inquiry_id = _naver_inquiry(database)
    _observe(database, inquiry_id, "처음 답변")
    _observe(database, inquiry_id, "수정한 답변")
    repository = NaverPostedAnswerRepository(database)
    before = repository.current(inquiry_id)

    naver_answer_observations(database, inquiry_id)

    after = repository.current(inquiry_id)
    assert after["answer_body"] == before["answer_body"] == "수정한 답변"
    assert after["provenance"] == AnswerProvenance.NAVER_POSTED.value


def test_an_approved_answer_is_untouched_by_the_thread(database):
    """승인된 Learning 과 draft 가 영향을 받지 않는다."""

    from services.approval_service import ApprovalService
    from repositories.workflow_repository import WorkflowRepository

    inquiry_id = _naver_inquiry(database, "Q-APPROVE")
    WorkflowRepository(database).initialize_steps(inquiry_id)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="GENERAL", reason="test",
            answer="설치 예정일은 카카오톡으로 안내됩니다.", provider="rules",
            auto_answerable=True, needs_review=False,
        ),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="staff",
    )
    before = LearningRepository(database).for_inquiry(inquiry_id)
    assert len(before) == 1

    _observe(database, inquiry_id, "관측된 본문")
    naver_answer_observations(database, inquiry_id)

    after = LearningRepository(database).for_inquiry(inquiry_id)
    assert len(after) == 1
    assert after[0]["final_answer"] == before[0]["final_answer"]


# ===========================================================================
# 13. 실제 운영 구조에 대한 회귀 -- 작성자 필드는 없다
# ===========================================================================

def test_the_operational_payload_still_has_no_author_field():
    """작성자 필드가 생기면 '확인 불가' 표시를 다시 판단해야 한다.

    26.10.22 스냅숏의 쿠팡 댓글 7,780 개는 네 필드만 가졌다. 마켓이 필드를
    추가하면 이 테스트가 먼저 알려준다.
    """

    import pathlib
    import sqlite3

    source = pathlib.Path("data/서버pc_data_26.10.22/oje_automation.db")
    if not source.exists():
        pytest.skip("운영 DB 복사본이 이 PC 에 없다")
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT raw_json FROM inquiries WHERE store_code LIKE 'COUPANG%'"
        ).fetchall()
    finally:
        connection.close()

    keys: set[str] = set()
    total = 0
    for (payload,) in rows:
        try:
            raw = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            continue
        nested = raw.get("source_payload")
        nested = nested if isinstance(nested, dict) else {}
        comments = nested.get("commentDtoList") or raw.get("commentDtoList")
        if not isinstance(comments, list):
            continue
        for comment in comments:
            if isinstance(comment, dict):
                total += 1
                keys |= set(comment)

    assert total > 0, "쿠팡 댓글이 하나도 없다는 전제는 틀렸다"
    assert keys == {
        "content", "inquiryCommentAt", "inquiryCommentId", "inquiryId",
    }, keys
