from __future__ import annotations

import hashlib
from typing import Any

from answer.answer_format import format_final_answer
from repositories.database import Database
from repositories.inquiry_repository import utc_now


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_answer(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return "\n".join(line.rstrip() for line in text.split("\n"))


class PostReviewRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def finalize_auto(
        self, *, inquiry_id: int, draft_id: int, run_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = utc_now()
        with self.database.transaction() as connection:
            inquiry = connection.execute(
                "SELECT post_status, source_answered FROM inquiries WHERE id=?",
                (int(inquiry_id),),
            ).fetchone()
            draft = connection.execute(
                "SELECT * FROM answer_drafts WHERE id=? AND inquiry_id=?",
                (int(draft_id), int(inquiry_id)),
            ).fetchone()
            if inquiry is None or draft is None:
                raise LookupError("LOCAL_STATE_MISSING")
            if str(inquiry["post_status"] or "").upper() not in {"NOT_POSTED", "POST_FAILED"}:
                raise ValueError(f"POST_STATUS_{inquiry['post_status']}")
            if bool(inquiry["source_answered"]):
                raise ValueError("ALREADY_ANSWERED")
            existing_final = str(draft["final_answer"] or "").strip()
            final_answer = format_final_answer(
                existing_final or draft["original_answer"]
            )
            if not final_answer:
                raise ValueError("FINAL_ANSWER_REQUIRED")
            if not existing_final:
                connection.execute(
                    """
                    UPDATE answer_drafts
                    SET final_answer=?, review_status='AUTO_FINALIZED', updated_at=?
                    WHERE id=? AND posted=0
                      AND trim(COALESCE(final_answer,''))=''
                    """,
                    (final_answer, now, int(draft_id)),
                )
            version = connection.execute(
                """
                SELECT * FROM answer_versions
                WHERE inquiry_id=? AND version_kind='AUTO_POST_INITIAL'
                ORDER BY version_number LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
            if version is None:
                metadata = draft["metadata_json"] or "{}"
                import json
                try:
                    parsed = json.loads(metadata)
                except (TypeError, json.JSONDecodeError):
                    parsed = {}
                route = str(parsed.get("selected_answer_route") or parsed.get("generation_mode") or "")
                cursor = connection.execute(
                    """
                    INSERT INTO answer_versions(
                        inquiry_id, answer_draft_id, version_number, version_kind,
                        answer_body, actor, author_type, route, generation_mode,
                        finalization_source, approval_status, naver_status,
                        answer_hash, modified_at
                    ) VALUES (?, ?, 1, 'AUTO_POST_INITIAL', ?,
                        'SYSTEM_AUTO_POST', 'SYSTEM_AUTO_POST', ?, ?,
                        'AUTO_POST', 'AUTO_FINALIZED', 'PENDING', ?, ?)
                    """,
                    (int(inquiry_id), int(draft_id), final_answer, route,
                     str(parsed.get("generation_mode") or route), _hash(final_answer), now),
                )
                version_id = int(cursor.lastrowid)
            else:
                version_id = int(version["id"])
        return self.get_version(version_id) or {}, self.get_draft(draft_id) or {}

    def create_review_after_post(
        self, *, inquiry_id: int, draft_id: int, version_id: int,
        run_id: str, route: str, needs_staff_review: bool, posted_at: str | None,
    ) -> dict[str, Any]:
        priority = 100 if needs_staff_review else 10
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE answer_versions SET naver_status='POSTED',
                    posted_at=COALESCE(?, posted_at), modified_at=? WHERE id=?
                """,
                (posted_at, now, int(version_id)),
            )
            connection.execute(
                """
                INSERT INTO post_reviews(
                    inquiry_id, answer_draft_id, initial_version_id,
                    current_version_id, status, needs_staff_review,
                    route, priority, auto_post_run_id
                ) VALUES (?, ?, ?, ?, 'AUTO_POSTED_UNREVIEWED', ?, ?, ?, ?)
                ON CONFLICT(inquiry_id) DO UPDATE SET
                    answer_draft_id=excluded.answer_draft_id,
                    initial_version_id=COALESCE(post_reviews.initial_version_id, excluded.initial_version_id),
                    current_version_id=excluded.current_version_id,
                    status='AUTO_POSTED_UNREVIEWED',
                    needs_staff_review=excluded.needs_staff_review,
                    route=excluded.route, priority=excluded.priority,
                    auto_post_run_id=excluded.auto_post_run_id,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (int(inquiry_id), int(draft_id), int(version_id), int(version_id),
                 int(bool(needs_staff_review)), route, priority, run_id),
            )
        return self.get(inquiry_id) or {}

    def mark_post_failure(
        self, *, inquiry_id: int, draft_id: int, version_id: int,
        run_id: str, route: str, status: str, needs_staff_review: bool,
    ) -> dict[str, Any]:
        target = "POST_UNKNOWN" if str(status).upper() == "POST_UNKNOWN" else "POST_FAILED"
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE answer_versions SET naver_status=?, modified_at=? WHERE id=?",
                (target, utc_now(), int(version_id)),
            )
            connection.execute(
                """
                INSERT INTO post_reviews(
                    inquiry_id, answer_draft_id, initial_version_id,
                    current_version_id, status, needs_staff_review,
                    route, priority, auto_post_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(inquiry_id) DO UPDATE SET status=excluded.status,
                    priority=excluded.priority, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (int(inquiry_id), int(draft_id), int(version_id), int(version_id),
                 target, int(bool(needs_staff_review)), route,
                 1000 if target == "POST_UNKNOWN" else 500, run_id),
            )
        return self.get(inquiry_id) or {}

    def begin_correction(
        self, *, inquiry_id: int, answer: str, actor: str,
        answer_content_id: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        clean = format_final_answer(answer)
        if not clean:
            raise ValueError("FINAL_ANSWER_REQUIRED")
        now = utc_now()
        with self.database.transaction() as connection:
            review = connection.execute(
                "SELECT * FROM post_reviews WHERE inquiry_id=?",
                (int(inquiry_id),),
            ).fetchone()
            if review is None:
                raise LookupError("POST_REVIEW_NOT_FOUND")
            if str(review["status"]) == "POST_UNKNOWN":
                raise ValueError("POST_UNKNOWN_REQUIRES_MANUAL_VERIFICATION")
            latest = connection.execute(
                "SELECT * FROM answer_versions WHERE id=?",
                (int(review["current_version_id"]),),
            ).fetchone()
            if latest is None:
                raise LookupError("ANSWER_VERSION_NOT_FOUND")
            number = int(latest["version_number"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO answer_versions(
                    inquiry_id, answer_draft_id, version_number, version_kind,
                    answer_body, actor, author_type, route, generation_mode,
                    finalization_source, approval_status, naver_status,
                    previous_version_id, answer_hash, modified_at
                ) VALUES (?, ?, ?, 'STAFF_CORRECTION_DRAFT', ?, ?, 'STAFF',
                    ?, ?, 'STAFF_CORRECTION', 'NOT_APPLICABLE',
                    'CORRECTION_PENDING', ?, ?, ?)
                """,
                (int(inquiry_id), review["answer_draft_id"], number, clean,
                 str(actor or "관리자"), review["route"], latest["generation_mode"],
                 int(latest["id"]), _hash(clean), now),
            )
            version_id = int(cursor.lastrowid)
            correction = connection.execute(
                """
                INSERT INTO post_corrections(
                    inquiry_id, proposed_version_id, status, actor,
                    answer_content_id, started_at
                ) VALUES (?, ?, 'POSTING', ?, ?, ?)
                RETURNING *
                """,
                (int(inquiry_id), version_id, str(actor or "관리자"),
                 str(answer_content_id or "") or None, now),
            ).fetchone()
            connection.execute(
                """
                UPDATE post_reviews SET status='CORRECTION_PENDING',
                    current_version_id=?, reviewed_by=?, updated_at=?
                WHERE inquiry_id=?
                """,
                (version_id, str(actor or "관리자"), now, int(inquiry_id)),
            )
        return self.get_version(version_id) or {}, dict(correction)

    def complete_correction(
        self, *, correction_id: int, http_status: int,
        response_id: str | None, payload_hash: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        now = utc_now()
        with self.database.transaction() as connection:
            correction = connection.execute(
                "SELECT * FROM post_corrections WHERE id=? AND status='POSTING'",
                (int(correction_id),),
            ).fetchone()
            if correction is None:
                raise ValueError("CORRECTION_NOT_ACTIVE")
            proposed = connection.execute(
                "SELECT * FROM answer_versions WHERE id=?",
                (int(correction["proposed_version_id"]),),
            ).fetchone()
            number = int(proposed["version_number"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO answer_versions(
                    inquiry_id, answer_draft_id, version_number, version_kind,
                    answer_body, actor, author_type, route, generation_mode,
                    finalization_source, approval_status, naver_status,
                    previous_version_id, answer_hash, posted_at, modified_at
                ) VALUES (?, ?, ?, 'NAVER_CORRECTION_APPLIED', ?, ?, 'STAFF',
                    ?, ?, 'NAVER_CORRECTION_SUCCESS', 'NOT_APPLICABLE',
                    'POSTED', ?, ?, ?, ?)
                """,
                (proposed["inquiry_id"], proposed["answer_draft_id"], number,
                 proposed["answer_body"], proposed["actor"], proposed["route"],
                 proposed["generation_mode"], proposed["id"], proposed["answer_hash"], now, now),
            )
            applied_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE post_corrections SET status='SUCCEEDED',
                    applied_version_id=?, payload_hash=?, http_status=?,
                    response_id=?, completed_at=? WHERE id=?
                """,
                (applied_id, payload_hash, int(http_status),
                 str(response_id or "")[:200] or None, now, int(correction_id)),
            )
            connection.execute(
                """
                UPDATE post_reviews SET status='CORRECTED_AND_REPOSTED',
                    current_version_id=?, reviewed_at=?, priority=0, updated_at=?
                WHERE inquiry_id=?
                """,
                (applied_id, now, now, int(proposed["inquiry_id"])),
            )
            connection.execute(
                """
                UPDATE inquiries SET posted_answer_hash=?,
                    post_http_status=?, post_response_id=COALESCE(?,post_response_id),
                    updated_at=? WHERE id=?
                """,
                (proposed["answer_hash"], int(http_status),
                 str(response_id or "")[:200] or None, now, int(proposed["inquiry_id"])),
            )
        return self.get_version(applied_id) or {}, self.get(int(proposed["inquiry_id"])) or {}

    def fail_correction(
        self, *, correction_id: int, status: str, error_code: str,
        error_message: str, http_status: int | None = None,
    ) -> dict[str, Any]:
        target = "UNKNOWN" if str(status).upper() == "UNKNOWN" else "FAILED"
        review_status = "POST_UNKNOWN" if target == "UNKNOWN" else "CORRECTION_FAILED"
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT inquiry_id FROM post_corrections WHERE id=? AND status='POSTING'",
                (int(correction_id),),
            ).fetchone()
            if row is None:
                raise ValueError("CORRECTION_NOT_ACTIVE")
            connection.execute(
                """
                UPDATE post_corrections SET status=?, http_status=?, error_code=?,
                    error_message=?, completed_at=? WHERE id=?
                """,
                (target, http_status, str(error_code)[:100],
                 str(error_message)[:500], now, int(correction_id)),
            )
            connection.execute(
                "UPDATE post_reviews SET status=?, priority=?, updated_at=? WHERE inquiry_id=?",
                (review_status, 1000 if target == "UNKNOWN" else 500,
                 now, int(row["inquiry_id"])),
            )
        return self.get(int(row["inquiry_id"])) or {}

    def reviewed_no_change(self, *, inquiry_id: int, actor: str) -> dict[str, Any]:
        now = utc_now()
        with self.database.transaction() as connection:
            review = connection.execute(
                "SELECT * FROM post_reviews WHERE inquiry_id=?",
                (int(inquiry_id),),
            ).fetchone()
            if review is None:
                raise LookupError("POST_REVIEW_NOT_FOUND")
            current = connection.execute(
                "SELECT * FROM answer_versions WHERE id=?",
                (int(review["current_version_id"]),),
            ).fetchone()
            number = int(current["version_number"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO answer_versions(
                    inquiry_id, answer_draft_id, version_number, version_kind,
                    answer_body, actor, author_type, route, generation_mode,
                    finalization_source, approval_status, naver_status,
                    previous_version_id, answer_hash, posted_at, modified_at
                ) VALUES (?, ?, ?, 'REVIEWED_NO_CHANGE', ?, ?, 'STAFF', ?, ?,
                    'STAFF_REVIEW_NO_CHANGE', 'NOT_APPLICABLE', 'POSTED', ?, ?, ?, ?)
                """,
                (int(inquiry_id), review["answer_draft_id"], number,
                 current["answer_body"], str(actor or "관리자"), current["route"],
                 current["generation_mode"], current["id"], current["answer_hash"],
                 current["posted_at"], now),
            )
            version_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE post_reviews SET status='REVIEWED_NO_CHANGE',
                    current_version_id=?, reviewed_by=?, reviewed_at=?,
                    priority=0, updated_at=? WHERE inquiry_id=?
                """,
                (version_id, str(actor or "관리자"), now, now, int(inquiry_id)),
            )
        return self.get_version(version_id) or {}

    def capture_remote_naver_edit(
        self, *, inquiry_id: int, answer_body: str, actor: str = "NAVER_DIRECT_EDIT"
    ) -> tuple[dict[str, Any] | None, bool]:
        """Persist a seller-center edit detected by Auto Sync.

        Returns (version, changed).  A review/version history must already exist,
        which guarantees that the remote answer is being compared with a local
        answer previously posted by Q&A Auto.  Historical seller answers that
        predate Auto Post are intentionally handled by the legacy SELLER_ANSWER
        learning path instead.
        """
        clean = str(answer_body or "").strip()
        if not clean:
            return None, False
        now = utc_now()
        digest = _hash(clean)
        with self.database.transaction() as connection:
            review = connection.execute(
                "SELECT * FROM post_reviews WHERE inquiry_id=?",
                (int(inquiry_id),),
            ).fetchone()
            if review is None or review["current_version_id"] is None:
                return None, False
            current = connection.execute(
                "SELECT * FROM answer_versions WHERE id=?",
                (int(review["current_version_id"]),),
            ).fetchone()
            if current is None:
                return None, False
            if str(current["naver_status"] or "").upper() != "POSTED":
                return dict(current), False
            if (
                str(current["answer_hash"] or "") == digest
                or _canonical_answer(current["answer_body"]) == _canonical_answer(clean)
            ):
                return dict(current), False
            number = int(current["version_number"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO answer_versions(
                    inquiry_id, answer_draft_id, version_number, version_kind,
                    answer_body, actor, author_type, route, generation_mode,
                    finalization_source, approval_status, naver_status,
                    previous_version_id, answer_hash, posted_at, modified_at
                ) VALUES (?, ?, ?, 'NAVER_CORRECTION_APPLIED', ?, ?, 'STAFF',
                    ?, ?, 'NAVER_DIRECT_EDIT_SYNC', 'NOT_APPLICABLE',
                    'POSTED', ?, ?, ?, ?)
                """,
                (
                    int(inquiry_id), current["answer_draft_id"], number, clean,
                    str(actor or "NAVER_DIRECT_EDIT"), current["route"],
                    current["generation_mode"], int(current["id"]), digest,
                    current["posted_at"] or now, now,
                ),
            )
            version_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE post_reviews
                SET status='CORRECTED_AND_REPOSTED', current_version_id=?,
                    reviewed_by=?, reviewed_at=?, priority=0, updated_at=?
                WHERE inquiry_id=?
                """,
                (version_id, str(actor or "NAVER_DIRECT_EDIT"), now, now, int(inquiry_id)),
            )
            connection.execute(
                """
                UPDATE inquiries
                SET posted_answer_hash=?, updated_at=?
                WHERE id=?
                """,
                (digest, now, int(inquiry_id)),
            )
        return self.get_version(version_id), True

    def mark_learning_saved(self, version_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE answer_versions SET learning_saved=1 WHERE id=?",
                (int(version_id),),
            )

    def get(self, inquiry_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM post_reviews WHERE inquiry_id=?",
                (int(inquiry_id),),
            ).fetchone()
        return self._row(row)

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM answer_versions WHERE id=?",
                (int(version_id),),
            ).fetchone()
        return self._row(row)

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM answer_drafts WHERE id=?",
                (int(draft_id),),
            ).fetchone()
        return self._row(row)

    def versions(self, inquiry_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM answer_versions WHERE inquiry_id=? ORDER BY version_number",
                (int(inquiry_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM post_reviews GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "pending_review": counts.get("AUTO_POSTED_UNREVIEWED", 0)
                + counts.get("CORRECTION_REQUIRED", 0),
            "corrected": counts.get("CORRECTED_AND_REPOSTED", 0),
            "post_unknown": counts.get("POST_UNKNOWN", 0),
            "counts": counts,
        }
