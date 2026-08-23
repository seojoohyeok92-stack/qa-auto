"""Dashboard rerun cost, and what a provider limit shows the operator.

Two production findings, measured on a 248MB database with 2,475 inquiries
and 250,409 activity_logs rows:

  * A rerun spent 0.43s scanning ``activity_logs`` by ``event_code`` and by
    ``level`` -- only ``created_at`` was indexed, so both reads scanned the
    whole log -- and 0.47s resolving Learning badges for every inquiry in the
    table when the page renders twenty rows.
  * A run refused by the provider limit failed in ~32ms, yet the dashboard
    reported the generic "요청을 처리하지 못했습니다. 활동 로그에서 상세 상태를
    확인해 주세요." and left the progress container reading "Facts 준비 중",
    so an operator could not tell a rate limit from a real failure.

Fakes only: no network, no real provider, no POST.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app as appmod
from answer.exceptions import AnswerGenerationError
from answer.models import AnswerResult, AnswerStatus
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import limit_notice


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "rerun.db")
    value.initialize()
    return value


def _inquiry(database: Database, source_id: str) -> int:
    return InquiryRepository(database).upsert_work_item(
        {
            "store_code": "S",
            "source_type": "NAVER",
            "source_question_id": source_id,
            "inquiry_type": "상품",
            "content": "설치는 언제 되나요?",
            "registered_at": "2026-08-24T00:00:00+09:00",
            "raw_json": {},
        }
    ).inquiry_id


# --------------------------------------------------------------- rerun cost


def test_activity_log_reads_are_indexed_by_event_code_and_level(
    database: Database,
) -> None:
    """The dashboard's two per-rerun log reads must seek, not scan."""

    with database.connection() as connection:
        by_event = " ".join(
            str(row[-1])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT inquiry_id FROM activity_logs"
                " WHERE event_code IN ('AUTO_ANSWER_SUCCEEDED')"
                " ORDER BY created_at DESC"
            )
        )
        by_level = " ".join(
            str(row[-1])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT event_code FROM activity_logs"
                " WHERE level IN ('ERROR','CRITICAL')"
                " ORDER BY created_at DESC LIMIT 20"
            )
        )
    assert "SEARCH" in by_event and "SCAN" not in by_event, by_event
    assert "SEARCH" in by_level and "SCAN" not in by_level, by_level


def test_learning_badges_are_resolved_only_for_the_rows_given(
    database: Database,
) -> None:
    """Decorating twenty rows must not resolve the whole table's state."""

    for index in range(6):
        _inquiry(database, f"LEARN-{index}")
    items = appmod.dashboard_work_items_from_database(database)
    assert len(items) == 6

    seen: list[list[int] | None] = []
    original = InquiryRepository.learning_states

    def spy(self, inquiry_ids=None):
        seen.append(None if inquiry_ids is None else list(inquiry_ids))
        return original(self, inquiry_ids)

    InquiryRepository.learning_states = spy
    try:
        decorated = appmod._attach_dashboard_learning(database, items[:2])
    finally:
        InquiryRepository.learning_states = original

    assert len(seen) == 1
    assert seen[0] is not None, "must not resolve every inquiry's state"
    assert len(seen[0]) == 2
    # The badge values themselves are unchanged by the scoping.
    for item in decorated:
        assert item["learning_status"] == "NONE"
        assert item["learning_tooltip"] == "Learning 이력 없음"


def test_scoped_and_unscoped_learning_badges_agree(
    database: Database,
) -> None:
    """Scoping is an optimisation, so it must not change any badge."""

    for index in range(4):
        _inquiry(database, f"AGREE-{index}")
    items = appmod.dashboard_work_items_from_database(database)

    repository = InquiryRepository(database)
    every_state = repository.learning_states()
    scoped = appmod._attach_dashboard_learning(database, list(items))

    with database.connection() as connection:
        identity = {
            str(row["source_question_id"]): int(row["id"])
            for row in connection.execute(
                "SELECT id, source_question_id FROM inquiries"
            )
        }
    for item in scoped:
        expected = every_state[identity[str(item["inquiry_id"])]]
        assert item["learning_status"] == expected["learning_status"]
        assert item["learning_labels"] == expected["learning_labels"]


