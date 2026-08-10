from __future__ import annotations

import hashlib
from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import utc_now


class NaverPostStateError(RuntimeError):
    pass


class NaverPostAlreadyAnsweredError(NaverPostStateError):
    pass


NON_RETRYABLE_TARGET_ERRORS = frozenset(
    {
        "TARGET_ID_MAPPING_ERROR",
        "TARGET_NOT_FOUND",
        "STORE_CREDENTIAL_MISMATCH",
        "INQUIRY_TYPE_ENDPOINT_MISMATCH",
        "REMOTE_TARGET_NOT_FOUND",
        "MASKED_EXTERNAL_ID",
    }
)


class NaverPostRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def latest(self, inquiry_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM naver_post_attempts
                WHERE inquiry_id=?
                ORDER BY started_at DESC, id DESC LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
        return self._row(row)

    def prepare_retry(self, inquiry_id: int) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE inquiries
                SET post_status='NOT_POSTED', post_error_code=NULL,
                    post_error_message=NULL, updated_at=?
                WHERE id=? AND post_status='POST_FAILED'
                """,
                (utc_now(), int(inquiry_id)),
            )
        return cursor.rowcount == 1

    def acquire(
        self,
        *,
        inquiry_id: int,
        draft_id: int,
        idempotency_key: str,
        external_id: str,
        store_code: str,
        source_type: str,
        method: str,
        endpoint_kind: str,
        final_answer_hash: str,
        payload_hash: str,
        actor: str,
        allow_unapproved: bool = False,
        auto_post_run_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            inquiry = connection.execute(
                """
                SELECT id, approval_status, post_status, source_answered,
                       store_code, source_type,
                       COALESCE(external_inquiry_id, source_question_id)
                           AS external_id
                FROM inquiries WHERE id=?
                """,
                (int(inquiry_id),),
            ).fetchone()
            draft = connection.execute(
                """
                SELECT id, inquiry_id, final_answer, posted, is_active
                FROM answer_drafts WHERE id=?
                """,
                (int(draft_id),),
            ).fetchone()
            if inquiry is None:
                raise LookupError(f"Inquiry not found: {inquiry_id}")
            if draft is None or int(draft["inquiry_id"]) != int(inquiry_id):
                raise LookupError(f"Draft not found: {draft_id}")
            if (
                not allow_unapproved
                and str(inquiry["approval_status"]).upper() != "APPROVED"
            ):
                raise NaverPostStateError("APPROVAL_REQUIRED")
            status = str(inquiry["post_status"] or "NOT_POSTED").upper()
            if status not in {"NOT_POSTED", "POST_FAILED"}:
                raise NaverPostStateError(f"POST_STATUS_{status}")
            if bool(inquiry["source_answered"]):
                raise NaverPostAlreadyAnsweredError("ALREADY_ANSWERED")
            if bool(draft["posted"]):
                raise NaverPostStateError("ALREADY_POSTED")
            if not str(draft["final_answer"] or "").strip():
                raise NaverPostStateError("FINAL_ANSWER_REQUIRED")
            current_answer = (
                str(draft["final_answer"])
                .replace("\r\n", "\n")
                .replace("\r", "\n")
                .strip()
            )
            current_hash = hashlib.sha256(
                current_answer.encode("utf-8")
            ).hexdigest()
            if current_hash != final_answer_hash:
                raise NaverPostStateError("FINAL_ANSWER_CHANGED")
            duplicate = connection.execute(
                """
                SELECT id, post_status FROM inquiries
                WHERE id<>? AND store_code=?
                  AND COALESCE(external_inquiry_id, source_question_id)=?
                  AND post_status IN ('POSTING','POSTED','POST_UNKNOWN')
                LIMIT 1
                """,
                (int(inquiry_id), store_code, external_id),
            ).fetchone()
            if duplicate is not None:
                raise NaverPostStateError(
                    f"DUPLICATE_EXTERNAL_{duplicate['post_status']}"
                )
            previous_failed = connection.execute(
                """
                SELECT id FROM naver_post_attempts
                WHERE inquiry_id=? AND status='POST_FAILED'
                ORDER BY started_at DESC, id DESC LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
            retry_of_attempt_id = (
                int(previous_failed["id"])
                if previous_failed is not None
                else None
            )
            connection.execute(
                """
                UPDATE inquiries
                SET post_status='POSTING', post_attempted_at=?,
                    post_error_code=NULL, post_error_message=NULL,
                    post_http_status=NULL, updated_at=?
                WHERE id=?
                """,
                (now, now, int(inquiry_id)),
            )
            cursor = connection.execute(
                """
                INSERT INTO naver_post_attempts(
                    inquiry_id, answer_draft_id, idempotency_key,
                    external_id, store_code, source_type, method,
                    endpoint_kind, status, final_answer_hash, payload_hash,
                    actor, started_at, auto_post_run_id, retry_of_attempt_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'POSTING', ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(inquiry_id),
                    int(draft_id),
                    idempotency_key,
                    external_id,
                    store_code,
                    source_type,
                    method,
                    endpoint_kind,
                    final_answer_hash,
                    payload_hash,
                    actor,
                    now,
                    str(auto_post_run_id or "") or None,
                    retry_of_attempt_id,
                ),
            )
            attempt_id = int(cursor.lastrowid)
        value = self.get_attempt(attempt_id)
        assert value is not None
        return value

    def get_attempt(self, attempt_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM naver_post_attempts WHERE id=?",
                (int(attempt_id),),
            ).fetchone()
        return self._row(row)

    def succeed(
        self,
        *,
        attempt_id: int,
        inquiry_id: int,
        draft_id: int,
        http_status: int,
        response_id: str | None,
        final_answer_hash: str,
        actor: str,
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE naver_post_attempts
                SET status='POSTED', http_status=?, response_id=?,
                    completed_at=?
                WHERE id=? AND inquiry_id=? AND status='POSTING'
                """,
                (
                    int(http_status),
                    str(response_id or "")[:200] or None,
                    now,
                    int(attempt_id),
                    int(inquiry_id),
                ),
            )
            if cursor.rowcount != 1:
                raise NaverPostStateError("POST_ATTEMPT_NOT_ACTIVE")
            connection.execute(
                """
                UPDATE inquiries
                SET post_status='POSTED', posted_at=?,
                    post_http_status=?, post_response_id=?,
                    posted_answer_hash=?, posted_draft_id=?, post_actor=?,
                    post_error_code=NULL, post_error_message=NULL,
                    workflow_status='POSTED', answer_status='ANSWERED',
                    updated_at=?
                WHERE id=? AND post_status='POSTING'
                """,
                (
                    now,
                    int(http_status),
                    str(response_id or "")[:200] or None,
                    final_answer_hash,
                    int(draft_id),
                    actor,
                    now,
                    int(inquiry_id),
                ),
            )
            connection.execute(
                """
                UPDATE answer_drafts
                SET posted=1, posted_at=?, updated_at=?
                WHERE id=? AND inquiry_id=?
                """,
                (now, now, int(draft_id), int(inquiry_id)),
            )

    def fail(
        self,
        *,
        attempt_id: int,
        inquiry_id: int,
        status: str,
        error_code: str,
        error_message: str,
        http_status: int | None = None,
    ) -> None:
        target = str(status).upper()
        if target not in {"POST_FAILED", "POST_UNKNOWN"}:
            raise ValueError(f"Invalid post failure state: {status}")
        now = utc_now()
        safe_code = str(error_code or "UNKNOWN_ERROR")[:100]
        safe_message = str(error_message or "")[:500]
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_post_attempts
                SET status=?, http_status=?, error_code=?, error_message=?,
                    completed_at=?
                WHERE id=? AND inquiry_id=? AND status='POSTING'
                """,
                (
                    target,
                    http_status,
                    safe_code,
                    safe_message,
                    now,
                    int(attempt_id),
                    int(inquiry_id),
                ),
            )
            connection.execute(
                """
                UPDATE inquiries
                SET post_status=?, post_http_status=?,
                    post_error_code=?, post_error_message=?, updated_at=?
                WHERE id=? AND post_status='POSTING'
                """,
                (
                    target,
                    http_status,
                    safe_code,
                    safe_message,
                    now,
                    int(inquiry_id),
                ),
            )

    def mark_already_answered(
        self,
        *,
        attempt_id: int,
        inquiry_id: int,
        http_status: int | None,
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE naver_post_attempts
                SET status='ALREADY_ANSWERED', http_status=?,
                    error_code='ALREADY_ANSWERED', completed_at=?
                WHERE id=? AND inquiry_id=? AND status='POSTING'
                """,
                (http_status, now, int(attempt_id), int(inquiry_id)),
            )
            connection.execute(
                """
                UPDATE inquiries
                SET post_status='POSTED', source_answered=1,
                    post_http_status=?, post_error_code='ALREADY_ANSWERED',
                    post_error_message='Naver already has an answer.',
                    updated_at=?
                WHERE id=? AND post_status='POSTING'
                """,
                (http_status, now, int(inquiry_id)),
            )
