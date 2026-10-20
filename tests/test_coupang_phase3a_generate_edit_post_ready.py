"""Generate, edit, and be ready to register -- while registration stays shut.

Phase 3-A opens the two steps a person performs and leaves the third closed.
The Dashboard already generates and edits an unanswered Coupang inquiry; what
it could not do was say whether the answer would have gone out, because
``preflight`` stops at ``MANUAL_POST_DISABLED`` and reports nothing after it.
An operator then sees a disabled button and no way to tell a ready answer
from a broken one.

``readiness`` reports the same checks with the market permission reported
rather than applied.  The asymmetry is the point and is pinned below: it
writes nothing, sends nothing, and ``post`` keeps applying the permission
itself, so nothing here is a way to register.

The rest of this file is the registration engine as it already stands, tested
against a double so no Coupang write can happen: which inquiry and account a
reply would address, which stored text it would carry, what a second click
does, and what a success, a failure and a retry leave behind.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from answer.answer_format import extract_answer_body
from api.coupang_post_client import CoupangPostError, CoupangReplyResult
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.coupang_post_service import CoupangPostService
from services.market_policy import (
    AUTOMATIC_POST_MARKETS, COUPANG, MANUAL_POST_MARKETS,
    is_store_automatic_post_enabled, is_store_dps_enabled,
    is_store_manual_post_enabled,
)

COUPANG_STORE = "COUPANG_OJE_NS"
NAVER_STORE = "OJE_PLUS"
ACCOUNT = "OJE_NS"
EXTERNAL_ID = "160959847"
AI_ANSWER = "해당 제품은 리모컨이 기본 구성품에 포함되어 있습니다."
# Deliberately does not end in the wrapper's own closing: the frame is
# stripped as an exact suffix, so a body ending in "감사합니다." would make
# these assertions about the template rather than about the edit.
STAFF_ANSWER = "리모컨과 건전지 2개가 기본 구성품에 포함되어 있습니다."


class FakeAccount:
    account_code = ACCOUNT
    vendor_id = "A00123456"
    wing_id = "oje_ns_wing"
    access_key = "ak"
    secret_key = "sk"
    post_configured = True

    @staticmethod
    def missing_post_variables():
        return []


class RecordingClient:
    """Stands in for the HTTP client. Nothing leaves the process."""

    def __init__(self, *, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def reply(self, *, vendor_id, inquiry_id, content, reply_by):
        self.calls.append({
            "vendor_id": vendor_id, "inquiry_id": inquiry_id,
            "content": content, "reply_by": reply_by,
        })
        if self.error is not None:
            raise self.error
        return self.result or CoupangReplyResult(200, "200", "OK")


@pytest.fixture
def database(tmp_path) -> Database:
    built = Database(tmp_path / "phase3a.db")
    built.initialize()
    return built


def seed(database: Database, *, answered: bool = False,
         raw: dict | None = None) -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": COUPANG_STORE,
        "source_type": "COUPANG_ONLINE_INQUIRY",
        "source_question_id": EXTERNAL_ID,
        "external_inquiry_id": EXTERNAL_ID,
        "title": "Coupang 상품문의",
        "content": "기본 구성품에 리모컨이 포함되어 있나요?",
        "registered_at": "2026-10-20T10:00:00+09:00",
        "source_answered": 1 if answered else 0,
        "raw_json": raw if raw is not None else {
            "inquiryId": EXTERNAL_ID, "vendorItemId": "91454239547",
            "commentDtoList": [],
        },
        "source_metadata_json": {"account_code": ACCOUNT},
    }).inquiry_id


def draft_for(database: Database, inquiry_id: int, *,
              edited: str | None = None) -> dict:
    """A generated draft, optionally with the operator's own text saved."""

    answers = AnswerRepository(database)
    draft = answers.create_program_draft(
        inquiry_id,
        SimpleNamespace(
            answer=AI_ANSWER, status=SimpleNamespace(value="NEEDS_REVIEW"),
            category="PRODUCT_GENERAL", reason="test",
            provider="test", auto_answerable=False, needs_review=True,
            metadata={},
        ),
    )
    if edited is not None:
        answers.save_edited_answer(int(draft["id"]), edited)
    return answers.get(int(draft["id"]))


def service(database: Database, client=None) -> CoupangPostService:
    return CoupangPostService(
        database, client=client, account_resolver=lambda _code: FakeAccount(),
    )


# --- 1-2. generate and edit are open ------------------------------------

def test_an_unanswered_coupang_inquiry_accepts_a_draft(database) -> None:
    """Phase 2 opened generation; this pins that a draft can be stored."""

    inquiry_id = seed(database)
    draft = draft_for(database, inquiry_id)

    # Stored wrapped, as every answer this system keeps is: the body is the
    # claim and the frame is the company template.
    assert extract_answer_body(draft["original_answer"]) == AI_ANSWER
    assert not str(draft.get("edited_answer") or "").strip()
    assert AnswerRepository(database).active_for_inquiry(inquiry_id) is not None


