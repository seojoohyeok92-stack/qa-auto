"""Migration 37: migration 35's retirement of the wall-mount policy, undone.

Migration 35 read "buy the extra wall-mount part, return only that part once
the engineer confirms the existing mount can be reused, and get that amount
refunded" as a procedure that had been withdrawn, and moved twenty rows off
the validity axis.  That procedure is current policy.  Every one of the twenty
states it as a condition the customer may act on -- 가능합니다, 요청 주실 수
있으시며, 도와드릴 수 있습니다 -- and none of them reports a refund already
issued to one customer, which is the distinction that matters: a policy may be
reused, one past order's outcome may not.

So migration 37 restores them.  It is scoped to the note migration 35 wrote
and to nothing else, which is what separates these rows from a row retired for
some other reason whose answer happens to mention 브라켓 부분 반품 -- the tests
below construct exactly that row, because the live corpus holds none.

Two things this deliberately does not do, each with a test that says so:
restoring validity is not the same as making a row usable, and the two safety
gates that still exclude seven of the twenty -- the redaction-token drop in
``search`` and the runtime quality gate -- are left exactly as they are.
"""

from __future__ import annotations

import json
import sqlite3

import repositories.database as database_module
from repositories.database import MIGRATIONS, Database
from tests.migration_contract import (
    assert_migration_applied,
    assert_reinitialize_is_noop,
    expected_migration_versions,
    migrations_through,
)

#: The note migration 35 wrote, which is the only thing migration 37 selects on.
RETIRED_NOTE = (
    "현재 정책과 충돌: 기존 브라켓 사용 시 브라켓 부분 반품/부분 환불 절차는 "
    "더 이상 답변 근거로 사용하지 않음"
)
#: The note migration 37 leaves behind, so an operator reading the row in the
#: Dashboard can see why it came back.
RESTORED_NOTE = (
    "정책 재확인: 추가 구매한 설치 부품(브라켓/벽걸이암 등)만 부분 반품하고 "
    "해당 금액을 환불하는 조건부 절차는 유효한 답변 근거로 사용함"
)

#: The three wordings of the one policy, taken from the live rows. The first
#: is the shape migration 35 matched; the other two are the ones its vocabulary
#: missed and an earlier migration 37 would have retired instead.
BRACKET_WORDING = (
    "기존 브라켓과의 호환 여부에 대해서는 현장 확인이 필요한 사항이 많아 "
    "현장에서 설치 기사님께서 확인해주실 수 있습니다. 하여 우선 추가상품의 "
    '"벽걸이 설치 변경" 항목을 선택하시어 구매해주신 뒤 현장에서 기존 브라켓에 '
    "설치가 가능하신 경우 기사님에게 구매하신 브라켓은 부분 반품 요청 주실 수 "
    "있으시며 이후 판매자고객센터로 연락주시면 처리 도와드리겠습니다."
)
TYPO_WORDING = (
    "기존 벽걸이 제품에 본 제품과 호환이 되지 않을수 있습니다. 벽걸형으로 "
    "구매후 기사님 설치 오시면, 기존 벽걸이에 사용 가능여부 확인 요청해주시고. "
    "기존 벽걸이형에 사용가능하시면, 추가로 벽걸암은 기사님께 부분반품 요청 "
    "해주세요. 반품이후 저희쪽으로 상담주시면 벽걸이 비용 환불 가능하게 "
    "해드리겠습니다."
)
MATERIAL_WORDING = (
    "벽걸이 형태로 설치를 원하시는 경우 주문 시 추가상품의 벽걸이 설치변경을 "
    "추가해주시면 됩니다. 다만 현장에 방문 삼성 설치기사가 보고 기존의 벽걸이 "
    "재 사용이 가능할지 여부를 판단할텐데, 가능할 경우엔 가져온 벽걸이 자재는 "
    "부분반품 할테니 다시 가져가도록 해주신 후 저희 쪽으로 연락을 주시면 "
    "벽걸이 추가금액은 부분 환불로 처리해드리겠습니다."
)
#: The most explicit statement of the policy in the store, and the one the
#: ``candidates`` test looks for.
PLAIN_POLICY_WORDING = (
    "네 기존건 이용 가능시 브라켓 부분반품 가능합니다. 벽걸이형 추가하시어 "
    "주문주신 뒤, 기사님 방문 시 기존브라켓 이용가능여부 여쭤봐주시고, 제품 "
    "이용 가능할 경우 브라켓만 부분반품 요청하시면 됩니다."
)

