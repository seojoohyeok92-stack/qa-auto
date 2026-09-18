"""What the answer panel draws for an inquiry production only reads.

The write gates were already in place; the screen still looked like work to do.
A Coupang inquiry that has a stored draft (two such drafts exist, created
before selection stopped generating them, and are kept as a record) showed
"검토 대기", a draft warning, generation notes and a row of buttons.  Now it
shows the inquiry, the marketplace's own reply, Learning context read-only and
a copy of that reply -- and a Naver inquiry shows exactly what it did.
"""

from __future__ import annotations

from pathlib import Path
import re
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.coupang_inquiry_normalizer import CoupangInquiryNormalizer
from services.inquiry_sync_service import normalize_work_item

REPLY = "안녕하세요 고객님, 해당 모델은 벽걸이 설치가 가능합니다."
DRAFT_TEXT = "예전에 잘못 만들어진 프로그램 답변 초안입니다."
CSS = (Path(__file__).resolve().parents[1] / "ui" / "dashboard.css").read_text(encoding="utf-8")


def _draft(database: Database, inquiry_id: int) -> None:
    AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="GENERAL", reason="test",
            answer=DRAFT_TEXT, provider="rules", auto_answerable=True, needs_review=False,
        ),
    )


def _coupang(
    database: Database, inquiry_id: str, *, answered: bool,
    content: str = "벽걸이 설치 되나요?",
) -> tuple[str, str, str]:
    """As it happened: a draft made while unanswered, the reply read back later."""

    def ready(with_reply: bool) -> dict:
        return normalize_work_item(
            CoupangInquiryNormalizer().online(
                _payload(inquiry_id, with_reply, content), account_code="OJE_NS"
            ).to_work_item()
        )

    first = ready(False)
    row_id = InquiryRepository(database).upsert_work_item(first).inquiry_id
    _draft(database, row_id)
    if answered:
        assert InquiryRepository(database).upsert_work_item(ready(True)).inquiry_id == row_id
    return first["store_code"], first["source_type"], first["source_question_id"]


def _payload(inquiry_id: str, answered: bool, content: str = "벽걸이 설치 되나요?") -> dict:
    return {
        "inquiryId": inquiry_id, "sellerProductId": "15654321531",
        "vendorItemId": "93128932886", "content": content,
        "inquiryAt": "2026-09-16T10:00:00+09:00", "orderIds": [],
        "commentDtoList": [{
            "inquiryCommentId": "c-1", "inquiryId": inquiry_id, "content": REPLY,
            "inquiryCommentAt": "2026-09-16T11:00:00+09:00",
        }] if answered else [],
    }


def _naver(database: Database) -> tuple[str, str, str]:
    row_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "N-1", "inquiry_type": "PRODUCT_INQUIRY",
        "title": "설치 문의", "content": "벽걸이 설치 되나요?", "product_name": "삼성 TV",
        "post_status": "NOT_POSTED", "raw_json": {},
    }).inquiry_id
    _draft(database, row_id)
    return "OJE_PLUS", "PRODUCT_INQUIRY", "N-1"


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "panel.db")
    value.initialize()
    return value


