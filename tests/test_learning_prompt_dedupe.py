"""One Learning, one body in the prompt.

A row stored as ``style_only`` is appended to the evidence list and the tone list
as the same payload object, so the prompt carried it twice and the provenance
recorded it twice. On 690027174 that is why the trace reported six selected ids
for four rows.

The evidence copy is the one that stays: it is the copy GPT ② may answer from,
and it already carries ``evidence_authority: SELLER_POSTED`` so the model still
knows the wording is the seller's own. Tone does not depend on the duplicate --
``oje_style_rules`` is summarised separately from a hundred-row pool.
"""

from __future__ import annotations

import pytest

from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.learning_context_service import LearningContextService
from tests.test_answer_learning_pipeline_regression import (  # type: ignore
    ORDER_ID, add_learning, make_database,
)

QUESTION = "설치는 기사님이 해주시나요?"


def _facts(target_id: int, question: str) -> AnswerFacts:
    return AnswerFacts(
        inquiry={"inquiry_id": target_id, "question": question,
                 "type": "CUSTOMER_INQUIRY"},
        product={"product_id": "TV-FAMILY", "name": "삼성 TV"},
        order={"order_id": ORDER_ID},
    )


def _build(database, target_id, question):
    intent = IntentResult(
        "INSTALLATION_METHOD", (question,), Emotion.NORMAL, "NORMAL",
        0.95, False, "installation method",
    )
    return LearningContextService(database).build(_facts(target_id, question),
                                                  intent)


@pytest.fixture
def prepared(tmp_path):
    database, source_id, target_id = make_database(tmp_path)
    repository = LearningRepository(database)
    learning_id = add_learning(
        repository, source_id=source_id, source_key="install-actor",
        question="기사님이 설치해주시나요?",
        answer="삼성 기사님이 방문하여 설치해 드립니다.",
    )
    return database, target_id, learning_id


def test_a_learning_body_appears_once_across_both_lists(prepared):
    database, target_id, _learning_id = prepared

    context = _build(database, target_id, QUESTION)

    evidence = [int(item["learning_example_id"])
                for item in context["similar_approved_answers"]]
    style = [int(item["learning_example_id"])
             for item in context["seller_style_examples"]]

    assert not (set(evidence) & set(style)), (evidence, style)
    bodies = len(evidence) + len(style)
    assert bodies == len(set(evidence) | set(style))


def test_the_trace_counts_each_learning_once(prepared):
    """``selected_learning_ids`` concatenated both lists.

    Four rows were reported as six ids, and an operator reading that saw
    duplicates where the retrieval had found none.
    """

    database, target_id, _learning_id = prepared

    trace = _build(database, target_id, QUESTION)["learning_retrieval"]
    selected = list(trace["selected_learning_ids"])

    assert len(selected) == len(set(selected)), selected
    assert trace["selected_count"] == len(selected)


def test_attached_to_prompt_reports_what_was_attached(prepared):
    """It used to be written as ``True`` before a prompt existed."""

    database, target_id, _learning_id = prepared

    trace = _build(database, target_id, QUESTION)["learning_retrieval"]
    attached = set(trace["selected_learning_ids"])

    for item in trace["selected"]:
        expected = int(item["learning_id"]) in attached
        assert item["attached_to_prompt"] is expected, item


def test_why_selected_is_not_the_old_constant(prepared):
    database, target_id, _learning_id = prepared

    trace = _build(database, target_id, QUESTION)["learning_retrieval"]

    for item in trace["selected"]:
        reason = str(item["why_selected"])
        assert reason
        assert "ACTIVE_VALIDITY_AND_RELEVANCE_THRESHOLD" not in reason


def test_the_evidence_copy_is_the_one_kept(prepared):
    """A seller-written row stays readable as the seller's wording.

    Dropping the duplicate must not hide who wrote it; the surviving copy
    carries that on ``evidence_authority``.
    """

    database, target_id, _learning_id = prepared

    context = _build(database, target_id, QUESTION)

    for item in context["similar_approved_answers"]:
        assert item.get("evidence_authority") in {
            "APPROVED", "SELLER_POSTED", "AUTO"}
    # Tone is summarised independently of the duplicated bodies.
    assert "oje_style_rules" in context


# --- the same body from two different rows -------------------------------

def test_two_rows_one_body_two_atoms_through_the_real_build(tmp_path):
    """Two Learning ids, one stored answer, one sub-question each.

    This goes through ``LearningContextService.build`` rather than exercising
    the helpers, because the property only holds if the merge, the evidence map
    and the atom bookkeeping agree with each other.

    Writing the body twice would state one fact as two sources, so one copy
    goes. The copy that goes is the only thing that knew the second
    sub-question had evidence, so the survivor inherits it -- otherwise that
    sub-question's evidence map reports nothing found, and the budget is free
    to drop the surviving body because no sub-question appears to need it.
    """

    from services.learning_context_service import _atoms_of

    database, source_id, target_id = make_database(tmp_path)
    repository = LearningRepository(database)
    body = "삼성 기사님이 방문하여 설치해 드리며, 설치비는 청구되지 않습니다."
    first = add_learning(
        repository, source_id=source_id, source_key="dup-install-actor",
        question="기사님이 설치해주시나요?", answer=body,
    )
    twin = add_learning(
        repository, source_id=source_id, source_key="dup-install-cost",
        question="설치비가 청구되나요?", answer=body,
    )

    atoms = ("기사님이 설치해주시나요?", "설치비가 청구되나요?")
    intent = IntentResult(
        "INSTALLATION_METHOD", atoms, Emotion.NORMAL, "NORMAL",
        0.95, False, "installation",
    )
    context = LearningContextService(database).build(
        _facts(target_id, " ".join(atoms)), intent)

    evidence = context["similar_approved_answers"]
    bodies = ["".join(str(item.get("answer") or "").split()) for item in evidence]
    assert len(bodies) == len(set(bodies)), bodies

    survivors = [item for item in evidence
                 if "".join(str(item.get("answer") or "").split())
                 == "".join(body.split())]
    assert len(survivors) == 1, "the same body reached the prompt twice"
    survivor = survivors[0]

    # Both ids are accounted for: one carries the body, the other is named.
    kept_id = int(survivor["learning_example_id"])
    assert kept_id in {first, twin}
    dropped = {first, twin} - {kept_id}
    if dropped:
        assert dropped <= set(survivor.get("duplicate_learning_ids") or []), (
            survivor.get("duplicate_learning_ids"))

    # And every sub-question the merged row answers still finds it.
    covered = _atoms_of(survivor)
    evidence_map = {entry.get("subquestion"): entry
                    for entry in (context.get("subquestion_evidence") or [])}
    for atom in covered:
        entry = evidence_map.get(atom)
        assert entry is not None, (atom, sorted(evidence_map))
        assert kept_id in [int(x) for x in (entry.get("learning_ids") or [])], (
            atom, entry.get("learning_ids"))
        assert entry.get("evidence_coverage") != "NO_RELIABLE_SOURCE", atom


def test_the_merged_row_protects_both_sub_questions():
    """What the merge is for: the budget must see two questions, not one."""

    from services.learning_context_service import _sole_representatives

    body = "가" * 300
    rows = [
        {"learning_example_id": 100, "answer": body,
         "matched_subquestions": ["A", "B"], "relevance": 0.10},
        {"learning_example_id": 300, "answer": "나" * 300,
         "matched_subquestions": ["A"], "relevance": 0.90},
    ]

    # The merged row is the last evidence both A and B have, so it is spared
    # even though it scores far below the other.
    assert _sole_representatives(rows) == {0}