#: Two of the twenty carry a redaction token, which keeps them out of
#: retrieval however valid they are. See the test that asserts it.
TOKEN_WORDING = (
    "기존 브라켓에 설치가 가능하신 경우 구매하신 브라켓은 부분 반품 요청 주실 "
    "수 있으시며 이후 판매자고객센터)<masked-phone>로 연락주시면 처리 "
    "도와드리겠습니다."
)
#: One of the twenty states the policy in a sentence the runtime quality gate
#: reads as one customer's order fact. Restoring validity does not change that.
ORDER_SPECIFIC_WORDING = (
    "구매하신 제품은 벽걸이형, 스탠드형의 차이가 벽걸이 브라켓의 포함 여부 "
    "입니다. 기존 브라켓에 설치가 가능하신 경우 구매하신 브라켓은 부분 반품 "
    "요청 주실 수 있습니다."
)

#: A row retired for a reason that has nothing to do with this policy, whose
#: answer nonetheless names the policy. The live corpus holds no such row --
#: every ``validity_active=0`` row in the 26.10.4 snapshot is one of migration
#: 35's twenty -- so the contract has to be written rather than observed.
UNRELATED_RETIRED_NOTE = "운영자 수동 비활성: 단종 모델 안내"
UNRELATED_RETIRED_WORDING = (
    "해당 모델은 단종되어 더 이상 주문하실 수 없습니다. 브라켓 부분 반품 "
    "안내와는 무관한 행입니다."
)
UNRELATED_RETIRED_EXPIRED_AT = "2026-09-01T00:00:00.000Z"

#: The metadata retrieval actually reads off a row: the signal type the
#: repository gate requires, the approval flag the quality override reads, and
#: the identity and topic fields the compatibility service looks for.
SEED_METADATA = json.dumps(
    {
        "learning_signal_type": "POSITIVE",
        "human_verified": True,
        "product_scope": "MODEL",
        "learning_topics": ["STAND_BRACKET_VESA", "RETURN_CANCEL"],
    },
    ensure_ascii=False,
)

ALL_COLUMNS = (
    "id, source_key, inquiry_id, answer_draft_id, approval_history_id,"
    " learning_source, question_original_masked, question_normalized,"
    " store_code, inquiry_type, intent, product_name, model_code,"
    " generation_mode, template_id, processing_route, validator_result,"
    " seller_answer, gpt_draft, edited_answer, final_answer, posted,"
    " posted_at, auto_posted, rating, edit_ratio, quality_score, style_only,"
    " version, style_features_json, metadata_json, active, usage_count,"
    " last_used_at, created_at, validity_type, event_name, valid_from,"
    " valid_until, validity_active, expired_at, validity_note, condition_json"
)


def _seed(
    connection: sqlite3.Connection,
    key: str,
    answer: str,
    *,
    validity_active: int = 1,
    validity_note: str | None = None,
    expired_at: str | None = None,
) -> int:
    """One Learning row. ``metadata_json`` carries the fields retrieval reads."""

    cursor = connection.execute(
        """
        INSERT INTO learning_examples(
            source_key, learning_source, question_original_masked,
            question_normalized, store_code, inquiry_type, final_answer,
            seller_answer, posted, rating, edit_ratio, quality_score,
            style_only, version, metadata_json, active, usage_count,
            validity_type, validity_active, validity_note, expired_at,
            created_at, updated_at
        ) VALUES (?, 'SELLER_ANSWER', '기존 브라켓 호환 문의',
                  '기존 브라켓 호환 문의', 'OJE_PLUS', 'PRODUCT_INQUIRY',
                  ?, ?, 1, 5, 0.0, 1.0, 0, 1, ?,
                  1, 0, 'PERMANENT', ?, ?, ?,
                  '2026-08-01T00:00:00.000Z', '2026-08-01T00:00:00.000Z')
        """,
        (key, answer, answer, SEED_METADATA, int(validity_active),
         validity_note, expired_at),
    )
    return int(cursor.lastrowid)


def _whole_row(database: Database, row_id: int) -> dict:
    """Every column except ``updated_at``, which every migration touches."""

    with database.connection() as connection:
        row = connection.execute(
            f"SELECT {ALL_COLUMNS} FROM learning_examples WHERE id=?", (row_id,)
        ).fetchone()
    return dict(row)


def _state(database: Database, row_id: int) -> dict:
    with database.connection() as connection:
        row = connection.execute(
            "SELECT validity_active, validity_note, expired_at, active,"
            " final_answer, metadata_json, rating, quality_score,"
            " validity_type, valid_from, valid_until, style_only"
            " FROM learning_examples WHERE id=?",
            (row_id,),
        ).fetchone()
    return dict(row)


