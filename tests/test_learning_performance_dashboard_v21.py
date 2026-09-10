from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_provenance_repository import LearningProvenanceRepository
from repositories.learning_repository import LearningRepository
from repositories.log_repository import LogRepository
from repositories.post_review_repository import PostReviewRepository
from services.learning_performance_service import LearningPerformanceService
from services.learning_service import LearningService
from services.historical_case_service import HistoricalCaseService
from services.post_review_service import PostReviewService
from core.time_utils import format_datetime_minute_kst


def _inquiry(database: Database, key: str, inquiry_type: str = "배송") -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "source_question_id": key, "external_inquiry_id": key,
        "inquiry_type": inquiry_type, "title": "배송 문의",
        "content": "언제 보내주시나요?", "product_name": "테스트 상품",
        "registered_at": datetime.now(UTC).isoformat(), "source_answered": False,
        "source_status": "WAITING", "source_created_at": datetime.now(UTC).isoformat(),
        "source_updated_at": datetime.now(UTC).isoformat(), "is_private": False,
        "source_metadata_json": {}, "workflow_status": "NEW",
        "answer_status": "UNANSWERED", "post_status": "NOT_POSTED", "raw_json": {},
    }).inquiry_id


def _post(database: Database, key: str, inquiry_type: str = "배송") -> tuple[int, int, int]:
    inquiry_id = _inquiry(database, key, inquiry_type)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category=inquiry_type, reason="safe",
            answer="현재 확인된 기준으로 안전하게 안내드립니다.", provider="rules",
            auto_answerable=True, needs_review=False,
            metadata={"selected_answer_route": "SAFE_RULE"},
        ),
    )
    with database.transaction() as connection:
        connection.execute("UPDATE answer_drafts SET validation_status='PASS' WHERE id=?", (draft["id"],))
    reviews = PostReviewRepository(database)
    version, _ = reviews.finalize_auto(
        inquiry_id=inquiry_id, draft_id=int(draft["id"]), run_id=key
    )
    reviews.create_review_after_post(
        inquiry_id=inquiry_id, draft_id=int(draft["id"]), version_id=int(version["id"]),
        run_id=key, route="SAFE_RULE", needs_staff_review=False,
        posted_at=datetime.now(UTC).isoformat(),
    )
    with database.transaction() as connection:
        connection.execute("UPDATE inquiries SET post_status='POSTED' WHERE id=?", (inquiry_id,))
    return inquiry_id, int(draft["id"]), int(version["id"])


def _seed_legacy_unchanged_learning(
    database: Database, inquiry_id: int,
) -> dict:
    version = next(
        item
        for item in PostReviewRepository(database).versions(inquiry_id)
        if item["version_kind"] == "REVIEWED_NO_CHANGE"
    )
    saved = LearningService(database).capture_auto_post_version(
        inquiry_id=inquiry_id,
        version_id=int(version["id"]),
        source="AUTO_POST_REVIEWED_NO_CHANGE",
    )
    assert saved is not None
    return saved


