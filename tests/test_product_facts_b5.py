"""B5: Product Facts as a restricted evidence source.

Two layers of coverage:

* Fixture tests build a small product_facts-shaped database so every unsafe
  state (NEEDS_REVIEW, CONFLICT, empty value, missing provenance, superseded,
  foreign model, accessory scope) is exercised deterministically.
* Real-DB tests read ``data/product_facts.db`` READ-ONLY and skip when it is
  absent, so the shipped knowledge base is checked as it actually is.

Nothing here writes to either database, and no Naver/GPT/DPS call is made.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from repositories.product_fact_repository import (
    ProductFactRepository,
    ProductFactsUnavailableError,
    get_product_facts_path,
)
from services.product_knowledge_service import (
    ProductKnowledgeService,
    fields_for_question,
)


# --------------------------------------------------------------------------
# fixture database shaped like product_facts.db
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE listings(listing_id TEXT PRIMARY KEY, product_id TEXT,
    product_url TEXT, input_listing_name TEXT, pilot_category TEXT,
    run_id TEXT, collection_status TEXT, collection_run_id TEXT);
CREATE TABLE canonical_facts(canonical_fact_id TEXT PRIMARY KEY, field TEXT,
    scope TEXT, scope_key TEXT, volatility TEXT, verification_status TEXT,
    resolution_status TEXT, selected_value_id TEXT, parser_version TEXT,
    analysis_version TEXT, source_set_hash TEXT, collected_at TEXT,
    valid_from TEXT, valid_until TEXT, last_verified_at TEXT,
    created_at TEXT, updated_at TEXT, identity_source TEXT,
    ontology_rule_id TEXT, lifecycle_status TEXT DEFAULT 'ACTIVE');
CREATE TABLE canonical_fact_values(value_id TEXT PRIMARY KEY,
    canonical_fact_id TEXT, raw_value_json TEXT, normalized_value_json TEXT,
    normalized_hash TEXT, normalization_rule TEXT, normalizer_version TEXT,
    relationship_status TEXT, first_seen_at TEXT, last_seen_at TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE canonical_fact_provenance(canonical_provenance_id TEXT PRIMARY KEY,
    canonical_fact_id TEXT, value_id TEXT, source_fact_id TEXT,
    source_provenance_id TEXT, source_run_id TEXT, source_type TEXT,
    source_url TEXT, source_section TEXT, source_locator TEXT,
    source_text TEXT, source_hash TEXT, extraction_method TEXT,
    analyzer TEXT, analyzer_version TEXT, confidence REAL, image_sha256 TEXT,
    image_region TEXT, collected_at TEXT, created_at TEXT,
    source_status TEXT, lifecycle_status TEXT DEFAULT 'ACTIVE');
CREATE TABLE canonical_fact_listings(canonical_fact_id TEXT, listing_id TEXT,
    product_id TEXT, model_code TEXT, discovered_run_id TEXT,
    first_seen_at TEXT, last_seen_at TEXT,
    PRIMARY KEY(canonical_fact_id, listing_id));
"""

PRODUCT_ID = "P-1000"
LISTING_ID = "L-1000"
MODEL = "LS32DM501EKXKR"


def _add_fact(
    connection, *, fact_id, field, value_json,
    verification="VERIFIED", resolution="SINGLE_SOURCE",
    lifecycle="ACTIVE", volatility="STATIC_PRODUCT_FACT",
    provenance=True, provenance_status="VERIFIED",
    provenance_lifecycle="ACTIVE", selected=True, model_code=MODEL,
    scope="PRODUCT_SPECIFIC",
):
    value_id = f"V-{fact_id}"
    connection.execute(
        "INSERT INTO canonical_facts(canonical_fact_id, field, scope, "
        "scope_key, volatility, verification_status, resolution_status, "
        "selected_value_id, lifecycle_status) VALUES(?,?,?,?,?,?,?,?,?)",
        (fact_id, field, scope, MODEL, volatility, verification, resolution,
         value_id if selected else None, lifecycle),
    )
    connection.execute(
        "INSERT INTO canonical_fact_values(value_id, canonical_fact_id, "
        "raw_value_json, normalized_value_json, relationship_status) "
        "VALUES(?,?,?,?,?)",
        (value_id, fact_id, value_json, value_json, resolution),
    )
    if provenance:
        connection.execute(
            "INSERT INTO canonical_fact_provenance(canonical_provenance_id, "
            "canonical_fact_id, value_id, source_type, source_url, "
            "source_text, source_status, lifecycle_status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (f"PR-{fact_id}", fact_id, value_id, "DETAIL_PAGE",
             "https://example.invalid/p", "사양표", provenance_status,
             provenance_lifecycle),
        )
    connection.execute(
        "INSERT INTO canonical_fact_listings(canonical_fact_id, listing_id, "
        "product_id, model_code, discovered_run_id) VALUES(?,?,?,?,?)",
        (fact_id, LISTING_ID, PRODUCT_ID, model_code, "RUN-1"),
    )