def _render(database: Database, identity: tuple[str, str, str]) -> AppTest:
    store, source, question = identity
    app = AppTest.from_string(f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db = Database(r"{database.path}")
_render_answer_panel(db, InquiryRepository(db).get_by_source("{store}", "{source}", "{question}"))
''').run(timeout=60)
    assert not app.exception, app.exception
    return app


def _texts(app: AppTest) -> str:
    parts = [*app.markdown, *app.caption, *app.info, *app.warning, *app.success]
    return "\n".join(str(element.value) for element in parts)


def _labels(elements) -> list[str]:
    return [str(element.label) for element in elements]


def _activity_rows(database: Database) -> int:
    connection = sqlite3.connect(str(database.path))
    try:
        return connection.execute("SELECT COUNT(*) FROM activity_logs").fetchone()[0]
    finally:
        connection.close()


# --- Coupang, answered, with a stored draft ------------------------------------

@pytest.fixture
def coupang_app(database):
    identity = _coupang(database, "160852234", answered=True)
    before = _activity_rows(database)
    app = _render(database, identity)
    return app, database, before


def test_status_reads_read_only_not_awaiting_review(coupang_app) -> None:
    app, _, _ = coupang_app
    text = _texts(app)
    assert "조회전용" in text
    assert "답변 조회" in text
    assert "검토 대기" not in text
    assert "답변 검토 및 승인" not in text


def test_no_draft_warning_or_generation_notes(coupang_app) -> None:
    text = _texts(coupang_app[0])
    for phrase in ("현재 작성 중인 초안", "새 답변을 생성하면", "주문번호는 보존되며",
                   "주문번호 요청 답변을 생성", "확정 운영 템플릿", "GPT가 답변 초안을",
                   "Validator"):
        assert phrase not in text, phrase
    assert "조회 전용입니다" in text


def test_no_generation_edit_or_registration_controls(coupang_app) -> None:
    app, _, _ = coupang_app
    buttons = _labels(app.button)
    for label in ("GPT 새 답변 생성", "초기화", "임시 저장"):
        assert label not in buttons, label
    assert not any("답변 등록" in label or "답변 생성" in label for label in buttons)
    assert "확정 운영 템플릿 사용" not in _labels(app.checkbox)
    expanders = _labels(app.expander)
    for label in ("수정 피드백", "Validator 및 GPT 상세", "처리 진단"):
        assert label not in expanders, label


def test_the_learning_approval_is_offered_exactly_as_on_naver(coupang_app) -> None:
    """The reply is read-only; whether to learn from it is still a decision.

    Approval here never registers anything at Coupang -- the reply is already
    there.  It is the Naver decision, so it is the Naver controls: Positive
    Learning 설정, 승인, and a 승인 취소 that only opens once something has
    been approved.
    """

    app, _, _ = coupang_app
    buttons = {button.label: button for button in app.button}
    # The Learning management block the Naver review screen offers, in full.
    for label in ("Positive Learning 설정", "이 답변이 잘못됨", "학습 제외"):
        assert label in _labels(app.expander), label
    assert "Negative Learning 저장" in buttons
    assert "학습 제외 저장" in buttons
    assert "승인" in buttons and not buttons["승인"].disabled
    # Nothing is approved yet, so cancelling is not available.
    assert "승인 취소" in buttons and buttons["승인 취소"].disabled
    cancel_reason = next(
        control for control in app.text_input
        if control.label == "승인 취소 사유"
    )
    assert cancel_reason.disabled


def test_no_staff_edit_program_or_final_answer_views(coupang_app) -> None:
    app, _, _ = coupang_app
    assert [list(control.options) for control in app.segmented_control] == [
        ["쿠팡 실제 판매자 답변"]
    ]
    areas = _labels(app.text_area)
    assert areas == ["쿠팡 실제 판매자 답변"]
    assert all(DRAFT_TEXT not in str(area.value) for area in app.text_area)


def test_the_marketplace_reply_is_still_shown_with_its_provenance(coupang_app) -> None:
    app, _, _ = coupang_app
    reply = app.text_area[0]
    assert reply.value == REPLY and reply.disabled
    text = _texts(app)
    assert "MARKETPLACE_SELLER_ANSWER" in text
    assert "NAVER_POSTED" not in text and "COUPANG_POSTED" not in text


def test_learning_context_stays_readable(coupang_app) -> None:
    app, _, _ = coupang_app
    assert "Learning 참고:" in _texts(app)


def test_inquiry_facts_and_source_answer_state_stay(coupang_app) -> None:
    text = _texts(coupang_app[0])
    assert "문의 정보" in text
    assert "쿠팡" in text and "오제앤에스" in text
    assert "쿠팡 답변" in text and "답변 완료" in text


def test_copy_copies_the_reply_and_writes_nothing(coupang_app) -> None:
    app, database, before = coupang_app
    copy = next(button for button in app.button if button.label == "복사")
    assert not copy.disabled
    copy.click()
    app.run(timeout=60)
    assert not app.exception
    # Rendering a hidden draft used to log GPT_PROGRAM_ANSWER_RENDERED.
    assert _activity_rows(database) == before


# --- Phase 2-1: an unanswered Coupang inquiry is a review workspace ------------

def test_an_unanswered_coupang_inquiry_opens_generation_and_review(database) -> None:
    app = _render(database, _coupang(database, "160852999", answered=False, content="스피커 있나요?"))
    text = _texts(app)
    buttons = {button.label: button for button in app.button}

    assert "조회전용" not in text and "답변 검토 및 승인" in text
    for label in ("GPT 새 답변 생성", "초기화", "임시 저장", "승인 취소", "승인", "복사"):
        assert label in buttons, label
    assert not buttons["GPT 새 답변 생성"].disabled
    assert not buttons["임시 저장"].disabled
    # Registration stays closed: shown, never pressable.
    assert buttons["쿠팡 답변 등록"].disabled
    assert "네이버 답변 등록" not in buttons
    assert "확정 운영 템플릿 사용" in _labels(app.checkbox)
    assert list(app.segmented_control[0].options) == ["Program Answer", "직원 수정본", "Final Answer"]
    assert "직원 수정본" in _labels(app.text_area)
    assert "네이버 실제 등록 답변" not in text


def test_a_coupang_order_or_schedule_inquiry_cannot_be_generated(database) -> None:
    app = _render(database, _coupang(database, "160853000", answered=False, content="배송 언제 오나요?"))
    text = _texts(app)
    generate = next(
        button for button in app.button
        if "생성" in str(button.label) and "답변" in str(button.label)
    )
    assert generate.disabled
    assert "일정 문의는 아직 답변 생성을 지원하지 않습니다" in text


# --- Naver keeps everything ----------------------------------------------------

def test_naver_workspace_is_unchanged(database) -> None:
    app = _render(database, _naver(database))
    text = _texts(app)
    buttons = {button.label: button for button in app.button}

    assert "답변 검토 및 승인" in text and "검토 대기" in text
    assert "조회전용" not in text and "조회 전용입니다" not in text
    assert "현재 작성 중인 초안이 있습니다" in text
    for label in ("GPT 새 답변 생성", "초기화", "임시 저장", "네이버 답변 등록",
                  "복사", "승인 취소", "승인"):
        assert label in buttons, label
    assert not buttons["GPT 새 답변 생성"].disabled
    assert not buttons["임시 저장"].disabled
    assert "확정 운영 템플릿 사용" in _labels(app.checkbox)
    assert "승인 취소 사유" in _labels(app.text_input)
    assert list(app.segmented_control[0].options) == ["Program Answer", "직원 수정본", "Final Answer"]
    assert "직원 수정본" in _labels(app.text_area)
    expanders = _labels(app.expander)
    for label in ("Positive Learning 설정", "이 답변이 잘못됨", "학습 제외"):
        assert label in expanders, label
    assert "Learning 참고:" in text


# --- disabled buttons look disabled ----------------------------------------------

def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert match, selector
    return match.group(1)


def test_the_primary_fill_is_for_enabled_buttons_only() -> None:
    enabled = _rule('.stButton > button[kind="primary"]:not(:disabled)')
    assert "linear-gradient(135deg, #5c50e7, #4336c4)" in enabled
    assert "color: #ffffff" in enabled
    assert not re.search(r'(?m)^\.stButton > button\[kind="primary"\]\s*\{', CSS)


def test_hover_lift_is_for_enabled_buttons_only() -> None:
    assert "translateY(-1px)" in _rule(".stButton > button:hover:not(:disabled)")
    assert not re.search(r"(?m)^\.stButton > button:hover\s*\{", CSS)


def test_a_disabled_button_has_its_own_muted_look() -> None:
    disabled = _rule(".stButton > button:disabled")
    assert "cursor: not-allowed" in disabled
    assert "linear-gradient" not in disabled
    assert "#ffffff" not in disabled


def test_the_scoped_disabled_styles_are_untouched() -> None:
    assert '[class*="st-key-dps_lookup_action_v6_"] [data-testid="stButton"] > button:disabled' in CSS
    assert '[class*="st-key-dashboard_pagination"] button:disabled' in CSS
