from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.auto_post_runtime_service import AutoPostRuntimeService
from services.dashboard_operations_service import DashboardOperationsService


def run(code: str) -> AppTest:
    return AppTest.from_string(code).run(timeout=40)


def test_sidebar_defaults_to_dashboard_only_and_admin_reveals_tools() -> None:
    app = run(
        """
from ui.sidebar import render_sidebar
render_sidebar([], db_status={"ok": True}, dps_status={"agent_running": True})
"""
    )
    labels = {button.label for button in app.button}
    assert labels == {"Dashboard"}
    assert not any("UAT" in label for label in labels)
    app.toggle[0].set_value(True)
    app.run(timeout=40)
    labels = {button.label for button in app.button}
    assert {
        "Dashboard", "Learning Manager", "Migration", "Debug",
        "Scheduler", "Activity", "UAT", "Settings",
    } <= labels


def test_filter_bar_signature_matches_call_and_keeps_required_filters() -> None:
    app = run(
        """
import inspect
import streamlit as st
from ui.dashboard import render_filter_bar
st.session_state["dashboard_available_routes"]=["TEMPLATE", "ORDER_ID_REQUEST"]
st.write("PARAMETERS", list(inspect.signature(render_filter_bar).parameters))
filters = render_filter_bar(
    {"STORE": "스토어"},
    ["UNCLASSIFIED"],
    ["UNCLASSIFIED"],
)
st.write("ROUTE_VALUE", filters["route"])
"""
    )
    assert not app.exception
    assert any("ROUTE_VALUE ALL" in item.value for item in app.markdown)
    assert {item.label for item in app.text_input} >= {"문의 검색"}
    assert {item.label for item in app.multiselect} >= {"Store"}
    assert {item.label for item in app.selectbox} >= {"문의 상태", "Route"}
    route = next(item for item in app.selectbox if item.label == "Route")
    assert route.options == ["ALL", "ORDER_ID_REQUEST", "TEMPLATE"]
    assert any(button.label == "새로고침" for button in app.button)


def test_operations_snapshot_reads_today_learning_and_runtime_state(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "operations.db")
    database.initialize()
    InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "Q-1",
            "content": "문의",
            "raw_json": {},
        }
    )
    snapshot = DashboardOperationsService(database).snapshot()
    assert snapshot["today_inquiries"] == 1
    assert snapshot["pending"] == 1
    assert snapshot["auto_post_settings"]["enabled"] is False
    assert snapshot["auto_sync_state"]["status"] == "STOPPED"
    assert set(snapshot["learning"]["quality_distribution"]) <= {1, 2, 3, 4, 5}


def test_realtime_dashboard_shows_required_status_and_environment_gate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    database = Database(path)
    database.initialize()
    app = run(
        f"""
import os
os.environ["NAVER_POST_ENABLED"]="false"
os.environ["NAVER_AUTO_POST_ENABLED"]="false"
from repositories.database import Database
from ui.production_dashboard import render_realtime_operations
db=Database(r"{path}")
db.initialize()
render_realtime_operations(db)
"""
    )
    assert not app.exception
    labels = {metric.label for metric in app.metric}
    assert {
        "Auto Sync", "Auto Processing", "Auto Post", "DPS Agent",
        "DPS Keepalive", "최근 Sync", "최근 Auto Process",
        "최근 Auto Post", "직원 검토 필요",
    } <= labels
    buttons = {button.label: button for button in app.button}
    assert buttons["자동처리 시작"].disabled
    assert buttons["자동처리 중지"].disabled
    assert app.warning
    warning_text = " ".join(str(w.value) for w in app.warning)
    assert "NAVER_POST_ENABLED" in warning_text
    assert "NAVER_AUTO_POST_ENABLED" in warning_text