def test_the_operators_edit_is_stored_beside_the_generated_answer(
    database,
) -> None:
    inquiry_id = seed(database)
    draft = draft_for(database, inquiry_id, edited=STAFF_ANSWER)

    assert extract_answer_body(draft["edited_answer"]) == STAFF_ANSWER
    # The generated text is kept, not overwritten: the two are different
    # records of what happened.
    assert extract_answer_body(draft["original_answer"]) == AI_ANSWER
    # The frame a Coupang customer would read names no other marketplace.
    assert "톡톡" not in str(draft["edited_answer"])
    assert "네이버" not in str(draft["edited_answer"])


# --- 3. the edit is what a registration would carry ---------------------

def test_the_stored_edit_is_the_payload_source(database) -> None:
    """``posting_answer(manual=True)`` prefers the staff edit.

    This is the single seam that decides whether the operator's own text or
    the model's first draft would reach Coupang.
    """

    inquiry_id = seed(database)
    draft = draft_for(database, inquiry_id, edited=STAFF_ANSWER)

    field, answer = AnswerRepository.posting_answer(draft, manual=True)
    assert field == "edited_answer"
    assert extract_answer_body(answer) == STAFF_ANSWER

    report = service(database).readiness(inquiry_id)
    assert report["answer_field"] == "edited_answer"
    assert report["answer_hash"] == hashlib.sha256(
        answer.encode("utf-8")).hexdigest()
    assert report["answer_chars"] == len(answer)


def test_without_an_edit_the_generated_answer_is_the_source(database) -> None:
    inquiry_id = seed(database)
    draft_for(database, inquiry_id)

    report = service(database).readiness(inquiry_id)
    assert report["answer_field"] == "original_answer"


# --- the readiness report: reports the permission, never grants it -------

def test_readiness_separates_a_ready_answer_from_a_closed_market(
    database,
) -> None:
    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)

    report = service(database).readiness(inquiry_id)

    assert report["answer_ready"] is True
    assert report["post_enabled"] is False
    assert report["post_disabled_reason"] == "MANUAL_POST_DISABLED"
    assert report["external_id"] == EXTERNAL_ID
    assert report["account_code"] == ACCOUNT
    assert report["vendor_id"] == FakeAccount.vendor_id
    assert EXTERNAL_ID in report["path"]
    assert FakeAccount.vendor_id in report["path"]
    assert report["network_call_count"] == 0
    # The WING id and the keys are never echoed to a screen.
    assert "wing_id" not in report and "secret_key" not in report


def test_readiness_sends_nothing_and_writes_nothing(database) -> None:
    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    client = RecordingClient()

    before = InquiryRepository(database).get(inquiry_id)
    service(database, client).readiness(inquiry_id)
    after = InquiryRepository(database).get(inquiry_id)

    assert client.calls == [], "readiness must not call Coupang"
    assert str(after.get("post_status") or "") == str(
        before.get("post_status") or "")
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM naver_post_attempts").fetchone()[0] == 0


def test_readiness_reports_a_missing_draft_rather_than_readiness(
    database,
) -> None:
    inquiry_id = seed(database)

    report = service(database).readiness(inquiry_id)
    assert report["answer_ready"] is False
    assert report["error_code"] == "LOCAL_STATE_MISSING"
    assert report["post_enabled"] is False


def test_readiness_cannot_authorise_a_post(database) -> None:
    """The permission is reported there and applied in ``post``.

    Asserted through ``post`` itself: a readiness call that said the answer
    was ready changes nothing about what ``post`` does.
    """

    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    client = RecordingClient()
    built = service(database, client)

    assert built.readiness(inquiry_id)["answer_ready"] is True
    result = built.post(inquiry_id, actor="관리자", confirmed=True)

    assert result.status == "BLOCKED"
    assert result.error_code == "MANUAL_POST_DISABLED"
    assert result.network_call_count == 0
    assert client.calls == []


# --- 4-5. the Phase 2 gates are untouched -------------------------------

def test_the_identity_gate_still_refuses_an_unconfirmed_model_question(
) -> None:
    from answer.exceptions import AutoAnswerProhibitedError
    from services.answer_service import (
        COUPANG_IDENTITY_REQUIRED_ACTIONS, AnswerService,
    )

    built = AnswerService.__new__(AnswerService)
    built.database = None
    built.product_knowledge = SimpleNamespace(catalog_repository=None)
    request = SimpleNamespace(
        store_code=COUPANG_STORE, product_name="샥스 SHAKS G1",
        question="해상도가 어떻게 되나요?",
        metadata={"market": "COUPANG", "mapping_resolution": "UNRESOLVED",
                  "gpt_understanding": {
                      "usable": True,
                      "questions": [{"text": "해상도", "action": "PRODUCT_SPEC"}]}},
    )
    assert "PRODUCT_SPEC" in COUPANG_IDENTITY_REQUIRED_ACTIONS
    with pytest.raises(AutoAnswerProhibitedError) as raised:
        built._require_confirmed_identity_for_model_question(request)
    assert raised.value.policy_reason == "COUPANG_MODEL_MAPPING_UNCONFIRMED"


