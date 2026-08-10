from __future__ import annotations

import pytest

from answer.exceptions import AnswerProviderUnavailableError
from answer.facts import AnswerFacts, build_answer_facts
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from answer.providers.interfaces import JsonGptProvider
from answer.providers.provider_factory import create_gpt_provider


def rule_result(**overrides) -> AnswerResult:
    values = {
        "status": AnswerStatus.GENERATED,
        "category": "제품 기능",
        "reason": "Rule",
        "answer": "넷플릭스를 사용할 수 있습니다.",
        "provider": "rules",
        "auto_answerable": True,
        "needs_review": False,
    }
    values.update(overrides)
    return AnswerResult(**values)


def request(**overrides) -> AnswerRequest:
    values = {
        "inquiry_id": 1,
        "question_id": "Q-1",
        "question": "넷플릭스 되나요?",
        "product_name": "삼성 TV",
        "order_id": "ORDER-1",
        "product_order_id": "PRODUCT-1",
        "metadata": {
            "source_type": "PRODUCT_INQUIRY",
            "dps": {
                "lookup_required": False,
                "lookup_status": "NOT_REQUIRED",
            },
        },
    }
    values.update(overrides)
    return AnswerRequest(**values)


def test_answer_facts_contains_required_sections() -> None:
    facts = build_answer_facts(request(), rule_result())
    assert set(facts.to_dict()) == {
        "inquiry",
        "product",
        "order",
        "delivery",
        "installation",
        "dps",
        "rule",
        "activity",
        "policy",
        "warnings",
    }


def test_answer_facts_keeps_identifiers_out_of_prompt() -> None:
    prompt = build_answer_facts(request(), rule_result()).to_prompt_dict()
    assert "order_id" not in prompt["order"]
    assert "product_order_id" not in prompt["order"]
    assert "question_id" not in prompt["inquiry"]


def test_answer_facts_get_existing_path() -> None:
    facts = build_answer_facts(request(), rule_result())
    assert facts.get_fact("product.name") == "삼성 TV"


def test_answer_facts_get_missing_path_returns_none() -> None:
    facts = build_answer_facts(request(), rule_result())
    assert facts.get_fact("installation.unknown") is None


def test_answer_facts_merges_warnings_without_duplicates() -> None:
    req = request(
        metadata={
            "dps": {
                "lookup_status": "NOT_FOUND",
                "warnings": ["확인 필요", "확인 필요"],
            }
        }
    )
    facts = build_answer_facts(
        req, rule_result(warnings=("Rule 경고", "Rule 경고"))
    )
    assert facts.warnings == ("Rule 경고", "확인 필요")


def test_fake_provider_satisfies_interface() -> None:
    assert isinstance(FakeGptProvider(), JsonGptProvider)


def test_provider_factory_defaults_to_fake(monkeypatch) -> None:
    monkeypatch.delenv("QNA_GPT_PROVIDER", raising=False)
    assert create_gpt_provider().name == "fake_gpt"


@pytest.mark.parametrize("provider", ["openai", "azure", "claude", "gemini"])
def test_unapproved_real_providers_are_disabled(
    provider: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("QNA_GPT_PROVIDER", "fake")
    with pytest.raises(AnswerProviderUnavailableError):
        create_gpt_provider(provider)


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="지원하지"):
        create_gpt_provider("unknown")


@pytest.mark.parametrize(
    ("question", "emotion"),
    [
        ("일반 문의입니다", "NORMAL"),
        ("이게 무슨 뜻인지 모르겠어요", "CONFUSED"),
        ("오늘 꼭 설치해주세요 급해요", "URGENT"),
        ("도대체 왜 안 오나요 최악입니다", "ANGRY"),
        ("감사합니다", "THANKFUL"),
        ("전에 문의했고 추가로 질문합니다", "FOLLOW_UP"),
    ],
)
def test_fake_provider_emotion_json(question: str, emotion: str) -> None:
    raw = FakeGptProvider().generate_json(
        task="UNDERSTANDING",
        prompt="{}",
        context={"question": question, "rule": {}, "dps": {}},
    )
    assert raw["emotion"] == emotion


def test_fake_provider_splits_compound_questions() -> None:
    raw = FakeGptProvider().generate_json(
        task="UNDERSTANDING",
        prompt="{}",
        context={
            "question": "넷플릭스 되나요?\n배송은 언제 오나요? 설치도 하나요?",
            "rule": {},
            "dps": {},
        },
    )
    assert len(raw["questions"]) == 3


def test_fake_provider_returns_gpt_draft_contract() -> None:
    raw = FakeGptProvider().generate_json(
        task="DRAFT",
        prompt="{}",
        context={
            "rule": {"answer": "Rule Answer", "needs_review": False},
            "dps": {},
        },
    )
    assert set(raw) == {
        "answer",
        "confidence",
        "used_facts",
        "missing_information",
        "requires_review",
        "warnings",
    }
    assert raw["used_facts"] == ["rule.answer"]


def test_fake_provider_custom_json_response() -> None:
    provider = FakeGptProvider(responses={"DRAFT": {"answer": "custom"}})
    assert provider.generate_json(
        task="DRAFT", prompt="{}", context={}
    ) == {"answer": "custom"}


def test_fake_provider_failure_is_injectable() -> None:
    provider = FakeGptProvider(fail_tasks={"DRAFT"})
    with pytest.raises(RuntimeError, match="DRAFT"):
        provider.generate_json(task="DRAFT", prompt="{}", context={})


def test_fake_provider_records_calls() -> None:
    provider = FakeGptProvider()
    provider.generate_json(
        task="UNDERSTANDING",
        prompt='{"safe": true}',
        context={"question": "문의", "rule": {}, "dps": {}},
    )
    assert provider.calls[0]["task"] == "UNDERSTANDING"
    assert provider.calls[0]["prompt"] == '{"safe": true}'