def test_realtime_dashboard_ready_environment_allows_start_dialog_and_stop_flow(
    tmp_path: Path, monkeypatch,
) -> None:
    path = tmp_path / "ready.db"
    database = Database(path)
    database.initialize()
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    monkeypatch.setattr(
        "services.auto_post_runtime_service.get_configured_stores",
        lambda: ["dummy"],
    )
    NaverSyncRepository(database).save_auto_settings(
        enabled=True, interval_minutes=5,
    )

    script = f"""
import os
os.environ["NAVER_POST_ENABLED"] = "true"
os.environ["NAVER_AUTO_POST_ENABLED"] = "true"
from repositories.database import Database
from ui.production_dashboard import render_realtime_operations
db = Database(r"{path}")
render_realtime_operations(db)
"""
    app = run(script)
    assert not app.exception
    buttons = {button.label: button for button in app.button}
    assert not buttons["자동처리 시작"].disabled
    assert buttons["자동처리 중지"].disabled
    assert not app.warning

    app.button(key="production_auto_processing_start").click().run(timeout=40)
    assert not app.exception
    dialog_buttons = {button.label: button for button in app.button}
    assert "자동등록 시작" in dialog_buttons

    AutoPostRuntimeService(
        database, authentication_ready=lambda: True,
    ).enable()
    app.run(timeout=40)
    assert not app.exception
    buttons = {button.label: button for button in app.button}
    assert buttons["자동처리 시작"].disabled
    assert not buttons["자동처리 중지"].disabled

    app.run(timeout=40)
    buttons = {button.label: button for button in app.button}
    assert buttons["자동처리 시작"].disabled
    assert not buttons["자동처리 중지"].disabled

    app.button(key="production_auto_processing_stop").click().run(timeout=40)
    assert not app.exception
    assert AutoPostRepository(database).settings()["enabled"] is False
    buttons = {button.label: button for button in app.button}
    assert not buttons["자동처리 시작"].disabled
    assert buttons["자동처리 중지"].disabled


def test_dashboard_bottom_sections_show_operations_and_learning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bottom.db"
    database = Database(path)
    database.initialize()
    app = run(
        f"""
from repositories.database import Database
from services.dashboard_operations_service import DashboardOperationsService
from ui.production_dashboard import render_operations_statistics, render_learning_status
db=Database(r"{path}")
db.initialize()
data=DashboardOperationsService(db).snapshot()
render_operations_statistics(data)
render_learning_status(data)
"""
    )
    labels = {metric.label for metric in app.metric}
    assert {"오늘 문의", "자동답변", "자동등록 성공", "자동등록 실패", "직원 수정", "Learning"} <= labels
    assert {
        "총 개수", "오늘 생성/승격", "오늘 답변 생성에서 참조",
        "최근 학습", "최근 수정",
    } <= labels
    assert any("품질 분포" in item.value for item in app.caption)


def test_sidebar_free_layout_is_symmetric_at_required_viewports() -> None:
    css = (Path(__file__).parents[1] / "ui" / "dashboard.css").read_text(
        encoding="utf-8"
    )
    final = css[css.index("Version 3.0 Final"):]
    assert '[data-testid="stSidebar"]' in final
    assert "display: none !important" in final
    assert "max-width: 1600px !important" in final
    assert "margin-left: auto !important" in final
    assert "margin-right: auto !important" in final
    assert "padding-left: 24px !important" in final
    assert "padding-right: 24px !important" in final
    for viewport in (1366, 1600, 1920):
        container = min(viewport, 1600)
        outer_left = (viewport - container) / 2
        outer_right = viewport - container - outer_left
        assert outer_left == outer_right
        assert container - 48 > 0


def test_workspace_prioritizes_detail_and_answer_without_progress_card() -> None:
    import inspect

    from ui.review_workspace import render_review_workspace

    source = inspect.getsource(render_review_workspace)
    css = (Path(__file__).parents[1] / "ui" / "dashboard.css").read_text(
        encoding="utf-8"
    )
    final = css[css.index("Final cascade overrides must remain last."):]
    assert "_render_progress" not in source
    assert 'height=680, key="official_detail_panel"' in source
    assert 'height=760, key="official_dps_panel"' in source
    assert 'height=760, key="official_answer_panel"' in source
    assert "min-height: 760px !important" in css
    assert "min-height: 420px !important" in css
    assert css.rstrip().endswith("}")


def test_full_dashboard_apptest_renders_collapsed_operations_and_hides_admin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "full-dashboard.db"
    Database(path).initialize()
    app = run(
        f'''
import os
os.environ["OJE_AUTOMATION_DB_PATH"] = r"{path}"
os.environ["NAVER_POST_ENABLED"] = "false"
os.environ["NAVER_AUTO_POST_ENABLED"] = "false"
os.environ["NAVER_AUTO_SYNC_ENABLED"] = "false"
import app
app.main()
'''
    )
    assert not app.exception
    expanders = {item.label: item for item in app.expander}
    assert {"오늘 운영 통계", "Learning Repository"} <= set(
        expanders
    )
    assert all(
        not expanders[label].proto.expanded
        for label in ("오늘 운영 통계", "Learning Repository")
    )
    assert any("DPS" in label for label in expanders)
    assert "관리자 상세" not in expanders
