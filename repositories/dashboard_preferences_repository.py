from __future__ import annotations

from datetime import UTC, datetime

from repositories.database import Database


class DashboardPreferencesRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def admin_mode(self, username: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT admin_mode FROM dashboard_operator_preferences WHERE username=?",
                (str(username).strip().lower(),),
            ).fetchone()
        return bool(row["admin_mode"]) if row else False

    def save_admin_mode(self, username: str, enabled: bool) -> bool:
        normalized = str(username).strip().lower()
        if not normalized:
            raise ValueError("username is required")
        now = datetime.now(UTC).isoformat(timespec="milliseconds")
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO dashboard_operator_preferences(username, admin_mode, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    admin_mode=excluded.admin_mode,
                    updated_at=excluded.updated_at
                """,
                (normalized, int(bool(enabled)), now),
            )
        return bool(enabled)
