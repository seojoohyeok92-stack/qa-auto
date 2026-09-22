from ui.learning_performance import _usage_reason_for_display


def test_internal_provider_not_used_code_is_never_rendered_raw():
    rendered = _usage_reason_for_display(
        system_reason="PROVIDER_DID_NOT_USE_REFERENCE"
    )

    assert rendered == "모델이 이 자료를 답변 근거로 선택하지 않음"
    assert "PROVIDER_DID_NOT_USE_REFERENCE" not in rendered


def test_provider_reason_has_priority_over_validator_reason():
    assert _usage_reason_for_display(
        provider_reason="상품 조건이 질문과 직접 관련되지 않아 사용하지 않음",
        system_reason="validator fallback",
    ) == "상품 조건이 질문과 직접 관련되지 않아 사용하지 않음"


def test_missing_reason_uses_human_readable_korean_default():
    assert _usage_reason_for_display() == (
        "모델이 이 자료를 답변 근거로 선택하지 않음"
    )


def test_validator_reason_is_shown_when_provider_reason_is_missing():
    assert _usage_reason_for_display(
        system_reason="현재 주문 정보와 충돌하여 제외"
    ) == "현재 주문 정보와 충돌하여 제외"
