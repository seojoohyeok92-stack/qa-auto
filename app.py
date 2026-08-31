from datetime import date, datetime
import logging
from typing import Any
import uuid

import streamlit as st

from core.time_utils import format_datetime_kst
from config import (
    NAVER_AUTO_SYNC_INTERVALS,
    NaverSyncSettings,
    get_configured_stores,
)
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.dashboard_preferences_repository import DashboardPreferencesRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.inquiry_sync_orchestrator import InquirySyncOrchestrator
from services.naver_auto_sync_scheduler import (
    ensure_auto_sync_scheduler,
)
from services.naver_auto_post_scheduler import (
    ensure_auto_post_scheduler,
    reset_auto_post_runtime_on_process_start,
)
from services.dps_agent_client import ensure_dps_session_monitor
from services.work_queue_service import (
    WorkItem,
    WorkQueueError,
    load_work_queue,
    parse_registered_at,
)
from ui.dashboard import (
    UNCLASSIFIED_FILTER_VALUE,
    apply_kpi_filter,
    load_dashboard_css,
    metadata_filter_matches,
    render_filter_bar,
    render_header,
    render_inquiry_table,
    render_kpi_cards,
)
from ui.inquiries import render_inquiries_page
from ui.activity_log_panel import render_activity_log_panel
from ui.build_info import render_build_footer
from ui.gpt_governance_panel import render_gpt_governance_panel
from ui.local_auth_panel import ensure_local_identity
from ui.session_identity import current_identity
from ui.review_workspace import paginate_items, render_review_workspace
from ui.uat_panel import render_uat_panel
from ui.auto_post_panel import render_auto_post_controls
from ui.learning_manager import render_learning_manager
from ui.gpt_copilot import render_gpt_copilot
from ui.historical_case_manager import render_historical_case_manager
from ui.learning_performance import render_learning_performance
from ui.rerun_profile import (
    begin as begin_rerun_profile,
    publish as publish_rerun_profile,
    stage as profile_stage,
)
from ui.production_dashboard import (
    dashboard_database_revision,
    render_learning_status,
    render_operations_statistics,
    render_realtime_operations,
    render_sync_status,
)


LOGGER = logging.getLogger(__name__)


def _restore_persisted_admin_mode(database: Database | None) -> bool:
    """Restore durable admin navigation after Streamlit widget cleanup."""

    identity = current_identity()
    if database is None or str(identity.get("role") or "").upper() != "ADMIN":
        return False
    username = str(identity.get("username") or "local-admin")
    enabled = bool(DashboardPreferencesRepository(database).admin_mode(username))
    if enabled:
        st.session_state["production_admin_mode"] = True
    return enabled


st.set_page_config(
    page_title="Q&A auto",
    page_icon="▷",
    layout="wide",
    initial_sidebar_state="collapsed",
)

load_dashboard_css()


@st.cache_data(ttl=300, show_spinner=False)
def load_dashboard_data() -> tuple[list[WorkItem], list[WorkQueueError]]:
    """네이버 문의와 작업 큐 데이터를 불러옵니다."""
    return load_work_queue()


def _search_text(work_item: WorkItem) -> str:
    search_fields: list[Any] = [
        work_item.get("title"),
        work_item.get("content"),
        work_item.get("product_name"),
        work_item.get("order_id"),
        work_item.get("product_order_ids"),
        work_item.get("inquiry_id"),
        work_item.get("customer_name"),
        work_item.get("customer_id"),
        work_item.get("writer_id"),
    ]

    orders = work_item.get("orders")
    if isinstance(orders, list):
        for order in orders:
            if isinstance(order, dict):
                search_fields.extend(
                    [order.get("order_id"), order.get("product_order_id")]
                )

    return " ".join(
        str(value) for value in search_fields if value not in (None, "")
    ).lower()


def _work_item_route(work_item: WorkItem) -> str:
    analysis = work_item.get("analysis")
    analysis_data = analysis if isinstance(analysis, dict) else {}
    plan = analysis_data.get("processing_plan")
    plan_data = plan if isinstance(plan, dict) else {}
    return str(
        work_item.get("selected_answer_route")
        or analysis_data.get("selected_answer_route")
        or plan_data.get("selected_answer_route")
        or plan_data.get("route")
        or work_item.get("generation_mode")
        or "UNCLASSIFIED"
    ).upper()


