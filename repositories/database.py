from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


DB_PATH_ENV = "OJE_AUTOMATION_DB_PATH"
DEFAULT_DB_PATH = Path("data") / "oje_automation.db"
BUSY_TIMEOUT_MS = 5_000


def get_database_path(path: str | os.PathLike[str] | None = None) -> Path:
    configured = path if path is not None else os.getenv(DB_PATH_ENV)
    return Path(configured) if configured else DEFAULT_DB_PATH


INQUIRY_STATUSES_SQL = (
    "'NEW','ANALYZING','ORDER_PENDING','DPS_PENDING','ANSWER_PENDING',"
    "'REVIEW_PENDING','READY_TO_POST','POSTED','NEEDS_ATTENTION','FAILED'"
)
STEP_CODES_SQL = (
    "'INQUIRY_COLLECTED','QUESTION_ANALYZED','ORDER_IDENTIFIED',"
    "'NAVER_ORDER_LOOKUP','DPS_LOOKUP','ANSWER_GENERATED','STAFF_REVIEW',"
    "'NAVER_POST','LEARNING_SAVED'"
)
STEP_STATUSES_SQL = (
    "'PENDING','RUNNING','COMPLETED','NEEDS_REVIEW','FAILED','SKIPPED'"
)


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            f"""
            CREATE TABLE inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_code TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_question_id TEXT NOT NULL,
                inquiry_type TEXT,
                title TEXT,
                content TEXT,
                product_name TEXT,
                option_name TEXT,
                customer_display TEXT,
                order_id TEXT,
                product_order_id TEXT,
                registered_at TEXT,
                workflow_status TEXT NOT NULL DEFAULT 'NEW'
                    CHECK (workflow_status IN ({INQUIRY_STATUSES_SQL})),
                answer_status TEXT NOT NULL DEFAULT 'UNANSWERED',
                post_status TEXT NOT NULL DEFAULT 'NOT_POSTED',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                raw_json TEXT NOT NULL DEFAULT '{{}}',
                UNIQUE(store_code, source_type, source_question_id)
            )
            """,
            f"""
            CREATE TABLE workflow_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                step_code TEXT NOT NULL CHECK (step_code IN ({STEP_CODES_SQL})),
                step_status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (step_status IN ({STEP_STATUSES_SQL})),
                started_at TEXT,
                completed_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK (attempt_count >= 0),
                last_error_code TEXT,
                last_error_message TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{{}}',
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE(inquiry_id, step_code),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER,
                level TEXT NOT NULL,
                event_code TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE dps_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                store_code TEXT NOT NULL,
                order_id TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT,
                success INTEGER NOT NULL DEFAULT 0 CHECK (success IN (0, 1)),
                result_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT,
                error_message TEXT,
                queried_at TEXT NOT NULL,
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE answer_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                program_status TEXT,
                category TEXT,
                reason TEXT,
                provider TEXT,
                original_answer TEXT,
                edited_answer TEXT,
                final_answer TEXT,
                review_status TEXT NOT NULL DEFAULT 'PENDING',
                posted INTEGER NOT NULL DEFAULT 0 CHECK (posted IN (0, 1)),
                posted_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE learning_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                answer_draft_id INTEGER,
                candidate_type TEXT NOT NULL,
                original_answer TEXT,
                edited_answer TEXT,
                candidate_status TEXT NOT NULL DEFAULT 'PENDING',
                exclusion_reason TEXT,
                reviewed_at TEXT,
                applied_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX idx_inquiries_workflow_status
            ON inquiries(workflow_status)
            """,
            """
            CREATE INDEX idx_inquiries_registered_at
            ON inquiries(registered_at)
            """,
            """
            CREATE INDEX idx_inquiries_order_id
            ON inquiries(order_id)
            """,
            """
            CREATE INDEX idx_workflow_steps_inquiry_status
            ON workflow_steps(inquiry_id, step_status)
            """,
            """
            CREATE INDEX idx_activity_logs_inquiry_created
            ON activity_logs(inquiry_id, created_at DESC)
            """,
            """
            CREATE INDEX idx_activity_logs_created
            ON activity_logs(created_at DESC)
            """,
            """
            CREATE INDEX idx_dps_results_store_order
            ON dps_results(store_code, order_id)
            """,
            """
            CREATE INDEX idx_dps_results_expires_at
            ON dps_results(expires_at)
            """,
            """
            CREATE INDEX idx_answer_drafts_inquiry
            ON answer_drafts(inquiry_id)
            """,
            """
            CREATE INDEX idx_learning_candidates_status
            ON learning_candidates(candidate_status)
            """,
        ),
    ),
    (
        2,
        (
            """
            CREATE TABLE dps_lookup_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                order_id TEXT NOT NULL,
                lookup_status TEXT NOT NULL CHECK (
                    lookup_status IN (
                        'NOT_REQUIRED','WAITING_FOR_ORDER_ID','PENDING',
                        'RUNNING','SUCCESS','NOT_FOUND','TIMEOUT',
                        'AGENT_OFFLINE','AUTOMATION_ERROR','PARSE_ERROR',
                        'STALE_CACHE','CANCELLED'
                    )
                ),
                raw_result_json TEXT NOT NULL DEFAULT '{}',
                normalized_result_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT,
                error_message TEXT,
                queried_at TEXT NOT NULL,
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_dps_lookup_results_order
            ON dps_lookup_results(order_id, queried_at DESC)
            """,
            """
            CREATE INDEX idx_dps_lookup_results_inquiry
            ON dps_lookup_results(inquiry_id, queried_at DESC)
            """,
            """
            CREATE INDEX idx_dps_lookup_results_status
            ON dps_lookup_results(lookup_status)
            """,
            """
            CREATE INDEX idx_dps_lookup_results_queried
            ON dps_lookup_results(queried_at DESC)
            """,
        ),
    ),
    (
        3,
        (
            """
            ALTER TABLE inquiries
            ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (approval_status IN ('PENDING','APPROVED','POSTED'))
            """,
            """
            ALTER TABLE inquiries
            ADD COLUMN approved_at TEXT
            """,
            """
            ALTER TABLE inquiries
            ADD COLUMN approved_by TEXT
            """,
            """
            CREATE TABLE approval_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                answer_draft_id INTEGER,
                action TEXT NOT NULL CHECK (
                    action IN (
                        'EDIT_SAVED','APPROVED','APPROVAL_CANCELLED','RESET'
                    )
                ),
                actor TEXT NOT NULL,
                reason TEXT,
                previous_status TEXT,
                new_status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX idx_approval_history_inquiry_created
            ON approval_history(inquiry_id, created_at DESC)
            """,
            """
            CREATE INDEX idx_approval_history_draft
            ON approval_history(answer_draft_id)
            """,
            """
            CREATE INDEX idx_inquiries_approval_status
            ON inquiries(approval_status)
            """,
        ),
    ),
    (
        4,
        (
            """
            ALTER TABLE answer_drafts
            ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'
            """,
            """
            CREATE INDEX idx_answer_drafts_provider_created
            ON answer_drafts(provider, created_at DESC)
            """,
        ),
    ),
    (
        5,
        (
            """
            CREATE TABLE gpt_provider_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER,
                draft_id INTEGER,
                correlation_id TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                model TEXT,
                mode TEXT NOT NULL CHECK (
                    mode IN ('FAKE','SHADOW','CANARY','ACTIVE','DISABLED')
                ),
                prompt_version TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                privacy_policy_version TEXT NOT NULL,
                validator_policy_version TEXT NOT NULL,
                company_tone_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0 CHECK (success IN (0,1)),
                error_type TEXT,
                error_message_masked TEXT,
                input_size INTEGER NOT NULL DEFAULT 0,
                output_size INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                estimated_cost_krw REAL,
                privacy_removed_count INTEGER NOT NULL DEFAULT 0,
                validator_passed INTEGER CHECK (
                    validator_passed IS NULL OR validator_passed IN (0,1)
                ),
                fallback_used INTEGER NOT NULL DEFAULT 0
                    CHECK (fallback_used IN (0,1)),
                retry_count INTEGER NOT NULL DEFAULT 0,
                canary_selected INTEGER NOT NULL DEFAULT 0
                    CHECK (canary_selected IN (0,1)),
                shadow_comparison_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (draft_id) REFERENCES answer_drafts(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX idx_gpt_provider_runs_inquiry_created
            ON gpt_provider_runs(inquiry_id, created_at DESC)
            """,
            """
            CREATE INDEX idx_gpt_provider_runs_mode_created
            ON gpt_provider_runs(mode, created_at DESC)
            """,
            """
            CREATE INDEX idx_gpt_provider_runs_provider_created
            ON gpt_provider_runs(provider, created_at DESC)
            """,
            """
            CREATE INDEX idx_gpt_provider_runs_created
            ON gpt_provider_runs(created_at DESC)
            """,
        ),
    ),
    (
        6,
        (
            """
            CREATE TABLE local_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (
                    role IN ('ADMIN','MANAGER','AGENT')
                ),
                active INTEGER NOT NULL DEFAULT 1
                    CHECK (active IN (0,1)),
                force_password_change INTEGER NOT NULL DEFAULT 1
                    CHECK (force_password_change IN (0,1)),
                password_changed_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            CREATE TABLE login_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT NOT NULL,
                event_code TEXT NOT NULL,
                success INTEGER NOT NULL CHECK (success IN (0,1)),
                reason_code TEXT,
                correlation_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (user_id) REFERENCES local_users(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE quality_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                answer_draft_id INTEGER NOT NULL,
                actor TEXT NOT NULL,
                category TEXT,
                character_change_ratio REAL NOT NULL,
                word_change_ratio REAL NOT NULL,
                sentences_added INTEGER NOT NULL DEFAULT 0,
                sentences_deleted INTEGER NOT NULL DEFAULT 0,
                fact_changed INTEGER NOT NULL DEFAULT 0
                    CHECK (fact_changed IN (0,1)),
                prohibited_expression_changed INTEGER NOT NULL DEFAULT 0
                    CHECK (prohibited_expression_changed IN (0,1)),
                tone_changed INTEGER NOT NULL DEFAULT 0
                    CHECK (tone_changed IN (0,1)),
                edit_duration_seconds INTEGER,
                approved INTEGER NOT NULL DEFAULT 0
                    CHECK (approved IN (0,1)),
                regeneration_count INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE uat_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL UNIQUE,
                actor TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'NORMAL','WARNING','FAILED','NOT_CONFIGURED',
                        'NOT_RUN','BLOCKED'
                    )
                ),
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            CREATE TABLE environment_check_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL UNIQUE,
                actor TEXT NOT NULL,
                status TEXT NOT NULL,
                valid_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            CREATE TABLE env_comparison_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL UNIQUE,
                actor TEXT NOT NULL,
                current_file_name TEXT NOT NULL,
                compared_file_name TEXT NOT NULL,
                status TEXT NOT NULL,
                same_count INTEGER NOT NULL DEFAULT 0,
                different_count INTEGER NOT NULL DEFAULT 0,
                missing_count INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            CREATE INDEX idx_local_users_role_active
            ON local_users(role, active)
            """,
            """
            CREATE INDEX idx_login_audit_user_created
            ON login_audit(user_id, created_at DESC)
            """,
            """
            CREATE INDEX idx_quality_metrics_inquiry_created
            ON quality_metrics(inquiry_id, created_at DESC)
            """,
            """
            CREATE INDEX idx_quality_metrics_category_created
            ON quality_metrics(category, created_at DESC)
            """,
            """
            CREATE INDEX idx_uat_runs_created
            ON uat_runs(created_at DESC)
            """,
        ),
    ),
    (
        7,
        (
            """
            ALTER TABLE inquiries ADD COLUMN order_date TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN order_status TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN order_lookup_at TEXT
            """,
            """
            ALTER TABLE dps_lookup_results ADD COLUMN correlation_id TEXT
            """,
            """
            ALTER TABLE dps_lookup_results ADD COLUMN lookup_started_at TEXT
            """,
            """
            ALTER TABLE dps_lookup_results ADD COLUMN lookup_completed_at TEXT
            """,
            """
            ALTER TABLE dps_lookup_results ADD COLUMN duration_seconds REAL
            """,
            """
            ALTER TABLE dps_lookup_results
            ADD COLUMN cached INTEGER NOT NULL DEFAULT 0
                CHECK (cached IN (0,1))
            """,
            """
            CREATE INDEX idx_dps_lookup_results_correlation
            ON dps_lookup_results(correlation_id)
            """,
        ),
    ),
    (
        8,
        (
            """
            ALTER TABLE dps_lookup_results
            ADD COLUMN required_delivery_date TEXT
            """,
            """
            ALTER TABLE dps_lookup_results
            ADD COLUMN installation_date TEXT
            """,
            """
            ALTER TABLE dps_lookup_results
            ADD COLUMN installation_date_source TEXT
            """,
            """
            ALTER TABLE dps_lookup_results
            ADD COLUMN raw_required_delivery_date TEXT
            """,
            """
            ALTER TABLE dps_lookup_results
            ADD COLUMN date_parse_status TEXT
            """,
        ),
    ),
    (
        9,
        (
            """
            ALTER TABLE answer_drafts ADD COLUMN order_id TEXT
            """,
            """
            ALTER TABLE answer_drafts ADD COLUMN source TEXT
            """,
            """
            ALTER TABLE answer_drafts ADD COLUMN validation_status TEXT
            """,
            """
            ALTER TABLE answer_drafts ADD COLUMN dps_lookup_id INTEGER
            """,
            """
            ALTER TABLE answer_drafts ADD COLUMN prompt_version TEXT
            """,
            """
            ALTER TABLE answer_drafts ADD COLUMN facts_version TEXT
            """,
            """
            ALTER TABLE answer_drafts
            ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0
                CHECK (is_active IN (0,1))
            """,
            """
            ALTER TABLE answer_drafts
            ADD COLUMN stale INTEGER NOT NULL DEFAULT 0
                CHECK (stale IN (0,1))
            """,
            """
            ALTER TABLE answer_drafts ADD COLUMN stale_reason TEXT
            """,
            """
            UPDATE answer_drafts
            SET is_active = 1
            WHERE id IN (
                SELECT MAX(id) FROM answer_drafts GROUP BY inquiry_id
            )
            """,
            """
            CREATE INDEX idx_answer_drafts_inquiry_active
            ON answer_drafts(inquiry_id, is_active, created_at DESC)
            """,
            """
            CREATE INDEX idx_answer_drafts_dps_lookup
            ON answer_drafts(dps_lookup_id)
            """,
        ),
    ),
    (
        10,
        (
            """
            ALTER TABLE inquiries
            ADD COLUMN is_private INTEGER
                CHECK (is_private IS NULL OR is_private IN (0,1))
            """,
            """
            ALTER TABLE inquiries
            ADD COLUMN source_metadata_json TEXT NOT NULL DEFAULT '{}'
            """,
            """
            ALTER TABLE inquiries
            ADD COLUMN phase9_status TEXT
                CHECK (
                    phase9_status IS NULL OR phase9_status IN (
                        'ORDER_INFO_REQUIRED','INFORMATION_REQUIRED',
                        'READY_FOR_REVIEW','MANUAL_REVIEW_REQUIRED',
                        'VALIDATION_BLOCKED'
                    )
                )
            """,
            """
            ALTER TABLE answer_drafts ADD COLUMN answer_strategy TEXT
            """,
            """
            ALTER TABLE answer_drafts
            ADD COLUMN inquiry_analysis_json TEXT NOT NULL DEFAULT '{}'
            """,
            """
            ALTER TABLE answer_drafts
            ADD COLUMN selected_facts_json TEXT NOT NULL DEFAULT '{}'
            """,
            """
            ALTER TABLE answer_drafts
            ADD COLUMN validator_result_json TEXT NOT NULL DEFAULT '{}'
            """,
            """
            CREATE INDEX idx_inquiries_phase9_status
            ON inquiries(phase9_status)
            """,
            """
            CREATE INDEX idx_answer_drafts_answer_strategy
            ON answer_drafts(answer_strategy, created_at DESC)
            """,
        ),
    ),
    (
        11,
        (
            """
            ALTER TABLE inquiries ADD COLUMN external_inquiry_id TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN product_id TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN masked_writer_id TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN source_answered INTEGER
                CHECK (
                    source_answered IS NULL
                    OR source_answered IN (0, 1)
                )
            """,
            """
            ALTER TABLE inquiries ADD COLUMN source_status TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN source_created_at TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN source_updated_at TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN last_synced_at TEXT
            """,
            """
            ALTER TABLE inquiries
            ADD COLUMN source_content_changed INTEGER NOT NULL DEFAULT 0
                CHECK (source_content_changed IN (0, 1))
            """,
            """
            UPDATE inquiries
            SET external_inquiry_id = source_question_id,
                source_created_at = registered_at
            WHERE external_inquiry_id IS NULL
            """,
            """
            CREATE UNIQUE INDEX
            idx_inquiries_external_identity
            ON inquiries(store_code, source_type, external_inquiry_id)
            WHERE external_inquiry_id IS NOT NULL
            """,
            """
            CREATE INDEX idx_inquiries_last_synced
            ON inquiries(last_synced_at DESC)
            """,
            """
            CREATE TABLE naver_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_id TEXT NOT NULL UNIQUE,
                store_id TEXT,
                inquiry_type TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('RUNNING','SUCCESS','PARTIAL_SYNC','FAILED')
                ),
                requested_from TEXT NOT NULL,
                requested_to TEXT NOT NULL,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            CREATE INDEX idx_naver_sync_runs_completed
            ON naver_sync_runs(status, completed_at DESC)
            """,
            """
            CREATE TABLE naver_sync_locks (
                store_id TEXT PRIMARY KEY,
                sync_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """,
        ),
    ),
    (
        12,
        (
            """
            ALTER TABLE naver_sync_locks
            ADD COLUMN sync_type TEXT NOT NULL DEFAULT 'MANUAL'
            """,
            """
            ALTER TABLE naver_sync_locks
            ADD COLUMN owner_id TEXT
            """,
            """
            ALTER TABLE naver_sync_locks
            ADD COLUMN status TEXT NOT NULL DEFAULT 'RUNNING'
            """,
            """
            CREATE TABLE naver_auto_sync_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (enabled IN (0, 1)),
                interval_minutes INTEGER NOT NULL DEFAULT 10
                    CHECK (interval_minutes IN (5,10,15,30,60)),
                updated_at TEXT
            )
            """,
            """
            INSERT OR IGNORE INTO naver_auto_sync_settings(
                id, enabled, interval_minutes, updated_at
            ) VALUES (1, 0, 10, NULL)
            """,
            """
            CREATE TABLE naver_auto_sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL DEFAULT 'STOPPED',
                owner_id TEXT,
                owner_pid INTEGER,
                lease_expires_at TEXT,
                last_started_at TEXT,
                last_completed_at TEXT,
                last_success_at TEXT,
                next_run_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                sync_id TEXT,
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            INSERT OR IGNORE INTO naver_auto_sync_state(id)
            VALUES (1)
            """,
        ),
    ),
    (
        13,
        (
            """
            CREATE TABLE naver_sync_runs_v13 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_id TEXT NOT NULL UNIQUE,
                store_id TEXT,
                inquiry_type TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'RUNNING','SUCCESS','PARTIAL_SYNC','FAILED','SKIPPED'
                    )
                ),
                requested_from TEXT NOT NULL,
                requested_to TEXT NOT NULL,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            INSERT INTO naver_sync_runs_v13
            SELECT * FROM naver_sync_runs
            """,
            """
            DROP TABLE naver_sync_runs
            """,
            """
            ALTER TABLE naver_sync_runs_v13 RENAME TO naver_sync_runs
            """,
            """
            CREATE INDEX idx_naver_sync_runs_completed
            ON naver_sync_runs(status, completed_at DESC)
            """,
        ),
    ),
    (
        14,
        (
            """
            ALTER TABLE inquiries ADD COLUMN posted_at TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN post_attempted_at TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN post_error_code TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN post_error_message TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN post_http_status INTEGER
            """,
            """
            ALTER TABLE inquiries ADD COLUMN post_response_id TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN posted_answer_hash TEXT
            """,
            """
            ALTER TABLE inquiries ADD COLUMN posted_draft_id INTEGER
            """,
            """
            ALTER TABLE inquiries ADD COLUMN post_actor TEXT
            """,
            """
            CREATE TABLE naver_post_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                answer_draft_id INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                external_id TEXT NOT NULL,
                store_code TEXT NOT NULL,
                source_type TEXT NOT NULL,
                method TEXT NOT NULL,
                endpoint_kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'POSTING','POSTED','POST_FAILED','POST_UNKNOWN',
                        'ALREADY_ANSWERED'
                    )
                ),
                final_answer_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                actor TEXT NOT NULL,
                http_status INTEGER,
                response_id TEXT,
                error_code TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE RESTRICT
            )
            """,
            """
            CREATE UNIQUE INDEX idx_naver_post_attempt_active_inquiry
            ON naver_post_attempts(inquiry_id)
            WHERE status='POSTING'
            """,
            """
            CREATE INDEX idx_naver_post_attempt_external
            ON naver_post_attempts(store_code, external_id, status)
            """,
            """
            CREATE INDEX idx_naver_post_attempt_recent
            ON naver_post_attempts(inquiry_id, started_at DESC)
            """,
        ),
    ),
    (
        15,
        (
            """
            CREATE TABLE learning_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                inquiry_id INTEGER,
                answer_draft_id INTEGER,
                approval_history_id INTEGER,
                learning_source TEXT NOT NULL CHECK (
                    learning_source IN (
                        'APPROVED_EDITED','APPROVED_UNEDITED',
                        'SELLER_ANSWER','TEMPLATE'
                    )
                ),
                question_original_masked TEXT NOT NULL,
                question_normalized TEXT NOT NULL,
                store_code TEXT,
                inquiry_type TEXT,
                intent TEXT,
                product_name TEXT,
                model_code TEXT,
                generation_mode TEXT,
                template_id TEXT,
                processing_route TEXT,
                validator_result TEXT,
                seller_answer TEXT,
                gpt_draft TEXT,
                edited_answer TEXT,
                final_answer TEXT NOT NULL,
                posted INTEGER NOT NULL DEFAULT 0 CHECK (posted IN (0,1)),
                posted_at TEXT,
                auto_posted INTEGER NOT NULL DEFAULT 0
                    CHECK (auto_posted IN (0,1)),
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                edit_ratio REAL NOT NULL DEFAULT 0
                    CHECK (edit_ratio BETWEEN 0 AND 1),
                quality_score REAL NOT NULL DEFAULT 0
                    CHECK (quality_score BETWEEN 0 AND 1),
                style_only INTEGER NOT NULL DEFAULT 0
                    CHECK (style_only IN (0,1)),
                version INTEGER NOT NULL DEFAULT 1,
                style_features_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (approval_history_id) REFERENCES approval_history(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX idx_learning_examples_search
            ON learning_examples(
                active, style_only, store_code, intent, inquiry_type,
                rating DESC, created_at DESC
            )
            """,
            """
            CREATE INDEX idx_learning_examples_product
            ON learning_examples(product_name, model_code, active)
            """,
            """
            CREATE INDEX idx_learning_examples_inquiry
            ON learning_examples(inquiry_id, created_at DESC)
            """,
        ),
    ),
    (
        16,
        (
            """
            CREATE TABLE naver_auto_post_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
                interval_minutes INTEGER NOT NULL DEFAULT 10 CHECK (
                    interval_minutes IN (5,10,15,30,60)
                ),
                max_retries INTEGER NOT NULL DEFAULT 1 CHECK (
                    max_retries BETWEEN 0 AND 10
                ),
                updated_at TEXT
            )
            """,
            """
            INSERT INTO naver_auto_post_settings(
                id, enabled, interval_minutes, max_retries
            ) VALUES (1, 0, 10, 1)
            """,
            """
            CREATE TABLE naver_auto_post_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT NOT NULL DEFAULT 'STOPPED',
                owner_id TEXT,
                owner_pid INTEGER,
                lease_expires_at TEXT,
                last_started_at TEXT,
                last_completed_at TEXT,
                last_success_at TEXT,
                next_run_at TEXT,
                processed_count INTEGER NOT NULL DEFAULT 0,
                succeeded_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                unknown_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                correction_count INTEGER NOT NULL DEFAULT 0,
                run_id TEXT,
                error_code TEXT,
                error_message TEXT,
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            INSERT INTO naver_auto_post_state(id) VALUES (1)
            """,
            """
            CREATE TABLE naver_auto_post_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('RUNNING','SUCCESS','PARTIAL','FAILED','SKIPPED')
                ),
                processed_count INTEGER NOT NULL DEFAULT 0,
                succeeded_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                unknown_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            CREATE TABLE naver_auto_post_locks (
                inquiry_id INTEGER PRIMARY KEY,
                store_code TEXT NOT NULL,
                external_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE(store_code, external_id),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE
            )
            """,
            """
            ALTER TABLE naver_post_attempts ADD COLUMN auto_post_run_id TEXT
            """,
            """
            CREATE TABLE answer_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                answer_draft_id INTEGER,
                version_number INTEGER NOT NULL,
                version_kind TEXT NOT NULL CHECK (
                    version_kind IN (
                        'AUTO_POST_INITIAL','STAFF_CORRECTION_DRAFT',
                        'NAVER_CORRECTION_APPLIED','REVIEWED_NO_CHANGE'
                    )
                ),
                answer_body TEXT NOT NULL,
                actor TEXT NOT NULL,
                author_type TEXT NOT NULL CHECK (
                    author_type IN ('SYSTEM_AUTO_POST','STAFF')
                ),
                route TEXT,
                generation_mode TEXT,
                finalization_source TEXT NOT NULL,
                approval_status TEXT NOT NULL,
                naver_status TEXT NOT NULL,
                previous_version_id INTEGER,
                answer_hash TEXT NOT NULL,
                posted_at TEXT,
                modified_at TEXT,
                learning_saved INTEGER NOT NULL DEFAULT 0
                    CHECK (learning_saved IN (0,1)),
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE(inquiry_id, version_number),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (previous_version_id) REFERENCES answer_versions(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX idx_answer_versions_inquiry
            ON answer_versions(inquiry_id, version_number DESC)
            """,
            """
            CREATE TABLE post_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL UNIQUE,
                answer_draft_id INTEGER,
                initial_version_id INTEGER,
                current_version_id INTEGER,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'AUTO_POSTED_UNREVIEWED','REVIEWED_NO_CHANGE',
                        'CORRECTION_REQUIRED','CORRECTION_PENDING',
                        'CORRECTED_AND_REPOSTED','CORRECTION_FAILED',
                        'POST_FAILED','POST_UNKNOWN'
                    )
                ),
                needs_staff_review INTEGER NOT NULL DEFAULT 0
                    CHECK (needs_staff_review IN (0,1)),
                route TEXT,
                priority INTEGER NOT NULL DEFAULT 0,
                auto_post_run_id TEXT,
                reviewed_by TEXT,
                reviewed_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (initial_version_id) REFERENCES answer_versions(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (current_version_id) REFERENCES answer_versions(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX idx_post_reviews_queue
            ON post_reviews(status, priority DESC, updated_at DESC)
            """,
            """
            CREATE TABLE post_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL,
                proposed_version_id INTEGER NOT NULL,
                applied_version_id INTEGER,
                status TEXT NOT NULL CHECK (
                    status IN ('PENDING','POSTING','SUCCEEDED','FAILED','UNKNOWN')
                ),
                actor TEXT NOT NULL,
                answer_content_id TEXT,
                payload_hash TEXT,
                http_status INTEGER,
                response_id TEXT,
                error_code TEXT,
                error_message TEXT,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (proposed_version_id) REFERENCES answer_versions(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (applied_version_id) REFERENCES answer_versions(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE UNIQUE INDEX idx_post_correction_active
            ON post_corrections(inquiry_id)
            WHERE status IN ('PENDING','POSTING','UNKNOWN')
            """,
            """
            ALTER TABLE learning_examples RENAME TO learning_examples_v15
            """,
            """
            CREATE TABLE learning_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                inquiry_id INTEGER,
                answer_draft_id INTEGER,
                approval_history_id INTEGER,
                learning_source TEXT NOT NULL CHECK (
                    learning_source IN (
                        'AUTO_POST_CORRECTED','APPROVED_EDITED',
                        'AUTO_POST_REVIEWED_NO_CHANGE','APPROVED_UNEDITED',
                        'SELLER_ANSWER','TEMPLATE'
                    )
                ),
                question_original_masked TEXT NOT NULL,
                question_normalized TEXT NOT NULL,
                store_code TEXT,
                inquiry_type TEXT,
                intent TEXT,
                product_name TEXT,
                model_code TEXT,
                generation_mode TEXT,
                template_id TEXT,
                processing_route TEXT,
                validator_result TEXT,
                seller_answer TEXT,
                gpt_draft TEXT,
                edited_answer TEXT,
                final_answer TEXT NOT NULL,
                posted INTEGER NOT NULL DEFAULT 0 CHECK (posted IN (0,1)),
                posted_at TEXT,
                auto_posted INTEGER NOT NULL DEFAULT 0 CHECK (auto_posted IN (0,1)),
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                edit_ratio REAL NOT NULL DEFAULT 0 CHECK (edit_ratio BETWEEN 0 AND 1),
                quality_score REAL NOT NULL DEFAULT 0 CHECK (quality_score BETWEEN 0 AND 1),
                style_only INTEGER NOT NULL DEFAULT 0 CHECK (style_only IN (0,1)),
                version INTEGER NOT NULL DEFAULT 1,
                style_features_json TEXT NOT NULL DEFAULT '{}',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                usage_count INTEGER NOT NULL DEFAULT 0 CHECK (usage_count >= 0),
                last_used_at TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id) ON DELETE SET NULL,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id) ON DELETE SET NULL,
                FOREIGN KEY (approval_history_id) REFERENCES approval_history(id) ON DELETE SET NULL
            )
            """,
            """
            INSERT INTO learning_examples(
                id, source_key, inquiry_id, answer_draft_id, approval_history_id,
                learning_source, question_original_masked, question_normalized,
                store_code, inquiry_type, intent, product_name, model_code,
                generation_mode, template_id, processing_route, validator_result,
                seller_answer, gpt_draft, edited_answer, final_answer, posted,
                posted_at, auto_posted, rating, edit_ratio, quality_score,
                style_only, version, style_features_json, metadata_json, active,
                usage_count, last_used_at, created_at, updated_at
            )
            SELECT id, source_key, inquiry_id, answer_draft_id, approval_history_id,
                learning_source, question_original_masked, question_normalized,
                store_code, inquiry_type, intent, product_name, model_code,
                generation_mode, template_id, processing_route, validator_result,
                seller_answer, gpt_draft, edited_answer, final_answer, posted,
                posted_at, auto_posted, rating, edit_ratio, quality_score,
                style_only, version, style_features_json, metadata_json, active,
                0, NULL, created_at, updated_at
            FROM learning_examples_v15
            """,
            """
            DROP TABLE learning_examples_v15
            """,
            """
            CREATE INDEX idx_learning_examples_search
            ON learning_examples(
                active, style_only, store_code, intent, inquiry_type,
                rating DESC, created_at DESC
            )
            """,
            """
            CREATE INDEX idx_learning_examples_product
            ON learning_examples(product_name, model_code, active)
            """,
            """
            CREATE INDEX idx_learning_examples_inquiry
            ON learning_examples(inquiry_id, created_at DESC)
            """,
        ),
    ),
    (
        17,
        (
            """
            ALTER TABLE naver_post_attempts
            ADD COLUMN retry_of_attempt_id INTEGER
                REFERENCES naver_post_attempts(id) ON DELETE SET NULL
            """,
            """
            CREATE INDEX idx_naver_post_attempt_retry
            ON naver_post_attempts(retry_of_attempt_id)
            """,
        ),
    ),
    (
        18,
        (
            """
            ALTER TABLE naver_auto_post_settings
            ADD COLUMN enabled_at TEXT
            """,
            """
            ALTER TABLE naver_auto_post_settings
            ADD COLUMN pause_reason TEXT
            """,
            """
            ALTER TABLE naver_auto_post_settings
            ADD COLUMN allow_existing_pending INTEGER NOT NULL DEFAULT 0
                CHECK (allow_existing_pending IN (0,1))
            """,
            """
            CREATE TABLE auto_sync_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER NOT NULL UNIQUE,
                store_code TEXT NOT NULL,
                external_id TEXT NOT NULL,
                source_sync_id TEXT,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'PENDING','PROCESSING','COMPLETED','FAILED',
                        'RETRY_BY_SCHEDULER','BLOCKED_AUTO_POST_OFF'
                    )
                ),
                attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK (attempt_count >= 0),
                claimed_by TEXT,
                lease_expires_at TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                processing_started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                UNIQUE(store_code, external_id),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX idx_auto_sync_events_queue
            ON auto_sync_events(status, created_at, id)
            """,
            """
            CREATE INDEX idx_auto_sync_events_lease
            ON auto_sync_events(status, lease_expires_at)
            """,
            """
            CREATE TABLE dashboard_operator_preferences (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                admin_mode INTEGER NOT NULL DEFAULT 0
                    CHECK (admin_mode IN (0,1)),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
        ),
    ),
    (
        19,
        (
            """
            CREATE TABLE gpt_chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                user_name TEXT NOT NULL,
                inquiry_id INTEGER,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX idx_gpt_chat_sessions_recent
            ON gpt_chat_sessions(user_name, updated_at DESC, id DESC)
            """,
            """
            CREATE TABLE gpt_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
                content TEXT NOT NULL,
                inquiry_id INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (session_id) REFERENCES gpt_chat_sessions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX idx_gpt_chat_messages_session
            ON gpt_chat_messages(session_id, id)
            """,
            """
            CREATE INDEX idx_gpt_chat_messages_inquiry
            ON gpt_chat_messages(inquiry_id, created_at DESC)
            """,
            """
            CREATE TABLE project_knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            CREATE INDEX idx_project_knowledge_active
            ON project_knowledge(active, category, updated_at DESC)
            """,
        ),
    ),
    (
        20,
        (
            """
            CREATE TABLE IF NOT EXISTS historical_import_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_key TEXT NOT NULL UNIQUE,
                source_mode TEXT NOT NULL CHECK (
                    source_mode IN ('LOCAL_DB','NAVER_API')
                ),
                store_code TEXT,
                inquiry_types TEXT NOT NULL DEFAULT '[]',
                date_from TEXT,
                date_to TEXT,
                answered_only INTEGER NOT NULL DEFAULT 1
                    CHECK (answered_only IN (0,1)),
                require_seller_answer INTEGER NOT NULL DEFAULT 1
                    CHECK (require_seller_answer IN (0,1)),
                status TEXT NOT NULL CHECK (
                    status IN ('RUNNING','COMPLETED','PARTIAL','FAILED')
                ),
                total_fetched INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                no_answer_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                last_page INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                started_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                completed_at TEXT,
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_historical_import_runs_recent
            ON historical_import_runs(started_at DESC, id DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS historical_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'NAVER_HISTORY',
                store_code TEXT NOT NULL,
                inquiry_id INTEGER,
                external_inquiry_id TEXT NOT NULL,
                inquiry_type TEXT NOT NULL,
                question TEXT NOT NULL,
                question_normalized TEXT NOT NULL,
                seller_answer TEXT,
                product_name TEXT,
                product_id TEXT,
                order_reference TEXT,
                source_answered INTEGER NOT NULL DEFAULT 0
                    CHECK (source_answered IN (0,1)),
                inquiry_created_at TEXT,
                answer_updated_at TEXT,
                imported_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                source_payload_reference TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                classification TEXT,
                policy_risk TEXT NOT NULL DEFAULT 'NONE',
                quality_score REAL NOT NULL DEFAULT 0
                    CHECK (quality_score BETWEEN 0 AND 1),
                confidence REAL NOT NULL DEFAULT 0
                    CHECK (confidence BETWEEN 0 AND 1),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                promoted_learning_id INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                case_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (promoted_learning_id) REFERENCES learning_examples(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_historical_cases_search
            ON historical_cases(active, store_code, inquiry_type,
                quality_score DESC, inquiry_created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_historical_cases_external
            ON historical_cases(store_code, inquiry_type, external_inquiry_id)
            """,
            """
            CREATE TABLE IF NOT EXISTS historical_case_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                historical_case_id INTEGER NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                seller_answer TEXT,
                quality_score REAL NOT NULL DEFAULT 0,
                policy_risk TEXT NOT NULL DEFAULT 'NONE',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                captured_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (historical_case_id) REFERENCES historical_cases(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_historical_case_versions_case
            ON historical_case_versions(historical_case_id, id DESC)
            """,
            """
            CREATE TABLE IF NOT EXISTS gpt_chat_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL,
                import_format TEXT NOT NULL,
                conversation_count INTEGER NOT NULL DEFAULT 0,
                knowledge_chunk_count INTEGER NOT NULL DEFAULT 0,
                imported_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_gpt_chat_imports_recent
            ON gpt_chat_imports(imported_at DESC, id DESC)
            """,
        ),
    ),
    (
        21,
        (
            """
            CREATE TABLE IF NOT EXISTS answer_learning_provenance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_run_id TEXT NOT NULL,
                inquiry_id INTEGER NOT NULL,
                answer_draft_id INTEGER,
                reference_kind TEXT NOT NULL CHECK (
                    reference_kind IN ('LEARNING','HISTORICAL')
                ),
                learning_example_id INTEGER,
                historical_case_id INTEGER,
                source_label TEXT NOT NULL,
                relevance REAL,
                included_in_prompt INTEGER NOT NULL DEFAULT 1
                    CHECK (included_in_prompt IN (0,1)),
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                CHECK (
                    (reference_kind='LEARNING' AND learning_example_id IS NOT NULL
                        AND historical_case_id IS NULL)
                    OR
                    (reference_kind='HISTORICAL' AND historical_case_id IS NOT NULL
                        AND learning_example_id IS NULL)
                ),
                UNIQUE(context_run_id, reference_kind,
                    learning_example_id, historical_case_id),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (learning_example_id) REFERENCES learning_examples(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (historical_case_id) REFERENCES historical_cases(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_answer_learning_provenance_inquiry
            ON answer_learning_provenance(inquiry_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_answer_learning_provenance_draft
            ON answer_learning_provenance(answer_draft_id, reference_kind)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_answer_learning_provenance_learning
            ON answer_learning_provenance(learning_example_id, created_at DESC)
            """,
        ),
    ),
    (
        22,
        (
            """
            CREATE TABLE IF NOT EXISTS learning_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                feedback_type TEXT NOT NULL CHECK (
                    feedback_type IN ('STAFF_CORRECTION','HISTORICAL_REVIEW')
                ),
                correction_reason TEXT NOT NULL,
                correction_note TEXT,
                corrected_intent TEXT,
                learning_signal_type TEXT NOT NULL CHECK (
                    learning_signal_type IN (
                        'NEGATIVE','INTENT_CORRECTION','EXCLUDED'
                    )
                ),
                source TEXT NOT NULL,
                inquiry_id INTEGER,
                answer_draft_id INTEGER,
                historical_case_id INTEGER,
                question_masked TEXT,
                original_answer_masked TEXT,
                corrected_answer_masked TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
                created_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at TEXT NOT NULL DEFAULT
                    (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (answer_draft_id) REFERENCES answer_drafts(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (historical_case_id) REFERENCES historical_cases(id)
                    ON DELETE SET NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_learning_feedback_signal
            ON learning_feedback(
                learning_signal_type, active, correction_reason, created_at DESC
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_learning_feedback_inquiry
            ON learning_feedback(inquiry_id, answer_draft_id, created_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_learning_feedback_historical
            ON learning_feedback(historical_case_id, created_at DESC)
            """,
        ),
    ),
    (
        23,
        (
            """
            CREATE TABLE IF NOT EXISTS naver_posted_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                inquiry_id INTEGER NOT NULL,
                answer_body TEXT,
                fetch_status TEXT NOT NULL CHECK (
                    fetch_status IN ('AVAILABLE','NOT_FETCHED')
                ),
                answer_id TEXT,
                posted_at TEXT,
                author_type TEXT,
                provenance TEXT NOT NULL DEFAULT 'NAVER_POSTED' CHECK (
                    provenance='NAVER_POSTED'
                ),
                source_api TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0,1)),
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                FOREIGN KEY (inquiry_id) REFERENCES inquiries(id)
                    ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_naver_posted_answers_current
            ON naver_posted_answers(inquiry_id, is_current, id DESC)
            """,
            """
            ALTER TABLE learning_feedback
            ADD COLUMN original_answer_source TEXT
            """,
            """
            ALTER TABLE learning_feedback
            ADD COLUMN original_answer_reference_id INTEGER
            """,
        ),
    ),
    (
        24,
        (
            """
            ALTER TABLE learning_examples
            ADD COLUMN validity_type TEXT NOT NULL DEFAULT 'PERMANENT'
                CHECK (validity_type IN ('PERMANENT','TEMPORARY'))
            """,
            """
            ALTER TABLE learning_examples ADD COLUMN event_name TEXT
            """,
            """
            ALTER TABLE learning_examples ADD COLUMN valid_from TEXT
            """,
            """
            ALTER TABLE learning_examples ADD COLUMN valid_until TEXT
            """,
            """
            ALTER TABLE learning_examples
            ADD COLUMN validity_active INTEGER NOT NULL DEFAULT 1
                CHECK (validity_active IN (0,1))
            """,
            """
            ALTER TABLE learning_examples ADD COLUMN expired_at TEXT
            """,
            """
            ALTER TABLE learning_examples ADD COLUMN validity_note TEXT
            """,
            """
            ALTER TABLE learning_examples
            ADD COLUMN condition_json TEXT NOT NULL DEFAULT '{}'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_learning_examples_validity
            ON learning_examples(
                active, validity_active, validity_type, valid_from, valid_until
            )
            """,
        ),
    ),
)


class Database:
    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = get_database_path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> list[int]:
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )

        applied_now: list[int] = []
        for version, statements in MIGRATIONS:
            with self.transaction() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                if exists:
                    continue
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)",
                    (version,),
                )
                applied_now.append(version)
        return applied_now

    def migration_versions(self) -> list[int]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        return [int(row["version"]) for row in rows]

    def backup_to(self, destination: str | os.PathLike[str]) -> Path:
        """Create a transactionally consistent copy with SQLite Backup API."""

        target_path = Path(destination)
        source_resolved = self.path.resolve()
        target_resolved = target_path.resolve()
        if source_resolved == target_resolved:
            raise ValueError("Backup destination must differ from source.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect()
        target = sqlite3.connect(str(target_path))
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
        return target_path

    def health(self) -> dict[str, Any]:
        try:
            with self.connection() as connection:
                connection.execute("SELECT 1").fetchone()
                journal_mode = connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
                foreign_keys = bool(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0]
                )
                migration_count = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations"
                ).fetchone()[0]
            return {
                "ok": True,
                "status": "정상",
                "path": str(self.path.resolve()),
                "journal_mode": str(journal_mode).upper(),
                "foreign_keys": foreign_keys,
                "migration_count": int(migration_count),
            }
        except Exception as error:
            return {
                "ok": False,
                "status": "오류",
                "path": str(self.path.resolve()),
                "error": error.__class__.__name__,
            }
