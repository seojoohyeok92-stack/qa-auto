from __future__ import annotations

import json
from typing import Any

from repositories.auto_post_repository import AutoPostRepository
from repositories.auto_post_event_repository import AutoPostEventRepository
from repositories.database import Database
from repositories.learning_repository import LearningRepository
from repositories.naver_sync_repository import NaverSyncRepository


TODAY_SQL = "date({column}, '+9 hours') = date('now', '+9 hours')"

REVIEW_EVENT_CODES = (
    "AUTO_PROCESSING_REVIEW_REQUIRED",
    "AUTO_PROCESSING_BLOCKED",
    "AUTO_POST_BLOCKED_DPS_SESSION",
)

SOFT_WARNING_EVENT_CODE = "AUTO_PROCESSING_SOFT_WARNING"

# Maps the machine-readable reason codes produced by
# AutoProcessingEligibilityService / AutoPostPipelineService's DPS-session
# guard to the operator-facing buckets requested for Dashboard diagnostics.
# Display-only: never used to change routing/eligibility decisions.
_REASON_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("주문번호 필요/조회 실패", (
        "REQUIRED_ORDER_ID_MISSING_OR_INVALID", "ORDER_LOOKUP_NOT_TRUSTED",
        "ROUTE_ORDER_ID_REQUEST", "ROUTE_ORDER_LOOKUP_FAILED",
        "ROUTE_DELIVERY_ORDER_NOT_FOUND",
    )),
    ("DPS 확인 필요", (
        "DPS_RESULT_NOT_TRUSTED", "DPS_SNAPSHOT_NOT_VALIDATED",
        "ROUTE_DPS_LOOKUP_FAILED", "ROUTE_DELIVERY_DATE_UNCONFIRMED",
        "LOGIN_REQUIRED", "OTP_REQUIRED", "CHROME_NOT_FOUND",
        "DPS_TAB_NOT_FOUND", "DPS_PAGE_NOT_FOUND", "CONNECTION_FAILED",
        "AGENT_CONNECTION_FAILED", "AGENT_CONNECT_TIMEOUT",
        "AGENT_REQUEST_FAILED", "AGENT_START_FAILED", "AGENT_START_TIMEOUT",
    )),
    ("Validator 경고", ("VALIDATOR_NOT_PASS", "ROUTE_REVIEW_REQUIRED_SAFE_DRAFT")),
    ("정책/고위험 검토", (
        "ANSWER_REQUIRES_MANUAL_REVIEW", "PROCESSING_PLAN_REQUIRES_REVIEW",
        "POLICY_OR_HIGH_RISK_REVIEW", "DRAFT_REVIEW_REQUIRED",
        "ROUTE_BLOCKED_REVIEW_REQUIRED",
    )),
    ("Evidence 부족(제품 사실 미검증)", ("PRODUCT_FACT_NOT_VERIFIED",)),
    ("제품 호환성 근거 부족", ("PRODUCT_COMPATIBILITY_NOT_VERIFIED",)),
    ("낮은 신뢰도", (
        "INTENT_CONFIDENCE_LOW", "INTENT_CONFIDENCE_UNKNOWN",
        "GPT_CONFIDENCE_LOW", "GPT_CONFIDENCE_UNKNOWN",
    )),
    ("자동등록 불가 라우트", ("INTENT_NOT_AUTO_POSTABLE",)),
    ("개인정보/보안 차단", ("PII_EXPOSURE", "SECRET_EXPOSURE")),
    ("답변 무결성 오류", (
        "FINAL_ANSWER_REQUIRED", "UNRESOLVED_PLACEHOLDER",
    )),
    ("이미 답변됨", ("ALREADY_ANSWERED_OR_POSTED",)),
    ("등록 직전 preflight 실패", ("PREFLIGHT_FAILED",)),
    ("주문번호 요청 답변(자동등록)", ("ORDER_ID_REQUESTED_FROM_CUSTOMER",)),
)
_REASON_TO_BUCKET = {
    code: bucket for bucket, codes in _REASON_BUCKETS for code in codes
}


def _reason_bucket(reason_code: str) -> str:
    """Map a raw reason code to an operator-facing bucket.

    Unknown codes deliberately fall through to the raw code itself rather
    than a generic "기타", so an operator never sees an unexplainable
    bucket for a reason the system actually recorded.
    """
    code = str(reason_code or "").upper()
    if code in _REASON_TO_BUCKET:
        return _REASON_TO_BUCKET[code]
    if code.startswith("ROUTE_"):
        return "자동등록 불가 라우트"
    return code or "기타"


