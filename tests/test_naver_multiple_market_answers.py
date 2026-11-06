"""네이버 상품문의 한 건에 달린 답변 여러 개를 모두 수집·보존·표시한다 -- 694758083.

2026-11-05 문의 694758083 에는 네이버에 답변이 2건 있다. 15:45:36 챗봇 답변과
15:46:02 직원 추가 답변. 읽기 전용 GET /v1/contents/qnas 응답(아래 fixture)은
그 둘을 ``answers`` 배열로 주고, 단일 문자열 ``answer`` 에는 첫 번째만 담는다.
배열 항목에는 ``answer`` 와 ``createDate`` 뿐이고 답변 ID·작성자는 없다.

수집은 ``answer`` 만 읽었으므로 직원 추가 답변은 정규화 단계에서 사라졌고,
대시보드 '답변 이력' 에는 마켓 답변이 한 건만 보였다.

여기서 고정하는 것:

* ``answers`` 의 각 답변이 ``naver_market_answers`` 에 따로 남는다.
* 같은 답변의 재동기화는 행을 늘리지 않는다. 본문이 같아도 등록 시각이 다르면
  서로 다른 답변이다.
* 기존 단일 답변 슬롯(``naver_posted_answers`` -- Learning feedback 이 읽는 것),
  생성 원본, 답변 상태, Learning 은 그대로다.
* 작성자는 마켓이 주지 않으므로 '작성자 확인 불가' 다.
"""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from answer.models import AnswerResult, AnswerStatus
from config import NaverSyncSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_posted_answer_repository import (
    NaverMarketAnswerRepository,
    NaverPostedAnswerRepository,
)
from services.naver_inquiry_normalizer import InquiryNormalizer
from services.naver_inquiry_sync_service import NaverInquirySyncService
from tests.migration_contract import assert_fresh_schema
from ui.answer_history_presenter import (
    AUTHOR_UNKNOWN,
    MARKET_ANSWER,
    NAVER_ANSWER,
    NAVER_EARLIER_ANSWER,
    build_answer_history,
)

FIXTURE = Path(__file__).parent / "fixtures" / "naver_product_qna_694758083.json"
REAL_ITEM = json.loads(FIXTURE.read_text(encoding="utf-8"))["item"]
CHATBOT = REAL_ITEM["answers"][0]["answer"]
STAFF = REAL_ITEM["answers"][1]["answer"]


def _settings() -> NaverSyncSettings:
    return NaverSyncSettings(
        enabled=True, lookback_days=7, page_size=100, max_pages=2,
        connect_timeout=1.0, read_timeout=2.0, max_retries=0,
        retry_backoff_seconds=0.0, max_runtime_seconds=30.0,
        lock_ttl_seconds=60,
    )


def _sync(database: Database, payload: dict, *, customer: dict | None = None) -> None:
    service = NaverInquirySyncService(
        database,
        settings=_settings(),
        token_provider=lambda **kwargs: "read-token",
        product_fetch=lambda **kwargs: {
            "contents": [payload] if payload else [],
            "totalPages": 1, "totalElements": 1 if payload else 0, "last": True,
        },
        customer_fetch=lambda **kwargs: {
            "content": [customer] if customer else [],
            "totalPages": 1, "last": True,
        },
    )
    end = datetime(2026, 11, 6, tzinfo=UTC)
    result = service.sync_inquiries(
        stores=[StoreConfig("OJE_PLUS", "오제플러스", "client", "secret", True)],
        inquiry_types=(
            ["PRODUCT_INQUIRY", "CUSTOMER_INQUIRY"] if customer
            else ["PRODUCT_INQUIRY"]
        ),
        from_datetime=end - timedelta(days=7),
        to_datetime=end,
    )
    assert result.status == "SUCCESS", result


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "market-answers.db")
    assert_fresh_schema(database)
    return database


