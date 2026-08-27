"""Who wrote a Learning answer, and which product it is actually about.

Two separate defects, both about identity.

Authority. ``metadata.human_verified`` was one bit doing two jobs. 306 active
rows carry it: 299 are seller answers that were already posted on Naver and
marked verified in bulk, 7 are answers a member of staff edited or reviewed in
this system. Ranking read the bit alone and returned 10 for all of them --
above APPROVED_EDITED's 5 -- so the 299 outranked the 7. The store already
records the difference in ``answer_provenance``, so the class is read rather
than guessed, and staff who compose an answer from scratch stay out of the
ladder entirely because no column distinguishes them from an edited draft.

That priority was also inert: it is the second sort key behind a float
relevance, and across the 36-question replay corpus nothing ever tied, so
inverting the whole table changed no selection at all. Authority now settles
candidates within a 0.01 relevance band -- but only settles them. It cannot
admit a candidate the topic, attribute or product gates rejected.

Model identity. Every row's ``model_code`` column is empty, and the Product
Knowledge database can fill some of them. The risk is not a missing value but
a wrong one: the compatibility gate hard-rejects two different model codes, so
a bad value silently deletes that product's Learning from its own answers. The
catalogue's ``model_code`` column is not clean -- half its values are the
numeric product id echoed back, several are display names -- and the listing
and the catalogue sometimes spell the same television differently ("BE85F" vs
"LH85BEFHLGFXKR"). Both spellings are real, which is exactly why neither may
be chosen automatically.
"""
from __future__ import annotations

import itertools
import json
import sqlite3

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from scripts.backfill_learning_model_code import (
    DEFAULT_AUTOMATION_DB_PATH,
    SKIP_ALREADY_SET,
    SKIP_AMBIGUOUS,
    SKIP_CONTRADICTS_LISTING,
    SKIP_NOT_IN_PRODUCT_KNOWLEDGE,
    SKIP_NO_PRODUCT_ID,
    SKIP_NO_USABLE_MODEL_CODE,
    apply_plan,
    build_plan,
    main,
    resolve_model_code,
)
from services.learning_compatibility_service import (
    LearningCompatibilityService,
    extract_product_identity,
)
from services.learning_evidence_policy import (
    APPROVED_EDITED,
    APPROVED_UNEDITED,
    HISTORICAL_PROMOTED,
    LEARNING_AUTHORITY,
    SELLER_ANSWER,
    SELLER_ANSWER_VERIFIED,
    UNKNOWN_PROVENANCE,
    classify_provenance,
)
from services.similar_answer_service import (
    AUTHORITY_TIE_BAND,
    SimilarAnswerService,
    _ranking_key,
)


PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
PRODUCT_ID = "12139453925"

_next_key = itertools.count()


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "authority.db")
    value.initialize()
    return value


def add(
    database: Database,
    *,
    question: str,
    answer: str,
    learning_source: str = "SELLER_ANSWER",
    provenance: str | None = "NAVER_POSTED",
    human_verified: bool = True,
    source_origin: str | None = None,
    style_only: bool = False,
    rating: int = 5,
    product_name: str = PRODUCT,
    model_code: str | None = None,
    created_at: str = "2026-08-01T00:00:00Z",
) -> int:
    metadata: dict[str, object] = {"learning_signal_type": "POSITIVE"}
    if human_verified:
        metadata["human_verified"] = True
    if provenance:
        metadata["answer_provenance"] = provenance
    if source_origin:
        metadata["source_origin"] = source_origin
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                model_code, final_answer, seller_answer, posted, rating,
                edit_ratio, quality_score, style_only, version, metadata_json,
                active, usage_count, created_at, updated_at, validity_type,
                validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"k-{next(_next_key)}",
                learning_source, question, question, "OJE_PLUS",
                "PRODUCT_INQUIRY", product_name, model_code, answer, answer,
                1, rating, 0.0, 1.0, int(style_only), 1,
                json.dumps(metadata, ensure_ascii=False), 1, 0,
                created_at, created_at, "PERMANENT", 1,
            ),
        )
        return int(cursor.lastrowid)


