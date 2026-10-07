"""This product's own answer is not removable by an incomplete taxonomy.

The attribute rule exists because another product's specification is not this
one's, and it decides by reading each side's subject off the text. That reading
is incomplete, and the gap is not rare: ``attribute_families`` has no family for
an OTT app, so on inquiry 3070 -- "BID-AT200 셋톱박스 sk브로드벤드에서 설치한건데
연결해도 ott볼수있는거죠?" -- the question reduced to INSTALLATION off the word
"설치한", while the matching approved answer, which says in so many words that
OTT is not built in and needs a set-top box carrying it, reduced to PORTS off
"RF 단자" and "HDMI 단자". The one subject both sides were actually about was
invisible to the comparison. The row was EXACT_MODEL and the semantic index
ranked it FIRST, and it was deleted before GPT ② saw it. The same inquiry kept
319049 -- the same listing's RF-port answer -- so whatever that block protected,
it was not identity.

So a row whose identity is confirmed is exempt from the hard block. It is NOT
waved through: it is carried, labelled ATTRIBUTE_MISMATCH rather than passed off
as UNRESOLVED, it earns no ATTRIBUTE_MATCH boost, and it still has to out-rank
the competition to reach the prompt. Everything with an unconfirmed identity --
another model, another listing, another category -- blocks exactly as before.

These tests drive ``SimilarAnswerService.search`` itself with a supplied
candidate pool, so the condition under test is the shipped one and not a
restatement of it.
"""

from __future__ import annotations

import pytest

from services.similar_answer_service import (
    ATTRIBUTE_MATCH_BOOST,
    CONFIRMED_IDENTITY,
    SimilarAnswerService,
)

# The question as GPT ① atomised it, in the form retrieval receives: the atom
# text twice (``requested_information`` and ``atomic_question`` are both set to
# it) plus the retrieval queries.
#
# The wording matters and is not interchangeable: this is the atom GPT ①
# produced for 3070, and "설치한" is the single word that makes the question
# INSTALLATION. A later sample of the same inquiry dropped it -- atom
# "BID-AT200 셋톱박스를 연결하면 OTT를 시청할 수 있는지 문의합니다." names no
# family at all, so the gate stays silent and nothing is blocked OR carried.
# Which is its own finding about the taxonomy, and the reason this fixture is
# pinned to the wording that reproduces.
OTT_QUESTION = (
    "SK브로드밴드에서 설치한 BID-AT200 셋톱박스를 연결하면"
    " OTT를 시청할 수 있는지 문의합니다."
)
OTT_GOAL = {
    "requested_information": OTT_QUESTION,
    "atomic_question": OTT_QUESTION,
    "retrieval_queries": [
        "BID-AT200 셋톱박스에서 OTT 서비스 시청이 가능한지에 대한 안내",
    ],
    "customer_goal": "PRODUCT_SPEC",
}
# 318647, as it sits in the corpus: identified only by its model code, and
# answering in port vocabulary.
OTT_ANSWER = (
    "해당 제품의 경우 RF 단자 있어 동축케이블 연결하여 사용가능하시며,"
    " HDMI 단자 통하여 셋톱박스 연결 후 사용 가능합니다."
    " 또한 OTT 기능은 내장되어 있지 않아, 기능이 내장된 셋톱박스 연결 시"
    " 사용 가능합니다."
)
PRODUCT_NAME = "삼성 4K UHD 스마트 비즈니스 TV LH50BEHHLGFXKR 1등급 125.7cm(50인치)"
MODEL = "LH50BEHHLGFXKR"


class _Repository:
    """A supplied pool must be reused, so nothing here may be called."""

    def candidates(self, **_kwargs):
        raise AssertionError("the supplied candidate pool must be reused")

    def candidate_diagnostics(self, **_kwargs):
        raise AssertionError("the supplied diagnostics must be reused")


