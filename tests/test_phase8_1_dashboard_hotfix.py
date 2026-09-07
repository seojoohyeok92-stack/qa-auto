from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app import dashboard_work_items_from_database
from config import StoreConfig
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from services.inquiry_sync_orchestrator import InquirySyncOrchestrator
from services.uat_order_service import UatOrderService
from ui.dashboard import _state_matches_filter
from ui.review_workspace import inquiry_list_summary, truncate_single_line
from workflow.models import StepCode


@pytest.fixture(autouse=True)
def manual_sync_unlocked(monkeypatch):
    """Manual sync is locked unless the deployment turns it on.

    ``config.NaverConfig`` reads ``NAVER_SYNC_ENABLED`` with ``default=False``,
    and the dashboard renders a "잠겨 있습니다" caption instead of running a
    sync when it is off. Two tests here click the sync button and assert on
    what it produced, so they were only ever green on a machine whose shell
    happened to export the flag -- the repository's own ``.env`` sets it, but
    that value does not reach this process, and the same two tests failed on a
    clean checkout for the same reason.

    The lock is deliberate production behaviour and is left alone. The tests
    that depend on it now say so.
    """

    monkeypatch.setenv("NAVER_SYNC_ENABLED", "true")


def run(code: str) -> AppTest:
    return AppTest.from_string(code).run(timeout=20)


def store(code: str = "STORE") -> StoreConfig:
    return StoreConfig(code, code, "client", "secret", True)


def work_item(question_id: str = "Q-1") -> dict:
    return {
        "store_code": "STORE",
        "source": "CUSTOMER_INQUIRY",
        "source_type": "CUSTOMER_INQUIRY",
        "inquiry_id": question_id,
        "source_question_id": question_id,
        "category": "배송",
        "title": "배송 문의",
        "content": "배송 일정을 확인해 주세요.",
        "product_name": "TV",
        "registered_at": "2026-07-30T10:00:00+09:00",
        "answered": False,
    }


def test_single_line_truncation_and_stored_summary_precedence() -> None:
    assert truncate_single_line("첫 줄\n둘째 줄", 20) == "첫 줄 둘째 줄"
    assert truncate_single_line("가" * 50, 10) == "가" * 9 + "…"
    assert inquiry_list_summary(
        {"summary": "저장된 요약", "content": "노출하면 안 되는 전체 원문"}
    ) == "저장된 요약"


def test_dashboard_snapshot_reads_database_without_api(tmp_path: Path) -> None:
    database = Database(tmp_path / "snapshot.db")
    database.initialize()
    InquiryRepository(database).upsert_work_item(work_item())
    items = dashboard_work_items_from_database(database)
    assert len(items) == 1
    assert items[0]["content"] == "배송 일정을 확인해 주세요."


def test_shared_sync_orchestrator_saves_and_logs(tmp_path: Path) -> None:
    database = Database(tmp_path / "sync.db")
    database.initialize()
    result = InquirySyncOrchestrator(
        database, loader=lambda **kwargs: ([work_item()], [])
    ).run(stores=[store()])
    assert result.requested_store_count == 1
    assert result.fetched_count == 1
    assert result.created_count == 1
    assert result.failed_count == 0
    with database.connection() as connection:
        events = {
            row[0]
            for row in connection.execute(
                "SELECT event_code FROM activity_logs"
            ).fetchall()
        }
    assert "NAVER_INQUIRY_SYNC_STARTED" in events
    assert "NAVER_INQUIRY_SYNC_SUCCEEDED" in events


def test_dashboard_action_bar_separates_sync_refresh_and_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "actions.db"
    at = run(
        f'''
from app import render_dashboard_actions
from config import StoreConfig
from repositories.database import Database
db=Database(r"{path}")
db.initialize()
render_dashboard_actions(
    db, [StoreConfig("STORE","스토어","id","secret",True)], []
)
'''
    )
    assert not at.exception
    labels = {button.label: button for button in at.button}
    assert "네이버 문의 동기화" in labels
    assert "화면 새로고침" in labels
    assert "선택 문의 주문 조회" not in labels
    assert "관리자 진단" not in labels
    assert any("API 동기화" in item.value for item in at.markdown)