@pytest.fixture
def facts_db(tmp_path) -> Path:
    path = tmp_path / "product_facts.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO listings(listing_id, product_id, input_listing_name) "
        "VALUES(?,?,?)", (LISTING_ID, PRODUCT_ID, "삼성 M5 스마트모니터"),
    )
    # A: fully usable
    _add_fact(connection, fact_id="F-HDMI", field="hdmi_port_count",
              value_json="2")
    # B: verification NEEDS_REVIEW
    _add_fact(connection, fact_id="F-VESA", field="vesa_mm",
              value_json='{"horizontal":100,"vertical":100}',
              verification="NEEDS_REVIEW")
    # C: resolution CONFLICT
    _add_fact(connection, fact_id="F-SPK", field="speaker_present",
              value_json='"YES"', resolution="CONFLICT")
    # D: empty / unknown value
    _add_fact(connection, fact_id="F-BT", field="bluetooth_version",
              value_json='"UNKNOWN"')
    # E: no provenance at all
    _add_fact(connection, fact_id="F-WIFI", field="wifi_standard",
              value_json='"Wi-Fi 5"', provenance=False)
    # E2: provenance exists but is not VERIFIED
    _add_fact(connection, fact_id="F-PANEL", field="panel_type",
              value_json='"VA"', provenance_status="NEEDS_REVIEW")
    # E3: provenance superseded
    _add_fact(connection, fact_id="F-RR", field="refresh_rate",
              value_json="60", provenance_lifecycle="SUPERSEDED")
    # F: foreign model
    _add_fact(connection, fact_id="F-USB", field="usb_port_count",
              value_json="2", model_code="OTHER-MODEL-9999")
    # superseded canonical fact
    _add_fact(connection, fact_id="F-OLD", field="response_time_ms",
              value_json="4", lifecycle="SUPERSEDED")
    # volatile listing fact
    _add_fact(connection, fact_id="F-PRICE", field="listing_price",
              value_json="399000", volatility="DYNAMIC_LISTING_FACT")
    # accessory-scope fact
    _add_fact(connection, fact_id="F-ACC", field="accessory_max_load_kg",
              value_json="12")
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def service(facts_db) -> ProductKnowledgeService:
    return ProductKnowledgeService(ProductFactRepository(facts_db))


def _fields(result):
    return {item.field_key: item for item in result.safe_facts}


def _excluded(result):
    return {item.field_key: item.exclusion_reason for item in result.excluded_facts}


# ------------------------------------------------------------- A. usable fact
def test_A_verified_fact_with_provenance_is_usable(service):
    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="HDMI 단자가 몇 개인가요?",
        model_code=MODEL,
    )
    assert result.matched is True
    fact = _fields(result)["hdmi_port_count"]
    assert fact.value == 2
    assert fact.safe_for_answer is True
    assert fact.exclusion_reason is None
    assert fact.provenance and fact.component_scope == "BASE_DEVICE"
    assert "hdmi_port_count" in result.evidence_text()


# --------------------------------------- B-G. every unsafe state is excluded
@pytest.mark.parametrize(
    "question,field,expected_reason",
    [
        ("베사홀 규격 알려주세요", "vesa_mm", "VERIFICATION_NEEDS_REVIEW"),
        ("스피커 내장인가요?", "speaker_present", "RESOLUTION_CONFLICT"),
        ("블루투스 되나요?", "bluetooth_version", "VALUE_EMPTY_OR_UNKNOWN"),
        ("와이파이 되나요?", "wifi_standard", "NO_ACTIVE_PROVENANCE"),
        ("패널 종류가 뭔가요?", "panel_type", "PROVENANCE_NOT_VERIFIED"),
        ("주사율 얼마인가요?", "refresh_rate", "NO_ACTIVE_PROVENANCE"),
        ("응답속도 얼마인가요?", "response_time_ms", "SUPERSEDED_BY_LATER_RUN"),
    ],
)
def test_BCDE_unsafe_states_are_excluded(service, question, field, expected_reason):
    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question=question, model_code=MODEL,
    )
    assert field not in _fields(result), f"{field} must not be usable"
    assert _excluded(result).get(field) == expected_reason


