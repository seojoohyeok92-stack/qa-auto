from __future__ import annotations

import json
from pathlib import Path

import pytest


FIXTURE_PATH = (
    Path(__file__).with_name("fixtures")
    / "gpt_governance_characterization.json"
)
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_governance_fixture_has_required_categories() -> None:
    categories = {item["category"] for item in FIXTURES}
    assert {
        "일반 상품 문의",
        "배송 문의",
        "설치 문의",
        "복합 문의",
        "불만 문의",
        "감사 문의",
        "짧은 질문",
        "오타 포함 질문",
        "개인정보 포함 질문",
        "사실 부족 질문",
        "DPS 조회 실패 질문",
        "환불·반품 고위험 문의",
    } <= categories


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item["id"])
def test_each_governance_fixture_has_complete_contract(fixture: dict) -> None:
    assert fixture["question"]
    assert isinstance(fixture["facts"], list)
    assert isinstance(fixture["allowed"], list)
    assert isinstance(fixture["forbidden"], list)
    assert isinstance(fixture["required_used_facts"], list)
    assert isinstance(fixture["requires_review"], bool)
    assert isinstance(fixture["validator_passed"], bool)
    assert not set(fixture["required_used_facts"]) - set(fixture["facts"])


def test_fixture_contains_no_real_customer_identifiers() -> None:
    text = FIXTURE_PATH.read_text(encoding="utf-8")
    assert "@example.com" not in text
    assert "20260729" not in text
    assert "홍길동" not in text
