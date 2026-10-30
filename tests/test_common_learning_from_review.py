"""승인 완료 영역에서 공통 Learning 을 그 자리에서 등록한다.

처음 구현은 Learning Manager 로 이동했다. 운영자는 열어둔 문의와 필터·검색어·
페이지를 잃고 한 건을 편집한 뒤 다시 찾아 돌아와야 했다. 그래서 편집기를 문의
상세 안으로 옮겼다 -- 폼과 저장 경로, 중복 검사는 Learning Manager 의 것을 그대로
쓰고 그려지는 자리만 바뀌었다.

이 파일이 고정하는 것은 그 자리다. 버튼이 승인된 Learning 이 있을 때만 나오는지,
편집기가 같은 화면에서 펼쳐지는지, 저장·취소·오류 어느 쪽으로 끝나도 화면 상태가
유지되는지, 그리고 문의를 바꿨을 때 앞 문의의 입력이 섞이지 않는지.

저장 로직 자체(적용 범위·마켓·기간·중복·검색 반영)는
``test_cross_model_policy_scope.py`` 가 고정하므로 여기서는 되풀이하지 않고,
이 경로로 저장한 값이 실제로 그렇게 들어갔는지까지만 확인한다.
"""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.learning_repository import (
    LearningRepository,
    is_common_learning,
)
from services.approval_service import ApprovalService
from ui.review_workspace import approval_learning_trace
from tests.test_positive_learning_review import _context


BUTTON_LABEL = "공통 Learning 추가"
SAVE_LABEL = "공통 Learning 저장"
CANCEL_LABEL = "취소"
QUESTION_LABEL = "공통으로 사용할 질문"
ANSWER_LABEL = "공통으로 사용할 답변"


def _approved(tmp_path, name: str):
    """승인까지 끝난 문의 하나. 승인은 기존 ApprovalService 가 한다."""

    database, inquiry_id, draft = _context(tmp_path, name)
    ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        actor="staff-common",
    )
    rows = LearningRepository(database).for_inquiry(inquiry_id)
    assert len(rows) == 1, rows
    return database, inquiry_id, int(rows[0]["id"])


def _second_inquiry(database: Database, name: str) -> tuple[int, int]:
    """같은 DB 에 승인된 문의를 하나 더 만든다."""

    from repositories.inquiry_repository import InquiryRepository
    from repositories.workflow_repository import WorkflowRepository

    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": f"SECOND-{name}",
        "inquiry_type": "PRODUCT_GENERAL",
        "title": "교환 문의",
        "content": "교환은 어떻게 하나요?",
        "post_status": "NOT_POSTED",
        "raw_json": {},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer="고객센터 접수 후 교환해 드립니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="staff-common",
    )
    rows = LearningRepository(database).for_inquiry(inquiry_id)
    assert len(rows) == 1, rows
    return inquiry_id, int(rows[0]["id"])


def _panel(database: Database, inquiry_id: int) -> AppTest:
    """문의 상세의 답변 패널만 렌더한다 -- 승인 완료 카드가 여기 있다."""

    return AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    ).run(timeout=40)


def _labels(app: AppTest) -> set[str]:
    return {button.label for button in app.button}


def _area(app: AppTest, label: str):
    return next(area for area in app.text_area if area.label == label)


def _has_area(app: AppTest, label: str) -> bool:
    return label in {area.label for area in app.text_area}


def _click(app: AppTest, label: str) -> AppTest:
    next(button for button in app.button if button.label == label).click()
    app.run(timeout=40)
    return app


def _choose(app: AppTest, label: str, value: str) -> AppTest:
    """라디오를 **원시값**으로 바꾸고, 실제로 바뀐 것을 확인한다.

    ``AppTest`` 의 ``set_value`` 는 맞지 않는 값을 조용히 무시한다 -- 표시
    라벨("기간 지정")을 넘기면 ``유효기간`` 라디오는 그대로 PERMANENT 로
    남았다. 그대로 두면 기본값을 단정하는 테스트가 통과해 버리므로, 값이
    바뀐 것을 여기서 확인한다.
    """

    next(radio for radio in app.radio if radio.label == label).set_value(value)
    app.run(timeout=40)
    assert not app.exception
    actual = next(radio for radio in app.radio if radio.label == label).value
    assert actual == value, f"{label} 을 {value} 로 바꾸지 못했다 (현재 {actual})"
    return app


