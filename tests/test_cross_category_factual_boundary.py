"""A different kind of product answering the same property is not evidence.

A monitor's "RF 단자가 없어 지상파를 수신할 수 없습니다" is a true sentence about a
monitor and a false one about a business television, which has an RF input and a
tuner. Nothing else catches it: the sentence carries no measurement, so the
stated-value rule never sees it, and it names no model, so the compatibility
service reports only PRODUCT_CATEGORY_MISMATCH -- which stays soft on purpose,
because hardening category alone was measured and removed 400 candidates from a
single inquiry.

Measured over 150 snapshot inquiries, that left a television's port question
holding seven monitor answers denying a port it has, and its speaker question
five denying a speaker it has.

So three things have to be true together: the customer asked for a property,
the row answers that same property, and the two products are of categories that
are both known and different. Everything narrower than that still travels -- an
undetermined category, a row found compatible as policy, the same category with
an unclear model -- which is what keeps this from becoming a category gate.
"""

from __future__ import annotations

import json

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.learning_compatibility_service import (
    LearningCompatibilityService, extract_product_identity,
)
from services.product_knowledge_service import attribute_families
from services.similar_answer_service import SimilarAnswerService

TV = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
OTHER_TV = "삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEFHLGFXKR 스탠드형"
MONITOR = "삼성 에센셜 모니터 68.6cm(27인치) LS27D400 IPS 100Hz 피벗 세로"

REASON = "FACTUAL_IDENTITY_MISMATCH:CROSS_CATEGORY_PRODUCT_MISMATCH"
PORT_QUESTION = "이 제품 RF 단자가 있나요? 안테나 연결되나요?"
PORT_QUERY = "제품의 RF 단자 및 안테나 수신 지원 여부에 대한 안내"


def _row(key, listing, answer, *, question, scope="MODEL", product_id="99999999"):
    return {
        "source_key": key, "learning_source": "APPROVED_UNEDITED",
        "question_original_masked": question,
        "question_normalized": f"상품 문의 {question}",
        "store_code": "OJE_PLUS", "inquiry_type": "PRODUCT_INQUIRY",
        "intent": "상품", "final_answer": answer, "rating": 5, "active": 1,
        "validity_active": 1, "validity_type": "PERMANENT",
        "product_name": listing, "model_code": "",
        "metadata_json": json.dumps(
            {"human_verified": True, "product_scope": scope,
             "learning_signal_type": "POSITIVE",
             "source_product_id": product_id}, ensure_ascii=False),
    }


@pytest.fixture
def store(tmp_path):
    database = Database(tmp_path / "learning.db")
    database.initialize()
    rows = [
        _row("monitor-no-rf", MONITOR,
             "문의하신 스마트모니터는 RF 단자가 없어 일반 TV처럼 지상파 방송을"
             " 직접 수신해 시청할 수 없습니다.",
             question="지상파 방송 바로 나오나요?"),
        _row("tv-has-rf", OTHER_TV,
             "문의주신 상품은 RF단자를 통하여 TV시청이 가능하며 셋톱박스를"
             " HDMI에 연결하실 수도 있습니다.",
             question="안테나 연결해서 볼 수 있나요?", product_id="88888888"),
    ]
    with database.transaction() as connection:
        columns = {c[1] for c in connection.execute(
            "PRAGMA table_info(learning_examples)")}
        for row in rows:
            usable = {k: v for k, v in row.items() if k in columns}
            connection.execute(
                f"INSERT INTO learning_examples ({','.join(usable)})"
                f" VALUES ({','.join('?' for _ in usable)})",
                list(usable.values()))
    return database


def _search(store, listing, *, enforced=True,
            question=PORT_QUESTION, query=PORT_QUERY, goal="PRODUCT_SPEC"):
    service = SimilarAnswerService(LearningRepository(store))
    selected = service.search(
        question, store_code="OJE_PLUS", product_name=listing,
        inquiry_type="PRODUCT_INQUIRY", limit=8, hard_conflicts_only=True,
        product_fact_sensitive=enforced, identity_enforced=enforced,
        semantic_goal={
            "customer_goal": goal, "requested_information": query,
            "atomic_question": question, "all_atomic_questions": [],
            "retrieval_queries": [query],
            "order_evidence_required": False, "schedule_scoped": False,
        },
    )
    return selected, dict(service.last_trace or {})


def _answers(selected):
    return " ".join(str(item["final_answer"]) for item in selected)


# --- the case this exists for -------------------------------------------

def test_a_monitors_port_fact_is_not_this_televisions(store):
    selected, trace = _search(store, TV)

    assert "RF 단자가 없어" not in _answers(selected)
    assert (trace.get("rejection_counts") or {}).get(REASON), trace.get(
        "rejection_counts")


def test_a_same_category_row_is_not_this_rules_business(store):
    """Same category, unclear model: a different rule decides, not this one.

    Under a specification question the existing Gate 1 already asks for
    positive identity and reports INSUFFICIENT_PRODUCT_IDENTITY, which is
    unchanged. What matters here is that the cross-category reason is not the
    one attached to it -- otherwise this rule would be a category gate wearing
    a different name.
    """

    _selected, trace = _search(store, TV)
    reasons = trace.get("rejection_counts") or {}

    identity = [key for key in reasons
                if key.endswith("INSUFFICIENT_PRODUCT_IDENTITY")
                or key.endswith("PRODUCT_VARIANT_MISMATCH")
                or key.endswith("MODEL_MISMATCH")]
    assert identity, reasons
    # The television row was removed by identity, the monitor row by category.
    assert reasons.get(REASON) == 1, reasons


# --- everything narrower than "both known and different" survives --------

def test_an_operational_question_is_never_cross_category_gated(store):
    """The rule keys on PRODUCT_FACT_ACTIONS, like every identity rule here."""

    selected, trace = _search(
        store, TV, enforced=False, goal="INSTALLATION_METHOD",
        question="설치는 어떻게 하나요?", query="설치 방식에 대한 안내")

    assert not (trace.get("rejection_counts") or {}).get(REASON)
    assert selected


def test_an_unshared_attribute_is_left_to_the_attribute_gate(store):
    """No shared family means this rule has nothing to say about the row."""

    query = "제품의 원산지에 대한 안내"
    assert not (attribute_families(query)
                & attribute_families("RF 단자가 없어 지상파 수신 불가"))


# --- the verdict the rule requires, stated against the real service ------

def test_a_monitor_against_a_television_reports_category_mismatch():
    decision = LearningCompatibilityService().evaluate(
        current_question=PORT_QUESTION,
        current_product=extract_product_identity(product_name=TV),
        candidate_question="지상파 나오나요?",
        candidate_answer="RF 단자가 없어 지상파 수신이 불가합니다.",
        candidate_product=extract_product_identity(product_name=MONITOR),
        candidate_metadata={"product_scope": "MODEL"},
        query_is_product_fact=True,
    )

    assert decision.reject_reason == "PRODUCT_CATEGORY_MISMATCH"
    assert decision.product_match == "MISMATCH"
    assert decision.eligible is False
    assert decision.current_product.category != decision.candidate_product.category
    assert decision.current_product.category
    assert decision.candidate_product.category


def test_two_televisions_are_the_same_category():
    """So the rule cannot fire between them, whatever else differs."""

    current = extract_product_identity(product_name=TV)
    candidate = extract_product_identity(product_name=OTHER_TV)

    assert current.category == candidate.category == "TV"


def test_an_unnamed_product_has_no_category_to_differ_from():
    """Undetermined is not different; the rule needs both sides known."""

    assert not extract_product_identity(product_name="").category
