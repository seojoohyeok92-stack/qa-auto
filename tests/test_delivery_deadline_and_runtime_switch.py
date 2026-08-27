"""Two operational faults: a promise nobody could keep, and a switch that forgot.

**A deadline the system cannot confirm.** "혹시 오늘 주문하면 9일까지 받아볼 수
있을까요?" was answered with the standing visit-installation policy -- 결제 확인
후 설치 기사님 일정에 맞춰 진행됩니다 -- and auto-posted. Every word of that is
true and none of it says whether the ninth is possible. It reached the customer
because the shipping rule matched on "주문하면" alone, and because nothing
downstream distinguishes an answer about the right topic from an answer to the
question: the validator passed it, semantic coverage returned UNKNOWN, atomic
completeness recorded it as undetermined rather than unresolved, and eligibility
found no blocking reason.

There is no basis to confirm a date for an order that does not exist yet -- no
order, so no DPS schedule, and standing policy states no lead time. So the rule
engine declines instead of substituting the policy, and eligibility refuses to
publish any answer to a deadline question that is not backed by a trusted
schedule, whichever route produced it. An existing order with a SUCCESS lookup
and a validated snapshot keeps the schedule routes it already had.

**A switch that forgot it had already run.** The persisted operator switch is
forced OFF once per process at startup, so a restarted server never resumes
customer-facing posting with nobody watching. That guard remembered which
databases it had already handled in a module-level set -- and Streamlit reloads
changed modules inside the running process, which re-executes the module top
level and empties the set. The guard then believed it had never run, and forced
the operator's ON back to OFF in the same process, with no restart and nobody
pressing Stop.

The gate itself is unchanged: a genuinely new interpreter still forces OFF. Only
the guard's memory moved somewhere a module reload cannot clear.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from answer.text_utils import is_delivery_deadline_question
from repositories.auto_post_repository import AutoPostRepository
from repositories.database import Database
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.auto_post_runtime_service import AutoPostRuntimeService
import services.naver_auto_post_scheduler as scheduler_module


# ==========================================================================
# P0-A.1  The question predicate
# ==========================================================================


@pytest.mark.parametrize("question", [
    "혹시 오늘 주문하면 9일까지 받아볼 수 있을까요?",
    "지금 주문하면 금요일까지 오나요?",
    "이번 주 안에 받을 수 있나요?",
    "다음 주 화요일 전까지 설치 가능한가요?",
    "오늘 구매하면 특정 날짜까지 배송되나요?",
    "25일까지 꼭 받아야 하는데 가능한가요?",
    "9월 5일까지 설치 받을 수 있나요?",
    "오늘 tv구매하면 19일 이전에 받을 수 있나요?",
])
def test_a_named_deadline_is_recognised(question) -> None:
    assert is_delivery_deadline_question(question)


@pytest.mark.parametrize("question", [
    # Standing policy answers all of these, and must keep answering them.
    "방문설치 상품은 어떻게 배송되나요?",
    "설치 일정은 어떻게 안내받나요?",
    "배송은 보통 며칠 걸리나요?",
    "주문하면 바로 배송되나요",
    "도서산간도 배송되나요?",
    "언제설치가능한가요?",
    "배송 예정일 알려주세요",
    "HDMI 단자가 몇 개인가요?",
    # Asks *when* it arrives, which is the ordinary schedule question the
    # existing routes already answer -- not a date proposed by the customer.
    "언제까지 배송되나요?",
    # A complaint about how long something has already dragged on. "까지도"
    # is not a deadline.
    "as 취소하고 케이블 불렀는데 오늘까지도 소리가 났다 안났다 합니다",
])
def test_an_ordinary_delivery_question_is_not_a_deadline(question) -> None:
    assert not is_delivery_deadline_question(question)


# ==========================================================================
# P0-A.2  Eligibility refuses to publish an unconfirmable deadline
# ==========================================================================


DEADLINE_INQUIRY = {
    "id": 1, "title": "상품 문의",
    "content": "혹시 오늘 주문하면 9일까지 받아볼 수 있을까요?",
    "source_answered": False, "post_status": "NONE",
}
POLICY_INQUIRY = {
    "id": 2, "title": "상품 문의", "content": "배송은 보통 며칠 걸리나요?",
    "source_answered": False, "post_status": "NONE",
}
POLICY_ANSWER = (
    "방문 설치 상품은 결제 확인 후 설치 기사님 일정에 맞춰 배송·설치가 "
    "진행됩니다. 설치 일정 관련 알림톡은 결제 후 수취인의 카카오톡으로 "
    "발송됩니다."
)


def draft(*, plan: dict | None = None, answer: str = POLICY_ANSWER) -> dict:
    return {
        "original_answer": answer,
        "validation_status": "PASS",
        "validator_result_json": {"passed": True},
        "review_status": "PENDING",
        "posted": False,
        "metadata_json": {"processing_plan": {"analysis": {}, **(plan or {})}},
    }


def evaluate(inquiry, drafted, route="TEMPLATE"):
    return AutoProcessingEligibilityService().evaluate(
        inquiry=inquiry, draft=drafted, route=route,
    )


def test_the_reported_case_is_no_longer_auto_postable() -> None:
    """The exact production inquiry, with the exact answer it received."""

    decision = evaluate(DEADLINE_INQUIRY, draft())

    assert decision.decision == "REVIEW_REQUIRED"
    assert "DELIVERY_DEADLINE_NOT_CONFIRMABLE" in decision.reasons
    assert decision.safe is False


@pytest.mark.parametrize("route", ["TEMPLATE", "GPT_FALLBACK", "SAFE_RULE"])
def test_no_route_may_publish_an_unconfirmable_deadline(route) -> None:
    """The same unanswerable question also reaches GPT, so the gate is global."""

    decision = evaluate(DEADLINE_INQUIRY, draft(), route=route)

    assert "DELIVERY_DEADLINE_NOT_CONFIRMABLE" in decision.reasons


def test_a_general_delivery_policy_question_still_auto_posts() -> None:
    decision = evaluate(POLICY_INQUIRY, draft())

    assert decision.decision == "SAFE"
    assert decision.reasons == ()


def test_a_confirmed_schedule_answers_the_deadline_normally() -> None:
    """An existing order whose schedule was actually looked up is untouched."""

    decision = evaluate(
        DEADLINE_INQUIRY,
        draft(plan={
            "requires_order_lookup": True,
            "order_id_status": "VALID",
            "order_lookup_status": "SUCCESS",
            "requires_dps_lookup": True,
            "dps_lookup_status": "SUCCESS",
            "valid_dps_snapshot_available": True,
        }),
        route="DELIVERY_WITH_INSTALLATION_DATE",
    )

    assert decision.decision == "SAFE"
    assert "DELIVERY_DEADLINE_NOT_CONFIRMABLE" not in decision.reasons


def test_a_lookup_that_did_not_land_still_blocks_the_deadline() -> None:
    """SUCCESS without a validated snapshot is not a confirmed schedule."""

    decision = evaluate(
        DEADLINE_INQUIRY,
        draft(plan={
            "requires_dps_lookup": True,
            "dps_lookup_status": "SUCCESS",
            "valid_dps_snapshot_available": False,
        }),
        route="GPT_FALLBACK",
    )

    assert "DELIVERY_DEADLINE_NOT_CONFIRMABLE" in decision.reasons


def test_the_reason_is_a_hard_block_not_a_recorded_note() -> None:
    decision = evaluate(DEADLINE_INQUIRY, draft())

    assert "DELIVERY_DEADLINE_NOT_CONFIRMABLE" not in decision.soft_reasons


# ==========================================================================
# P0-A.3  The rule engine stops substituting the policy for the answer
# ==========================================================================


def test_the_rule_engine_declines_a_deadline_question() -> None:
    from answer.engine import AnswerEngine

    engine = AnswerEngine()
    result = engine.answer(
        "삼성 125.7cm(50인치) UHD 4K 비즈니스TV LH50BEFHLGFXKR 스탠드형",
        "혹시 오늘 주문하면 9일까지 받아볼 수 있을까요?",
    )

    text = str(getattr(result, "answer", "") or "")
    assert "기사님 일정에 맞춰" not in text, (
        "the standing policy must not stand in for a date commitment"
    )


def test_the_rule_engine_still_answers_the_general_question() -> None:
    from answer.engine import AnswerEngine

    engine = AnswerEngine()
    result = engine.answer(
        "삼성 125.7cm(50인치) UHD 4K 비즈니스TV LH50BEFHLGFXKR 스탠드형",
        "배송은 보통 며칠 걸리나요?",
    )

    assert str(getattr(result, "answer", "") or "").strip()


# ==========================================================================
# P0-B  The operator switch survives a module reload
# ==========================================================================


@pytest.fixture
def runtime_database(tmp_path, monkeypatch) -> Database:
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    database = Database(tmp_path / "runtime.db")
    database.initialize()
    return database


@pytest.fixture(autouse=True)
def _isolate_startup_registry():
    """Each test gets its own view of what this interpreter has already reset."""

    previous = getattr(sys, "_oje_auto_post_startup_reset_done", None)
    if hasattr(sys, "_oje_auto_post_startup_reset_done"):
        delattr(sys, "_oje_auto_post_startup_reset_done")
    yield
    if previous is None:
        if hasattr(sys, "_oje_auto_post_startup_reset_done"):
            delattr(sys, "_oje_auto_post_startup_reset_done")
    else:
        sys._oje_auto_post_startup_reset_done = previous


def turn_on(database: Database) -> None:
    runtime = AutoPostRuntimeService(database, authentication_ready=lambda: True)
    runtime.enable()
    assert AutoPostRepository(database).settings()["enabled"] is True


def test_operator_on_survives_a_streamlit_module_reload(runtime_database) -> None:
    """The reported symptom, reproduced without any process restart.

    Streamlit re-executes a changed module inside the running process. The
    guard used to keep its memory in a module global, so the reload emptied it
    and the very next rerun forced the operator's switch back off.
    """

    scheduler_module.reset_auto_post_runtime_on_process_start(runtime_database)
    turn_on(runtime_database)

    reloaded = importlib.reload(scheduler_module)
    fired = reloaded.reset_auto_post_runtime_on_process_start(runtime_database)

    assert fired is False, "a module reload is not a new process"
    assert AutoPostRepository(runtime_database).settings()["enabled"] is True


def test_operator_on_survives_repeated_reruns_and_reloads(runtime_database) -> None:
    scheduler_module.reset_auto_post_runtime_on_process_start(runtime_database)
    turn_on(runtime_database)

    module = scheduler_module
    for _ in range(3):
        module.reset_auto_post_runtime_on_process_start(runtime_database)
        module = importlib.reload(module)
        module.reset_auto_post_runtime_on_process_start(runtime_database)

    assert AutoPostRepository(runtime_database).settings()["enabled"] is True


def test_an_explicit_stop_still_stays_off(runtime_database) -> None:
    """Only the operator turns it off -- and when they do, it stays off."""

    scheduler_module.reset_auto_post_runtime_on_process_start(runtime_database)
    turn_on(runtime_database)

    runtime = AutoPostRuntimeService(
        runtime_database, authentication_ready=lambda: True
    )
    runtime.disable()
    assert AutoPostRepository(runtime_database).settings()["enabled"] is False

    reloaded = importlib.reload(scheduler_module)
    reloaded.reset_auto_post_runtime_on_process_start(runtime_database)
    scheduler_module.reset_auto_post_runtime_on_process_start(runtime_database)

    assert AutoPostRepository(runtime_database).settings()["enabled"] is False


def test_a_new_interpreter_still_forces_the_switch_off(runtime_database) -> None:
    """The safety gate is unchanged: a genuinely new process starts OFF.

    The registry lives on ``sys``, which a fresh interpreter does not inherit,
    so an empty registry is exactly what a new process sees.
    """

    AutoPostRepository(runtime_database).save_settings(
        enabled=True, interval_minutes=10, max_retries=1,
    )
    if hasattr(sys, "_oje_auto_post_startup_reset_done"):
        delattr(sys, "_oje_auto_post_startup_reset_done")

    fired = scheduler_module.reset_auto_post_runtime_on_process_start(
        runtime_database
    )

    assert fired is True
    assert AutoPostRepository(runtime_database).settings()["enabled"] is False
    assert AutoPostRepository(runtime_database).state()["status"] == "STOPPED"


def test_the_guard_is_still_per_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NAVER_POST_ENABLED", "true")
    monkeypatch.setenv("NAVER_AUTO_POST_ENABLED", "true")
    first = Database(tmp_path / "first.db")
    first.initialize()
    second = Database(tmp_path / "second.db")
    second.initialize()
    for database in (first, second):
        AutoPostRepository(database).save_settings(
            enabled=True, interval_minutes=10, max_retries=1,
        )

    assert scheduler_module.reset_auto_post_runtime_on_process_start(first)
    assert scheduler_module.reset_auto_post_runtime_on_process_start(second)
    assert AutoPostRepository(first).settings()["enabled"] is False
    assert AutoPostRepository(second).settings()["enabled"] is False


def test_a_second_session_on_the_same_path_does_not_reset(
    runtime_database,
) -> None:
    """A second browser session constructs its own Database for one file."""

    scheduler_module.reset_auto_post_runtime_on_process_start(runtime_database)
    turn_on(runtime_database)

    second_session = Database(Path(runtime_database.path))
    fired = scheduler_module.reset_auto_post_runtime_on_process_start(
        second_session
    )

    assert fired is False
    assert AutoPostRepository(runtime_database).settings()["enabled"] is True
