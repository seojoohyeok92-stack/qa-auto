"""문의 상세의 "승인 완료" 영역에서 공통 Learning 을 바로 추가한다.

운영자는 답변을 승인한 자리에서 그 답변의 공통 부분을 등록한다. 승인 직후
Learning Manager 로 넘어가 방금 만들어진 id 를 다시 찾아야 한다면 그 경로는
쓰이지 않는다.

이 파일이 고정하는 것은 연결이다 -- 버튼이 승인된 Learning 이 있을 때만
나타나는지, 넘기는 id 가 그 Learning 의 것인지, 그리고 넘어간 화면에서 해당
Learning 의 편집기가 **바로** 열리는지. 저장 로직과 중복 방지, 마켓·기간
동작은 ``test_cross_model_policy_scope.py`` 가 이미 고정하고 있으므로 여기서는
되풀이하지 않고, 이 경로로 저장한 결과가 관리 목록에 들어오는지까지만 본다.

UI 는 ``AppTest`` 로 실제 렌더해서 읽는다. 버튼의 존재 조건을 테스트가 다시
구현하면 조건이 갈라져도 둘 다 통과해 버리기 때문이다.
"""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from repositories.database import Database
from repositories.learning_repository import (
    LearningRepository,
    is_common_learning,
)
from services.approval_service import ApprovalService
from ui.learning_manager import (
    COMMON_LEARNING_TARGET_KEY,
    open_common_learning_editor,
)
from ui.review_workspace import approval_learning_trace
from tests.test_positive_learning_review import _context


BUTTON_LABEL = "공통 Learning 추가"


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


def _answer_panel(database: Database, inquiry_id: int) -> AppTest:
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


# ===========================================================================
# 버튼 표시 조건
# ===========================================================================

def test_the_button_appears_once_a_learning_is_approved(tmp_path):
    database, inquiry_id, learning_id = _approved(tmp_path, "shown")

    app = _answer_panel(database, inquiry_id)

    assert not app.exception
    rendered = "\n".join(item.value for item in app.markdown)
    # 기존 승인 완료 표시는 그대로 남아야 한다.
    assert "승인 완료" in rendered
    assert f"Learning ID: {learning_id}" in rendered
    assert BUTTON_LABEL in _labels(app)


def test_the_button_is_absent_before_approval(tmp_path):
    database, inquiry_id, _draft = _context(tmp_path, "not-approved")

    app = _answer_panel(database, inquiry_id)

    assert not app.exception
    assert BUTTON_LABEL not in _labels(app)


def test_the_button_is_absent_when_the_approval_left_no_learning(tmp_path):
    """Learning ID 가 없으면 표시하지 않는다.

    승인 Learning 을 비활성화하면 ``approval_learning_trace`` 의
    ``positive_learning_id`` 가 비고, 버튼의 조건이 바로 그 값이다.
    """

    database, inquiry_id, learning_id = _approved(tmp_path, "no-learning")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE learning_examples SET active=0 WHERE id=?", (learning_id,)
        )

    app = _answer_panel(database, inquiry_id)

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

    app = _answer_panel(database, inquiry_id)

    assert not app.exception
    assert BUTTON_LABEL in _labels(app)


# ===========================================================================
# 전달되는 Learning ID
# ===========================================================================

def test_the_trace_carries_the_id_the_button_hands_over(tmp_path):
    """버튼이 넘기는 값이 그 문의의 승인 Learning 인지 확인한다."""

    database, inquiry_id, learning_id = _approved(tmp_path, "id-match")
    from repositories.answer_repository import AnswerRepository
    from repositories.approval_repository import ApprovalRepository

    draft = AnswerRepository(database).active_for_inquiry(inquiry_id)
    trace = approval_learning_trace(
        database,
        inquiry_id=inquiry_id,
        draft=draft,
        approval_state=ApprovalRepository(database).get_inquiry_approval(
            inquiry_id
        ),
        source_answered=False,
    )

    assert trace["approval_complete"] is True
    assert int(trace["positive_learning_id"]) == learning_id


def test_opening_the_editor_sets_the_target_and_route():
    """``open_common_learning_editor`` 가 세션에 남기는 것.

    ``app.py`` 의 라우팅 가드는 admin mode 가 아니면 비-dashboard 페이지를
    되돌려 보내므로, 두 키가 함께 설정되어야 화면이 실제로 열린다.
    """

    app = AppTest.from_string(
        '''
import streamlit as st
from ui.learning_manager import open_common_learning_editor
open_common_learning_editor(319327)
st.write("done")
'''
    ).run(timeout=40)

    assert not app.exception
    assert app.session_state[COMMON_LEARNING_TARGET_KEY] == 319327
    assert app.session_state["current_page"] == "learning"
    assert app.session_state["production_admin_mode"] is True
    # 편집기가 열린 상태로 시작해야 한다 -- 넘어간 화면에서 다시 버튼을
    # 누르게 만들지 않는다.
    assert app.session_state["common_target_common_open_319327"] is True