def test_attach_learning_handles_an_empty_page(database: Database) -> None:
    assert appmod._attach_dashboard_learning(database, []) == []


# ------------------------------------------------------- provider limit UI


def test_rate_limited_generation_carries_a_machine_readable_reason() -> None:
    error = AnswerGenerationError(
        "GPT 답변이 안전 검증을 통과하지 못했습니다: RATE_LIMITED",
        reason_code="RATE_LIMITED",
    )
    assert error.reason_code == "RATE_LIMITED"


def test_rate_limit_gets_its_own_operator_message() -> None:
    notice = limit_notice(
        AnswerGenerationError("boom", reason_code="RATE_LIMITED")
    )
    assert notice is not None
    assert "요청 제한" in notice
    assert "잠시 후 다시 시도" in notice
    assert "기존 Program Answer는 그대로 유지" in notice
    assert "활동 로그" not in notice


def test_cost_limit_is_explained_as_a_limit_not_a_rate_limit() -> None:
    notice = limit_notice(
        AnswerGenerationError("boom", reason_code="COST_LIMITED")
    )
    assert notice is not None
    assert "사용 한도" in notice
    assert "기존 Program Answer는 그대로 유지" in notice


def test_other_generation_failures_keep_the_generic_message() -> None:
    """A validation failure must never be reported as a rate limit."""

    assert limit_notice(AnswerGenerationError("검증 실패")) is None
    assert (
        limit_notice(
            AnswerGenerationError(
                "검증 실패", reason_code="GPT_VALIDATION_FAILED"
            )
        )
        is None
    )
    assert limit_notice(RuntimeError("EMPTY_GENERATED_DRAFT")) is None
    assert limit_notice(ValueError("bad input")) is None


def test_progress_container_is_closed_in_the_error_state() -> None:
    """A half-second refusal must not leave "Facts 준비 중" on screen."""

    from ui.review_workspace import _close_generation_status

    class Box:
        def __init__(self) -> None:
            self.updates: list[dict] = []

        def update(self, **kwargs) -> None:
            self.updates.append(kwargs)

    box = Box()
    _close_generation_status(box, "OpenAI 요청 제한으로 생성하지 못했습니다")
    assert box.updates == [
        {
            "label": "OpenAI 요청 제한으로 생성하지 못했습니다",
            "state": "error",
            "expanded": False,
        }
    ]


def test_closing_the_progress_container_never_masks_the_real_failure() -> None:
    class Broken:
        def update(self, **kwargs):
            raise RuntimeError("container gone")

    from ui.review_workspace import _close_generation_status

    _close_generation_status(Broken(), "실패")  # must not raise
    _close_generation_status(None, "실패")


# ------------------------------------------- provider limit end to end


class RateLimitedHybrid:
    """A hybrid service that falls back the way a provider limit makes it.

    Mirrors ``GptGovernanceService`` refusing before any provider round trip:
    the rule answer comes back with ``fallback_used`` set and the reason
    recorded as ``RATE_LIMITED``.
    """

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request, rule_result):
        from answer.facts import build_answer_facts
        from services.hybrid_answer_service import HybridAnswerOutcome

        self.calls += 1
        result = AnswerResult(
            status=AnswerStatus.NEEDS_REVIEW,
            category="일반",
            reason="rate limited",
            answer="규칙 기반 임시 답변입니다.",
            provider="rules",
            auto_answerable=False,
            needs_review=True,
            matched_rule="RULE",
            metadata={
                "hybrid": {
                    "fallback_used": True,
                    "fallback_reason": "RATE_LIMITED",
                    "provider_telemetry": {
                        "provider_call_count": 0,
                        "tasks": [],
                    },
                }
            },
        )
        return HybridAnswerOutcome(
            result=result,
            facts=build_answer_facts(request, rule_result),
            intent=None,
            draft=None,
            self_review=None,
            validation=None,
            fallback_used=True,
            events=(),
        )


def _existing_active_draft(
    database: Database, inquiry_id: int, text: str
) -> tuple[int, str]:
    """Store an already-approved Program Answer and make it the active one."""

    from repositories.answer_repository import AnswerRepository

    repository = AnswerRepository(database)
    draft = repository.create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="일반",
            reason="existing",
            answer=text,
            provider="openai",
            auto_answerable=True,
            needs_review=False,
            matched_rule="EXISTING",
            metadata={"generation_mode": "GPT_DIRECT"},
        ),
    )
    draft_id = int(draft["id"])
    repository.activate_draft(inquiry_id=inquiry_id, draft_id=draft_id)
    return draft_id, str(draft["original_answer"])


