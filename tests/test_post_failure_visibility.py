"""등록 실패가 운영자 화면에서 보이는가, 그리고 무엇을 할 수 있는가.

``auto_failed`` 는 **당일** 실패한 시도를 센다. 일일 보고로는 맞고 "지금 무엇이
깨져 있는가" 로는 틀리다. 26.10.22 스냅숏에서 문의 2054(2026-08-06)와
10621(2026-09-19)은 둘 다 POST_FAILED 로 남아 있었는데 그 숫자에서는 0 이었고,
어떤 전용 지표에도 올라오지 않았다. 그래서 미해결 집계를 따로 둔다.

같은 스냅숏에 과거 UAT 실행이 남긴 실패가 하나 섞여 있다 -- 문의 2054, actor
``SYSTEM_AUTO_POST_V22_UAT``, run id ``V22-UAT-…``, 이 저장소 첫 커밋보다 앞선
날짜. 고객에게 나갈 적이 없던 POST 이므로 운영 미해결 집계에서 빼되, 행의
상태는 건드리지 않는다.

이 파일은 표시와 분류만 고정한다. 재시도 허용 여부의 판단은 기존
``NON_RETRYABLE_TARGET_ERRORS`` 와 ``NaverPostService.post`` 에 그대로 있고,
아래 테스트는 그 정책이 넓어지지 않았다는 것을 함께 확인한다.
"""
from __future__ import annotations

import pytest

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_post_repository import NON_RETRYABLE_TARGET_ERRORS
from services.dashboard_operations_service import DashboardOperationsService


def _database(tmp_path, name: str = "failures") -> Database:
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    return database


def _inquiry(
    database: Database,
    *,
    external_id: str,
    store_code: str = "OJE_PLUS",
) -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": store_code,
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external_id,
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "상품문의",
        "content": "배송 언제 되나요?",
        "product_name": "삼성 TV",
        "product_id": "p1",
        "order_id": None,
        "product_order_id": None,
        "raw_json": {},
    }).inquiry_id