def test_learning_performance_rates_sources_and_real_provenance(tmp_path: Path) -> None:
    database = Database(tmp_path / "performance.db"); database.initialize()
    unchanged_id, unchanged_draft, _ = _post(database, "unchanged", "배송")
    PostReviewService(database).complete_without_change(
        inquiry_id=unchanged_id, actor="tester"
    )
    # Seed one preserved pre-policy row: this test verifies historical
    # analytics/provenance, not the retired automatic creation path.
    unchanged_learning = _seed_legacy_unchanged_learning(
        database, unchanged_id
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE learning_examples
            SET metadata_json=json_set(metadata_json,'$.acceptance_mode','AUTO_OBSERVATION')
            WHERE id=?
            """,
            (unchanged_learning["id"],),
        )
    provenance = LearningProvenanceRepository(database)
    provenance.record_context(
        inquiry_id=unchanged_id,
        learning=[{
            "learning_example_id": unchanged_learning["id"],
            "learning_source": unchanged_learning["learning_source"],
            "relevance": 0.91,
        }], historical=[],
    )
    provenance.attach_latest_context(inquiry_id=unchanged_id, draft_id=unchanged_draft)

    historical_service = HistoricalCaseService(database)
    historical_case = historical_service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "performance-history", "title": "배송 문의",
        "content": "언제 보내주시나요?",
        "seller_answer": "현재 주문 정보 확인 후 배송 일정을 안내해 주세요.",
        "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="TEST:performance")
    historical_row, _ = historical_service.repository.upsert(historical_case)
    provenance.record_context(
        inquiry_id=unchanged_id, learning=[], historical=[{
            "historical_case_id": historical_row["id"],
            "source": "HISTORICAL_REFERENCE", "relevance": 0.78,
        }],
    )
    provenance.attach_latest_context(inquiry_id=unchanged_id, draft_id=unchanged_draft)

    corrected_id, _, _ = _post(database, "corrected", "상품")
    reviews = PostReviewRepository(database)
    corrected_version, changed = reviews.capture_remote_naver_edit(
        inquiry_id=corrected_id, answer_body="직원이 수정한 최종 안내입니다."
    )
    assert changed is True
    LearningService(database).capture_auto_post_version(
        inquiry_id=corrected_id, version_id=int(corrected_version["id"]),
        source="AUTO_POST_CORRECTED",
    )

    data = LearningPerformanceService(database).snapshot()
    assert data["current_30"]["known"] == 2
    assert data["current_30"]["unchanged_rate"] == 50.0
    assert data["current_30"]["correction_rate"] == 50.0
    assert data["provenance"]["generated_with_learning"] == 1
    assert data["provenance"]["generated_with_historical"] == 1
    assert data["provenance"]["used"]["unchanged_rate"] == 100.0
    assert data["provenance"]["not_used"]["unchanged_rate"] == 0.0
    assert any(row["source_group"] == "POSITIVE_LEARNING" for row in data["sources"])
    assert any(
        row["source_group"] == "HISTORICAL_VERIFIED_LEARNING"
        for row in data["sources"]
    )
    assert {row["inquiry_type"] for row in data["types"]} == {"배송", "상품"}


def test_empty_performance_never_invents_rates(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db"); database.initialize()
    data = LearningPerformanceService(database).snapshot()
    assert data["current_7"]["unchanged_rate"] is None
    assert data["current_30"]["correction_rate"] is None
    assert data["provenance"]["used"]["unchanged_rate"] is None
    assert data["trend"] == []
    assert data["quality"]["current"]["generation_rate"] is None
    assert data["quality"]["current"]["correction_rate"] is None
    for days in (7, 30, 90):
        quality = LearningPerformanceService(database).snapshot(
            period_days=days
        )["quality"]
        assert quality["period_days"] == days
        assert quality["current"]["processed"] == 0
        assert quality["previous"]["generation_rate"] is None


@pytest.mark.parametrize(
    ("current", "previous", "higher_is_better", "expected", "color"),
    [
        (90.0, 80.0, True, "+10.0%p · 개선", "normal"),
        (70.0, 80.0, True, "-10.0%p · 악화", "normal"),
        (5.0, 10.0, False, "-5.0%p · 개선", "inverse"),
        (12.0, 10.0, False, "+2.0%p · 악화", "inverse"),
        (10.0, 10.0, False, "변화 없음", "off"),
        (None, 10.0, False, "이전 기간 데이터 부족", "off"),
    ],
)
def test_quality_metric_delta_direction(
    current, previous, higher_is_better, expected, color
) -> None:
    from ui.learning_performance import _metric_delta

    assert _metric_delta(
        current, previous, higher_is_better=higher_is_better
    ) == (expected, color)


def _event(
    database: Database, inquiry_id: int, code: str, *, days_ago: int = 0
) -> None:
    event_id = LogRepository(database).record_inquiry(
        inquiry_id, code, "quality fixture"
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE activity_logs SET created_at=datetime('now', ?) WHERE id=?",
            (f"-{days_ago} days", event_id),
        )


def _auto_post_attempt(
    database: Database,
    *,
    inquiry_id: int,
    draft_id: int,
    key: str,
    days_ago: int = 0,
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO naver_post_attempts(
              inquiry_id, answer_draft_id, idempotency_key, external_id,
              store_code, source_type, method, endpoint_kind, status,
              final_answer_hash, payload_hash, actor, started_at,
              completed_at, auto_post_run_id
            ) VALUES (?, ?, ?, ?, 'OJE_PLUS', 'PRODUCT_INQUIRY', 'POST',
                      'PRODUCT_INQUIRY', 'POSTED', 'answer-hash',
                      'payload-hash', 'SYSTEM_AUTO_POST', datetime('now', ?),
                      datetime('now', ?), ?)
            """,
            (
                inquiry_id, draft_id, f"attempt-{key}", key,
                f"-{days_ago} days", f"-{days_ago} days", f"run-{key}",
            ),
        )


