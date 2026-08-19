from ui.review_workspace import _dps_error_message, _dps_status_label


def status(error_code: str) -> str:
    return _dps_status_label(
        {"lookup_status": "AUTOMATION_ERROR", "error_code": error_code},
        in_progress=False,
        has_order_id=True,
        has_order_date=True,
    )


def test_login_and_automation_failures_are_rendered_separately() -> None:
    assert status("DPS_LOGIN_REQUIRED") == "로그인 필요"
    assert status("DPS_TAB_NOT_FOUND") == "DPS 탭 없음"
    assert status("DPS_PAGE_NOT_READY") == "DPS 화면 준비 안 됨"
    assert status("ORDER_INPUT_NOT_FOUND") == "DPS UI 요소 없음"
    assert status("DPS_AUTOMATION_ERROR") == "DPS 자동화 오류"


def test_ui_element_failure_message_does_not_claim_logged_out() -> None:
    message = _dps_error_message(
        latest={"error_code": "ORDER_INPUT_NOT_FOUND"}
    )
    assert "로그인 필요" not in message
    assert "로그인 상태는 유지" in message
    assert "UI 요소" in message


def test_not_required_status_is_independent_of_order_date() -> None:
    assert _dps_status_label(
        None,
        in_progress=False,
        has_order_id=True,
        has_order_date=False,
        lookup_required=False,
    ) == "조회 불필요"
