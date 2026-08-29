"""Phase 11-B: the three gates that had to close before a newer Product DB lands.

R1  A listing that could not be collected today may still describe its panel,
    but not its own offer. Static product facts survive; the listing's price,
    availability, delivery cutoffs and return terms do not.
R2  A package listing's brand and manufacturer describe what is sold, not what
    is bundled inside it. Naming a component withholds the listing identity.
R4  brand, manufacturer and country_of_origin answer three different questions,
    and a product line stored in brand never proves who made the product.

Every gate withholds evidence. None of them produces a negative claim: a
withheld field is unknown, exactly as an absent one already was.

Nothing here writes to any database and no Naver/GPT/DPS call is made.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from repositories.product_fact_repository import ProductFactRepository
from services.product_knowledge_service import (
    ProductKnowledgeService,
    asks_about_a_bundled_component,
    fields_for_question,
)

REAL_DB = Path("data") / "product_facts.db"
real_db = pytest.mark.skipif(
    not REAL_DB.is_file(), reason="data/product_facts.db not present"
)

# Same shape as data/product_facts.db -- the repository selects identity and
# validity columns, so a trimmed schema would fail the lookup instead of
# exercising the gate.
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

PRODUCT_ID = "P-2000"
LISTING_ID = "L-2000"
MODEL = "LS32DM501EKXKR"


def _add(connection, *, fact_id, field, value_json,
         volatility="STATIC_PRODUCT_FACT", verification="VERIFIED",
         resolution="SINGLE_SOURCE"):
    value_id = f"V-{fact_id}"
    connection.execute(
        "INSERT INTO canonical_facts(canonical_fact_id, field, scope, "
        "scope_key, volatility, verification_status, resolution_status, "
        "selected_value_id, lifecycle_status) VALUES(?,?,?,?,?,?,?,?,'ACTIVE')",
        (fact_id, field, "PRODUCT_SPECIFIC", MODEL, volatility, verification,
         resolution, value_id),
    )
    connection.execute(
        "INSERT INTO canonical_fact_values(value_id, canonical_fact_id, "
        "raw_value_json, normalized_value_json, relationship_status) "
        "VALUES(?,?,?,?,?)", (value_id, fact_id, value_json, value_json,
                              resolution),
    )
    connection.execute(
        "INSERT INTO canonical_fact_provenance(canonical_provenance_id, "
        "canonical_fact_id, value_id, source_type, source_url, source_text, "
        "source_status, lifecycle_status) VALUES(?,?,?,?,?,?,'VERIFIED','ACTIVE')",
        (f"PR-{fact_id}", fact_id, value_id, "DETAIL_PAGE",
         "https://example.invalid/p", "사양표"),
    )
    connection.execute(
        "INSERT INTO canonical_fact_listings(canonical_fact_id, listing_id, "
        "product_id, model_code, discovered_run_id) VALUES(?,?,?,?,'RUN-1')",
        (fact_id, LISTING_ID, PRODUCT_ID, MODEL),
    )


def build_db(path: Path, *, collection_status, brand='"삼성"',
             manufacturer='"삼성전자"',
             stand_volatility="STATIC_PRODUCT_FACT") -> Path:
    """A product_facts-shaped database with one listing.

    ``stand_volatility`` exists because of a measured fact about the shipped
    data: every field the topic map is willing to request is
    STATIC_PRODUCT_FACT today (see
    ``test_real_db_topic_map_only_requests_static_fields``). To exercise the
    collection gate at all, the fixture therefore declares one *requestable*
    field with a listing-scoped volatility. That is the shape a future topic
    map entry for a delivery or policy field would have.
    """

    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO listings(listing_id, product_id, input_listing_name, "
        "collection_status) VALUES(?,?,?,?)",
        (LISTING_ID, PRODUCT_ID, "삼성 85인치 비즈니스TV + 셋탑박스",
         collection_status),
    )
    # static: the panel itself
    _add(connection, fact_id="F-HDMI", field="hdmi_port_count", value_json="3")
    _add(connection, fact_id="F-SIZE", field="screen_size",
         value_json='{"inch":85}')
    # requestable, but scoped to the listing rather than the hardware
    _add(connection, fact_id="F-CERT", field="certification_number",
         value_json='"KC-12345"', volatility=stand_volatility)
    # identity
    _add(connection, fact_id="F-BRAND", field="brand", value_json=brand)
    _add(connection, fact_id="F-MAKER", field="manufacturer",
         value_json=manufacturer)
    _add(connection, fact_id="F-ORIGIN", field="country_of_origin",
         value_json='"국산"')
    _add(connection, fact_id="F-NAME", field="model_name",
         value_json='"삼성 비즈니스TV BE85F-H"')
    _add(connection, fact_id="F-STAND", field="stand_type",
         value_json='"BASIC FEET"')
    connection.commit()
    connection.close()
    return path


def service_for(tmp_path, **kwargs) -> ProductKnowledgeService:
    path = build_db(tmp_path / "product_facts.db", **kwargs)
    return ProductKnowledgeService(ProductFactRepository(path))


def ask(service, question):
    return service.facts_for_inquiry(product_id=PRODUCT_ID, question=question)


def safe(result):
    return {item.field_key: item.value for item in result.safe_facts}


def blocked(result):
    return {item.field_key: item.exclusion_reason
            for item in result.excluded_facts}


# ===================================================== R1 collection_status ==
def test_A_collected_listing_answers_static_facts(tmp_path):
    service = service_for(tmp_path, collection_status="COLLECTION_SUCCESS")
    result = ask(service, "HDMI 단자가 몇 개인가요?")
    assert safe(result)["hdmi_port_count"] == 3
    assert result.collection_status == "COLLECTION_SUCCESS"


def test_B_dynamic_fact_is_withheld_even_on_a_collected_listing(tmp_path):
    """The pre-existing rule, restated: listing state is never answer evidence.

    A collected listing changes nothing here -- volatility alone settles it.
    """

    service = service_for(
        tmp_path, collection_status="COLLECTION_SUCCESS",
        stand_volatility="DYNAMIC_LISTING_FACT",
    )
    result = ask(service, "인증번호가 어떻게 되나요?")
    assert "certification_number" not in safe(result)
    assert blocked(result)["certification_number"] == "VOLATILE_LISTING_FACT"


def test_C_uncollected_listing_keeps_its_static_product_facts(tmp_path):
    """A delisted product still has the panel it always had.

    This is the half of the gate that must *not* over-block: screen size and
    port counts are properties of the hardware, not of the offer.
    """

    service = service_for(tmp_path, collection_status="COLLECTION_FAILED")
    result = ask(service, "HDMI 몇 개이고 화면은 몇 인치인가요?")
    assert safe(result)["hdmi_port_count"] == 3
    assert safe(result)["screen_size"] == {"inch": 85}


def test_D_uncollected_listing_withholds_dynamic_facts(tmp_path):
    service = service_for(
        tmp_path, collection_status="COLLECTION_FAILED",
        stand_volatility="DYNAMIC_LISTING_FACT",
    )
    result = ask(service, "인증번호가 어떻게 되나요?")
    assert "certification_number" not in safe(result)


def test_D2_uncollected_listing_withholds_its_own_terms(tmp_path):
    """The gap the gate actually closes.

    Dynamic facts were already blocked outright by volatility, whatever the
    listing status. A SEMI_STATIC_POLICY_FACT was not: it is usable evidence
    today, and on a listing that no longer collects it describes an offer
    nobody has confirmed still stands.
    """

    service = service_for(
        tmp_path, collection_status="COLLECTION_FAILED",
        stand_volatility="SEMI_STATIC_POLICY_FACT",
    )
    result = ask(service, "인증번호가 어떻게 되나요?")
    assert "certification_number" not in safe(result)
    assert (
        blocked(result)["certification_number"]
        == "COLLECTION_STATUS_NOT_CURRENT"
    )


def test_D3_collected_listing_still_answers_its_own_terms(tmp_path):
    """No regression: the same field stays usable while the listing collects."""

    service = service_for(
        tmp_path, collection_status="COLLECTION_SUCCESS",
        stand_volatility="SEMI_STATIC_POLICY_FACT",
    )
    result = ask(service, "인증번호가 어떻게 되나요?")
    assert safe(result)["certification_number"] == "KC-12345"


@pytest.mark.parametrize(
    "status", [None, "", "UNKNOWN", "COLLECTION_PENDING", "success", "  "],
)
def test_EF_unrecognised_status_fails_closed(tmp_path, status):
    """An unreadable status is not evidence that the listing is live."""

    service = service_for(
        tmp_path, collection_status=status,
        stand_volatility="SEMI_STATIC_POLICY_FACT",
    )
    assert "certification_number" not in safe(ask(service, "인증번호 알려주세요"))
    # ...while the hardware facts are still answerable.
    assert safe(ask(service, "HDMI 몇 개인가요?"))["hdmi_port_count"] == 3


def test_G_withheld_facts_never_reach_the_prompt_or_evidence(tmp_path):
    service = service_for(
        tmp_path, collection_status="COLLECTION_FAILED",
        stand_volatility="SEMI_STATIC_POLICY_FACT",
    )
    result = ask(service, "인증번호가 어떻게 되나요?")
    assert result.prompt_block() == ""
    assert result.evidence_text() == ""
    assert "KC-12345" not in result.prompt_block()


def test_H_withheld_facts_do_not_make_an_answer_look_supported(tmp_path):
    """A blocked fact must not count towards auto-post eligibility."""

    service = service_for(
        tmp_path, collection_status="COLLECTION_FAILED",
        stand_volatility="SEMI_STATIC_POLICY_FACT",
    )
    result = ask(service, "인증번호가 어떻게 되나요?")
    assert result.has_safe_facts is False
    assert result.supports_question("인증번호가 어떻게 되나요?") is False
    assert result.covers_all(["certification_number"]) is False


# ================================================ R2 component subject gate ==
@pytest.mark.parametrize("question", [
    "셋톱박스도 삼성 제품인가요?",
    "셋톱박스 제조사도 삼성인가요?",
    "스탠드 제조사가 삼성인가요?",
    "구성품 원산지가 어디인가요?",
])
def test_component_question_never_borrows_the_listing_identity(
    tmp_path, question
):
    service = service_for(tmp_path, collection_status="COLLECTION_SUCCESS")
    result = ask(service, question)
    assert result.component_subject is True
    for field in ("brand", "manufacturer", "country_of_origin"):
        assert field not in safe(result)
    assert "삼성전자" not in result.evidence_text()


@pytest.mark.parametrize("question", [
    "이 셋톱박스 제조사는 어디인가요?",
    "이 스탠드 브랜드가 어디인가요?",
    "본 상품 브랜드가 뭔가요?",
])
def test_a_listing_that_is_the_component_still_answers_its_own_identity(
    tmp_path, question
):
    """The false positive this gate must not create.

    A stand-only or set-top-only listing is asked about its own brand, and the
    customer says "이 스탠드". That is the listing, not a bundled part.
    """

    service = service_for(tmp_path, collection_status="COLLECTION_SUCCESS")
    result = ask(service, question)
    assert result.component_subject is False
    assert safe(result), question


def test_plain_identity_question_is_unaffected(tmp_path):
    service = service_for(tmp_path, collection_status="COLLECTION_SUCCESS")
    assert safe(ask(service, "브랜드가 뭔가요?"))["brand"] == "삼성"
    assert safe(ask(service, "제조사가 어디인가요?"))["manufacturer"] == "삼성전자"


def test_component_gate_only_touches_identity_fields(tmp_path):
    """Asking about the stand still gets the stand's own specification."""

    service = service_for(tmp_path, collection_status="COLLECTION_SUCCESS")
    result = ask(service, "스탠드 종류가 어떻게 되나요?")
    assert safe(result)["stand_type"] == "BASIC FEET"