def filter_work_items(
    work_items: list[WorkItem],
    *,
    store_codes: list[str],
    source: str,
    queues: list[str],
    priorities: list[str],
    answer_status: str,
    delivery_only: bool,
    search_query: str,
    start_date: date,
    end_date: date,
    route: str = "ALL",
    learning_status: str = "ALL",
) -> list[WorkItem]:
    normalized_query = search_query.strip().lower()
    filtered: list[WorkItem] = []

    for item in work_items:
        registered_at = parse_registered_at(item.get("registered_at"))

        if (
            registered_at != datetime.min
            and not start_date <= registered_at.date() <= end_date
        ):
            continue
        if store_codes and item.get("store_code") not in store_codes:
            continue
        if source != "ALL" and item.get("source") != source:
            continue
        if route != "ALL" and _work_item_route(item) != route:
            continue
        statuses = {
            str(value).upper()
            for value in item.get("learning_statuses") or [
                item.get("learning_status") or "NONE"
            ]
        }
        if learning_status != "ALL" and learning_status not in statuses:
            continue
        if not metadata_filter_matches(item.get("queue"), queues):
            continue
        if not metadata_filter_matches(item.get("priority"), priorities):
            continue
        if answer_status == "UNANSWERED" and item.get("answered") is not False:
            continue
        if answer_status == "ANSWERED" and item.get("answered") is not True:
            continue

        analysis = item.get("analysis")
        analysis_data = analysis if isinstance(analysis, dict) else {}
        if delivery_only and not (
            item.get("is_delivery") or analysis_data.get("is_delivery")
        ):
            continue
        if normalized_query and normalized_query not in _search_text(item):
            continue

        filtered.append(item)

    return filtered


def _latest_first(work_items: list[WorkItem]) -> list[WorkItem]:
    return sorted(
        work_items,
        key=lambda item: parse_registered_at(item.get("registered_at")),
        reverse=True,
    )


def _show_load_errors(errors: list[WorkQueueError]) -> None:
    with st.expander(f"데이터 조회 경고 {len(errors)}건", expanded=False):
        for error in errors:
            inquiry_suffix = (
                f" · 문의 {error['inquiry_id']}" if error["inquiry_id"] else ""
            )
            st.warning(
                f"{error['store_name']} · {error['stage']}"
                f"{inquiry_suffix}: {error['message']}"
            )


def _load_work_items() -> tuple[
    list[WorkItem],
    list[WorkQueueError],
    bool,
]:
    try:
        with st.spinner("스토어 문의를 조회하고 분석하는 중입니다..."):
            work_items, errors = load_dashboard_data()
            return work_items, errors, True
    except Exception as error:
        LOGGER.exception("문의 목록 로드 실패")
        st.error(
            "문의 데이터를 불러오지 못했습니다. "
            "연결 설정과 기술 로그를 확인해 주세요."
        )
        return [], [], False


def _initialize_database() -> tuple[
    Database | None,
    dict[str, Any],
]:
    database = Database()
    try:
        database.initialize()
        return database, database.health()
    except Exception as error:
        LOGGER.exception("Q&A auto DB 초기화 실패")
        return None, {
            "ok": False,
            "status": "오류",
            "path": str(database.path.resolve()),
            "error": error.__class__.__name__,
        }


def _dashboard_work_items_from_rows(
    inquiries: list[dict[str, Any]],
) -> list[WorkItem]:
    result: list[WorkItem] = []
    for inquiry in inquiries:
        raw = (
            dict(inquiry.get("raw_json") or {})
            if isinstance(inquiry.get("raw_json"), dict)
            else {}
        )
        item: WorkItem = dict(raw)
        item.update(
            {
                "store_code": inquiry.get("store_code"),
                "store_name": raw.get("store_name")
                or inquiry.get("store_code"),
                "source": inquiry.get("source_type"),
                "source_type": inquiry.get("source_type"),
                "inquiry_id": inquiry.get("source_question_id"),
                "source_question_id": inquiry.get("source_question_id"),
                "category": inquiry.get("inquiry_type"),
                "inquiry_type": inquiry.get("inquiry_type"),
                "title": inquiry.get("title"),
                "content": inquiry.get("content"),
                "product_name": inquiry.get("product_name"),
                "customer_name": inquiry.get("customer_display"),
                "order_id": inquiry.get("order_id"),
                "product_order_id": inquiry.get("product_order_id"),
                "order_date": inquiry.get("order_date"),
                "order_status": inquiry.get("order_status"),
                "order_lookup_at": inquiry.get("order_lookup_at"),
                "registered_at": inquiry.get("registered_at"),
                "answered": (
                    bool(inquiry.get("source_answered"))
                    if inquiry.get("source_answered") is not None
                    else str(inquiry.get("answer_status")).upper()
                    == "ANSWERED"
                ),
                "workflow_status": inquiry.get("workflow_status"),
                "approval_status": inquiry.get("approval_status"),
                "post_status": inquiry.get("post_status"),
                "learning_status": inquiry.get("learning_status", "NONE"),
                "learning_statuses": inquiry.get("learning_statuses", ["NONE"]),
                "learning_labels": inquiry.get("learning_labels", ["-"]),
                "learning_tooltip": inquiry.get(
                    "learning_tooltip", "Learning 이력 없음"
                ),
            }
        )
        result.append(item)
    return result


