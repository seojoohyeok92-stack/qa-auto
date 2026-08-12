from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import paginate_items, pagination_group


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


def test_pagination_groups_cover_boundaries_and_last_page() -> None:
    expected_first = tuple(range(1, 11))
    for current in (1, 5, 10):
        pages, previous, next_page = pagination_group(current, 151)
        assert pages == expected_first
        assert previous == 1
        assert next_page == 11

    pages, previous, next_page = pagination_group(11, 151)
    assert pages == tuple(range(11, 21))
    assert previous == 1
    assert next_page == 21

    pages, previous, next_page = pagination_group(149, 151)
    assert pages == tuple(range(141, 151))
    assert previous == 131
    assert next_page == 151

    pages, previous, next_page = pagination_group(151, 151)
    assert pages == (151,)
    assert previous == 141
    assert next_page == 151


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
    assert any(item.label == "수정 피드백" for item in at.expander)
    assert any(item.label == "수정 사유" for item in at.selectbox)
    assert any(item.label == "상세 메모 (선택)" for item in at.text_input)
    at.segmented_control[0].set_value("Program Answer")
    at.run(timeout=30)
    assert len(at.text_area) == 1
    assert at.text_area[0].value == "Program 본문"


def test_inquiry_detail_prioritizes_large_question_body(tmp_path: Path) -> None:
    path = tmp_path / "detail.db"
    database = Database(path)
    database.initialize()
    inquiry_id = seed_inquiry(database)
    at = run(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_inquiry_detail
db=Database(r"{path}")
db.initialize()
_render_inquiry_detail(InquiryRepository(db).get({inquiry_id}), None)
'''
    )
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    assert "inquiry-detail-layout" in rendered
    assert "official-fields two" in rendered
    assert "inquiry-content-scroll" in rendered
    assert "문의 내용" in rendered


def test_inquiry_detail_renders_long_question_without_truncating_source(
    tmp_path: Path,
) -> None:
    path = tmp_path / "long-detail.db"
    database = Database(path)
    database.initialize()
    inquiry_id = seed_inquiry(database, "Q-LONG")
    long_question = "긴 문의 본문입니다. " * 120
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET content = ? WHERE id = ?",
            (long_question, inquiry_id),
        )
    at = run(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_inquiry_detail
db=Database(r"{path}")
db.initialize()
_render_inquiry_detail(InquiryRepository(db).get({inquiry_id}), None)
'''
    )
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    assert long_question in rendered


