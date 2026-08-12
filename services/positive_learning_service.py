from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from config import PositiveLearningSettings
from repositories.database import Database
from repositories.learning_repository import deserialize_json
from repositories.post_review_repository import _canonical_answer
from services.learning_service import LearningService


def _time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class PositiveLearningService:
    """Accept an unchanged Auto Post only after remote observation proves it."""

    def __init__(
        self, database: Database, *, settings: PositiveLearningSettings | None = None
    ) -> None:
        self.database = database
        self.settings = settings or PositiveLearningSettings.from_environment()
        self.learning = LearningService(database)

    def observe(
        self, *, inquiry_id: int, seller_answer: str, observed_at: datetime | None = None
    ) -> dict[str, Any]:
        now = (observed_at or datetime.now(UTC)).astimezone(UTC)
        answer = str(seller_answer or "").strip()
        if not answer:
            return {"saved": False, "reason": "SELLER_ANSWER_MISSING"}
        with self.database.connection() as connection:
            inquiry = connection.execute(
                "SELECT post_status FROM inquiries WHERE id=?", (int(inquiry_id),)
            ).fetchone()
            if inquiry is None or str(inquiry["post_status"] or "").upper() != "POSTED":
                return {"saved": False, "reason": "INQUIRY_POST_NOT_CONFIRMED"}
            review = connection.execute(
                "SELECT * FROM post_reviews WHERE inquiry_id=?", (int(inquiry_id),)
            ).fetchone()
            if review is None:
                return {"saved": False, "reason": "AUTO_POST_HISTORY_MISSING"}
            review = dict(review)
            if bool(review.get("needs_staff_review")):
                return {"saved": False, "reason": "STAFF_REVIEW_REQUIRED"}
            route = str(review.get("route") or "").upper()
            if any(marker in route for marker in ("STAFF", "REVIEW", "RISK", "MANUAL")):
                return {"saved": False, "reason": "UNSAFE_ROUTE"}
            version = connection.execute(
                "SELECT * FROM answer_versions WHERE id=?",
                (int(review["current_version_id"]),),
            ).fetchone()
            if version is None:
                return {"saved": False, "reason": "ANSWER_VERSION_MISSING"}
            version = dict(version)
            if str(version.get("version_kind") or "").upper() != "AUTO_POST_INITIAL":
                return {"saved": False, "reason": "ANSWER_ALREADY_REVIEWED_OR_CORRECTED"}
            if str(version.get("naver_status") or "").upper() != "POSTED":
                return {"saved": False, "reason": "REMOTE_POST_NOT_CONFIRMED"}
            posted_at = _time(version.get("posted_at"))
            if posted_at is None or now < posted_at + timedelta(days=self.settings.observation_days):
                return {"saved": False, "reason": "OBSERVATION_PERIOD_NOT_ELAPSED"}
            if _canonical_answer(version.get("answer_body")) != _canonical_answer(answer):
                return {"saved": False, "reason": "REMOTE_ANSWER_CHANGED"}
            attempts = [dict(row) for row in connection.execute(
                "SELECT * FROM naver_post_attempts WHERE inquiry_id=? ORDER BY id",
                (int(inquiry_id),),
            ).fetchall()]
            posted = [row for row in attempts if str(row.get("status") or "").upper() == "POSTED"]
            if not posted:
                return {"saved": False, "reason": "POST_ATTEMPT_NOT_CONFIRMED"}
            latest_success = posted[-1]
            if any(
                int(row["id"]) > int(latest_success["id"])
                and str(row.get("status") or "").upper() in {"POST_FAILED", "POST_UNKNOWN", "POSTING"}
                for row in attempts
            ):
                return {"saved": False, "reason": "LATER_POST_FAILURE_OR_UNKNOWN"}
            draft = connection.execute(
                "SELECT * FROM answer_drafts WHERE id=?", (int(version["answer_draft_id"]),)
            ).fetchone()
            if draft is None:
                return {"saved": False, "reason": "ANSWER_DRAFT_MISSING"}
            draft = dict(draft)
            validator = str(draft.get("validation_status") or "").upper()
            if not validator or validator.startswith("FAIL") or "REVIEW" in validator:
                return {"saved": False, "reason": "VALIDATOR_NOT_SAFE"}
            metadata = deserialize_json(draft.get("metadata_json")) or {}
            plan = metadata.get("processing_plan") if isinstance(metadata, dict) else {}
            selected_route = str((plan or {}).get("selected_answer_route") or "").upper()
            if any(marker in selected_route for marker in ("STAFF", "REVIEW", "RISK", "MANUAL")):
                return {"saved": False, "reason": "PROCESSING_PLAN_REQUIRES_REVIEW"}
            corrected = connection.execute(
                """
                SELECT 1 FROM learning_examples
                WHERE inquiry_id=? AND learning_source='AUTO_POST_CORRECTED'
                LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
            if corrected is not None:
                return {"saved": False, "reason": "CORRECTED_LEARNING_EXISTS"}
            negative = connection.execute(
                """
                SELECT 1 FROM learning_feedback
                WHERE inquiry_id=? AND active=1
                  AND learning_signal_type IN ('NEGATIVE','INTENT_CORRECTION')
                LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
            if negative is not None:
                return {"saved": False, "reason": "NEGATIVE_FEEDBACK_EXISTS"}
        saved = self.learning.capture_auto_unchanged_accepted(
            inquiry_id=int(inquiry_id),
            version_id=int(version["id"]),
            post_attempt_id=int(latest_success["id"]),
            observed_answer=answer,
            observed_at=now.isoformat(timespec="milliseconds"),
            observation_days=self.settings.observation_days,
        )
        return {
            "saved": saved is not None,
            "reason": "SAVED" if saved is not None else "PRIVACY_OR_QUALITY_REJECTED",
            "learning": saved,
        }
