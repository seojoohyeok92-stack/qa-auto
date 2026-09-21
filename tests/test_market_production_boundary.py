"""Which marketplaces production acts on, and which it only collects.

A Coupang inquiry reached a real chat room: the auto-post queue was not scoped
by market, so a Coupang question entered the answer pipeline, was held for
review, and the hold notification went out reading "네이버 등록: 안 됨" for
something nobody asked on Naver.

Two things are pinned here, and neither is the list of markets in scope.  The
scope is applied in SQL, before the ordering and the LIMIT, so a market
outside it is never offered no matter how many rows it has.  And a
notification names the market it is actually about.

Coupang has since been admitted to that scope, on its own decision, and is
notified in its own room.  The defect was never that a particular market was
in the queue; it was a queue that had no scope at all, and a message that
assumed the only market there could be.
"""

from __future__ import annotations

import pytest

import kakao_notify
from kakao_notify import format_qna_message, notify_qna_safely, recipient_for_market
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.market_policy import (
    KAKAO_MARKETS,
    POST_MARKETS,
    is_store_automatic_post_enabled,
    is_store_post_enabled,
    market_of,
    post_enabled_store_codes,
    store_display_name,
)

NAVER_STORE = "OJE_PLUS"
COUPANG_NS = "COUPANG_OJE_NS"
COUPANG_PLUS = "COUPANG_OJE_PLUS"


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "market-boundary.db")
    database.initialize()
    return database


def _inquiry(database: Database, *, store_code: str, question_id: str) -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": store_code,
        "source_type": (
            "COUPANG_ONLINE_INQUIRY"
            if store_code.startswith("COUPANG_")
            else "PRODUCT_INQUIRY"
        ),
        "source_question_id": question_id,
        "external_inquiry_id": question_id,
        "title": "문의",
        "content": "와이파이가안됩니다 기계치라 자세한 설명 부탁드립니다",
        "registered_at": "2026-09-16T10:00:00+09:00",
        "source_answered": 0,
        "raw_json": {},
    }).inquiry_id


# --- market detection ------------------------------------------------------

@pytest.mark.parametrize(
    "store_code,market",
    [
        pytest.param(NAVER_STORE, "NAVER", id="naver"),
        pytest.param(COUPANG_NS, "COUPANG", id="coupang-ns"),
        pytest.param(COUPANG_PLUS, "COUPANG", id="coupang-plus"),
    ],
)
def test_store_codes_map_to_markets(store_code, market) -> None:
    assert market_of(store_code) == market


def test_post_markets_is_now_only_the_read_only_rule() -> None:
    """What is left of it: which answered inquiries may still be edited.

    The auto-post queue was split out into ``AUTOMATIC_POST_MARKETS``, so
    adding a market here no longer means "may be posted to" -- it means the
    answer path and the screens stop treating an answered inquiry as final.
    """

    assert POST_MARKETS == frozenset({"NAVER"})
    assert is_store_post_enabled(NAVER_STORE) is True
    assert is_store_post_enabled(COUPANG_NS) is False
    assert is_store_post_enabled(COUPANG_PLUS) is False
    assert is_store_automatic_post_enabled(COUPANG_NS) is True


def test_both_answerable_markets_are_notified() -> None:
    """A market a person can register to is a market worth telling them about."""

    assert KAKAO_MARKETS == frozenset({"NAVER", "COUPANG"})


def test_the_queue_scope_is_the_markets_it_may_post_to() -> None:
    """Both answerable markets are in scope now, and a blank code never is."""

    assert post_enabled_store_codes(
        [NAVER_STORE, COUPANG_NS, COUPANG_PLUS]
    ) == [NAVER_STORE, COUPANG_NS, COUPANG_PLUS]
    assert post_enabled_store_codes(["", None, "   "]) == []


# --- the queue that caused it ----------------------------------------------

def test_the_auto_post_queue_is_scoped_by_market_not_unscoped(tmp_path) -> None:
    """The root cause was candidates() having no market scope at all.

    The scope is still applied in SQL; what it admits has changed now that a
    Coupang answer may be posted.  A market outside it is still never offered.
    """

    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n1")
    _inquiry(database, store_code=COUPANG_NS, question_id="c1")
    _inquiry(database, store_code=COUPANG_PLUS, question_id="c2")
    repository = AutoPostRepository(database)

    scoped = repository.candidates(
        max_retries=3,
        store_codes=post_enabled_store_codes(repository.distinct_store_codes()),
    )

    # The scope is still applied in SQL; it now admits both answerable
    # markets, and an empty scope still selects nothing at all.
    assert {row["store_code"] for row in scoped} == {
        NAVER_STORE, COUPANG_NS, COUPANG_PLUS
    }
    assert repository.candidates(max_retries=3, store_codes=[]) == []


def test_the_scope_is_applied_in_sql_before_the_limit(tmp_path) -> None:
    """Ordered by arrival and cut by LIMIT: filtering must happen in SQL.

    A scope that excluded the only Naver row would leave the page empty
    rather than silently returning the rows it was told to skip.
    """

    database = _database(tmp_path)
    for index in range(20):
        _inquiry(database, store_code=COUPANG_NS, question_id=f"c{index}")
    _inquiry(database, store_code=NAVER_STORE, question_id="n-last")
    repository = AutoPostRepository(database)

    rows = repository.candidates(
        max_retries=3, limit=5, store_codes=[NAVER_STORE],
    )
    assert [row["source_question_id"] for row in rows] == ["n-last"]


def test_an_empty_market_scope_selects_nothing(tmp_path) -> None:
    database = _database(tmp_path)
    _inquiry(database, store_code=NAVER_STORE, question_id="n1")
    assert AutoPostRepository(database).candidates(
        max_retries=3, store_codes=[]
    ) == []