def _item(*answers: tuple[str, str | None]) -> dict:
    """The real 694758083 item with its ``answers`` replaced."""

    item = copy.deepcopy(REAL_ITEM)
    item["answers"] = [
        {"answer": body, **({"createDate": created} if created else {})}
        for body, created in answers
    ]
    item["answer"] = answers[0][0] if answers else None
    item["answered"] = bool(answers)
    return item


def _inquiry_id(database: Database) -> int:
    return int(InquiryRepository(database).list()[0]["id"])


def _rows(database: Database) -> list[dict]:
    return NaverMarketAnswerRepository(database).history(_inquiry_id(database))


def _draft(database: Database, inquiry_id: int, text: str) -> dict:
    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="GENERAL", reason="test",
            answer=text, provider="rules", auto_answerable=True,
            needs_review=False,
        ),
    )


def _history(database: Database) -> list:
    inquiry_id = _inquiry_id(database)
    inquiry = InquiryRepository(database).get(inquiry_id)
    posted = NaverPostedAnswerRepository(database).current(inquiry_id) or {}
    return list(build_answer_history(
        inquiry,
        AnswerRepository(database).history_for_inquiry(inquiry_id),
        market_answer=str(posted.get("answer_body") or ""),
        market_answers=NaverMarketAnswerRepository(database).history(inquiry_id),
    ).entries)


# --- 694758083 실제 응답 ----------------------------------------------------

def test_the_real_response_lists_both_answers_and_answer_holds_the_first():
    """fixture 자체가 원인이다: ``answer`` 는 첫 번째 답변뿐이다."""

    assert REAL_ITEM["answer"] == CHATBOT
    assert len(REAL_ITEM["answers"]) == 2
    assert all(set(item) == {"answer", "createDate"} for item in REAL_ITEM["answers"])


def test_694758083_keeps_both_answers(tmp_path):
    database = _database(tmp_path)
    _sync(database, REAL_ITEM)

    rows = _rows(database)
    assert [row["answer_body"] for row in rows] == [CHATBOT, STAFF]
    assert [row["market_created_at"] for row in rows] == [
        "2026-11-05T15:45:36.897+09:00", "2026-11-05T15:46:02.298+09:00",
    ]
    assert all(row["author_type"] is None for row in rows)
    assert all(row["market_answer_id"] is None for row in rows)


def test_694758083_history_shows_generated_and_both_market_answers(tmp_path):
    database = _database(tmp_path)
    _sync(database, _item())  # 미답변으로 먼저 수집 -> 생성
    inquiry_id = _inquiry_id(database)
    draft = _draft(database, inquiry_id, CHATBOT)
    _sync(database, REAL_ITEM)

    entries = _history(database)
    labels = [entry.label for entry in entries]
    assert labels == ["최초 생성 답변", f"{NAVER_ANSWER} 1", f"{NAVER_ANSWER} 2 (최신)"]
    assert entries[1].body == CHATBOT and entries[2].body == STAFF
    assert MARKET_ANSWER not in labels  # 같은 마켓 답변을 한 번 더 넣지 않는다
    assert AUTHOR_UNKNOWN in entries[2].note
    assert "2026-11-05" in entries[2].note
    # 생성 원본은 그대로
    assert AnswerRepository(database).get(draft["id"])["original_answer"] == CHATBOT


def test_the_single_posted_answer_slot_is_unchanged(tmp_path):
    """Learning feedback 이 읽는 슬롯은 여전히 ``answer`` (첫 번째 답변) 하나다."""

    database = _database(tmp_path)
    _sync(database, REAL_ITEM)

    inquiry_id = _inquiry_id(database)
    posted = NaverPostedAnswerRepository(database)
    assert posted.current(inquiry_id)["answer_body"] == CHATBOT
    assert len(posted.history(inquiry_id)) == 1


# --- 12개 필수 사례 -----------------------------------------------------------

