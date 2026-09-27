"""Promoting a withheld row changes its status and nothing else about it.

369 rows carried ``WITHHELD_REVIEW_REQUIRED``, which the runtime status gate
treats as unusable. Re-read on their evidence -- provenance, a settled target,
whether a sibling for the same target and field disagrees in *meaning* rather
than in spelling -- 159 turned out to be a single source-backed value, or a set
of variants saying one thing: "2024-04" beside "2024년 4월", "삼성전자" beside
"삼성전자㈜", one telephone number written three ways.

Those 159 are now ``CANDIDATE_REVIEW_RESOLVED``. What must stay true is that the
promotion moved a status and left the fact alone, that the rows still withheld
are still withheld, and that a promoted row cannot reach a target it does not
belong to.

One thing these also pin is a limit found while checking the result: eligibility
is not reachability. A separate runtime step drops rows that disagree for the
same target and field, comparing the stored strings, so 75 of the 159 are still
withheld from an answer by *that* gate rather than by their status. The
measurement is recorded here so the number cannot drift unnoticed.
"""

from __future__ import annotations

import pytest

from repositories.product_catalog_repository import (
    ProductCatalogRepository,
    canonical_model_identity,
)
from services.product_knowledge_service import (
    _RUNTIME_PRODUCT_KNOWLEDGE_STATUSES,
    ProductKnowledgeService,
)

PROMOTED_BY_FIELD = {
    "manufacturer": 55, "as_contact": 48, "release_ym": 46,
    "origin_country": 8, "warranty_policy": 2,
    # Six dimension rows joined later, once reading their sources showed the
    # two readings were one measurement printed to different precision.
    "dimensions_labelled": 6,
}
ALLOWED_REASONS = ("SIBLINGS_AGREE_IN_MEANING",
                   "SOLE_SOURCE_BACKED_VALUE_FOR_A_SETTLED_TARGET",
                   "ROUNDING_EQUIVALENT_HIGHER_PRECISION_REPRESENTS")


@pytest.fixture(scope="module")
def service():
    return ProductKnowledgeService(ProductCatalogRepository())


@pytest.fixture(scope="module")
def knowledge(service):
    return service.catalog_repository.product_knowledge()


@pytest.fixture(scope="module")
def promoted(knowledge):
    return [
        (index, row) for index, row in enumerate(knowledge["model_facts"])
        if isinstance(row, dict) and "P4_REVIEW" in str(row.get("notes") or "")
    ]


def test_exactly_the_audited_rows_were_promoted(promoted):
    assert len(promoted) == 165
    counts: dict[str, int] = {}
    for _index, row in promoted:
        counts[str(row["field"])] = counts.get(str(row["field"]), 0) + 1
    assert counts == PROMOTED_BY_FIELD


def test_a_promoted_row_is_runtime_eligible_and_records_why(promoted):
    for _index, row in promoted:
        assert row["operational_status"] == "CANDIDATE_REVIEW_RESOLVED"
        assert row["operational_status"] in _RUNTIME_PRODUCT_KNOWLEDGE_STATUSES
        assert any(reason in str(row["notes"]) for reason in ALLOWED_REASONS)


def test_promotion_did_not_touch_the_fact_itself(promoted):
    """Status and the note that explains it. Nothing else.

    A promotion that quietly normalised a value would be a data edit wearing a
    status change's clothes.
    """

    for _index, row in promoted:
        assert row["provenance"], row["field"]
        assert row["scope_status"] == "RESOLVED"
        assert row["scope"] not in {"UNKNOWN_SCOPE", "MULTI_MODEL"}
        assert row["value_state"] != "EXPLICIT_NA"
        assert str(row.get("value") or "").strip()
        # Each provenance entry still points at something readable -- the text
        # it was read from, or the image it was read out of. What the promotion
        # must not do is leave a row asserting a value with nothing behind it.
        for entry in row["provenance"]:
            assert isinstance(entry, dict)
            assert entry.get("source_type")
            assert (entry.get("source_text")
                    or entry.get("source_image_hash")
                    or entry.get("canonical_source_path")), row["field"]


