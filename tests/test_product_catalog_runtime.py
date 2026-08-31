from __future__ import annotations

import json
from pathlib import Path

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_knowledge_service import ProductKnowledgeService


def _service(tmp_path: Path) -> ProductKnowledgeService:
    path = tmp_path / "model_data_with_color.json"
    path.write_text(json.dumps({
        "MODEL_CATALOG": {
            "ABC55X": {
                "model": "ABC55X", "size_inch": 55,
                "spec": "HDMI 2개 / RF단자 1개 / 스탠드 간격 900mm",
                "vesa": "200x200mm", "weight": "12kg",
            },
        },
        "MODEL_ALIASES": {"ABC 55": "ABC55X"},
    }, ensure_ascii=False), encoding="utf-8")
    return ProductKnowledgeService(
        catalog_repository=ProductCatalogRepository(path)
    )


def test_catalog_exact_model_only_and_literal_evidence(tmp_path: Path) -> None:
    result = _service(tmp_path).facts_for_inquiry(
        product_id="listing", product_name="ABC55X 스마트 TV",
        question="동축케이블 연결 가능한가요? HDMI도 있나요?",
        model_code="ABC55X",
    )
    assert result.matched is True
    assert {fact.field_key for fact in result.safe_facts} >= {
        "rf_terminal", "hdmi_present",
    }
    assert "PRODUCT_CATALOG_JSON" in result.prompt_block()
    assert "없는 기능" not in result.prompt_block()


def test_catalog_ambiguous_or_missing_model_is_unknown(tmp_path: Path) -> None:
    result = _service(tmp_path).facts_for_inquiry(
        product_id="listing", product_name="55인치 TV", question="HDMI 있나요?"
    )
    assert result.matched is False
    assert result.safe_facts == ()
    assert result.unavailable_reason == "PRODUCT_CATALOG_MODEL_NOT_FOUND"


def test_catalog_never_uses_weight_for_explicit_stand_scope(tmp_path: Path) -> None:
    result = _service(tmp_path).facts_for_inquiry(
        product_id="listing", product_name="ABC55X", model_code="ABC55X",
        question="스탠드 포함 무게가 몇 kg인가요?",
    )
    assert "weight_catalog" not in result.safe_field_keys()