# --- the notification boundary ---------------------------------------------

@pytest.fixture
def outbox(monkeypatch):
    """Record what would have been sent.  Nothing leaves the process."""

    sent: list[dict] = []
    # conftest disables Kakao for the whole suite so nothing can escape.  This
    # fixture turns it back on for one test at a time and replaces the outbox
    # writer, so the gate is exercised for real while the message still goes
    # nowhere.
    monkeypatch.setenv("KAKAO_NOTIFY_ENABLED", "1")
    monkeypatch.setattr(
        kakao_notify, "enqueue_kakao_message",
        lambda **kwargs: sent.append(kwargs) or kakao_notify.OUTBOX,
    )
    monkeypatch.setattr(kakao_notify, "_claim_notification", lambda **_k: True)
    monkeypatch.setattr(kakao_notify, "_mark_notification_sent", lambda *_a: None)
    monkeypatch.setattr(kakao_notify, "_validate_kakao_service", lambda: None)
    return sent


@pytest.mark.parametrize("store_code", [COUPANG_NS, COUPANG_PLUS])
def test_a_coupang_inquiry_reaches_only_the_coupang_room(outbox, store_code) -> None:
    result = notify_qna_safely(
        title="[Q&A 미등록 / 직원 확인 필요]",
        store_code=store_code,
        product="-",
        option_name="",
        question="와이파이가안됩니다",
        answer="-",
        action="needs_review",
        inquiry_id=f"c1-{store_code}",
    )
    assert result is True
    assert len(outbox) == 1
    # Both accounts resolve to the one Coupang room, never Naver's.
    assert outbox[0]["market"] == "COUPANG"
    assert recipient_for_market(outbox[0]["market"]) == recipient_for_market("COUPANG")
    assert recipient_for_market(outbox[0]["market"]) != recipient_for_market("NAVER")


def test_naver_inquiry_still_notifies(outbox) -> None:
    result = notify_qna_safely(
        title="[Q&A 미등록 / 직원 확인 필요]",
        store_code=NAVER_STORE,
        product="32인치 모니터",
        option_name="",
        question="와이파이 연결 방법",
        answer="-",
        action="needs_review",
        inquiry_id="n1",
        hold_reason="직원 확인 필요",
        hold_codes=("GPT_EVIDENCE_REQUIRED",),
    )
    assert result is True
    assert len(outbox) == 1
    assert "네이버 등록: 안 됨" in outbox[0]["message"]


def test_a_caller_that_names_no_store_keeps_the_naver_behaviour(outbox) -> None:
    """Legacy call sites default to Naver, which is the only enabled market."""

    assert notify_qna_safely(
        title="[네이버 Q&A 답변 등록 완료]", product="상품", option_name="",
        question="질문", answer="답변", action="posted", inquiry_id="n2",
    ) is True
    assert len(outbox) == 1


# --- recipients ------------------------------------------------------------

def test_each_market_has_its_own_room(monkeypatch) -> None:
    monkeypatch.delenv("KAKAO_QNA_RECIPIENT", raising=False)
    monkeypatch.delenv("KAKAO_COUPANG_QNA_RECIPIENT", raising=False)
    assert recipient_for_market("NAVER") == "오제 네이버 자동답변 확인방"
    assert recipient_for_market("COUPANG") == "오제 쿠팡 자동답변 확인방"
    assert recipient_for_market(None) == "오제 네이버 자동답변 확인방"


def test_both_coupang_accounts_share_one_room() -> None:
    assert (
        recipient_for_market(market_of(COUPANG_NS))
        == recipient_for_market(market_of(COUPANG_PLUS))
    )


def test_a_coupang_notification_would_never_go_to_the_naver_room() -> None:
    assert recipient_for_market("COUPANG") != recipient_for_market("NAVER")


def test_env_overrides_the_room(monkeypatch) -> None:
    monkeypatch.setenv("KAKAO_COUPANG_QNA_RECIPIENT", "다른 쿠팡 방")
    assert recipient_for_market("COUPANG") == "다른 쿠팡 방"


# --- message wording -------------------------------------------------------

def _held(market=None, product="상품명 예시"):
    return format_qna_message(
        market=market, product=product, option_name="",
        question="와이파이가안됩니다", answer="-", action="needs_review",
        hold_reason="직원 확인 필요", hold_codes=("GPT_EVIDENCE_REQUIRED",),
    )


def test_naver_message_keeps_naming_naver() -> None:
    assert "네이버 등록: 안 됨" in _held()
    assert "네이버 등록: 안 됨" in _held("NAVER")


def test_coupang_message_names_coupang() -> None:
    message = _held("COUPANG")
    assert "쿠팡 등록: 안 됨" in message
    assert "네이버 등록" not in message


def test_market_label_is_the_only_thing_that_changes() -> None:
    naver = _held("NAVER")
    coupang = _held("COUPANG")
    for line in ("상품명: 상품명 예시", "질문: 와이파이가안됩니다", "미등록 사유:", "답변: -"):
        assert line in naver
        assert line in coupang
    assert naver.replace("네이버 등록", "MARKET 등록") == coupang.replace(
        "쿠팡 등록", "MARKET 등록"
    )


def test_internal_store_codes_never_appear_in_a_message() -> None:
    message = _held("COUPANG")
    for code in (COUPANG_NS, COUPANG_PLUS, NAVER_STORE):
        assert code not in message
    assert store_display_name(COUPANG_NS) == "쿠팡"


def test_a_real_product_name_is_carried_through_when_there_is_one() -> None:
    """Phase C: the label is ready; the name still has to be supplied."""

    assert "상품명: 삼성 32인치 모니터" in _held("COUPANG", "삼성 32인치 모니터")