def test_F_other_model_provenance_is_excluded(service):
    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="USB 포트 몇 개인가요?",
        model_code=MODEL,
    )
    assert "usb_port_count" not in _fields(result)
    assert _excluded(result)["usb_port_count"] == "MODEL_SCOPE_MISMATCH"


def test_G_accessory_and_base_scope_are_labelled_separately(service):
    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="거치대 최대 하중이 얼마인가요?",
        model_code=MODEL,
    )
    fact = _fields(result)["accessory_max_load_kg"]
    assert fact.component_scope == "ACCESSORY"
    hdmi = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="HDMI 몇 개인가요?", model_code=MODEL,
    )
    assert _fields(hdmi)["hdmi_port_count"].component_scope == "BASE_DEVICE"


def test_volatile_listing_fact_is_never_evidence(service):
    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="가격이 얼마인가요?", model_code=MODEL,
    )
    assert "listing_price" not in _fields(result)


# ------------------------------------------------- D. missing != negative
def test_D_missing_field_never_becomes_a_negative_claim(service):
    """The service must offer nothing rather than an absence."""

    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="와이파이 되나요?", model_code=MODEL,
    )
    assert not result.has_safe_facts
    text = (result.evidence_text() + result.prompt_block()).lower()
    for negative in ("없습니다", "미지원", "지원하지", "not supported", "no "):
        assert negative not in text
    # And the prompt block is empty rather than asserting anything.
    assert result.prompt_block() == ""


def test_prompt_block_states_the_unknown_rule(service):
    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="HDMI 몇 개인가요?", model_code=MODEL,
    )
    block = result.prompt_block()
    assert "hdmi_port_count" in block
    assert "UNKNOWN" in block
    assert "Never say a feature is absent" in block
    assert "Never infer a value from another model" in block


# ------------------------------------------------------- lookup restrictions
def test_unrelated_question_requests_no_product_fields(service):
    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="배송 언제 오나요?", model_code=MODEL,
    )
    assert result.requested_fields == ()
    assert result.unavailable_reason == "NO_PRODUCT_FACT_TOPIC"
    assert not result.has_safe_facts


def test_unknown_product_returns_no_facts(service):
    result = service.facts_for_inquiry(
        product_id="NOT-IN-DB", question="HDMI 몇 개인가요?",
    )
    assert result.matched is False
    assert result.unavailable_reason == "PRODUCT_NOT_IN_PRODUCT_DB"
    assert not result.has_safe_facts


def test_missing_product_id_is_not_guessed_from_the_name(service):
    result = service.facts_for_inquiry(
        product_id="", question="HDMI 몇 개인가요?",
    )
    assert result.unavailable_reason == "NO_PRODUCT_ID"
    assert not result.has_safe_facts


def test_missing_database_degrades_quietly(tmp_path):
    service = ProductKnowledgeService(
        ProductFactRepository(tmp_path / "absent.db")
    )
    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID, question="HDMI 몇 개인가요?",
    )
    assert result.unavailable_reason == "PRODUCT_FACTS_DB_UNAVAILABLE"
    assert not result.has_safe_facts


def test_compound_question_keeps_each_answerable_topic(service):
    """One unanswerable sub-question must not discard the other's fact."""

    result = service.facts_for_inquiry(
        product_id=PRODUCT_ID,
        questions=["HDMI 단자가 몇 개인가요", "와이파이는 되나요"],
        model_code=MODEL,
    )
    assert "hdmi_port_count" in _fields(result)
    assert "wifi_standard" not in _fields(result)


def test_field_topic_mapping_is_scoped_to_the_question():
    fields, topics = fields_for_question("HDMI 몇 개인가요?")
    assert "hdmi_port_count" in fields and "vesa_mm" not in fields
    assert topics == ("hdmi",)
    assert fields_for_question("반품하고 싶습니다") == ((), ())
    assert fields_for_question("") == ((), ())


# ------------------------------------------------------------ read-only guard
def test_repository_refuses_to_write(facts_db):
    repository = ProductFactRepository(facts_db)
    with repository.connection() as connection:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        for statement in (
            "CREATE TABLE probe(x)",
            "UPDATE canonical_facts SET field='x'",
            "DELETE FROM canonical_facts",
            "INSERT INTO listings(listing_id) VALUES('z')",
        ):
            with pytest.raises(sqlite3.OperationalError):
                connection.execute(statement)
        assert connection.total_changes == 0


