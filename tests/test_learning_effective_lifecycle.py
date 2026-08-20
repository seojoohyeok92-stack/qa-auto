from __future__ import annotations

from contextlib import contextmanager

from answer.answer_format import format_final_answer
from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from repositories.naver_posted_answer_repository import NaverPostedAnswerRepository
from services.learning_context_service import LearningContextService
from services.learning_feedback_service import LearningFeedbackService
from services.learning_privacy_service import LearningPrivacyService
from ui.learning_manager import _history_status_label, _learning_status_label


QUESTION = "배송기사님 방문 일정은 어떻게 정하나요?"
OLD_ANSWER = "배송 전 기사님이 연락하여 방문 일정을 조율합니다."
NEW_ANSWER = "주문 상태를 확인한 뒤 담당 기사와 방문 일정을 조율해 주세요."


def _database(tmp_path, *, external_id: str = "325076443"):
    database = Database(tmp_path / f"lifecycle-{external_id}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": external_id,
            "inquiry_type": "CUSTOMER_INQUIRY",
            "title": "배송 일정 문의",
            "content": QUESTION,
            "product_name": "삼성 TV",
            "registered_at": "2026-08-20T09:00:00+09:00",
            "raw_json": {"queue": "AUTO_PROCESSABLE", "priority": "MEDIUM"},
        }
    ).inquiry_id
    posted = NaverPostedAnswerRepository(database).observe(
        inquiry_id=inquiry_id,
        answer_body=OLD_ANSWER,
        answer_id=f"ANSWER-{external_id}",
        source_api="TEST_FIXTURE",
    )
    return database, inquiry_id, posted


def _positive(
    repository: LearningRepository,
    *,
    inquiry_id: int,
    source_key: str,
    answer: str,
    human_verified: bool,
    provenance: str,
    reference_id: int,
    atomic: bool = False,
):
    example = {
        "source_key": source_key,
        "inquiry_id": inquiry_id,
        "learning_source": "APPROVED_EDITED" if human_verified else "SELLER_ANSWER",
        "question_original_masked": QUESTION,
        "question_normalized": QUESTION,
        "store_code": "OJE_PLUS",
        "inquiry_type": "CUSTOMER_INQUIRY",
        "intent": "DELIVERY_POLICY",
        "product_name": "삼성 TV",
        "final_answer": LearningPrivacyService().mask(format_final_answer(answer)),
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 1.0,
        "style_only": False,
        "version": 1,
        "validator_result": (
            "HUMAN_VERIFIED_NAVER_POSTED" if human_verified else "PASS"
        ),
        "metadata_json": {
            "learning_signal_type": "POSITIVE",
            "human_verified": human_verified,
            "answer_provenance": provenance,
            "answer_reference_id": reference_id,
        },
        "active": True,
    }
    if atomic:
        return repository.upsert_human_verified_atomic(
            example, feedback_answer_sources=(provenance,)
        )
    return repository.upsert(example)


def _negative(database, inquiry_id: int, posted_id: int):
    return LearningFeedbackService(database).capture_dashboard_negative(
        inquiry_id=inquiry_id,
        original_answer_source="NAVER_POSTED",
        original_answer_reference_id=posted_id,
        correction_reason="FACT_ERROR",
        correction_note="게시 답변의 정책 근거가 부정확함",
    )


def _dashboard_ids(database: Database, status: str) -> list[int]:
    rows, _, _ = InquiryRepository(database).dashboard_page(
        store_codes=["OJE_PLUS"],
        source="ALL",
        queues=["AUTO_PROCESSABLE"],
        priorities=["MEDIUM"],
        answer_status="ALL",
        delivery_only=False,
        search_query="배송 일정",
        start_date="2026-08-01",
        end_date="2026-08-31",
        kpi_filter=None,
        page=1,
        page_size=20,
        learning_status=status,
    )
    return [int(row["id"]) for row in rows]