def test_operator_quality_kpis_use_durable_period_sources(tmp_path: Path) -> None:
    database = Database(tmp_path / "quality-kpi.db")
    database.initialize()

    current_posted, current_draft, current_version = _post(database, "current-posted")
    PostReviewService(database).complete_without_change(
        inquiry_id=current_posted, actor="tester"
    )
    _event(database, current_posted, "AUTO_ANSWER_STARTED")
    _event(database, current_posted, "AUTO_ANSWER_SUCCEEDED")
    _auto_post_attempt(
        database, inquiry_id=current_posted, draft_id=current_draft,
        key="current-posted",
    )

    current_corrected, _, current_corrected_version = _post(
        database, "current-corrected"
    )
    PostReviewRepository(database).capture_remote_naver_edit(
        inquiry_id=current_corrected,
        answer_body="직원이 수정한 현재 기간 답변입니다.",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE answer_versions SET posted_at=datetime('now','-1 day') WHERE id=?",
            (current_corrected_version,),
        )

    current_success = _inquiry(database, "current-success")
    current_review = _inquiry(database, "current-review")
    current_failed = _inquiry(database, "current-failed")
    current_policy = _inquiry(database, "current-policy")
    for inquiry_id in (
        current_success, current_review, current_failed, current_policy
    ):
        _event(database, inquiry_id, "AUTO_ANSWER_STARTED")
    _event(database, current_success, "AUTO_ANSWER_SUCCEEDED")
    _event(database, current_review, "AUTO_PROCESSING_REVIEW_REQUIRED")
    _event(database, current_failed, "AUTO_ANSWER_FAILED")
    _event(database, current_policy, "AUTO_POST_SKIPPED_POLICY_BLOCKED")

    previous_one, previous_one_draft, previous_one_version = _post(
        database, "previous-one"
    )
    previous_two, previous_two_draft, previous_two_version = _post(
        database, "previous-two"
    )
    PostReviewService(database).complete_without_change(
        inquiry_id=previous_one, actor="tester"
    )
    PostReviewRepository(database).capture_remote_naver_edit(
        inquiry_id=previous_two,
        answer_body="직원이 수정한 이전 기간 답변입니다.",
    )
    previous_success = _inquiry(database, "previous-success")
    previous_review = _inquiry(database, "previous-review")
    for inquiry_id in (
        previous_one, previous_two, previous_success, previous_review
    ):
        _event(database, inquiry_id, "AUTO_ANSWER_STARTED", days_ago=10)
    for inquiry_id in (previous_one, previous_two, previous_success):
        _event(database, inquiry_id, "AUTO_ANSWER_SUCCEEDED", days_ago=10)
    _event(
        database, previous_review, "AUTO_PROCESSING_REVIEW_REQUIRED",
        days_ago=10,
    )
    _auto_post_attempt(
        database, inquiry_id=previous_one, draft_id=previous_one_draft,
        key="previous-one", days_ago=10,
    )
    _auto_post_attempt(
        database, inquiry_id=previous_two, draft_id=previous_two_draft,
        key="previous-two", days_ago=10,
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_created_at=datetime('now','-10 days'), "
            "created_at=datetime('now','-10 days') WHERE id IN (?, ?, ?, ?)",
            (previous_one, previous_two, previous_success, previous_review),
        )
        connection.execute(
            "UPDATE inquiries SET answer_status='REVIEW_REQUIRED' WHERE id=?",
            (current_review,),
        )
        connection.execute(
            "UPDATE answer_versions SET posted_at=datetime('now','-10 days') "
            "WHERE id IN (?, ?)",
            (previous_one_version, previous_two_version),
        )

    data = LearningPerformanceService(database).snapshot(period_days=7)
    current = data["quality"]["current"]
    previous = data["quality"]["previous"]
    assert (current["processed"], current["generated"]) == (6, 2)
    assert (current["auto_posted"], current["review_required"]) == (1, 2)
    assert current["generation_rate"] == round(2 / 6 * 100, 1)
    # 자동 등록률의 분모는 "답변이 만들어진 문의"다.
    #
    # 이 fixture 가 그 이유를 그대로 보여준다: 들어온 문의 6건 중 답변이 있는
    # 것은 2건이고, 그중 1건이 자동등록됐다. 6을 분모로 쓰면 16.7% 가 되어
    # 답변조차 만들어지지 않은 4건을 자동등록 실패로 읽게 된다. "자동등록 대상"
    # 은 어디에도 저장되지 않으며, 저장 구조가 실제로 아는 것은 답변의 존재다.
    assert current["auto_post_rate"] == round(1 / 2 * 100, 1)
    assert current["auto_post_denominator"] == 2
    # 직원 검토 필요율은 분자·분모가 같은 모집단을 쓴다. 이전에는 분자가 로그
    # 시각으로, 분모가 문의 시각으로 필터링돼 100% 를 넘을 수 있었다.
    assert current["review_required_rate"] == round(2 / 6 * 100, 1)
    assert current["review_required_denominator"] == 6
    # 직원 수정률의 분모는 기간 내 답변 전체다. 이전에는 "수정됨 + 무수정 확인"
    # 만을 분모로 써서, 무수정 확인이 없으면 수정 1건으로도 50~100% 가 나왔다.
    assert current["corrected_in_period"] == 1
    assert current["correction_rate"] == round(1 / 2 * 100, 1)
    assert current["correction_denominator"] == 2
    # 판정 완료/대기 수치는 상세 표용으로 그대로 유지된다.
    assert current["correction_known"] == 2
    assert current["corrected"] == 1
    assert (previous["processed"], previous["generated"]) == (4, 2)
    assert previous["auto_post_rate"] == round(2 / 2 * 100, 1)
    assert previous["review_required_rate"] == 25.0
    assert previous["correction_rate"] == round(1 / 2 * 100, 1)
    # The selected 7-day KPI trend contains the current cohort only; the
    # preceding period is shown by the card comparison, not mixed into chart.
    assert len(data["quality"]["correction_trend"]) == 1


