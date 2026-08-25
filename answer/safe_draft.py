"""The conservative draft used when an answer must not be composed.

Two situations produce it and they are deliberately the same draft: the
provider failed, or the pipeline decided before calling the provider that no
generated answer could be published anyway.  In both cases the customer must
still get a reply and staff must still get something to edit, so this echoes
back what was actually asked and says a person is checking -- it never
asserts a fact.

Lives here rather than in ``answer_service`` because the hybrid path needs the
same draft when it stops ahead of the provider, and importing the service from
inside its own dependency would be a cycle.
"""
from __future__ import annotations

from typing import Any

from answer.answer_format import format_final_answer
from answer.models import AnswerResult, AnswerStatus
from answer.text_utils import restore_question_mark


def review_required_safe_result(
    request: Any,
    *,
    template_preferred: bool,
    failure_code: str,
    questions: tuple[str, ...] = (),
    generation_skipped: bool = False,
    skip_reasons: tuple[str, ...] = (),
) -> AnswerResult:
    """Return the last-resort, non-empty customer draft for GPT outages.

    A provider or validator failure is an expected operational state.  It must
    not leave an unanswered inquiry without a Program Answer; staff can replace
    this conservative draft later.

    When the GPT UNDERSTANDING step already decomposed the inquiry before the
    failure, ``questions`` carries what the customer actually asked.  Echoing
    those back keeps this safety draft on-topic instead of a static
    "사용 방법 또는 기능"/"주문 또는 상품" category label that may not match
    the real question (e.g. a product-availability question mislabeled as a
    usage/feature question).  With no decomposed questions available (a
    failure before UNDERSTANDING even ran), the generic category fallback is
    kept as the last resort.
    """

    cleaned_questions = tuple(
        dict.fromkeys(
            restore_question_mark(item)
            for item in questions
            if str(item).strip()
        )
    )
    if cleaned_questions:
        if len(cleaned_questions) == 1:
            confirmation_body = f'문의주신 "{cleaned_questions[0]}"'
        else:
            bullet_list = "\n".join(f"- {item}" for item in cleaned_questions)
            confirmation_body = f"문의주신 아래 내용은\n\n{bullet_list}"
        answer = format_final_answer(
            f"""{confirmation_body} 관련하여 정확한 정보 확인이 필요합니다.

확인되지 않은 내용을 임의로 안내하지 않고 직원 검토가 필요한 상태로 처리하겠습니다."""
        )
    else:
        product_inquiry = str(request.inquiry_type).upper() == "PRODUCT_INQUIRY"
        subject = (
            "문의하신 상품의 사용 방법 또는 기능"
            if product_inquiry
            else "문의하신 주문 또는 상품 관련 내용"
        )
        answer = format_final_answer(
            f"""{subject}은 정확한 정보 확인이 필요한 문의입니다.

확인되지 않은 내용을 임의로 안내하지 않고 직원 검토가 필요한 상태로 처리하겠습니다."""
        )
    return AnswerResult(
        status=AnswerStatus.NEEDS_REVIEW,
        category="직원검토/안전초안",
        reason="자동 답변 공급자 또는 검증 단계 실패 시 사용하는 안전 초안입니다.",
        answer=answer,
        provider="safe_rule",
        auto_answerable=False,
        needs_review=True,
        matched_rule="REVIEW_REQUIRED_SAFE_DRAFT",
        metadata={
            "answer_type": "review_required_safe_draft",
            "answer_source": "SAFE_TEMPLATE",
            "generation_mode": "SAFE_RULE",
            "selected_answer_route": "REVIEW_REQUIRED_SAFE_DRAFT",
            "template_preferred": bool(template_preferred),
            "template_override": False,
            "template_id": "REVIEW_REQUIRED_SAFE_DRAFT",
            "template_name": "REVIEW_REQUIRED_SAFE_DRAFT",
            # A skipped generation never called a provider. Reporting it as
            # "gpt_called" would tell the operator an answer was composed and
            # rejected, when in fact none was ever asked for.
            "gpt_called": not generation_skipped,
            "generation_skipped": bool(generation_skipped),
            "generation_skip_reasons": list(skip_reasons),
            "safe_failure_code": str(failure_code)[:100],
            "draft_created": True,
            "requires_manual_review": True,
        },
    )
