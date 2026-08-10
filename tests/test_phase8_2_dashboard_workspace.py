from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import paginate_items


def run(code: str) -> AppTest:
    return AppTest.from_string(code).run(timeout=30)


def seed_inquiry(database: Database, question_id: str = "Q-8-2") -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": question_id,
            "inquiry_type": "배송",
            "title": "배송 일정 문의",
            "content": "배송 일정을 확인해 주세요.",
            "product_name": "대형 TV",
            "customer_display": "홍길동",
            "order_id": "NORMAL-ORDER-82",
            "registered_at": "2026-07-30T10:00:00+09:00",
            "raw_json": {},
        }
    ).inquiry_id


def test_pagination_uses_supported_page_sizes_and_clamps() -> None:
    items = [{"id": index} for index in range(23)]
    page, current, total = paginate_items(items, 2, 10)
    assert [item["id"] for item in page] == list(range(10, 20))
    assert (current, total) == (2, 3)
    page, current, total = paginate_items(items, 99, 10)
    assert [item["id"] for item in page] == [20, 21, 22]
    assert (current, total) == (3, 3)


def test_workspace_python_order_matches_operations_grid(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace.db"
    database = Database(path)
    database.initialize()
    seed_inquiry(database)
    at = run(
        f'''
from app import dashboard_work_items_from_database
from repositories.database import Database
from ui.review_workspace import render_review_workspace
db=Database(r"{path}")
db.initialize()
items=dashboard_work_items_from_database(db)
render_review_workspace(items, len(items), db, page_size=10)
'''
    )
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    positions = [
        rendered.index(label)
        for label in (
            "문의 리스트",
            "문의 상세",
            "답변 검토 및 승인",
            "DPS 정보",
        )
    ]
    assert positions == sorted(positions)
    assert "진행 단계" not in rendered
    assert "desktop-operations-layout" in rendered


def test_answer_workspace_uses_one_switchable_answer_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "answer.db"
    database = Database(path)
    database.initialize()
    inquiry_id = seed_inquiry(database)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO answer_drafts (
                inquiry_id, program_status, category, reason, provider,
                original_answer, edited_answer, review_status,
                metadata_json
            ) VALUES (?, 'GENERATED', '배송', 'rule', 'fake',
                      'Program 본문', '직원 수정 본문', 'PENDING', '{}')
            """,
            (inquiry_id,),
        )
    at = run(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    )
    assert not at.exception
    assert len(at.segmented_control) == 1
    assert len(at.text_area) == 1
    assert at.segmented_control[0].options == [
        "Program Answer", "직원 수정본", "Final Answer"
    ]
    assert at.text_area[0].value == "직원 수정 본문"
    at.segmented_control[0].set_value("Program Answer")
    at.run(timeout=30)
    assert len(at.text_area) == 1
    assert at.text_area[0].value == "Program 본문"


def test_validator_is_compact_and_vertical_stage_chain_removed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "validator.db"
    database = Database(path)
    database.initialize()
    inquiry_id = seed_inquiry(database)
    at = run(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    )
    rendered = "\n".join(item.value for item in at.markdown)
    assert not at.exception
    assert "validator-status-bar" in rendered
    assert "answer-flow-arrow" not in rendered
    assert "answer-stage-label" not in rendered
    assert any(button.label == "승인" for button in at.button)
    assert "네이버 등록 잠금" in rendered


def test_kpi_cards_have_svg_icons_tones_and_real_trend_fallback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kpi.db"
    database = Database(path)
    database.initialize()
    seed_inquiry(database)
    at = run(
        f'''
from app import dashboard_work_items_from_database
from repositories.database import Database
from ui.dashboard import render_kpi_cards
db=Database(r"{path}")
db.initialize()
render_kpi_cards(dashboard_work_items_from_database(db), db)
'''
    )
    rendered = "\n".join(item.value for item in at.markdown)
    assert not at.exception
    assert rendered.count("operations-kpi-card") == 5
    assert rendered.count("<svg") >= 5
    for tone in ("blue", "green", "amber", "violet", "red"):
        assert f"operations-kpi-card {tone}" in rendered
    assert (
        "kpi-sparkline" in rendered
        or "최근 7일 추세 데이터 없음" in rendered
    )


def test_topbar_uses_current_identity_and_real_alert_count(
    tmp_path: Path,
) -> None:
    path = tmp_path / "topbar.db"
    database = Database(path)
    database.initialize()
    seed_inquiry(database)
    at = run(
        f'''
import streamlit as st
from repositories.database import Database
from ui.dashboard import render_header
st.session_state["local_identity"]={{
 "id":1,"username":"agent","display_name":"운영담당","role":"AGENT",
 "force_password_change":False,"auth_enabled":True
}}
db=Database(r"{path}")
db.initialize()
render_header([], db)
'''
    )
    rendered = "\n".join(item.value for item in at.markdown)
    assert not at.exception
    assert "operations-top-status" in rendered
    assert "운영 보호 적용" in rendered
    assert "운영담당" in rendered
    assert "AGENT" in rendered


def legacy_sidebar_groups_and_actual_status_values() -> None:
    at = run(
        '''
from ui.sidebar import render_sidebar
render_sidebar(
 ["스토어 A", "스토어 B"],
 db_status={"ok":True},
 dps_status={"agent_running":True,"login_status":"LOGGED_IN"}
)
'''
    )
    rendered = "\n".join(item.value for item in at.markdown)
    labels = {button.label for button in at.button}
    assert not at.exception
    for group in ("문의 관리", "진행 관리", "시스템 관리", "시스템 상태"):
        assert group in rendered
    assert {"◉  신규 문의", "▣  답변 초안", "▤  검토 대기"} <= labels
    assert "DPS Agent" in rendered and "정상" in rendered
    assert "네이버 API" in rendered and "설정됨 2" in rendered
    assert "Chrome 연결" in rendered


def test_pagination_controls_render_and_change_page(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pages.db"
    database = Database(path)
    database.initialize()
    for index in range(12):
        seed_inquiry(database, f"Q-{index:02d}")
    at = run(
        f'''
from app import dashboard_work_items_from_database
from repositories.database import Database
from ui.review_workspace import render_review_workspace
db=Database(r"{path}")
db.initialize()
items=dashboard_work_items_from_database(db)
render_review_workspace(items, len(items), db, page_size=10)
'''
    )
    assert not at.exception
    next_button = next(button for button in at.button if button.label == "다음")
    assert not next_button.disabled
    next_button.click()
    at.run(timeout=30)
    assert any(
        "2</b> / 2 페이지" in item.value for item in at.markdown
    )


def test_phase8_2_css_contains_desktop_and_fallback_layout() -> None:
    css = Path("ui/dashboard.css").read_text(encoding="utf-8")
    for fragment in (
        "operations-kpi-card",
        "operations-kpi-icon",
        "sidebar-system-card",
        "min-height: 390px",
        "min-height: 455px",
        "@media (max-width: 1199px)",
        "white-space: nowrap",
    ):
        assert fragment in css
