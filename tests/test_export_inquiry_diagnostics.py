"""One inquiry, explained in a file you can attach to a message.

Diagnosing inquiry 325318746 required copying a 520MB operational database to a
development machine. The investigation used one inquiry row, one draft, its
stored metadata and that inquiry's activity log -- 32KB once exported.

Two properties matter more than completeness, and most of these tests are about
them.

It reads and never writes. The connection is opened ``mode=ro`` with
``PRAGMA query_only = ON``, and nothing calls the answer pipeline: re-running
generation would answer today's question with today's code and destroy the
evidence being collected. The database file must be byte-identical afterwards.

It leaves the building. Every free-text field goes through the project's own
``LearningPrivacyService.mask`` rather than a second scheme that could drift
from the first, order numbers are reduced to four digits from the column, and
the finished document is walked once more for credential-shaped keys. Learning
and Product Facts contribute identifiers and verdicts only -- an export is a
diagnosis, not a partial copy of the store.

The one exemption is deliberate and was found by running it: a Naver inquiry id
is nine digits, the privacy path reads any 8-15 digit run as a product order
number, and the first export masked the very identifier it was named after.
Identifier fields this module writes from identifier columns are exempt; free
text never is.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from scripts.export_inquiry_diagnostics import (
    IDENTIFIER_KEYS,
    NOT_AVAILABLE,
    REDACTED,
    SCHEMA_VERSION,
    DiagnosticsError,
    build,
    last4,
    main,
    open_read_only,
    scrub,
    write_export,
)


NAVER_ID = "325318746"
ORDER_ID = "2026082643289231"
CUSTOMER_PHONE = "010-1234-5678"


def make_store(path: Path) -> Path:
    """A miniature store with the tables the exporter reads."""

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE inquiries (
            id INTEGER PRIMARY KEY, store_code TEXT, source_type TEXT,
            source_question_id TEXT, external_inquiry_id TEXT,
            inquiry_type TEXT, title TEXT, content TEXT, product_name TEXT,
            product_id TEXT, option_name TEXT, order_id TEXT,
            product_order_id TEXT, order_status TEXT, is_private INTEGER,
            workflow_status TEXT, answer_status TEXT, approval_status TEXT,
            phase9_status TEXT, post_status TEXT, source_answered INTEGER,
            created_at TEXT, updated_at TEXT, registered_at TEXT,
            posted_at TEXT, post_error_code TEXT, post_http_status INTEGER,
            post_actor TEXT, posted_draft_id INTEGER, raw_json TEXT
        );
        CREATE TABLE answer_drafts (
            id INTEGER PRIMARY KEY, inquiry_id INTEGER, source TEXT,
            provider TEXT, category TEXT, reason TEXT, answer_strategy TEXT,
            review_status TEXT, program_status TEXT, is_active INTEGER,
            stale INTEGER, stale_reason TEXT, posted INTEGER, posted_at TEXT,
            created_at TEXT, original_answer TEXT, edited_answer TEXT,
            final_answer TEXT, validation_status TEXT,
            validator_result_json TEXT, metadata_json TEXT,
            inquiry_analysis_json TEXT, selected_facts_json TEXT
        );
        CREATE TABLE activity_logs (
            id INTEGER PRIMARY KEY, inquiry_id INTEGER, level TEXT,
            event_code TEXT, message TEXT, details_json TEXT, created_at TEXT
        );
        """
    )
    metadata = {
        "selected_answer_route": "TEMPLATE",
        "answer_source": "rule_engine",
        "template_id": "스탠드모델",
        "template_match_kind": "FIXED_PRODUCT_ACCESSORY",
        "question_category": "INSTALLATION_GENERAL",
        "gpt_called": False,
        "order_id_status": "VALID",
        "dps_lookup_status": "NOT_REQUIRED",
        "processing_plan": {"analysis": {
            "question_category": "INSTALLATION_GENERAL",
            "inquiry_subtype": "GENERAL_INSTALLATION_GUIDANCE",
            "detected_intent": "GENERAL", "confidence": 0.94,
            "manual_review_required": False, "can_generate_answer": True,
            "requires_dps_lookup": False,
        }},
        "semantic_coverage": {"status": "PASS", "score": 1.0,
                              "subquestions": [
                                  {"question": "스탠드가 없어요",
                                   "status": "COVERED", "reason": "TOPIC_MATCH"}]},
        "atomic_completeness": {"answered": 2, "unresolved": 0,
                                "undetermined": 0, "questions": []},
        "semantic_analysis": {"called": False,
                              "router": {"use_semantic": False, "reasons": []}},
        "product_fact_guard": {"classification": "COMMON_OR_NON_PRODUCT_FACT",
                               "sensitive": False},
        # Something credential-shaped, buried where nobody looks.
        "provider_debug": {"api_key": "sk-live-do-not-export",
                           "authorization": "Bearer abcdef"},
    }
    connection.execute(
        "INSERT INTO inquiries (id, store_code, source_type, "
        " source_question_id, inquiry_type, title, content, product_name, "
        " order_id, post_status, source_answered, created_at, raw_json) "
        "VALUES (1,'OJE_PLUS','NAVER',?,'CUSTOMER_INQUIRY','스탠드가 없어요',?,"
        " 'TV', ?, 'NOT_POSTED', 0, '2026-08-27T13:07:31Z', ?)",
        (NAVER_ID,
         f"오베닉 스탠드가 안왔어요 연락처는 {CUSTOMER_PHONE} 입니다. "
         f"주문번호 {ORDER_ID}",
         ORDER_ID,
         json.dumps({"huge": "x" * 5000})),
    )
    connection.execute(
        "INSERT INTO answer_drafts (id, inquiry_id, source, review_status, "
        " posted, created_at, original_answer, validation_status, "
        " validator_result_json, metadata_json) "
        "VALUES (10,1,'rule_engine','PENDING',0,'2026-08-27T13:10:15Z',"
        " '스탠드는 오베닉 스탠드 FMS 모델로 출고되고 있습니다.','PASS',?,?)",
        (json.dumps({"passed": True}), json.dumps(metadata, ensure_ascii=False)),
    )
    for index, (code, message) in enumerate([
        ("INQUIRY_ANALYZED", "문의를 분석했습니다"),
        ("ANSWER_ROUTE_SELECTED", "TEMPLATE 경로를 선택했습니다"),
        ("VALIDATOR_PASSED", "검증을 통과했습니다"),
    ], start=1):
        connection.execute(
            "INSERT INTO activity_logs (id, inquiry_id, level, event_code, "
            " message, details_json, created_at) VALUES (?,1,'INFO',?,?,?,?)",
            (index, code, message, json.dumps({"step": index}),
             f"2026-08-27T13:0{index}:00Z"),
        )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def store(tmp_path) -> Path:
    return make_store(tmp_path / "store.db")