def test_generation_context_is_attached_only_to_actual_draft(tmp_path: Path) -> None:
    database = Database(tmp_path / "provenance.db"); database.initialize()
    source_id, _, _ = _post(database, "source")
    PostReviewService(database).complete_without_change(inquiry_id=source_id, actor="tester")
    learning = _seed_legacy_unchanged_learning(database, source_id)
    target_id = _inquiry(database, "target")
    repository = LearningProvenanceRepository(database)
    repository.record_context(
        inquiry_id=target_id,
        learning=[{
            "learning_example_id": learning["id"],
            "learning_source": learning["learning_source"],
            "relevance": 0.87,
        }], historical=[],
    )
    draft = AnswerRepository(database).create_program_draft(
        target_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="배송", reason="test",
            answer="참고자료를 포함한 답변입니다.", provider="rules",
            auto_answerable=True, needs_review=False, metadata={},
        ),
    )
    rows = repository.for_draft(int(draft["id"]))
    assert len(rows) == 1
    assert rows[0]["learning_example_id"] == learning["id"]
    assert rows[0]["relevance"] == 0.87


def test_dashboard_list_header_scroll_and_minute_format_are_separated() -> None:
    workspace = (Path(__file__).parents[1] / "ui" / "review_workspace.py").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "ui" / "dashboard.css").read_text(encoding="utf-8")
    assert workspace.index("_render_list_header(total_count)") < workspace.index('key="official_inquiry_rows_scroll"')
    assert workspace.index('key="official_inquiry_rows_scroll"') < workspace.index("_render_pagination(resolved_page")
    assert 'key="official_inquiry_list_panel"' in workspace
    assert "overflow-y: auto !important" in css[css.index("st-key-official_inquiry_rows_scroll"):]
    assert "received-time" in css and "text-overflow: clip" in css
    assert format_datetime_minute_kst("2026-08-07T06:24:59Z") == "2026-08-07 15:24"