def test_inquiry_detail_preserves_newlines_wraps_tokens_and_escapes_html(
    tmp_path: Path,
) -> None:
    path = tmp_path / "safe-detail.db"
    database = Database(path)
    database.initialize()
    inquiry_id = seed_inquiry(database, "Q-SAFE")
    content = (
        "리뷰비 배송 언제 오나요?\n한달도 넘었어요...\n0105418 6373\n"
        "https://example.com/this/is/a/very/long/path/that/must/wrap\n"
        "<script>alert('xss')</script>"
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET content=? WHERE id=?",
            (content, inquiry_id),
        )
    at = run(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_inquiry_detail
db=Database(r"{path}")
db.initialize()
_render_inquiry_detail(InquiryRepository(db).get({inquiry_id}), None)
'''
    )
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    assert "리뷰비 배송 언제 오나요?\n한달도 넘었어요" in rendered
    assert "https://example.com/this/is/a/very/long/path" in rendered
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in rendered
    assert "<script>alert('xss')</script>" not in rendered


def test_general_dashboard_does_not_render_naver_post_prepare_card(
    tmp_path: Path,
) -> None:
    import inspect

    from ui.review_workspace import render_review_workspace

    source = inspect.getsource(render_review_workspace)
    assert "_render_naver_post_prepare" not in source
    path = tmp_path / "without-post-card.db"
    database = Database(path)
    database.initialize()
    seed_inquiry(database, "Q-NO-POST-CARD")
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
    rendered = "\n".join(
        item.value for item in [*at.markdown, *at.caption, *at.info]
    )
    assert "네이버 등록 준비" not in rendered


def test_workspace_css_stretches_detail_row_and_expands_question_body() -> None:
    css = Path("ui/dashboard.css").read_text(encoding="utf-8")
    assert ':has([class*="st-key-official_inquiry_list_panel"])' in css
    assert "align-items: stretch !important" in css
    assert ".inquiry-detail-layout" in css
    assert "min-height: 180px !important" in css
    assert "max-height: 400px !important" in css
    assert "max-height: none !important" in css
    assert "overflow: visible !important" in css
    assert "white-space: pre-wrap !important" in css
    assert "overflow-wrap: anywhere !important" in css
    assert "overflow-y: auto !important" in css
    assert "@media (max-width: 1199px)" in css
    assert "max-height: 360px !important" in css


def test_answer_tabs_have_readable_dark_theme_states_and_accents() -> None:
    css = Path("ui/dashboard.css").read_text(encoding="utf-8")
    scope = css[css.index("/* The tab accent communicates provenance") :]
    for accent in ("#7299ff", "#61e5c7", "#ffad66", "#a995ff"):
        assert accent in scope
    assert 'button[aria-pressed="true"]' in scope
    assert '[role="radio"][aria-checked="true"]' in scope
    assert "button:hover" in scope
    assert "button:focus-visible" in scope
    assert "button:disabled" in scope
    assert "background: #122234 !important" in scope
    assert "color: #e8eff7 !important" in scope


def test_answer_body_tokens_cover_editable_placeholder_and_read_only() -> None:
    css = Path("ui/dashboard.css").read_text(encoding="utf-8")
    root = css[css.index(":root") : css.index("* {")]
    assert "--answer-text: #f4f8fc" in root
    assert "--answer-placeholder: #a8b8c9" in root
    assert "--answer-disabled: #e7eef6" in root
    scope = css[css.index("[class*=\"st-key-official_answer_panel\"] textarea {") :]
    assert "color: var(--answer-text) !important" in scope
    assert "font-size: 16px !important" in scope
    assert "line-height: 1.7 !important" in scope
    assert "textarea::placeholder" in scope
    assert "color: var(--answer-placeholder) !important" in scope
    assert "textarea:disabled" in scope
    assert "textarea[readonly]" in scope
    assert "color: var(--answer-disabled) !important" in scope
    assert "-webkit-text-fill-color: var(--answer-disabled) !important" in scope


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
    assert at.button(key="dashboard_page_previous").disabled
    at.button(key="dashboard_page_number_2").click()
    at.run(timeout=30)
    assert any(
        "2</b> / 2 페이지" in item.value for item in at.markdown
    )


def test_grouped_pagination_direct_and_group_navigation(tmp_path: Path) -> None:
    path = tmp_path / "grouped-pages.db"
    database = Database(path)
    database.initialize()
    seed_inquiry(database)
    at = run(
        f'''
import streamlit as st
from app import dashboard_work_items_from_database
from repositories.database import Database
from ui.review_workspace import render_review_workspace
db=Database(r"{path}")
db.initialize()
st.session_state.setdefault("dashboard_page", 1)
items=dashboard_work_items_from_database(db)
render_review_workspace(
    items, 2265, db, page_size=15,
    current_page=st.session_state["dashboard_page"], total_pages=151,
)
'''
    )
    assert not at.exception
    assert [at.button(key=f"dashboard_page_number_{page}").label for page in range(1, 11)] == [
        str(page) for page in range(1, 11)
    ]
    assert at.button(key="dashboard_page_previous").disabled
    assert not at.button(key="dashboard_page_next").disabled
    assert at.button(key="dashboard_page_number_1").proto.type == "primary"

    at.button(key="dashboard_page_number_5").click().run(timeout=30)
    assert at.session_state["dashboard_page"] == 5
    assert at.button(key="dashboard_page_number_5").proto.type == "primary"

    at.button(key="dashboard_page_next").click().run(timeout=30)
    assert at.session_state["dashboard_page"] == 11
    assert at.button(key="dashboard_page_number_11").proto.type == "primary"
    assert at.button(key="dashboard_page_number_20").label == "20"

    at.button(key="dashboard_page_previous").click().run(timeout=30)
    assert at.session_state["dashboard_page"] == 1


def test_grouped_pagination_last_group_disables_next(tmp_path: Path) -> None:
    path = tmp_path / "last-page.db"
    database = Database(path)
    database.initialize()
    seed_inquiry(database)
    at = run(
        f'''
import streamlit as st
from app import dashboard_work_items_from_database
from repositories.database import Database
from ui.review_workspace import render_review_workspace
db=Database(r"{path}")
db.initialize()
st.session_state.setdefault("dashboard_page", 151)
items=dashboard_work_items_from_database(db)
render_review_workspace(
    items, 2265, db, page_size=15,
    current_page=st.session_state["dashboard_page"], total_pages=151,
)
'''
    )
    assert not at.exception
    assert at.button(key="dashboard_page_number_151").proto.type == "primary"
    assert at.button(key="dashboard_page_next").disabled
    assert not at.button(key="dashboard_page_previous").disabled
    assert not any(
        button.key and button.key.startswith("dashboard_page_number_15")
        and button.key != "dashboard_page_number_151"
        for button in at.button
    )


def test_page_number_click_drives_database_limit_and_offset(tmp_path: Path) -> None:
    path = tmp_path / "database-page-click.db"
    database = Database(path)
    database.initialize()
    for index in range(35):
        seed_inquiry(database, f"Q-{index:02d}")
    at = run(
        f'''
import streamlit as st
from app import _dashboard_work_items_from_rows
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import render_review_workspace
db=Database(r"{path}")
db.initialize()
st.session_state.setdefault("dashboard_page", 1)
rows, total, total_pages = InquiryRepository(db).dashboard_page(
    store_codes=["STORE"], source="ALL", queues=[], priorities=[],
    answer_status="ALL", delivery_only=False, search_query="",
    start_date="2026-07-30", end_date="2026-07-30", kpi_filter=None,
    page=st.session_state["dashboard_page"], page_size=10,
)
st.write("VISIBLE_FIRST", rows[0]["source_question_id"], "VISIBLE_COUNT", len(rows))
render_review_workspace(
    _dashboard_work_items_from_rows(rows), total, db, page_size=10,
    current_page=st.session_state["dashboard_page"], total_pages=total_pages,
)
'''
    )
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    assert "Q-34" in rendered
    at.button(key="dashboard_page_number_3").click().run(timeout=30)
    assert at.session_state["dashboard_page"] == 3
    rendered = "\n".join(item.value for item in at.markdown)
    assert "Q-14" in rendered
    assert "VISIBLE_COUNT `10`" in rendered


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
        "st-key-official_inquiry_list_header",
        "padding: 16px 14px 12px !important",
        "st-key-dashboard_pagination",
        "overflow-x: auto !important",
        "min-width: 700px",
    ):
        assert fragment in css
