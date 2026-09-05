"""The verifier that judges a stored answer against the question that was asked.

The two cases at the centre of this file survived four selector designs and a
labelling pass over the stored answers. They are here because every cheaper test
-- same topic, same product, same property label -- lets them through, and only
reading the answer beside the question does not.
"""
from __future__ import annotations

import pytest

from services.evidence_verification_service import (
    CONTEXT_INCOMPATIBLE,
    METADATA_KEY,
    NOT_SUPPORTED,
    PARTIALLY_SUPPORTED,
    REASON_CODE,
    SUPPORTED,
    EvidenceVerificationService,
    decision_from_metadata,
    record,
    unverified,
)


class FakeProvider:
    """Returns a scripted verdict, and counts what it was asked."""

    name = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0
        self.prompts = []

    def generate_json(self, *, task, prompt, context, **_kwargs):
        # The production JsonGptProvider signature. This mock previously took a
        # ``system`` keyword the real provider does not have, so it agreed with
        # the service about an interface neither shared with Production.
        self.calls += 1
        self.prompts.append(prompt)
        if not self.replies:
            return {"verdict": NOT_SUPPORTED, "why": "no script"}
        return self.replies.pop(0)


class BrokenProvider:
    name = "broken"

    def generate_json(self, **_kwargs):
        raise TimeoutError("read timeout")


CM18_ATOM = {
    "text": "사다리차가 필요하면 비용은 누가 내나요?",
    "requested_information": "사다리차 비용 부담 주체",
    "requested_attribute": "ACTOR",
    "product": "삼성 125.7cm(50인치) 비즈니스TV",
    "purchase_state": "UNKNOWN",
}
CM18_CANDIDATE = {
    "id": 317203,
    "question": "사다리차 비용 유상무상 궁금합니다",
    "answer": "사다리차 사용 여부에 대해서는 저희가 확인해드릴 수 없으며 설치 기사님께서"
              " 사용 여부를 판단하여 안내드리고 있으며 유상으로 알고 있습니다.",
    "supported_information": ["ladder truck service is paid"],
    "answer_kind": "POLICY",
}
CM01_ATOM = {
    "text": "설치 기사님 안 부르고 받아만 볼 수 있나요?",
    "requested_information": "설치 없이 배송만 받을 수 있는지",
    "requested_attribute": "PERMISSION_OR_OPTION",
    "product": "삼성 107.9cm(43인치) 비즈니스TV",
    "purchase_state": "UNKNOWN",
}
CM01_CANDIDATE = {
    "id": 154681,
    "question": "설치해주시는 상품이라 주문했습니다",
    "answer": "삼성 설치 기사님께서 배송 후 설치까지 진행해드리는 제품 입니다.",
    "supported_information": ["Samsung installer delivery and installation process"],
    "answer_kind": "PROCEDURE",
}


# --------------------------------------------------------------- 판정 계약
def test_a_not_supported_verdict_is_not_usable_evidence():
    service = EvidenceVerificationService(
        FakeProvider({"verdict": NOT_SUPPORTED,
                      "missing": "비용 부담 주체", "why": "판단 주체만 말합니다"}))
    result = service.verify(atom=CM18_ATOM, candidate=CM18_CANDIDATE)
    assert result.verdict == NOT_SUPPORTED
    assert result.usable is False


