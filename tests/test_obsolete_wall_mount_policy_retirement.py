"""Migration 37: the two wall-mount rows migration 35's vocabulary missed.

Migration 35 retired the former procedure -- buy the extra bracket, return
only that bracket after the engineer confirms the existing mount can be
reused, then ask for a partial refund -- and it worked: twenty rows in the
live store carry its note.  Two did not.

Its first condition required the part to be called 브라켓 or 벽걸이암.  One
answer calls it 벽걸이 자재 and another writes 벽걸암, dropping the 이.  Both
then describe the retired procedure in full, so both stayed answerable while
their twenty siblings did not.

What is widened here is what the part may be called.  The refund half of the
condition is untouched and still required, which is what keeps an ordinary
wall-mount answer -- VESA size, whether a bracket is included, who installs
it -- answerable.  Those are the tests below that assert nothing happened.
"""

from __future__ import annotations

import sqlite3

import repositories.database as database_module
from repositories.database import Database
from tests.migration_contract import (
    assert_migration_applied,
    assert_reinitialize_is_noop,
    migrations_through,
)

#: The two shapes migration 35 could not see, reproduced from the live rows.
#: 벽걸이 자재 for the part, and 벽걸암 with the 이 dropped.
MATERIAL_WORDING = (
    "벽걸이 형태로 설치를 원하시는 경우 주문 시 추가상품의 벽걸이 설치변경을 "
    "추가해주시면 됩니다. 다만 현장에 방문 삼성 설치기사가 보고 기존의 벽걸이 "
    "재 사용이 가능할지 여부를 판단할텐데, 가능할 경우엔 가져온 벽걸이 자재는 "
    "부분반품 할테니 다시 가져가도록 해주신 후 저희 쪽으로 연락을 주시면 "
    "벽걸이 추가금액은 부분 환불로 처리해드리겠습니다."
)
TYPO_WORDING = (
    "기존 벽걸이 제품에 본 제품과 호환이 되지 않을수 있습니다. 벽걸형으로 "
    "구매후 기사님 설치 오시면, 기존 벽걸이에 사용 가능여부 확인 요청해주시고. "
    "기존 벽걸이형에 사용가능하시면, 추가로 벽걸암은 기사님께 부분반품 요청 "
    "해주세요. 반품이후 저희쪽으로 상담주시면 벽걸이 비용 환불 가능하게 "
    "해드리겠습니다."
)
#: The spelling migration 35 did recognise, kept so this file proves the old
#: shape still retires rather than assuming it.
ORIGINAL_WORDING = (
    "기존 브라켓과의 호환 여부에 대해서는 현장 확인이 필요한 사항이 많아 "
    "현장에서 설치 기사님께서 확인해주실 수 있습니다. 하여 우선 추가상품의 "
    '"벽걸이 설치 변경" 항목을 선택하시어 구매해주신 뒤 현장에서 기존 브라켓에 '
    "설치가 가능하신 경우 기사님에게 구매하신 브라켓은 부분 반품 요청 주실 수 "
    "있으시며 이후 판매자고객센터로 연락주시면 처리 도와드리겠습니다."
)

#: Ordinary wall-mount answers. Each names a bracket or VESA and must stay
#: answerable: these are the facts the store still gives out.
KEEP_WORDINGS = {
    "vesa_size": "해당 모델의 VESA 규격은 200x200mm 입니다.",
    "bracket_included": (
        "벽걸이 추가하시게 될 경우 벽걸이용 브라켓이 함께 출고되며, "
        "설치비는 청구되지 않습니다."
    ),
    "bracket_purchase": (
        "벽걸이 브라켓은 벽걸이형 설치 추가상품을 통하여 추가구매해주셔야합니다."
    ),
    "ceiling_not_supported": (
        "VESA홀이 있어서 설치 자체는 가능하나 삼성 기사님들께서는 천정형 "
        "설치는 해드리지 않습니다. 별도 사설업체를 섭외해주셔야 합니다."
    ),
    # A partial return that has nothing to do with a wall mount. The live
    # corpus holds none of these, so it is constructed here rather than
    # assumed: the refund condition alone must not retire a row.
    "unrelated_partial_return": (
        "구성품 중 리모컨만 부분반품 요청하시면 해당 금액은 부분 환불로 "
        "처리해드립니다."
    ),
    # A cost phrase next to a partial refund, with no part named and no part
    # returned. ``벽걸이 비용`` and ``벽걸이 추가금액`` were briefly in the
    # part-name half of the condition, which would have matched this; they are
    # not part names and were removed. No such row exists in the store today,
    # which is why it is written here -- the contract is about the shape, not
    # about today's corpus.
    "cost_wording_only": (
        "벽걸이 비용은 부분 환불 가능합니다. 벽걸이 추가금액은 주문 취소 시 "
        "함께 정산됩니다."
    ),
}