# ===========================================================================
# 1. 버튼 표시 조건
# ===========================================================================

def test_the_button_appears_once_a_learning_is_approved(tmp_path):
    database, inquiry_id, learning_id = _approved(tmp_path, "shown")

    app = _panel(database, inquiry_id)

    assert not app.exception
    rendered = "\n".join(item.value for item in app.markdown)
    # 기존 승인 완료 표시는 그대로 남아야 한다.
    assert "승인 완료" in rendered
    assert f"Learning ID: {learning_id}" in rendered
    assert BUTTON_LABEL in _labels(app)
    # 아직 편집기는 닫혀 있다.
    assert not _has_area(app, QUESTION_LABEL)


def test_the_button_is_absent_before_approval(tmp_path):
    database, inquiry_id, _draft = _context(tmp_path, "not-approved")

    app = _panel(database, inquiry_id)

    assert not app.exception
    assert BUTTON_LABEL not in _labels(app)


def test_the_button_is_absent_when_the_approval_left_no_learning(tmp_path):
    database, inquiry_id, learning_id = _approved(tmp_path, "no-learning")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE learning_examples SET active=0 WHERE id=?", (learning_id,)
        )

    app = _panel(database, inquiry_id)

    assert not app.exception
    assert BUTTON_LABEL not in _labels(app)


def test_the_button_stays_available_after_the_answer_is_posted(tmp_path):
    """이미 등록된 답변에서도 접근 가능해야 한다."""

    database, inquiry_id, _ = _approved(tmp_path, "posted")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET post_status='POSTED', source_answered=1 "
            "WHERE id=?",
            (inquiry_id,),
        )

    app = _panel(database, inquiry_id)

    assert not app.exception
    assert BUTTON_LABEL in _labels(app)


# ===========================================================================
# 2, 3, 4. 같은 화면에서 열리고, 이동하지 않고, 값이 채워진다
# ===========================================================================

def test_clicking_opens_the_editor_in_place(tmp_path):
    database, inquiry_id, learning_id = _approved(tmp_path, "inplace")
    origin = LearningRepository(database).get(learning_id)

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)

    assert not app.exception
    # 원본 질문과 승인된 최종 답변이 채워져 있어야 한다.
    assert _area(app, QUESTION_LABEL).value == origin["question_original_masked"]
    assert _area(app, ANSWER_LABEL).value == origin["final_answer"]
    radios = {radio.label for radio in app.radio}
    assert {"적용 모델", "적용 마켓", "유효기간"} <= radios, radios
    assert SAVE_LABEL in _labels(app)
    assert CANCEL_LABEL in _labels(app)


def test_opening_the_editor_does_not_navigate_away(tmp_path):
    """Learning Manager 로 이동하지 않는다.

    ``app.py`` 의 라우팅은 ``current_page`` 로 화면을 고르므로, 그 값이
    ``learning`` 으로 바뀌면 다음 rerun 에서 대시보드를 떠난다. 편집기를
    여는 것만으로 그 값이 생기지 않아야 한다.
    """

    database, inquiry_id, _ = _approved(tmp_path, "nonav")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)

    assert not app.exception
    assert "current_page" not in app.session_state
    assert "production_admin_mode" not in app.session_state
    # 승인 완료 카드도 그대로 남아 있어야 한다.
    assert "승인 완료" in "\n".join(item.value for item in app.markdown)


# ===========================================================================
# 5~8. 저장 -- 선택한 부분과 적용 범위가 그대로 들어간다
# ===========================================================================