def _migrations_up_to(version: int):
    """``MIGRATIONS`` up to and including ``version``, and nothing after it.

    ``migrations_through`` stops just *before* its argument, which is the
    boundary the fixtures need. A test that measures migration 37's own effect
    needs the other one: 37 applied, and 38 and everything later not applied,
    so a later migration's schema change cannot be read as 37's.

    Read from the real ledger rather than from ``database_module.MIGRATIONS``,
    because the caller is holding a monkeypatched one when it asks.
    """

    target = int(version)
    assert any(int(value) == target for value, _ in MIGRATIONS), (
        f"migration {target} is not declared")
    return tuple(entry for entry in MIGRATIONS if int(entry[0]) <= target)


def _at_36(tmp_path, monkeypatch, name="restore.db"):
    """A database on the schema the live server is on, with 37 still pending.

    Pinned by version, so this keeps meaning "just before 37" after a
    migration 38 is written.
    """

    monkeypatch.setattr(database_module, "MIGRATIONS", migrations_through(37))
    database = Database(tmp_path / name)
    database.initialize()
    return database


def _retired_as_migration_35_left_them(tmp_path, monkeypatch, name="restore.db"):
    """The live server's state: at 36, with the twenty already retired.

    Migration 35 ran on the production database on 2026-09-25 and the rows
    have carried its note ever since, so the fixture reproduces that state
    directly rather than re-deriving it.  ``_the_whole_ledger`` covers the
    other direction, where 35 and 37 run back to back.
    """

    database = _at_36(tmp_path, monkeypatch, name)
    ids: dict[str, int] = {}
    with database.transaction() as connection:
        for key, answer in (
            ("bracket", BRACKET_WORDING),
            ("typo", TYPO_WORDING),
            ("material", MATERIAL_WORDING),
            ("plain", PLAIN_POLICY_WORDING),
            ("token", TOKEN_WORDING),
            ("order_specific", ORDER_SPECIFIC_WORDING),
        ):
            ids[key] = _seed(
                connection, key, answer,
                validity_active=0, validity_note=RETIRED_NOTE,
                expired_at="2026-09-25T06:57:45.939Z",
            )
        # Retired by somebody else, for something else.
        ids["unrelated_retired"] = _seed(
            connection, "unrelated-retired", UNRELATED_RETIRED_WORDING,
            validity_active=0, validity_note=UNRELATED_RETIRED_NOTE,
            expired_at=UNRELATED_RETIRED_EXPIRED_AT,
        )
        # The two rows an earlier migration 37 would have retired. They are
        # valid now and must stay valid.
        ids["still_valid_typo"] = _seed(connection, "valid-typo", TYPO_WORDING)
        ids["still_valid_material"] = _seed(
            connection, "valid-material", MATERIAL_WORDING)
    before = {key: _whole_row(database, row_id) for key, row_id in ids.items()}
    monkeypatch.undo()
    applied = database.initialize()
    return database, ids, applied, before


RESTORED_KEYS = ("bracket", "typo", "material", "plain", "token", "order_specific")


# ----------------------------------------------------------------------
# A: the rows migration 35 retired come back
# ----------------------------------------------------------------------


def test_rows_migration_35_retired_are_restored(tmp_path, monkeypatch) -> None:
    """CASE A: validity_active 0 -> 1 and expired_at -> NULL, for all of them."""

    database, ids, applied, _before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    # 37 ran. Not "37 was the only thing that ran" -- the fixture restores the
    # real ledger before upgrading, so every migration written after 37 applies
    # here too, and pinning the list meant this failed the day 38 was added
    # without anything about migration 37 having changed.
    assert 37 in applied
    for key in RESTORED_KEYS:
        state = _state(database, ids[key])
        assert state["validity_active"] == 1, key
        assert state["expired_at"] is None, key
        assert state["validity_note"] == RESTORED_NOTE, key


def test_the_whole_ledger_leaves_the_policy_valid(tmp_path, monkeypatch) -> None:
    """CASE L: run every migration there is; the policy ends up answerable.

    The guard against the retirement coming back, written as behaviour rather
    than as a search for a string: seed the policy in all three of its
    wordings before migration 35 exists, apply the complete ledger, and the
    rows must be valid at the end.  Migration 35 still retires them on the way
    through -- that is history and is not being rewritten -- and 37 restores
    them.  A future migration that retired them again would fail here.
    """

    monkeypatch.setattr(database_module, "MIGRATIONS", migrations_through(35))
    database = Database(tmp_path / "ledger.db")
    database.initialize()
    ids: dict[str, int] = {}
    with database.transaction() as connection:
        for key, answer in (("bracket", BRACKET_WORDING),
                            ("typo", TYPO_WORDING),
                            ("material", MATERIAL_WORDING),
                            ("plain", PLAIN_POLICY_WORDING)):
            ids[key] = _seed(connection, key, answer)
    monkeypatch.undo()
    applied = database.initialize()
    # The three this case is about ran, in the ledger. Anything written later
    # runs too, and must not change the outcome asserted below -- which is the
    # guard this test exists for.
    assert {35, 36, 37} <= set(applied)
    for key, row_id in ids.items():
        state = _state(database, row_id)
        assert state["validity_active"] == 1, key
        assert state["expired_at"] is None, key
        assert state["final_answer"], key


