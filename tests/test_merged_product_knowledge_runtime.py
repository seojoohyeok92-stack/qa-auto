"""The merged v8.2 Product Knowledge, read the way runtime reads it.

These pin the two things the runtime verification actually caught, plus the
identity boundaries the merge had to respect.

The first is a question that the record could answer and the lookup could not.
"설치비가 추가로 드나요?" is answered by ``additional_cost`` ("무료설치배송"),
but no keyword routed a question to that field, so it was never asked for --
and once the merged record made the candidate list long enough for the prompt
budget to start cutting, the one field that answers the question was cut.

The second is the same shape: 운영체제 routed only to ``operating_system``
while a third of the stored readings use ``os``.

The third is about keeping two questions apart. Who installs is not what
installation costs, and ``additional_cost`` is evidence for the second only.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)
from services.product_knowledge_service import (
    ProductKnowledgeService,
    select_prompt_facts,
)


def _selected(model: str, question: str):
    repository = ProductCatalogRepository()
    result = ProductKnowledgeService(repository).facts_for_inquiry(
        product_id="", product_name=model, model_code=model,
        question=question, include_all_catalog_fields=True,
    )
    kept, _, selection = select_prompt_facts(
        result.safe_facts,
        requested_fields=result.requested_fields,
        topics=result.topics,
        question=question,
    )
    return result, kept, selection


# --- the two questions the record could answer and the lookup could not ---------

def test_an_installation_cost_question_reaches_the_field_that_answers_it():
    result, kept, _ = _selected("LH50BEDHLGFXKR", "설치비가 추가로 드나요?")

    assert "additional_cost" in result.requested_fields
    assert [fact for fact in kept if fact.field_key == "additional_cost"]


def test_an_operating_system_question_reaches_both_spellings():
    """The record writes this as ``operating_system`` and as ``os``."""

    result, kept, _ = _selected(
        "LH55BEHHLGFXKR", "스마트 기능과 운영체제가 무엇인가요?")

    assert {"operating_system", "os"} <= set(result.requested_fields)
    found = {fact.field_key for fact in kept} & {"operating_system", "os"}
    assert found, "운영체제 질문인데 운영체제 사실이 하나도 선택되지 않았다"


def test_an_installer_question_asks_about_installation_not_price():
    """Who installs and what it costs are two questions.

    "설치는 누가 하나요?" reached no installation field at all, which is worth
    fixing. What it must not do is pull ``additional_cost``: a delivery term
    ("무료설치배송") is not evidence for who does the work. Who installs is
    answered by the store's own confirmed statement in ``answer/engine.py``,
    which GPT (2) receives as a candidate.
    """

    result, kept, selection = _selected(
        "LH50BEFHLGFXKR", "블루투스와 와이파이 되나요? 설치는 누가 하나요?")

    assert "설치는 누가" in result.topics
    assert "installation_method" in result.requested_fields
    assert "additional_cost" not in result.requested_fields
    assert not [fact for fact in kept if fact.field_key == "additional_cost"]
    assert not selection["asked_fields_dropped"], selection["asked_fields_dropped"]


def test_an_installation_inclusive_question_asks_about_both():
    """"설치 포함인가요?" is the one phrasing that needs the price too."""

    result, _, _ = _selected("LH50BEDHLGFXKR", "설치 포함인가요?")

    assert "installation_method" in result.requested_fields
    assert "additional_cost" in result.requested_fields


# --- the identity boundaries the merged record has to keep ----------------------

@pytest.mark.parametrize(
    "model, forbidden",
    [
        ("LH43BEHHLGFXKR", ("LH43BEDHLGFXKR", "LH43BEDH", "LH43BEFHLGFXKR")),
        ("LH43BEDHLGFXKR", ("LH43BEHHLGFXKR",)),
        ("LS22D400GAKXKR", ("LS24D400GAKXKR", "LS27D400GAKXKR")),
        ("LS32DM500EKXKR", ("LS32DM501EKXKR",)),
        ("LS32DM501EKXKR", ("LS32DM500EKXKR",)),
    ],
)
def test_retrieved_evidence_never_crosses_into_a_neighbouring_model(model, forbidden):
    repository = ProductCatalogRepository()
    aliases = repository.catalog()["aliases"]
    _, kept, _ = _selected(model, "크기와 무게, 응답속도 알려주세요")

    target = canonical_model_identity(model, aliases=aliases)
    for fact in kept:
        if not fact.model_code:
            continue
        assert canonical_model_identity(fact.model_code, aliases=aliases) == target, (
            fact.field_key, fact.model_code
        )
    seen = {str(fact.model_code) for fact in kept}
    assert not (seen & set(forbidden)), seen & set(forbidden)


# --- a fact the exact model does not have is not borrowed -----------------------

@pytest.mark.parametrize(
    "model, question, absent",
    [
        ("LH43BEHHLGFXKR", "VESA 벽걸이 규격이 어떻게 되나요?", ("vesa", "vesa_mm")),
        ("LH50BEHHLGFXKR", "주사율이 몇 Hz인가요?", ("refresh_rate", "hz")),
        ("LH85BEHHLGFXKR", "화면 응답속도가 몇 ms인가요?", ("response_time",)),
    ],
)
def test_a_missing_fact_is_not_filled_in_from_a_neighbour(model, question, absent):
    """The BE-H sheets state no VESA size, refresh rate or response time.

    BE-D and BE-F do. Answering from those would be this product's
    specification replaced by another model's.
    """

    _, kept, _ = _selected(model, question)

    assert not [fact for fact in kept if fact.field_key in absent], [
        (fact.field_key, fact.value, fact.model_code)
        for fact in kept if fact.field_key in absent
    ]


# --- the merged rows are what they claim to be ----------------------------------

def test_every_merged_row_carries_its_source_image():
    knowledge = ProductCatalogRepository().product_knowledge()
    merged = [row for row in knowledge["model_facts"]
              if str(row.get("notes", "")).startswith("v8.2 ")]

    assert merged, "v8.2 rows are not in the record"
    for row in merged:
        assert row["scope"] in {"EXACT_MODEL", "ACCESSORY"}, row["field"]
        assert row["scope_status"] == "RESOLVED"
        assert row["operational_status"] == "CANDIDATE_NOT_APPROVED"
        assert isinstance(row["value"], str) and row["value"].strip()
        assert row["provenance"], row["field"]
        for entry in row["provenance"]:
            assert entry["source_type"] == "IMAGE_VISION"
            assert entry["source_image_hash"]
