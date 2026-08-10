from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_sync_repository import NaverSyncRepository


def _app(code: str) -> AppTest:
    return AppTest.from_string(code).run(timeout=30)


def test_sync_button_is_explicit_and_refreshes_list_without_losing_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sync-ui.db"
    code = f'''
import os
os.environ["NAVER_SYNC_ENABLED"]="true"
import streamlit as st
from app import dashboard_work_items_from_database, render_dashboard_actions
from config import StoreConfig
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
import app

db=Database(r"{path}")
db.initialize()
st.session_state.setdefault("selected_inquiry_key", "keep-selected")
st.session_state.setdefault("dashboard_filter_signature", "keep-filter")

class Result:
    def to_dict(self):
        return {{
            "sync_id":"ui-sync","status":"SUCCESS",
            "requested_store_count":1,"successful_store_count":1,
            "fetched_count":3,"created_count":3,"inserted_count":3,
            "updated_count":0,"unchanged_count":0,"skipped_count":0,
            "failed_count":0,
            "requested_from":"2026-07-24T00:00:00+00:00",
            "requested_to":"2026-07-31T00:00:00+00:00",
            "completed_at":"2026-07-31T11:30:00+09:00",
            "errors":[]
        }}

class Sync:
    def __init__(self, database):
        self.database=database
    def run(self, **kwargs):
        LogRepository(self.database).record_system(
            "TEST_READ_API_CALLED", "fixture read called"
        )
        repo=InquiryRepository(self.database)
        for index in range(3):
            repo.upsert_work_item({{
                "store_code":"STORE","source_type":"PRODUCT_INQUIRY",
                "source_question_id":f"UI-Q-{{index}}",
                "title":"상품 문의","content":f"신규 문의 {{index}}"
            }})
        runs=NaverSyncRepository(self.database)
        if runs.get("ui-sync") is None:
            runs.start(
                sync_id="ui-sync",store_id="STORE",
                inquiry_type="PRODUCT_INQUIRY",
                requested_from="2026-07-24T00:00:00+00:00",
                requested_to="2026-07-31T00:00:00+00:00"
            )
            runs.finish(
                "ui-sync",status="SUCCESS",fetched_count=3,
                inserted_count=3,updated_count=0,unchanged_count=0,
                skipped_count=0,failed_count=0,duration_ms=10
            )
        return Result()

app.InquirySyncOrchestrator=Sync
render_dashboard_actions(
    db,[StoreConfig("STORE","스토어","id","secret",True)], []
)
items=dashboard_work_items_from_database(db)
st.write("LIST_COUNT", len(items))
st.write("SELECTED", st.session_state["selected_inquiry_key"])
st.write("FILTER", st.session_state["dashboard_filter_signature"])
'''
    app = _app(code)
    assert not app.exception
    assert any(
        button.label == "네이버 문의 동기화" and not button.disabled
        for button in app.button
    )
    assert InquiryRepository(Database(path)).count() == 0
    with Database(path).connection() as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM activity_logs
            WHERE event_code='TEST_READ_API_CALLED'
            """
        ).fetchone()[0] == 0

    next(
        button
        for button in app.button
        if button.label == "네이버 문의 동기화"
    ).click()
    app = app.run(timeout=30)
    assert not app.exception
    assert any("동기화 완료" in item.value for item in app.success)
    rendered = "\n".join(item.value for item in app.markdown)
    assert "LIST_COUNT `3`" in rendered
    assert "SELECTED keep-selected" in rendered
    assert "FILTER keep-filter" in rendered
    assert InquiryRepository(Database(path)).count() == 3


def test_last_sync_is_visible_after_new_apptest_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persisted-ui.db"
    database = Database(path)
    database.initialize()
    runs = NaverSyncRepository(database)
    runs.start(
        sync_id="persisted",
        store_id="STORE",
        inquiry_type="PRODUCT_INQUIRY",
        requested_from="2026-07-24T00:00:00+00:00",
        requested_to="2026-07-31T00:00:00+00:00",
    )
    runs.finish(
        "persisted",
        status="SUCCESS",
        fetched_count=4,
        inserted_count=3,
        updated_count=1,
        unchanged_count=0,
        skipped_count=0,
        failed_count=0,
        duration_ms=25,
    )
    app = _app(
        f'''
import os
os.environ["NAVER_SYNC_ENABLED"]="true"
from app import render_dashboard_actions
from config import StoreConfig
from repositories.database import Database
db=Database(r"{path}")
db.initialize()
render_dashboard_actions(
    db,[StoreConfig("STORE","스토어","id","secret",True)], []
)
'''
    )
    assert not app.exception
    assert any("동기화 완료" in item.value for item in app.success)
    values = {metric.label: metric.value for metric in app.metric}
    assert values["조회 문의"] == "4"
    assert values["신규 저장"] == "3"
    assert values["갱신"] == "1"
    assert any("조회 기간" in item.value for item in app.caption)


def test_sync_error_is_rendered_without_stopping_app(
    tmp_path: Path,
) -> None:
    path = tmp_path / "error-ui.db"
    app = _app(
        f'''
import os
os.environ["NAVER_SYNC_ENABLED"]="true"
from app import render_dashboard_actions
from config import StoreConfig
from repositories.database import Database
import app
class Sync:
    def __init__(self, database): pass
    def run(self, **kwargs):
        raise RuntimeError("sensitive technical detail")
app.InquirySyncOrchestrator=Sync
db=Database(r"{path}")
db.initialize()
render_dashboard_actions(
    db,[StoreConfig("STORE","스토어","id","secret",True)], []
)
'''
    )
    next(
        button
        for button in app.button
        if button.label == "네이버 문의 동기화"
    ).click()
    app = app.run(timeout=30)
    assert not app.exception
    assert app.error
    messages = "\n".join(item.value for item in app.error)
    assert "로그 ID" in messages
    assert "sensitive technical detail" not in messages


def test_sync_in_progress_is_rendered_as_skipped_not_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "skipped-ui.db"
    app = _app(
        f'''
import streamlit as st
from app import _render_sync_result
from repositories.database import Database
db=Database(r"{path}")
db.initialize()
st.session_state["dashboard_sync_result"]={{
    "status":"SKIPPED",
    "requested_store_count":1,
    "fetched_count":0,
    "created_count":0,
    "updated_count":0,
    "unchanged_count":0,
    "skipped_count":1,
    "failed_count":0,
    "error_code":"SYNC_IN_PROGRESS",
    "errors":[],
}}
_render_sync_result(db)
'''
    )
    assert not app.exception
    assert app.info
    assert "동일 스토어의 동기화가 진행 중" in app.info[0].value
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["건너뜀"] == "1"
    assert metrics["실패"] == "0"
    assert not app.error