def test_only_the_selected_part_is_saved(tmp_path):
    """복합 문의에서 필요한 질문·답변만 남겨 저장할 수 있다."""

    database, inquiry_id, learning_id = _approved(tmp_path, "part")
    repository = LearningRepository(database)
    origin_before = repository.get(learning_id)

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    _area(app, QUESTION_LABEL).set_value("거래명세서 발급되나요?")
    _area(app, ANSWER_LABEL).set_value("구매 내역 > 주문 상세에서 받으실 수 있습니다.")
    app.run(timeout=40)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    listed = repository.common_learnings()
    assert len(listed) == 1, listed
    created = listed[0]
    assert created["question_original_masked"] == "거래명세서 발급되나요?"
    assert "주문 상세" in created["final_answer"]
    assert is_common_learning(created["metadata_json"])
    assert created["metadata_json"]["common_learning_origin_id"] == learning_id
    # 원본은 그대로다.
    origin_after = repository.get(learning_id)
    assert origin_after["question_original_masked"] == (
        origin_before["question_original_masked"]
    )
    assert origin_after["final_answer"] == origin_before["final_answer"]
    assert "common_learning" not in origin_after["metadata_json"]


@pytest.mark.parametrize(
    "scope", ["POLICY", "MODEL"],
)
def test_each_application_target_is_stored(tmp_path, scope):
    database, inquiry_id, _ = _approved(tmp_path, f"scope-{scope}")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    app = _choose(app, "적용 모델", scope)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    created = LearningRepository(database).common_learnings()[0]
    assert created["metadata_json"]["product_scope"] == scope


def test_product_family_is_not_offered_for_a_common_learning(tmp_path):
    """제품군 정보가 저장된 행이 없으므로 "동일 제품군"은 "현재 모델"과
    같은 범위로만 적용될 수 있다. 같은 범위에 두 이름을 주지 않는다."""

    database, inquiry_id, _ = _approved(tmp_path, "scope-family")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)

    radio = next(radio for radio in app.radio if radio.label == "적용 모델")
    assert list(radio.options) == ["현재 모델", "전 모델 공통 정책"]


@pytest.mark.parametrize(
    "applicability", ["NAVER_ONLY", "COUPANG_ONLY", "COMMON"],
)
def test_each_market_scope_is_stored(tmp_path, applicability):
    database, inquiry_id, _ = _approved(tmp_path, f"market-{applicability}")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    app = _choose(app, "적용 마켓", applicability)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    created = LearningRepository(database).common_learnings()[0]
    assert created["metadata_json"]["market_applicability"] == applicability


def test_a_period_can_be_set_from_the_dashboard(tmp_path):
    database, inquiry_id, _ = _approved(tmp_path, "period")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    app = _choose(app, "유효기간", "TEMPORARY")
    next(item for item in app.text_input if item.label == "이벤트명").set_value(
        "10월 카드 행사"
    )
    app.run(timeout=40)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    created = LearningRepository(database).common_learnings()[0]
    assert created["validity_type"] == "TEMPORARY"
    assert created["event_name"] == "10월 카드 행사"
    assert created["valid_from"] and created["valid_until"]


# ===========================================================================
# 9, 10, 11. 저장·취소·오류 후 화면 유지
# ===========================================================================

def test_the_inquiry_screen_survives_a_save(tmp_path):
    database, inquiry_id, learning_id = _approved(tmp_path, "stay-save")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    # 같은 문의의 승인 완료 화면이 그대로다.
    rendered = "\n".join(item.value for item in app.markdown)
    assert "승인 완료" in rendered
    assert f"Learning ID: {learning_id}" in rendered
    assert "current_page" not in app.session_state
    # 편집 영역은 닫히고, 새 ID 가 표시된다.
    assert not _has_area(app, QUESTION_LABEL)
    created = LearningRepository(database).common_learnings()[0]
    assert any(str(created["id"]) in item.value for item in app.success), [
        item.value for item in app.success
    ]
    assert BUTTON_LABEL in _labels(app)


def test_the_inquiry_screen_survives_a_cancel(tmp_path):
    database, inquiry_id, learning_id = _approved(tmp_path, "stay-cancel")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    _area(app, QUESTION_LABEL).set_value("저장하지 않을 질문")
    app.run(timeout=40)
    app = _click(app, CANCEL_LABEL)

    assert not app.exception
    assert not LearningRepository(database).common_learnings(), "취소했는데 저장됐다"
    rendered = "\n".join(item.value for item in app.markdown)
    assert "승인 완료" in rendered
    assert f"Learning ID: {learning_id}" in rendered
    assert "current_page" not in app.session_state
    assert not _has_area(app, QUESTION_LABEL)


