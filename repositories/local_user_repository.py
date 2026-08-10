from __future__ import annotations

import uuid
from typing import Any

from repositories.database import Database
from repositories.inquiry_repository import utc_now
from uat.models import UserRole


class LocalUserRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        *,
        username: str,
        display_name: str,
        password_hash: str,
        role: UserRole | str,
        force_password_change: bool = True,
    ) -> dict[str, Any]:
        clean_username = str(username).strip().lower()
        if not clean_username:
            raise ValueError("사용자명이 필요합니다.")
        clean_role = UserRole(role).value
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO local_users (
                    username, display_name, password_hash, role,
                    force_password_change
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    clean_username,
                    str(display_name).strip() or clean_username,
                    password_hash,
                    clean_role,
                    int(force_password_change),
                ),
            )
            user_id = int(cursor.lastrowid)
        result = self.get_by_id(user_id)
        assert result is not None
        return result

    def get_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_users WHERE id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM local_users WHERE username = ? COLLATE NOCASE",
                (str(username).strip(),),
            ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, username, display_name, role, active,
                       force_password_change, password_changed_at,
                       created_at, updated_at
                FROM local_users ORDER BY username
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def update_password(self, user_id: int, password_hash: str) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE local_users
                SET password_hash = ?, force_password_change = 0,
                    password_changed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (password_hash, now, now, user_id),
            )

    def record_login(
        self,
        *,
        username: str,
        success: bool,
        event_code: str,
        reason_code: str | None = None,
        user_id: int | None = None,
        correlation_id: str | None = None,
    ) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO login_audit (
                    user_id, username, event_code, success,
                    reason_code, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    str(username).strip().lower()[:100],
                    str(event_code)[:100],
                    int(success),
                    str(reason_code)[:100] if reason_code else None,
                    correlation_id or str(uuid.uuid4()),
                ),
            )
        return int(cursor.lastrowid)