# ===========================================================================
# 넘어간 화면에서 편집기가 바로 열린다
# ===========================================================================

def _manager(database: Database, learning_id: int | None) -> AppTest:
    """Learning Manager 를 렌더한다.

    버튼은 한 번 눌리므로 ``open_common_learning_editor`` 도 첫 실행에서만
    호출한다. 매 rerun 마다 호출하면 저장 후 비워진 대상이 다시 채워져,
    테스트가 production 과 다른 것을 보게 된다 -- 실제로 그렇게 한 번 틀렸다.
    """

    target = (
        f'open_common_learning_editor({learning_id})'
        if learning_id is not None else "pass"
    )
    return AppTest.from_string(
        f'''
import streamlit as st
from repositories.database import Database
from ui.learning_manager import render_learning_manager, open_common_learning_editor
db=Database(r"{database.path}")
db.initialize()
if not st.session_state.get("_clicked_once"):
    st.session_state["_clicked_once"] = True
    {target}
render_learning_manager(db)
'''
    ).run(timeout=60)


def test_the_manager_opens_the_editor_on_the_handed_over_learning(tmp_path):
    database, inquiry_id, learning_id = _approved(tmp_path, "deeplink")
    origin = LearningRepository(database).get(learning_id)

    app = _manager(database, learning_id)

    assert not app.exception
    headers = "\n".join(item.value for item in app.subheader)
    assert "공통 Learning 추가" in headers
    # 원본 질문과 승인된 최종 답변이 미리 채워져 있어야 한다.
    areas = {area.label: area.value for area in app.text_area}
    assert "공통으로 사용할 질문" in areas, list(areas)
    assert "공통으로 사용할 답변" in areas, list(areas)
    assert areas["공통으로 사용할 질문"] == origin["question_original_masked"]
    assert areas["공통으로 사용할 답변"] == origin["final_answer"]
    # 적용 범위와 유효기간 선택이 함께 있어야 한다.
    radios = {radio.label for radio in app.radio}
    assert {"적용 모델", "적용 마켓", "유효기간"} <= radios, radios


def test_the_manager_renders_normally_without_a_target(tmp_path):
    """기존 Learning Manager 동작은 그대로다."""

    database, _inquiry_id, _learning_id = _approved(tmp_path, "notarget")

    app = _manager(database, None)

    assert not app.exception
    assert COMMON_LEARNING_TARGET_KEY not in app.session_state
    headers = "\n".join(item.value for item in app.subheader)
    assert "Positive Learning" in headers
    assert "공통 Learning 관리" in headers
    # 대상이 없으면 편집기 입력란도 없다.
    assert "공통으로 사용할 질문" not in {
        area.label for area in app.text_area
    }


def test_a_missing_target_is_reported_rather_than_crashing(tmp_path):
    database, _inquiry_id, _learning_id = _approved(tmp_path, "missing")

    app = _manager(database, 999999)

    assert not app.exception
    warnings = "\n".join(item.value for item in app.warning)
    assert "999999" in warnings


def test_a_target_that_is_already_common_is_reported(tmp_path):
    database, _inquiry_id, learning_id = _approved(tmp_path, "already")
    repository = LearningRepository(database)
    created = repository.add_common_learning(
        learning_id,
        question="거래명세서는 어떻게 발급하나요?",
        answer="구매 내역에서 거래명세서를 확인해 주세요.",
        product_scope="POLICY", market_applicability="COMMON",
        actor="staff-common",
    )

    app = _manager(database, int(created["id"]))

    assert not app.exception
    info = "\n".join(item.value for item in app.info)
    assert "이미 공통 Learning" in info


# ===========================================================================
# 이 경로로 저장한 결과
# ===========================================================================

