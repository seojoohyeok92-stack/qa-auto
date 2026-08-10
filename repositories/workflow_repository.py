from __future__ import annotations

from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json, utc_now
from workflow.models import (
    DEFAULT_STEP_ORDER,
    STEP_ORDER_INDEX,
    StepCode,
    StepStatus,
    validate_step_code,
    validate_step_status,
    validate_step_transition,
)


class WorkflowRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize_steps(self, inquiry_id: int) -> int:
        # Auto Sync revisits every inquiry, including rows whose workflow was
        # fully initialized long ago.  Avoid opening a write transaction for
        # the common idempotent case.  Besides reducing WAL contention, this
        # prevents a transient read-only filesystem window from turning an
        # otherwise successful unchanged sync into DB_WRITE_FAILED.
        expected = {step_code.value for step_code in DEFAULT_STEP_ORDER}
        with self.database.connection() as connection:
            existing = {
                str(row["step_code"])
                for row in connection.execute(
                    """
                    SELECT step_code FROM workflow_steps
                    WHERE inquiry_id = ?
                    """,
                    (inquiry_id,),
                ).fetchall()
            }
        if expected.issubset(existing):
            return 0

        inserted = 0
        with self.database.transaction() as connection:
            for step_code in DEFAULT_STEP_ORDER:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO workflow_steps (
                        inquiry_id, step_code, step_status, metadata_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        inquiry_id,
                        step_code.value,
                        StepStatus.PENDING.value,
                        "{}",
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def _get_row(self, connection: Any, inquiry_id: int, step_code: StepCode) -> Any:
        row = connection.execute(
            """
            SELECT * FROM workflow_steps
            WHERE inquiry_id = ? AND step_code = ?
            """,
            (inquiry_id, step_code.value),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"Workflow step not initialized: {inquiry_id}/{step_code.value}"
            )
        return row

    def start_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        *,
        metadata: Any = None,
    ) -> dict[str, Any]:
        code = validate_step_code(step_code)
        with self.database.transaction() as connection:
            row = self._get_row(connection, inquiry_id, code)
            validate_step_transition(row["step_status"], StepStatus.RUNNING)
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_steps
                SET step_status = ?, started_at = ?,
                    completed_at = NULL, attempt_count = attempt_count + 1,
                    last_error_code = NULL, last_error_message = NULL,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    StepStatus.RUNNING.value,
                    now,
                    serialize_json(metadata),
                    now,
                    row["id"],
                ),
            )
        return self.get_step(inquiry_id, code)

    def complete_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        *,
        metadata: Any = None,
    ) -> dict[str, Any]:
        code = validate_step_code(step_code)
        with self.database.transaction() as connection:
            row = self._get_row(connection, inquiry_id, code)
            current = validate_step_status(row["step_status"])
            now = utc_now()
            if current is StepStatus.PENDING:
                validate_step_transition(current, StepStatus.RUNNING)
                current = StepStatus.RUNNING
                attempt_increment = 1
            else:
                attempt_increment = 0
            validate_step_transition(current, StepStatus.COMPLETED)
            connection.execute(
                """
                UPDATE workflow_steps
                SET step_status = ?, started_at = COALESCE(started_at, ?),
                    completed_at = ?,
                    attempt_count = attempt_count + ?,
                    last_error_code = NULL, last_error_message = NULL,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    StepStatus.COMPLETED.value,
                    now,
                    now,
                    attempt_increment,
                    serialize_json(metadata),
                    now,
                    row["id"],
                ),
            )
        return self.get_step(inquiry_id, code)

    def fail_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        error_code: str,
        error_message: str,
        *,
        metadata: Any = None,
    ) -> dict[str, Any]:
        return self._finish_exceptional_step(
            inquiry_id,
            step_code,
            StepStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            metadata=metadata,
        )

    def mark_needs_review(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        *,
        error_code: str | None = None,
        message: str | None = None,
        metadata: Any = None,
    ) -> dict[str, Any]:
        return self._finish_exceptional_step(
            inquiry_id,
            step_code,
            StepStatus.NEEDS_REVIEW,
            error_code=error_code,
            error_message=message,
            metadata=metadata,
        )

    def _finish_exceptional_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        target: StepStatus,
        *,
        error_code: str | None,
        error_message: str | None,
        metadata: Any,
    ) -> dict[str, Any]:
        code = validate_step_code(step_code)
        with self.database.transaction() as connection:
            row = self._get_row(connection, inquiry_id, code)
            current = validate_step_status(row["step_status"])
            validate_step_transition(current, target)
            increment = 1 if current is StepStatus.PENDING else 0
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_steps
                SET step_status = ?, started_at = COALESCE(started_at, ?),
                    completed_at = ?, attempt_count = attempt_count + ?,
                    last_error_code = ?, last_error_message = ?,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target.value,
                    now,
                    now,
                    increment,
                    error_code[:100] if error_code else None,
                    error_message[:2_000] if error_message else None,
                    serialize_json(metadata),
                    now,
                    row["id"],
                ),
            )
        return self.get_step(inquiry_id, code)

    def skip_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        *,
        metadata: Any = None,
    ) -> dict[str, Any]:
        code = validate_step_code(step_code)
        with self.database.transaction() as connection:
            row = self._get_row(connection, inquiry_id, code)
            validate_step_transition(row["step_status"], StepStatus.SKIPPED)
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_steps
                SET step_status = ?, completed_at = ?,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    StepStatus.SKIPPED.value,
                    now,
                    serialize_json(metadata),
                    now,
                    row["id"],
                ),
            )
        return self.get_step(inquiry_id, code)

    def retry_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        *,
        metadata: Any = None,
    ) -> dict[str, Any]:
        code = validate_step_code(step_code)
        with self.database.transaction() as connection:
            row = self._get_row(connection, inquiry_id, code)
            validate_step_transition(row["step_status"], StepStatus.RUNNING)
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_steps
                SET step_status = ?, started_at = ?, completed_at = NULL,
                    attempt_count = attempt_count + 1,
                    last_error_code = NULL, last_error_message = NULL,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    StepStatus.RUNNING.value,
                    now,
                    serialize_json(metadata),
                    now,
                    row["id"],
                ),
            )
        return self.get_step(inquiry_id, code)

    def reopen_skipped_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        *,
        metadata: Any = None,
    ) -> dict[str, Any]:
        """Reopen a step skipped by a stale inquiry classification.

        This is deliberately narrower than a generic reset: only SKIPPED
        steps can be reopened, and existing attempt history is preserved.
        """

        code = validate_step_code(step_code)
        with self.database.transaction() as connection:
            row = self._get_row(connection, inquiry_id, code)
            current = validate_step_status(row["step_status"])
            if current is StepStatus.SKIPPED:
                now = utc_now()
                connection.execute(
                """
                UPDATE workflow_steps
                SET step_status = ?, started_at = NULL, completed_at = NULL,
                    last_error_code = NULL, last_error_message = NULL,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                    (
                        StepStatus.PENDING.value,
                        serialize_json(metadata),
                        now,
                        row["id"],
                    ),
                )
        return self.get_step(inquiry_id, code)

    def restart_completed_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
        *,
        metadata: Any = None,
    ) -> dict[str, Any]:
        code = validate_step_code(step_code)
        if code not in {
            StepCode.ANSWER_GENERATED,
            StepCode.DPS_LOOKUP,
            StepCode.STAFF_REVIEW,
        }:
            raise ValueError(
                "Only answer, DPS, and staff review steps support restart."
            )
        with self.database.transaction() as connection:
            row = self._get_row(connection, inquiry_id, code)
            current = validate_step_status(row["step_status"])
            if current is not StepStatus.COMPLETED:
                raise ValueError(
                    "Explicit restart requires a COMPLETED step."
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE workflow_steps
                SET step_status = ?, started_at = ?, completed_at = NULL,
                    attempt_count = attempt_count + 1,
                    last_error_code = NULL, last_error_message = NULL,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    StepStatus.RUNNING.value,
                    now,
                    serialize_json(metadata),
                    now,
                    row["id"],
                ),
            )
        return self.get_step(inquiry_id, code)

    def get_step(
        self,
        inquiry_id: int,
        step_code: str | StepCode,
    ) -> dict[str, Any]:
        code = validate_step_code(step_code)
        with self.database.connection() as connection:
            row = self._get_row(connection, inquiry_id, code)
        result = dict(row)
        result["metadata_json"] = deserialize_json(result["metadata_json"])
        return result

    def list_steps(self, inquiry_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_steps WHERE inquiry_id = ?",
                (inquiry_id,),
            ).fetchall()
        results = [dict(row) for row in rows]
        for result in results:
            result["metadata_json"] = deserialize_json(result["metadata_json"])
        return sorted(
            results,
            key=lambda item: STEP_ORDER_INDEX[item["step_code"]],
        )