def test_bundled_component_detector_reads_the_subject_not_the_word():
    assert asks_about_a_bundled_component("셋톱박스도 삼성인가요?") is True
    assert asks_about_a_bundled_component("이 셋톱박스 제조사는?") is False
    assert asks_about_a_bundled_component("HDMI 몇 개인가요?") is False
    assert asks_about_a_bundled_component("") is False


# ============================================== R4 brand / manufacturer ======
def test_the_three_identity_questions_map_to_three_different_fields():
    assert fields_for_question("브랜드가 뭐예요?")[0] == ("brand",)
    assert fields_for_question("제조사가 어디예요?")[0] == ("manufacturer",)
    assert fields_for_question("원산지가 어디예요?")[0] == ("country_of_origin",)


def test_is_it_samsung_is_answered_by_the_maker_not_the_brand(tmp_path):
    """brand holds product lines as often as makers, so it cannot settle this.

    The listing below is branded 오디세이 and made by 삼성전자. Answering "삼성
    제품인가요?" from brand would read the product line as the maker.
    """

    service = service_for(
        tmp_path, collection_status="COLLECTION_SUCCESS", brand='"오디세이"',
    )
    result = ask(service, "삼성 제품인가요?")
    assert result.requested_fields == ("manufacturer",)
    assert safe(result)["manufacturer"] == "삼성전자"
    assert "brand" not in safe(result)


