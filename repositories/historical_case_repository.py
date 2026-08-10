from __future__ import annotations

import json
from typing import Any

from repositories.database import Database


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}


class HistoricalCaseRepository:
    """Persistence boundary isolated from inquiries and the Auto Post outbox."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for key in ("raw_json", "metadata_json"):
            value[key] = _loads(value.get(key))
        for key in ("source_answered", "active"):
            value[key] = bool(value.get(key))
        # ``active`` is the persisted compatibility field.  The public alias
        # makes its opt-out meaning explicit without a destructive migration.
        value["learning_enabled"] = value["active"]
        return value

    def start_run(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO historical_import_runs(
                    import_key, source_mode, store_code, inquiry_types,
                    date_from, date_to, answered_only, require_seller_answer,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING')
                RETURNING *
                """,
                (
                    values["import_key"], values["source_mode"],
                    values.get("store_code"), _json(values.get("inquiry_types") or []),
                    values.get("date_from"), values.get("date_to"),
                    int(bool(values.get("answered_only", True))),
                    int(bool(values.get("require_seller_answer", True))),
                ),
            ).fetchone()
        return dict(row)

    def update_run(self, run_id: int, **counts: Any) -> dict[str, Any]:
        allowed = {
            "status", "total_fetched", "inserted_count", "updated_count",
            "duplicate_count", "no_answer_count", "skipped_count",
            "failed_count", "last_page", "error_message", "completed_at",
        }
        values = {key: value for key, value in counts.items() if key in allowed}
        if not values:
            return self.get_run(run_id) or {}
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.database.transaction() as connection:
            connection.execute(
                f"""
                UPDATE historical_import_runs SET {assignments},
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (*values.values(), int(run_id)),
            )
        return self.get_run(run_id) or {}

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM historical_import_runs WHERE id=?", (int(run_id),)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["inquiry_types"] = _loads(value.get("inquiry_types"))
        return value

    def recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM historical_import_runs ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert(self, case: dict[str, Any]) -> tuple[dict[str, Any], str]:
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM historical_cases WHERE case_key=?",
                (case["case_key"],),
            ).fetchone()
            if existing is not None and str(existing["fingerprint"]) == str(case["fingerprint"]):
                result = self._row(existing)
                assert result is not None
                return result, "duplicate"

            columns = (
                "source", "store_code", "inquiry_id", "external_inquiry_id",
                "inquiry_type", "question", "question_normalized", "seller_answer",
                "product_name", "product_id", "order_reference", "source_answered",
                "inquiry_created_at", "answer_updated_at", "source_payload_reference",
                "raw_json", "classification", "policy_risk", "quality_score",
                "confidence", "active", "metadata_json", "case_key", "fingerprint",
            )
            encoded = dict(case)
            encoded["raw_json"] = _json(case.get("raw_json"))
            encoded["metadata_json"] = _json(case.get("metadata_json"))
            encoded["source_answered"] = int(bool(case.get("source_answered")))
            encoded["active"] = int(bool(case.get("active", True)))
            if existing is None:
                placeholders = ",".join("?" for _ in columns)
                cursor = connection.execute(
                    f"INSERT INTO historical_cases({','.join(columns)}) VALUES ({placeholders})",
                    tuple(encoded.get(column) for column in columns),
                )
                case_id = int(cursor.lastrowid)
                outcome = "inserted"
            else:
                case_id = int(existing["id"])
                # A refresh/import must never undo an administrator's learning
                # opt-out.  Preserve the persisted state and its audit metadata.
                encoded["active"] = int(existing["active"])
                old_metadata = _loads(existing["metadata_json"])
                new_metadata = _loads(encoded["metadata_json"])
                if not isinstance(new_metadata, dict):
                    new_metadata = {}
                if isinstance(old_metadata, dict):
                    for key, value in old_metadata.items():
                        if key.startswith("learning_"):
                            new_metadata[key] = value
                encoded["metadata_json"] = _json(new_metadata)
                assignments = ",".join(f"{column}=?" for column in columns if column != "case_key")
                connection.execute(
                    f"""
                    UPDATE historical_cases SET {assignments},
                        imported_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE id=?
                    """,
                    tuple(encoded.get(column) for column in columns if column != "case_key") + (case_id,),
                )
                outcome = "updated"
            connection.execute(
                """
                INSERT OR IGNORE INTO historical_case_versions(
                    historical_case_id, fingerprint, seller_answer,
                    quality_score, policy_risk, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id, case["fingerprint"], case.get("seller_answer"),
                    case.get("quality_score", 0), case.get("policy_risk", "NONE"),
                    _json(case.get("metadata_json")),
                ),
            )
            row = connection.execute(
                "SELECT * FROM historical_cases WHERE id=?", (case_id,)
            ).fetchone()
        result = self._row(row)
        assert result is not None
        return result, outcome

    def get(self, case_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM historical_cases WHERE id=?", (int(case_id),)
            ).fetchone()
        return self._row(row)

    def get_by_case_key(self, case_key: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM historical_cases WHERE case_key=?", (str(case_key),)
            ).fetchone()
        return self._row(row)

    def count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM historical_cases").fetchone()[0])

    def summary(self, *, minimum_quality: float = 0.50) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                  SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS learning_enabled,
                  SUM(CASE WHEN active=0 THEN 1 ELSE 0 END) AS learning_excluded,
                  SUM(CASE WHEN active=1 AND seller_answer IS NOT NULL
                    AND trim(seller_answer)<>'' AND quality_score>=?
                    AND upper(policy_risk) NOT IN ('BLOCK','BLOCKED','HIGH')
                    THEN 1 ELSE 0 END) AS auto_usable,
                  SUM(CASE WHEN active=1 AND NOT (seller_answer IS NOT NULL
                    AND trim(seller_answer)<>'' AND quality_score>=?
                    AND upper(policy_risk) NOT IN ('BLOCK','BLOCKED','HIGH'))
                    THEN 1 ELSE 0 END) AS safety_excluded,
                  SUM(CASE WHEN promoted_learning_id IS NOT NULL THEN 1 ELSE 0 END) AS promoted,
                  SUM(CASE WHEN quality_score>=0.70 THEN 1 ELSE 0 END) AS quality_high,
                  SUM(CASE WHEN quality_score>=? AND quality_score<0.70 THEN 1 ELSE 0 END) AS quality_medium,
                  SUM(CASE WHEN quality_score<? THEN 1 ELSE 0 END) AS quality_low
                FROM historical_cases
                """,
                (minimum_quality, minimum_quality, minimum_quality, minimum_quality),
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def answer_frequency(self, seller_answer: str) -> int:
        with self.database.connection() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM historical_cases WHERE seller_answer=?",
                (str(seller_answer or ""),),
            ).fetchone()[0])

    def candidates(self, *, store_code: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM historical_cases
                WHERE active=1 AND seller_answer IS NOT NULL
                  AND trim(seller_answer)<>''
                  AND (? IS NULL OR store_code=?)
                ORDER BY quality_score DESC, inquiry_created_at DESC, id DESC
                LIMIT ?
                """,
                (store_code, store_code, max(1, min(int(limit), 2000))),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def list_cases(
        self, *, store_code: str | None = None, inquiry_type: str | None = None,
        search: str = "", active: bool | None = None, has_answer: bool | None = None,
        min_quality: float = 0.0, policy_risk: str | None = None,
        date_from: str | None = None, date_to: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses, params = ["quality_score>=?"], [max(0.0, min(float(min_quality), 1.0))]
        if store_code:
            clauses.append("store_code=?"); params.append(store_code)
        if inquiry_type:
            clauses.append("inquiry_type=?"); params.append(inquiry_type)
        if active is not None:
            clauses.append("active=?"); params.append(int(active))
        if policy_risk:
            clauses.append("upper(policy_risk)=?"); params.append(str(policy_risk).upper())
        if date_from:
            clauses.append("inquiry_created_at>=?"); params.append(date_from)
        if date_to:
            clauses.append("inquiry_created_at<=?"); params.append(date_to)
        if has_answer is not None:
            clauses.append("trim(COALESCE(seller_answer,''))<>''" if has_answer else "trim(COALESCE(seller_answer,''))=''" )
        if search.strip():
            clauses.append("(question LIKE ? OR seller_answer LIKE ? OR product_name LIKE ? OR external_inquiry_id LIKE ?)")
            token = f"%{search.strip()}%"; params.extend([token] * 4)
        params.append(max(1, min(int(limit), 1000)))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM historical_cases WHERE {' AND '.join(clauses)} ORDER BY inquiry_created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def set_learning_enabled(
        self, case_id: int, enabled: bool, *, reason: str = "", actor: str = "",
    ) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM historical_cases WHERE id=?",
                (int(case_id),),
            ).fetchone()
            if row is None:
                raise LookupError("Historical Case를 찾을 수 없습니다.")
            metadata = _loads(row["metadata_json"])
            if not isinstance(metadata, dict):
                metadata = {}
            now = connection.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ).fetchone()[0]
            if enabled:
                for key in (
                    "learning_exclusion_reason", "learning_excluded_at",
                    "learning_excluded_by",
                ):
                    metadata.pop(key, None)
                metadata["learning_reenabled_at"] = now
                if actor:
                    metadata["learning_reenabled_by"] = str(actor)
            else:
                metadata["learning_exclusion_reason"] = str(reason or "기타")
                metadata["learning_excluded_at"] = now
                if actor:
                    metadata["learning_excluded_by"] = str(actor)
            connection.execute(
                """
                UPDATE historical_cases
                SET active=?, metadata_json=?,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE id=?
                """,
                (int(bool(enabled)), _json(metadata), int(case_id)),
            )

    def set_active(self, case_id: int, active: bool) -> None:
        """Backward-compatible wrapper for the Historical learning toggle."""

        self.set_learning_enabled(case_id, active)

    def mark_promoted(self, case_id: int, learning_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE historical_cases SET promoted_learning_id=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (int(learning_id), int(case_id)),
            )