def test_sync_button_is_disabled_while_sync_is_running(tmp_path: Path) -> None:
    path = tmp_path / "running.db"
    at = run(
        f'''
import streamlit as st
from app import render_dashboard_actions
from config import StoreConfig
from repositories.database import Database
st.session_state["dashboard_sync_running"]=True
db=Database(r"{path}")
db.initialize()
render_dashboard_actions(
    db, [StoreConfig("STORE","스토어","id","secret",True)], []
)
'''
    )
    sync = next(
        button for button in at.button
        if button.label == "네이버 문의 동기화"
    )
    assert sync.disabled


def test_sync_completion_is_presented_after_rerun(tmp_path: Path) -> None:
    path = tmp_path / "sync-action.db"
    at = run(
        f'''
from app import render_dashboard_actions
from config import StoreConfig
from repositories.database import Database
class Result:
    def to_dict(self):
        return {{
            "requested_store_count":2,"fetched_count":12,
            "created_count":3,"updated_count":9,"failed_count":0,
            "completed_at":"2026-07-30T10:05:00+09:00"
        }}
class Sync:
    def __init__(self, database): pass
    def run(self, **kwargs): return Result()
import app
app.InquirySyncOrchestrator=Sync
db=Database(r"{path}")
db.initialize()
render_dashboard_actions(
    db, [StoreConfig("STORE","스토어","id","secret",True)], []
)
'''
    )
    next(
        button for button in at.button
        if button.label == "네이버 문의 동기화"
    ).click()
    at.run(timeout=20)
    assert not at.exception
    assert at.success
    assert any("동기화 완료" in item.value for item in at.success)
    assert {metric.value for metric in at.metric} >= {"12", "3", "9", "0"}


def test_sync_failure_shows_safe_log_id_without_traceback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sync-failure.db"
    at = run(
        f'''
from app import render_dashboard_actions
from config import StoreConfig
from repositories.database import Database
class Sync:
    def __init__(self, database): pass
    def run(self, **kwargs): raise RuntimeError("private failure detail")
import app
app.InquirySyncOrchestrator=Sync
db=Database(r"{path}")
db.initialize()
render_dashboard_actions(
    db, [StoreConfig("STORE","스토어","id","secret",True)], []
)
'''
    )
    next(
        button for button in at.button
        if button.label == "네이버 문의 동기화"
    ).click()
    at.run(timeout=20)
    assert not at.exception
    assert at.error
    message = "\n".join(item.value for item in at.error)
    assert "로그 ID" in message
    assert "private failure detail" not in message


def test_list_does_not_render_full_question_or_existing_answer() -> None:
    full_question = "목록에 절대 전부 나오면 안 되는 문의 원문 " + "질문" * 40
    existing_answer = "목록에 절대 나오면 안 되는 기존 답변 고유문구"
    at = run(
        f'''
from ui.review_workspace import _render_list
_render_list([{{
 "store_code":"S","store_name":"스토어","source":"CUSTOMER_INQUIRY",
 "inquiry_id":"Q","registered_at":"2026-07-30T10:00:00+09:00",
 "content":{full_question!r},"summary":"배송 일정 확인 요청",
 "existing_answer":{existing_answer!r},"product_name":"상품","order_id":"O"
}}], 1)
'''
    )
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    assert full_question not in rendered
    assert existing_answer not in rendered
    assert "배송 일정 확인 요청" in rendered


def test_list_keeps_valid_selection_and_resets_only_missing_selection() -> None:
    at = run(
        '''
import streamlit as st
from ui.review_workspace import _item_key, _render_list
items=[
 {"store_code":"S","source":"CUSTOMER_INQUIRY","inquiry_id":"Q1",
  "registered_at":"2026-07-30T10:00:00+09:00","content":"첫 문의"},
 {"store_code":"S","source":"CUSTOMER_INQUIRY","inquiry_id":"Q2",
  "registered_at":"2026-07-30T11:00:00+09:00","content":"둘째 문의"}
]
st.session_state["selected_inquiry_key"]=_item_key(items[1])
selected=_render_list(items, 2)
st.write(selected["inquiry_id"])
'''
    )
    assert not at.exception
    assert any(item.value == "Q2" for item in at.markdown)


