"""후보를 좁히는 단계가 어휘 점수가 아니라 의미 판단이어야 하는 이유.

retrieval 은 atom 당 스무 개 남짓을 돌려주는데, 그중 무엇이 verifier 에게
전달되는지는 결정론적 lexical answer-support 점수가 정했다. 325584049 의
"TV 무료 설치인가요" atom 은 후보 16건을 찾아 1건으로 좁혀졌고, 그 1건이
0.33 으로 0.5 문턱에 걸려 **verifier 도달 0건** 이 되었다. 근거 없이 답한
것이 아니라, 근거를 볼 기회 자체가 없었다.

이 파일이 고정하는 계약은 두 가지다: selector 는 넓히기만 하고, 무엇을 근거로
쓸지는 여전히 verifier 가 정한다.
"""
from __future__ import annotations

import pytest

from services.evidence_selection_service import (
    MAX_CANDIDATES,
    EvidenceSelectionService,
    record,
)
from services.evidence_verification_service import (
    selected_pairs_from_context,
)
from services.semantic_analysis import AtomicQuestion


class FakeProvider:
    """대본대로 답하고, 무엇을 물었는지 기록한다."""

    name = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    def generate_json(self, *, task, prompt, context, **_kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if not self.replies:
            return {"selected": []}
        return self.replies.pop(0)


class BrokenProvider:
    name = "broken"

    def generate_json(self, **_kwargs):
        raise TimeoutError("read timeout")


ATOM = {
    "text": "TV 무료 설치인가요",
    "requested_information": "설치 비용 무료 여부",
    "requested_attribute": "AMOUNT_OR_COST",
    "product": "삼성 107.9cm(43인치) 비즈니스TV",
    "purchase_state": "UNKNOWN",
}
CANDIDATES = [
    {"id": 101, "kind": "LEARNING", "question": "설치비가 따로 있나요",
     "answer": "기사님 방문 설치는 무료로 제공됩니다."},
    {"id": 202, "kind": "LEARNING", "question": "폐가전도 수거되나요",
     "answer": "설치 기사님 방문 시 폐가전 수거를 요청하시면 됩니다."},
]


class Semantic:
    usable = True

    def __init__(self, *atoms):
        self.atomic_questions = list(atoms)


def atom_object(text: str, *, attribute: str = "AMOUNT_OR_COST") -> AtomicQuestion:
    return AtomicQuestion(
        text=text, action="INSTALLATION_METHOD",
        requested_information="설치 비용 무료 여부",
        requested_attribute=attribute,
    )


# ------------------------------------------------------------- 선택 계약
def test_the_selector_may_choose_nothing():
    """정답이 없으면 0개가 정상이다. 할당량을 채우게 만들지 않는다."""
    service = EvidenceSelectionService(FakeProvider({"selected": []}))
    outcome = service.select(atom=ATOM, candidates=CANDIDATES)
    assert outcome.selected_ids == ()
    assert outcome.considered == 2


def test_the_selector_may_choose_several():
    service = EvidenceSelectionService(FakeProvider(
        {"selected": [{"id": 101, "why": "비용"}, {"id": 202, "why": "수거"}]}))
    outcome = service.select(atom=ATOM, candidates=CANDIDATES)
    assert set(outcome.selected_ids) == {101, 202}


def test_a_candidate_never_offered_cannot_be_selected():
    """보여주지 않은 후보를 모델이 지어내도 통과시키지 않는다."""
    service = EvidenceSelectionService(FakeProvider(
        {"selected": [{"id": 999, "why": "지어낸 것"}]}))
    assert service.select(atom=ATOM, candidates=CANDIDATES).selected_ids == ()


def test_the_asked_property_reaches_the_selector():
    service = EvidenceSelectionService(FakeProvider({"selected": []}))
    service.select(atom=ATOM, candidates=CANDIDATES)
    prompt = service.prompts[0] if hasattr(service, "prompts") else \
        service.provider.prompts[0]
    assert "AMOUNT_OR_COST" in prompt
    assert "설치 비용 무료 여부" in prompt


def test_the_candidate_answers_reach_the_selector():
    service = EvidenceSelectionService(FakeProvider({"selected": []}))
    service.select(atom=ATOM, candidates=CANDIDATES)
    prompt = service.provider.prompts[0]
    assert "무료로 제공됩니다" in prompt


def test_a_provider_fault_selects_nothing():
    service = EvidenceSelectionService(BrokenProvider())
    outcome = service.select(atom=ATOM, candidates=CANDIDATES)
    assert outcome.selected_ids == ()
    assert outcome.error.startswith("PROVIDER_")


def test_an_unreadable_reply_selects_nothing():
    service = EvidenceSelectionService(FakeProvider("not a mapping"))
    assert service.select(atom=ATOM, candidates=CANDIDATES).selected_ids == ()


def test_the_same_atom_and_candidates_are_not_paid_for_twice():
    provider = FakeProvider({"selected": []}, {"selected": []})
    service = EvidenceSelectionService(provider)
    service.select(atom=ATOM, candidates=CANDIDATES)
    service.select(atom=ATOM, candidates=CANDIDATES)
    assert provider.calls == 1
    assert service.cache_hits == 1


def test_the_candidate_list_is_bounded():
    provider = FakeProvider({"selected": []})
    service = EvidenceSelectionService(provider)
    many = [{"id": i, "kind": "LEARNING", "question": "q", "answer": "a"}
            for i in range(MAX_CANDIDATES + 10)]
    outcome = service.select(atom=ATOM, candidates=many)
    assert outcome.considered == MAX_CANDIDATES


# ------------------------------------------------- 넓히기만 한다 (핵심 안전)
def _context():
    return {
        "similar_approved_answers": [
            {"learning_example_id": 101, "question": "설치비가 따로 있나요",
             "answer": "기사님 방문 설치는 무료로 제공됩니다.",
             "learning_source": "STAFF_EDITED"},
            {"learning_example_id": 202, "question": "폐가전도 수거되나요",
             "answer": "설치 기사님 방문 시 폐가전 수거를 요청하시면 됩니다.",
             "learning_source": "STAFF_EDITED"},
        ],
        "historical_cases": [],
        "subquestion_evidence": [{
            "subquestion": "TV 무료 설치인가요", "status": "ANSWERABLE",
            "source": "ACTIVE_POSITIVE_LEARNING",
            "learning_ids": [101], "historical_case_ids": [],
        }],
    }


def test_what_the_deterministic_ladder_chose_is_always_kept():
    """selector 가 무엇을 하든 기존 후보는 verifier 에 그대로 간다."""
    selector = EvidenceSelectionService(FakeProvider({"selected": []}))
    pairs = selected_pairs_from_context(
        _context(), Semantic(atom_object("TV 무료 설치인가요")), selector)
    ids = {candidate["id"] for _atom, candidates in pairs for candidate in candidates}
    assert 101 in ids


def test_the_selector_can_add_a_candidate_the_ladder_dropped():
    """어휘 점수에 걸려 사라지던 후보가 verifier 까지 도달한다."""
    selector = EvidenceSelectionService(
        FakeProvider({"selected": [{"id": 202, "why": "같은 방문에서 처리"}]}))
    pairs = selected_pairs_from_context(
        _context(), Semantic(atom_object("TV 무료 설치인가요")), selector)
    ids = {candidate["id"] for _atom, candidates in pairs for candidate in candidates}
    assert {101, 202} <= ids


def test_a_selector_fault_leaves_the_pair_set_unchanged():
    from services.evidence_verification_service import pairs_from_context

    semantic = Semantic(atom_object("TV 무료 설치인가요"))
    base = pairs_from_context(_context(), semantic)
    widened = selected_pairs_from_context(
        _context(), semantic, EvidenceSelectionService(BrokenProvider()))
    assert [
        {c["id"] for c in candidates} for _a, candidates in widened
    ] == [
        {c["id"] for c in candidates} for _a, candidates in base
    ]


def test_no_selector_at_all_leaves_the_pair_set_unchanged():
    from services.evidence_verification_service import pairs_from_context

    semantic = Semantic(atom_object("TV 무료 설치인가요"))
    base = pairs_from_context(_context(), semantic)
    assert selected_pairs_from_context(_context(), semantic, None) == base


# --------------------------------------------------------------- 기록
def test_the_record_separates_considered_from_selected():
    """운영자가 '고려되지 않음' 과 '고려했지만 제외' 를 구분할 수 있어야 한다."""
    selector = EvidenceSelectionService(
        FakeProvider({"selected": [{"id": 101, "why": "비용을 말함"}]}))
    outcome = selector.select(atom=ATOM, candidates=CANDIDATES)
    payload = record([outcome])
    assert payload["considered"] == 2
    assert payload["selected"] == 1
    assert payload["errors"] == []