def _parse_reasons(details_json: object) -> list[str]:
    if isinstance(details_json, dict):
        data = details_json
    else:
        try:
            data = json.loads(str(details_json or "{}"))
        except (TypeError, ValueError):
            return []
    reasons = data.get("reasons") if isinstance(data, dict) else None
    if isinstance(reasons, list):
        return [str(item) for item in reasons]
    soft = data.get("soft_reasons") if isinstance(data, dict) else None
    if isinstance(soft, list):
        return [str(item) for item in soft]
    safe_error_code = data.get("safe_error_code") if isinstance(data, dict) else None
    return [str(safe_error_code)] if safe_error_code else []


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
            review_required = scalar(
                """
                SELECT COUNT(*) FROM inquiries
                WHERE COALESCE(source_answered,0)=0
                  AND post_status != 'POSTED'
                  AND workflow_status IN ('REVIEW_PENDING','NEEDS_ATTENTION')
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
            recent_auto_process = connection.execute(
                """
                SELECT inquiry_id, event_code, created_at FROM activity_logs
                WHERE event_code IN (
                    'AUTO_ANSWER_SUCCEEDED',
                    'AUTO_PROCESSING_REVIEW_REQUIRED',
                    'AUTO_PROCESSING_BLOCKED'
                )
                ORDER BY created_at DESC, id DESC LIMIT 1
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
            "review_required": review_required,
            "existing_pending": existing_pending,
            "new_pending": max(0, pending - existing_pending),
            "automatic_waiting": automatic_waiting,
            "manual_waiting": manual_waiting,
            "event_summary": event_summary,
            "recent_error": dict(recent_error) if recent_error else None,
            "recent_post": dict(recent_post) if recent_post else None,
            "recent_auto_process": (
                dict(recent_auto_process) if recent_auto_process else None
            ),
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

    def queue_diagnostics(self, *, recent_limit: int = 20) -> dict[str, Any]:
        """Read-only breakdown of the auto-post queue for operator diagnosis.

        Distinguishes claimable queue state (``auto_sync_events``) from the
        ``inquiries``-table-derived Pending/Review-Required KPIs in
        :meth:`snapshot`, and surfaces *why* each currently review-required
        inquiry was held, without changing any routing/eligibility decision.
        """

        event_summary = AutoPostEventRepository(self.database).summary()
        with self.database.connection() as connection:
            review_rows = connection.execute(
                """
                SELECT a.details_json
                FROM activity_logs a
                JOIN inquiries i ON i.id = a.inquiry_id
                WHERE a.event_code IN ({codes})
                  AND a.id = (
                      SELECT MAX(a2.id) FROM activity_logs a2
                      WHERE a2.inquiry_id = a.inquiry_id
                        AND a2.event_code IN ({codes})
                  )
                  AND i.workflow_status IN ('REVIEW_PENDING', 'NEEDS_ATTENTION')
                  AND COALESCE(i.source_answered, 0) = 0
                """.format(
                    codes=",".join("?" for _ in REVIEW_EVENT_CODES)
                ),
                REVIEW_EVENT_CODES * 2,
            ).fetchall()
            dps_required = connection.execute(
                """
                SELECT COUNT(*) FROM inquiries
                WHERE COALESCE(source_answered, 0) = 0
                  AND post_status != 'POSTED'
                  AND workflow_status IN ('REVIEW_PENDING', 'NEEDS_ATTENTION')
                  AND id IN (
                      SELECT inquiry_id FROM activity_logs
                      WHERE event_code = 'AUTO_POST_BLOCKED_DPS_SESSION'
                  )
                """
            ).fetchone()[0]
            recent_rows = connection.execute(
                """
                SELECT e.id, e.inquiry_id, e.external_id, e.status AS queue_status,
                       e.updated_at, e.attempt_count, e.last_error_code,
                       i.workflow_status, i.post_status,
                       (SELECT p.status FROM naver_post_attempts p
                         WHERE p.inquiry_id = e.inquiry_id
                         ORDER BY p.id DESC LIMIT 1) AS last_post_status,
                       (SELECT a.details_json FROM activity_logs a
                         WHERE a.inquiry_id = e.inquiry_id
                           AND a.event_code IN ({codes})
                         ORDER BY a.id DESC LIMIT 1) AS review_details_json,
                       (SELECT a.details_json FROM activity_logs a
                         WHERE a.inquiry_id = e.inquiry_id
                           AND a.event_code = ?
                         ORDER BY a.id DESC LIMIT 1) AS soft_details_json
                FROM auto_sync_events e
                JOIN inquiries i ON i.id = e.inquiry_id
                ORDER BY e.updated_at DESC, e.id DESC
                LIMIT ?
                """.format(codes=",".join("?" for _ in REVIEW_EVENT_CODES)),
                (
                    *REVIEW_EVENT_CODES, SOFT_WARNING_EVENT_CODE,
                    int(recent_limit),
                ),
            ).fetchall()

        reason_counts: dict[str, int] = {}
        for row in review_rows:
            for reason in _parse_reasons(row["details_json"]):
                bucket = _reason_bucket(reason)
                reason_counts[bucket] = reason_counts.get(bucket, 0) + 1

        recent_events: list[dict[str, Any]] = []
        for row in recent_rows:
            reasons = _parse_reasons(row["review_details_json"])
            soft_reasons = _parse_reasons(row["soft_details_json"])
            queue_status = str(row["queue_status"])
            post_status = str(row["last_post_status"] or "")
            if post_status == "POSTED":
                result = (
                    "SOFT_WARNING_AUTO_POSTED" if soft_reasons
                    else "AUTO_POSTED"
                )
                auto_posted = True
            elif queue_status == "BLOCKED_AUTO_POST_OFF":
                result = "BLOCKED_AUTO_POST_OFF"
                auto_posted = False
            elif reasons:
                result = "REVIEW_REQUIRED"
                auto_posted = False
            elif queue_status in {"FAILED", "RETRY_BY_SCHEDULER"}:
                result = queue_status
                auto_posted = False
            elif queue_status == "COMPLETED":
                # Queue COMPLETED means the auto-post run finished handling
                # this event, which is NOT the same as having posted. Keep the
                # two meanings distinct for the operator.
                result = "COMPLETED_NO_POST"
                auto_posted = False
            else:
                result = queue_status
                auto_posted = False
            recent_events.append({
                "inquiry_id": int(row["inquiry_id"]),
                "external_inquiry_id": row["external_id"],
                "queue_status": queue_status,
                "workflow_status": row["workflow_status"],
                "result": result,
                "auto_posted": auto_posted,
                "reasons": [_reason_bucket(item) for item in reasons],
                "soft_reasons": [
                    _reason_bucket(item) for item in soft_reasons
                ],
                # Raw codes preserved so an operator can diagnose a reason the
                # bucket map does not yet name.
                "raw_reason_codes": list(reasons),
                "last_error_code": row["last_error_code"],
                "attempt_count": int(row["attempt_count"] or 0),
                "retryable": queue_status == "RETRY_BY_SCHEDULER",
                "updated_at": row["updated_at"],
            })

        return {
            "queue": {
                "claimable_pending": event_summary["PENDING"],
                "processing": event_summary["PROCESSING"],
                "retry_scheduled": event_summary["RETRY_BY_SCHEDULER"],
                "blocked_auto_post_off": event_summary["BLOCKED_AUTO_POST_OFF"],
                "failed": event_summary["FAILED"],
                "completed": event_summary["COMPLETED"],
            },
            "review_required_reasons": reason_counts,
            "dps_required_count": int(dps_required or 0),
            "recent_events": recent_events,
            "failed_events": self._failed_event_diagnostics(),
            # Operator-facing severity split: a HARD block genuinely withheld
            # the answer; a SOFT warning was recorded but the answer still
            # posted; FAILED is a real pipeline failure, never a policy hold.
            "severity_counts": {
                "hard_block": sum(
                    1 for event in recent_events
                    if event["result"] == "REVIEW_REQUIRED"
                ),
                "soft_warning_auto_posted": sum(
                    1 for event in recent_events
                    if event["result"] == "SOFT_WARNING_AUTO_POSTED"
                ),
                "failed": sum(
                    1 for event in recent_events
                    if event["result"] in {"FAILED", "RETRY_BY_SCHEDULER"}
                ),
            },
        }

    def _failed_event_diagnostics(
        self, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Sanitized failure detail for FAILED/RETRY queue events.

        Only the already-sanitized ``error_type``/``error_code`` that the
        pipeline recorded are surfaced -- never a stack trace, request body,
        credential, or customer field.
        """
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT e.inquiry_id, e.external_id, e.status, e.attempt_count,
                       e.last_error_code, e.updated_at,
                       (SELECT a.details_json FROM activity_logs a
                         WHERE a.inquiry_id = e.inquiry_id
                           AND a.event_code IN (
                               'AUTO_ANSWER_FAILED', 'AUTO_SYNC_EVENT_FAILED'
                           )
                         ORDER BY a.id DESC LIMIT 1) AS failure_details_json
                FROM auto_sync_events e
                WHERE e.status IN ('FAILED', 'RETRY_BY_SCHEDULER')
                ORDER BY e.updated_at DESC, e.id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

        failures: list[dict[str, Any]] = []
        for row in rows:
            try:
                details = json.loads(str(row["failure_details_json"] or "{}"))
            except (TypeError, ValueError):
                details = {}
            if not isinstance(details, dict):
                details = {}
            failures.append({
                "inquiry_id": int(row["inquiry_id"]),
                "external_inquiry_id": row["external_id"],
                "queue_status": str(row["status"]),
                "stage": "AUTO_POST_PIPELINE",
                "error_type": details.get("error_type"),
                "error_code": (
                    details.get("error_code") or row["last_error_code"]
                ),
                "attempt_count": int(row["attempt_count"] or 0),
                "retryable": str(row["status"]) == "RETRY_BY_SCHEDULER",
                "updated_at": row["updated_at"],
            })
        return failures