def _row(identifier, *, question, answer, product_name, model_code,
         rating=5, created_at="2026-08-20T00:00:00Z"):
    return {
        "id": identifier,
        "learning_source": "APPROVED_UNEDITED",
        "question_original_masked": question,
        "question_normalized": question,
        "final_answer": answer,
        "metadata_json": {
            "learning_signal_type": "POSITIVE",
            "human_verified": True,
        },
        "source_product_id": None,
        "source_product_name": product_name,
        "source_option_name": None,
        "product_name": product_name,
        "model_code": model_code,
        "inquiry_type": "PRODUCT_GENERAL",
        "intent": "PRODUCT_SPEC",
        "style_only": False,
        "style_features_json": {},
        "rating": rating,
        "created_at": created_at,
    }


def _search(pool, *, question=OTT_QUESTION, goal=None, model_code=MODEL,
            product_name=PRODUCT_NAME, semantic_ranks=None):
    service = SimilarAnswerService(_Repository())
    results = service.search(
        question,
        store_code="OJE_PLUS",
        product_name=product_name,
        model_code=model_code,
        inquiry_type="PRODUCT_GENERAL",
        intent="PRODUCT_SPEC",
        product_fact_sensitive=True,
        identity_enforced=True,
        semantic_goal=OTT_GOAL if goal is None else goal,
        semantic_ranks=semantic_ranks or {},
        candidate_pool=list(pool),
        candidate_diagnostics={
            "active_candidates": len(pool),
            "filtered_by_validity": 0,
            "revoked": 0,
            "negative_excluded": 0,
        },
        # Production builds the context service with this on, and it is not
        # incidental here: with it off, a topic verdict removes the row before
        # the attribute rule is reached (TOPIC_PARTIAL_COVERAGE), and the test
        # would pass or fail for a reason that has nothing to do with the
        # change under test.
        hard_conflicts_only=True,
    )
    return service, results


def _by_id(results):
    return {int(item["id"]): item for item in results}


def _rejections(service):
    return dict(service.last_trace.get("rejection_counts") or {})


# --- the question this change was made for --------------------------------

def test_the_same_model_ott_answer_is_no_longer_hard_blocked():
    """3070 / 318647: EXACT_MODEL, and the taxonomy saw INSTALLATION vs PORTS."""

    service, results = _search([
        _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name=PRODUCT_NAME, model_code=MODEL),
    ])
    found = _by_id(results)
    assert 318647 in found, "this listing's own answer was removed again"
    assert _rejections(service).get("ATTRIBUTE_MISMATCH", 0) == 0


def test_the_carried_row_says_it_is_a_mismatch_rather_than_unresolved():
    """A known disagreement must not be filed as 'could not tell'."""

    _service, results = _search([
        _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name=PRODUCT_NAME, model_code=MODEL),
    ])
    row = _by_id(results)[318647]
    assert row["attribute_state"] == "ATTRIBUTE_MISMATCH"
    assert row["query_attributes"] == ["INSTALLATION"]
    assert row["answer_attributes"] == ["PORTS"]
    assert row["compatibility"]["product_match"] in CONFIRMED_IDENTITY


def test_the_trace_counts_what_it_carried():
    """Not a rejection, so it is counted separately and stays visible."""

    service, _results = _search([
        _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name=PRODUCT_NAME, model_code=MODEL),
    ])
    assert service.last_trace["attribute_mismatch_carried"] == 1
    assert "attribute_mismatch_carried" not in _rejections(service)


# --- what must NOT have changed -------------------------------------------

def test_another_model_is_still_hard_blocked():
    """The rule's whole point. A different model code, same mismatch."""

    service, results = _search([
        _row(900001, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name="삼성 107.9cm(43인치) 비즈니스TV 4K UHD LH43BEFHLGFXKR",
             model_code="LH43BEFHLGFXKR"),
    ])
    assert 900001 not in _by_id(results)
    assert _rejections(service).get("ATTRIBUTE_MISMATCH", 0) == 1
    assert service.last_trace["attribute_mismatch_carried"] == 0