# ----------------------------------------------------------------------
# B: nothing else about the row moves
# ----------------------------------------------------------------------


def test_restore_touches_only_the_validity_axis(tmp_path, monkeypatch) -> None:
    """CASE B: every other column is byte-identical afterwards.

    Compared over the whole row rather than a chosen few, so a column added to
    the table later is covered without this test being edited.
    """

    database, ids, _applied, before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    moved = {"validity_active", "expired_at", "validity_note"}
    for key in RESTORED_KEYS:
        after = _whole_row(database, ids[key])
        assert set(after) == set(before[key])
        for column in after:
            if column in moved:
                continue
            assert after[column] == before[key][column], (key, column)
        assert before[key]["validity_active"] == 0
        assert after["validity_active"] == 1


# ----------------------------------------------------------------------
# C: a row retired for another reason is not swept up
# ----------------------------------------------------------------------


def test_a_row_retired_for_another_reason_stays_retired(
    tmp_path, monkeypatch
) -> None:
    """CASE C: naming the policy is not enough; the note decides.

    This row's answer says 브라켓 부분 반품 and it was retired by an operator
    for a different reason.  A content-keyed restore would have taken it.
    """

    database, ids, _applied, before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    row_id = ids["unrelated_retired"]
    assert "브라켓 부분 반품" in UNRELATED_RETIRED_WORDING
    assert _whole_row(database, row_id) == before["unrelated_retired"]
    state = _state(database, row_id)
    assert state["validity_active"] == 0
    assert state["validity_note"] == UNRELATED_RETIRED_NOTE
    assert state["expired_at"] == UNRELATED_RETIRED_EXPIRED_AT


# ----------------------------------------------------------------------
# D, E: the two rows the earlier migration 37 would have retired
# ----------------------------------------------------------------------


def test_rows_that_were_never_retired_are_untouched(tmp_path, monkeypatch) -> None:
    """CASES D and E: 벽걸암 and 벽걸이 자재 are valid and stay that way.

    The live ids are 319144 and 172477.  They are reproduced here by their
    wording rather than by id, because migration 37 selects on the retire note
    and neither of them carries one -- which is the reason they are safe, and
    the reason this test is about the shape and not about two numbers.
    """

    database, ids, _applied, before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    for key in ("still_valid_typo", "still_valid_material"):
        assert _whole_row(database, ids[key]) == before[key], key
        state = _state(database, ids[key])
        assert state["validity_active"] == 1, key
        assert state["validity_note"] is None, key
        assert state["expired_at"] is None, key


# ----------------------------------------------------------------------
# F, G, H: migration mechanics
# ----------------------------------------------------------------------


def test_migration_is_safe_to_rerun(tmp_path, monkeypatch) -> None:
    """CASE F: a second run applies nothing and changes nothing.

    Delivered by the predicate itself: it looks for migration 35's note, and
    after the first run no row carries it.
    """

    database, ids, _applied, _before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    after_first = {key: _whole_row(database, row_id) for key, row_id in ids.items()}
    assert database.initialize() == []
    assert_reinitialize_is_noop(database)
    assert {key: _whole_row(database, row_id) for key, row_id in ids.items()} \
        == after_first
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) c FROM learning_examples WHERE validity_note=?",
            (RETIRED_NOTE,),
        ).fetchone()["c"] == 0


def test_migration_ledger_records_37(tmp_path, monkeypatch) -> None:
    """CASE G: 36 -> 37, and the earlier ones are still recorded."""

    database, _ids, applied, _before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    assert 37 in applied
    assert_migration_applied(database, 35)
    assert_migration_applied(database, 36)
    assert_migration_applied(database, 37)
    versions = expected_migration_versions()
    assert versions.index(37) == versions.index(36) + 1