def test_325076443_auto_is_not_displayed_as_positive_approved(tmp_path) -> None:
    database, inquiry_id, posted = _database(tmp_path)
    _positive(
        LearningRepository(database),
        inquiry_id=inquiry_id,
        source_key="external-325076443-auto",
        answer=OLD_ANSWER,
        human_verified=False,
        provenance="NAVER_POSTED",
        reference_id=int(posted["id"]),
    )

    state = InquiryRepository(database).learning_states([inquiry_id])[inquiry_id]
    manager = LearningRepository(database).manager_rows()[0]

    assert state["learning_status"] == "AUTO"
    assert state["learning_labels"] == ["자동"]
    assert _learning_status_label(manager) == "Positive 자동"
    assert _history_status_label(manager) == "Positive 자동"
    assert manager["human_verified"] is False
    assert inquiry_id in _dashboard_ids(database, "AUTO")
    assert inquiry_id not in _dashboard_ids(database, "APPROVED")


def test_auto_negative_soft_revokes_and_unifies_badge_filter_and_manager(tmp_path) -> None:
    database, inquiry_id, posted = _database(tmp_path, external_id="AUTO-NEGATIVE")
    learning = _positive(
        LearningRepository(database),
        inquiry_id=inquiry_id,
        source_key="auto-before-negative",
        answer=OLD_ANSWER,
        human_verified=False,
        provenance="NAVER_POSTED",
        reference_id=int(posted["id"]),
    )

    feedback = _negative(database, inquiry_id, int(posted["id"]))
    preserved = LearningRepository(database).get(int(learning["id"]))
    state = InquiryRepository(database).learning_states([inquiry_id])[inquiry_id]
    positive_manager = LearningRepository(database).manager_rows()[0]
    feedback_manager = LearningFeedbackRepository(database).manager_rows()[0]

    assert feedback and feedback[0]["learning_signal_type"] == "NEGATIVE"
    assert preserved is not None and preserved["active"] is False
    assert preserved["metadata_json"]["excluded_by_feedback_id"] == feedback[0]["id"]
    assert state["learning_status"] == "EXCLUDED"
    assert state["learning_labels"] == ["제외"]
    assert inquiry_id not in _dashboard_ids(database, "AUTO")
    assert inquiry_id in _dashboard_ids(database, "EXCLUDED")
    assert _learning_status_label(positive_manager) == "제외"
    assert _history_status_label(positive_manager) == "Positive 자동 (비활성)"
    assert _learning_status_label(feedback_manager) == "제외"


def test_legacy_active_auto_with_negative_is_never_selected_attached_or_used(tmp_path) -> None:
    database, inquiry_id, posted = _database(tmp_path, external_id="LEGACY-CONFLICT")
    learning = _positive(
        LearningRepository(database),
        inquiry_id=inquiry_id,
        source_key="legacy-active-auto",
        answer=OLD_ANSWER,
        human_verified=False,
        provenance="NAVER_POSTED",
        reference_id=int(posted["id"]),
    )
    _negative(database, inquiry_id, int(posted["id"]))
    # Reproduce pre-fix data where Negative and AUTO both remained active.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE learning_examples SET active=1 WHERE id=?", (int(learning["id"]),)
        )

    assert LearningRepository(database).get(int(learning["id"]))["active"] is True
    assert LearningRepository(database).candidates(store_code="OJE_PLUS") == []
    diagnostics = LearningRepository(database).candidate_diagnostics(
        store_code="OJE_PLUS"
    )
    assert diagnostics["active_candidates"] == 0
    assert diagnostics["negative_excluded"] == 1
    intent = IntentResult(
        category="DELIVERY_POLICY",
        questions=(QUESTION,),
        emotion=Emotion.NORMAL,
        urgency="NORMAL",
        confidence=0.95,
        requires_review=False,
        reason="test",
    )
    context = LearningContextService(database).build(
        AnswerFacts(
            inquiry={
                "inquiry_id": inquiry_id,
                "question": QUESTION,
                "type": "CUSTOMER_INQUIRY",
            },
            product={"name": "삼성 TV"},
        ),
        intent,
    )
    trace = context["learning_retrieval"]
    assert trace["selected_count"] == 0
    assert trace["selected_learning_ids"] == []
    assert context["similar_approved_answers"] == []
    with database.connection() as connection:
        provenance_count = connection.execute(
            "SELECT COUNT(*) FROM answer_learning_provenance "
            "WHERE learning_example_id=? AND included_in_prompt=1",
            (int(learning["id"]),),
        ).fetchone()[0]
    assert provenance_count == 0
    assert LearningRepository(database).get(int(learning["id"]))["usage_count"] == 0