@pytest.fixture
def document(store) -> dict:
    connection = open_read_only(store)
    try:
        return build(connection, naver_id=NAVER_ID, database_path=store)
    finally:
        connection.close()


def digest(path: Path) -> tuple[str, int, int]:
    data = path.read_bytes()
    stat = path.stat()
    return hashlib.sha256(data).hexdigest(), stat.st_size, int(stat.st_mtime)


# ==========================================================================
# 1. It reads, and it cannot write
# ==========================================================================


def test_the_connection_refuses_writes(store) -> None:
    connection = open_read_only(store)
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE inquiries SET title='changed'")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM inquiries")
    finally:
        connection.close()


def test_the_database_is_byte_identical_afterwards(store, tmp_path) -> None:
    before = digest(store)

    connection = open_read_only(store)
    document = build(connection, naver_id=NAVER_ID, database_path=store)
    connection.close()
    write_export(document, output_dir=tmp_path / "out")

    assert digest(store) == before


def test_a_missing_database_fails_before_anything_is_written(tmp_path) -> None:
    with pytest.raises(DiagnosticsError) as raised:
        open_read_only(tmp_path / "absent.db")

    assert "not found" in str(raised.value)
    assert not (tmp_path / "diagnostics").exists()


def test_nothing_in_the_module_can_reach_a_model_or_a_service() -> None:
    """The exporter reads records. It must not acquire a pipeline."""

    import inspect

    import scripts.export_inquiry_diagnostics as module

    source = inspect.getsource(module)
    # Call surfaces, not field names: the exporter legitimately reads a column
    # called requires_dps_lookup, and must never be able to perform one.
    for forbidden in (
        "generate_for_inquiry(", "generate_json(", "ensure_for_inquiry(",
        "AnswerService", "AutomaticDraftService", "InquiryAnalysisService",
        "DpsEnrichmentService", "NaverPostService", "create_gpt_provider",
        "import requests", "GptSemanticAnalyzerService",
    ):
        assert forbidden not in source, forbidden


# ==========================================================================
# 2. Finding the inquiry
# ==========================================================================


def test_it_is_found_by_the_naver_id(document) -> None:
    assert document["export"]["naver_inquiry_id"] == NAVER_ID
    assert document["export"]["schema_version"] == SCHEMA_VERSION