def test_detail_shows_full_question_and_existing_answer_only_there() -> None:
    at = run(
        '''
from ui.review_workspace import _render_inquiry_detail
_render_inquiry_detail({
 "source_question_id":"Q-1","registered_at":"2026-07-30T10:00:00+09:00",
 "inquiry_type":"배송","order_id":"O-1","product_name":"TV",
 "customer_display":"홍길동","store_code":"STORE",
 "content":"상세 전용 전체 문의 원문",
 "raw_json":{"existing_answer":"상세 전용 기존 답변"}
})
'''
    )
    assert not at.exception
    rendered = "\n".join(item.value for item in at.markdown)
    assert "상세 전용 전체 문의 원문" in rendered
    assert "상세 전용 기존 답변" in rendered
    assert "홍길동" not in rendered


def test_dps_without_normal_order_id_is_disabled_and_explained(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dps.db"
    database = Database(path)
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            **work_item(),
            "order_id": None,
            "product_order_id": "PRODUCT-ORDER-ONLY",
        }
    ).inquiry_id
    at = run(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_dps
db=Database(r"{path}")
db.initialize()
_render_dps(db, InquiryRepository(db).get({inquiry_id}))
'''
    )
    assert not at.exception
    assert all(button.disabled for button in at.button)
    captions = "\n".join(item.value for item in at.caption)
    assert "일반 네이버 주문번호가 필요합니다" in captions


def test_progress_always_marks_naver_post_as_locked(tmp_path: Path) -> None:
    path = tmp_path / "progress.db"
    database = Database(path)
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        work_item()
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    at = run(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_progress
db=Database(r"{path}")
db.initialize()
_render_progress(db, InquiryRepository(db).get({inquiry_id}))
'''
    )
    assert not at.exception
    assert any(button.label == "네이버 등록" for button in at.button)
    assert any(item.value == "잠금" for item in at.caption)


def test_order_lookup_updates_common_workflow_step(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "order.db"
    database = Database(path)
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {**work_item(), "order_id": "NORMAL-ORDER-1"}
    ).inquiry_id
    monkeypatch.setattr(
        "services.uat_order_service.get_store_config",
        lambda code: store(),
    )
    service = UatOrderService(
        database,
        token_provider=lambda **kwargs: "masked-token",
        lookup=lambda *args, **kwargs: {
            "success": True,
            "lookup_number": "NORMAL-ORDER-1",
            "lookup_type": "ORDER_ID",
            "orders": [{"order_id": "NORMAL-ORDER-1"}],
            "error_code": None,
            "error_message": None,
            "cached": False,
            "queried_at": "2026-07-30T10:00:00+09:00",
        },
    )
    assert service.lookup_for_inquiry(inquiry_id)["success"]
    step = WorkflowRepository(database).get_step(
        inquiry_id, StepCode.NAVER_ORDER_LOOKUP
    )
    assert step["step_status"] == "COMPLETED"


def test_approved_kpi_is_not_naver_posted() -> None:
    approved = {
        "approval_status": "APPROVED",
        "post_status": "NOT_POSTED",
    }
    assert _state_matches_filter(approved, "APPROVED")
    assert not _state_matches_filter(
        {"approval_status": "PENDING", "post_status": "POSTED"},
        "APPROVED",
    )


def test_dashboard_css_has_fixed_scroll_and_ellipsis() -> None:
    css = Path("ui/dashboard.css").read_text(encoding="utf-8")
    assert "height: 590px" in css
    assert "overflow-y: auto" in css
    assert "position: sticky" in css
    assert "white-space: nowrap" in css
    assert "text-overflow: ellipsis" in css