def ranked_ids(database: Database, question: str, **filters) -> list[int]:
    service = SimilarAnswerService(LearningRepository(database))
    results = service.search(
        question, store_code="OJE_PLUS",
        product_name=filters.pop("product_name", PRODUCT),
        product_id=filters.pop("product_id", PRODUCT_ID),
        inquiry_type="PRODUCT_INQUIRY", limit=3, **filters,
    )
    return [int(item["id"]) for item in results]


def row(database: Database, learning_id: int) -> dict:
    with database.connection() as conn:
        conn.row_factory = sqlite3.Row
        found = dict(conn.execute(
            "SELECT * FROM learning_examples WHERE id=?", (learning_id,)
        ).fetchone())
    found["metadata_json"] = json.loads(found["metadata_json"] or "{}")
    return found


# ==========================================================================
# 1. Provenance is read, never guessed
# ==========================================================================


@pytest.mark.parametrize(
    "learning_source,provenance,human_verified,origin,expected",
    [
        ("APPROVED_EDITED", "STAFF_EDITED", True, None, APPROVED_EDITED),
        ("APPROVED_UNEDITED", "PROGRAM_GENERATED", True, None, APPROVED_UNEDITED),
        ("SELLER_ANSWER", "NAVER_POSTED", True, None, SELLER_ANSWER_VERIFIED),
        ("SELLER_ANSWER", "NAVER_POSTED", False, None, SELLER_ANSWER),
        ("SELLER_ANSWER", None, False, None, SELLER_ANSWER),
        ("APPROVED_EDITED", None, False, None, UNKNOWN_PROVENANCE),
        ("APPROVED_EDITED", "STAFF_EDITED", True, "HISTORICAL_PROMOTED",
         HISTORICAL_PROMOTED),
    ],
)
def test_provenance_classes_match_what_the_store_records(
    learning_source, provenance, human_verified, origin, expected
) -> None:
    item = {
        "learning_source": learning_source,
        "metadata_json": {
            k: v for k, v in (
                ("answer_provenance", provenance),
                ("human_verified", True if human_verified else None),
                ("source_origin", origin),
            ) if v is not None
        },
    }
    assert classify_provenance(item) == expected


def test_staff_written_answers_are_not_invented_as_a_class() -> None:
    """DIRECT_HUMAN is unidentifiable today, so nothing may claim to be it.

    No column separates an answer a person composed from one they edited. A
    class inferred from the text would be a guess wearing a provenance label,
    so such rows fall to the bottom of the ladder rather than the top.
    """

    item = {"learning_source": "SELLER_ANSWER", "metadata_json": {}}
    assert classify_provenance(item) == SELLER_ANSWER
    assert UNKNOWN_PROVENANCE not in LEARNING_AUTHORITY
    assert SELLER_ANSWER not in LEARNING_AUTHORITY
    assert HISTORICAL_PROMOTED not in LEARNING_AUTHORITY


def test_human_verified_alone_no_longer_outranks_staff_work(database) -> None:
    seller = row(database, add(
        database, question="배송 문의", answer="배송은 순차 진행됩니다.",
        learning_source="SELLER_ANSWER", provenance="NAVER_POSTED"))
    edited = row(database, add(
        database, question="배송 문의합니다", answer="배송은 결제 후 진행됩니다.",
        learning_source="APPROVED_EDITED", provenance="STAFF_EDITED"))

    assert seller["metadata_json"]["human_verified"] is True
    assert edited["metadata_json"]["human_verified"] is True
    assert (SimilarAnswerService._source_priority(seller)
            < SimilarAnswerService._source_priority(edited))


def test_the_ladder_is_ordered(database) -> None:
    assert (LEARNING_AUTHORITY[APPROVED_EDITED]
            > LEARNING_AUTHORITY[APPROVED_UNEDITED]
            > LEARNING_AUTHORITY[SELLER_ANSWER_VERIFIED])
    promoted = row(database, add(
        database, question="과거 사례", answer="과거에는 이렇게 안내했습니다.",
        learning_source="APPROVED_EDITED", provenance="STAFF_EDITED",
        source_origin="HISTORICAL_PROMOTED"))
    assert (SimilarAnswerService._source_priority(promoted)
            < LEARNING_AUTHORITY[SELLER_ANSWER_VERIFIED])


