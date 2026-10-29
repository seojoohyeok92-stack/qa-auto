"""자동등록의 세 결과가 운영자에게 서로 다르게 보고된다.

성공 / 등록 실패 / Eligibility 보류. 2026-10-29 서버 사본에서 세 건(11392,
11394, 11395)의 보류 알림은 실제로 발송돼 있었고(`review-required:*`,
status=sent), 누락된 것은 **등록 실패 알림**이었다 -- 974건의 발송 이력에
"등록 실패" 제목이 한 건도 없고, 트리에서 그 제목을 가진 코드는 2026-09-01
이후 실행되지 않는 ``naver_workflow`` CLI 뿐이었다. 그래서 POST_FAILED 는
활동 로그와 대시보드에만 남고 채팅방에는 침묵했다.

보류 알림의 본문도 운영자가 바로 처리하기엔 부족했다: 스토어(판매 계정),
문의 ID, 답변 초안이 모두 빠져 있었고 ID 는 발송되지 않는 ``notify_key``
안에만 있었다.

카카오톡에는 아무것도 보내지 않는다. outbox 는 ``tmp_path`` 의 파일이고
dispatcher 는 실행되지 않으며, 마켓 클라이언트는 기록용 더블이다.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kakao_notify
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from tests.test_coupang_kakao_routing import (
    ACCOUNTS,
    coupang_posting_opened,  # noqa: F401 - fixture
    COUPANG_ROOM,
    NAVER_ROOM,
    RecordingTransport,
    add_draft,
    coupang_inquiry,
    seed_catalog,
)

HOLD_TITLE = "[Q&A 미등록 / 직원 확인 필요]"


# --- 공통 더블 ---------------------------------------------------------------

@pytest.fixture
def outbox(tmp_path: Path, monkeypatch) -> Path:
    """발송 대기열을 tmp_path 로 돌린다. 실제 발송 경로는 쓰지 않는다."""

    service_dir = tmp_path / "common_service" / "kakao"
    service_dir.mkdir(parents=True)
    (service_dir / "kakao_dispatcher.py").write_text("# marker\n", encoding="utf-8")
    path = service_dir / "outbox_events.jsonl"
    monkeypatch.setenv("KAKAO_NOTIFY_ENABLED", "1")
    monkeypatch.delenv("KAKAO_QNA_RECIPIENT", raising=False)
    monkeypatch.delenv("KAKAO_COUPANG_QNA_RECIPIENT", raising=False)
    monkeypatch.setattr(kakao_notify, "KAKAO_SERVICE_DIR", service_dir)
    monkeypatch.setattr(kakao_notify, "OUTBOX", path)
    monkeypatch.setattr(
        kakao_notify, "NOTIFY_DB", tmp_path / "kakao_notify_history.sqlite3"
    )
    return path


@pytest.fixture
def broken_outbox(tmp_path: Path, monkeypatch) -> None:
    """공용 폴더가 없는 상태. ``_validate_kakao_service`` 가 거부한다."""

    monkeypatch.setenv("KAKAO_NOTIFY_ENABLED", "1")
    monkeypatch.setattr(
        kakao_notify, "KAKAO_SERVICE_DIR", tmp_path / "missing" / "kakao"
    )
    monkeypatch.setattr(
        kakao_notify, "OUTBOX", tmp_path / "missing" / "kakao" / "outbox.jsonl"
    )
    monkeypatch.setattr(
        kakao_notify, "NOTIFY_DB", tmp_path / "kakao_notify_history.sqlite3"
    )


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "kakao_outcomes.db")
    value.initialize()
    return value


def events(outbox: Path) -> list[dict]:
    if not outbox.exists():
        return []
    return [
        json.loads(line)
        for line in outbox.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def logs(database: Database, inquiry_id: int, event_code: str) -> list[dict]:
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT event_code, level, details_json FROM activity_logs
            WHERE inquiry_id=? AND event_code=?
            """,
            (inquiry_id, event_code),
        ).fetchall()
    return [dict(row) for row in rows]


# --- 1 / 3. 네이버 성공과 실패 -------------------------------------------------

NAVER_STORE = "OJE_PLUS"


def naver_inquiry(database: Database, *, external: str = "900001") -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": NAVER_STORE, "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external, "external_inquiry_id": external,
        "inquiry_type": "PRODUCT_GENERAL", "title": "상품문의",
        "content": "스탠드도 설치해주시나요?",
        "product_name": "삼성 4K UHD 스마트 비즈니스 TV LH50BEHHLGFXKR",
        "product_id": "p-1", "raw_json": {},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