def test_another_product_entirely_is_still_hard_blocked():
    service, results = _search([
        _row(900002, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name="삼성 오디세이 G3 LS32DG300 80.1cm(32인치) 게이밍 모니터",
             model_code="LS32DG300"),
    ])
    assert 900002 not in _by_id(results)
    assert _rejections(service).get("ATTRIBUTE_MISMATCH", 0) == 1


def test_an_unidentified_row_is_still_hard_blocked():
    """No name and no model is not a confirmed identity."""

    service, results = _search([
        _row(900003, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name=None, model_code=None),
    ])
    assert 900003 not in _by_id(results)
    assert _rejections(service).get("ATTRIBUTE_MISMATCH", 0) == 1


def test_the_exemption_needs_the_identity_and_not_just_the_mismatch():
    """Same answer, same question, two rows: only the identified one survives."""

    service, results = _search([
        _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name=PRODUCT_NAME, model_code=MODEL),
        _row(900004, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name="삼성 125.7cm(50인치) UHD 4K 비즈니스TV LH50BEFHLGFXKR",
             model_code="LH50BEFHLGFXKR"),
    ])
    found = _by_id(results)
    assert 318647 in found
    assert 900004 not in found
    assert _rejections(service).get("ATTRIBUTE_MISMATCH", 0) == 1
    assert service.last_trace["attribute_mismatch_carried"] == 1


# --- carried is not promoted ----------------------------------------------

def test_a_carried_row_earns_no_attribute_match_boost():
    """``attribute_state`` is the boost's only input, and it is not MATCH.

    Asserted against the ranking function itself rather than an observed order,
    so it cannot pass because some other signal happened to agree.
    """

    from services.similar_answer_service import _band, _ranking_key

    _service, results = _search([
        _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name=PRODUCT_NAME, model_code=MODEL),
    ])
    row = _by_id(results)[318647]
    assert row["attribute_state"] != "MATCH"
    with_state = _ranking_key(0.5, row, 1)
    neutral = _ranking_key(0.5, {**row, "attribute_state": "X"}, 1)
    assert with_state == neutral
    boosted = _ranking_key(0.5, {**row, "attribute_state": "MATCH"}, 1)
    assert boosted[0] == _band(0.5 + ATTRIBUTE_MATCH_BOOST)
    assert boosted[0] > with_state[0]


def test_a_matching_row_outranks_a_carried_one_at_equal_relevance():
    """Being on the asked-for property still wins the tie it always won."""

    on_topic = _row(
        900005,
        question="셋톱박스 연결하면 설치는 어떻게 되나요?",
        answer="설치 기사님이 방문하여 셋톱박스 연결까지 도와드립니다.",
        product_name=PRODUCT_NAME, model_code=MODEL)
    _service, results = _search([
        on_topic,
        _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
             product_name=PRODUCT_NAME, model_code=MODEL),
    ])
    found = _by_id(results)
    assert found[900005]["attribute_state"] == "MATCH"
    assert found[318647]["attribute_state"] == "ATTRIBUTE_MISMATCH"
    order = [int(item["id"]) for item in results]
    assert order.index(900005) < order.index(318647)


# --- the gate still only runs where it ran ---------------------------------

def test_a_matching_same_product_row_is_untouched_by_any_of_this():
    """319049, the row the same inquiry kept all along.

    Its families are PORTS *and* INSTALLATION -- it talks about what the
    engineer reconnects on the visit -- so it overlaps an INSTALLATION question
    and reaches MATCH on its own merit. Keeping the installation clause in the
    fixture is the point: without it the row is a mismatch like any other, and
    this test would be asserting nothing about MATCH.
    """

    service, results = _search([
        _row(319049, question="RF 단자 있나요?",
             answer="RF 단자가 있어 동축케이블을 연결하실 수 있습니다."
                    " 설치 기사님 방문 시 기존 케이블 재연결까지 진행해 드립니다.",
             product_name=PRODUCT_NAME, model_code=MODEL),
    ])
    row = _by_id(results)[319049]
    assert row["attribute_state"] == "MATCH"
    assert service.last_trace["attribute_mismatch_carried"] == 0