# ==========================================================================
# 2. Authority orders comparable candidates -- and only those
# ==========================================================================


QUESTION = "벽걸이 설치 가능한가요?"


def _four_provenances(database: Database) -> dict[str, int]:
    """The same question and answer, differing only in who produced it."""

    answer = "벽걸이 설치는 기사님이 방문하여 진행합니다."
    return {
        "D": add(database, question=QUESTION, answer=answer + " 안내드립니다.",
                 learning_source="APPROVED_EDITED", provenance="STAFF_EDITED",
                 source_origin="HISTORICAL_PROMOTED"),
        "C": add(database, question=QUESTION, answer=answer + " 참고 바랍니다.",
                 learning_source="SELLER_ANSWER", provenance="NAVER_POSTED"),
        "B": add(database, question=QUESTION, answer=answer + " 확인 바랍니다.",
                 learning_source="APPROVED_UNEDITED",
                 provenance="PROGRAM_GENERATED"),
        "A": add(database, question=QUESTION, answer=answer + " 도움 되시길 바랍니다.",
                 learning_source="APPROVED_EDITED", provenance="STAFF_EDITED"),
    }


def test_equally_relevant_candidates_rank_by_who_wrote_them(database) -> None:
    ids = _four_provenances(database)
    order = ranked_ids(database, QUESTION)

    assert order == [ids["A"], ids["B"], ids["C"]], (
        "A > B > C > D must hold when relevance and compatibility do not "
        "separate the candidates"
    )
    assert ids["D"] not in order


def test_authority_cannot_rescue_an_irrelevant_answer(database) -> None:
    """Relevance decides eligibility; authority only orders what qualifies."""

    staff = add(
        database, question="보증기간이 얼마나 되나요?",
        answer="보증기간은 구매일로부터 2년입니다.",
        learning_source="APPROVED_EDITED", provenance="STAFF_EDITED")
    seller = add(
        database, question=QUESTION,
        answer="벽걸이 설치는 기사님이 방문하여 진행합니다.",
        learning_source="SELLER_ANSWER", provenance="NAVER_POSTED")

    order = ranked_ids(database, QUESTION)

    assert order and order[0] == seller
    assert staff not in order


def test_authority_never_promotes_a_topic_rejected_candidate(database) -> None:
    staff = add(
        database, question="온누리 환급 신청 기간이 언제까지인가요?",
        answer="온누리 환급 신청은 9월 5일까지 가능합니다.",
        learning_source="APPROVED_EDITED", provenance="STAFF_EDITED")

    order = ranked_ids(
        database, "온누리 신청할 때 판매처를 뭐라고 입력해야 하나요?")

    assert staff not in order, (
        "asked about the seller name, offered the rebate period -- an "
        "attribute mismatch the ladder must not overturn"
    )


def test_a_clearly_better_match_still_wins_across_the_band(database) -> None:
    """Authority settles a near-tie; it does not overturn a real difference."""

    seller = add(
        database, question="벽걸이 설치 가능한가요?",
        answer="벽걸이 설치는 기사님이 방문하여 진행합니다.",
        learning_source="SELLER_ANSWER", provenance="NAVER_POSTED")
    staff = add(
        database, question="벽걸이 브라켓 규격이 궁금합니다",
        answer="벽걸이 브라켓은 400x400 규격을 사용합니다.",
        learning_source="APPROVED_EDITED", provenance="STAFF_EDITED")

    service = SimilarAnswerService(LearningRepository(database))
    results = service.search(
        QUESTION, store_code="OJE_PLUS", product_name=PRODUCT,
        product_id=PRODUCT_ID, inquiry_type="PRODUCT_INQUIRY", limit=3)
    scores = {int(item["id"]): item["relevance"] for item in results}

    assert scores[seller] - scores.get(staff, 0.0) > AUTHORITY_TIE_BAND
    assert [int(item["id"]) for item in results][0] == seller


