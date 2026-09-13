"""Targeted integration coverage for the single-file Product Knowledge source."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from services.hybrid_answer_service import HybridAnswerService
from services.product_knowledge_service import ProductKnowledgeService
from repositories.product_catalog_repository import ProductCatalogRepository


ROOT = Path(__file__).resolve().parents[1]
FINAL = ROOT / "data" / "model_data_with_color.json"


def _repo() -> ProductCatalogRepository:
    return ProductCatalogRepository(FINAL)


def _service() -> ProductKnowledgeService:
    return ProductKnowledgeService(_repo())


def _candidate(section: str) -> dict:
    data = json.loads(FINAL.read_text(encoding="utf-8"))["PRODUCT_KNOWLEDGE"]
    for row in data[section]:
        if (
            row.get("operational_status") in {
                "CANDIDATE_NOT_APPROVED", "CANDIDATE_REVIEW_RESOLVED",
            }
            and row.get("scope_status") == "RESOLVED"
            and row.get("value") not in (None, "")
            and (row.get("product_id") or row.get("applies_to_product_id") or row.get("source_product_ids"))
        ):
            return row
    raise AssertionError(f"no runtime candidate in {section}")


def _lookup(row: dict):
    product_id = (
        row.get("product_id") or row.get("applies_to_product_id")
        or row.get("source_product_ids", [None])[0]
    )
    return _service().facts_for_inquiry(
        product_id=product_id, model_code=row.get("model_code"),
        question="제품 사양을 알려주세요", include_all_catalog_fields=True,
    )


def test_case_1_existing_catalog_fact_is_preserved():
    result = _service().facts_for_inquiry(
        product_id="10198648691", model_code="LS32DM501EKXKR",
        question="주사율이 어떻게 되나요?", include_all_catalog_fields=True,
    )
    assert any(
        fact.field_key == "refresh_rate"
        and fact.verification_status == "CATALOG_JSON"
        for fact in result.safe_facts
    )


def test_case_2_integrated_evidence_reaches_actual_gpt_context_with_provenance():
    result = _service().facts_for_inquiry(
        product_id="10198648691", model_code="LS32DM501EKXKR",
        question="주사율이 어떻게 되나요?", include_all_catalog_fields=True,
    )
    context = HybridAnswerService._product_facts_context(
        SimpleNamespace(metadata={"product_knowledge": result})
    )
    facts = context["product_catalog"]["facts"]
    integrated = [item for item in facts if item.get("verification", "").startswith("CANDIDATE")]
    assert integrated
    assert {"subject", "model_code", "scope", "source_type"} <= set(integrated[0])


def test_case_3_same_model_evidence_can_be_reused_across_listing_ids():
    first = _service().facts_for_inquiry(
        product_id="10198648691", model_code="LS32DM501EKXKR",
        question="제품 사양", include_all_catalog_fields=True,
    )
    second = _service().facts_for_inquiry(
        product_id="11745748916", model_code="LS32DM501EKXKR",
        question="제품 사양", include_all_catalog_fields=True,
    )
    assert any(f.field_key == "refresh_rate" and f.subject == "MAIN_PRODUCT" for f in first.safe_facts)
    assert any(f.field_key == "refresh_rate" and f.subject == "MAIN_PRODUCT" for f in second.safe_facts)


def test_case_4_bundle_accessory_stays_labeled_as_bundle_accessory():
    row = _candidate("bundle_accessory_facts")
    result = _lookup(row)
    facts = [f for f in result.safe_facts if f.field_key == row["field"]]
    assert facts and all(f.component_scope == "BUNDLE_ACCESSORY" for f in facts)
    assert all(f.subject != "MAIN_PRODUCT" for f in facts)


def test_case_5_listing_fact_requires_its_exact_listing_id():
    row = _candidate("listing_facts")
    result = _lookup(row)
    assert any(f.field_key == row["field"] and f.applies_to_product_id for f in result.safe_facts)
    other = _service().facts_for_inquiry(
        product_id="__different_listing__", question="제품 사양",
        include_all_catalog_fields=True,
    )
    assert not any(f.field_key == row["field"] and f.subject == "LISTING" for f in other.safe_facts)


def test_case_6_policy_fact_requires_its_exact_policy_listing_scope():
    row = _candidate("policy_facts")
    result = _lookup(row)
    facts = [f for f in result.safe_facts if f.field_key == row["field"]]
    assert facts and all(f.component_scope == "POLICY" for f in facts)


def test_case_7_withheld_and_final_unresolved_never_become_prompt_facts():
    data = json.loads(FINAL.read_text(encoding="utf-8"))["PRODUCT_KNOWLEDGE"]
    withheld = next(r for r in data["model_facts"] if r.get("operational_status") == "WITHHELD_REVIEW_REQUIRED")
    result = _service().facts_for_inquiry(
        product_id=str(withheld["source_product_ids"][0]), model_code=withheld["model_code"],
        question="제품 사양", include_all_catalog_fields=True,
    )
    assert not any(f.verification_status == "WITHHELD_REVIEW_REQUIRED" for f in result.safe_facts)
    assert not any(f.field_key == withheld["field"] and f.model_code == withheld["model_code"] for f in result.safe_facts)


def test_case_8_product_knowledge_and_learning_context_can_coexist_without_code_selection():
    result = _service().facts_for_inquiry(
        product_id="10198648691", model_code="LS32DM501EKXKR",
        question="제품 사양", include_all_catalog_fields=True,
    )
    context = {"learning_examples": [{"id": "L-1", "answer": "learning evidence"}]}
    context.update(HybridAnswerService._product_facts_context(SimpleNamespace(metadata={"product_knowledge": result})))
    assert context["learning_examples"] and context["product_catalog"]["facts"]


def test_case_9_context_attachment_does_not_add_a_post_gpt_semantic_gate():
    result = _service().facts_for_inquiry(
        product_id="10198648691", model_code="LS32DM501EKXKR",
        question="제품 사양", include_all_catalog_fields=True,
    )
    context = HybridAnswerService._product_facts_context(SimpleNamespace(metadata={"product_knowledge": result}))
    assert "product_catalog" in context
    # Attachment contains evidence only; no decision/approval/selection flag is injected.
    assert not {"semantic_gate", "approval", "selected_fact"} & set(context["product_catalog"])


def test_case_10_catalog_only_product_remains_usable_when_no_integrated_record_matches():
    catalog = _repo().catalog()["catalog"]
    integrated_models = {
        str(r.get("model_code"))
        for r in _repo().product_knowledge().get("model_facts", ())
        if isinstance(r, dict)
    }
    model = next(key for key in catalog if key not in integrated_models)
    result = _service().facts_for_inquiry(
        product_id="catalog-only", model_code=model,
        question="제품 사양", include_all_catalog_fields=True,
    )
    assert result.matched and result.safe_facts
    assert all(f.verification_status == "CATALOG_JSON" for f in result.safe_facts)


def test_listing_with_exact_api_model_provenance_reaches_integrated_model_facts():
    """A listing-only catalog miss may still have one exact API model identity."""
    result = _service().facts_for_inquiry(
        product_id="11815213767",
        product_name="삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 이동식 거치대",
        question="해상도 HDMI USB Wi-Fi 블루투스 스피커 무게를 알려주세요",
        include_all_catalog_fields=True,
    )
    assert result.identity_status == "LISTING_EXACT_API_MODEL"
    assert any(
        fact.subject == "MAIN_PRODUCT" and fact.model_code == "LH50BEHHLGFXKR"
        for fact in result.safe_facts
    )
    shaks = [
        fact for fact in result.safe_facts
        if fact.subject == "BUNDLED_SET_TOP_BOX" and fact.model_code == "SHAKS G1"
    ]
    assert shaks and all(fact.component_scope == "BUNDLE_ACCESSORY" for fact in shaks)