def test_it_is_found_by_the_internal_id(store) -> None:
    connection = open_read_only(store)
    try:
        document = build(connection, internal_id=1, database_path=store)
    finally:
        connection.close()

    assert document["inquiry"]["internal_id"] == 1


def test_an_unknown_id_is_refused(store) -> None:
    connection = open_read_only(store)
    try:
        with pytest.raises(DiagnosticsError):
            build(connection, naver_id="999999999", database_path=store)
        with pytest.raises(DiagnosticsError):
            build(connection, internal_id=4242, database_path=store)
    finally:
        connection.close()


# ==========================================================================
# 3. Privacy
# ==========================================================================


def test_the_full_order_number_never_appears(document) -> None:
    text = json.dumps(document, ensure_ascii=False)

    assert ORDER_ID not in text
    assert document["inquiry"]["order_id"] == "last4:9231"
    assert re.search(r"(?<!\d)\d{16}(?!\d)", text) is None


def test_a_phone_number_never_appears(document) -> None:
    text = json.dumps(document, ensure_ascii=False)

    assert CUSTOMER_PHONE not in text
    assert "<masked-phone>" in document["inquiry"]["content"]


def test_credential_shaped_keys_are_redacted(document) -> None:
    text = json.dumps(document, ensure_ascii=False)

    assert "sk-live-do-not-export" not in text
    assert "Bearer abcdef" not in text
    for word in ("token", "secret", "password", "authorization",
                 "cookie", "api_key"):
        for match in re.finditer(
            rf'"[^"]*{word}[^"]*"\s*:\s*"([^"]*)"', text, re.IGNORECASE
        ):
            assert match.group(1) == REDACTED, match.group(0)


@pytest.mark.parametrize("payload,expected", [
    ({"api_key": "abc"}, REDACTED),
    ({"Authorization": "Bearer x"}, REDACTED),
    ({"session_id": "s"}, REDACTED),
    ({"refresh_token": "r"}, REDACTED),
    ({"nested": {"client_secret": "c"}}, None),
])
def test_the_final_scan_catches_credentials_anywhere(payload, expected) -> None:
    cleaned = scrub(payload)

    if expected is None:
        assert cleaned["nested"]["client_secret"] == REDACTED
    else:
        assert list(cleaned.values()) == [expected]


def test_the_identifier_this_file_is_named_after_survives_masking() -> None:
    """Found by running it: a nine-digit id looks like a product order id."""

    cleaned = scrub({"naver_inquiry_id": NAVER_ID, "content": NAVER_ID})

    assert cleaned["naver_inquiry_id"] == NAVER_ID
    assert cleaned["content"] != NAVER_ID, "free text is still masked"
    assert "naver_inquiry_id" in IDENTIFIER_KEYS


def test_an_identifier_exemption_never_covers_free_text() -> None:
    """Only scalars written from identifier columns are exempt."""

    cleaned = scrub({"naver_inquiry_id": {"content": CUSTOMER_PHONE}})

    assert CUSTOMER_PHONE not in json.dumps(cleaned)


@pytest.mark.parametrize("value,expected", [
    (ORDER_ID, "last4:9231"), ("12", "last4:short"), (None, None), ("", None),
])
def test_only_four_digits_of_an_identifier_are_carried(value, expected) -> None:
    assert last4(value) == expected


def test_bulk_blobs_are_recorded_but_not_copied(document) -> None:
    """raw_json holds a third-party payload. Its existence is the diagnostic."""

    text = json.dumps(document, ensure_ascii=False)

    assert "x" * 200 not in text
    assert len(text) < 60_000


# ==========================================================================
# 4. What the document has to explain
# ==========================================================================


def test_every_section_is_present(document) -> None:
    assert set(document) == {
        "export", "inquiry", "analysis", "atomic_questions", "order_and_dps",
        "answer_routing", "semantic", "verdicts", "draft", "auto_post",
        "evidence", "product_facts", "workflow_steps", "activity",
    }


def test_the_root_cause_is_reconstructable(document) -> None:
    """Everything the 325318746 investigation needed, from the file alone."""

    assert "안왔" in document["inquiry"]["content"]
    assert document["analysis"]["question_category"] == "INSTALLATION_GENERAL"
    assert document["analysis"]["confidence"] == 0.94
    assert document["semantic"]["router"]["use_semantic"] is False
    assert document["answer_routing"]["selected_answer_route"] == "TEMPLATE"
    assert document["answer_routing"]["template_id"] == "스탠드모델"
    assert (document["answer_routing"]["template_match_kind"]
            == "FIXED_PRODUCT_ACCESSORY")
    assert document["verdicts"]["validation_status"] == "PASS"
    assert document["verdicts"]["semantic_coverage"]["status"] == "PASS"
    assert document["verdicts"]["semantic_coverage"]["score"] == 1.0
    assert document["inquiry"]["post_status"] == "NOT_POSTED"
    assert document["draft"]["posted"] == 0