def test_the_rest_stay_withheld(knowledge):
    """202 real conflicts and 2 unresolved scopes -- 204 in all, untouched.

    The 4 rounding variants and the 2 deferred dimension rows left this set once
    their sources showed them to be one measurement at two precisions.
    """

    withheld = [
        row for section in ("model_facts", "listing_facts",
                            "bundle_accessory_facts", "policy_facts")
        for row in knowledge[section]
        if isinstance(row, dict)
        and row.get("operational_status") == "WITHHELD_REVIEW_REQUIRED"
    ]
    assert len(withheld) == 204
    for row in withheld:
        assert "P4_REVIEW" not in str(row.get("notes") or "")


def test_a_withheld_row_never_reaches_an_answer(service, knowledge):
    """The status gate is what holds them, and it still holds."""

    models = []
    for row in knowledge["model_facts"]:
        if (isinstance(row, dict)
                and row.get("operational_status") == "WITHHELD_REVIEW_REQUIRED"
                and row.get("model_code")):
            models.append(str(row["model_code"]))
        if len(models) >= 8:
            break

    for model in models:
        result = service.facts_for_inquiry(
            product_id="", question="제조사랑 제조국이랑 크기 알려주세요",
            model_code=model, product_name=model,
            include_all_catalog_fields=True,
        )
        for fact in result.safe_facts:
            # The model catalogue is a separate source and reports its own
            # status (``CATALOG_JSON``). The status gate governs the integrated
            # Product Knowledge rows, which are the ones this is about.
            if not str(fact.canonical_fact_id or "").startswith("integrated:"):
                continue
            assert fact.verification_status in _RUNTIME_PRODUCT_KNOWLEDGE_STATUSES


def test_a_promoted_row_only_answers_for_its_own_target(service, promoted):
    """A status change must not widen what a fact applies to."""

    aliases = service.catalog_repository.catalog()["aliases"]
    checked = 0
    for _index, row in promoted:
        model = str(row.get("model_code") or "")
        if not model:
            continue
        match = service.catalog_repository.match(model_code=model)
        if not match.model_key:
            continue                      # model absent from the catalogue
        result = service.facts_for_inquiry(
            product_id="", question="제조사 알려주세요", model_code=model,
            product_name=model, include_all_catalog_fields=True,
        )
        expected = canonical_model_identity(match.model_key, aliases=aliases)
        for fact in result.safe_facts:
            if not fact.model_code or "model_facts" not in str(
                    fact.canonical_fact_id):
                continue
            identity = canonical_model_identity(fact.model_code, aliases=aliases)
            if identity and expected:
                assert identity == expected, (model, fact.field_key, identity)
        checked += 1
        if checked >= 6:
            break
    assert checked, "no promoted row had a catalogued model to check"


def test_eligibility_is_not_reachability(service, promoted):
    """All 165 are now eligible, and 162 of them arrive.

    Promotion made these rows eligible; two further gates decided whether they
    arrived. 75 were dropped by the runtime exact-conflict step comparing stored
    strings -- "삼성 서비스센터 : 1588-3366" against "삼성전자서비스센터 /
    1588-3366" -- and 31 belonged to six model codes MODEL_CATALOG did not hold,
    so no model-keyed lookup could find them. Field-aware equivalence and six
    catalogue entries closed both.

    The 3 that still do not arrive are the rounded copies of a dimension whose
    precise reading represents the group; they are corroboration, not absence.
    """

    reached = 0
    cache: dict[tuple[str, str], set[str]] = {}
    for index, row in promoted:
        model, field = str(row.get("model_code") or ""), str(row["field"])
        if not model:
            continue
        key = (model, field)
        if key not in cache:
            result = service.facts_for_inquiry(
                product_id="", question="알려주세요", model_code=model,
                product_name=model, include_all_catalog_fields=True,
            )
            cache[key] = {str(f.canonical_fact_id) for f in result.safe_facts}
        if f"integrated:model_facts:{index}" in cache[key]:
            reached += 1

    assert reached == 162, (
        "the promoted-but-unreachable split changed; re-measure before "
        "adjusting this number", reached)
