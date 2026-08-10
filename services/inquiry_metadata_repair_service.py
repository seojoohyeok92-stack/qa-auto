from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json
from repositories.log_repository import LogRepository
from services.naver_inquiry_normalizer import derive_operational_metadata


REPAIR_FIELDS = ("queue", "priority", "analysis")
LOCAL_RAW_PROTECTION_FIELDS = (
    "order_lookup",
    "order_date",
    "dps_lookup",
    "dps_result",
    "staff_metadata",
    "validation_metadata",
    "workflow_metadata",
)


@dataclass(frozen=True)
class MetadataRepairResult:
    dry_run: bool
    target_count: int
    queue_recoverable_count: int
    priority_recoverable_count: int
    analysis_recoverable_count: int
    repaired_count: int
    unclassified_count: int
    conflict_count: int
    failed_count: int
    protected_fields_changed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _missing(value: Any) -> bool:
    return value in (None, "", {}, [])


def protected_state_fingerprint(database: Database) -> str:
    """Hash local workflow/answer/order state without exposing its contents."""

    digest = hashlib.sha256()
    with database.connection() as connection:
        for table in (
            "answer_drafts",
            "approval_history",
            "dps_lookup_results",
            "dps_results",
            "workflow_steps",
        ):
            rows = connection.execute(
                f"SELECT * FROM {table} ORDER BY id"
            ).fetchall()
            digest.update(table.encode("utf-8"))
            for row in rows:
                digest.update(
                    json.dumps(
                        list(row),
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
        rows = connection.execute(
            """
            SELECT id, order_id, product_order_id, order_date,
                   order_status, order_lookup_at, workflow_status,
                   answer_status, post_status, approval_status,
                   approved_by, approved_at, raw_json
            FROM inquiries
            ORDER BY id
            """
        ).fetchall()
        digest.update(b"inquiry-local-state")
        for row in rows:
            raw = deserialize_json(row["raw_json"])
            protected_raw = {
                key: raw[key]
                for key in LOCAL_RAW_PROTECTION_FIELDS
                if isinstance(raw, dict) and key in raw
            }
            values = [
                row[key]
                for key in row.keys()
                if key != "raw_json"
            ]
            values.append(protected_raw)
            digest.update(
                json.dumps(
                    values,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    return digest.hexdigest()


class InquiryMetadataRepairService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def run(self, *, dry_run: bool = True) -> MetadataRepairResult:
        before = protected_state_fingerprint(self.database)
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, title, content, order_id, raw_json
                FROM inquiries
                ORDER BY id
                """
            ).fetchall()

        target_count = 0
        queue_recoverable = 0
        priority_recoverable = 0
        analysis_recoverable = 0
        repaired_count = 0
        unclassified_count = 0
        conflict_count = 0
        failed_count = 0
        repairs: list[tuple[str, int]] = []

        for row in rows:
            try:
                raw = deserialize_json(row["raw_json"])
                if not isinstance(raw, dict):
                    raw = {}
                missing_fields = [
                    field for field in REPAIR_FIELDS if _missing(raw.get(field))
                ]
                if not missing_fields:
                    continue
                target_count += 1
                derived = derive_operational_metadata(
                    row["title"],
                    row["content"],
                    order_id=row["order_id"],
                )
                for field in REPAIR_FIELDS:
                    if field not in missing_fields:
                        if (
                            not _missing(derived.get(field))
                            and raw.get(field) != derived.get(field)
                        ):
                            conflict_count += 1
                        continue
                    value = derived.get(field)
                    if _missing(value):
                        continue
                    raw[field] = value
                    if field == "queue":
                        queue_recoverable += 1
                    elif field == "priority":
                        priority_recoverable += 1
                    else:
                        analysis_recoverable += 1
                if not _missing(derived.get("is_delivery")):
                    raw.setdefault("is_delivery", derived["is_delivery"])
                if not _missing(derived.get("queue_label")):
                    raw.setdefault("queue_label", derived["queue_label"])
                if any(_missing(raw.get(field)) for field in REPAIR_FIELDS):
                    unclassified_count += 1
                else:
                    repaired_count += 1
                repairs.append((serialize_json(raw), int(row["id"])))
            except Exception:
                failed_count += 1

        if not dry_run and repairs:
            with self.database.transaction() as connection:
                connection.executemany(
                    "UPDATE inquiries SET raw_json = ? WHERE id = ?",
                    repairs,
                )
            LogRepository(self.database).record_system(
                "INQUIRY_METADATA_REPAIR_COMPLETED",
                "문의 파생 운영 메타데이터 복구를 완료했습니다.",
                details={
                    "target_count": target_count,
                    "repaired_count": repaired_count,
                    "unclassified_count": unclassified_count,
                    "conflict_count": conflict_count,
                    "failed_count": failed_count,
                },
            )

        after = protected_state_fingerprint(self.database)
        return MetadataRepairResult(
            dry_run=dry_run,
            target_count=target_count,
            queue_recoverable_count=queue_recoverable,
            priority_recoverable_count=priority_recoverable,
            analysis_recoverable_count=analysis_recoverable,
            repaired_count=repaired_count,
            unclassified_count=unclassified_count,
            conflict_count=conflict_count,
            failed_count=failed_count,
            protected_fields_changed=before != after,
        )
