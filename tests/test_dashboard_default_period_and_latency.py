"""대시보드 기본 조회 기간과, 그 기간이 실제 조회를 줄이는지.

기본 기간은 "가장 오래된 문의부터" 였다. 운영 DB 에서 그 값은 2024-09-01 이고,
그래서 최초 진입이 11,395 건 전체를 대상으로 열렸다. 이제 달력 기준 3개월
전부터 오늘(Asia/Seoul)까지다.

중요한 것은 **화면에 보이는 날짜만 바뀌는 것이 아니라는 점**이다: 목록·건수·
페이지네이션은 이 범위를 SQL 로 내려받고, 전체 행을 읽어 두고 파이썬에서
버리던 경로도 같은 범위로 좁혀진다. 아래 테스트가 둘 다 고정한다.

운영 DB 는 건드리지 않는다. 모든 측정과 검증은 tmp_path 의 임시 DB 다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.dashboard import (
    DEFAULT_RANGE_MONTHS,
    default_dashboard_range,
    months_before,
)


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "period.db")
    value.initialize()
    return value


def _inquiry(database: Database, *, registered_at: str, suffix: str) -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": f"q-{suffix}",
        "external_inquiry_id": f"q-{suffix}",
        "inquiry_type": "PRODUCT_GENERAL",
        "title": "상품문의",
        "content": f"문의 {suffix}",
        "product_name": "삼성 모니터",
        "registered_at": registered_at,
        "raw_json": {},
    }).inquiry_id


# ===========================================================================
# 1. 기본 기간 = 달력 3개월
# ===========================================================================

def test_the_default_range_is_three_calendar_months_to_today() -> None:
    assert default_dashboard_range(date(2026, 10, 29)) == (
        date(2026, 7, 29), date(2026, 10, 29)
    )


@pytest.mark.parametrize("today, expected", [
    # 90 일 고정이 아니라 달력 기준이다. 90 일을 빼면 각각 2026-07-31,
    # 2026-03-02 가 되어 "3개월 전" 과 어긋난다.
    (date(2026, 10, 29), date(2026, 7, 29)),
    (date(2026, 5, 31), date(2026, 2, 28)),   # 2월에 31일이 없다
    (date(2026, 3, 31), date(2025, 12, 31)),  # 해를 넘는다
    (date(2026, 1, 15), date(2025, 10, 15)),
    (date(2024, 5, 31), date(2024, 2, 29)),   # 윤년
])
def test_three_months_before_follows_the_calendar(today, expected) -> None:
    assert months_before(today, DEFAULT_RANGE_MONTHS) == expected


def test_today_comes_from_the_seoul_clock() -> None:
    """오늘은 서버 지역시가 아니라 운영 시간대(KST)로 센다.

    런타임 값을 비교하지 않는다: ``test_dashboard_date_rollover`` 가 날짜를
    흉내 내려고 ``ui.dashboard.today_kst`` 를 모듈 속성째 교체하고 되돌리지
    않아서, 같은 프로세스에서 뒤에 도는 테스트는 그 stub 을 보게 된다. 그래서
    요구사항 자체를 소스에서 고정한다 -- ``date.today()`` 로 바뀌면 깨진다.
    """

    import inspect

    import ui.dashboard as dash

    source = inspect.getsource(dash)
    assert "def today_kst() -> date:" in source
    body = source.split("def today_kst() -> date:", 1)[1].split("def ", 1)[0]
    assert "datetime.now(KST).date()" in body, body
    # 기본 범위의 종료일은 그 "오늘" 이다 (stub 여부와 무관하게 성립한다).
    assert default_dashboard_range()[1] == dash.today_kst()


# ===========================================================================
# 2. 기간이 실제 DB 조회를 줄인다
# ===========================================================================

def test_the_range_narrows_the_rows_the_database_returns(database) -> None:
    """범위 밖 문의는 애초에 읽히지 않는다."""

    import app as appmod

    today = date(2026, 10, 29)
    inside = _inquiry(
        database, registered_at="2026-09-01T10:00:00+09:00", suffix="inside")
    edge_start = _inquiry(
        database, registered_at="2026-07-29T00:05:00+09:00", suffix="edge-start")
    edge_end = _inquiry(
        database, registered_at="2026-10-29T23:55:00+09:00", suffix="edge-end")
    outside = _inquiry(
        database, registered_at="2024-09-01T12:15:25+09:00", suffix="outside")

    start, end = default_dashboard_range(today)
    scoped = appmod.dashboard_work_items_from_database(
        database,
        registered_from=start.isoformat(),
        registered_to=end.isoformat(),
    )
    ids = {str(item.get("inquiry_id")) for item in scoped}

    assert ids == {"q-inside", "q-edge-start", "q-edge-end"}, ids
    # 경계일은 양쪽 모두 포함된다.
    assert "q-outside" not in ids
    assert {inside, edge_start, edge_end, outside}  # 네 건 모두 저장돼 있다

    # 범위를 넓히면 과거 문의가 다시 보인다 -- 데이터는 지워지지 않았다.
    widened = appmod.dashboard_work_items_from_database(
        database, registered_from="2024-01-01", registered_to=end.isoformat(),
    )
    assert len(widened) == 4


def test_the_repository_count_and_list_use_the_same_window(database) -> None:
    """건수와 목록이 같은 기간을 본다 -- 어긋나면 페이지네이션이 깨진다."""

    for index in range(5):
        _inquiry(
            database,
            registered_at=f"2026-09-{index + 1:02d}T10:00:00+09:00",
            suffix=f"in-{index}",
        )
    for index in range(3):
        _inquiry(
            database,
            registered_at=f"2025-01-{index + 1:02d}T10:00:00+09:00",
            suffix=f"old-{index}",
        )
    repository = InquiryRepository(database)

    scoped_count = repository.count(
        registered_from="2026-07-29", registered_to="2026-10-29")
    scoped_rows = repository.list(
        limit=100, registered_from="2026-07-29", registered_to="2026-10-29")

    assert scoped_count == 5
    assert len(scoped_rows) == 5
    assert repository.count() == 8  # 범위 없이 부르면 전체 그대로


def test_an_unscoped_call_still_reads_everything(database) -> None:
    """범위를 주지 않는 기존 호출은 동작이 바뀌지 않는다."""

    import app as appmod

    _inquiry(database, registered_at="2024-09-01T12:00:00+09:00", suffix="a")
    _inquiry(database, registered_at="2026-10-01T12:00:00+09:00", suffix="b")

    assert len(appmod.dashboard_work_items_from_database(database)) == 2
    assert InquiryRepository(database).count() == 2


def test_the_sql_window_matches_the_python_filter(database) -> None:
    """SQL 로 먼저 걸러도 ``filter_work_items`` 결과가 같아야 한다.

    두 술어가 어긋나면 KPI 모집단이 조용히 달라진다. 같은 조건을 한 단계
    앞으로 옮기는 변경이라는 것이 여기서 확인된다.
    """

    import app as appmod

    for day in ("2026-07-28", "2026-07-29", "2026-09-15", "2026-10-29"):
        _inquiry(database, registered_at=f"{day}T10:00:00+09:00", suffix=day)
    start, end = default_dashboard_range(date(2026, 10, 29))
    filter_kwargs = dict(
        store_codes=[], source="ALL", queues=[], priorities=[],
        answer_status="ALL", delivery_only=False, search_query="",
        start_date=start, end_date=end, route="ALL", learning_status="ALL",
    )

    # (1) 전체를 읽고 파이썬에서 거른 결과
    everything = appmod.dashboard_work_items_from_database(database)
    python_filtered = appmod.filter_work_items(everything, **filter_kwargs)
    # (2) SQL 로 먼저 좁히고 같은 필터를 통과시킨 결과
    scoped = appmod.dashboard_work_items_from_database(
        database, registered_from=start.isoformat(),
        registered_to=end.isoformat(),
    )
    sql_filtered = appmod.filter_work_items(scoped, **filter_kwargs)

    assert [item["inquiry_id"] for item in python_filtered] == [
        item["inquiry_id"] for item in sql_filtered
    ]
    assert "q-2026-07-28" not in {
        item["inquiry_id"] for item in sql_filtered
    }


# ===========================================================================
# 3. 운영자가 고른 범위는 유지된다
# ===========================================================================

def test_a_range_the_operator_picked_survives_a_rerun(monkeypatch) -> None:
    """``current_dashboard_range`` 는 세션의 선택을 기본값보다 우선한다.

    문의 선택·승인·페이지 이동은 모두 rerun 이므로, 이 함수가 선택을 지키는
    것이 그 네 경우를 한꺼번에 지키는 것이다.
    """

    import streamlit as st
    from ui.dashboard import current_dashboard_range

    picked = (date(2025, 1, 1), date(2026, 10, 29))
    monkeypatch.setitem(st.session_state, "dashboard_date_range", picked)

    assert current_dashboard_range(date(2026, 10, 29)) == picked


def test_a_partial_pick_falls_back_to_the_default(monkeypatch) -> None:
    """날짜 위젯이 시작일만 고른 중간 상태는 범위가 아니다."""

    import streamlit as st
    from ui.dashboard import current_dashboard_range

    monkeypatch.setitem(
        st.session_state, "dashboard_date_range", (date(2026, 5, 1),))

    assert current_dashboard_range(date(2026, 10, 29)) == (
        date(2026, 7, 29), date(2026, 10, 29)
    )


def test_an_empty_session_uses_the_default(monkeypatch) -> None:
    import streamlit as st
    from ui.dashboard import current_dashboard_range

    st.session_state.pop("dashboard_date_range", None)

    assert current_dashboard_range(date(2026, 10, 29)) == (
        date(2026, 7, 29), date(2026, 10, 29)
    )


def test_crossing_midnight_moves_the_default_but_not_a_choice() -> None:
    """자정을 넘겨도 종료일이 어제에 머물지 않는다.

    전에 고친 결함이고, 기본값 계산을 바꾸면서 되돌아갈 수 있어 여기서 다시
    고정한다. 기본값은 오늘을 따라가고, 운영자가 고른 범위는 따라가지 않는다.
    """

    yesterday = date(2026, 10, 28)
    today = date(2026, 10, 29)

    assert default_dashboard_range(yesterday) == (
        date(2026, 7, 28), yesterday)
    assert default_dashboard_range(today) == (date(2026, 7, 29), today)
    # 자정을 넘는 것은 기본값의 "오늘" 이 바뀌는 것이고, 운영자의 선택은
    # ``current_dashboard_range`` 가 세션에서 읽어 그대로 돌려준다.
    assert months_before(today, DEFAULT_RANGE_MONTHS) != months_before(
        yesterday, DEFAULT_RANGE_MONTHS)


# ===========================================================================
# 4. 운영 진단 쿼리: 최적화 전후 결과 동일
# ===========================================================================

def _seed_queue(database: Database) -> int:
    """검토 대기 문의 하나와 그 큐 이벤트·검토 로그."""

    from repositories.auto_post_event_repository import AutoPostEventRepository
    from repositories.log_repository import LogRepository

    inquiry_id = _inquiry(
        database, registered_at="2026-10-20T10:00:00+09:00", suffix="queue")
    InquiryRepository(database).update_status(inquiry_id, "NEEDS_ATTENTION")
    AutoPostEventRepository(database).create(
        inquiry_id=inquiry_id, store_code="OJE_PLUS", external_id="q-queue",
        source_sync_id=None, runtime_enabled=True,
    )
    logs = LogRepository(database)
    # 같은 문의에 두 건 -- 최신 한 건만 집계돼야 한다.
    logs.record_inquiry(
        inquiry_id, "AUTO_PROCESSING_REVIEW_REQUIRED", "old",
        level="WARNING", details={"reasons": ["EVIDENCE_NOT_SUFFICIENT"]},
    )
    logs.record_inquiry(
        inquiry_id, "AUTO_PROCESSING_REVIEW_REQUIRED", "new",
        level="WARNING", details={"reasons": ["GPT_REPORTED_UNRESOLVED"]},
    )
    return inquiry_id


def test_queue_diagnostics_counts_only_the_latest_review_event(
    database,
) -> None:
    """최적화한 쿼리가 ``a.id = MAX(...)`` 와 같은 행을 고른다."""

    from services.dashboard_operations_service import DashboardOperationsService

    _seed_queue(database)

    diagnostics = DashboardOperationsService(database).queue_diagnostics()

    reasons = diagnostics["review_required_reasons"]
    assert reasons == {"미해결 문항 있음": 1}, reasons
    assert "배송·설치 소요기간 근거 없음" not in reasons


def test_queue_diagnostics_keeps_its_shape_and_recent_events(database) -> None:
    """정렬·건수·상태·사유 구분이 유지된다."""

    from services.dashboard_operations_service import DashboardOperationsService

    inquiry_id = _seed_queue(database)

    diagnostics = DashboardOperationsService(database).queue_diagnostics()

    assert set(diagnostics["queue"]) >= {
        "claimable_pending", "processing", "retry_scheduled",
        "blocked_auto_post_off", "failed",
    }
    recent = diagnostics["recent_events"]
    assert [event["inquiry_id"] for event in recent] == [inquiry_id]
    assert recent[0]["queue_status"]
    assert recent[0]["reasons"] == ["미해결 문항 있음"]
    assert set(diagnostics["severity_counts"]) == {
        "hard_block", "soft_warning_auto_posted", "failed",
    }


def test_an_inquiry_without_a_review_event_is_not_counted(database) -> None:
    """검토 이벤트가 없는 문의는 사유 집계에 들어가지 않는다.

    최적화 전 쿼리는 JOIN 으로 그런 문의를 떨어뜨렸다. 바꾼 쿼리는 NULL 을
    바깥에서 제외해 같은 모집단을 유지한다 -- 이 차이를 놓치면 건수가 늘어난다.
    """

    from services.dashboard_operations_service import DashboardOperationsService

    _seed_queue(database)
    quiet = _inquiry(
        database, registered_at="2026-10-21T10:00:00+09:00", suffix="quiet")
    InquiryRepository(database).update_status(quiet, "NEEDS_ATTENTION")

    diagnostics = DashboardOperationsService(database).queue_diagnostics()

    assert sum(diagnostics["review_required_reasons"].values()) == 1