def test_absent_database_raises_a_typed_error(tmp_path):
    repository = ProductFactRepository(tmp_path / "nope.db")
    assert repository.available() is False
    with pytest.raises(ProductFactsUnavailableError):
        with repository.connection():
            pass


def test_default_path_is_configurable(monkeypatch, tmp_path):
    assert get_product_facts_path() == Path("data") / "product_facts.db"
    monkeypatch.setenv("OJE_PRODUCT_FACTS_DB_PATH", str(tmp_path / "x.db"))
    assert get_product_facts_path() == tmp_path / "x.db"
    assert get_product_facts_path(tmp_path / "y.db") == tmp_path / "y.db"


# --------------------------------------------------------------------------
# Real shipped database, READ-ONLY. Skipped where it is not present.
# --------------------------------------------------------------------------
REAL_DB = Path("data") / "product_facts.db"
real_db = pytest.mark.skipif(
    not REAL_DB.is_file(), reason="data/product_facts.db not present"
)


@real_db
def test_real_db_is_opened_read_only():
    repository = ProductFactRepository(REAL_DB)
    with repository.connection() as connection:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE __probe__(x)")
        assert connection.total_changes == 0


@real_db
def test_real_db_sample_product_resolves_verified_specs():
    """A product the audit reports as well covered answers spec questions."""

    service = ProductKnowledgeService(ProductFactRepository(REAL_DB))
    result = service.facts_for_inquiry(
        product_id="10198648691",
        questions=["HDMI 단자가 몇 개인가요", "베사홀 규격이 어떻게 되나요"],
    )
    assert result.matched is True
    keys = result.safe_field_keys()
    assert "hdmi_port_count" in keys
    assert "vesa_mm" in keys
    for fact in result.safe_facts:
        assert fact.verification_status == "VERIFIED"
        assert fact.resolution_status not in {"CONFLICT", "NEEDS_REVIEW"}
        assert fact.lifecycle_status == "ACTIVE"
        assert fact.provenance, fact.field_key
        assert fact.product_id == "10198648691"


@real_db
def test_real_db_absent_field_stays_absent():
    """Coverage gaps must surface as 'unknown', never as a usable fact."""

    service = ProductKnowledgeService(ProductFactRepository(REAL_DB))
    result = service.facts_for_inquiry(
        product_id="10194603339", question="HDMI 단자가 몇 개인가요?"
    )
    # This listing has hdmi_present but no verified port count.
    assert "hdmi_port_count" not in result.safe_field_keys()
    assert "없" not in result.evidence_text()


@real_db
@pytest.mark.parametrize(
    "product_id",
    ["10198648691", "10194603339", "10281039085", "10245185134",
     "10281774644"],
)
def test_real_db_sample_products_never_yield_unsafe_facts(product_id):
    """Whatever comes back for a real product must pass every condition."""

    service = ProductKnowledgeService(ProductFactRepository(REAL_DB))
    result = service.facts_for_inquiry(
        product_id=product_id,
        questions=["화면 크기", "해상도", "주사율", "HDMI", "베사", "무게",
                   "스피커", "블루투스", "와이파이", "스탠드"],
    )
    for fact in result.safe_facts:
        assert fact.verification_status == "VERIFIED"
        assert fact.resolution_status not in {"CONFLICT", "NEEDS_REVIEW"}
        assert fact.lifecycle_status == "ACTIVE"
        assert fact.volatility != "DYNAMIC_LISTING_FACT"
        assert fact.value not in (None, "", [], {})
        assert any(
            str(item.get("source_status")).upper() == "VERIFIED"
            and str(item.get("lifecycle_status")) == "ACTIVE"
            for item in fact.provenance
        ), fact.field_key


@real_db
def test_real_db_lookup_is_indexed_and_fast():
    import time

    service = ProductKnowledgeService(ProductFactRepository(REAL_DB))
    start = time.perf_counter()
    for _ in range(20):
        service.facts_for_inquiry(
            product_id="10198648691", question="HDMI 단자가 몇 개인가요?"
        )
    elapsed_ms = (time.perf_counter() - start) * 1000 / 20
    assert elapsed_ms < 50, f"{elapsed_ms:.1f}ms per lookup"