def test_auto_negative_then_new_human_answer_becomes_approved_only(tmp_path) -> None:
    database, inquiry_id, posted = _database(tmp_path, external_id="SUPERSESSION")
    old = _positive(
        LearningRepository(database),
        inquiry_id=inquiry_id,
        source_key="superseded-auto",
        answer=OLD_ANSWER,
        human_verified=False,
        provenance="NAVER_POSTED",
        reference_id=int(posted["id"]),
    )
    negative = _negative(database, inquiry_id, int(posted["id"]))[0]
    new = _positive(
        LearningRepository(database),
        inquiry_id=inquiry_id,
        source_key="new-human-corrected-answer",
        answer=NEW_ANSWER,
        human_verified=True,
        provenance="STAFF_EDITED",
        reference_id=9001,
        atomic=True,
    )

    state = InquiryRepository(database).learning_states([inquiry_id])[inquiry_id]
    old_after = LearningRepository(database).get(int(old["id"]))
    feedback_after = LearningFeedbackRepository(database).for_inquiry(inquiry_id)[0]
    candidates = LearningRepository(database).candidates(store_code="OJE_PLUS")

    assert state["learning_status"] == "APPROVED"
    assert inquiry_id in _dashboard_ids(database, "APPROVED")
    assert inquiry_id not in _dashboard_ids(database, "EXCLUDED")
    assert old_after["active"] is False
    assert int(feedback_after["id"]) == int(negative["id"])
    assert feedback_after["active"] is True
    assert feedback_after["metadata_json"]["lifecycle_superseded_by_learning_id"] == new["id"]
    assert [row["id"] for row in candidates] == [new["id"]]
    manager = LearningRepository(database).manager_rows()
    assert {_learning_status_label(row) for row in manager} == {"Positive 승인"}
    assert {_history_status_label(row) for row in manager} == {
        "Positive 자동 (비활성)", "Positive 승인"
    }


def test_effective_lifecycle_precedence_and_batch_query_have_one_resolver(
    tmp_path, monkeypatch
) -> None:
    database, inquiry_id, posted = _database(tmp_path, external_id="BATCH")
    _positive(
        LearningRepository(database),
        inquiry_id=inquiry_id,
        source_key="batch-approved",
        answer=NEW_ANSWER,
        human_verified=True,
        provenance="STAFF_EDITED",
        reference_id=8001,
    )
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO learning_feedback(
                source_key, feedback_type, correction_reason,
                learning_signal_type, source, inquiry_id, metadata_json
            ) VALUES ('batch-correction', 'STAFF_CORRECTION', 'ROUTING_ERROR',
                      'INTENT_CORRECTION', 'TEST', ?, '{}')
            """,
            (inquiry_id,),
        )
    repository = InquiryRepository(database)
    original_connection = database.connection
    calls = 0

    @contextmanager
    def counted_connection():
        nonlocal calls
        calls += 1
        with original_connection() as connection:
            yield connection

    monkeypatch.setattr(database, "connection", counted_connection)
    states = repository.learning_states([inquiry_id])

    assert calls == 1
    assert states[inquiry_id]["learning_status"] == "CORRECTED"
    assert states[inquiry_id]["learning_labels"] == ["교정"]