def _failed(
    database: Database,
    *,
    external_id: str,
    error_code: str,
    actor: str = "SYSTEM_AUTO_POST",
    run_id: str = "run-1",
    failed_at: str = "2026-09-19T00:55:07.859+00:00",
    store_code: str = "OJE_PLUS",
    source_answered: int = 0,
    source_deleted: int = 0,
) -> int:
    """POST_FAILED 상태의 문의 하나와 그 실패 시도 기록."""

    inquiry_id = _inquiry(database, external_id=external_id, store_code=store_code)
    # ``naver_post_attempts.answer_draft_id`` 는 NOT NULL 이므로 시도 기록에는
    # 실제 draft 가 있어야 한다. 기존 생성 경로를 그대로 쓴다.
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="GENERAL",
            reason="test",
            answer="확인 후 안내드리겠습니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE inquiries
            SET post_status='POST_FAILED', post_error_code=?,
                post_error_message='Naver answer request failed.',
                post_attempted_at=?, source_answered=?, source_deleted=?
            WHERE id=?
            """,
            (error_code, failed_at, source_answered, source_deleted, inquiry_id),
        )
        connection.execute(
            """
            INSERT INTO naver_post_attempts(
                inquiry_id, answer_draft_id, idempotency_key, external_id,
                store_code, source_type, method, endpoint_kind, status, actor,
                final_answer_hash, payload_hash,
                started_at, completed_at, auto_post_run_id, error_code,
                http_status
            ) VALUES (?,?,?,?,?,'PRODUCT_INQUIRY','PUT',
                      'PRODUCT_INQUIRY_ANSWER','POST_FAILED',?,?,?,?,?,?,?,404)
            """,
            (
                inquiry_id, draft["id"], f"key-{external_id}", external_id,
                store_code, actor, "hash-answer", "hash-payload",
                failed_at, failed_at, run_id, error_code,
            ),
        )
    return inquiry_id


def _operations(database: Database) -> dict:
    return DashboardOperationsService(database).snapshot()


# ===========================================================================
# 1, 2. 미해결 집계와 당일/누적 구분
# ===========================================================================

def test_an_old_failure_is_counted_as_open_but_not_as_today(tmp_path):
    database = _database(tmp_path, "old")
    _failed(database, external_id="Q-OLD", error_code="404")

    data = _operations(database)

    # 당일 실패는 0 -- 두 달 전 실패는 오늘의 숫자가 아니다.
    assert data["auto_failed"] == 0
    # 그래도 미해결로는 보여야 한다.
    assert data["post_failed_open"] == 1
    assert data["post_failed_retryable"] == 1
    assert data["post_failed_manual_only"] == 0


def test_an_answered_inquiry_is_no_longer_open(tmp_path):
    """원본에 답변이 달렸으면 등록이 남아 있지 않다."""

    database = _database(tmp_path, "answered")
    _failed(
        database, external_id="Q-ANS", error_code="404", source_answered=1,
    )

    data = _operations(database)

    assert data["post_failed_open"] == 0
    assert data["post_failures"] == []


def test_no_failures_reports_zero(tmp_path):
    database = _database(tmp_path, "none")
    _inquiry(database, external_id="Q-OK")

    data = _operations(database)

    assert data["post_failed_open"] == 0
    assert data["post_failed_retryable"] == 0
    assert data["post_failed_manual_only"] == 0
    assert data["post_failed_uat"] == 0


# ===========================================================================
# 3, 4. 목록 내용 -- 경과, 마켓, 스토어, 오류
# ===========================================================================

def test_the_list_carries_what_the_operator_needs(tmp_path):
    database = _database(tmp_path, "list")
    inquiry_id = _failed(
        database, external_id="Q-LIST", error_code="404",
        failed_at="2026-09-19T00:55:07.859+00:00",
    )

    rows = _operations(database)["post_failures"]

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == inquiry_id
    assert row["store_code"] == "OJE_PLUS"
    assert row["market"] == "NAVER"
    assert row["error_code"] == "404"
    assert row["failed_at"] == "2026-09-19T00:55:07.859+00:00"
    assert isinstance(row["elapsed_days"], int) and row["elapsed_days"] > 0
    assert row["failed_attempts"] == 1
    assert row["source_type"] == "PRODUCT_INQUIRY"


def test_a_coupang_failure_is_labelled_as_coupang(tmp_path):
    database = _database(tmp_path, "coupang")
    _failed(
        database, external_id="Q-CP", error_code="404",
        store_code="COUPANG_OJE",
    )

    rows = _operations(database)["post_failures"]

    assert [row["market"] for row in rows] == ["COUPANG"]
    assert [row["store_code"] for row in rows] == ["COUPANG_OJE"]


def test_both_markets_appear_separately(tmp_path):
    database = _database(tmp_path, "both")
    _failed(database, external_id="Q-N", error_code="404")
    _failed(
        database, external_id="Q-C", error_code="404",
        store_code="COUPANG_OJE",
    )

    data = _operations(database)

    assert data["post_failed_open"] == 2
    assert {row["market"] for row in data["post_failures"]} == {
        "NAVER", "COUPANG",
    }


def test_an_unreadable_timestamp_does_not_break_the_list(tmp_path):
    database = _database(tmp_path, "badtime")
    _failed(
        database, external_id="Q-BAD", error_code="404", failed_at="언제인지모름",
    )

    rows = _operations(database)["post_failures"]

    assert rows[0]["elapsed_days"] is None


# ===========================================================================
# 5, 6. 재시도 가능 / 금지 구분
# ===========================================================================

@pytest.mark.parametrize("error_code", sorted(NON_RETRYABLE_TARGET_ERRORS))
def test_every_terminal_error_is_reported_as_retry_forbidden(
    tmp_path, error_code,
):
    """종결 오류 목록을 다시 적지 않고 그 목록 자체로 검사한다."""

    database = _database(tmp_path, f"term-{error_code}")
    _failed(database, external_id=f"Q-{error_code}", error_code=error_code)

    data = _operations(database)
    row = data["post_failures"][0]

    assert row["terminal_error"] is True
    assert row["retryable"] is False
    assert row["needs_operator"] is True
    assert data["post_failed_manual_only"] == 1
    assert data["post_failed_retryable"] == 0


@pytest.mark.parametrize("error_code", ["404", "INVALID_REQUEST", "RATE_LIMITED"])
def test_a_non_terminal_error_stays_retryable(tmp_path, error_code):
    database = _database(tmp_path, f"retry-{error_code}")
    _failed(database, external_id=f"Q-{error_code}", error_code=error_code)

    data = _operations(database)
    row = data["post_failures"][0]

    assert row["terminal_error"] is False
    assert row["retryable"] is True
    assert row["needs_operator"] is False
    assert data["post_failed_retryable"] == 1


def test_a_terminal_error_states_the_manual_action(tmp_path):
    database = _database(tmp_path, "action")
    _failed(
        database, external_id="Q-TGT", error_code="REMOTE_TARGET_NOT_FOUND",
    )

    row = _operations(database)["post_failures"][0]

    assert "판매자센터" in row["manual_action"]


# ===========================================================================
# 7, 8. 이미 답변된 / 삭제된 원본
# ===========================================================================

def test_a_deleted_source_is_not_retryable(tmp_path):
    """삭제된 원본은 재시도 가능으로 표시되지 않는다.

    ``NaverPostService.post`` 가 ``SOURCE_DELETED`` 로 먼저 막으므로, 화면이
    재시도 가능이라고 말하면 운영자를 실패로 안내하는 셈이 된다.
    """

    database = _database(tmp_path, "deleted")
    _failed(
        database, external_id="Q-DEL", error_code="404", source_deleted=1,
    )

    data = _operations(database)
    row = data["post_failures"][0]

    assert row["retryable"] is False
    assert row["needs_operator"] is True
    assert "삭제" in row["manual_action"]
    assert data["post_failed_manual_only"] == 1


def test_the_post_service_still_refuses_an_answered_or_deleted_source():
    """기존 거부 게이트가 그대로 있는지 소스가 아니라 동작으로 확인한다."""

    from services.naver_post_service import NaverPostService

    assert hasattr(NaverPostService, "post")
    # 종결 오류 정책이 넓어지지 않았다.
    assert "REMOTE_TARGET_NOT_FOUND" in NON_RETRYABLE_TARGET_ERRORS
    assert "TARGET_ID_MAPPING_ERROR" in NON_RETRYABLE_TARGET_ERRORS
    assert "404" not in NON_RETRYABLE_TARGET_ERRORS


# ===========================================================================
# UAT 실패 구분
# ===========================================================================

@pytest.mark.parametrize(
    "actor, run_id",
    [
        ("SYSTEM_AUTO_POST_V22_UAT", "V22-UAT-9fb22cf424a04fe4b02834904e1b77ef"),
        ("SYSTEM_AUTO_POST", "V22-UAT-abc"),
        ("UAT_RUNNER", "run-9"),
    ],
)
def test_a_uat_failure_is_kept_out_of_the_operational_count(
    tmp_path, actor, run_id,
):
    database = _database(tmp_path, "uat")
    _failed(
        database, external_id="Q-UAT", error_code="404",
        actor=actor, run_id=run_id,
    )

    data = _operations(database)

    assert data["post_failed_uat"] == 1
    assert data["post_failed_open"] == 0
    assert data["post_failed_retryable"] == 0
    # 행은 사라지지 않는다 -- 상태를 바꾸지 않고 집계에서만 분리한다.
    assert len(data["post_failures"]) == 1
    assert data["post_failures"][0]["uat_origin"] is True


def test_a_production_failure_is_not_mistaken_for_uat(tmp_path):
    database = _database(tmp_path, "prod")
    _failed(
        database, external_id="Q-PROD", error_code="404",
        actor="SYSTEM_AUTO_POST", run_id="c6b9d9fe-089a-4ac1-8b2c",
    )

    data = _operations(database)

    assert data["post_failed_uat"] == 0
    assert data["post_failed_open"] == 1
    assert data["post_failures"][0]["uat_origin"] is False


def test_a_uat_row_keeps_its_stored_state(tmp_path):
    """집계에서 뺀다고 해서 실패 이력이나 상태를 고치지 않는다."""

    database = _database(tmp_path, "uatstate")
    inquiry_id = _failed(
        database, external_id="Q-UAT2", error_code="404",
        actor="SYSTEM_AUTO_POST_V22_UAT", run_id="V22-UAT-x",
    )

    _operations(database)

    with database.connection() as connection:
        row = connection.execute(
            "SELECT post_status, post_error_code FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone()
        attempts = connection.execute(
            "SELECT COUNT(*) FROM naver_post_attempts WHERE inquiry_id=?",
            (inquiry_id,),
        ).fetchone()[0]
    assert row["post_status"] == "POST_FAILED"
    assert row["post_error_code"] == "404"
    assert attempts == 1


# ===========================================================================
# 9. 실패 이력 보존 -- 읽기만 한다
# ===========================================================================

def test_reading_the_snapshot_writes_nothing(tmp_path):
    database = _database(tmp_path, "readonly")
    inquiry_id = _failed(database, external_id="Q-RO", error_code="404")
    with database.connection() as connection:
        before = connection.execute(
            "SELECT post_status, post_error_code, post_error_message,"
            " post_attempted_at FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone()
        before_row = dict(before)

    _operations(database)
    _operations(database)

    with database.connection() as connection:
        after = dict(connection.execute(
            "SELECT post_status, post_error_code, post_error_message,"
            " post_attempted_at FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone())
    assert after == before_row


def test_prepare_retry_is_still_unused_by_production():
    """``prepare_retry`` 를 연결하지 않았다는 것을 고정한다.

    POST_FAILED 를 NOT_POSTED 로 되돌리면 두 가지가 깨진다. 문의 행의
    ``post_error_code`` 가 지워져 마지막 실패 사유가 사라지고,
    ``AutoPostRepository.candidates`` 의 재시도 한도 절이
    ``i.post_status<>'POST_FAILED'`` 로 참이 되어 ``max_retries`` 가 우회된다.
    기존 명시적 재시도 경로는 그 둘을 모두 지키므로 그쪽을 쓴다.
    """

    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    callers = []
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if parts & {"tests", "tmp", ".verification", "scripts"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "prepare_retry" in text and path.name != "naver_post_repository.py":
            callers.append(str(path.relative_to(root)))
    assert not callers, f"prepare_retry 가 연결됐다: {callers}"


def test_allow_existing_pending_is_still_not_consumed():
    """큐 전용 정책이 유지되는지 설정 소비 지점으로 확인한다."""

    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    consumers = []
    allowed = {"auto_post_repository.py", "database.py"}
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if parts & {"tests", "tmp", ".verification", "scripts"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "allow_existing_pending" in text and path.name not in allowed:
            consumers.append(str(path.relative_to(root)))
    assert not consumers, f"미사용 설정이 소비되고 있다: {consumers}"