def test_a_general_inquiry_is_not_gated() -> None:
    from services.answer_service import AnswerService

    built = AnswerService.__new__(AnswerService)
    request = SimpleNamespace(
        store_code=COUPANG_STORE, product_name="샥스 SHAKS G1",
        question="폐가전도 수거해주시나요?",
        metadata={"market": "COUPANG", "mapping_resolution": "UNRESOLVED",
                  "gpt_understanding": {
                      "usable": True,
                      "questions": [{"text": "폐가전", "action": "COLLECTION"}]}},
    )
    built._require_confirmed_identity_for_model_question(request)


# --- 6-7-8-9. nothing is opened by this step ----------------------------

def test_coupang_dps_stays_closed() -> None:
    assert is_store_dps_enabled(COUPANG_STORE) is False


def test_manual_post_permission_stays_off() -> None:
    assert COUPANG not in MANUAL_POST_MARKETS
    assert is_store_manual_post_enabled(COUPANG_STORE) is False


def test_automatic_post_permission_stays_off() -> None:
    assert COUPANG not in AUTOMATIC_POST_MARKETS
    assert is_store_automatic_post_enabled(COUPANG_STORE) is False


def test_no_coupang_request_is_made_anywhere_in_this_step(database) -> None:
    """Every entry point, one client, zero calls."""

    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    client = RecordingClient()
    built = service(database, client)

    built.readiness(inquiry_id)
    built.preflight(inquiry_id)
    built.post(inquiry_id, actor="관리자", confirmed=True)
    built.post(inquiry_id, actor="관리자", confirmed=False)

    assert client.calls == []


# --- 10. an answered inquiry is never posted over ------------------------

def test_an_inquiry_the_marketplace_answered_is_refused(database) -> None:
    inquiry_id = seed(database, answered=True)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)

    report = service(database).readiness(inquiry_id)
    assert report["answer_ready"] is False
    assert report["error_code"] == "ALREADY_ANSWERED"


def test_a_stored_seller_comment_is_refused_even_if_the_flag_lags(
    database,
) -> None:
    """``source_answered`` can trail the payload; the reply itself decides."""

    inquiry_id = seed(database, raw={
        "inquiryId": EXTERNAL_ID, "vendorItemId": "91454239547",
        "commentDtoList": [{
            "inquiryId": EXTERNAL_ID, "inquiryCommentId": "1",
            "inquiryCommentAt": "2026-10-20T11:00:00+09:00",
            "content": "안녕하세요 고객님. 리모컨은 포함되어 있습니다.",
        }],
    })
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)

    report = service(database).readiness(inquiry_id)
    assert report["answer_ready"] is False
    assert report["error_code"] == "SELLER_ANSWER_EXISTS"


# --- 11-14. success, failure and retry, through the real service --------

@pytest.fixture
def posting_opened(monkeypatch):
    """Open the manual permission for the engine tests only.

    The policy itself is asserted closed above. This exercises the code that
    runs once a later phase opens it, against a double, so the engine is
    verified without the permission being relaxed anywhere real.
    """

    import services.market_policy as policy
    import services.coupang_post_service as post_service

    monkeypatch.setattr(
        policy, "MANUAL_POST_MARKETS", frozenset({"NAVER", COUPANG}))
    monkeypatch.setattr(
        post_service, "is_store_manual_post_enabled", lambda _s: True)
    return True


def test_a_simulated_success_records_the_registration(
    database, posting_opened
) -> None:
    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    client = RecordingClient(result=CoupangReplyResult(200, "200", "OK"))

    result = service(database, client).post(
        inquiry_id, actor="관리자", confirmed=True)

    assert result.status == "POSTED"
    assert result.http_status == 200
    assert result.network_call_count == 1
    # The operator's text, to this inquiry, on this account -- and no other
    # marketplace named in the frame the customer would read.
    assert len(client.calls) == 1
    sent = client.calls[0]
    assert sent["vendor_id"] == FakeAccount.vendor_id
    assert sent["inquiry_id"] == EXTERNAL_ID
    assert sent["reply_by"] == FakeAccount.wing_id
    assert extract_answer_body(sent["content"]) == STAFF_ANSWER
    assert "톡톡" not in sent["content"] and "네이버" not in sent["content"]
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status, inquiry_id, final_answer_hash, http_status"
            " FROM naver_post_attempts WHERE id=?",
            (result.attempt_id,)).fetchone()
        # The attempt row's own vocabulary, which is the post_status the
        # repository records rather than a second spelling of success.
        assert row["status"] == "POSTED"
        assert row["inquiry_id"] == inquiry_id
        assert row["final_answer_hash"] == hashlib.sha256(
            sent["content"].encode("utf-8")).hexdigest()
        events = {
            r[0] for r in connection.execute(
                "SELECT event_code FROM activity_logs WHERE inquiry_id=?",
                (inquiry_id,)).fetchall()
        }
    assert "COUPANG_POST_SUCCEEDED" in events