def test_rate_limited_regeneration_keeps_the_existing_answer(
    database: Database,
) -> None:
    """A limit must not replace, empty or deactivate the stored answer."""

    from services.answer_service import AnswerService

    inquiry_id = _inquiry(database, "LIMIT-KEEP")
    kept_id, kept = _existing_active_draft(
        database, inquiry_id, "기존에 승인된 Program Answer 본문입니다."
    )

    with database.connection() as connection:
        before = int(
            connection.execute(
                "SELECT COUNT(*) FROM answer_drafts WHERE inquiry_id=?",
                (inquiry_id,),
            ).fetchone()[0]
        )

    hybrid = RateLimitedHybrid()
    with pytest.raises(AnswerGenerationError) as raised:
        AnswerService(database, hybrid_service=hybrid).generate_for_inquiry(
            inquiry_id, prefer_template=False
        )

    # The cause survives all the way to the caller, so the UI can explain it.
    assert raised.value.reason_code == "RATE_LIMITED"
    assert limit_notice(raised.value) is not None

    with database.connection() as connection:
        after = int(
            connection.execute(
                "SELECT COUNT(*) FROM answer_drafts WHERE inquiry_id=?",
                (inquiry_id,),
            ).fetchone()[0]
        )
        active = connection.execute(
            "SELECT id, original_answer FROM answer_drafts"
            " WHERE inquiry_id=? AND is_active=1",
            (inquiry_id,),
        ).fetchall()

    assert after == before, "a refused run must not create a draft"
    assert len(active) == 1
    assert int(active[0]["id"]) == kept_id
    assert active[0]["original_answer"] == kept


def test_rate_limited_regeneration_makes_no_provider_call(
    database: Database,
) -> None:
    from services.answer_service import AnswerService

    inquiry_id = _inquiry(database, "LIMIT-NOCALL")
    _existing_active_draft(database, inquiry_id, "기존 답변 본문입니다.")
    hybrid = RateLimitedHybrid()
    with pytest.raises(AnswerGenerationError):
        AnswerService(database, hybrid_service=hybrid).generate_for_inquiry(
            inquiry_id, prefer_template=False
        )
    # The limit is refused before any round trip, so the count stays at zero
    # -- a refusal must never spend a call, and never retry into one.
    assert hybrid.calls == 1


# ------------------------------------------------------- the profiler itself


def test_profiler_is_inert_unless_switched_on(monkeypatch) -> None:
    """An un-profiled production rerun must record nothing at all."""

    import ui.rerun_profile as profile

    monkeypatch.delenv(profile.ENV_FLAG, raising=False)
    assert profile.enabled() is False
    profile.begin()
    with profile.stage("work_queue_load"):
        pass
    assert profile.snapshot() == {}
    assert profile.publish() == {}


def test_profiler_reports_elapsed_and_cumulative(monkeypatch) -> None:
    import ui.rerun_profile as profile

    monkeypatch.setenv(profile.ENV_FLAG, "1")
    profile.begin()
    with profile.stage("app_start"):
        pass
    with profile.stage("work_queue_load"):
        pass
    result = profile.snapshot()

    names = [row["stage"] for row in result["stages"]]
    assert names == ["app_start", "work_queue_load", "other"]
    for row in result["stages"]:
        assert row["elapsed_seconds"] >= 0
        assert row["cumulative_seconds"] >= 0
    # "other" closes the gap, so the stages account for the whole run.
    assert result["stages"][-1]["cumulative_seconds"] == pytest.approx(
        result["total_seconds"], abs=0.05
    )


def test_profiler_starts_each_run_clean(monkeypatch) -> None:
    import ui.rerun_profile as profile

    monkeypatch.setenv(profile.ENV_FLAG, "1")
    profile.begin()
    with profile.stage("app_start"):
        pass
    profile.begin()
    with profile.stage("header"):
        pass
    assert [row["stage"] for row in profile.snapshot()["stages"]] == [
        "header",
        "other",
    ]