class _Client:
    """네이버 등록 클라이언트 더블. 성공 또는 지정한 오류를 돌려준다."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def post_answer(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        from api.naver_answer_client import NaverAnswerResponse

        return NaverAnswerResponse(http_status=200, response_id="r-1")


def naver_post(database: Database, inquiry_id: int, *, error=None,
               automatic: bool = True):
    from services.naver_post_service import NaverPostService

    service = NaverPostService(database, client=_Client(error))
    return service.post(
        inquiry_id, actor="SYSTEM_AUTO_POST" if automatic else "관리자",
        confirmed=True, automatic=automatic,
    )


# --- 5 ~ 10. 보류 알림 --------------------------------------------------------

def hold_notification(
    database: Database, inquiry_id: int, reasons: tuple[str, ...],
) -> bool:
    """실제 파이프라인의 보류 알림 경로를 그대로 호출한다."""

    from services.auto_post_pipeline_service import AutoPostPipelineService

    pipeline = AutoPostPipelineService(database)
    pipeline._notify_review_terminal(inquiry_id=inquiry_id, reasons=reasons)
    return True


def test_a_coupang_hold_tells_the_operator_store_id_reason_and_draft(
    database, outbox,
) -> None:
    """5. REVIEW_REQUIRED 최초 발생 → 보류 알림."""

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100001")
    add_draft(database, inquiry_id, "확인된 사양에 저장 용량이 없어 안내가 어렵습니다.")

    hold_notification(
        database, inquiry_id,
        ("GPT_REPORTED_UNRESOLVED", "GPT_WITHHELD_AUTO_POST"),
    )

    sent = events(outbox)
    assert len(sent) == 1, sent
    assert sent[0]["title"] == HOLD_TITLE
    assert sent[0]["recipient"] == COUPANG_ROOM
    message = sent[0]["message"]
    assert "스토어: 오제앤에스" in message, message
    assert f"문의 ID: {inquiry_id}" in message or "문의 ID: 17100001" in message
    assert "미등록 사유: " in message
    assert "세부 사유: 미해결 문항 있음 · 자동등록 보류 판단" in message
    assert "답변 초안(미등록): 확인된 사양에 저장 용량이 없어" in message
    assert "쿠팡 등록: 안 됨" in message
    assert "처리: 답변 초안을 확인한 뒤 직접 등록해 주세요." in message
    # 보류는 등록이 아니다. ``답변``은 등록된 답변에만 쓰인다.
    assert "답변: -" in message


def test_the_same_hold_seen_again_does_not_notify_twice(database, outbox) -> None:
    """6. 동일 REVIEW_REQUIRED 재조회 → 중복 알림 없음.

    스케줄러는 10분마다 같은 문의를 다시 본다. 중복 방지는 기존
    ``notify_key`` 선점이 그대로 담당한다.
    """

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100002")
    add_draft(database, inquiry_id)
    reasons = ("GPT_REPORTED_UNRESOLVED", "GPT_WITHHELD_AUTO_POST")

    for _ in range(3):
        hold_notification(database, inquiry_id, reasons)

    assert len(events(outbox)) == 1


def test_a_changed_hold_reason_still_sends_only_one_message(
    database, outbox,
) -> None:
    """7. 보류 사유가 변경된 경우 -- 현재 구조의 확정된 동작을 고정한다.

    ``review-required:{inquiry_id}`` 는 초안 단계의 보류와 Eligibility 단계의
    보류가 **한 통**만 만들도록 일부러 공유하는 키다(두 호출부의 주석).
    그래서 사유가 바뀌어도 두 번째 알림은 가지 않는다. 사유 변경 시 재알림을
    원한다면 두 단계 중 어느 쪽을 알림 소유자로 둘지 먼저 정해야 하고,
    그것은 코드가 임의로 결정할 수 없다. 이 테스트는 그 선택이 내려지기 전의
    동작을 명시적으로 기록한다.
    """

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100003")
    add_draft(database, inquiry_id)

    hold_notification(database, inquiry_id, ("GPT_REPORTED_UNRESOLVED",))
    hold_notification(database, inquiry_id, ("RETURN_OR_DAMAGE_POLICY_REVIEW",))

    sent = events(outbox)
    assert len(sent) == 1, sent
    assert "미해결 문항 있음" in sent[0]["message"]


def test_nothing_is_sent_while_notification_is_disabled(
    database, outbox, monkeypatch,
) -> None:
    """9. 자동처리 OFF 상태에서 불필요한 알림 없음.

    자동처리가 꺼져 있으면 스케줄러가 이 경로에 도달하지 않는다. 그 위에
    발송 자체의 스위치도 독립적으로 닫힌다 -- 둘 중 하나만으로도 조용하다.
    """

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100004")
    add_draft(database, inquiry_id)
    monkeypatch.setenv("KAKAO_NOTIFY_ENABLED", "0")

    hold_notification(database, inquiry_id, ("GPT_REPORTED_UNRESOLVED",))

    assert events(outbox) == []


def test_an_untouched_backlog_inquiry_is_never_announced(
    database, outbox,
) -> None:
    """10. 과거 검토 대기 전체에 대한 일괄 알림 없음.

    알림은 파이프라인이 **이 문의를 처리한 결과**로만 나간다. 처리되지 않은
    문의는 호출 자체가 없으므로 대기열도 비어 있다.
    """

    seed_catalog(database)
    for index in range(3):
        coupang_inquiry(database, inquiry_id=f"1710001{index}")

    assert events(outbox) == []


# --- 11. 목적지 분리 ----------------------------------------------------------

def test_a_naver_hold_and_a_coupang_hold_reach_different_rooms(
    database, outbox,
) -> None:
    """11. 마켓·스토어별 알림 목적지 혼입 없음."""

    seed_catalog(database)
    coupang_id = coupang_inquiry(database, inquiry_id="17100020")
    add_draft(database, coupang_id)
    naver_id = naver_inquiry(database, external="900020")
    add_draft(database, naver_id)

    hold_notification(database, coupang_id, ("GPT_REPORTED_UNRESOLVED",))
    hold_notification(database, naver_id, ("GPT_REPORTED_UNRESOLVED",))

    rooms = [item["recipient"] for item in events(outbox)]
    assert rooms == [COUPANG_ROOM, NAVER_ROOM], rooms
    bodies = {item["recipient"]: item["message"] for item in events(outbox)}
    assert "쿠팡 등록: 안 됨" in bodies[COUPANG_ROOM]
    assert "네이버 등록: 안 됨" in bodies[NAVER_ROOM]
    assert "스토어: 오제앤에스" in bodies[COUPANG_ROOM]
    assert "오제앤에스" not in bodies[NAVER_ROOM]


@pytest.mark.parametrize("account", ["OJE_NS", "OJE_PLUS"])
def test_both_coupang_accounts_report_into_the_one_coupang_room(
    database, outbox, account,
) -> None:
    """계정은 메시지로 구분되고 방은 마켓으로 정해진다 -- 기존 설정 유지."""

    seed_catalog(database, account=account)
    inquiry_id = coupang_inquiry(
        database, account=account, inquiry_id="17100030",
    )
    add_draft(database, inquiry_id)

    hold_notification(database, inquiry_id, ("GPT_REPORTED_UNRESOLVED",))

    sent = events(outbox)[0]
    assert sent["recipient"] == COUPANG_ROOM
    expected = "오제앤에스" if account == "OJE_NS" else "오제플러스"
    assert f"스토어: {expected}" in sent["message"]


# --- 12. 발송 실패 기록 --------------------------------------------------------

def test_a_failed_enqueue_is_recorded_instead_of_disappearing(
    database, broken_outbox,
) -> None:
    """12. 카카오 발송 실패 시 오류 기록.

    ``notify_qna_safely`` 는 자신의 예외를 삼키고 False 를 돌려준다. 그래서
    호출부의 ``except`` 는 실행되지 않았고, 알리지 못한 보류는 콘솔 한 줄만
    남긴 뒤 사라졌다. ``on_error`` 훅이 그 한 줄을 DB 기록으로 만든다.
    """

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100040")
    add_draft(database, inquiry_id)

    hold_notification(database, inquiry_id, ("GPT_REPORTED_UNRESOLVED",))

    recorded = logs(database, inquiry_id, "KAKAO_NOTIFICATION_ENQUEUE_FAILED")
    assert recorded, "발송 실패가 기록되지 않았다"
    assert recorded[0]["level"] == "WARNING"
    assert not logs(database, inquiry_id, "KAKAO_NOTIFICATION_ENQUEUED")


# --- 14. 검토 대기 사유 한글화 --------------------------------------------------

def test_the_review_queue_shows_korean_reasons() -> None:
    """14. 세 코드가 운영자 문구로 표시되고, 판정 코드는 그대로다."""

    from answer.hold_reasons import STAFF_REASON_LABELS
    from services.dashboard_operations_service import _reason_bucket

    assert _reason_bucket("GPT_REPORTED_UNRESOLVED") == "미해결 문항 있음"
    assert _reason_bucket("GPT_WITHHELD_AUTO_POST") == "자동등록 보류 판단"
    assert (
        _reason_bucket("RETURN_OR_DAMAGE_POLICY_REVIEW")
        == "반품·교환·파손 직원 처리"
    )
    # 카카오 메시지와 같은 어휘를 읽는다 -- 사전이 하나다.
    for code in (
        "GPT_REPORTED_UNRESOLVED", "GPT_WITHHELD_AUTO_POST",
        "RETURN_OR_DAMAGE_POLICY_REVIEW",
    ):
        assert _reason_bucket(code) == STAFF_REASON_LABELS[code]


def test_the_existing_buckets_and_unknown_codes_are_unchanged() -> None:
    """기존 라벨과 미등록 코드의 동작은 건드리지 않는다."""

    from services.dashboard_operations_service import _reason_bucket

    assert _reason_bucket("INTENT_CONFIDENCE_LOW") == "낮은 신뢰도"
    assert _reason_bucket("EVIDENCE_CONFLICT") == "근거 충돌"
    assert _reason_bucket("ROUTE_ANYTHING") == "자동등록 불가 라우트"
    assert _reason_bucket("TOTALLY_UNKNOWN_CODE") == "TOTALLY_UNKNOWN_CODE"
    assert _reason_bucket("") == "기타"


# --- 13. Eligibility 불변 ------------------------------------------------------

def test_eligibility_reasons_are_untouched_by_the_display_change() -> None:
    """13. Eligibility 및 자동등록 조건 불변.

    표시 라벨은 ``dashboard_operations_service`` 안에만 있고, 판정은
    ``AutoProcessingEligibilityService`` 가 내리는 코드 그대로다.
    """

    import inspect

    from services import auto_processing_eligibility_service as policy

    source = inspect.getsource(policy)
    assert "STAFF_REASON_LABELS" not in source
    assert "_reason_bucket" not in source
    for code in (
        "GPT_REPORTED_UNRESOLVED", "GPT_WITHHELD_AUTO_POST",
        "RETURN_OR_DAMAGE_POLICY_REVIEW",
    ):
        assert code in source


# --- 15. 쿠팡 계정별 동기화 결과 ------------------------------------------------

def _scheduler(database: Database, coupang_sync):
    """스케줄러를 직접 만든다. 타이머는 돌지 않고 네이버 주기도 실행되지 않는다."""

    from services.naver_auto_sync_scheduler import NaverAutoSyncScheduler

    return NaverAutoSyncScheduler(
        database,
        timer_factory=lambda _seconds, _callback: None,
        coupang_sync=coupang_sync,
    )


def _sync_records(database: Database) -> list[dict]:
    with database.connection() as connection:
        return [
            dict(row) for row in connection.execute(
                """
                SELECT event_code, level, details_json FROM activity_logs
                WHERE event_code LIKE 'COUPANG_SYNC%' ORDER BY id
                """
            )
        ]


def test_each_coupang_account_records_its_own_sync_outcome(database) -> None:
    """15. 두 계정의 실행 결과가 각각 남고 서로 구분된다.

    기록은 수집 서비스가 아니라 스케줄러가 한다. 그 서비스는 읽기·저장 경로로
    묶여 있고(``test_the_service_imports_only_the_read_path`` 가 ``LogRepository``
    를 금지, ``test_a_cycle_writes_no_table_but_inquiries_and_the_queue`` 가
    activity_logs 기록을 금지), 그 경계는 그대로 둔다.
    """

    from services.coupang_inquiry_sync_service import CoupangInquirySyncResult

    results = [
        CoupangInquirySyncResult(
            account_code="OJE_NS", fetched=7, new=2, updated=1, unchanged=4,
            http_requests=1,
        ),
        CoupangInquirySyncResult(account_code="OJE_PLUS", error="READ_FAILED"),
    ]
    scheduler = _scheduler(database, lambda _db: results)

    scheduler._run_coupang_sync()

    rows = _sync_records(database)
    assert [row["event_code"] for row in rows] == [
        "COUPANG_SYNC_ACCOUNT_COMPLETED", "COUPANG_SYNC_ACCOUNT_FAILED",
    ], rows
    completed = json.loads(rows[0]["details_json"])
    assert completed["account_code"] == "OJE_NS"
    assert completed["fetched"] == 7 and completed["new"] == 2
    assert rows[0]["level"] == "INFO"
    failed = json.loads(rows[1]["details_json"])
    assert failed["account_code"] == "OJE_PLUS"
    assert failed["error"] == "READ_FAILED"
    assert failed["fetched"] == 0
    assert rows[1]["level"] == "ERROR"


def test_an_account_that_collected_nothing_is_distinguishable_from_a_failure(
    database,
) -> None:
    """성공했지만 신규 0건인 주기와 호출 실패를 구분할 수 있다.

    이것이 2026-10-29 사본에서 오제플러스에 대해 답할 수 없던 질문이다.
    """

    from services.coupang_inquiry_sync_service import CoupangInquirySyncResult

    scheduler = _scheduler(database, lambda _db: [
        CoupangInquirySyncResult(
            account_code="OJE_PLUS", http_requests=1, fetched=0, new=0,
        ),
    ])

    scheduler._run_coupang_sync()

    rows = _sync_records(database)
    assert [row["event_code"] for row in rows] == [
        "COUPANG_SYNC_ACCOUNT_COMPLETED"
    ]
    details = json.loads(rows[0]["details_json"])
    assert details["http_requests"] == 1 and details["fetched"] == 0
    assert details["error"] in ("", None)


def test_a_failed_coupang_cycle_is_recorded(database) -> None:
    """주기 자체가 예외로 끝난 경우도 남는다 -- 전에는 로거 한 줄뿐이었다."""

    def explode(_db):
        raise RuntimeError("boom")

    _scheduler(database, explode)._run_coupang_sync()

    rows = _sync_records(database)
    assert [row["event_code"] for row in rows] == ["COUPANG_SYNC_CYCLE_FAILED"]
    assert rows[0]["level"] == "ERROR"
    assert json.loads(rows[0]["details_json"])["error_type"] == "RuntimeError"


def test_a_sync_record_failure_never_fails_the_cycle(database, monkeypatch) -> None:
    """기록은 진단이다. 수집한 문의를 잃는 쪽이 더 나쁘다."""

    from services.coupang_inquiry_sync_service import CoupangInquirySyncResult

    scheduler = _scheduler(database, lambda _db: [
        CoupangInquirySyncResult(account_code="OJE_NS", fetched=1),
    ])
    monkeypatch.setattr(
        scheduler.logs, "record_system",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log down")),
    )

    scheduler._run_coupang_sync()

    assert _sync_records(database) == []


def test_the_sync_service_itself_still_writes_no_log(database) -> None:
    """경계가 유지된다는 증거: 수집 서비스는 여전히 로그를 쓰지 않는다."""

    import ast
    from pathlib import Path as _Path

    import services.coupang_inquiry_sync_service as sync_module

    source = _Path(sync_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "LogRepository" not in names
    assert "LogRepository" not in source


# ===========================================================================
# 1 ~ 4, 8. 성공 알림 유지 / 실패 알림 신설
# ===========================================================================

def _naver_store(_: str | None = None):
    from config import StoreConfig

    return StoreConfig("STORE", "오제플러스", "client-id", "client-secret", True)


def _approved_naver(database: Database, *, external_id: str = "12345") -> int:
    """등록 직전 상태의 승인된 Program Answer (기존 네이버 테스트와 같은 경로)."""

    from answer.models import AnswerResult, AnswerStatus
    from repositories.answer_repository import AnswerRepository
    from services.approval_service import ApprovalService

    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "STORE",
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external_id,
        "external_inquiry_id": external_id,
        "raw_json": {
            "source": "PRODUCT_INQUIRY",
            "questionId": external_id,
            "source_payload": {"questionId": external_id},
        },
        "content": "스탠드도 설치해주시나요?",
        "product_name": "삼성 4K UHD 스마트 비즈니스 TV LH50BEHHLGFXKR",
    }).inquiry_id
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="설치", reason="test",
            answer="스탠드 설치도 함께 진행해 드립니다.", provider="rules",
            auto_answerable=True, needs_review=False,
        ),
    )
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=int(draft["id"]), actor="approver",
    )
    return inquiry_id


class _RecordingNaverClient:
    def __init__(self, outcome=None) -> None:
        from api.naver_answer_client import NaverAnswerResponse

        self.requests: list[Any] = []
        self.outcome = outcome or NaverAnswerResponse(204, "response-1")

    def send(self, request, *, access_token):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _naver_service(database: Database, client, *, enabled: bool = True):
    from config import NaverPostSettings
    from services.naver_post_service import NaverPostService

    return NaverPostService(
        database,
        settings=NaverPostSettings(enabled=enabled),
        store_resolver=_naver_store,
        token_provider=lambda **kwargs: "mock-token",
        client=client,
    )


def test_a_posted_naver_answer_keeps_its_existing_notification(
    database, outbox,
) -> None:
    """1. 네이버 자동등록 성공 → 기존 성공 알림 유지.

    본문도 그대로다: 성공 알림에는 스토어·문의 ID 머리글이 붙지 않는다.
    """

    inquiry_id = _approved_naver(database, external_id="900101")
    result = _naver_service(database, _RecordingNaverClient()).post(
        inquiry_id, actor="SYSTEM_AUTO_POST", confirmed=True, automatic=True,
    )

    assert result.status == "POSTED", result
    sent = events(outbox)
    assert len(sent) == 1, sent
    assert sent[0]["title"] == "[네이버 Q&A 자동등록 완료]"
    assert sent[0]["recipient"] == NAVER_ROOM
    message = sent[0]["message"]
    # 등록된 본문은 머리말·맺음말이 붙은 최종 답변이다.
    assert "답변: " in message
    assert "스탠드 설치도 함께 진행해 드립니다." in message
    assert "스토어:" not in message
    assert "문의 ID:" not in message
    assert "등록 실패 사유:" not in message


def test_a_failed_naver_post_now_reports_the_failure(database, outbox) -> None:
    """3. 네이버 등록 실패 → 실패 알림.

    이 알림이 없던 것이 이번에 확인된 누락이다: 발송 이력 974건에 실패 제목이
    0건이었다.
    """

    from api.naver_answer_client import NaverAnswerClientError

    inquiry_id = _approved_naver(database, external_id="900102")
    client = _RecordingNaverClient(
        NaverAnswerClientError(
            "TARGET_NOT_FOUND", http_status=404, retryable=False,
        )
    )
    result = _naver_service(database, client).post(
        inquiry_id, actor="SYSTEM_AUTO_POST", confirmed=True, automatic=True,
    )

    assert result.status == "POST_FAILED", result
    sent = events(outbox)
    assert len(sent) == 1, sent
    assert sent[0]["title"] == "[네이버 Q&A 자동등록 실패]"
    assert sent[0]["recipient"] == NAVER_ROOM
    message = sent[0]["message"]
    assert "등록 실패 사유: TARGET_NOT_FOUND (HTTP 404)" in message
    assert "네이버 등록: 실패" in message
    assert "처리: 운영자 확인 후 재시도가 필요합니다." in message
    # 실패는 보류가 아니다.
    assert "미등록 사유:" not in message
    assert "등록: 안 됨" not in message


def test_a_manual_naver_failure_is_titled_for_a_person(database, outbox) -> None:
    """운영자가 직접 누른 등록의 실패는 자동등록 실패와 제목이 다르다."""

    from api.naver_answer_client import NaverAnswerClientError

    inquiry_id = _approved_naver(database, external_id="900103")
    client = _RecordingNaverClient(
        NaverAnswerClientError(
            "BAD_REQUEST", http_status=400, retryable=True,
        )
    )
    _naver_service(database, client).post(
        inquiry_id, actor="관리자", confirmed=True, automatic=False,
    )

    assert events(outbox)[0]["title"] == "[네이버 Q&A 답변 등록 실패]"


def test_an_uncertain_naver_post_never_says_it_failed(database, outbox) -> None:
    """등록 여부를 알 수 없는 결과는 실패라고 말하지 않는다.

    POST_UNKNOWN 은 재시도 차단 상태다. "실패" 라고 알리면 운영자가 중복
    등록을 하게 된다.
    """

    from api.naver_answer_client import NaverAnswerClientError

    inquiry_id = _approved_naver(database, external_id="900104")
    client = _RecordingNaverClient(
        NaverAnswerClientError(
            "NETWORK_ERROR", http_status=None, retryable=True, uncertain=True,
        )
    )
    result = _naver_service(database, client).post(
        inquiry_id, actor="SYSTEM_AUTO_POST", confirmed=True, automatic=True,
    )

    assert result.status == "POST_UNKNOWN", result
    sent = events(outbox)[0]
    assert sent["title"] == "[네이버 Q&A 등록 결과 확인 필요]"
    assert "확인 필요 사유: NETWORK_ERROR" in sent["message"]
    assert "네이버 등록 결과: 확인 필요" in sent["message"]
    assert "실패" not in sent["message"]


def _coupang_post(database: Database, inquiry_id: int, transport):
    from api.coupang_post_client import CoupangPostClient
    from services.coupang_post_service import CoupangPostService

    return CoupangPostService(
        database,
        client=CoupangPostClient(
            access_key="k", secret_key="s", transport=transport,
        ),
        account_resolver=lambda code: ACCOUNTS[str(code).upper()],
    ).post(inquiry_id, actor="관리자", confirmed=True)


def test_a_posted_coupang_answer_keeps_its_existing_notification(
    coupang_posting_opened, database, outbox,
) -> None:
    """2. 쿠팡 자동등록 성공 → 기존 성공 알림 유지."""

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100100")
    add_draft(database, inquiry_id)

    result = _coupang_post(database, inquiry_id, RecordingTransport())

    assert result.status == "POSTED", result
    sent = [item for item in events(outbox) if "등록 완료" in item["title"]]
    assert len(sent) == 1, events(outbox)
    assert sent[0]["title"] == "[쿠팡 Q&A 답변 등록 완료]"
    assert sent[0]["recipient"] == COUPANG_ROOM
    assert "스토어:" not in sent[0]["message"]


def test_a_failed_coupang_post_now_reports_the_failure(
    coupang_posting_opened, database, outbox,
) -> None:
    """4. 쿠팡 등록 실패 → 실패 알림."""

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100101")
    add_draft(database, inquiry_id)

    result = _coupang_post(
        database, inquiry_id,
        RecordingTransport(status=404, body={"code": "404", "message": "NOT FOUND"}),
    )

    assert result.status == "POST_FAILED", result
    sent = [item for item in events(outbox) if "실패" in item["title"]]
    assert len(sent) == 1, events(outbox)
    assert sent[0]["title"] == "[쿠팡 Q&A 답변 등록 실패]"
    assert sent[0]["recipient"] == COUPANG_ROOM
    message = sent[0]["message"]
    assert "스토어: 오제앤에스" in message
    assert f"문의 ID: {inquiry_id}" in message
    assert "등록 실패 사유: " in message
    assert "쿠팡 등록: 실패" in message


def test_a_manual_registration_does_not_duplicate_the_hold_message(
    coupang_posting_opened, database, outbox,
) -> None:
    """8. 운영자가 수동 등록한 경우 기존 알림과 중복되지 않는다.

    보류 알림과 등록 완료 알림은 서로 다른 사건이고 키도 다르다. 같은 사건이
    두 번 나가지 않는다는 것이 중복 방지이며, 두 사건을 한 통으로 합치는 것은
    아니다.
    """

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100102")
    add_draft(database, inquiry_id)

    hold_notification(database, inquiry_id, ("GPT_REPORTED_UNRESOLVED",))
    _coupang_post(database, inquiry_id, RecordingTransport())
    # 등록 이후 같은 보류 알림을 다시 시도해도 늘지 않는다.
    hold_notification(database, inquiry_id, ("GPT_REPORTED_UNRESOLVED",))

    titles = [item["title"] for item in events(outbox)]
    assert titles == [HOLD_TITLE, "[쿠팡 Q&A 답변 등록 완료]"], titles


def test_a_failed_coupang_notification_is_recorded(
    coupang_posting_opened, database, broken_outbox,
) -> None:
    """12(쿠팡). 실패 알림을 보내지 못한 것도 기록된다."""

    seed_catalog(database)
    inquiry_id = coupang_inquiry(database, inquiry_id="17100103")
    add_draft(database, inquiry_id)

    _coupang_post(
        database, inquiry_id,
        RecordingTransport(status=404, body={"code": "404", "message": "NOT FOUND"}),
    )

    assert logs(database, inquiry_id, "KAKAO_FAILED_NOTIFICATION_WARNING")