def test_product_line_question_is_grounded_only_by_a_value_that_says_so(
    tmp_path
):
    service = service_for(
        tmp_path, collection_status="COLLECTION_SUCCESS", brand='"오디세이"',
    )
    result = ask(service, "오디세이 제품인가요?")
    assert safe(result)["brand"] == "오디세이"


def test_product_line_absent_from_every_value_stays_unknown(tmp_path):
    """R4 + the standing contract: absence is unknown, never a denial."""

    service = service_for(
        tmp_path, collection_status="COLLECTION_SUCCESS", brand='"삼성"',
    )
    result = ask(service, "오디세이 제품인가요?")
    assert "brand" not in safe(result)
    assert "model_name" not in safe(result)
    assert blocked(result)["brand"] == "PRODUCT_LINE_NOT_IN_VALUE"
    # Nothing in what reaches the model suggests the answer is "no".
    for text in (result.evidence_text(), result.prompt_block()):
        assert "아닙니다" not in text
        assert "오디세이" not in text


def test_a_withheld_identity_is_not_a_negative_claim(tmp_path):
    service = service_for(tmp_path, collection_status="COLLECTION_SUCCESS")
    result = ask(service, "셋톱박스도 삼성 제품인가요?")
    assert result.prompt_block() == ""
    assert result.evidence_text() == ""