def _key(relevance: float, support: float, priority: int):
    return _ranking_key(
        relevance,
        {"answer_support": support, "rating": 5,
         "created_at": "2026-08-01T00:00:00Z"},
        priority,
    )


def test_authority_gives_no_credit_to_an_answer_that_covers_nothing() -> None:
    """Measured regression: this promoted unsupported answers into evidence.

    Over 275 live questions, ranking the band by authority alone moved ten
    answers into the factual slot whose answer support averaged 0.058 -- six of
    them exactly zero, meaning the answer addressed nothing that was asked.
    """

    unsupported = _key(0.3000, 0.0, priority=8)
    supported = _key(0.3005, 0.14, priority=4)

    assert unsupported[0] == supported[0], "same relevance band"
    assert supported > unsupported
    assert unsupported[2] == 0, "no authority credit without support"
    assert supported[2] == 4


def test_authority_still_decides_an_exact_relevance_tie() -> None:
    """When nothing else separates two candidates, provenance always has.

    The band gate above must not take that away: at identical relevance there
    is no better signal left, and removing it silently reordered the existing
    approved-over-legacy preference.
    """

    staff = _key(0.3000, 0.0, priority=8)
    legacy = _key(0.3000, 0.0, priority=1)

    assert staff > legacy


def test_the_tie_band_is_narrow_enough_to_mean_a_tie() -> None:
    """Adjacent live candidates sit a median 0.043 apart, lower quartile 0.018.

    A band at or above the quartile would let authority reorder candidates
    relevance genuinely separates, which is the failure this whole change is
    meant to avoid.
    """

    assert 0 < AUTHORITY_TIE_BAND < 0.018


# ==========================================================================
# 3. model_code resolution: the value must *be* a model code
# ==========================================================================


@pytest.mark.parametrize("value", [
    "LH50BEFHLGFXKR", "LS32FG500EKXKR", "FMS100B", "LH50BE-H",
])
def test_real_model_codes_resolve(value) -> None:
    assert resolve_model_code(value) == value


@pytest.mark.parametrize("value", [
    "12492434806",            # the numeric product id echoed into the column
    "214CM", "85인치", "214.7cm",
    "삼성 80.1cm(32인치) 스탠바이미 이동식 스탠드 패키지",
    "삼성 UHD 4K BE85F 214.7cm(85인치) 비즈니스 TV 스탠드",
    "", None, "TV", "STAND",
])
def test_anything_that_is_not_a_model_code_is_refused(value) -> None:
    assert resolve_model_code(value) is None


def test_a_model_code_is_never_extracted_out_of_a_product_name() -> None:
    """The name contains a real code -- and it is still refused.

    Pulling "BE85F" out of the middle of a name is picking which token looks
    most like a model, which is a guess whether or not it happens to be right.
    """

    name = "삼성 UHD 4K BE85F 214.7cm(85인치) 비즈니스 TV 스탠드"
    assert "BE85F" in name
    assert resolve_model_code(name) is None


# ==========================================================================
# 4. Backfill plan: what it takes, what it leaves alone
# ==========================================================================


@pytest.fixture
def knowledge(tmp_path) -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / "product_facts.db")
    connection.execute(
        "CREATE TABLE canonical_fact_listings ("
        " canonical_fact_id TEXT, listing_id TEXT, product_id TEXT,"
        " model_code TEXT)"
    )
    return connection


def stock(connection: sqlite3.Connection, rows) -> None:
    connection.executemany(
        "INSERT INTO canonical_fact_listings VALUES (?,?,?,?)",
        [(f"f{i}", f"l{i}", pid, code) for i, (pid, code) in enumerate(rows)],
    )
    connection.commit()


def plan_for(database: Database, knowledge: sqlite3.Connection):
    with database.connection() as conn:
        return build_plan(conn, knowledge)