def test_a_cancelled_editor_reopens_from_the_origin_text(tmp_path):
    """취소한 뒤 다시 열면 지웠던 원본 문장이 돌아와야 한다."""

    database, inquiry_id, learning_id = _approved(tmp_path, "reopen")
    origin = LearningRepository(database).get(learning_id)

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    _area(app, QUESTION_LABEL).set_value("임시로 바꾼 질문")
    app.run(timeout=40)
    app = _click(app, CANCEL_LABEL)
    app = _click(app, BUTTON_LABEL)

    assert not app.exception
    assert _area(app, QUESTION_LABEL).value == origin["question_original_masked"]


def test_a_save_error_keeps_the_typed_values_and_the_screen(tmp_path):
    """중복 저장이 거부돼도 입력과 화면이 유지된다."""

    database, inquiry_id, learning_id = _approved(tmp_path, "error")
    repository = LearningRepository(database)
    origin = repository.get(learning_id)
    # 같은 원본·본문·범위로 이미 등록해 둔다 -> 두 번째 저장은 거부된다.
    repository.add_common_learning(
        learning_id,
        question=origin["question_original_masked"],
        answer=origin["final_answer"],
        product_scope="POLICY", market_applicability="COMMON",
        actor="staff-common",
    )

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    errors = "\n".join(item.value for item in app.error)
    assert "이미 등록" in errors, errors
    # 편집 영역이 열린 채로 입력이 남아 있어야 한다.
    assert _area(app, QUESTION_LABEL).value == origin["question_original_masked"]
    assert _area(app, ANSWER_LABEL).value == origin["final_answer"]
    assert SAVE_LABEL in _labels(app)
    # 화면도 그대로다.
    assert "승인 완료" in "\n".join(item.value for item in app.markdown)
    assert "current_page" not in app.session_state
    assert len(repository.common_learnings()) == 1


# ===========================================================================
# 12. 문의를 바꿨을 때 입력이 섞이지 않는다
# ===========================================================================

def test_another_inquiry_does_not_inherit_the_open_editor(tmp_path):
    database, first_id, _first_learning = _approved(tmp_path, "mix")
    second_id, _second_learning = _second_inquiry(database, "mix")

    first = _click(_panel(database, first_id), BUTTON_LABEL)
    _area(first, QUESTION_LABEL).set_value("첫 문의에서만 쓰는 질문")
    first.run(timeout=40)
    assert not first.exception

    second = _panel(database, second_id)

    assert not second.exception
    # 두 번째 문의는 편집기가 닫힌 상태로 시작한다.
    assert not _has_area(second, QUESTION_LABEL)
    assert BUTTON_LABEL in _labels(second)


def test_two_inquiries_keep_separate_editor_state(tmp_path):
    """한 화면에 두 문의가 있어도 열린 편집기는 누른 쪽 하나뿐이다."""

    database, first_id, first_learning = _approved(tmp_path, "sep")
    second_id, second_learning = _second_inquiry(database, "sep")
    repository = LearningRepository(database)

    app = AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
inquiries=InquiryRepository(db)
_render_answer_panel(db, inquiries.get({first_id}))
_render_answer_panel(db, inquiries.get({second_id}))
'''
    ).run(timeout=60)
    assert not app.exception
    buttons = [item for item in app.button if item.label == BUTTON_LABEL]
    assert len(buttons) == 2, len(buttons)

    buttons[0].click()
    app.run(timeout=60)

    assert not app.exception
    areas = [area for area in app.text_area if area.label == QUESTION_LABEL]
    assert len(areas) == 1, "한 문의만 열려야 한다"
    assert areas[0].value == repository.get(first_learning)[
        "question_original_masked"
    ]
    assert repository.get(second_learning)["question_original_masked"] != (
        areas[0].value
    )


# ===========================================================================
# 13~15. 저장 결과와 관리 목록
# ===========================================================================

def _manager(database: Database) -> AppTest:
    return AppTest.from_string(
        f'''