# ================================================= real shipped database =====
@real_db
def test_real_db_package_listing_does_not_lend_its_maker_to_the_set_top_box():
    """The concrete case this gate exists for.

    Listing 11848813000 is "삼성 85인치 4K UHD 스마트 비즈니스TV+OTT 구글TV
    셋탑박스", brand 삼성 / manufacturer 삼성전자. The set-top box sold on its
    own (11779070305) is SHAKS, made by 이노피아테크. The package's identity is
    therefore the wrong answer to a question about the box.
    """

    service = ProductKnowledgeService(ProductFactRepository(REAL_DB))
    package = service.facts_for_inquiry(
        product_id="11848813000", question="셋톱박스도 삼성 제품인가요?",
    )
    assert package.component_subject is True
    assert "manufacturer" not in package.safe_field_keys()
    assert "삼성전자" not in package.evidence_text()

    box = service.facts_for_inquiry(
        product_id="11779070305", question="이 셋톱박스 제조사는 어디인가요?",
    )
    assert {
        item.field_key: item.value for item in box.safe_facts
    }["manufacturer"] == "이노피아테크"


@real_db
def test_real_db_every_requestable_listing_scoped_field_is_gated():
    """A listing-scoped field a question can reach must be gated, not merely
    present.

    This replaces an assertion that FIELD_TOPICS requested only
    STATIC_PRODUCT_FACT fields. That was true of the map as first written, and
    it was recorded with a note saying the collection gate "becomes
    load-bearing the moment someone adds a delivery, price or policy topic --
    and this test will then start reporting which one". It duly reported
    ``installation_method``, added so that "설치는 어떻게 하나요?" can be
    answered.

    So the check moves onto the contract the gate actually owes: for every
    requestable field that describes the listing rather than the hardware,
    a listing that no longer collects must refuse it. That is stricter than
    the old assertion, which only described what the map happened to contain.
    """

    from services.product_knowledge_service import FIELD_TOPICS

    requested = set()
    for _keywords, base_fields, accessory_fields in FIELD_TOPICS:
        requested.update(base_fields)
        requested.update(accessory_fields)

    repository = ProductFactRepository(REAL_DB)
    service = ProductKnowledgeService(repository)
    with repository.connection() as connection:
        volatility = {
            row[0]: row[1] for row in connection.execute(
                "SELECT DISTINCT field, volatility FROM canonical_facts "
                "WHERE lifecycle_status = 'ACTIVE'"
            )
        }
        uncollected = [
            row["product_id"] for row in connection.execute(
                "SELECT product_id FROM listings "
                "WHERE collection_status IS NULL "
                "   OR collection_status <> 'COLLECTION_SUCCESS'"
            )
        ]

    listing_scoped = {
        field for field in requested
        if volatility.get(field) not in (None, "STATIC_PRODUCT_FACT")
    }

    for product_id in uncollected:
        listing = repository.listing_for_product(product_id)
        rows = [row for row in repository.facts_for_product(product_id)
                if row["field"] in listing_scoped]
        provenance = service._provenance_for(rows)
        for row in rows:
            fact = service._judge(
                row, provenance, expected_model=None,
                collection_status=listing["collection_status"],
            )
            assert not fact.safe_for_answer, (product_id, row["field"])
            assert fact.exclusion_reason in {
                "COLLECTION_STATUS_NOT_CURRENT", "VOLATILE_LISTING_FACT",
            }, (product_id, row["field"], fact.exclusion_reason)


