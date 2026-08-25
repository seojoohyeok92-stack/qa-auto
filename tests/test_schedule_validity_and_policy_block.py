"""Regression tests for B2/B3/B4.

B2  install schedule rules carry a validity window; expired ones are never
    selected and the fallback names no date or event.
B4  a high-risk inquiry is still blocked with no draft, but the block is
    recorded as policy rather than as a system fault.
B3  the 직원 수정본 view only claims STAFF_EDITED when a staff edit exists.

B1 is deliberately absent: the product-fact gate is not changed here until the
server Learning provenance for 686290219 is confirmed.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from answer.config_loader import (
    SCHEDULE_VALIDITY_KEY,
    VALIDITY_ACTIVE,
    VALIDITY_EXPIRED,
    VALIDITY_INVALID,
    VALIDITY_SCHEDULED,
    active_install_schedule_rules,
    clear_config_cache,
    install_schedule_status,
    load_answer_config,
    parse_schedule_validity,
)
from answer.engine import AnswerEngine
from answer.exceptions import AutoAnswerProhibitedError
from core.time_utils import KST


NEW_ORDER_QUESTION = "지금 주문하면 배송은 보통 어떻게 진행되나요?"
BUSINESS_TV_43 = "삼성 107.9cm(43인치) 비즈니스TV 4K UHD 1등급 LH43BEDHLGFXKR 스탠드형"
BUSINESS_TV_50 = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"


# ---------------------------------------------------------------- B2 validity
def test_permanent_rule_is_always_active():
    row = {"사용여부": "Y", "우선순위": 10, "유효유형": "PERMANENT"}
    assert install_schedule_status(row) == VALIDITY_ACTIVE


def test_temporary_rule_expires_after_valid_until():
    row = {
        "사용여부": "Y",
        "우선순위": 20,
        "유효유형": "TEMPORARY",
        "유효시작": "2026-08-10",
        "유효종료": "2026-08-16",
    }
    inside = datetime(2026, 8, 12, tzinfo=KST)
    boundary = datetime(2026, 8, 16, 23, 59, tzinfo=KST)
    after = datetime(2026, 8, 17, 0, 1, tzinfo=KST)
    before = datetime(2026, 8, 9, tzinfo=KST)
    assert install_schedule_status(row, now=inside) == VALIDITY_ACTIVE
    assert install_schedule_status(row, now=boundary) == VALIDITY_ACTIVE
    assert install_schedule_status(row, now=after) == VALIDITY_EXPIRED
    assert install_schedule_status(row, now=before) == VALIDITY_SCHEDULED


@pytest.mark.parametrize(
    "row",
    [
        {"유효유형": "TEMPORARY"},                       # no end date
        {"유효유형": "TEMPORARY", "유효종료": "8월 둘째 주"},  # unparseable
        {"유효유형": "TEMPORARY", "유효시작": "2026-09-01",
         "유효종료": "2026-08-01"},                        # inverted window
        {"유효유형": "SOMETIME", "유효종료": "2026-08-01"},  # unknown type
    ],
)
def test_incomplete_temporary_window_is_never_used(row):
    """Fail closed: a schedule the operator could not date must not be used."""

    assert install_schedule_status({"사용여부": "Y", **row}) == VALIDITY_INVALID


def test_english_aliases_are_accepted():
    row = {"validity_type": "TEMPORARY", "valid_until": "2026-08-16"}
    parsed = parse_schedule_validity(row)
    assert parsed["type"] == "TEMPORARY"
    assert parsed["error"] is None
    assert (
        install_schedule_status(row, now=datetime(2026, 8, 20, tzinfo=KST))
        == VALIDITY_EXPIRED
    )


def test_expired_rules_are_kept_on_record_not_deleted():
    """B2 asks for deactivation, not deletion: the row must survive loading."""

    clear_config_cache()
    config = load_answer_config()
    raw = json.loads(
        (
            __import__("pathlib").Path("answer_data/configs/install_schedule_rules.json")
        ).read_text(encoding="utf-8")
    )
    enabled = [r for r in raw if str(r.get("사용여부", "Y")).upper() == "Y"]
    assert len(config.install_schedule_rules) == len(enabled)
    assert all(
        SCHEDULE_VALIDITY_KEY in rule for rule in config.install_schedule_rules
    )
    expired = [
        rule for rule in config.install_schedule_rules
        if install_schedule_status(rule) == VALIDITY_EXPIRED
    ]
    assert expired, "감사제 기간성 rule이 만료 상태로 보존되어 있어야 합니다."


def test_active_subset_shrinks_as_the_event_window_passes():
    clear_config_cache()
    rules = load_answer_config().install_schedule_rules
    during = active_install_schedule_rules(
        rules, now=datetime(2026, 8, 12, tzinfo=KST)
    )
    after = active_install_schedule_rules(
        rules, now=datetime(2026, 8, 24, tzinfo=KST)
    )
    assert len(after) < len(during)


# ------------------------------------------------------- B2 answer behaviour
def test_valid_rule_is_still_applied():
    """A rule that has not expired keeps answering exactly as before."""

    body = AnswerEngine().answer(BUSINESS_TV_43, NEW_ORDER_QUESTION).answer
    assert "1~2주" in body


def test_expired_rule_is_not_used_and_falls_back_safely():
    result = AnswerEngine().answer(BUSINESS_TV_50, NEW_ORDER_QUESTION)
    assert result.status == "답변 가능"
    assert "감사제" not in result.answer
    assert "둘째 주" not in result.answer
    assert "설치 기사님 일정" in result.answer


@pytest.mark.parametrize(
    "product",
    [BUSINESS_TV_50, "삼성 무빙스타일 LS32DM501E-2WO", "삼성 85인치 LH85BEFH 사이니지"],
)
@pytest.mark.parametrize(
    "question",
    [
        NEW_ORDER_QUESTION,
        "주문하면 얼마나 걸리나요",
        "설치일 조율 가능한가요? 토요일 원해요",
    ],
)
def test_no_expired_event_wording_reaches_any_install_answer(product, question):
    body = AnswerEngine().answer(product, question).answer
    assert "감사제" not in body
    assert "둘째 주" not in body


def test_fallback_used_when_every_rule_for_the_product_expired(monkeypatch):
    """With no valid rule at all, the generic install guidance is used."""

    engine = AnswerEngine()
    monkeypatch.setattr(
        "answer.engine.active_install_schedule_rules", lambda rules, **_: []
    )
    body = engine.answer(BUSINESS_TV_43, NEW_ORDER_QUESTION).answer
    assert "1~2주" not in body
    assert "감사제" not in body
    assert engine.config.shipping["install_new_order_default_answer"] in body


# ------------------------------------------- B2 event notice inside a template
MOVING_STYLE = "삼성 무빙스타일 LS32DM501E-2WO 32인치 스마트모니터"
REVIEW_DELAY_QUESTION = "리뷰 이벤트 참여하려는데 배송예정일이 한달이상 걸리는데요"


def test_expired_event_notice_is_dropped_from_the_review_answer():
    body = AnswerEngine().answer(MOVING_STYLE, REVIEW_DELAY_QUESTION).answer
    assert "감사제" not in body
    # The permanent half of the answer must survive.
    assert "수령일로부터 30일" in body


def test_event_notice_is_appended_while_its_window_is_open(monkeypatch):
    from answer.config_loader import VALIDITY_ACTIVE as ACTIVE

    monkeypatch.setattr(
        "answer.engine.install_schedule_status", lambda row, **_: ACTIVE
    )
    body = AnswerEngine().answer(MOVING_STYLE, REVIEW_DELAY_QUESTION).answer
    assert "감사제" in body
    assert "수령일로부터 30일" in body


def test_no_config_answer_reachable_today_still_names_the_event():
    """Nothing the rule engine can emit today may carry the lapsed notice."""

    engine = AnswerEngine()
    probes = [
        (MOVING_STYLE, REVIEW_DELAY_QUESTION),
        (BUSINESS_TV_50, NEW_ORDER_QUESTION),
        (BUSINESS_TV_50, "설치일 조율 가능한가요? 토요일 원해요"),
        (BUSINESS_TV_43, "주문하면 얼마나 걸리나요"),
        ("삼성 85인치 LH85BEFH 사이니지", "지금 주문하면 배송 언제 되나요"),
    ]
    for product, question in probes:
        body = engine.answer(product, question).answer
        assert "감사제" not in body, (product, question)
        assert "둘째 주" not in body, (product, question)


# ------------------------------------------------------------- B2 cache reload
def test_config_cache_reloads_when_the_file_changes(tmp_path):
    """An operator edit must not need a process restart to take effect."""

    import shutil
    from pathlib import Path

    root = tmp_path / "answer_data"
    shutil.copytree(Path("answer_data"), root)
    clear_config_cache()
    first = load_answer_config(root)
    assert load_answer_config(root) is first  # unchanged files still cache

    target = root / "configs" / "install_schedule_rules.json"
    rows = json.loads(target.read_text(encoding="utf-8"))
    rows[0]["신규주문안내"] = "배송/설치까지 빠르면 3~4일 정도 소요되고 있습니다."
    target.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    second = load_answer_config(root)
    assert second is not first
    assert second.install_schedule_rules[0]["신규주문안내"].endswith(
        "3~4일 정도 소요되고 있습니다."
    )


# --------------------------------------------------------------- B4 policy block
@pytest.fixture
def database(tmp_path):
    from repositories.database import Database

    value = Database(tmp_path / "policy-block.db")
    value.initialize()
    return value


def _high_risk_inquiry(
    database,
    question="제품이 파손돼서 왔는데 환불해주세요.",
    source_id="HIGH-RISK-1",
):
    from repositories.inquiry_repository import InquiryRepository

    return InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source": "PRODUCT_INQUIRY",
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_id": source_id,
        "source_question_id": source_id,
        "external_inquiry_id": source_id,
        "inquiry_type": "PRODUCT_INQUIRY",
        "title": "상품 문의",
        "content": question,
        "product_name": BUSINESS_TV_43,
        "registered_at": "2026-08-24T14:00:00+09:00",
        "raw_json": {},
    }).inquiry_id


def _logs(database, inquiry_id):
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT event_code, level, details_json FROM activity_logs "
            "WHERE inquiry_id=? ORDER BY id",
            (inquiry_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_high_risk_inquiry_still_produces_no_draft(database):
    from services.automatic_draft_service import AutomaticDraftService
    from repositories.answer_repository import AnswerRepository

    inquiry_id = _high_risk_inquiry(database)
    outcome = AutomaticDraftService(database).ensure_for_inquiry(inquiry_id)

    assert outcome.status == "POLICY_BLOCKED"
    assert outcome.draft_id is None
    assert outcome.route == "BLOCKED_REVIEW_REQUIRED"
    assert outcome.error_code == "AUTO_ANSWER_PROHIBITED"
    assert AnswerRepository(database).active_for_inquiry(inquiry_id) is None


def test_policy_block_is_not_recorded_as_a_system_error(database):
    from services.automatic_draft_service import AutomaticDraftService

    inquiry_id = _high_risk_inquiry(database)
    AutomaticDraftService(database).ensure_for_inquiry(inquiry_id)

    rows = _logs(database, inquiry_id)
    codes = {row["event_code"] for row in rows}
    assert "AUTOMATIC_DRAFT_POLICY_BLOCKED" in codes
    assert "AUTOMATIC_DRAFT_FAILED" not in codes
    assert "ANSWER_POLICY_BLOCKED" in codes
    assert not [
        row for row in rows
        if row["level"] == "ERROR"
        and row["event_code"].startswith(("AUTOMATIC_DRAFT", "ANSWER_"))
    ]
    blocked = next(
        row for row in rows if row["event_code"] == "AUTOMATIC_DRAFT_POLICY_BLOCKED"
    )
    details = json.loads(blocked["details_json"])
    assert details["policy_blocked"] is True
    assert details["policy_reason"] == "HIGH_RISK_OR_DISPUTE"


def test_blocked_inquiry_stays_visible_to_staff(database):
    """No draft means the queue status is the only thing surfacing it.

    The staff queue selects on
    ``workflow_status IN ('REVIEW_PENDING','NEEDS_ATTENTION')``. If the policy
    path stopped setting that, a high-risk inquiry with no draft would sit at
    NEW and never reach a person -- the opposite of what the block is for.
    """

    from repositories.inquiry_repository import InquiryRepository
    from services.automatic_draft_service import AutomaticDraftService

    inquiry_id = _high_risk_inquiry(database)
    AutomaticDraftService(database).ensure_for_inquiry(inquiry_id)

    inquiry = InquiryRepository(database).get(inquiry_id)
    assert inquiry["workflow_status"] in {"REVIEW_PENDING", "NEEDS_ATTENTION"}
    assert inquiry["phase9_status"] == "MANUAL_REVIEW_REQUIRED"


def test_auto_answer_prohibited_error_carries_its_reason():
    error = AutoAnswerProhibitedError("blocked", policy_reason="high_risk_or_dispute")
    assert error.reason_code == "AUTO_ANSWER_PROHIBITED"
    assert error.policy_reason == "HIGH_RISK_OR_DISPUTE"


def test_policy_blocked_still_skips_auto_post(database):
    """The new status must not slip past the auto-post skip branch.

    If POLICY_BLOCKED fell through, a high-risk inquiry would continue into
    the posting path -- the exact regression this status could introduce.
    """

    from services.auto_post_pipeline_service import AutoPostPipelineService

    inquiry_id = _high_risk_inquiry(database)
    outcome = AutoPostPipelineService(database).run_pending(
        run_id="policy-block-run",
        owner_id="test-owner",
        max_retries=1,
        limit=10,
        inquiry_ids=[inquiry_id],
    )

    assert outcome.failed_count == 0, "정책 차단은 실패로 집계되면 안 됩니다."
    codes = {row["event_code"] for row in _logs(database, inquiry_id)}
    assert "AUTO_ANSWER_FAILED" not in codes
    if "AUTO_ANSWER_STARTED" in codes:
        assert "AUTO_POST_SKIPPED_POLICY_BLOCKED" in codes
    # Whatever the queue decided, nothing may have been posted.
    with database.connection() as connection:
        posted = connection.execute(
            "SELECT COUNT(*) FROM naver_post_attempts WHERE inquiry_id=?",
            (inquiry_id,),
        ).fetchone()[0]
    assert posted == 0


# ------------------------------------------------------------ B3 UI provenance
def test_untouched_program_answer_is_not_labelled_staff_edited():
    from ui.review_workspace import answer_view_presentation, staff_edit_body

    draft = {"original_answer": "프로그램이 생성한 답변", "edited_answer": ""}
    body, provenance, edited = staff_edit_body(draft)

    assert body == "프로그램이 생성한 답변"  # seed is still shown
    assert provenance == "PROGRAM_GENERATED"
    assert edited is False

    _, shown, tone = answer_view_presentation(
        "직원 수정본", staff_edit_provenance=provenance
    )
    assert "STAFF_EDITED" not in shown
    assert "직원 미수정" in shown
    assert tone == "program"


def test_real_staff_edit_is_labelled_staff_edited():
    from ui.review_workspace import answer_view_presentation, staff_edit_body

    draft = {"original_answer": "프로그램 원문", "edited_answer": "직원이 고친 답변"}
    body, provenance, edited = staff_edit_body(draft)

    assert body == "직원이 고친 답변"
    assert provenance == "STAFF_EDITED"
    assert edited is True

    _, shown, tone = answer_view_presentation(
        "직원 수정본", staff_edit_provenance=provenance
    )
    assert shown == "STAFF_EDITED"
    assert tone == "staff"


def test_posted_answer_seed_is_not_labelled_staff_edited():
    from ui.review_workspace import staff_edit_body

    draft = {"original_answer": "프로그램 원문", "edited_answer": ""}
    body, provenance, edited = staff_edit_body(
        draft, posted_answer_body="네이버에 실제 등록된 답변"
    )
    assert body == "네이버에 실제 등록된 답변"
    assert provenance == "NAVER_POSTED"
    assert edited is False


def test_other_views_keep_their_provenance():
    from ui.review_workspace import answer_view_presentation

    assert answer_view_presentation("Program Answer")[1] == "PROGRAM_GENERATED"
    assert answer_view_presentation("네이버 실제 등록 답변")[1] == "NAVER_POSTED"
    assert answer_view_presentation("Final Answer")[1] == "FINAL_ANSWER"
