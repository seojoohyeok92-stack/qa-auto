"""The prompt budget has to be a budget, and the trim has to drop the weakest.

Two defects, measured on the 26.9.27 server snapshot, both of which spent a
Product Knowledge listing's prompt on things nobody asked about and paid for it
with the answers that were asked for.

The first: ``select_prompt_facts`` reserved one fact per ``model_code`` and let
every reserved fact past the cap. A listing's bundled options each carry their
own model code -- a wall-mount bracket, four lengths of extension cord, two
soundbars, 폐가전수거요청 -- so on 12139453925 eleven ``supplement_product`` rows
were admitted after the budget was full and the block came out at 29,126
characters against 24,000. Across five replayed inquiries the overshoot ran
1,926-5,108 characters and every character of it was an accessory row.

The second: when the assembled prompt still had to shrink, the evidence list
lost its *last* entry. ``similar_approved_answers`` is not ranked best-first --
it is the per-atom interleave -- so position says which sub-question an entry
came from, not how well it answers. On 689033056 that discarded relevance ranks
2, 3, 4 and 5, one of them the store's own answer to the question asked at
0.5375, and kept rank 6 at 0.3149.
"""

from __future__ import annotations

import json

import pytest

from services.learning_context_service import (
    DRAFT_PROMPT_BUDGET_CHARS, _least_relevant, apply_prompt_budget,
)
from services.product_knowledge_service import (
    PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS, _PROMPT_BLOCK_OVERHEAD_CHARS,
    ProductKnowledgeService, select_prompt_facts,
)
from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_fact_guard import extract_model_code

# A listing whose record bundles many add-on options, which is the shape that
# made the reservation unbounded. Read from the catalogue, not fabricated: the
# point is that a real record stays inside the cap.
BUNDLED_LISTING = "12139453925"
BUNDLED_NAME = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
QUESTION = "벽걸이추가만하면 설치비랑 다 포함되는건가요? 삼성기사님이 설치해주시는건가요?"


@pytest.fixture(scope="module")
def knowledge():
    service = ProductKnowledgeService(ProductCatalogRepository())
    return service.facts_for_inquiry(
        product_id=BUNDLED_LISTING, questions=[QUESTION], question=QUESTION,
        model_code=extract_model_code(BUNDLED_NAME), product_name=BUNDLED_NAME,
        include_all_catalog_fields=True)


def _selected(knowledge):
    return select_prompt_facts(
        knowledge.safe_facts, requested_fields=knowledge.requested_fields,
        topics=knowledge.topics, question=QUESTION)


# --- the block stays inside the budget it is given -------------------------

def test_the_rendered_block_fits_the_budget(knowledge):
    """What the prompt carries, not what the selector intended to carry."""

    if not knowledge.safe_facts:
        pytest.skip("this listing has no verified record in the loaded catalogue")

    kept, prompt_facts, selection = _selected(knowledge)
    block = knowledge.prompt_block(
        kept, also_reported=selection.get("merged_fields"))
    rendered = len(json.dumps(prompt_facts, ensure_ascii=False)) + len(block)

    assert rendered <= PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS, (
        rendered, len(kept), selection.get("dropped_for_budget"))


def test_prompt_block_overhead_is_measured(knowledge):
    """The allowance is a measurement, so it must cover the real block.

    Both branches: the footer differs depending on whether the list is the
    whole record or a selection from it. What scales with the list is charged
    per fact in ``cost``, so this covers the fixed part only.
    """

    if not knowledge.safe_facts:
        pytest.skip("this listing has no verified record in the loaded catalogue")

    facts = list(knowledge.safe_facts)
    for chosen in (facts[:1], facts):
        block = knowledge.prompt_block(chosen)
        lines = sum(len(item.as_prompt_line(None)) for item in chosen)
        wrapper = len(json.dumps(
            {"product_id": knowledge.product_id,
             "identity_status": getattr(knowledge, "identity_status", None)},
            ensure_ascii=False))
        assert (len(block) - lines) + wrapper <= (
            _PROMPT_BLOCK_OVERHEAD_CHARS), len(chosen)


