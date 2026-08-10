from __future__ import annotations

from typing import Any

from repositories.auto_post_repository import AutoPostRepository
from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.database import Database
from repositories.learning_repository import LearningRepository
from repositories.naver_sync_repository import NaverSyncRepository


TODAY_SQL = "date({column}, '+9 hours') = date('now', '+9 hours')"


class DashboardOperationsService:
    """Read-only operational projection for the production Dashboard."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def snapshot(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            scalar = lambda sql, parameters=(): int(
                connection.execute(sql, parameters).fetchone()[0] or 0
            )
            today_inquiries = scalar(
                f"SELECT COUNT(*) FROM inquiries WHERE {TODAY_SQL.format(column='created_at')}"
            )
            auto_answers = scalar(
                f"""
                SELECT COUNT(DISTINCT inquiry_id) FROM activity_logs
                WHERE event_code='AUTO_ANSWER_SUCCEEDED'
                  AND {TODAY_SQL.format(column='created_at')}
                """
            )
            auto_posted = scalar(
                f"""
                SELECT COUNT(*) FROM naver_post_attempts
                WHERE status='POSTED' AND auto_post_run_id IS NOT NULL
                  AND {TODAY_SQL.format(column='completed_at')}
                """
            )
            auto_failed = scalar(
                f"""
                SELECT COUNT(*) FROM naver_post_attempts
                WHERE status='POST_FAILED' AND auto_post_run_id IS NOT NULL
                  AND {TODAY_SQL.format(column='completed_at')}
                """
            )
            staff_corrections = scalar(
                f"""
                SELECT COUNT(*) FROM answer_versions
                WHERE version_kind IN (
                    'STAFF_CORRECTION_DRAFT','NAVER_CORRECTION_APPLIED',
                    'REVIEWED_NO_CHANGE'
                ) AND {TODAY_SQL.format(column='created_at')}
                """
            )
            learning_today = scalar(
                f"SELECT COUNT(*) FROM learning_examples WHERE {TODAY_SQL.format(column='created_at')}"
            )
            learning_used_today = scalar(
                f"""
                SELECT COUNT(*) FROM learning_examples
                WHERE usage_count > 0 AND {TODAY_SQL.format(column='last_used_at')}
                """
            )
            pending = scalar(
                """
                SELECT COUNT(*) FROM inquiries
                WHERE COALESCE(source_answered,0)=0
                  AND post_status IN ('NOT_POSTED','POST_FAILED')
                """
            )
            existing_pending = scalar(
                """
                SELECT COUNT(*) FROM inquiries i
                WHERE COALESCE(i.source_answered,0)=0
                  AND i.post_status IN ('NOT_POSTED','POST_FAILED')
                  AND NOT EXISTS (
                      SELECT 1 FROM auto_sync_events e WHERE e.inquiry_id=i.id
                  )
                """
            )
            recent_error = connection.execute(
                """
                SELECT event_code, message, created_at FROM activity_logs
                WHERE level IN ('ERROR','CRITICAL')
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            recent_post = connection.execute(
                """
                SELECT inquiry_id, completed_at FROM naver_post_attempts
                WHERE status='POSTED'
                ORDER BY completed_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            latest_event = connection.execute(
                """
                SELECT event_code, details_json, created_at FROM activity_logs
                WHERE inquiry_id IS NULL AND event_code IN (
                    'AUTO_POST_RUN_STARTED','AUTO_POST_TRIGGER_FAILED',
                    'NAVER_AUTO_SYNC_COMPLETED','NAVER_AUTO_SYNC_FAILED'
                ) ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            quality_rows = connection.execute(
                """
                SELECT rating, COUNT(*) AS count FROM learning_examples
                WHERE active=1 GROUP BY rating ORDER BY rating DESC
                """
            ).fetchall()
            recent_learning = connection.execute(
                """
                SELECT learning_source, created_at, updated_at
                FROM learning_examples
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ).fetchone()

        learning = LearningRepository(self.database).manager_summary()
        sync = NaverSyncRepository(self.database)
        post = AutoPostRepository(self.database)
        event_summary = AutoPostEventRepository(self.database).summary()
        automatic_waiting = (
            event_summary["PENDING"]
            + event_summary["PROCESSING"]
            + event_summary["RETRY_BY_SCHEDULER"]
        )
        manual_waiting = existing_pending + event_summary["BLOCKED_AUTO_POST_OFF"]
        return {
            "today_inquiries": today_inquiries,
            "auto_answers": auto_answers,
            "auto_posted": auto_posted,
            "auto_failed": auto_failed,
            "staff_corrections": staff_corrections,
            "learning_today": learning_today,
            "learning_used_today": learning_used_today,
            "pending": pending,
            "existing_pending": existing_pending,
            "new_pending": max(0, pending - existing_pending),
            "automatic_waiting": automatic_waiting,
            "manual_waiting": manual_waiting,
            "event_summary": event_summary,
            "recent_error": dict(recent_error) if recent_error else None,
            "recent_post": dict(recent_post) if recent_post else None,
            "latest_event": dict(latest_event) if latest_event else None,
            "learning": {
                **learning,
                "today": learning_today,
                "used_today": learning_used_today,
                "quality_distribution": {
                    int(row["rating"]): int(row["count"])
                    for row in quality_rows
                },
                "latest": dict(recent_learning) if recent_learning else None,
            },
            "auto_sync_settings": sync.auto_settings(),
            "auto_sync_state": sync.auto_state(),
            "auto_post_settings": post.settings(),
            "auto_post_state": post.state(),
        }