def test_one_answer_only(tmp_path):
    database = _database(tmp_path)
    _sync(database, _item((CHATBOT, "2026-11-05T15:45:36.897+09:00")))

    assert [row["answer_body"] for row in _rows(database)] == [CHATBOT]
    assert [e.label for e in _history(database)] == [f"{NAVER_ANSWER} 1"]


def test_several_staff_answers_are_all_kept_in_order(tmp_path):
    database = _database(tmp_path)
    _sync(database, _item(
        (CHATBOT, "2026-11-05T15:45:36.897+09:00"),
        (STAFF, "2026-11-05T15:46:02.298+09:00"),
        ("추가 안내드립니다. 수거는 설치 당일 진행됩니다.", "2026-11-05T17:10:00.000+09:00"),
    ))

    labels = [e.label for e in _history(database)]
    assert labels == [f"{NAVER_ANSWER} 1", f"{NAVER_ANSWER} 2", f"{NAVER_ANSWER} 3 (최신)"]


def test_an_answer_whose_body_changed_keeps_both_bodies(tmp_path):
    """같은 등록 시각의 본문이 바뀌면 두 본문을 모두 남기되 수정이라고 단정하지 않는다."""

    database = _database(tmp_path)
    created = "2026-11-05T15:46:02.298+09:00"
    _sync(database, _item((CHATBOT, "2026-11-05T15:45:36.897+09:00"), (STAFF, created)))
    edited = STAFF + "\n감사합니다."
    _sync(database, _item((CHATBOT, "2026-11-05T15:45:36.897+09:00"), (edited, created)))

    rows = _rows(database)
    assert [row["answer_body"] for row in rows].count(STAFF) == 1
    assert [row["answer_body"] for row in rows].count(edited) == 1
    entries = _history(database)
    earlier = [e for e in entries if e.label == NAVER_EARLIER_ANSWER]
    assert [e.body for e in earlier] == [STAFF]
    assert "최근 조회에서 보이지 않음" in earlier[0].note
    assert all("수정" not in e.label for e in entries)


def test_an_answer_added_after_the_inquiry_was_already_answered(tmp_path):
    database = _database(tmp_path)
    _sync(database, _item((CHATBOT, "2026-11-05T15:45:36.897+09:00")))
    first = _rows(database)[0]
    _sync(database, REAL_ITEM)

    rows = _rows(database)
    assert [row["answer_body"] for row in rows] == [CHATBOT, STAFF]
    assert rows[0]["first_observed_at"] == first["first_observed_at"]
    assert rows[1]["first_observed_at"] >= first["first_observed_at"]
    assert InquiryRepository(database).get(_inquiry_id(database))["answer_status"] == "ANSWERED"


def test_repeated_sync_does_not_duplicate(tmp_path):
    database = _database(tmp_path)
    for _ in range(3):
        _sync(database, REAL_ITEM)

    rows = _rows(database)
    assert len(rows) == 2
    assert rows[0]["last_observed_at"] >= rows[0]["first_observed_at"]
    assert [e.label for e in _history(database)].count(f"{NAVER_ANSWER} 1") == 1


def test_identical_bodies_on_different_answers_stay_separate(tmp_path):
    database = _database(tmp_path)
    same = "확인 후 다시 안내드리겠습니다."
    _sync(database, _item(
        (same, "2026-11-05T15:45:36.897+09:00"),
        (same, "2026-11-05T16:00:00.000+09:00"),
    ))

    assert [row["answer_body"] for row in _rows(database)] == [same, same]


def test_no_author_is_shown_as_unknown_not_as_staff(tmp_path):
    database = _database(tmp_path)
    _sync(database, REAL_ITEM)

    for entry in _history(database):
        assert AUTHOR_UNKNOWN in entry.note
        assert "직원" not in entry.note and "챗봇" not in entry.note


def test_answers_without_id_or_time_are_kept_by_position(tmp_path):
    database = _database(tmp_path)
    _sync(database, _item((CHATBOT, None), (STAFF, None)))
    _sync(database, _item((CHATBOT, None), (STAFF, None)))

    rows = _rows(database)
    assert [row["answer_key"] for row in rows] == ["POSITION:1", "POSITION:2"]
    assert [row["answer_body"] for row in rows] == [CHATBOT, STAFF]