def test_a_bundled_accessory_never_bypasses_the_budget(knowledge):
    """It competes on relevance; it does not get a free slot.

    This is the whole overshoot: every fact admitted past the cap was one of
    these.
    """

    if not knowledge.safe_facts:
        pytest.skip("this listing has no verified record in the loaded catalogue")

    kept, prompt_facts, selection = _selected(knowledge)

    def cost(index, fact):
        return (len(json.dumps(prompt_facts[index], ensure_ascii=False,
                               default=str))
                + len(fact.as_prompt_line(
                    (selection.get("merged_fields") or {}).get(
                        fact.canonical_fact_id))))

    spent = _PROMPT_BLOCK_OVERHEAD_CHARS
    for index, fact in enumerate(kept):
        spent += cost(index, fact)
    assert spent <= PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS, spent


def test_the_question_still_gets_its_fields(knowledge):
    """Narrowing the reservation must not cost an asked-for field."""

    if not knowledge.safe_facts:
        pytest.skip("this listing has no verified record in the loaded catalogue")

    _kept, _facts, selection = _selected(knowledge)

    assert selection["asked_fields_dropped"] == []


def test_each_device_model_still_keeps_a_fact(knowledge):
    """The comparison guarantee is what the reservation is for.

    Only add-on options lost it; the models the answer may be about did not.
    """

    if not knowledge.safe_facts:
        pytest.skip("this listing has no verified record in the loaded catalogue")

    kept, _facts, _selection = _selected(knowledge)
    device_models = {
        str(item.model_code or "") for item in knowledge.safe_facts
        if item.component_scope != "BUNDLE_ACCESSORY"
    }
    carried = {str(item.model_code or "") for item in kept}

    assert device_models <= carried, sorted(device_models - carried)


# --- the trim drops the weakest entry, not the last one --------------------

def _entry(learning_id, relevance, padding=400):
    return {"learning_example_id": learning_id, "relevance": relevance,
            "answer": "가" * padding}


def test_the_trim_drops_the_least_relevant_entry():
    """689033056: rank 4 was cut and rank 6 survived, on position alone."""

    entries = [_entry(319038, 0.9867), _entry(19290, 0.3149),
               _entry(19553, 0.7674), _entry(155709, 0.2922),
               _entry(169371, 0.6604), _entry(1352, 0.1953),
               _entry(117, 0.5375), _entry(106383, 0.1900),
               _entry(33943, 0.5249)]
    context = {"similar_approved_answers": entries}

    trimmed, report = apply_prompt_budget(context, budget=2_600)

    kept = [item["learning_example_id"]
            for item in trimmed["similar_approved_answers"]]
    assert kept, report
    # Whatever survives is the strongest, and the store's own answer is in it.
    assert 117 in kept, kept
    assert min(item["relevance"]
               for item in trimmed["similar_approved_answers"]) >= 0.5249
    # Order is not re-sorted: the interleave the rest of the prompt reads is
    # preserved.
    assert kept == [lid for lid in
                    [319038, 19553, 169371, 117, 33943] if lid in kept]


def test_the_survivors_keep_their_arrival_order():
    entries = [_entry(1, 0.10), _entry(2, 0.90), _entry(3, 0.50)]

    trimmed, _report = apply_prompt_budget(
        {"similar_approved_answers": entries}, budget=1_400)

    kept = [item["learning_example_id"]
            for item in trimmed["similar_approved_answers"]]
    assert kept == sorted(kept), kept


def test_an_entry_without_relevance_goes_first():
    """A style reference says nothing about answering this question."""

    entries = [_entry(1, 0.90), {"learning_example_id": 2, "answer": "가" * 400},
               _entry(3, 0.80)]

    assert _least_relevant(entries) == 1


def test_equal_relevance_breaks_toward_the_earlier_entry():
    entries = [_entry(1, 0.40), _entry(2, 0.40)]

    assert _least_relevant(entries) == 1


def test_shrinking_to_empty_is_still_possible():
    """The floor is unchanged; only the order of removal moved."""

    entries = [_entry(1, 0.90, padding=4_000)]

    trimmed, report = apply_prompt_budget(
        {"similar_approved_answers": entries}, budget=200)

    assert trimmed["similar_approved_answers"] == []
    assert report["dropped"][0]["kept_records"] == 0


def test_the_draft_budget_is_unchanged():
    assert DRAFT_PROMPT_BUDGET_CHARS == 60_000
    assert PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS == 24_000


# --- what the trace says was attached, and what actually was ---------------