def test_saving_through_this_route_stores_only_the_selected_part(tmp_path):
    """복합 문의의 일부만 저장되고 원본은 변하지 않는다.

    편집기 조작은 ``AppTest`` 로 하고, 저장은 Manager 가 호출하는 것과 같은
    ``add_common_learning`` 이 처리한다.
    """

    database, inquiry_id, learning_id = _approved(tmp_path, "save")
    repository = LearningRepository(database)
    origin_before = repository.get(learning_id)

    app = _manager(database, learning_id)
    assert not app.exception
    question = next(
        area for area in app.text_area if area.label == "공통으로 사용할 질문"
    )
    answer = next(
        area for area in app.text_area if area.label == "공통으로 사용할 답변"
    )
    question.set_value("거래명세서 발급되나요?")
    answer.set_value("구매 내역 > 주문 상세에서 거래명세서를 받으실 수 있습니다.")
    app.run(timeout=60)
    next(
        button for button in app.button if button.label == "공통 Learning 저장"
    ).click()
    app.run(timeout=60)

    assert not app.exception
    listed = repository.common_learnings()
    assert len(listed) == 1, listed
    created = listed[0]
    assert created["question_original_masked"] == "거래명세서 발급되나요?"
    assert "주문 상세" in created["final_answer"]
    assert is_common_learning(created["metadata_json"])
    assert created["metadata_json"]["common_learning_origin_id"] == learning_id

    origin_after = repository.get(learning_id)
    assert origin_after["question_original_masked"] == (
        origin_before["question_original_masked"]
    )
    assert origin_after["final_answer"] == origin_before["final_answer"]
    assert "common_learning" not in origin_after["metadata_json"]


def test_the_target_is_cleared_after_a_save(tmp_path):
    """저장하면 대상이 비워져 관리 목록으로 돌아간다."""

    database, _inquiry_id, learning_id = _approved(tmp_path, "cleared")

    app = _manager(database, learning_id)
    next(
        button for button in app.button if button.label == "공통 Learning 저장"
    ).click()
    app.run(timeout=60)

    assert not app.exception
    assert COMMON_LEARNING_TARGET_KEY not in app.session_state
    assert len(LearningRepository(database).common_learnings()) == 1


def test_a_duplicate_save_through_this_route_is_refused(tmp_path):
    database, _inquiry_id, learning_id = _approved(tmp_path, "dup")
    repository = LearningRepository(database)
    origin = repository.get(learning_id)
    repository.add_common_learning(
        learning_id,
        question=origin["question_original_masked"],
        answer=origin["final_answer"],
        product_scope="POLICY", market_applicability="COMMON",
        actor="staff-common",
    )

    app = _manager(database, learning_id)
    next(
        button for button in app.button if button.label == "공통 Learning 저장"
    ).click()
    app.run(timeout=60)

    assert not app.exception
    errors = "\n".join(item.value for item in app.error)
    assert "이미 등록" in errors, errors
    assert len(repository.common_learnings()) == 1


def test_the_saved_row_shows_up_in_the_management_list(tmp_path):
    database, _inquiry_id, learning_id = _approved(tmp_path, "listed")
    repository = LearningRepository(database)
    origin = repository.get(learning_id)
    created = repository.add_common_learning(
        learning_id,
        question=origin["question_original_masked"],
        answer=origin["final_answer"],
        product_scope="POLICY", market_applicability="NAVER_ONLY",
        actor="staff-common",
    )

    app = _manager(database, None)

    assert not app.exception
    headers = "\n".join(item.value for item in app.subheader)
    assert "공통 Learning 관리" in headers
    # 선택 목록에 그 행이 올라와 있어야 한다.
    options = [
        str(option)
        for box in app.selectbox if box.label == "상세 관리 항목"
        for option in box.options
    ]
    assert options, "상세 관리 항목 선택 목록이 비어 있다"
    assert any(f"공통 {created['id']}" in option for option in options), options


@pytest.mark.parametrize("active", [False, True])
def test_stopping_and_resuming_from_the_management_list(tmp_path, active):
    """사용 중지·다시 사용이 이 경로로 만든 행에서도 동작한다."""

    database, _inquiry_id, learning_id = _approved(tmp_path, f"toggle-{active}")
    repository = LearningRepository(database)
    origin = repository.get(learning_id)
    created = repository.add_common_learning(
        learning_id,
        question=origin["question_original_masked"],
        answer=origin["final_answer"],
        product_scope="POLICY", market_applicability="COMMON",
        actor="staff-common",
    )
    common_id = int(created["id"])
    if not active:
        row = repository.get(common_id)
        repository.update_validity(
            common_id, validity_type="PERMANENT", validity_active=False,
            condition={}, expected_updated_at=str(row["updated_at"]),
        )

    app = _manager(database, None)

    assert not app.exception
    labels = _labels(app)
    assert ("사용 중지" if active else "다시 사용") in labels, labels
    # 중지된 항목도 목록에서 사라지지 않는다.
    assert len(repository.common_learnings()) == 1
