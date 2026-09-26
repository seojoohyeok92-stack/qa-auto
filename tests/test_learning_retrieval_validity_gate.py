"""A Learning row stops being retrievable when its validity window closes.

The live store holds 1,039 rows and every one is PERMANENT, so the TEMPORARY
branch of ``LearningRepository.candidates`` cannot be exercised against real
data.  These build the row instead, in a temp database, and check the gate from
both sides of ``valid_until``.

The gate is SQL, not ranking, so no amount of relevance brings an expired row
back -- which is the property that matters for a promotion that has ended.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.similar_answer_service import SimilarAnswerService

QUESTION = "행사 기간에 구매하면 사은품 주나요?"
ANSWER = "행사 기간 구매 고객께는 사은품을 함께 보내드립니다."


def _store(tmp_path, *, validity_type, valid_from, valid_until):
    database = Database(tmp_path / "validity.db")
    database.initialize()
    with database.transaction() as connection:
        columns = {c[1] for c in connection.execute(
            "PRAGMA table_info(learning_examples)")}
        row = {
            "source_key": "validity-fixture",
            "learning_source": "APPROVED_UNEDITED",
            "question_original_masked": QUESTION,
            "question_normalized": QUESTION,
            "store_code": "OJE_PLUS",
            "inquiry_type": "PRODUCT_INQUIRY",
            "intent": "상품",
            "final_answer": ANSWER,
            "rating": 5,
            "active": 1,
            "validity_active": 1,
            "validity_type": validity_type,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "metadata_json": json.dumps(
                {"human_verified": True, "product_scope": "POLICY",
                 "learning_signal_type": "POSITIVE"},
                ensure_ascii=False),
        }
        usable = {k: v for k, v in row.items() if k in columns}
        connection.execute(
            f"INSERT INTO learning_examples ({','.join(usable)})"
            f" VALUES ({','.join('?' for _ in usable)})",
            list(usable.values()),
        )
    return database


def _retrieve(database):
    service = SimilarAnswerService(LearningRepository(database))
    context = service.context(
        QUESTION,
        store_code="OJE_PLUS",
        semantic_goal={
            "retrieval_queries": ["행사 기간 사은품 제공에 대한 안내"],
            "customer_goal": None,
            "requested_information": QUESTION,
            "atomic_question": QUESTION,
            "all_atomic_questions": [],
            "order_evidence_required": False,
            "schedule_scoped": False,
        },
    )
    return context.get("similar_approved_answers") or []


def _iso(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(
        timespec="milliseconds")


def test_a_temporary_row_inside_its_window_is_retrievable(tmp_path):
    database = _store(tmp_path, validity_type="TEMPORARY",
                      valid_from=_iso(-7), valid_until=_iso(+7))

    found = _retrieve(database)

    assert found, "a TEMPORARY row inside its window should be retrievable"
    assert any(ANSWER in str(item.get("answer")) for item in found)


def test_an_expired_temporary_row_is_not_retrievable(tmp_path):
    """The promotion ended. Relevance does not bring it back."""

    database = _store(tmp_path, validity_type="TEMPORARY",
                      valid_from=_iso(-30), valid_until=_iso(-1))

    found = _retrieve(database)

    assert not [item for item in found if ANSWER in str(item.get("answer"))], [
        item.get("learning_example_id") for item in found
    ]


def test_a_temporary_row_before_its_window_is_not_retrievable(tmp_path):
    database = _store(tmp_path, validity_type="TEMPORARY",
                      valid_from=_iso(+3), valid_until=_iso(+10))

    found = _retrieve(database)

    assert not [item for item in found if ANSWER in str(item.get("answer"))]


def test_a_permanent_row_is_retrievable_regardless_of_dates(tmp_path):
    """The control: the fixture itself is findable when validity is not the gate."""

    database = _store(tmp_path, validity_type="PERMANENT",
                      valid_from=None, valid_until=None)

    found = _retrieve(database)

    assert any(ANSWER in str(item.get("answer")) for item in found)


@pytest.mark.parametrize("flag", ["active", "validity_active"])
def test_a_deactivated_row_is_not_retrievable(tmp_path, flag):
    database = _store(tmp_path, validity_type="PERMANENT",
                      valid_from=None, valid_until=None)
    with database.transaction() as connection:
        connection.execute(f"UPDATE learning_examples SET {flag}=0")

    assert not [item for item in _retrieve(database)
                if ANSWER in str(item.get("answer"))]


def test_a_negative_signal_row_is_not_retrievable(tmp_path):
    """NEGATIVE rows record what not to say; they are never candidates."""

    database = _store(tmp_path, validity_type="PERMANENT",
                      valid_from=None, valid_until=None)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE learning_examples SET metadata_json=?",
            (json.dumps({"human_verified": True, "product_scope": "POLICY",
                         "learning_signal_type": "NEGATIVE"},
                        ensure_ascii=False),),
        )

    assert not [item for item in _retrieve(database)
                if ANSWER in str(item.get("answer"))]