from repositories.database import Database
from ui.learning_manager import render_learning_manager
db=Database(r"{database.path}")
db.initialize()
render_learning_manager(db)
'''
    ).run(timeout=60)


def test_the_saved_row_is_in_the_management_list(tmp_path):
    database, inquiry_id, _learning_id = _approved(tmp_path, "listed")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    app = _click(app, SAVE_LABEL)
    assert not app.exception

    manager = _manager(database)

    assert not manager.exception
    headers = "\n".join(item.value for item in manager.subheader)
    assert "공통 Learning 관리" in headers
    created = LearningRepository(database).common_learnings()[0]
    options = [
        str(option)
        for box in manager.selectbox if box.label == "상세 관리 항목"
        for option in box.options
    ]
    assert options, "상세 관리 항목이 비어 있다"
    assert any(f"공통 {created['id']}" in option for option in options), options


def test_a_duplicate_save_is_refused(tmp_path):
    database, inquiry_id, _ = _approved(tmp_path, "dup")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    app = _click(app, SAVE_LABEL)
    assert not app.exception
    app = _click(app, BUTTON_LABEL)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    assert "이미 등록" in "\n".join(item.value for item in app.error)
    assert len(LearningRepository(database).common_learnings()) == 1


def test_a_common_learning_row_does_not_offer_the_editor_again(tmp_path):
    """공통 Learning 자체에는 '공통 Learning 추가' 가 나오지 않는다."""

    from ui.learning_manager import render_common_learning_add

    database, _inquiry_id, learning_id = _approved(tmp_path, "already")
    repository = LearningRepository(database)
    origin = repository.get(learning_id)
    created = repository.add_common_learning(
        learning_id,
        question=origin["question_original_masked"],
        answer=origin["final_answer"],
        product_scope="POLICY", market_applicability="COMMON",
        actor="staff-common",
    )

    assert is_common_learning(created["metadata_json"])
    assert render_common_learning_add(
        created, repository, key_prefix="noop"
    ) is None


# ===========================================================================
# 16, 17. 기존 기능 유지
# ===========================================================================

def test_learning_manager_still_offers_the_same_editor(tmp_path):
    """Learning Manager 쪽 공통 Learning 추가도 그대로 동작한다."""

    database, _inquiry_id, _learning_id = _approved(tmp_path, "manager")

    manager = _manager(database)

    assert not manager.exception
    headers = "\n".join(item.value for item in manager.subheader)
    assert "Positive Learning" in headers
    assert "공통 Learning 관리" in headers
    # 상세 안의 추가 버튼은 남아 있다.
    assert BUTTON_LABEL in {item.label for item in manager.button}


def test_the_approved_learning_id_is_the_one_handed_to_the_editor(tmp_path):
    """편집기가 받는 원본 id 가 그 문의의 승인 Learning 인지 확인한다."""

    from repositories.approval_repository import ApprovalRepository

    database, inquiry_id, learning_id = _approved(tmp_path, "id-match")
    trace = approval_learning_trace(
        database,
        inquiry_id=inquiry_id,
        draft=AnswerRepository(database).active_for_inquiry(inquiry_id),
        approval_state=ApprovalRepository(database).get_inquiry_approval(
            inquiry_id
        ),
        source_answered=False,
    )

    assert trace["approval_complete"] is True
    assert int(trace["positive_learning_id"]) == learning_id
    assert learning_id in list(trace["approved_learning_ids"])


def test_saving_does_not_touch_the_inquiry_or_its_approval(tmp_path):
    """자동답변·자동등록이 읽는 상태를 바꾸지 않는다."""

    database, inquiry_id, _ = _approved(tmp_path, "untouched")
    columns = (
        "workflow_status", "post_status", "source_answered", "post_error_code",
    )
    with database.connection() as connection:
        before = dict(connection.execute(
            f"SELECT {', '.join(columns)} FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone())

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    app = _click(app, SAVE_LABEL)
    assert not app.exception

    with database.connection() as connection:
        after = dict(connection.execute(
            f"SELECT {', '.join(columns)} FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone())
    assert after == before