def learning_with_product(
    database: Database, product_id: str | None, **kwargs
) -> int:
    learning_id = add(
        database, question="설치 문의", answer="설치는 기사님이 진행합니다.",
        **kwargs)
    if product_id is not None:
        with database.connection() as conn:
            conn.execute(
                "UPDATE learning_examples "
                "   SET metadata_json=json_set(metadata_json,"
                "       '$.product_identity', json_object('product_id', ?))"
                " WHERE id=?",
                (product_id, learning_id),
            )
    return learning_id


def test_one_product_id_with_one_model_code_is_a_candidate(
    database, knowledge
) -> None:
    stock(knowledge, [("P1", "LH50BEFHLGFXKR")])
    learning_id = learning_with_product(database, "P1", product_name="TV 상품")

    plan = plan_for(database, knowledge)

    assert [c["learning_id"] for c in plan.candidates] == [learning_id]
    assert plan.candidates[0]["resolved_model_code"] == "LH50BEFHLGFXKR"
    assert plan.candidates[0]["product_id"] == "P1"


def test_a_row_without_a_product_id_is_skipped(database, knowledge) -> None:
    stock(knowledge, [("P1", "LH50BEFHLGFXKR")])
    learning_with_product(database, None, product_name="TV 상품")

    plan = plan_for(database, knowledge)

    assert plan.candidates == []
    assert plan.skipped[SKIP_NO_PRODUCT_ID] == 1


def test_a_product_id_product_knowledge_has_never_seen_is_skipped(
    database, knowledge
) -> None:
    stock(knowledge, [("P1", "LH50BEFHLGFXKR")])
    learning_with_product(database, "P-UNKNOWN", product_name="TV 상품")

    plan = plan_for(database, knowledge)

    assert plan.candidates == []
    assert plan.skipped[SKIP_NOT_IN_PRODUCT_KNOWLEDGE] == 1


def test_a_product_id_whose_values_are_all_ids_or_names_is_skipped(
    database, knowledge
) -> None:
    stock(knowledge, [("P1", "P1"), ("P1", "삼성 80.1cm(32인치) 스탠바이미")])
    learning_with_product(database, "P1", product_name="모니터 상품")

    plan = plan_for(database, knowledge)

    assert plan.candidates == []
    assert plan.skipped[SKIP_NO_USABLE_MODEL_CODE] == 1


def test_two_genuine_model_codes_for_one_product_id_is_skipped(
    database, knowledge
) -> None:
    stock(knowledge, [("P1", "LH50BEFHLGFXKR"), ("P1", "LH43BEHHLGFXKR")])
    learning_with_product(database, "P1", product_name="TV 상품")

    plan = plan_for(database, knowledge)

    assert plan.candidates == []
    assert plan.skipped[SKIP_AMBIGUOUS] == 1


def test_a_code_contradicting_the_listing_name_is_skipped(
    database, knowledge
) -> None:
    """The listing says BE85F, the catalogue says LH85BEFHLGFXKR.

    Both name the same television. Writing the catalogue's spelling in makes
    the listing stop matching its own Learning, because the question side
    derives its code from that same listing name.
    """

    stock(knowledge, [("P1", "LH85BEFHLGFXKR")])
    learning_with_product(
        database, "P1",
        product_name="삼성 UHD 4K BE85F 214.7cm(85인치) 비즈니스 TV 스탠드")

    plan = plan_for(database, knowledge)

    assert plan.candidates == []
    assert plan.skipped[SKIP_CONTRADICTS_LISTING] == 1


def test_an_existing_model_code_is_never_overwritten(
    database, knowledge
) -> None:
    stock(knowledge, [("P1", "LH50BEFHLGFXKR")])
    learning_id = learning_with_product(
        database, "P1", product_name="TV 상품", model_code="LH43BEHHLGFXKR")

    plan = plan_for(database, knowledge)

    assert plan.candidates == []
    assert plan.skipped[SKIP_ALREADY_SET] == 1

    with database.connection() as conn:
        apply_plan(conn, plan.candidates)
    assert row(database, learning_id)["model_code"] == "LH43BEHHLGFXKR"


