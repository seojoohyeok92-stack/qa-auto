from __future__ import annotations

import json
from pathlib import Path

import pytest

from answer.engine import AnswerEngine
from answer.exceptions import (
    AnswerGenerationError,
    AnswerProviderUnavailableError,
)
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.providers.base import AnswerProvider
from answer.providers.openai_provider import OpenAIProvider
from answer.text_utils import mask_personal_information


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "auto_qna_characterization.json"
)


@pytest.fixture(scope="module")
def engine() -> AnswerEngine:
    return AnswerEngine()


@pytest.fixture(scope="module")
def characterization_cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def expected_public_status(expected: dict) -> AnswerStatus:
    if expected["status"] == "답변 가능":
        return AnswerStatus.GENERATED
    if expected["category"] == "기타/직원확인":
        return AnswerStatus.NOT_SUPPORTED
    return AnswerStatus.NEEDS_REVIEW


def test_characterization_fixture_matches_original_results(
    engine: AnswerEngine,
    characterization_cases: list[dict],
) -> None:
    assert len(characterization_cases) == 10
    for case in characterization_cases:
        expected = case["expected"]
        result = engine.generate(
            AnswerRequest(
                product_name=case["product"],
                question=case["question"],
                option_name=case["option_name"],
            )
        )
        assert result.status is expected_public_status(expected), case["id"]
        assert result.answer == expected["answer"], case["id"]
        assert result.reason == expected["reason"], case["id"]
        assert result.category == expected["category"], case["id"]
        assert result.provider == expected["provider"], case["id"]
        assert result.metadata["source_status"] == expected["status"], case["id"]
        assert result.metadata["question_count"] == (
            expected["question_count"]
        ), case["id"]
        assert result.metadata["question_breakdown"] == (
            expected["question_breakdown"]
        ), case["id"]


def test_generated_result_is_auto_answerable(engine: AnswerEngine) -> None:
    result = engine.generate(
        AnswerRequest(
            product_name="삼성 스마트모니터 M5 32인치",
            question="배송은 얼마나 걸리나요?",
        )
    )
    assert result.status is AnswerStatus.GENERATED
    assert result.auto_answerable is True
    assert result.needs_review is False
    assert result.answer


def test_unsupported_result_requires_review(engine: AnswerEngine) -> None:
    result = engine.generate(
        AnswerRequest(
            product_name="삼성 스마트모니터 M5",
            question="이 제품 정말 좋은가요?",
        )
    )
    assert result.status is AnswerStatus.NOT_SUPPORTED
    assert result.auto_answerable is False
    assert result.needs_review is True
    assert result.answer == ""


def test_empty_question_is_not_success(engine: AnswerEngine) -> None:
    result = engine.generate(
        AnswerRequest(product_name="삼성 스마트모니터 M5", question="")
    )
    assert result.status is AnswerStatus.NOT_SUPPORTED
    assert result.answer == ""
    assert "문의 내용이 비어 있습니다." in result.warnings


class EmptySuccessProvider(AnswerProvider):
    name = "empty"

    def generate(self, request, rule_evaluator):
        return AnswerResult(
            status=AnswerStatus.GENERATED,
            category="test",
            reason="test",
            answer="",
            provider=self.name,
            auto_answerable=True,
            needs_review=False,
        )


def test_empty_generated_answer_is_rejected() -> None:
    engine = AnswerEngine(provider=EmptySuccessProvider())
    with pytest.raises(AnswerGenerationError, match="답변 본문"):
        engine.generate(AnswerRequest(question="질문", product_name="상품"))


def test_personal_information_masking() -> None:
    value = mask_personal_information(
        "고객명: 홍길동 / 010-1234-5678 / user@example.com / "
        "2026072912345678"
    )
    assert "홍길동" not in value
    assert "010-1234-5678" not in value
    assert "user@example.com" not in value
    assert "2026072912345678" not in value


def test_openai_provider_is_explicitly_disabled(engine: AnswerEngine) -> None:
    provider = OpenAIProvider(enabled=True)
    assert provider.enabled is False
    with pytest.raises(AnswerProviderUnavailableError, match="비활성화"):
        provider.generate(
            AnswerRequest(question="질문"),
            engine._generate_with_rules,
        )
