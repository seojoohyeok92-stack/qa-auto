"""What the review workspace tells an approver to do next.

Approval only produces a Final Answer; registering it is a separate step.  The
notice must match the button beside it: it may point at that step only on a
market a person can actually register to, and must never name another market.
"""

from __future__ import annotations

import pytest

from ui.review_workspace import _approval_next_step_notice

NAVER = {"store_code": "OJE_PLUS"}
COUPANG_NS = {"store_code": "COUPANG_OJE_NS"}
COUPANG_PLUS = {"store_code": "COUPANG_OJE_PLUS"}


def test_naver_still_points_at_the_registration_step() -> None:
    notice = _approval_next_step_notice(NAVER)
    assert notice == (
        "승인 완료했습니다. 아래에서 네이버 답변 등록을 별도로 진행할 수 있습니다."
    )


@pytest.mark.parametrize(
    "inquiry", [COUPANG_NS, COUPANG_PLUS], ids=["ns", "plus"]
)
def test_coupang_now_points_at_its_own_registration_step(inquiry) -> None:
    """Manual Coupang registration is open, so the notice offers that step."""

    notice = _approval_next_step_notice(inquiry)
    assert notice == (
        "승인 완료했습니다. 아래에서 쿠팡 답변 등록을 별도로 진행할 수 있습니다."
    )


@pytest.mark.parametrize(
    "inquiry", [COUPANG_NS, COUPANG_PLUS], ids=["ns", "plus"]
)
def test_a_coupang_notice_never_says_naver(inquiry) -> None:
    assert "네이버" not in _approval_next_step_notice(inquiry)


@pytest.mark.parametrize(
    "inquiry", [COUPANG_NS, COUPANG_PLUS], ids=["ns", "plus"]
)
def test_internal_store_codes_stay_out_of_the_notice(inquiry) -> None:
    notice = _approval_next_step_notice(inquiry)
    assert inquiry["store_code"] not in notice
    assert "COUPANG" not in notice


def test_an_unrecognised_store_is_still_treated_as_naver() -> None:
    """Recording the existing rule, not endorsing it.

    ``market_from_store_code`` maps the COUPANG_ prefix to Coupang and calls
    everything else Naver -- the corpus predating marketplaces is Naver.  That
    single rule also governs the Kakao gate and the auto-post queue, so this
    notice must not disagree with it.  When a third marketplace arrives, the
    rule is what has to learn about it, and this test will fail and say so.
    """

    notice = _approval_next_step_notice({"store_code": "GMARKET_MAIN"})
    assert "네이버 답변 등록을 별도로 진행할 수 있습니다" in notice