def _attach_dashboard_routes(
    database: Database, items: list[WorkItem]
) -> list[WorkItem]:
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT i.store_code, i.source_type, i.source_question_id,
                   COALESCE(
                       pr.route,
                       json_extract(d.metadata_json, '$.selected_answer_route'),
                       json_extract(d.metadata_json, '$.processing_plan.selected_answer_route'),
                       json_extract(d.metadata_json, '$.generation_mode')
                   ) AS route
            FROM inquiries i
            LEFT JOIN answer_drafts d
              ON d.inquiry_id=i.id AND d.is_active=1
            LEFT JOIN post_reviews pr ON pr.inquiry_id=i.id
            """
        ).fetchall()
    routes = {
        (str(row["store_code"]), str(row["source_type"]), str(row["source_question_id"])):
        str(row["route"] or "UNCLASSIFIED").upper()
        for row in rows
    }
    for item in items:
        key = (
            str(item.get("store_code") or ""),
            str(item.get("source") or ""),
            str(item.get("inquiry_id") or ""),
        )
        item["selected_answer_route"] = routes.get(key, _work_item_route(item))
    return items


def _attach_dashboard_learning(
    database: Database, items: list[WorkItem]
) -> list[WorkItem]:
    """Attach Learning badges to exactly the rows handed in.

    Resolving one inquiry's Learning state runs a dozen correlated subqueries
    over the Learning tables, so doing it for the whole table cost far more
    than the twenty rows a page shows. Both queries are now scoped to the
    inquiries actually being decorated; the mapping and the fallback for a row
    with no Learning history are unchanged.
    """

    if not items:
        return items
    repository = InquiryRepository(database)
    empty = repository._empty_learning_state()
    question_ids = sorted(
        {
            str(
                item.get("inquiry_id")
                or item.get("source_question_id")
                or ""
            )
            for item in items
        }
        - {""}
    )
    if not question_ids:
        for item in items:
            item.update(empty)
        return items
    placeholders = ",".join("?" for _ in question_ids)
    with database.connection() as connection:
        identities = connection.execute(
            "SELECT id, store_code, source_type, source_question_id"
            f" FROM inquiries WHERE source_question_id IN ({placeholders})",
            question_ids,
        ).fetchall()
    states = repository.learning_states(
        [int(row["id"]) for row in identities]
    )
    by_source = {
        (str(row["store_code"]), str(row["source_type"]), str(row["source_question_id"])):
        states.get(int(row["id"]), empty)
        for row in identities
    }
    for item in items:
        key = (
            str(item.get("store_code") or ""),
            str(item.get("source") or item.get("source_type") or ""),
            str(item.get("inquiry_id") or item.get("source_question_id") or ""),
        )
        item.update(by_source.get(key, empty))
    return items


def dashboard_work_items_from_database(
    database: Database, *, limit: int | None = None
) -> list[WorkItem]:
    """외부 API 호출 없이 현재 DB 상태를 Dashboard 작업 항목으로 변환합니다."""

    repository = InquiryRepository(database)
    resolved_limit = repository.count() if limit is None else int(limit)
    return _attach_dashboard_routes(
        database,
        _dashboard_work_items_from_rows(
            repository.list(limit=max(1, resolved_limit))
        ),
    )


def _selected_database_inquiry(
    database: Database, items: list[WorkItem]
) -> dict[str, Any] | None:
    selected_key = st.session_state.get("selected_inquiry_key")
    if not selected_key:
        return None
    for item in items:
        key = "|".join(
            str(item.get(name) or "")
            for name in ("store_code", "source", "inquiry_id", "registered_at")
        )
        if key != selected_key:
            continue
        return InquiryRepository(database).get_by_source(
            str(item.get("store_code") or ""),
            str(item.get("source") or ""),
            str(item.get("inquiry_id") or ""),
        )
    return None


def _render_sync_result(database: Database | None = None) -> None:
    # The panel must describe the sync that actually ran most recently, which
    # is usually an automatic one. Reading the session copy first pinned it to
    # whichever manual sync this browser session had run: every automatic sync
    # afterwards stayed invisible, so a query window from hours earlier looked
    # like the live one and an inquiry that simply had not been collected yet
    # looked like it had been skipped. The stored run covers manual and
    # automatic syncs alike, so it is read first and the session copy is only
    # a fallback for a run that never reached the repository.
    result = None
    last_success = None
    if database is not None:
        last_success = NaverSyncRepository(database).latest(
            successful_only=True
        )
        persisted = NaverSyncRepository(database).latest()
        if isinstance(persisted, dict):
            result = {
                **persisted,
                "created_count": persisted.get("inserted_count", 0),
                "requested_store_count": len(
                    [
                        value
                        for value in str(
                            persisted.get("store_id") or ""
                        ).split(",")
                        if value
                    ]
                ),
                "errors": (
                    (persisted.get("details_json") or {}).get(
                        "errors", []
                    )
                ),
            }
    if not isinstance(result, dict):
        result = st.session_state.get("dashboard_sync_result")
    if not isinstance(result, dict):
        st.caption(
            "마지막 동기화: 아직 실행되지 않음 · 기본 조회 기간 최근 7일"
        )
        return
    status = str(result.get("status") or "SUCCESS")
    failed_count = int(result.get("failed_count") or 0)
    fetched_count = int(result.get("fetched_count") or 0)
    if status == "SKIPPED":
        st.info(
            "현재 동일 스토어의 동기화가 진행 중입니다. "
            "기존 작업이 완료되면 자동으로 최신 상태가 반영됩니다."
        )
    elif status == "FAILED":
        st.error(
            result.get("error_message")
            or "네이버 문의 동기화를 완료하지 못했습니다."
        )
    elif failed_count or status == "PARTIAL_SYNC":
        st.warning(
            "네이버 문의 동기화가 일부 실패했습니다. "
            f"조회 {fetched_count}건 · 실패 {failed_count}건"
        )
    else:
        st.success("네이버 문의 동기화 완료")
    if isinstance(last_success, dict):
        st.caption(
            "마지막 성공 동기화: "
            f"{format_datetime_kst(last_success.get('completed_at') or last_success.get('started_at'))}"
        )
    columns = st.columns(7, gap="small")
    values = (
        ("조회 스토어", result.get("requested_store_count", 0)),
        ("조회 문의", result.get("fetched_count", 0)),
        ("신규 저장", result.get("created_count", 0)),
        ("갱신", result.get("updated_count", 0)),
        ("변경 없음", result.get("unchanged_count", 0)),
        ("건너뜀", result.get("skipped_count", 0)),
        ("실패", result.get("failed_count", 0)),
    )
    for column, (label, value) in zip(columns, values):
        column.metric(label, value)
    api_latest = result.get("api_latest_registered_at")
    database_latest = result.get("database_latest_registered_at")
    if api_latest or database_latest:
        st.caption(
            "네이버 API 최신 문의 "
            f"{format_datetime_kst(api_latest, empty='없음')} · DB 최신 문의 "
            f"{format_datetime_kst(database_latest, empty='없음')}"
        )
    requested_from = result.get("requested_from")
    requested_to = result.get("requested_to")
    if requested_from or requested_to:
        # Stamp the window with the run it belongs to and with the next
        # scheduled run. Without those, a window that ends minutes ago is
        # indistinguishable from a scheduler that has stopped.
        ran_at = format_datetime_kst(
            result.get("completed_at") or result.get("started_at"),
            empty="시각 미상",
        )
        line = (
            f"최근 실행 {ran_at} · 조회 기간: "
            f"{format_datetime_kst(requested_from)} ~ "
            f"{format_datetime_kst(requested_to)}"
        )
        if database is not None:
            state = NaverSyncRepository(database).auto_state()
            if str(state.get("status") or "").upper() != "STOPPED":
                line += (
                    " · 다음 자동 동기화 "
                    + format_datetime_kst(
                        state.get("next_run_at"), empty="예정 없음"
                    )
                )
        st.caption(line)
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        with st.expander("동기화 오류 단계 확인", expanded=True):
            for error in errors:
                if not isinstance(error, dict):
                    continue
                st.error(
                    "{store} · {stage} · {source}: {message}".format(
                        store=error.get("store_name")
                        or error.get("store_code")
                        or "스토어",
                        stage=error.get("stage") or "조회",
                        source=error.get("source") or "공통",
                        message=error.get("message") or "오류",
                    )
                )
            st.caption(
                "상세 단계 로그: logs/naver_inquiry_sync.log "
                f"· correlation ID {result.get('correlation_id') or '-'}"
            )


def _render_auto_sync_controls(
    database: Database,
    *,
    sync_enabled: bool,
) -> bool:
    runs = NaverSyncRepository(database)
    settings = runs.auto_settings()
    state = runs.auto_state()
    active_locks = runs.active_locks()
    running = bool(active_locks) or state.get("status") == "RUNNING"
    with st.expander("네이버 문의 자동 동기화", expanded=False):
        columns = st.columns([1.5, 1.6, 1.2], gap="small")
        enabled = columns[0].checkbox(
            "자동 동기화 사용",
            value=bool(settings.get("enabled")),
            key="naver_auto_sync_enabled_widget",
            disabled=not sync_enabled,
        )
        interval = columns[1].selectbox(
            "동기화 간격",
            options=list(NAVER_AUTO_SYNC_INTERVALS),
            index=list(NAVER_AUTO_SYNC_INTERVALS).index(
                int(settings.get("interval_minutes") or 10)
            ),
            format_func=lambda value: f"{value}분",
            key="naver_auto_sync_interval_widget",
        )
        save = columns[2].button(
            "자동 설정 저장",
            key="naver_auto_sync_save",
            width="stretch",
            disabled=not sync_enabled,
        )
        if save:
            runs.save_auto_settings(
                enabled=enabled,
                interval_minutes=int(interval),
            )
            ensure_auto_sync_scheduler(database)
            st.session_state["naver_auto_sync_saved"] = True
            st.rerun()
        if st.session_state.pop("naver_auto_sync_saved", False):
            st.success("자동 동기화 설정을 저장했습니다.")
        status_text = (
            "실행 중"
            if running
            else "ON"
            if settings.get("enabled")
            else "OFF"
        )
        metrics = st.columns(6, gap="small")
        values = (
            ("상태", status_text),
            ("간격", f"{settings.get('interval_minutes', 10)}분"),
            ("최근 조회", state.get("fetched_count") or 0),
            ("신규", state.get("inserted_count") or 0),
            ("갱신", state.get("updated_count") or 0),
            ("오류", state.get("failed_count") or 0),
        )
        for column, (label, value) in zip(metrics, values):
            column.metric(label, value)
        st.caption(
            "마지막 실행 "
            f"{format_datetime_kst(state.get('last_completed_at'), empty='없음')} · "
            "마지막 성공 "
            f"{format_datetime_kst(state.get('last_success_at'), empty='없음')} · "
            "다음 예정 "
            f"{format_datetime_kst(state.get('next_run_at'), empty='없음')}"
        )
        if state.get("error_code"):
            st.warning(
                "최근 자동 동기화 오류: "
                f"{state.get('error_code')} · "
                f"{state.get('error_message') or '상세 로그를 확인해 주세요.'}"
            )
        if running:
            st.info("이미 동기화가 진행 중입니다.")
    return running


def render_dashboard_actions(
    database: Database | None,
    configured_stores: list[Any],
    items: list[WorkItem],
) -> dict[str, Any] | None:
    """외부 동기화와 로컬 화면 갱신을 명확히 분리합니다."""

    sync_settings = NaverSyncSettings.from_environment()
    operations = (
        render_realtime_operations(
            database,
            dashboard_database_revision(database),
        )
        if database is not None
        else None
    )
    persisted_running = bool(
        database is not None
        and (
            NaverSyncRepository(database).active_locks()
            or str((operations or {}).get("auto_sync_state", {}).get("status") or "").upper()
            == "RUNNING"
        )
    )
    if database is not None and st.session_state.get("production_admin_mode", False):
        with st.expander("관리자 Scheduler · 상세 설정", expanded=False):
            persisted_running = _render_auto_sync_controls(
                database,
                sync_enabled=sync_settings.enabled,
            ) or persisted_running
            render_auto_post_controls(database)
    sync_running = bool(
        st.session_state.get("dashboard_sync_running")
        or persisted_running
    )
    state = (operations or {}).get("auto_sync_state") or {}
    sync_summary = (
        "네이버 문의 동기화 · "
        f"최근 {format_datetime_kst(state.get('last_completed_at'), empty='없음')} · "
        f"조회 {state.get('fetched_count') or 0}건"
    )
    with st.expander(sync_summary, expanded=False):
        columns = st.columns([1.45, 1.1, 7.45], gap="small")
        sync_requested = columns[0].button(
            "네이버 문의 동기화",
            key="dashboard_naver_sync",
            type="primary",
            width="stretch",
            disabled=(
                sync_running
                or database is None
                or not configured_stores
                or not sync_settings.enabled
            ),
        )
        refresh_requested = columns[1].button(
            "화면 새로고침",
            key="dashboard_screen_refresh",
            width="stretch",
        )
        columns[2].markdown(
            '<div class="dashboard-action-hint">'
            "<b>API 동기화</b>는 새 문의를 가져오며 "
            "<b>화면 새로고침</b>은 현재 DB만 다시 표시합니다.</div>",
            unsafe_allow_html=True,
        )
        if persisted_running:
            st.info("이미 동기화가 진행 중입니다.")
        if not sync_settings.enabled:
            st.caption(
                "수동 동기화는 기본적으로 잠겨 있습니다. "
                "NAVER_SYNC_ENABLED=true 설정 후 사용할 수 있습니다."
            )

        if refresh_requested:
            st.toast("현재 DB 내용을 다시 표시합니다.")
            st.rerun()
        if sync_requested and database is not None:
            st.session_state["dashboard_sync_running"] = True
            try:
                with st.spinner("연결된 스토어의 네이버 문의를 동기화하고 있습니다..."):
                    result = InquirySyncOrchestrator(database).run(
                        stores=configured_stores,
                        sync_type="MANUAL",
                    )
                st.session_state["dashboard_sync_result"] = result.to_dict()
                load_dashboard_data.clear()
            except Exception as error:
                log_id = str(uuid.uuid4())
                LogRepository(database).record_system(
                    "NAVER_INQUIRY_SYNC_UI_FAILED",
                    "Dashboard 문의 동기화에 실패했습니다.",
                    level="ERROR",
                    details={
                        "correlation_id": log_id,
                        "error_type": error.__class__.__name__,
                    },
                )
                st.session_state["dashboard_sync_error"] = log_id
            finally:
                st.session_state["dashboard_sync_running"] = False
            st.rerun()

        error_id = st.session_state.pop("dashboard_sync_error", None)
        if error_id:
            st.error(
                "네이버 문의 동기화를 완료하지 못했습니다. "
                f"관리자 진단에서 로그 ID {error_id}를 확인해 주세요."
            )
        _render_sync_result(database)
    return operations


def _render_admin_details(database: Database, operations: dict[str, Any]) -> None:
    if not st.session_state.get("production_admin_mode", False):
        return
    with st.expander("관리자 상세", expanded=False):
        st.caption("Scheduler · Activity Log · Migration · Debug · UAT · Settings")
        columns = st.columns(8, gap="small")
        destinations = (
            ("Learning Manager", "learning"),
            ("Historical Cases", "historical"),
            ("Scheduler", "dashboard"),
            ("Activity Log", "activity"),
            ("Migration", "uat"),
            ("Debug", "uat"),
            ("UAT", "uat"),
            ("Settings", "settings"),
        )
        for index, (label, page) in enumerate(destinations):
            if columns[index].button(
                label, key=f"admin_detail_{index}", width="stretch"
            ):
                st.session_state["current_page"] = page
                st.rerun()
        state = operations.get("auto_post_state") or {}
        st.caption(
            f"Scheduler {state.get('status') or 'STOPPED'} · "
            f"Migration v{max(database.migration_versions(), default=0)}"
        )


def _stored_inquiry_count(database: Database | None) -> int | None:
    if database is None:
        return None
    try:
        return InquiryRepository(database).count()
    except Exception:
        LOGGER.exception("저장된 문의 수 조회 실패")
        return None


def _render_database_diagnostics(
    db_status: dict[str, Any],
    inquiry_count: int | None,
    sync_result: dict[str, int] | None,
) -> None:
    last_result = (
        sync_result
        or st.session_state.get("dashboard_sync_result")
    )
    with st.sidebar:
        with st.expander("관리자 진단", expanded=False):
            st.caption("프로젝트명")
            st.write("Q&A auto")
            st.caption("DB 경로")
            st.code(str(db_status.get("path") or "확인 불가"))
            st.caption("DB 상태")
            if db_status.get("ok"):
                st.success(
                    f"{db_status.get('status', '정상')} · "
                    f"{db_status.get('journal_mode', 'WAL')}"
                )
            else:
                st.error(
                    "DB를 사용할 수 없습니다. 기존 문의 화면은 계속 동작합니다."
                )
            st.metric(
                "저장된 문의",
                inquiry_count if inquiry_count is not None else "확인 불가",
            )
            st.caption("최근 동기화")
            if isinstance(last_result, dict):
                st.write(
                    "신규 {created} · 갱신 {updated} · "
                    "변경 없음 {unchanged} · 오류 {failed}".format(
                        created=last_result.get(
                            "created_count", last_result.get("new", 0)
                        ),
                        updated=last_result.get("updated_count", 0),
                        unchanged=last_result.get("unchanged_count", 0),
                        failed=last_result.get(
                            "failed_count", last_result.get("failed", 0)
                        ),
                    )
                )
            else:
                st.write("아직 실행되지 않음")


def render_preparation_page(title: str, description: str) -> None:
    st.markdown(
        '<div class="preparation-page">'
        f'<h1>{title}</h1><p>{description}</p>'
        '<div class="preparation-card"><strong>화면 준비 중</strong>'
        '<span>이 메뉴는 다음 개발 단계에서 실제 기능을 연결합니다.</span>'
        '</div></div>',
        unsafe_allow_html=True,
    )


def render_dashboard_page(
    configured_stores: list[Any],
    work_items: list[WorkItem],
    load_errors: list[WorkQueueError],
    database: Database | None,
) -> None:
    with profile_stage("dashboard_states"):
        dashboard_states = (
            ApprovalRepository(database).dashboard_states()
            if database is not None
            else []
        )
    with profile_stage("header"):
        start_date, end_date = render_header(
            work_items, database, dashboard_states
        )
    with profile_stage("dashboard_actions"):
        operations = render_dashboard_actions(
            database, configured_stores, work_items
        )
        if (
            operations is not None
            and st.session_state.get("production_admin_mode", False)
        ):
            render_operations_statistics(operations)
            render_learning_status(operations)
            _render_admin_details(database, operations)
    with profile_stage("learning_performance"):
        if database is not None:
            render_learning_performance(database)

    if load_errors:
        _show_load_errors(load_errors)
    if not configured_stores:
        st.warning(
            "활성화되고 인증정보가 등록된 스토어가 없습니다. "
            ".env 설정을 확인해 주세요."
        )

    kpi_slot = st.container()

    available_stores = {
        str(item.get("store_code")): str(item.get("store_name"))
        for item in work_items
        if item.get("store_code")
    }
    if not available_stores:
        available_stores = {store.code: store.name for store in configured_stores}

    available_queues = sorted(
        {str(item.get("queue")) for item in work_items if item.get("queue")}
    )
    if UNCLASSIFIED_FILTER_VALUE not in available_queues:
        available_queues.append(UNCLASSIFIED_FILTER_VALUE)
    available_priorities = [
        code
        for code in ("HIGH", "MEDIUM", "NORMAL")
        if any(item.get("priority") == code for item in work_items)
    ]
    if UNCLASSIFIED_FILTER_VALUE not in available_priorities:
        available_priorities.append(UNCLASSIFIED_FILTER_VALUE)

    available_routes = sorted({_work_item_route(item) for item in work_items})
    st.session_state["dashboard_available_routes"] = available_routes
    with profile_stage("filter_bar"):
        filters = render_filter_bar(
            available_stores,
            available_queues,
            available_priorities,
        )
    learning_filter = str(filters.get("learning_status") or "ALL")
    with profile_stage("attach_learning"):
        # Only the Learning filter needs every candidate row's state; when it
        # is off, the badges are read solely by the rows that get rendered, so
        # they are decorated further down once the page is known.
        if database is not None and learning_filter != "ALL":
            work_items = _attach_dashboard_learning(database, work_items)
    # Streamlit can keep an already-imported ui.dashboard module alive while
    # reloading this file.  During that transition an older filter result has
    # no route key, so treat it as the unfiltered route until the next rerun.
    route_filter = str(filters.get("route") or "ALL")
    with profile_stage("filter_work_items"):
        scoped_items = filter_work_items(
            work_items,
            store_codes=filters["stores"],
            source=filters["source"],
            queues=filters["queues"],
            priorities=filters["priorities"],
            answer_status=filters["answer_status"],
            delivery_only=filters["delivery_only"],
            search_query=filters["search_query"],
            start_date=start_date,
            end_date=end_date,
            route=route_filter,
            learning_status=learning_filter,
        )
    with profile_stage("dashboard_kpi_counts"):
        kpi_counts = (
            InquiryRepository(database).dashboard_kpi_counts(
                store_codes=filters["stores"],
                source=filters["source"],
                queues=filters["queues"],
                priorities=filters["priorities"],
                answer_status=filters["answer_status"],
                delivery_only=filters["delivery_only"],
                search_query=filters["search_query"],
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                learning_status=learning_filter,
            )
            if database is not None
            else {}
        )
    with profile_stage("kpi_cards"), kpi_slot:
        kpi_filter = render_kpi_cards(
            scoped_items,
            database,
            states=dashboard_states,
            value_counts=kpi_counts,
        )
    filter_signature = repr(
        (
            filters["stores"],
            filters["source"],
            filters["queues"],
            filters["priorities"],
            filters["answer_status"],
            filters["delivery_only"],
            filters["search_query"],
            route_filter,
            learning_filter,
            start_date,
            end_date,
            kpi_filter,
            filters["display_limit"],
        )
    )
    if st.session_state.get("dashboard_filter_signature") != filter_signature:
        st.session_state["dashboard_filter_signature"] = filter_signature
        st.session_state["dashboard_page"] = 1

    if database is None:
        st.info("DB 연결이 없어 문의 목록을 조회할 수 없습니다.")
        return
    if route_filter != "ALL":
        route_items = apply_kpi_filter(
            scoped_items,
            database,
            kpi_filter,
            states=dashboard_states,
        )
        page_items, current_page, total_pages = paginate_items(
            route_items,
            int(st.session_state.get("dashboard_page", 1)),
            int(filters["display_limit"]),
        )
        total_count = len(route_items)
    else:
        with profile_stage("dashboard_page_query"):
            page_rows, total_count, total_pages = InquiryRepository(
                database
            ).dashboard_page(
                store_codes=filters["stores"],
                source=filters["source"],
                queues=filters["queues"],
                priorities=filters["priorities"],
                answer_status=filters["answer_status"],
                delivery_only=filters["delivery_only"],
                search_query=filters["search_query"],
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                kpi_filter=kpi_filter,
                page=int(st.session_state.get("dashboard_page", 1)),
                page_size=int(filters["display_limit"]),
                learning_status=learning_filter,
            )
        current_page = min(
            max(1, int(st.session_state.get("dashboard_page", 1))),
            total_pages,
        )
        with profile_stage("page_items_build"):
            page_items = _attach_dashboard_routes(
                database, _dashboard_work_items_from_rows(page_rows)
            )
    st.session_state["dashboard_page"] = current_page

    if not page_items:
        st.info("현재 조건에 맞는 문의가 없습니다.")
        render_gpt_copilot(database)
        return

    with profile_stage("attach_learning_page"):
        page_items = _attach_dashboard_learning(database, page_items)

    with profile_stage("review_workspace_render"):
        render_review_workspace(
            page_items,
            total_count,
            database,
            page_size=int(filters["display_limit"]),
            current_page=current_page,
            total_pages=total_pages,
        )

    with profile_stage("gpt_copilot"):
        render_gpt_copilot(database)

    if not st.session_state.get("selected_inquiry_key"):
        st.caption(
            "목록 오른쪽의 ‘보기’ 버튼을 누르면 선택한 문의 행 바로 아래에서 "
            "상세 내용을 확인할 수 있습니다."
        )


def main() -> None:
    begin_rerun_profile()
    with profile_stage("app_start"):
        database, db_status = _initialize_database()
    if database is not None:
        ensure_local_identity(database)
        # Must run before ensure_auto_post_scheduler, and only ever resets
        # anything on this process's very first pass (see docstring) -- a
        # same-process Streamlit rerun or an operator's own enable() call
        # afterward is never affected.
        reset_auto_post_runtime_on_process_start(database)
        ensure_auto_sync_scheduler(database)
        ensure_auto_post_scheduler(database)
        ensure_dps_session_monitor()
    with profile_stage("repository_init"):
        configured_stores = get_configured_stores()
    current_page = str(st.session_state.get("current_page") or "dashboard")
    persisted_admin_mode = _restore_persisted_admin_mode(database)
    refresh_requested = False

    if (
        current_page != "dashboard"
        and not (
            st.session_state.get("production_admin_mode", False)
            or persisted_admin_mode
        )
    ):
        st.session_state["current_page"] = "dashboard"
        current_page = "dashboard"

    if refresh_requested:
        st.toast("현재 DB 내용을 다시 표시합니다.")
        st.rerun()

    if current_page in {"dashboard", "inquiries"}:
        if database is not None:
            with profile_stage("work_queue_load"):
                work_items = dashboard_work_items_from_database(database)
            load_errors, load_succeeded = [], True
        else:
            work_items, load_errors, load_succeeded = [], [], False
    else:
        work_items, load_errors, load_succeeded = [], [], False

    if not db_status.get("ok"):
        st.warning(
            "작업 상태 저장소를 초기화하지 못했습니다. "
            "기존 문의 조회 화면은 계속 사용할 수 있습니다."
        )

    if current_page != "dashboard" and st.session_state.get(
        "production_admin_mode", False
    ):
        if st.button("← Dashboard", key="admin_return_dashboard"):
            st.session_state["current_page"] = "dashboard"
            st.rerun()

    if current_page == "dashboard":
        render_dashboard_page(
            configured_stores, work_items, load_errors, database
        )
        render_build_footer()
        publish_rerun_profile()
        return
    if current_page == "inquiries":
        if load_errors:
            _show_load_errors(load_errors)
        render_inquiries_page(work_items, configured_stores)
        return
    if current_page == "progress":
        render_preparation_page("진행 관리", "문의별 9단계 처리 흐름을 관리합니다.")
        return
    if current_page == "dps":
        render_preparation_page("DPS 관리", "삼성 DPS 설치 일정과 조회 이력을 관리합니다.")
        return
    if current_page == "settings":
        render_gpt_governance_panel(database)
        return
    if current_page == "uat":
        render_uat_panel(database)
        return
    if current_page == "activity":
        render_activity_log_panel(database)
        return
    if current_page == "learning":
        render_learning_manager(database)
        return
    if current_page == "historical":
        render_historical_case_manager(database)
        return

    st.session_state["current_page"] = "dashboard"
    st.rerun()


if __name__ == "__main__":
    main()