def test_a_supported_verdict_is_usable_evidence():
    service = EvidenceVerificationService(
        FakeProvider({"verdict": SUPPORTED, "stated_fact": "오토피봇 가능한 제품입니다."}))
    result = service.verify(
        atom={"text": "오토 피벗이 되는 모델인가요?",
              "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
        candidate={"id": 158169, "answer": "오토피봇 가능한 제품입니다."})
    assert result.usable is True


@pytest.mark.parametrize("verdict", [PARTIALLY_SUPPORTED, CONTEXT_INCOMPATIBLE])
def test_only_a_full_verdict_may_stand_as_grounds(verdict):
    service = EvidenceVerificationService(FakeProvider({"verdict": verdict}))
    assert service.verify(atom=CM18_ATOM, candidate=CM18_CANDIDATE).usable is False


def test_an_unknown_verdict_is_treated_as_unverified():
    service = EvidenceVerificationService(FakeProvider({"verdict": "PROBABLY"}))
    result = service.verify(atom=CM18_ATOM, candidate=CM18_CANDIDATE)
    assert result.verdict == NOT_SUPPORTED
    assert result.why == "UNKNOWN_VERDICT"


def test_a_provider_fault_never_becomes_grounds_to_publish():
    service = EvidenceVerificationService(BrokenProvider())
    result = service.verify(atom=CM18_ATOM, candidate=CM18_CANDIDATE)
    assert result.usable is False
    assert result.why.startswith("PROVIDER_")


def test_an_unreadable_reply_is_not_grounds():
    service = EvidenceVerificationService(FakeProvider("not a mapping"))
    assert service.verify(atom=CM18_ATOM, candidate=CM18_CANDIDATE).usable is False


# --------------------------------------------------------------- 프롬프트 내용
def test_the_prompt_carries_the_question_and_one_candidate_only():
    service = EvidenceVerificationService(FakeProvider({"verdict": NOT_SUPPORTED}))
    prompt = service.build_prompt(atom=CM18_ATOM, candidate=CM18_CANDIDATE)
    assert "비용은 누가 내나요" in prompt
    assert "사다리차 비용 부담 주체" in prompt
    assert "ACTOR" in prompt
    assert "유상으로 알고 있습니다" in prompt
    # One candidate. Nothing to rank against.
    assert prompt.count("STORED ANSWER:") == 1


def test_the_asked_property_reaches_the_verifier_as_context():
    """requested_attribute survived the retired gate as verifier context."""
    service = EvidenceVerificationService(FakeProvider({"verdict": NOT_SUPPORTED}))
    prompt = service.build_prompt(atom=CM01_ATOM, candidate=CM01_CANDIDATE)
    assert "PERMISSION_OR_OPTION" in prompt


# --------------------------------------------------------------- 캐시
def test_the_same_pair_is_not_paid_for_twice():
    provider = FakeProvider({"verdict": SUPPORTED}, {"verdict": SUPPORTED})
    service = EvidenceVerificationService(provider)
    service.verify(atom=CM18_ATOM, candidate=CM18_CANDIDATE)
    service.verify(atom=CM18_ATOM, candidate=CM18_CANDIDATE)
    assert provider.calls == 1
    assert service.cache_hits == 1


def test_a_failure_is_not_cached_and_may_be_retried():
    service = EvidenceVerificationService(BrokenProvider())
    service.verify(atom=CM18_ATOM, candidate=CM18_CANDIDATE)
    assert service._cache == {}


# --------------------------------------------------------------- 기록과 hold
def test_nothing_recorded_holds_nothing():
    """Still no hold; the reason now says *why* there is nothing to hold.

    "NOT_RECORDED" conflated "nothing to check" with "should have been checked
    and was not", which is how a missing producer went unnoticed for a
    release. The hold itself is unchanged.
    """
    assert decision_from_metadata({}) == (False, "VERIFICATION_NOT_REQUIRED")


def test_offered_learning_with_nothing_verified_holds():
    payload = record([
        unverified(317203, "판단 주체만 말합니다"),
        unverified(18572, "다른 주제"),
    ])
    assert payload["holds_auto_post"] is True
    hold, why = decision_from_metadata({METADATA_KEY: payload})
    assert hold is True
    assert "NO_USABLE_EVIDENCE" in why


def test_one_verified_candidate_lifts_the_hold():
    from services.evidence_verification_service import Verification
    payload = record([
        Verification(1, NOT_SUPPORTED),
        Verification(2, SUPPORTED, stated_fact="오토피봇 가능한 제품입니다."),
    ])
    assert payload["holds_auto_post"] is False
    assert payload["usable_ids"] == [2]
    assert decision_from_metadata({METADATA_KEY: payload})[0] is False


def test_a_single_verified_candidate_is_enough():
    """Eight of thirteen measured correct auto-answers rest on one evidence."""
    from services.evidence_verification_service import Verification
    payload = record([Verification(158169, SUPPORTED)])
    assert payload["holds_auto_post"] is False


def test_partial_alone_does_not_lift_the_hold():
    from services.evidence_verification_service import Verification
    payload = record([Verification(1, PARTIALLY_SUPPORTED, supports="유상 여부")])
    assert payload["holds_auto_post"] is True


def test_no_candidates_offered_means_this_gate_says_nothing():
    """Absence of Learning is another gate's business, not this one's."""
    payload = record([])
    assert payload["holds_auto_post"] is False


def test_reason_code_is_stable():
    assert REASON_CODE == "EVIDENCE_NOT_VERIFIED"