def test_a_dimension_never_becomes_a_model_code(database, knowledge) -> None:
    """214cm and 85인치 describe a size, and no size may enter that column."""

    stock(knowledge, [("P1", "214CM"), ("P1", "85")])
    learning_with_product(
        database, "P1", product_name="삼성 214cm(85인치) 비즈니스 TV")

    plan = plan_for(database, knowledge)

    assert plan.candidates == []
    assert all(
        c["resolved_model_code"] not in {"214CM", "85", "214.7CM"}
        for c in plan.candidates
    )


def test_apply_writes_only_the_planned_rows(database, knowledge) -> None:
    stock(knowledge, [("P1", "LH50BEFHLGFXKR")])
    filled = learning_with_product(database, "P1", product_name="TV 상품")
    untouched = learning_with_product(database, None, product_name="TV 상품")

    plan = plan_for(database, knowledge)
    with database.connection() as conn:
        updated = apply_plan(conn, plan.candidates)

    assert updated == 1
    assert row(database, filled)["model_code"] == "LH50BEFHLGFXKR"
    assert not (row(database, untouched)["model_code"] or "")


def test_apply_refuses_the_production_store(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--apply", "--database", str(DEFAULT_AUTOMATION_DB_PATH)])
    assert "production" in capsys.readouterr().err


def test_apply_refuses_to_guess_a_database(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--apply"])
    assert "--database" in capsys.readouterr().err


# ==========================================================================
# 5. A filled model_code must help the right product and no other
# ==========================================================================


def test_the_same_model_matches_on_the_explicit_code() -> None:
    identity = extract_product_identity(
        product_id="P1", product_name="이동식 스탠드", model_code="FMS100B")
    decision = LearningCompatibilityService().evaluate(
        current_question="HDMI 단자가 몇 개인가요?",
        current_product=identity,
        candidate_question="HDMI 단자 개수 문의",
        candidate_answer="HDMI 단자는 3개입니다.",
        candidate_product=identity,
    )

    assert decision.eligible
    assert decision.product_match == "EXACT_MODEL"


def test_a_different_model_is_rejected_not_merely_unrewarded() -> None:
    asked = extract_product_identity(
        product_id="P1", product_name="TV 상품", model_code="LH50BEFHLGFXKR")
    offered = extract_product_identity(
        product_id="P2", product_name="TV 상품", model_code="LH43BEHHLGFXKR")

    decision = LearningCompatibilityService().evaluate(
        current_question="HDMI 단자가 몇 개인가요?",
        current_product=asked,
        candidate_question="HDMI 단자 개수 문의",
        candidate_answer="HDMI 단자는 3개입니다.",
        candidate_product=offered,
    )

    assert not decision.eligible
    assert decision.reject_reason == "MODEL_MISMATCH"


def test_backfilling_does_not_disturb_an_unrelated_product(
    database, knowledge
) -> None:
    """The row that gets a code, and the row that does not, both still rank."""

    stock(knowledge, [("P1", "FMS100B")])
    filled = add(
        database, question="설치 방법이 궁금합니다",
        answer="설치는 기사님이 방문하여 진행합니다.",
        learning_source="SELLER_ANSWER", provenance="NAVER_POSTED",
        product_name="이동식 스탠드")
    with database.connection() as conn:
        conn.execute(
            "UPDATE learning_examples SET metadata_json=json_set("
            "  metadata_json, '$.product_identity',"
            "  json_object('product_id', 'P1')) WHERE id=?", (filled,))

    before = ranked_ids(database, "설치 방법이 궁금합니다",
                        product_name="이동식 스탠드", product_id="P1")
    plan = plan_for(database, knowledge)
    with database.connection() as conn:
        apply_plan(conn, plan.candidates)
    after = ranked_ids(database, "설치 방법이 궁금합니다",
                       product_name="이동식 스탠드", product_id="P1")

    assert before == [filled]
    assert after == before, "an explicit code must not evict its own listing"