def test_learning_performance_apptest_and_session_state(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest
    path = tmp_path / "app.db"
    Database(path).initialize()
    app = AppTest.from_string(f'''
import streamlit as st
from repositories.database import Database
from ui.learning_performance import render_learning_performance
db=Database(r"{path}")
db.initialize()
st.session_state.setdefault("dashboard_page", 5)
st.session_state.setdefault("historical_selected_case_id", 77)
render_learning_performance(db)
''').run(timeout=60)
    assert not app.exception
    assert app.session_state["dashboard_page"] == 5
    assert app.session_state["historical_selected_case_id"] == 77
    assert app.session_state["learning_performance_period"] == "최근 30일"
    rendered = "\n".join(item.value for item in [*app.markdown, *app.caption, *app.info])
    assert "Learning 성과" in rendered
    assert len(app.metric[:3]) == 3 or {metric.label for metric in app.metric[:3]} == {
        "자동 답변 생성률", "자동 등록률", "직원 수정률", "직원 검토 필요율",
    }
    selector = next(item for item in app.selectbox if item.label == "품질 기간")
    assert selector.options == ["최근 7일", "최근 30일", "최근 90일"]
    assert any(item.label == "상세 분석" for item in app.expander)
    # 분모가 0 이면 0.0%/100.0% 대신 "데이터 없음" 을 보여준다.
    assert any(metric.value == "데이터 없음" for metric in app.metric)


def test_kpi_denominators_are_the_period_population(tmp_path: Path) -> None:
    """세 KPI 의 분자/분모를 fixture 로 직접 세어 확인한다.

    이 테스트가 막는 것은 세 가지 실제 결함이다.

    * 자동 등록률이 답변조차 없는 문의를 분모에 넣어 낮게 나오는 것
    * 직원 수정률이 "수정됨 + 무수정 확인" 만을 분모로 써서, 무수정 확인이 없으면
      수정 1건으로도 100% 가 되는 것
    * 직원 검토 필요율의 분자가 로그 시각으로, 분모가 문의 시각으로 필터링돼
      기간 밖 문의가 분자에만 들어가 100% 를 넘을 수 있는 것
    """

    database = Database(tmp_path / "kpi-denominator.db")
    database.initialize()

    # 답변 있음 + 자동등록 성공
    posted, posted_draft, _ = _post(database, "kpi-posted")
    _auto_post_attempt(
        database, inquiry_id=posted, draft_id=posted_draft, key="kpi-posted",
    )
    # 답변 있음 + 자동등록 없음 + 직원 수정 있음
    corrected, _, _ = _post(database, "kpi-corrected")
    PostReviewRepository(database).capture_remote_naver_edit(
        inquiry_id=corrected, answer_body="직원이 고친 답변입니다.",
    )
    # 답변 있음 + 수정 없음 + 검토 필요
    reviewed, _, _ = _post(database, "kpi-reviewed")
    _event(database, reviewed, "AUTO_PROCESSING_REVIEW_REQUIRED")
    # 답변 없음 (분모에서 제외되어야 하는 쪽)
    no_answer = _inquiry(database, "kpi-no-answer")
    _event(database, no_answer, "AUTO_ANSWER_STARTED")

    # 기간 밖 문의인데 검토 로그는 기간 안에 있는 경우 -> 분자에 들어가면 안 된다.
    outside = _inquiry(database, "kpi-outside")
    _event(database, outside, "AUTO_PROCESSING_REVIEW_REQUIRED")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_created_at=datetime('now','-40 days'),"
            " created_at=datetime('now','-40 days') WHERE id=?",
            (outside,),
        )

    current = LearningPerformanceService(database).snapshot(
        period_days=7
    )["quality"]["current"]

    # 분모: 기간 내 문의 4건(기간 밖 1건 제외), 그중 답변이 있는 것 3건
    assert current["processed"] == 4
    assert current["generated"] == 3
    # 자동 등록률 = 1 / 3 (답변 없는 1건은 분모에 없다)
    assert current["auto_posted"] == 1
    assert current["auto_post_denominator"] == 3
    assert current["auto_post_rate"] == round(1 / 3 * 100, 1)
    # 직원 수정률 = 1 / 3 (수정 1건, 분모는 기간 내 답변 전체)
    assert current["corrected_in_period"] == 1
    assert current["correction_denominator"] == 3
    assert current["correction_rate"] == round(1 / 3 * 100, 1)
    # 직원 검토 필요율 = 1 / 4. 기간 밖 문의의 검토 로그는 분자에 없다.
    assert current["review_required"] == 1
    assert current["review_required_denominator"] == 4
    assert current["review_required_rate"] == round(1 / 4 * 100, 1)
    assert current["review_required_rate"] <= 100.0