# --------------------------------------------------------------------------
# Gate wiring: facts are collected and recorded, but do not yet relax the gate
# --------------------------------------------------------------------------
def test_product_facts_do_not_yet_relax_the_auto_post_gate():
    """PRODUCT_FACT_NOT_VERIFIED must still hold until the model sees them.

    B5 wires collection, judgement and audit. It deliberately stops short of
    letting a verified fact satisfy the gate, because the provider prompt and
    the validator's grounding corpus are not fed from the same facts yet: a
    gate opened on evidence the model never read would pass a guessed value.
    """

    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    validator = {"status": "PASS", "passed": True, "errors": [],
                 "review_signals": [], "warnings": []}
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "content": "HDMI 단자가 몇 개인가요?",
                 "inquiry_type": "PRODUCT_INQUIRY", "source_answered": 0,
                 "post_status": "NOT_POSTED"},
        draft={
            "id": 1, "original_answer": "HDMI 단자는 2개입니다.",
            "review_status": "PENDING", "validation_status": "PASS",
            "validator_result_json": validator, "posted": 0,
            "metadata_json": {
                "selected_answer_route": "GPT_FALLBACK",
                "processing_plan": {"analysis": {}},
                "product_fact_guard": {
                    "sensitive": True,
                    "current_fact_verified": False,
                    "product_knowledge_would_verify": True,
                },
                "hybrid": {"validation": validator},
            },
        },
        route="GPT_FALLBACK",
    )
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "PRODUCT_FACT_NOT_VERIFIED" in verdict.reasons


def test_answer_service_accepts_an_injected_product_knowledge_service(tmp_path):
    """The service is injectable so no test touches the shipped DB by accident."""

    from repositories.database import Database
    from services.answer_service import AnswerService

    database = Database(tmp_path / "svc.db")
    database.initialize()
    knowledge = ProductKnowledgeService(
        ProductFactRepository(tmp_path / "absent.db")
    )
    service = AnswerService(database, product_knowledge=knowledge)
    assert service.product_knowledge is knowledge


# --------------------------------------------------------------------------
# UI: display only, never a second opinion on the gate
# --------------------------------------------------------------------------
def _draft_with_knowledge(knowledge: dict) -> dict:
    validator = {"status": "PASS", "passed": True, "errors": [],
                 "review_signals": [], "warnings": []}
    return {
        "id": 1, "original_answer": "안내드립니다.", "review_status": "PENDING",
        "validation_status": "PASS", "validator_result_json": validator,
        "posted": 0,
        "metadata_json": {
            "selected_answer_route": "TEMPLATE",
            "processing_plan": {"analysis": {}},
            "product_fact_guard": {
                "sensitive": True, "current_fact_verified": False,
                "product_knowledge": knowledge,
            },
            "hybrid": {"validation": validator},
        },
    }


def test_ui_shows_verified_and_withheld_product_facts():
    from ui.answer_status_presenter import build_answer_status

    view = build_answer_status(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft=_draft_with_knowledge({
            "matched": True,
            "safe_facts": [
                {"field_key": "hdmi_port_count", "value": 2, "unit": "개"},
            ],
            "excluded_facts": [
                {"field_key": "vesa_mm",
                 "exclusion_reason": "VERIFICATION_NEEDS_REVIEW"},
            ],
        }),
        route="TEMPLATE",
    )
    assert view.product_fact_label == "VERIFIED · 1건 (제외 1건)"
    assert ("hdmi_port_count", "2 개") in view.product_facts
    field, message = view.product_fact_exclusions[0]
    assert field == "vesa_mm"
    assert "검증 대기" in message


def test_ui_explains_an_empty_value_without_calling_it_unsupported():
    from ui.answer_status_presenter import build_answer_status

    view = build_answer_status(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft=_draft_with_knowledge({
            "matched": True, "safe_facts": [],
            "excluded_facts": [
                {"field_key": "wifi_standard",
                 "exclusion_reason": "VALUE_EMPTY_OR_UNKNOWN"},
            ],
        }),
        route="TEMPLATE",
    )
    _, message = view.product_fact_exclusions[0]
    assert "미지원이라는 뜻이 아닙니다" in message


def test_ui_omits_the_section_when_no_product_knowledge_was_looked_up():
    from ui.answer_status_presenter import build_answer_status

    view = build_answer_status(
        inquiry={"source_answered": 0, "post_status": "NOT_POSTED"},
        draft={
            "id": 1, "original_answer": "안내드립니다.",
            "review_status": "PENDING", "validation_status": "PASS",
            "validator_result_json": {"status": "PASS", "passed": True,
                                      "errors": [], "review_signals": []},
            "posted": 0,
            "metadata_json": {"selected_answer_route": "TEMPLATE",
                              "processing_plan": {"analysis": {}}},
        },
        route="TEMPLATE",
    )
    assert view.product_fact_label == ""
    assert view.product_facts == ()