def test_attached_to_prompt_reflects_the_assembled_prompt():
    """The flag is written before the budget runs, so it has to be redone.

    ``apply_prompt_budget`` deletes whole entries after the context -- and its
    trace -- are already written. Without reconciliation a row the budget
    dropped went on reporting itself as attached, and an operator reading the
    trace saw evidence in a prompt that never carried it.
    """

    from services.draft_generation_service import _reconcile_prompt_attachment

    learning_context = {
        "similar_approved_answers": [
            {"learning_example_id": 1}, {"learning_example_id": 2},
            {"learning_example_id": 3},
        ],
        "learning_retrieval": {
            "selected": [
                {"learning_id": 1, "attached_to_prompt": True},
                {"learning_id": 2, "attached_to_prompt": True},
                {"learning_id": 3, "attached_to_prompt": True},
            ],
        },
    }
    # The budget kept two of the three.
    prompt_input = {
        "similar_approved_answers": [
            {"learning_example_id": 1}, {"learning_example_id": 3},
        ],
        "seller_style_examples": [],
    }

    _reconcile_prompt_attachment(learning_context, prompt_input)

    state = {int(item["learning_id"]): item["attached_to_prompt"]
             for item in learning_context["learning_retrieval"]["selected"]}
    assert state == {1: True, 2: False, 3: True}
    assert learning_context["learning_retrieval"]["attached_learning_ids"] == [1, 3]
    # The context now describes the evidence that was actually sent.
    assert [item["learning_example_id"]
            for item in learning_context["similar_approved_answers"]] == [1, 3]


def test_a_style_reference_counts_as_attached():
    """Both prompt channels carry Learning, so both settle the flag."""

    from services.draft_generation_service import _reconcile_prompt_attachment

    learning_context = {
        "learning_retrieval": {
            "selected": [{"learning_id": 7, "attached_to_prompt": False}],
        },
    }
    _reconcile_prompt_attachment(
        learning_context,
        {"similar_approved_answers": [],
         "seller_style_examples": [{"learning_example_id": 7}]},
    )

    assert learning_context["learning_retrieval"]["selected"][0][
        "attached_to_prompt"] is True


def test_reconciliation_is_safe_without_a_trace():
    """It filters what the context has; it never invents what it has not.

    Copying the prompt's own projection back would do the opposite -- it would
    add keys the context never held, and those projections are stripped of the
    provenance the recovery and validation passes read.
    """

    from services.draft_generation_service import _reconcile_prompt_attachment

    context = {}
    _reconcile_prompt_attachment(context, {"similar_approved_answers": []})

    assert context == {}


def test_reconciliation_keeps_context_only_provenance():
    """The prompt drops ``compatibility``; the context must not.

    ``prompt_context`` strips it deliberately, and
    ``_apply_learning_grounded_recovery`` and ``_validate_historical_usage``
    read it after this call. Filtering by id keeps the full row; copying the
    projection back would have deleted it.
    """

    from services.draft_generation_service import _reconcile_prompt_attachment

    context = {
        "similar_approved_answers": [
            {"learning_example_id": 1,
             "compatibility": {"product_match": "EXACT_MODEL"},
             "evidence_origin": {"scope": "MODEL"}},
            {"learning_example_id": 2, "compatibility": {"product_match": "MISMATCH"}},
        ],
        "historical_cases": [
            {"historical_case_id": "H1", "answer_style_reference": "…",
             "compatibility": {"ok": True}},
            {"historical_case_id": "H2", "compatibility": {"ok": False}},
        ],
    }
    # The prompt copy carries neither field, and holds only what survived.
    _reconcile_prompt_attachment(context, {
        "similar_approved_answers": [{"learning_example_id": 1}],
        "seller_style_examples": [],
        "historical_cases": [{"historical_case_id": "H1"}],
    })

    kept = context["similar_approved_answers"]
    assert [row["learning_example_id"] for row in kept] == [1]
    assert kept[0]["compatibility"] == {"product_match": "EXACT_MODEL"}
    assert kept[0]["evidence_origin"] == {"scope": "MODEL"}

    cases = context["historical_cases"]
    assert [row["historical_case_id"] for row in cases] == ["H1"]
    assert cases[0]["answer_style_reference"] == "…"
    assert cases[0]["compatibility"] == {"ok": True}