def test_a_simulated_failure_records_the_reason(
    database, posting_opened
) -> None:
    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    client = RecordingClient(error=CoupangPostError(
        "REPLY_BY_INVALID", http_status=400,
        message="replyBy(WING ID)가 해당 vendorId와 일치하지 않습니다."))

    result = service(database, client).post(
        inquiry_id, actor="관리자", confirmed=True)

    assert result.status == "POST_FAILED"
    assert result.error_code == "REPLY_BY_INVALID"
    assert result.http_status == 400
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status, error_code FROM naver_post_attempts WHERE id=?",
            (result.attempt_id,)).fetchone()
        assert row["status"] == "POST_FAILED"
        assert row["error_code"] == "REPLY_BY_INVALID"
        events = {
            r[0] for r in connection.execute(
                "SELECT event_code FROM activity_logs WHERE inquiry_id=?",
                (inquiry_id,)).fetchall()
        }
    assert "COUPANG_POST_FAILED" in events


def test_a_network_failure_is_unknown_rather_than_failed(
    database, posting_opened
) -> None:
    """Coupang may or may not have taken it, so it is never auto-retried."""

    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    client = RecordingClient(error=CoupangPostError(
        "NETWORK_ERROR", message="timeout", retryable=False))

    result = service(database, client).post(
        inquiry_id, actor="관리자", confirmed=True)
    assert result.status == "POST_UNKNOWN"
    assert len(client.calls) == 1


def test_a_failed_registration_needs_an_explicit_retry(
    database, posting_opened
) -> None:
    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    failing = RecordingClient(error=CoupangPostError(
        "SERVER_ERROR", http_status=503, message="unavailable"))
    built = service(database, failing)
    assert built.post(inquiry_id, actor="관리자",
                      confirmed=True).status == "POST_FAILED"

    # A second plain click is refused; the operator has to ask for a retry.
    again = built.post(inquiry_id, actor="관리자", confirmed=True)
    assert again.error_code == "EXPLICIT_RETRY_REQUIRED"
    assert len(failing.calls) == 1

    retried = service(database, RecordingClient()).post(
        inquiry_id, actor="관리자", confirmed=True, retry_requested=True)
    assert retried.status == "POSTED"


def test_a_successful_registration_is_not_registered_twice(
    database, posting_opened
) -> None:
    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    client = RecordingClient()
    built = service(database, client)

    assert built.post(inquiry_id, actor="관리자",
                      confirmed=True).status == "POSTED"
    for kwargs in ({}, {"retry_requested": True}):
        repeat = built.post(inquiry_id, actor="관리자", confirmed=True, **kwargs)
        assert repeat.status == "BLOCKED", kwargs
        assert repeat.error_code in {"ALREADY_POSTED", "ALREADY_ANSWERED"}
    assert len(client.calls) == 1, "a repeated click sent a second reply"


def test_confirmation_is_required_before_anything_is_sent(
    database, posting_opened
) -> None:
    inquiry_id = seed(database)
    draft_for(database, inquiry_id, edited=STAFF_ANSWER)
    client = RecordingClient()

    result = service(database, client).post(
        inquiry_id, actor="관리자", confirmed=False)
    assert result.error_code == "CONFIRMATION_REQUIRED"
    assert client.calls == []


# --- 15. Naver is untouched ---------------------------------------------

def test_naver_posting_permissions_are_unchanged() -> None:
    assert "NAVER" in MANUAL_POST_MARKETS
    assert "NAVER" in AUTOMATIC_POST_MARKETS
    assert is_store_manual_post_enabled(NAVER_STORE) is True
    assert is_store_automatic_post_enabled(NAVER_STORE) is True


def test_the_coupang_service_refuses_a_naver_inquiry(database) -> None:
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": NAVER_STORE, "source_type": "PRODUCT_INQUIRY",
        "source_question_id": "n1", "title": "문의", "content": "질문",
        "registered_at": "2026-10-20T10:00:00+09:00", "raw_json": {},
    }).inquiry_id

    report = service(database).readiness(inquiry_id)
    assert report["error_code"] == "NOT_COUPANG"
    assert report["answer_ready"] is False
