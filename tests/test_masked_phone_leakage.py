"""Regression: an internal redaction token reached a customer answer.

Observed on the server:

    "확인이 안 될 경우 <masked-phone>로 문의 바랍니다."

Two independent defects, fixed separately here.

1. A published contact number was treated as personal data. Masking it
   produces an answer that hides the very thing the customer needs, and once
   such an answer is stored as a learning example the redaction token is what
   later answers copy. Approved numbers are now exempt -- by explicit list, so
   the exemption can never widen to a real customer's number.

2. Nothing checked customer-facing text for redaction tokens. The existing
   placeholder rule only knew {{...}}/${...}/[[...]]/{name}, and the PII check
   passes trivially because re-masking already-masked text changes nothing.

The token is never repaired into a number: the answer records that something
was redacted, not what, and guessing risks publishing a real customer's
number as if it were the company's. A person decides.
"""
from __future__ import annotations

import pytest

from answer.answer_validator import AnswerValidator
from answer.text_utils import (
    OFFICIAL_CONTACT_NUMBERS,
    contains_personal_phone,
    is_official_contact_number,
    mask_personal_information,
)
from services.auto_post_validation_service import (
    INTERNAL_REDACTION_TOKENS,
    AutoPostTechnicalValidator,
)
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)
from services.learning_privacy_service import LearningPrivacyService
from services.prompt_privacy_service import PromptPrivacyService

OFFICIAL = "1588-3366"
OJE_OFFICIAL = "02-706-2678"
PERSONAL = "010-1234-5678"
PERSONAL_PLAIN = "01012345678"
LEAK = "확인이 안 될 경우 <masked-phone>로 문의 바랍니다."

_V_PASS = {"status": "PASS", "passed": True, "errors": [],
           "review_signals": [], "warnings": []}


def _mask_all(text: str) -> list[str]:
    """Every masking layer a customer answer can pass through."""

    return [
        mask_personal_information(text),
        LearningPrivacyService().mask(text),
    ]


# --------------------------------------------------- A. official number kept
@pytest.mark.parametrize(
    "text",
    [
        f"제품 관련 문의는 {OFFICIAL}으로 문의해 주세요.",
        f"삼성전자 고객센터 {OFFICIAL}으로 A/S 접수해 주시면 됩니다.",
        f"자세한 내용은 {OFFICIAL} 로 문의해 확인해 주세요.",
    ],
)
def test_A_official_contact_number_survives_every_masking_layer(text):
    for masked in _mask_all(text):
        assert OFFICIAL in masked, masked
        assert "<masked-phone>" not in masked, masked


def test_A_official_number_answer_is_not_held_by_the_gate():
    verdict = _evaluate(f"확인이 안 될 경우 {OFFICIAL}으로 문의 바랍니다.")
    assert verdict.decision == "SAFE", verdict.reasons


def test_A_official_number_passes_template_validation():
    result = AnswerValidator().validate_template_text(
        f"{OFFICIAL}으로 문의해 주세요."
    )
    assert result.passed is True, result.errors


def test_A_allowlist_is_an_explicit_list_not_a_pattern():
    """Widening this to a shape would exempt unrelated numbers."""

    assert OFFICIAL in OFFICIAL_CONTACT_NUMBERS
    assert OJE_OFFICIAL in OFFICIAL_CONTACT_NUMBERS
    assert is_official_contact_number(OFFICIAL) is True
    assert is_official_contact_number(OJE_OFFICIAL) is True
    # Neighbouring numbers of the same shape are not exempt.
    assert is_official_contact_number("1588-1234") is False
    assert is_official_contact_number("02-706-2679") is False
    assert is_official_contact_number("02-706-26780") is False
    assert is_official_contact_number(PERSONAL) is False
    assert is_official_contact_number("") is False


@pytest.mark.parametrize("number", ["1588-3366", "02-706-2678"])
def test_A_every_approved_number_survives_every_layer(number):
    text = f"확인이 안 되시는 경우 {number}로 문의해 주세요."
    for masked in _mask_all(text):
        assert number in masked, masked
        assert "<masked-phone>" not in masked, masked
    assert contains_personal_phone(text) is False


@pytest.mark.parametrize(
    "key",
    ["install_existing_happycall_answer", "install_existing_order_answer"],
)
def test_A_shipping_templates_carrying_the_official_number_now_validate(key):
    """These were blocked as PII exposure because of the company number."""

    from answer.answer_format import format_final_answer
    from answer.config_loader import clear_config_cache, load_answer_config

    clear_config_cache()
    rendered = format_final_answer(load_answer_config().shipping[key])
    assert OJE_OFFICIAL in rendered
    result = AnswerValidator().validate_template_text(rendered)
    assert result.passed is True, result.errors


# ------------------------------------------------- B. personal number masked
@pytest.mark.parametrize("number", [PERSONAL, PERSONAL_PLAIN, "010 1234 5678"])
def test_B_personal_number_is_still_masked(number):
    text = f"제 전화번호는 {number}입니다."
    for masked in _mask_all(text):
        assert number not in masked, masked
        assert "<masked-phone>" in masked, masked
        # and never "restored" into the company number
        assert OFFICIAL not in masked, masked