@real_db
def test_real_db_uncollected_listing_refuses_the_installation_method_question():
    """The first customer question that reaches the collection gate.

    ``installation_method`` is SEMI_STATIC_POLICY_FACT: it describes how this
    listing is installed, not what the panel is. On the delisted listing it
    must be withheld even though it is VERIFIED, while that listing's static
    specifications keep answering.
    """

    repository = ProductFactRepository(REAL_DB)
    service = ProductKnowledgeService(repository)
    with repository.connection() as connection:
        uncollected = [
            row["product_id"] for row in connection.execute(
                "SELECT product_id FROM listings "
                "WHERE collection_status <> 'COLLECTION_SUCCESS'"
            )
        ]

    for product_id in uncollected:
        result = service.facts_for_inquiry(
            product_id=product_id, question="설치는 어떻게 하나요?")
        assert "installation_method" not in result.safe_field_keys()
        held = {item.field_key: item.exclusion_reason
                for item in result.excluded_facts}
        if "installation_method" in held:
            assert held["installation_method"] == "COLLECTION_STATUS_NOT_CURRENT"
        # the same listing still answers a hardware question
        hardware = service.facts_for_inquiry(
            product_id=product_id, question="화면 몇 인치예요?")
        assert hardware.safe_field_keys(), product_id


@real_db
def test_real_db_uncollected_listing_is_actually_gated():
    """Whatever the shipped DB holds, an uncollected listing must be gated.

    This replaces an assertion that every shipped listing was
    COLLECTION_SUCCESS. That was true of the artifact in hand when the gate was
    written, and it was recorded so a newer artifact carrying a delisted
    listing would say so out loud -- which is exactly what happened. "All
    listings collect" was never the safety contract, though; the contract is
    that a listing which no longer collects stops lending its *offer* to an
    answer while keeping its *hardware* facts. So the check moves onto the
    contract itself, and gets stricter rather than looser: it now walks every
    uncollected listing and demands the gate actually fired on it.
    """

    repository = ProductFactRepository(REAL_DB)
    service = ProductKnowledgeService(repository)
    with repository.connection() as connection:
        uncollected = [
            row["product_id"] for row in connection.execute(
                "SELECT product_id FROM listings "
                "WHERE collection_status IS NULL "
                "   OR collection_status <> 'COLLECTION_SUCCESS'"
            )
        ]

    for product_id in uncollected:
        listing = repository.listing_for_product(product_id)
        rows = repository.facts_for_product(product_id)
        provenance = service._provenance_for(rows)
        judged = [
            service._judge(
                row, provenance, expected_model=None,
                collection_status=listing["collection_status"],
            )
            for row in rows
        ]
        usable = [fact for fact in judged if fact.safe_for_answer]

        # Nothing that describes the listing's own offer may survive.
        for fact in usable:
            assert fact.volatility == "STATIC_PRODUCT_FACT", (
                product_id, fact.field_key, fact.volatility,
            )

        # ...and the listing-scoped facts must be held for that exact reason,
        # not incidentally by some other condition.
        held = {
            fact.field_key: fact.exclusion_reason for fact in judged
            if fact.volatility == "SEMI_STATIC_POLICY_FACT"
            and fact.verification_status == "VERIFIED"
        }
        assert held, product_id
        assert set(held.values()) == {"COLLECTION_STATUS_NOT_CURRENT"}, held