def test_kpi_shows_no_data_instead_of_zero_when_period_is_empty(
    tmp_path: Path,
) -> None:
    """분모가 0 이면 0.0%/100.0% 가 아니라 None -> "데이터 없음" 이어야 한다."""

    from ui.learning_performance import _percent

    database = Database(tmp_path / "kpi-empty.db")
    database.initialize()
    current = LearningPerformanceService(database).snapshot(
        period_days=7
    )["quality"]["current"]

    assert current["processed"] == 0
    for key in ("auto_post_rate", "correction_rate", "review_required_rate"):
        assert current[key] is None, key
        assert _percent(current[key]) == "데이터 없음"


def test_period_selection_uses_one_window_for_all_three_kpis(
    tmp_path: Path,
) -> None:
    """기간 선택이 세 KPI 에 같은 start/end 로 적용된다."""

    database = Database(tmp_path / "kpi-period.db")
    database.initialize()
    inside, inside_draft, _ = _post(database, "kpi-inside")
    _auto_post_attempt(
        database, inquiry_id=inside, draft_id=inside_draft, key="kpi-inside",
    )
    older, older_draft, _ = _post(database, "kpi-older")
    _auto_post_attempt(
        database, inquiry_id=older, draft_id=older_draft, key="kpi-older",
    )
    _event(database, older, "AUTO_PROCESSING_REVIEW_REQUIRED", days_ago=20)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET source_created_at=datetime('now','-20 days'),"
            " created_at=datetime('now','-20 days') WHERE id=?",
            (older,),
        )

    service = LearningPerformanceService(database)
    seven = service.snapshot(period_days=7)["quality"]["current"]
    thirty = service.snapshot(period_days=30)["quality"]["current"]

    # 7일 창에는 최근 1건만, 30일 창에는 2건 모두. 세 KPI 가 같은 모집단을 쓴다.
    assert (seven["processed"], seven["generated"]) == (1, 1)
    assert seven["review_required_denominator"] == 1
    assert seven["auto_post_denominator"] == 1
    assert seven["correction_denominator"] == 1
    assert (thirty["processed"], thirty["generated"]) == (2, 2)
    assert thirty["review_required_denominator"] == 2
    assert thirty["auto_post_denominator"] == 2
    assert thirty["correction_denominator"] == 2
    # 기간 경계: 20일 전 문의는 7일 창에서 완전히 빠진다(분자·분모 모두).
    assert seven["review_required"] == 0
    assert thirty["review_required"] == 1


def test_correction_rate_does_not_read_as_100_percent_without_observations(
    tmp_path: Path,
) -> None:
    """무수정 확인이 한 건도 없어도 직원 수정률이 100% 로 읽히지 않는다."""

    database = Database(tmp_path / "kpi-correction.db")
    database.initialize()
    for index in range(4):
        _post(database, f"kpi-plain-{index}")
    corrected, _, _ = _post(database, "kpi-one-correction")
    PostReviewRepository(database).capture_remote_naver_edit(
        inquiry_id=corrected, answer_body="직원이 고친 답변입니다.",
    )

    current = LearningPerformanceService(database).snapshot(
        period_days=7
    )["quality"]["current"]

    # 수정 1건 / 답변 5건. 예전 분모(수정 1 + 무수정 확인 0)로는 100.0% 였다.
    assert current["corrected_in_period"] == 1
    assert current["correction_denominator"] == 5
    assert current["correction_rate"] == 20.0
    assert current["correction_known"] == 1
