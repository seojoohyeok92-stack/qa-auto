"""A compound inquiry's sub-questions must not starve each other.

Each atomic question runs its own retrieval, and the results are merged into one
list before the prompt budget sees them. The budget then drops from that merged
list by relevance, which is fair across rows and unfair across questions: a
narrow question's best answer scores below a broad question's fourth, so the
narrow question loses everything it had while the broad one keeps a stack.

Measured on a three-part inquiry -- 벽걸이 가능한가요 / 설치도 해주시나요 / 무게는
몇 kg인가요 -- retrieval found five installation answers, one of them written for
this very listing, and the assembled prompt contained none of them: 15 rows were
cut to 3 and all three survivors were about the wall mount.

So a sub-question's *last* remaining evidence is spared while anything else can
go instead. That is all this does. It never adds a row back, so a row removed
for identity, attribute or duplication stays removed; and when every survivor is
some sub-question's last, the weakest still goes, so the budget stays absolute
and the list can still empty.
"""

from __future__ import annotations

from services.learning_context_service import (
    _atoms_of, _least_relevant, _sole_representatives, apply_prompt_budget,
)

WALL = "벽걸이 가능한가요?"
INSTALL = "설치도 해주시나요?"
WEIGHT = "무게는 몇 kg인가요?"


def _row(learning_id, relevance, atom, *, atoms=None, body=400):
    item = {
        "learning_example_id": learning_id,
        "relevance": relevance,
        "matched_subquestion": atom,
        "answer": "가" * body,
    }
    if atoms:
        item["matched_subquestions"] = list(atoms)
    return item


def _kept(context, budget):
    trimmed, _report = apply_prompt_budget(context, budget=budget)
    return [item["learning_example_id"]
            for item in trimmed["similar_approved_answers"]]


# --- A. every sub-question that found evidence keeps some ------------------

def test_each_subquestion_keeps_its_best_when_the_prompt_shrinks():
    rows = [
        _row(1, 0.95, WALL), _row(2, 0.90, WALL), _row(3, 0.85, WALL),
        _row(4, 0.40, INSTALL), _row(5, 0.35, INSTALL),
        _row(6, 0.30, WEIGHT),
    ]

    kept = _kept({"similar_approved_answers": rows}, 1_600)

    assert 4 in kept, "the installation question kept nothing"
    assert 6 in kept, "the weight question kept nothing"
    assert 1 in kept
    # The surplus went from the question that had most to spare.
    assert 3 not in kept


def test_the_lowest_scoring_question_is_not_the_one_silenced():
    """Global relevance order alone would have emptied INSTALL first."""

    rows = [_row(1, 0.95, WALL), _row(2, 0.90, WALL),
            _row(3, 0.20, INSTALL)]

    assert _kept({"similar_approved_answers": rows}, 1_300) == [1, 3]


# --- B. nothing is invented for a question that found nothing --------------

def test_a_question_with_no_evidence_gets_none():
    """Preservation protects what retrieval found; it does not add.

    Only the wall-mount question found anything. Trimming must not conjure a
    row for the other two, and must not refuse to shrink on their behalf.
    """

    rows = [_row(1, 0.95, WALL), _row(2, 0.90, WALL), _row(3, 0.85, WALL)]

    trimmed, _report = apply_prompt_budget(
        {"similar_approved_answers": rows}, budget=1_300)
    kept = trimmed["similar_approved_answers"]

    assert len(kept) < len(rows), "the budget still shrank the list"
    assert {item["matched_subquestion"] for item in kept} == {WALL}
    # Whatever survives is the strongest of what was there, never an addition.
    assert kept[0]["learning_example_id"] == 1
    assert len(kept) == len({i["learning_example_id"] for i in kept})


def test_a_rejected_row_never_returns():
    """Identity, attribute and duplicate removals happen upstream.

    ``apply_prompt_budget`` only ever removes from the list it is handed, so a
    row that retrieval rejected cannot be restored by this rule.
    """

    rows = [_row(1, 0.95, WALL)]

    trimmed, _report = apply_prompt_budget(
        {"similar_approved_answers": list(rows)}, budget=10_000)

    assert [r["learning_example_id"] for r in trimmed["similar_approved_answers"]] == [1]


# --- C. one body, provenance for every question it answered ----------------

def test_a_row_answering_two_questions_protects_both():
    shared = _row(1, 0.50, WALL, atoms=[WALL, INSTALL])
    rows = [shared, _row(2, 0.95, WEIGHT), _row(3, 0.90, WEIGHT)]

    assert _atoms_of(shared) == (WALL, INSTALL)
    kept = _kept({"similar_approved_answers": rows}, 1_300)
    assert 1 in kept, "dropping it would have silenced two questions"


def test_the_shared_body_appears_once():
    shared = _row(1, 0.50, WALL, atoms=[WALL, INSTALL])

    trimmed, _report = apply_prompt_budget(
        {"similar_approved_answers": [shared]}, budget=10_000)

    assert len(trimmed["similar_approved_answers"]) == 1
    assert trimmed["similar_approved_answers"][0]["matched_subquestions"] == [
        WALL, INSTALL]


# --- D. a tight budget still shrinks, and still ends at empty --------------

def test_surplus_goes_before_any_representative():
    rows = [_row(1, 0.95, WALL), _row(2, 0.94, WALL), _row(3, 0.93, WALL),
            _row(4, 0.10, INSTALL)]

    kept = _kept({"similar_approved_answers": rows}, 1_300)

    assert kept == [1, 4]


def test_when_everything_is_a_representative_the_weakest_still_goes():
    rows = [_row(1, 0.90, WALL), _row(2, 0.10, INSTALL)]

    assert _kept({"similar_approved_answers": rows}, 700) == [1]


def test_the_list_can_still_shrink_to_empty():
    rows = [_row(1, 0.90, WALL, body=4_000)]

    trimmed, report = apply_prompt_budget(
        {"similar_approved_answers": rows}, budget=200)

    assert trimmed["similar_approved_answers"] == []
    assert report["dropped"][0]["kept_records"] == 0


# --- E/F. the helpers say what they mean -----------------------------------

def test_an_entry_without_an_atom_is_never_protected():
    """Style references and historical cases belong to no sub-question."""

    assert _atoms_of({"answer": "x"}) == ()
    assert _atoms_of("not a mapping") == ()
    assert _sole_representatives([{"answer": "x"}, {"answer": "y"}]) == set()


def test_sole_representatives_names_only_the_last_one():
    rows = [_row(1, 0.9, WALL), _row(2, 0.8, WALL), _row(3, 0.7, INSTALL)]

    assert _sole_representatives(rows) == {2}


def test_least_relevant_prefers_an_expendable_row():
    rows = [_row(1, 0.9, WALL), _row(2, 0.8, WALL), _row(3, 0.1, INSTALL)]

    # Index 2 is weakest overall but is INSTALL's only row, so index 1 goes.
    assert _least_relevant(rows) == 1