def test_an_atom_never_cites_evidence_the_prompt_lost():
    """``learning_ids`` means attached; what retrieval found is kept beside it."""

    from services.draft_generation_service import _reconcile_prompt_attachment

    context = {
        "similar_approved_answers": [{"learning_example_id": 1}],
        "subquestion_evidence": [{"subquestion": "A", "learning_ids": [1, 2]}],
        "atomic_questions": [{"text": "A", "learning_ids": [1, 2]}],
    }
    prompt_input = {
        "similar_approved_answers": [{"learning_example_id": 1}],
        "subquestion_evidence": [{"subquestion": "A", "learning_ids": [1, 2]}],
        "atomic_questions": [{"text": "A", "learning_ids": [1, 2]}],
    }

    _reconcile_prompt_attachment(context, prompt_input)

    for holder in (context, prompt_input):
        assert holder["subquestion_evidence"][0]["learning_ids"] == [1]
        assert holder["atomic_questions"][0]["learning_ids"] == [1]
    # The record of what was found stays on the context and only there: the
    # prompt copy would put it in front of the model as if it were evidence.
    assert context["subquestion_evidence"][0]["retrieved_learning_ids"] == [1, 2]
    assert "retrieved_learning_ids" not in prompt_input["subquestion_evidence"][0]
    assert "retrieved_learning_ids" not in prompt_input["atomic_questions"][0]


# --- retrieved provenance never reaches the model --------------------------
#
# ``retrieved_*`` records what retrieval found before the budget spoke. It is
# for the trace and the dashboard. Serialising it into the prompt would hand
# the model the rows the budget had just removed -- and because a retry reuses
# the same learning context, it would do so on every following attempt too.

_PROVENANCE_KEYS = (
    "retrieved_learning_ids",
    "retrieved_similar_approved_answers",
    "retrieved_seller_style_examples",
)


def _reconciled(dropped_body="벽걸이 설치비는 청구되지 않습니다."):
    """A context and the prompt built from it, after reconciliation."""

    from services.draft_generation_service import _reconcile_prompt_attachment
    from services.learning_context_service import prompt_context

    context = {
        "similar_approved_answers": [
            {"learning_example_id": 1, "answer": "첨부된 답변입니다.",
             "compatibility": {"product_match": "EXACT_MODEL"}},
            {"learning_example_id": 2, "answer": dropped_body,
             "compatibility": {"product_match": "EXACT_MODEL"}},
        ],
        "seller_style_examples": [],
        "subquestion_evidence": [{"subquestion": "q", "learning_ids": [1, 2]}],
        "atomic_questions": [{"text": "q", "learning_ids": [1, 2]}],
    }
    prompt_input = prompt_context(context)
    # The budget kept only the first row.
    prompt_input["similar_approved_answers"] = [
        row for row in prompt_input["similar_approved_answers"]
        if row["learning_example_id"] == 1
    ]
    _reconcile_prompt_attachment(context, prompt_input)
    return context, prompt_input


def test_the_prompt_never_carries_retrieval_provenance():
    """CASE A: same call."""

    context, prompt_input = _reconciled()
    serialised = json.dumps(prompt_input, ensure_ascii=False)

    for key in _PROVENANCE_KEYS:
        assert key not in serialised, key
    assert prompt_input["subquestion_evidence"][0]["learning_ids"] == [1]
    assert prompt_input["atomic_questions"][0]["learning_ids"] == [1]
    # And the context still holds the record.
    assert context["subquestion_evidence"][0]["retrieved_learning_ids"] == [1, 2]
    assert [row["learning_example_id"]
            for row in context["retrieved_similar_approved_answers"]] == [1, 2]


def test_a_retry_cannot_resurrect_what_the_budget_dropped():
    """CASE B: the same learning context, used again.

    A retry projects the same context a second time. The provenance written by
    the first pass must not become evidence on the second -- neither the keys
    nor, more importantly, the body of the row that was dropped.
    """

    from services.learning_context_service import prompt_context

    dropped = "이 문장은 예산 때문에 제거된 근거입니다."
    context, _first = _reconciled(dropped_body=dropped)

    retry_prompt = prompt_context(context)
    serialised = json.dumps(retry_prompt, ensure_ascii=False)

    for key in _PROVENANCE_KEYS:
        assert key not in serialised, key
    assert dropped not in serialised, "the dropped body came back on retry"
    assert [row["learning_example_id"]
            for row in retry_prompt["similar_approved_answers"]] == [1]
    assert retry_prompt["subquestion_evidence"][0]["learning_ids"] == [1]


def test_the_evidence_map_rows_are_not_shared_with_the_context():
    """Writing the record onto the context must not write it to the prompt.

    They used to be the same dict objects, so the two could not disagree.
    """

    context, prompt_input = _reconciled()

    assert context["subquestion_evidence"][0] is not (
        prompt_input["subquestion_evidence"][0])
    assert "retrieved_learning_ids" in context["subquestion_evidence"][0]
    assert "retrieved_learning_ids" not in prompt_input["subquestion_evidence"][0]
