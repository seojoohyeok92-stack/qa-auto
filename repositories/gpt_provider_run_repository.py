from __future__ import annotations

from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json
from repositories.log_repository import mask_sensitive_text


class GptProviderRunRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["success"] = bool(result["success"])
        result["fallback_used"] = bool(result["fallback_used"])
        result["canary_selected"] = bool(result["canary_selected"])
        if result["validator_passed"] is not None:
            result["validator_passed"] = bool(result["validator_passed"])
        result["shadow_comparison_json"] = deserialize_json(
            result["shadow_comparison_json"]
        )
        return result

    def create_run(self, **values: Any) -> dict[str, Any]:
        error_message = values.get("error_message_masked")
        masked_error = (
            mask_sensitive_text(str(error_message))[:1_000]
            if error_message
            else None
        )
        columns = (
            "inquiry_id",
            "draft_id",
            "correlation_id",
            "provider",
            "model",
            "mode",
            "prompt_version",
            "policy_version",
            "privacy_policy_version",
            "validator_policy_version",
            "company_tone_version",
            "started_at",
            "completed_at",
            "duration_ms",
            "success",
            "error_type",
            "error_message_masked",
            "input_size",
            "output_size",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "estimated_cost_krw",
            "privacy_removed_count",
            "validator_passed",
            "fallback_used",
            "retry_count",
            "canary_selected",
            "shadow_comparison_json",
        )
        parameters = [
            values.get("inquiry_id"),
            values.get("draft_id"),
            str(values["correlation_id"]),
            str(values.get("provider") or "unknown"),
            str(values.get("model") or ""),
            str(values.get("mode") or "DISABLED"),
            str(values.get("prompt_version") or ""),
            str(values.get("policy_version") or ""),
            str(values.get("privacy_policy_version") or ""),
            str(values.get("validator_policy_version") or ""),
            str(values.get("company_tone_version") or ""),
            str(values["started_at"]),
            str(values["completed_at"]),
            max(0, int(values.get("duration_ms") or 0)),
            int(bool(values.get("success"))),
            str(values.get("error_type") or "")[:100] or None,
            masked_error,
            max(0, int(values.get("input_size") or 0)),
            max(0, int(values.get("output_size") or 0)),
            values.get("input_tokens"),
            values.get("output_tokens"),
            values.get("total_tokens"),
            values.get("estimated_cost_krw"),
            max(0, int(values.get("privacy_removed_count") or 0)),
            (
                None
                if values.get("validator_passed") is None
                else int(bool(values.get("validator_passed")))
            ),
            int(bool(values.get("fallback_used"))),
            max(0, int(values.get("retry_count") or 0)),
            int(bool(values.get("canary_selected"))),
            serialize_json(values.get("shadow_comparison") or {}),
        ]
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO gpt_provider_runs ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                parameters,
            )
            row_id = int(cursor.lastrowid)
        result = self.get(row_id)
        assert result is not None
        return result

    def get(self, run_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM gpt_provider_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._row(row)

    def attach_draft(self, run_id: int, draft_id: int) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE gpt_provider_runs SET draft_id = ? WHERE id = ?",
                (draft_id, run_id),
            )
        return cursor.rowcount == 1

    def recent(
        self, *, inquiry_id: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if inquiry_id is None:
            sql = (
                "SELECT * FROM gpt_provider_runs "
                "ORDER BY created_at DESC, id DESC LIMIT ?"
            )
            parameters = (max(1, min(limit, 1_000)),)
        else:
            sql = (
                "SELECT * FROM gpt_provider_runs WHERE inquiry_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?"
            )
            parameters = (inquiry_id, max(1, min(limit, 1_000)))
        with self.database.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [result for row in rows if (result := self._row(row))]

    def latest_for_draft(
        self,
        draft_id: int,
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM gpt_provider_runs
                WHERE draft_id = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (int(draft_id),),
            ).fetchone()
        return self._row(row)

    def count_since(
        self, since: str, *, inquiry_id: int | None = None
    ) -> int:
        sql = "SELECT COUNT(*) FROM gpt_provider_runs WHERE created_at >= ?"
        parameters: list[Any] = [since]
        if inquiry_id is not None:
            sql += " AND inquiry_id = ?"
            parameters.append(inquiry_id)
        with self.database.connection() as connection:
            return int(connection.execute(sql, parameters).fetchone()[0])

    def cost_since(self, since: str) -> float:
        with self.database.connection() as connection:
            value = connection.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_krw), 0)
                FROM gpt_provider_runs WHERE created_at >= ?
                """,
                (since,),
            ).fetchone()[0]
        return float(value or 0)

    def dashboard_stats(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS requests,
                       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                       SUM(fallback_used) AS fallbacks,
                       COALESCE(SUM(estimated_cost_krw), 0) AS cost,
                       COALESCE(AVG(duration_ms), 0) AS average_ms,
                       SUM(CASE WHEN error_type = 'PRIVACY_BLOCKED'
                           THEN 1 ELSE 0 END) AS privacy_blocks
                FROM gpt_provider_runs
                WHERE substr(created_at, 1, 10) = date('now')
                """
            ).fetchone()
        return {
            "requests": int(row["requests"] or 0),
            "failures": int(row["failures"] or 0),
            "fallbacks": int(row["fallbacks"] or 0),
            "estimated_cost_krw": float(row["cost"] or 0),
            "average_duration_ms": float(row["average_ms"] or 0),
            "privacy_blocks": int(row["privacy_blocks"] or 0),
        }
