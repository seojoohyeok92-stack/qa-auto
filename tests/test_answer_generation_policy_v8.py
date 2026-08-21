"""Acceptance for the Learning+GPT-first answer generation policy.

Root cause this pins: ``_template_unavailable_reason`` used to accept *any*
active, store/type-compatible rule result as the final customer answer. The
rulebook matches on substring keywords only, so a question like
"AS는 삼성서비스센터에서 하나요?" matched an "A/S 접수 전화번호" rule and the
customer got a phone number instead of an answer to their actual question --
while the manual "GPT 새 답변 생성" button (prefer_template=False) produced a
correct direct answer. Templates may now decide the final answer only when the
match is deterministic for the situation.

No provider calls: these assert the routing decision, not generated prose.
"""
from __future__ import annotations

import dataclasses

import pytest

from answer.config_loader import load_answer_config
from answer.engine import AnswerEngine
from services.answer_service import (
    EXACT_TEMPLATE_MATCH_KINDS,
    _template_may_answer,
)


AS_QUESTION = "AS는 삼성서비스센터에서 하나요?"
AS_TEMPLATE_ANSWER = (
    "제품 사용 중 고장이나 불량이 의심되는 경우 삼성전자 고객센터 "
    "1588-3366으로 문의해 A/S 접수해 주시면 됩니다."
)


def engine_with_rule(**rule_overrides) -> AnswerEngine:
    """An engine whose rulebook holds one keyword rule, as production does."""
    rule = {
        "상품키워드": "",
        "질문키워드": "AS",
        "우선순위": 1,
        "답변본문": AS_TEMPLATE_ANSWER,
        "카테고리": "AS/고장",
    }
    rule.update(rule_overrides)
    config = dataclasses.replace(
        load_answer_config(), learned_rules=(rule,)
    )
    return AnswerEngine(config=config)


# CASE A -- the real production AS case: a keyword rule matches, but must not
# become the customer-facing final answer.
def test_case_a_as_keyword_rule_does_not_decide_final_answer() -> None:
    result = engine_with_rule().answer("삼성 UHD BE85D-H TV", AS_QUESTION)
    assert result.match_kind == "KEYWORD_LEARNED_RULE"
    assert _template_may_answer(
        {"template_match_kind": result.match_kind}
    ) is False


# CASE K -- partial keyword overlap must never force the final answer, even
# when the rule text is perfectly valid on its own.
@pytest.mark.parametrize(
    "question",
    [
        "AS는 삼성서비스센터에서 하나요?",
        "AS 받으려면 어디로 가야 하나요?",
        "구매 후 AS 기간이 궁금합니다",
    ],
)
def test_case_k_partial_keyword_overlap_never_forces_template(
    question: str,
) -> None:
    result = engine_with_rule().answer("삼성 TV", question)
    if result.match_kind == "KEYWORD_LEARNED_RULE":
        assert _template_may_answer(
            {"template_match_kind": result.match_kind}
        ) is False


# CASE C/J -- deterministic fixed policy and catalog answers keep their
# authority; the relaxation must not disable legitimate templates.
@pytest.mark.parametrize("kind", sorted(EXACT_TEMPLATE_MATCH_KINDS))
def test_case_j_exact_fixed_kinds_may_still_answer(kind: str) -> None:
    assert _template_may_answer({"template_match_kind": kind}) is True


# The generic product-usage catch-all is not an exact answer either.
def test_generic_product_usage_is_not_an_exact_template() -> None:
    assert _template_may_answer(
        {"template_match_kind": "KEYWORD_SIMPLE_PRODUCT_USAGE"}
    ) is False


def test_no_match_never_answers() -> None:
    assert _template_may_answer({"template_match_kind": "NO_MATCH"}) is False


# Results built outside AnswerEngine (legacy fixtures, injected providers)
# must keep working exactly as before.
def test_unknown_match_kind_preserves_legacy_behaviour() -> None:
    assert _template_may_answer({}) is True
    assert _template_may_answer({"template_match_kind": "UNKNOWN"}) is True


# CASE T -- an ambiguous question must not be captured by an unrelated
# keyword template; it has to reach the Learning/GPT path instead.
def test_case_t_ambiguous_question_is_not_captured_by_keyword_template() -> None:
    result = engine_with_rule().answer("삼성 TV", "튼튼한가요?")
    assert _template_may_answer(
        {"template_match_kind": result.match_kind}
    ) is False


# CASE B/D -- the manual button and the automatic pipeline must run the same
# core generator, so their answer quality cannot diverge again.
def test_case_b_manual_and_automatic_share_one_generation_service() -> None:
    import inspect

    import services.automatic_draft_service as auto_module
    import ui.review_workspace as manual_module

    auto_source = inspect.getsource(auto_module)
    manual_source = inspect.getsource(manual_module)
    # Neither path may implement its own answer composition.
    assert "generate_for_inquiry" in auto_source
    assert "generate_for_inquiry" in manual_source
    assert "HybridAnswerService(" not in auto_source
    assert "HybridAnswerService(" not in manual_source


# The engine must tag every dispatch branch, otherwise a future matcher would
# silently inherit final-answer authority through the UNKNOWN fallback.
def test_every_engine_branch_declares_a_match_kind() -> None:
    import inspect

    source = inspect.getsource(AnswerEngine.answer)
    finalize_calls = source.count("self._finalize(")
    tagged = source.count("match_kind=")
    assert finalize_calls > 0
    assert tagged == finalize_calls