def test_migration_37_changes_no_schema(tmp_path, monkeypatch) -> None:
    """CASE H: data only -- the schema before and after is identical."""

    def schema(database: Database) -> list[str]:
        with database.connection() as connection:
            return [
                str(row["sql"])
                for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                    " ORDER BY name"
                ).fetchall()
            ]

    database = _at_36(tmp_path, monkeypatch, "schema.db")
    before = schema(database)
    # Only 37, deliberately. Restoring the whole ledger here would upgrade
    # through every later migration as well, and migration 38 adds two columns
    # to coupang_product_mappings -- a real schema change that is not 37's and
    # would be reported as though it were.
    monkeypatch.setattr(
        database_module, "MIGRATIONS", _migrations_up_to(37))
    assert database.initialize() == [37]
    assert schema(database) == before


# ----------------------------------------------------------------------
# I: the consumer
# ----------------------------------------------------------------------


def test_restored_rows_return_to_the_retrieval_corpus(
    tmp_path, monkeypatch
) -> None:
    """CASE I: the point of restoring -- the row is a candidate again.

    ``LearningRepository.candidates`` filters on ``validity_active`` in SQL,
    so this checks the consequence rather than only the column. The row
    retired for another reason is still absent.
    """

    from repositories.learning_repository import LearningRepository

    database, _ids, _applied, _before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    answers = {
        str(item.get("final_answer") or "")
        for item in LearningRepository(database).candidates(
            store_code="OJE_PLUS", market="NAVER", limit=2000
        )
    }
    assert PLAIN_POLICY_WORDING in answers
    assert BRACKET_WORDING in answers
    assert TYPO_WORDING in answers
    assert MATERIAL_WORDING in answers
    assert UNRELATED_RETIRED_WORDING not in answers


# ----------------------------------------------------------------------
# J, K: restoring validity is not the same as making a row usable
# ----------------------------------------------------------------------


def test_a_restored_row_holding_a_redaction_token_stays_out_of_retrieval(
    tmp_path, monkeypatch
) -> None:
    """CASE J: the redaction gate is not weakened by this migration.

    ``<masked-phone>`` is a record that something was removed, not a sentence
    to show anyone, and ``search`` drops such a row whatever its validity. Six
    of the live twenty are in this state; restoring them puts them back in the
    repository pool and no further.
    """

    from repositories.learning_repository import LearningRepository
    from services.similar_answer_service import SimilarAnswerService

    database, _ids, _applied, _before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    repository = LearningRepository(database)
    pool_answers = {
        str(item.get("final_answer") or "")
        for item in repository.candidates(
            store_code="OJE_PLUS", market="NAVER", limit=2000)
    }
    # Restored, so it is in the pool...
    assert TOKEN_WORDING in pool_answers
    # ...and still never offered as evidence.
    selected = SimilarAnswerService(repository).search(
        "기존 브라켓에 설치 가능하면 브라켓만 부분 반품할 수 있나요",
        store_code="OJE_PLUS", limit=50, hard_conflicts_only=True,
    )
    offered = {str(item.get("final_answer") or "") for item in selected}
    assert TOKEN_WORDING not in offered
    assert PLAIN_POLICY_WORDING in offered


def test_a_restored_row_the_quality_gate_rejects_is_still_rejected(
    tmp_path, monkeypatch
) -> None:
    """CASE K: the runtime quality gate is not weakened either.

    One of the live twenty states the policy in a sentence the gate reads as
    one customer's order fact, and ``PAST_ORDER_FACT_NOT_REUSABLE`` is in
    ``DATA_UNSAFE_REASONS`` -- so the row is removed before the search sees
    it, approved or not.  Whether that gate is too broad is a separate
    question from whether this policy is valid, and it is not touched here.
    """

    from repositories.learning_repository import LearningRepository
    from services.historical_learning_quality_service import (
        HistoricalLearningQualityService,
        is_data_unsafe,
    )

    database, _ids, _applied, _before = _retired_as_migration_35_left_them(
        tmp_path, monkeypatch)
    pool = LearningRepository(database).candidates(
        store_code="OJE_PLUS", market="NAVER", limit=2000)
    row = next(item for item in pool
               if str(item.get("final_answer") or "") == ORDER_SPECIFIC_WORDING)
    # Restored: validity no longer excludes it.
    assert row["validity_active"] == 1
    verdict = HistoricalLearningQualityService().assess(
        question=str(row.get("question_original_masked") or ""),
        answer=str(row.get("final_answer") or ""),
        stored_quality=float(row.get("quality_score") or 0),
        active=bool(row.get("active")),
    )
    assert verdict.status == "ORDER_SPECIFIC"
    assert is_data_unsafe(verdict) is True
    assert verdict.context_eligible is False
