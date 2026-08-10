from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from config import NaverPostSettings
from repositories.database import Database
from services.naver_post_dry_run_service import NaverPostDryRunService


def _schema(database: Database) -> dict[str, list[str]]:
    schema: dict[str, list[str]] = {}
    with database.connection() as connection:
        tables = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND name NOT LIKE 'sqlite_%'
              AND name <> 'schema_migrations'
            ORDER BY name
            """
        ).fetchall()
        for row in tables:
            table = str(row["name"])
            schema[table] = [
                str(column["name"])
                for column in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            ]
    return schema


def _fingerprints(
    database: Database,
    schema: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    with database.connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        for table, columns in schema.items():
            selected = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(
                f'SELECT {selected} FROM "{table}" ORDER BY {selected}'
            ).fetchall()
            digest = hashlib.sha256()
            for row in rows:
                digest.update(
                    json.dumps(
                        dict(row),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                )
                digest.update(b"\n")
            results[table] = {
                "row_count": len(rows),
                "sha256": digest.hexdigest(),
            }
    return results


def _integrity(database: Database) -> str:
    with database.connection() as connection:
        return str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        )


def _copy_dry_run(database: Database) -> str:
    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT i.id
            FROM inquiries i
            JOIN answer_drafts d
              ON d.inquiry_id=i.id AND d.is_active=1
            WHERE i.approval_status='APPROVED'
              AND i.post_status='NOT_POSTED'
              AND COALESCE(d.final_answer, '') <> ''
            ORDER BY i.id
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return "NO_ELIGIBLE_INQUIRY"
    locked = NaverPostSettings(enabled=False)
    result = NaverPostDryRunService(
        database, settings=locked
    ).run(int(row["id"]))
    return f"{result.status}:NETWORK_CALLS=0"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="data/oje_automation.db")
    parser.add_argument("--backup-dir", default="data/backups")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source = Database(args.database)
    before_schema = _schema(source)
    before = _fingerprints(source, before_schema)
    before_versions = source.migration_versions()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = (
        Path(args.backup_dir)
        / f"{Path(args.database).stem}_before_v13_{timestamp}.db"
    )
    source.backup_to(backup_path)

    copy = Database(backup_path)
    copy_applied = copy.initialize()
    copy_after = _fingerprints(copy, before_schema)
    copy_preserved = before == copy_after
    copy_schema = _schema(copy)
    report: dict[str, Any] = {
        "source": str(source.path.resolve()),
        "backup": str(backup_path.resolve()),
        "before_migrations": before_versions,
        "copy_applied_migrations": copy_applied,
        "copy_after_migrations": copy.migration_versions(),
        "backup_integrity": _integrity(copy),
        "copy_migration_preserved": copy_preserved,
        "copy_new_tables": sorted(set(copy_schema) - set(before_schema)),
        "copy_dry_run": _copy_dry_run(copy),
        "protected_table_count": len(before),
        "apply_requested": bool(args.apply),
        "naver_post_enabled_during_validation": False,
    }
    if not copy_preserved:
        report["status"] = "BLOCKED_COPY_FINGERPRINT_MISMATCH"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    if args.apply:
        applied = source.initialize()
        after = _fingerprints(source, before_schema)
        report.update(
            {
                "applied_migrations": applied,
                "after_migrations": source.migration_versions(),
                "source_integrity": _integrity(source),
                "source_fingerprint_preserved": before == after,
                "changed_protected_tables": sorted(
                    table
                    for table in before
                    if before[table] != after.get(table)
                ),
            }
        )
        report["status"] = (
            "PASS"
            if report["source_fingerprint_preserved"]
            and report["source_integrity"] == "ok"
            else "FAILED"
        )
    else:
        report["status"] = "DRY_RUN_ONLY"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"PASS", "DRY_RUN_ONLY"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