def test_B_personal_number_never_becomes_the_official_number():
    masked = mask_personal_information(f"제 번호는 {PERSONAL}입니다.")
    assert OFFICIAL not in masked


# --------------------------------------------------------------- C. mixed
def test_C_mixed_text_keeps_official_and_masks_personal():
    text = (f"삼성 고객센터는 {OFFICIAL}, 오제 고객센터는 {OJE_OFFICIAL}이고 "
            f"제 번호는 {PERSONAL}입니다.")
    for masked in _mask_all(text):
        assert OFFICIAL in masked, masked
        assert OJE_OFFICIAL in masked, masked
        assert PERSONAL not in masked, masked
        assert masked.count("<masked-phone>") == 1, masked


def test_C_prompt_privacy_keeps_official_and_masks_personal():
    result = PromptPrivacyService().sanitize(
        {"question": f"고객센터 {OFFICIAL} / 제 번호 {PERSONAL}"}
    )
    rendered = str(result.sanitized_payload)
    assert OFFICIAL in rendered
    assert PERSONAL not in rendered
    assert "<masked-phone>" in rendered


# ------------------------------------------- D. placeholder leakage blocked
def _evaluate(answer: str):
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "content": "배송 문의", "source_answered": 0,
                 "post_status": "NOT_POSTED"},
        draft={"id": 1, "original_answer": answer, "review_status": "PENDING",
               "validation_status": "PASS", "validator_result_json": _V_PASS,
               "posted": 0,
               "metadata_json": {
                   "selected_answer_route": "TEMPLATE",
                   "processing_plan": {"analysis": {}},
                   "product_fact_guard": {"sensitive": False,
                                          "current_fact_verified": False},
                   "hybrid": {"validation": _V_PASS}}},
        route="TEMPLATE",
    )


def test_D_leaked_placeholder_fails_the_pre_post_validator():
    result = AutoPostTechnicalValidator().validate_answer(LEAK)
    assert result.passed is False
    assert "INTERNAL_PLACEHOLDER_EXPOSURE" in result.errors


def test_D_leaked_placeholder_fails_template_validation():
    result = AnswerValidator().validate_template_text(LEAK)
    assert result.passed is False


def test_D_leaked_placeholder_is_never_auto_postable():
    verdict = _evaluate(LEAK)
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "INTERNAL_PLACEHOLDER_EXPOSURE" in verdict.reasons


def test_D_leaked_placeholder_never_reaches_the_post_client(tmp_path):
    from answer.models import AnswerResult, AnswerStatus
    from repositories.answer_repository import AnswerRepository
    from services.auto_post_pipeline_service import AutoPostPipelineService
    from tests.test_auto_post_pipeline import (
        MockClient, make_database, make_inquiry, post_service,
    )

    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="배송", reason="test",
            answer=LEAK, provider="rules", auto_answerable=True,
            needs_review=False,
            metadata={"selected_answer_route": "TEMPLATE",
                      "generation_mode": "TEMPLATE",
                      "validator_result": {"status": "PASS", "passed": True},
                      "hybrid": {"validation": _V_PASS}}),
    )
    client = MockClient()
    outcome = AutoPostPipelineService(
        database,
        post_service=post_service(database, client),
        dps_status_provider=lambda: {"session_status": "READY"},
    ).run_pending(run_id="LEAK", owner_id="LEAK", max_retries=1)

    assert outcome.succeeded_count == 0
    assert client.calls == 0


@pytest.mark.parametrize("token", INTERNAL_REDACTION_TOKENS)
def test_D_every_internal_redaction_token_is_guarded(token):
    """Whichever layer produced it, the token must not reach a customer."""

    answer = f"안내드립니다. <{token}> 를 참고해 주세요."
    assert AutoPostTechnicalValidator().validate_answer(answer).passed is False
    assert AnswerValidator().validate_template_text(answer).passed is False


def test_D_placeholder_is_never_repaired_into_a_number():
    """The guard blocks; it must not guess what was redacted."""

    result = AutoPostTechnicalValidator().validate_answer(LEAK)
    assert result.passed is False
    # The answer text is untouched by validation -- no restoration anywhere.
    assert "<masked-phone>" in LEAK
    assert OFFICIAL not in LEAK


def test_ordinary_angle_brackets_are_not_mistaken_for_a_leak():
    for answer in ("<주의> 설치 시 참고해 주세요.", "a < b 인 경우입니다.",
                   "<b>굵게</b> 표시된 부분입니다."):
        assert AutoPostTechnicalValidator().validate_answer(answer).passed is True, answer


# ------------------------------------------------- template / learning paths
def test_template_path_preserves_the_official_number():
    from answer.answer_format import format_final_answer

    rendered = format_final_answer(
        f"제품 사용 중 고장이 의심되는 경우 삼성전자 고객센터 {OFFICIAL}으로 "
        "문의해 A/S 접수해 주시면 됩니다."
    )
    assert OFFICIAL in rendered
    assert AnswerValidator().validate_template_text(rendered).passed is True


def test_learning_store_no_longer_redacts_the_official_number():
    """The path that poisoned the stored examples in the first place."""

    stored = LearningPrivacyService().mask(
        f"확인이 어려우시면 {OFFICIAL}로 문의 바랍니다."
    )
    assert OFFICIAL in stored
    assert "<masked-phone>" not in stored
