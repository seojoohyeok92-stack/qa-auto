from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from repositories.database import Database
from workflow.models import InquiryStatus, validate_inquiry_status


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def serialize_json(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def deserialize_json(value: str | None) -> Any:
    if not value:
        return {}
    return json.loads(value)


@dataclass(frozen=True)
class UpsertResult:
    inquiry_id: int
    outcome: str

    @property
    def created(self) -> bool:
        return self.outcome == "new"


SOURCE_OWNED_FIELDS = (
    "inquiry_type",
    "title",
    "content",
    "product_id",
    "product_name",
    "option_name",
    "customer_display",
    "masked_writer_id",
    "order_id",
    "product_order_id",
    "registered_at",
    "source_answered",
    "source_status",
    "source_created_at",
    "source_updated_at",
    "is_private",
)

SYNC_FIELDS = SOURCE_OWNED_FIELDS + (
    "source_metadata_json",
    "raw_json",
)

DERIVED_OPERATIONAL_RAW_FIELDS = (
    "queue",
    "queue_label",
    "priority",
    "analysis",
    "is_delivery",
)


class InquiryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["raw_json"] = deserialize_json(result.get("raw_json"))
        if "source_metadata_json" in result:
            result["source_metadata_json"] = deserialize_json(
                result.get("source_metadata_json")
            )
        if "is_private" in result and result["is_private"] is not None:
            result["is_private"] = bool(result["is_private"])
        return result

    def upsert_work_item(self, inquiry: dict[str, Any]) -> UpsertResult:
        required = ("store_code", "source_type", "source_question_id")
        missing = [
            field
            for field in required
            if inquiry.get(field) in (None, "")
        ]
        if missing:
            raise ValueError(
                "Missing required inquiry fields: " + ", ".join(missing)
            )

        key = (
            str(inquiry["store_code"]),
            str(inquiry["source_type"]),
            str(inquiry["source_question_id"]),
        )
        external_inquiry_id = str(
            inquiry.get("external_inquiry_id")
            or inquiry["source_question_id"]
        )
        normalized = {
            field: (
                serialize_json(inquiry.get(field))
                if field in {"raw_json", "source_metadata_json"}
                else inquiry.get(field)
            )
            for field in SYNC_FIELDS
        }
        now = utc_now()

        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM inquiries
                WHERE store_code = ?
                  AND source_type = ?
                  AND source_question_id = ?
                """,
                key,
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO inquiries (
                        store_code, source_type, source_question_id,
                        external_inquiry_id, inquiry_type, title, content,
                        product_id, product_name, option_name,
                        customer_display, masked_writer_id,
                        order_id, product_order_id, registered_at,
                        source_answered, source_status, source_created_at,
                        source_updated_at, last_synced_at,
                        source_content_changed,
                        is_private, source_metadata_json,
                        workflow_status, answer_status,
                        post_status, created_at, updated_at, raw_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        *key,
                        external_inquiry_id,
                        normalized["inquiry_type"],
                        normalized["title"],
                        normalized["content"],
                        normalized["product_id"],
                        normalized["product_name"],
                        normalized["option_name"],
                        normalized["customer_display"],
                        normalized["masked_writer_id"],
                        normalized["order_id"],
                        normalized["product_order_id"],
                        normalized["registered_at"],
                        normalized["source_answered"],
                        normalized["source_status"],
                        normalized["source_created_at"],
                        normalized["source_updated_at"],
                        now,
                        normalized["is_private"],
                        normalized["source_metadata_json"],
                        validate_inquiry_status(
                            inquiry.get("workflow_status", InquiryStatus.NEW)
                        ).value,
                        str(inquiry.get("answer_status") or "UNANSWERED"),
                        str(inquiry.get("post_status") or "NOT_POSTED"),
                        now,
                        now,
                        normalized["raw_json"],
                    ),
                )
                return UpsertResult(int(cursor.lastrowid), "new")

            # Source refreshes merge into raw_json. Arbitrary local-only keys
            # remain intact, the current safe source payload wins for its own
            # keys, and None derived metadata never deletes a valid old value.
            existing_raw = deserialize_json(existing["raw_json"])
            incoming_raw = deserialize_json(normalized["raw_json"])
            if isinstance(existing_raw, dict) and isinstance(incoming_raw, dict):
                if normalized["order_id"] in (None, ""):
                    # Inquiry-list payloads may omit immutable order identifiers
                    # that came from a detail response or a verified local
                    # lookup. Omission is not a source-side deletion.
                    normalized["order_id"] = existing["order_id"]
                if normalized["product_order_id"] in (None, ""):
                    normalized["product_order_id"] = existing[
                        "product_order_id"
                    ]
                merged_raw = dict(existing_raw)
                incoming_source = incoming_raw.get("source_payload")
                if isinstance(incoming_source, dict):
                    merged_raw["source_payload"] = incoming_source
                    merged_raw.update(incoming_source)
                for raw_key, raw_value in incoming_raw.items():
                    if raw_key == "source_payload":
                        continue
                    if (
                        raw_key in DERIVED_OPERATIONAL_RAW_FIELDS
                        and raw_value in (None, "")
                    ):
                        continue
                    merged_raw[raw_key] = raw_value
                normalized["raw_json"] = serialize_json(merged_raw)

            source_changed = any(
                existing[field] != normalized[field]
                for field in SOURCE_OWNED_FIELDS
            )
            inquiry_id = int(existing["id"])
            if not source_changed:
                connection.execute(
                    """
                    UPDATE inquiries
                    SET last_synced_at = ?, source_content_changed = 0,
                        source_metadata_json = ?, raw_json = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        normalized["source_metadata_json"],
                        normalized["raw_json"],
                        inquiry_id,
                    ),
                )
                return UpsertResult(inquiry_id, "unchanged")

            source_content_changed = bool(
                existing["title"] != normalized["title"]
                or existing["content"] != normalized["content"]
            )
            connection.execute(
                """
                UPDATE inquiries
                SET inquiry_type = ?, title = ?, content = ?,
                    product_id = ?, product_name = ?, option_name = ?,
                    customer_display = ?, masked_writer_id = ?,
                    order_id = ?, product_order_id = ?, registered_at = ?,
                    source_answered = ?, source_status = ?,
                    source_created_at = ?, source_updated_at = ?,
                    last_synced_at = ?, source_content_changed = ?,
                    is_private = ?, source_metadata_json = ?,
                    raw_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized["inquiry_type"],
                    normalized["title"],
                    normalized["content"],
                    normalized["product_id"],
                    normalized["product_name"],
                    normalized["option_name"],
                    normalized["customer_display"],
                    normalized["masked_writer_id"],
                    normalized["order_id"],
                    normalized["product_order_id"],
                    normalized["registered_at"],
                    normalized["source_answered"],
                    normalized["source_status"],
                    normalized["source_created_at"],
                    normalized["source_updated_at"],
                    now,
                    1 if source_content_changed else 0,
                    normalized["is_private"],
                    normalized["source_metadata_json"],
                    normalized["raw_json"],
                    now,
                    inquiry_id,
                ),
            )
            return UpsertResult(inquiry_id, "updated")

    def get(self, inquiry_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM inquiries WHERE id = ?",
                (inquiry_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def update_order_snapshot(
        self,
        inquiry_id: int,
        *,
        order_id: str | None,
        product_order_id: str | None,
        order_date: str | None,
        product_name: str | None,
        order_status: str | None,
        lookup_at: str,
        lookup_type: str | None,
        cached: bool,
    ) -> dict[str, Any]:
        """Persist the safe Naver order fields used by Dashboard and DPS."""

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT raw_json FROM inquiries WHERE id = ?",
                (inquiry_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Inquiry not found: {inquiry_id}")
            raw = deserialize_json(row["raw_json"])
            if not isinstance(raw, dict):
                raw = {}
            raw["order_lookup"] = {
                "order_id": order_id,
                "product_order_id": product_order_id,
                "order_date": order_date,
                "product_name": product_name,
                "order_status": order_status,
                "lookup_at": lookup_at,
                "lookup_type": lookup_type,
                "cached": bool(cached),
            }
            raw["order_date"] = order_date
            cursor = connection.execute(
                """
                UPDATE inquiries
                SET order_id = COALESCE(?, order_id),
                    product_order_id = COALESCE(?, product_order_id),
                    order_date = ?,
                    product_name = COALESCE(?, product_name),
                    order_status = ?,
                    order_lookup_at = ?,
                    raw_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    order_id,
                    product_order_id,
                    order_date,
                    product_name,
                    order_status,
                    lookup_at,
                    serialize_json(raw),
                    utc_now(),
                    inquiry_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("Order snapshot was not saved.")
        updated = self.get(inquiry_id)
        if updated is None:
            raise RuntimeError("Saved order snapshot could not be reloaded.")
        return updated

    def update_delivery_routing_metadata(
        self,
        inquiry_id: int,
        *,
        queue: str,
        routing: dict[str, Any],
    ) -> bool:
        """Update only derived delivery routing fields in the local payload."""

        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT raw_json FROM inquiries WHERE id = ?",
                (inquiry_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Inquiry not found: {inquiry_id}")
            raw = deserialize_json(row["raw_json"])
            if not isinstance(raw, dict):
                raw = {}
            analysis = raw.get("analysis")
            if not isinstance(analysis, dict):
                analysis = {}
            analysis["delivery_routing"] = dict(routing)
            analysis["is_delivery"] = True
            raw["analysis"] = analysis
            raw["is_delivery"] = True
            raw["queue"] = str(queue)
            cursor = connection.execute(
                """
                UPDATE inquiries
                SET raw_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (serialize_json(raw), utc_now(), inquiry_id),
            )
        return cursor.rowcount == 1

    def get_by_source(
        self,
        store_code: str,
        source_type: str,
        source_question_id: str,
    ) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM inquiries
                WHERE store_code = ? AND source_type = ?
                  AND source_question_id = ?
                """,
                (store_code, source_type, source_question_id),
            ).fetchone()
        return self._row_to_dict(row)

    def list(
        self,
        *,
        workflow_status: str | InquiryStatus | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if workflow_status is not None:
            clauses.append("workflow_status = ?")
            parameters.append(validate_inquiry_status(workflow_status).value)
        if search and search.strip():
            pattern = f"%{search.strip()}%"
            clauses.append(
                """
                (title LIKE ? OR content LIKE ? OR product_name LIKE ?
                 OR order_id LIKE ? OR source_question_id LIKE ?)
                """
            )
            parameters.extend([pattern] * 5)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.extend([max(1, int(limit)), max(0, int(offset))])
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM inquiries{where}
                ORDER BY registered_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._row_to_dict(row) for row in rows if row is not None]

    def count(
        self,
        workflow_status: str | InquiryStatus | None = None,
    ) -> int:
        if workflow_status is None:
            sql = "SELECT COUNT(*) FROM inquiries"
            parameters: tuple[Any, ...] = ()
        else:
            sql = "SELECT COUNT(*) FROM inquiries WHERE workflow_status = ?"
            parameters = (validate_inquiry_status(workflow_status).value,)
        with self.database.connection() as connection:
            return int(connection.execute(sql, parameters).fetchone()[0])

    @staticmethod
    def _effective_learning_status_sql(inquiry_alias: str) -> str:
        """One SQL definition shared by dashboard badges and filters."""

        approved = f"""
            EXISTS (SELECT 1 FROM learning_examples ap
                    WHERE ap.inquiry_id={inquiry_alias}.id
                      AND ap.active=1 AND ap.validity_active=1
                      AND COALESCE(json_extract(ap.metadata_json,
                          '$.learning_signal_type'),'POSITIVE')='POSITIVE'
                      AND (COALESCE(json_extract(ap.metadata_json,
                              '$.human_verified'),0)=1
                        OR ap.approval_history_id IS NOT NULL
                        OR upper(COALESCE(ap.validator_result,'')) IN (
                          'HUMAN_VERIFIED_NAVER_POSTED',
                          'HISTORICAL_ADMIN_APPROVED'
                        )
                        OR (upper(COALESCE(json_extract(ap.metadata_json,
                              '$.source_origin'),''))='HISTORICAL_PROMOTED'
                          AND trim(COALESCE(json_extract(ap.metadata_json,
                              '$.promoted_by'),''))<>'')))
        """
        automatic = f"""
            EXISTS (SELECT 1 FROM learning_examples au
                    WHERE au.inquiry_id={inquiry_alias}.id
                      AND au.active=1 AND au.validity_active=1
                      AND COALESCE(json_extract(au.metadata_json,
                          '$.learning_signal_type'),'POSITIVE')='POSITIVE'
                      AND COALESCE(json_extract(au.metadata_json,
                          '$.human_verified'),0)=0
                      AND au.approval_history_id IS NULL
                      AND upper(COALESCE(au.validator_result,'')) NOT IN (
                        'HUMAN_VERIFIED_NAVER_POSTED',
                        'HISTORICAL_ADMIN_APPROVED'
                      )
                      AND NOT (upper(COALESCE(json_extract(au.metadata_json,
                            '$.source_origin'),''))='HISTORICAL_PROMOTED'
                        AND trim(COALESCE(json_extract(au.metadata_json,
                            '$.promoted_by'),''))<>''))
        """
        exclusion = f"""
            (EXISTS (SELECT 1 FROM learning_feedback ex
                     WHERE ex.inquiry_id={inquiry_alias}.id AND ex.active=1
                       AND ex.learning_signal_type IN ('NEGATIVE','EXCLUDED')
                       AND NOT (
                         json_extract(ex.metadata_json,
                           '$.lifecycle_superseded_by_learning_id') IS NOT NULL
                         AND EXISTS (
                           SELECT 1 FROM learning_examples sup
                           WHERE sup.id=json_extract(ex.metadata_json,
                             '$.lifecycle_superseded_by_learning_id')
                             AND sup.active=1
                             AND COALESCE(json_extract(sup.metadata_json,
                               '$.human_verified'),0)=1
                         )
                       ))
             OR EXISTS (SELECT 1 FROM learning_examples rv
                        WHERE rv.inquiry_id={inquiry_alias}.id AND rv.active=0
                          AND COALESCE(json_extract(rv.metadata_json,
                              '$.verification_revoked'),0)=1
                          AND NOT (
                            json_extract(rv.metadata_json,
                              '$.lifecycle_superseded_by_learning_id') IS NOT NULL
                            AND EXISTS (
                              SELECT 1 FROM learning_examples sup
                              WHERE sup.id=json_extract(rv.metadata_json,
                                '$.lifecycle_superseded_by_learning_id')
                                AND sup.active=1
                                AND COALESCE(json_extract(sup.metadata_json,
                                  '$.human_verified'),0)=1
                            )
                          )))
        """
        corrected = f"""
            EXISTS (SELECT 1 FROM learning_feedback co
                    WHERE co.inquiry_id={inquiry_alias}.id AND co.active=1
                      AND co.learning_signal_type='INTENT_CORRECTION'
                      AND NOT (
                        json_extract(co.metadata_json,
                          '$.lifecycle_superseded_by_learning_id') IS NOT NULL
                        AND EXISTS (
                          SELECT 1 FROM learning_examples sup
                          WHERE sup.id=json_extract(co.metadata_json,
                            '$.lifecycle_superseded_by_learning_id')
                            AND sup.active=1
                            AND COALESCE(json_extract(sup.metadata_json,
                              '$.human_verified'),0)=1
                        )
                      ))
        """
        return f"""
            CASE
              WHEN {exclusion} THEN 'EXCLUDED'
              WHEN {corrected} THEN 'CORRECTED'
              WHEN {approved} THEN 'APPROVED'
              WHEN {automatic} THEN 'AUTO'
              ELSE 'NONE'
            END
        """

    @staticmethod
    def _dashboard_where(
        *,
        store_codes: list[str],
        source: str,
        queues: list[str],
        priorities: list[str],
        answer_status: str,
        delivery_only: bool,
        search_query: str,
        start_date: str,
        end_date: str,
        kpi_filter: str | None,
        learning_status: str = "ALL",
    ) -> tuple[str, list[Any]]:
        clauses = [
            "substr(registered_at, 1, 10) BETWEEN ? AND ?",
        ]
        parameters: list[Any] = [start_date, end_date]
        if store_codes:
            placeholders = ",".join("?" for _ in store_codes)
            clauses.append(f"store_code IN ({placeholders})")
            parameters.extend(store_codes)
        if source != "ALL":
            clauses.append("source_type = ?")
            parameters.append(source)

        def add_json_filter(field: str, selected: list[str]) -> None:
            if not selected:
                return
            include_unclassified = "UNCLASSIFIED" in selected
            values = [value for value in selected if value != "UNCLASSIFIED"]
            parts: list[str] = []
            if values:
                placeholders = ",".join("?" for _ in values)
                parts.append(
                    f"json_extract(raw_json, '$.{field}') IN ({placeholders})"
                )
                parameters.extend(values)
            if include_unclassified:
                parts.append(
                    f"(json_extract(raw_json, '$.{field}') IS NULL "
                    f"OR json_extract(raw_json, '$.{field}') = '')"
                )
            clauses.append("(" + " OR ".join(parts) + ")")

        add_json_filter("queue", queues)
        add_json_filter("priority", priorities)
        if answer_status == "UNANSWERED":
            clauses.append(
                "COALESCE(source_answered, "
                "CASE WHEN upper(answer_status)='ANSWERED' THEN 1 ELSE 0 END)=0"
            )
        elif answer_status == "ANSWERED":
            clauses.append(
                "COALESCE(source_answered, "
                "CASE WHEN upper(answer_status)='ANSWERED' THEN 1 ELSE 0 END)=1"
            )
        if delivery_only:
            clauses.append(
                "COALESCE(json_extract(raw_json, '$.is_delivery'), "
                "json_extract(raw_json, '$.analysis.is_delivery'), 0)=1"
            )
        normalized_search = search_query.strip()
        if normalized_search:
            pattern = f"%{normalized_search}%"
            clauses.append(
                """
                (title LIKE ? OR content LIKE ? OR product_name LIKE ?
                 OR order_id LIKE ? OR product_order_id LIKE ?
                 OR source_question_id LIKE ? OR customer_display LIKE ?)
                """
            )
            parameters.extend([pattern] * 7)
        if kpi_filter == "NEW":
            clauses.append("workflow_status = 'NEW'")
        elif kpi_filter == "DRAFTED":
            clauses.extend(
                [
                    "approval_status = 'PENDING'",
                    "post_status != 'POSTED'",
                    "EXISTS (SELECT 1 FROM answer_drafts d "
                    "WHERE d.inquiry_id=inquiries.id AND d.is_active=1)",
                ]
            )
        elif kpi_filter == "REVIEW":
            clauses.extend(
                [
                    "approval_status = 'PENDING'",
                    "workflow_status IN ('REVIEW_PENDING','NEEDS_ATTENTION')",
                    "EXISTS (SELECT 1 FROM answer_drafts d "
                    "WHERE d.inquiry_id=inquiries.id AND d.is_active=1)",
                ]
            )
        elif kpi_filter == "APPROVED":
            clauses.append("approval_status = 'APPROVED'")
        elif kpi_filter == "ATTENTION":
            clauses.append("workflow_status IN ('NEEDS_ATTENTION','FAILED')")
        normalized_learning = str(learning_status or "ALL").upper()
        if normalized_learning in {
            "APPROVED", "AUTO", "EXCLUDED", "CORRECTED", "NONE"
        }:
            clauses.append(
                f"({InquiryRepository._effective_learning_status_sql('inquiries')})=?"
            )
            parameters.append(normalized_learning)
        return " WHERE " + " AND ".join(clauses), parameters

    def dashboard_page(
        self,
        *,
        store_codes: list[str],
        source: str,
        queues: list[str],
        priorities: list[str],
        answer_status: str,
        delivery_only: bool,
        search_query: str,
        start_date: str,
        end_date: str,
        kpi_filter: str | None,
        page: int,
        page_size: int,
        learning_status: str = "ALL",
    ) -> tuple[list[dict[str, Any]], int, int]:
        safe_size = page_size if page_size in {10, 15, 20, 30} else 15
        where, parameters = self._dashboard_where(
            store_codes=store_codes,
            source=source,
            queues=queues,
            priorities=priorities,
            answer_status=answer_status,
            delivery_only=delivery_only,
            search_query=search_query,
            start_date=start_date,
            end_date=end_date,
            kpi_filter=kpi_filter,
            learning_status=learning_status,
        )
        with self.database.connection() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM inquiries{where}",
                    parameters,
                ).fetchone()[0]
            )
            total_pages = max(1, (total + safe_size - 1) // safe_size)
            safe_page = min(max(1, int(page)), total_pages)
            rows = connection.execute(
                f"""
                SELECT * FROM inquiries{where}
                ORDER BY registered_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*parameters, safe_size, (safe_page - 1) * safe_size],
            ).fetchall()
        values = [
                self._row_to_dict(row)
                for row in rows
                if row is not None
            ]
        states = self.learning_states([int(row["id"]) for row in values])
        for row in values:
            row.update(states.get(int(row["id"]), self._empty_learning_state()))
        return values, total, total_pages

    def dashboard_kpi_counts(
        self,
        *,
        store_codes: list[str],
        source: str,
        queues: list[str],
        priorities: list[str],
        answer_status: str,
        delivery_only: bool,
        search_query: str,
        start_date: str,
        end_date: str,
        learning_status: str = "ALL",
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        with self.database.connection() as connection:
            for code in (
                "NEW",
                "DRAFTED",
                "REVIEW",
                "APPROVED",
                "ATTENTION",
            ):
                where, parameters = self._dashboard_where(
                    store_codes=store_codes,
                    source=source,
                    queues=queues,
                    priorities=priorities,
                    answer_status=answer_status,
                    delivery_only=delivery_only,
                    search_query=search_query,
                    start_date=start_date,
                    end_date=end_date,
                    kpi_filter=code,
                    learning_status=learning_status,
                )
                result[code] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM inquiries{where}",
                        parameters,
                    ).fetchone()[0]
                )
        return result

    @staticmethod
    def _empty_learning_state() -> dict[str, Any]:
        from services.learning_lifecycle_service import resolve_learning_lifecycle

        return resolve_learning_lifecycle({})

    def learning_states(
        self, inquiry_ids: list[int] | None = None
    ) -> dict[int, dict[str, Any]]:
        """Batch-resolve current Learning state; never query per inquiry."""

        from services.learning_lifecycle_service import resolve_learning_lifecycle

        clauses = ""
        parameters: list[Any] = []
        clean = sorted({int(value) for value in inquiry_ids or []})
        if inquiry_ids is not None:
            if not clean:
                return {}
            placeholders = ",".join("?" for _ in clean)
            clauses = f"WHERE i.id IN ({placeholders})"
            parameters.extend(clean)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT i.id,
                  ({self._effective_learning_status_sql('i')}) effective_status,
                  EXISTS (SELECT 1 FROM learning_examples ap
                    WHERE ap.inquiry_id=i.id AND ap.active=1
                      AND COALESCE(json_extract(ap.metadata_json,
                        '$.human_verified'),0)=1) has_approved,
                  EXISTS (SELECT 1 FROM learning_examples au
                    WHERE au.inquiry_id=i.id AND au.active=1
                      AND COALESCE(json_extract(au.metadata_json,
                        '$.learning_signal_type'),'POSITIVE')='POSITIVE'
                      AND COALESCE(json_extract(au.metadata_json,
                        '$.human_verified'),0)=0) has_auto,
                  EXISTS (SELECT 1 FROM learning_feedback ex
                    WHERE ex.inquiry_id=i.id AND ex.active=1
                      AND ex.learning_signal_type IN ('NEGATIVE','EXCLUDED'))
                    has_excluded,
                  EXISTS (SELECT 1 FROM learning_feedback co
                    WHERE co.inquiry_id=i.id AND co.active=1
                      AND co.learning_signal_type='INTENT_CORRECTION')
                    has_corrected
                FROM inquiries i
                {clauses}
                """,
                parameters,
            ).fetchall()
        return {
            int(row["id"]): resolve_learning_lifecycle(dict(row))
            for row in rows
        }

    def latest_registered_at(
        self,
        *,
        store_code: str | None = None,
        source_type: str | None = None,
    ) -> str | None:
        clauses: list[str] = []
        parameters: list[Any] = []
        if store_code:
            clauses.append("store_code = ?")
            parameters.append(str(store_code))
        if source_type:
            clauses.append("source_type = ?")
            parameters.append(str(source_type))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.connection() as connection:
            value = connection.execute(
                f"SELECT MAX(registered_at) FROM inquiries{where}",
                parameters,
            ).fetchone()[0]
        return str(value) if value not in (None, "") else None

    def sync_watermarks(self) -> dict[tuple[str, str], str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT store_code, source_type, MAX(registered_at) AS latest
                FROM inquiries
                WHERE registered_at IS NOT NULL AND registered_at != ''
                GROUP BY store_code, source_type
                """
            ).fetchall()
        return {
            (str(row["store_code"]), str(row["source_type"])): str(row["latest"])
            for row in rows
            if row["latest"] not in (None, "")
        }

    def update_status(
        self,
        inquiry_id: int,
        workflow_status: str | InquiryStatus,
    ) -> bool:
        status = validate_inquiry_status(workflow_status).value
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE inquiries
                SET workflow_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, utc_now(), inquiry_id),
            )
        return cursor.rowcount == 1

    def update_phase9_status(
        self,
        inquiry_id: int,
        phase9_status: str,
    ) -> bool:
        allowed = {
            "ORDER_INFO_REQUIRED",
            "INFORMATION_REQUIRED",
            "READY_FOR_REVIEW",
            "MANUAL_REVIEW_REQUIRED",
            "VALIDATION_BLOCKED",
        }
        value = str(phase9_status or "").upper()
        if value not in allowed:
            raise ValueError(f"Invalid Phase 9 status: {phase9_status}")
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE inquiries
                SET phase9_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (value, utc_now(), inquiry_id),
            )
        return cursor.rowcount == 1

    def delete(self, inquiry_id: int) -> bool:
        with self.database.transaction() as connection:
            inquiry = connection.execute(
                "SELECT post_status FROM inquiries WHERE id = ?",
                (inquiry_id,),
            ).fetchone()
            if inquiry is None:
                return False
            posted_draft = connection.execute(
                """
                SELECT 1 FROM answer_drafts
                WHERE inquiry_id = ? AND posted = 1
                LIMIT 1
                """,
                (inquiry_id,),
            ).fetchone()
            if (
                str(inquiry["post_status"] or "").upper() == "POSTED"
                or posted_draft is not None
            ):
                raise ValueError("등록 완료된 문의는 삭제할 수 없습니다.")
            cursor = connection.execute(
                "DELETE FROM inquiries WHERE id = ?",
                (inquiry_id,),
            )
        return cursor.rowcount == 1