def test_activity_is_in_the_order_it_happened(document) -> None:
    events = [item["event"] for item in document["activity"]]

    assert events == [
        "INQUIRY_ANALYZED", "ANSWER_ROUTE_SELECTED", "VALIDATOR_PASSED",
    ]
    stamps = [item["at"] for item in document["activity"]]
    assert stamps == sorted(stamps)


def test_learning_is_referenced_by_id_and_not_by_body(document) -> None:
    evidence = document["evidence"]

    assert evidence["learning_reference_count"] == 0
    assert evidence["learning_references"] == NOT_AVAILABLE
    assert "bodies are not exported" in evidence["note"]


def test_product_facts_are_a_verdict_not_a_dump(document) -> None:
    facts = document["product_facts"]

    assert facts["classification"] == "COMMON_OR_NON_PRODUCT_FACT"
    assert "product_facts.db is not read" in facts["note"]


def test_the_export_names_no_machine_or_path(document, store) -> None:
    text = json.dumps(document, ensure_ascii=False)

    assert str(store) not in text
    assert document["export"]["source_database"] == store.name


# ==========================================================================
# 5. Incomplete stores
# ==========================================================================


def test_an_inquiry_without_a_draft_exports_cleanly(tmp_path) -> None:
    path = make_store(tmp_path / "nodraft.db")
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM answer_drafts")
    connection.commit()
    connection.close()

    reader = open_read_only(path)
    try:
        document = build(reader, naver_id=NAVER_ID, database_path=path)
    finally:
        reader.close()

    assert document["draft"] == {"exists": False, "draft_id": None}
    assert document["analysis"] == NOT_AVAILABLE
    assert document["answer_routing"] == NOT_AVAILABLE
    assert document["verdicts"] == NOT_AVAILABLE


def test_a_missing_optional_table_is_not_a_failure(tmp_path) -> None:
    """An older deployment simply does not have every table."""

    path = make_store(tmp_path / "old.db")
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE activity_logs")
    connection.commit()
    connection.close()

    reader = open_read_only(path)
    try:
        document = build(reader, naver_id=NAVER_ID, database_path=path)
    finally:
        reader.close()

    assert document["activity"] == NOT_AVAILABLE
    assert document["inquiry"]["internal_id"] == 1


def test_unreadable_stored_json_is_reported_not_guessed(tmp_path) -> None:
    path = make_store(tmp_path / "broken.db")
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE answer_drafts SET metadata_json='{not json'")
    connection.commit()
    connection.close()

    reader = open_read_only(path)
    try:
        document = build(reader, naver_id=NAVER_ID, database_path=path)
    finally:
        reader.close()

    assert document["analysis"] == NOT_AVAILABLE
    assert document["draft"]["exists"] is True


# ==========================================================================
# 6. Writing the file
# ==========================================================================


def test_an_existing_export_is_never_overwritten(document, tmp_path) -> None:
    out = tmp_path / "diagnostics"

    first = write_export(document, output_dir=out)
    second = write_export(document, output_dir=out)

    assert first != second
    assert first.exists() and second.exists()
    assert first.read_text(encoding="utf-8")


def test_the_file_name_carries_the_inquiry_id(document, tmp_path) -> None:
    path = write_export(document, output_dir=tmp_path / "diagnostics")

    assert NAVER_ID in path.name
    assert path.suffix == ".json"


def test_the_cli_writes_one_file(store, tmp_path, capsys) -> None:
    out = tmp_path / "cli"

    exit_code = main([
        "--inquiry", NAVER_ID, "--database", str(store), "--out", str(out),
    ])

    assert exit_code == 0
    written = list(out.glob("*.json"))
    assert len(written) == 1
    assert "exported ->" in capsys.readouterr().out
    assert json.loads(written[0].read_text(encoding="utf-8"))


def test_the_cli_requires_exactly_one_identifier(store) -> None:
    with pytest.raises(SystemExit):
        main(["--database", str(store)])
    with pytest.raises(SystemExit):
        main(["--inquiry", NAVER_ID, "--internal-id", "1",
              "--database", str(store)])


def test_the_cli_reports_an_unknown_inquiry_without_writing(
    store, tmp_path,
) -> None:
    out = tmp_path / "none"

    with pytest.raises(SystemExit):
        main(["--inquiry", "111111111", "--database", str(store),
              "--out", str(out)])

    assert not out.exists()