RETIRED_NOTE = (
    "현재 정책과 충돌: 기존 브라켓 사용 시 브라켓 부분 반품/부분 환불 절차는 "
    "더 이상 답변 근거로 사용하지 않음"
)


def _seed(connection: sqlite3.Connection, key: str, answer: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO learning_examples(
            source_key, learning_source, question_original_masked,
            question_normalized, store_code, inquiry_type, final_answer,
            rating, quality_score, style_only, version, active,
            validity_type, validity_active, created_at, updated_at
        ) VALUES (?, 'SELLER_ANSWER', '벽걸이 문의', '벽걸이 문의',
                  'OJE_PLUS', 'PRODUCT_INQUIRY', ?, 5, 0.9, 0, 1, 1,
                  'PERMANENT', 1,
                  strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                  strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        """,
        (key, answer),
    )
    return int(cursor.lastrowid)


def _state(database: Database, row_id: int) -> dict:
    with database.connection() as connection:
        row = connection.execute(
            "SELECT validity_active, validity_note, expired_at, active,"
            " final_answer FROM learning_examples WHERE id=?",
            (row_id,),
        ).fetchone()
    return dict(row)


def _upgraded(tmp_path, monkeypatch, name="wall-mount.db"):
    """A database built just before 37, seeded, then upgraded onto it.

    Pinned to 37 by version: ``MIGRATIONS[:-1]`` would stop meaning "before
    37" the moment a migration 38 is written, and the seeded rows would then
    be inserted into a database that had already corrected them.
    """

    monkeypatch.setattr(
        database_module, "MIGRATIONS", migrations_through(37)
    )
    database = Database(tmp_path / name)
    database.initialize()
    ids: dict[str, int] = {}
    with database.transaction() as connection:
        ids["material"] = _seed(connection, "material", MATERIAL_WORDING)
        ids["typo"] = _seed(connection, "typo", TYPO_WORDING)
        ids["original"] = _seed(connection, "original", ORIGINAL_WORDING)
        for key, answer in KEEP_WORDINGS.items():
            ids[key] = _seed(connection, key, answer)
    # Nothing is retired yet: the rows were inserted after 35 ran.
    for row_id in ids.values():
        assert _state(database, row_id)["validity_active"] == 1
    monkeypatch.undo()
    applied = database.initialize()
    return database, ids, applied


# ----------------------------------------------------------------------
# A, B: the shapes that must retire
# ----------------------------------------------------------------------


def test_material_wording_is_retired(tmp_path, monkeypatch) -> None:
    """CASE A-1: the part called 벽걸이 자재."""

    database, ids, applied = _upgraded(tmp_path, monkeypatch)
    assert 37 in applied
    state = _state(database, ids["material"])
    assert state["validity_active"] == 0
    assert state["validity_note"] == RETIRED_NOTE
    assert state["expired_at"] is not None
    # Retired, not deleted or rewritten: what was sent to the customer stays
    # auditable, which is the whole reason this uses the validity axis.
    assert state["active"] == 1
    assert state["final_answer"] == MATERIAL_WORDING


def test_typo_wording_is_retired(tmp_path, monkeypatch) -> None:
    """CASE A-2: 벽걸암, the spelling that escaped migration 35."""

    database, ids, _ = _upgraded(tmp_path, monkeypatch)
    state = _state(database, ids["typo"])
    assert state["validity_active"] == 0
    assert state["validity_note"] == RETIRED_NOTE
    assert state["expired_at"] is not None
    assert state["active"] == 1


def test_original_bracket_wording_still_retires(tmp_path, monkeypatch) -> None:
    """CASE B: widening the vocabulary did not drop the spelling 35 handled."""

    database, ids, _ = _upgraded(tmp_path, monkeypatch)
    state = _state(database, ids["original"])
    assert state["validity_active"] == 0
    assert state["validity_note"] == RETIRED_NOTE


# ----------------------------------------------------------------------
# C, D: what must survive
# ----------------------------------------------------------------------


def test_ordinary_wall_mount_answers_are_untouched(
    tmp_path, monkeypatch
) -> None:
    """CASE C: naming a bracket or VESA is not the retired procedure."""

    database, ids, _ = _upgraded(tmp_path, monkeypatch)
    for key in (
        "vesa_size", "bracket_included", "bracket_purchase",
        "ceiling_not_supported",
    ):
        state = _state(database, ids[key])
        assert state["validity_active"] == 1, key
        assert state["validity_note"] is None, key
        assert state["expired_at"] is None, key


def test_cost_wording_alone_does_not_retire(tmp_path, monkeypatch) -> None:
    """A cost phrase is not a part name, and must not reach this migration.

    What migration 35 retired is a procedure: the engineer judges the existing
    mount, the purchased part goes back as a partial return, the charge is
    refunded. A sentence that mentions a wall-mount charge and a partial
    refund without naming or returning the part is not that procedure.

    The fixture does not claim to state current policy. The only contract
    here is that the part-name half of the condition stays about part names.
    """

    database, ids, _ = _upgraded(tmp_path, monkeypatch)
    state = _state(database, ids["cost_wording_only"])
    assert state["validity_active"] == 1
    assert state["validity_note"] is None
    assert state["expired_at"] is None


def test_unrelated_partial_return_is_untouched(tmp_path, monkeypatch) -> None:
    """CASE D: the refund wording alone retires nothing.

    Both halves of the condition are required. A partial return of a remote
    control is a current policy and has to keep working.
    """

    database, ids, _ = _upgraded(tmp_path, monkeypatch)
    state = _state(database, ids["unrelated_partial_return"])
    assert state["validity_active"] == 1
    assert state["validity_note"] is None


def test_retired_rows_leave_the_retrieval_corpus(tmp_path, monkeypatch) -> None:
    """The point of retiring: the row stops being an answer candidate.

    ``LearningRepository.candidates`` filters on ``validity_active`` in SQL,
    so this checks the consequence rather than only the column.
    """

    from repositories.learning_repository import LearningRepository

    database, ids, _ = _upgraded(tmp_path, monkeypatch)
    answers = {
        str(item.get("final_answer") or "")
        for item in LearningRepository(database).candidates(
            store_code="OJE_PLUS", limit=2000
        )
    }
    assert MATERIAL_WORDING not in answers
    assert TYPO_WORDING not in answers
    assert ORIGINAL_WORDING not in answers
    assert KEEP_WORDINGS["vesa_size"] in answers
    assert KEEP_WORDINGS["unrelated_partial_return"] in answers


# ----------------------------------------------------------------------
# E, F, G: migration mechanics
# ----------------------------------------------------------------------


def test_migration_is_safe_to_rerun(tmp_path, monkeypatch) -> None:
    """CASE E: applying again changes nothing.

    ``validity_active=1`` in the WHERE clause is what delivers this, and it
    is also what stops the rows migration 35 retired from having their
    ``expired_at`` pushed forward by this one.
    """

    database, ids, _ = _upgraded(tmp_path, monkeypatch)
    before = {key: _state(database, row_id) for key, row_id in ids.items()}
    assert database.initialize() == []
    assert_reinitialize_is_noop(database)
    after = {key: _state(database, row_id) for key, row_id in ids.items()}
    assert after == before


def test_migration_ledger_records_37(tmp_path, monkeypatch) -> None:
    """CASE F: the ledger is complete and 37 is in it."""

    database, _ids, applied = _upgraded(tmp_path, monkeypatch)
    assert applied == [37]
    assert_migration_applied(database, 37)
    assert_migration_applied(database, 35)
    assert_migration_applied(database, 36)


def test_migration_37_applies_after_36_on_a_fresh_database(tmp_path) -> None:
    """CASE G: a database created today reaches 37 in order, and 36 still holds."""

    from tests.migration_contract import (
        assert_columns_exist,
        assert_fresh_schema,
        expected_migration_versions,
    )

    database = Database(tmp_path / "fresh.db")
    assert_fresh_schema(database)
    versions = expected_migration_versions()
    assert versions[-1] == 37
    assert versions.index(37) == versions.index(36) + 1
    # 36's columns are still there: a data migration must not disturb schema.
    assert_columns_exist(
        database, "inquiries",
        ("source_deletion_tracked", "source_deleted",
         "source_deleted_detected_at", "source_missing_streak"),
    )


def test_migration_37_changes_no_schema(tmp_path, monkeypatch) -> None:
    """Data only: the table definition before and after is identical."""

    def schema(database: Database) -> list[str]:
        with database.connection() as connection:
            return [
                str(row["sql"])
                for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                    " ORDER BY name"
                ).fetchall()
            ]

    monkeypatch.setattr(
        database_module, "MIGRATIONS", migrations_through(37)
    )
    database = Database(tmp_path / "schema.db")
    database.initialize()
    before = schema(database)
    monkeypatch.undo()
    assert database.initialize() == [37]
    assert schema(database) == before