def test_a_response_without_answers_behaves_as_before(tmp_path):
    """``answers`` 가 없는 응답(구형/고객문의)은 새 테이블을 건드리지 않는다."""

    database = _database(tmp_path)
    legacy = copy.deepcopy(REAL_ITEM)
    legacy.pop("answers")
    _sync(database, legacy)

    assert _rows(database) == []
    inquiry_id = _inquiry_id(database)
    assert NaverPostedAnswerRepository(database).current(inquiry_id)["answer_body"] == CHATBOT
    assert [e.label for e in _history(database)] == [MARKET_ANSWER]


def test_naver_customer_inquiries_and_coupang_are_untouched():
    """고객문의 응답 구조는 확인하지 않았으므로 답변 목록을 만들지 않는다."""

    customer = {
        "inquiryNo": 90001, "inquiryTitle": "배송", "inquiryContent": "언제 오나요?",
        "answered": True, "answerContent": "내일 도착 예정입니다.",
        "answerContentId": 1, "inquiryRegistrationDateTime": "2026-11-05T10:00:00",
        "answers": [{"answer": "가정한 배열", "createDate": "2026-11-05T11:00:00"}],
    }
    item = InquiryNormalizer().customer(customer, store_code="OJE_PLUS").to_work_item()
    assert item["posted_answers"] == []
    assert item["seller_answer"] == "내일 도착 예정입니다."

    import services.coupang_inquiry_sync_service as coupang_sync

    source = Path(coupang_sync.__file__).read_text(encoding="utf-8")
    # 쿠팡 동기화는 InquirySyncService.sync 를 쓰지 않는다
    # (test_coupang_operational_inquiry_sync 가 고정). 새 저장소도 쓰지 않는다.
    assert "NaverMarketAnswerRepository" not in source


def test_the_added_answer_triggers_no_generation_learning_or_state_change(tmp_path):
    database = _database(tmp_path)
    _sync(database, _item())  # 미답변으로 수집 -> 생성
    inquiry_id = _inquiry_id(database)
    _draft(database, inquiry_id, CHATBOT)
    _sync(database, _item((CHATBOT, "2026-11-05T15:45:36.897+09:00")))

    def snapshot():
        with database.connection() as connection:
            return {
                "drafts": connection.execute(
                    "SELECT COUNT(*) FROM answer_drafts").fetchone()[0],
                "learning": connection.execute(
                    "SELECT COUNT(*) FROM learning_examples").fetchone()[0],
                "versions": connection.execute(
                    "SELECT COUNT(*) FROM answer_versions").fetchone()[0],
                "status": tuple(connection.execute(
                    "SELECT workflow_status, answer_status, post_status"
                    " FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()),
                "originals": [r[0] for r in connection.execute(
                    "SELECT original_answer FROM answer_drafts ORDER BY id")],
            }

    before = snapshot()
    _sync(database, REAL_ITEM)
    assert snapshot() == before
    assert len(_rows(database)) == 2


def test_a_capture_failure_does_not_fail_the_sync(tmp_path, monkeypatch):
    database = _database(tmp_path)

    def broken(self, **kwargs):
        raise RuntimeError("disk")

    monkeypatch.setattr(NaverMarketAnswerRepository, "observe_all", broken)
    _sync(database, REAL_ITEM)

    inquiry_id = _inquiry_id(database)
    assert NaverPostedAnswerRepository(database).current(inquiry_id)["answer_body"] == CHATBOT
    with database.connection() as connection:
        codes = [r[0] for r in connection.execute(
            "SELECT event_code FROM activity_logs WHERE inquiry_id=?", (inquiry_id,))]
    assert "NAVER_MARKET_ANSWERS_CAPTURE_FAILED" in codes
