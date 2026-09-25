from __future__ import annotations

from pathlib import Path

from repositories.database import Database
from services.product_knowledge_service import (
    ProductFact,
    ProductKnowledgeService,
    _resolve_exact_fact_conflicts,
)
from services.similar_answer_service import _resolve_latest_valid_conflicts


def _product_fact(*, model: str, value: object, source: str = "API", at: str | None = None):
    provenance = ({"source_type": source, "collected_at": at},)
    return ProductFact(
        product_id="p1", listing_id="p1", model_code=model,
        field_key="hdmi_port_count", value=value, raw_value=value, unit="개",
        scope="EXACT_MODEL", scope_key=model, component_scope="BASE_DEVICE",
        volatility="STATIC_PRODUCT_FACT", verification_status="VERIFIED",
        resolution_status="RESOLVED", lifecycle_status="ACTIVE",
        canonical_fact_id=f"{model}:{value}:{at}", value_id=None,
        provenance=provenance, safe_for_answer=True, subject="MAIN_PRODUCT",
        source_type=source,
    )


def _learning(identifier: int, *, answer: str, verified_at: str | None):
    metadata = {
        "human_verified": True,
        "product_scope": "MODEL",
        "learning_topics": ["PORT_CONNECTIVITY"],
    }
    if verified_at:
        metadata["verified_at"] = verified_at
    return {
        "id": identifier,
        "final_answer": answer,
        "question_original_masked": "HDMI 단자는 몇 개인가요?",
        "source_product_id": "p1",
        "model_code": "LH43BEDHLGFXKR",
        "metadata_json": metadata,
        "compatibility": {
            "product_scope": "MODEL",
            "candidate_product": {
                "product_id": "p1", "model_code": "LH43BEDHLGFXKR",
                "category": "TV",
            },
        },
    }


def test_689732949_exact_dimensions_reach_runtime_without_all_fields_mode():
    question = (
        "가로세로 몇센치인가요 세로 센치가 없네요 "
        "그리고 보통 삼성 tv40인치는 가로세로가몇일까요?"
    )
    result = ProductKnowledgeService().facts_for_inquiry(
        product_id="11363535046", model_code="LH43BEDHLGFXKR",
        question=question,
    )
    dimensions = [
        fact for fact in result.safe_facts
        if fact.field_key in {"dimensions_product", "dimensions_labelled"}
    ]
    # LH43BEDHLGFXKR reduces to the catalogued LH43BEDH, so the catalogue
    # identifies it and the listing fallback is no longer needed. The facts
    # below are the listing's own, exactly as before.
    assert result.identity_status == "UNIQUE_MATCH"
    assert result.supports_question(question)
    assert dimensions
    assert any("967.5" in str(fact.value) and "561.4" in str(fact.value) for fact in dimensions)
    assert {fact.model_code for fact in dimensions} == {"LH43BEDHLGFXKR"}


def test_product_conflict_uses_newer_source_only_inside_same_exact_identity():
    old = _product_fact(model="MODEL-A", value=2, at="2026-08-01T00:00:00Z")
    new = _product_fact(model="MODEL-A", value=3, at="2026-09-01T00:00:00Z")
    other_model = _product_fact(model="MODEL-B", value=4, at="2026-10-01T00:00:00Z")
    safe, excluded = _resolve_exact_fact_conflicts([old, new, other_model])
    assert {(item.model_code, item.value) for item in safe} == {
        ("MODEL-A", 3), ("MODEL-B", 4),
    }
    assert excluded[0].value == 2
    assert excluded[0].exclusion_reason == "SUPERSEDED_BY_NEWER_AUTHORITATIVE_SOURCE"


def test_product_conflict_without_reliable_source_time_stays_review():
    left = _product_fact(model="MODEL-A", value=2, at=None)
    right = _product_fact(model="MODEL-A", value=3, at=None)
    safe, excluded = _resolve_exact_fact_conflicts([left, right])
    assert safe == []
    assert len(excluded) == 2
    assert {item.resolution_status for item in excluded} == {"CONFLICT"}


def test_latest_human_approved_learning_wins_same_scope_topic_conflict():
    old = _learning(1, answer="HDMI 단자는 2개입니다.", verified_at="2026-08-01T00:00:00Z")
    new = _learning(2, answer="HDMI 단자는 3개입니다.", verified_at="2026-09-01T00:00:00Z")
    ranked, conflicts = _resolve_latest_valid_conflicts(
        [(1.0, old), (0.9, new)], priority_for=lambda _item: 8,
    )
    assert [item[1]["id"] for item in ranked] == [2]
    assert conflicts[0]["winner_learning_id"] == 2
    assert conflicts[0]["reason"] == "NEWER_EFFECTIVE_APPROVAL"


def test_newer_human_policy_can_supersede_older_correction():
    correction = _learning(
        3, answer="HDMI 단자는 2개입니다.", verified_at="2026-08-01T00:00:00Z",
    )
    correction["learning_source"] = "AUTO_POST_CORRECTED"
    current = _learning(
        4, answer="HDMI 단자는 3개입니다.", verified_at="2026-09-01T00:00:00Z",
    )
    current["learning_source"] = "APPROVED_EDITED"
    ranked, conflicts = _resolve_latest_valid_conflicts(
        [(1.0, correction), (0.9, current)], priority_for=lambda _item: 8,
    )
    assert [item[1]["id"] for item in ranked] == [4]
    assert conflicts[0]["winner_learning_id"] == 4


def test_learning_conflict_without_reliable_time_withholds_both():
    left = _learning(5, answer="HDMI 단자는 2개입니다.", verified_at=None)
    right = _learning(6, answer="HDMI 단자는 3개입니다.", verified_at=None)
    ranked, conflicts = _resolve_latest_valid_conflicts(
        [(1.0, left), (0.9, right)], priority_for=lambda _item: 8,
    )
    assert ranked == []
    assert conflicts[0]["winner_learning_id"] is None
    assert conflicts[0]["reason"] == "UNRESOLVED_NO_RELIABLE_TIME"


def test_obsolete_bracket_partial_return_learning_is_preserved_but_invalidated(tmp_path: Path):
    database = Database(tmp_path / "policy-migration.db")
    database.initialize()
    with database.connection() as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=35")
        cursor = connection.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, final_answer,
                seller_answer, posted, rating, edit_ratio, quality_score,
                style_only, version, metadata_json, active, usage_count,
                created_at, updated_at, validity_type, validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "obsolete-bracket", "SELLER_ANSWER", "기존 브라켓 재사용", "기존 브라켓 재사용",
                "OJE_PLUS", "PRODUCT_INQUIRY",
                "기존 브라켓 사용 가능 시 구매한 브라켓은 부분 반품 요청 가능합니다.",
                "기존 브라켓 사용 가능 시 구매한 브라켓은 부분 반품 요청 가능합니다.",
                1, 5, 0.0, 1.0, 0, 1, "{}", 1, 0,
                "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z", "PERMANENT", 1,
            ),
        )
        learning_id = int(cursor.lastrowid)
    assert database.initialize() == [35]
    with database.connection() as connection:
        row = connection.execute(
            "SELECT active, validity_active, validity_note, final_answer "
            "FROM learning_examples WHERE id=?", (learning_id,),
        ).fetchone()
    assert row["active"] == 1
    assert row["validity_active"] == 0
    assert "부분 반품" in row["final_answer"]
    assert "더 이상 답변 근거로 사용하지 않음" in row["validity_note"]