def test_nothing_is_carried_when_the_question_names_no_attribute():
    """An empty query family set blocks nothing and therefore carries nothing."""

    goal = {**OTT_GOAL, "requested_information": "이거 어떤가요?",
            "atomic_question": "이거 어떤가요?", "retrieval_queries": []}
    service, results = _search(
        [
            _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
                 product_name=PRODUCT_NAME, model_code=MODEL),
        ],
        question="이거 어떤가요?", goal=goal)
    row = _by_id(results)[318647]
    assert row["attribute_state"] == "ATTRIBUTE_UNRESOLVED"
    assert service.last_trace["attribute_mismatch_carried"] == 0


@pytest.mark.parametrize("action", ["INSTALLATION_METHOD", "DELIVERY_POLICY",
                                    "COLLECTION"])
def test_a_non_product_fact_question_carries_nothing_either(action):
    """``identity_enforced`` is off there, so neither branch can run."""

    service = SimilarAnswerService(_Repository())
    results = service.search(
        OTT_QUESTION,
        store_code="OJE_PLUS",
        product_name=PRODUCT_NAME,
        model_code=MODEL,
        product_fact_sensitive=False,
        identity_enforced=False,
        semantic_goal={**OTT_GOAL, "customer_goal": action},
        candidate_pool=[
            _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
                 product_name=PRODUCT_NAME, model_code=MODEL),
        ],
        candidate_diagnostics={
            "active_candidates": 1, "filtered_by_validity": 0,
            "revoked": 0, "negative_excluded": 0,
        },
        hard_conflicts_only=True,
    )
    assert 318647 in _by_id(results)
    assert service.last_trace["attribute_mismatch_carried"] == 0


# --- the diagnostics reach the context and stop there ---------------------

def test_the_three_diagnostic_fields_reach_the_context():
    service = SimilarAnswerService(_Repository())
    context = service.context(
        OTT_QUESTION,
        store_code="OJE_PLUS",
        product_name=PRODUCT_NAME,
        model_code=MODEL,
        product_fact_sensitive=True,
        identity_enforced=True,
        semantic_goal=OTT_GOAL,
        candidate_pool=[
            _row(318647, question="ott 볼수있나요?", answer=OTT_ANSWER,
                 product_name=PRODUCT_NAME, model_code=MODEL),
        ],
        candidate_diagnostics={
            "active_candidates": 1, "filtered_by_validity": 0,
            "revoked": 0, "negative_excluded": 0,
        },
        hard_conflicts_only=True,
    )
    offered = (context["similar_approved_answers"]
               + context["seller_style_examples"])
    assert offered, "the row did not reach the context at all"
    item = next(x for x in offered
                if int(x["learning_example_id"]) == 318647)
    assert item["attribute_state"] == "ATTRIBUTE_MISMATCH"
    assert item["query_attributes"] == ["INSTALLATION"]
    assert item["answer_attributes"] == ["PORTS"]


def test_the_diagnostic_fields_are_kept_out_of_the_prompt():
    """They are retrieval working, not evidence, and the projection is a
    deny-list -- so leaving them unlisted would have published all three."""

    from services.learning_context_service import (
        _LEARNING_ITEM_PROMPT_DROP, prompt_context,
    )

    assert {"attribute_state", "query_attributes", "answer_attributes"} <= (
        _LEARNING_ITEM_PROMPT_DROP)
    projected = prompt_context({
        "similar_approved_answers": [{
            "learning_example_id": 1,
            "answer": "본문",
            "attribute_state": "ATTRIBUTE_MISMATCH",
            "query_attributes": ["INSTALLATION"],
            "answer_attributes": ["PORTS"],
        }],
    })
    item = projected["similar_approved_answers"][0]
    assert item["answer"] == "본문"
    assert "attribute_state" not in item
    assert "query_attributes" not in item
    assert "answer_attributes" not in item
