from __future__ import annotations

import json
from typing import Any

from repositories.database import Database


class GptChatRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _session_row(row: Any) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _message_row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        try:
            value["metadata_json"] = json.loads(value.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            value["metadata_json"] = {}
        return value

    def create_session(
        self,
        *,
        user_name: str,
        title: str = "새 대화",
        inquiry_id: int | None = None,
    ) -> dict[str, Any]:
        clean_user = str(user_name or "local-user").strip() or "local-user"
        clean_title = str(title or "새 대화").strip()[:120] or "새 대화"
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO gpt_chat_sessions(title, user_name, inquiry_id)
                VALUES (?, ?, ?)
                RETURNING *
                """,
                (clean_title, clean_user, inquiry_id),
            ).fetchone()
        result = self._session_row(row)
        assert result is not None
        return result

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM gpt_chat_sessions WHERE id=?",
                (int(session_id),),
            ).fetchone()
        return self._session_row(row)

    def list_sessions(
        self,
        *,
        user_name: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        sql = "SELECT * FROM gpt_chat_sessions"
        if user_name:
            sql += " WHERE user_name=? COLLATE NOCASE"
            params.append(str(user_name))
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self.database.connection() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def update_title(self, session_id: int, title: str) -> None:
        clean = str(title or "").strip()[:120]
        if not clean:
            return
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE gpt_chat_sessions
                SET title=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id=?
                """,
                (clean, int(session_id)),
            )

    def add_message(
        self,
        *,
        session_id: int,
        role: str,
        content: str,
        inquiry_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in {"user", "assistant", "system"}:
            raise ValueError(f"Unsupported chat role: {role}")
        clean_content = str(content or "").strip()
        if not clean_content:
            raise ValueError("Chat content is required.")
        payload = json.dumps(
            metadata or {}, ensure_ascii=False, separators=(",", ":")
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                INSERT INTO gpt_chat_messages(
                    session_id, role, content, inquiry_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?)
                RETURNING *
                """,
                (int(session_id), normalized_role, clean_content, inquiry_id, payload),
            ).fetchone()
            connection.execute(
                """
                UPDATE gpt_chat_sessions
                SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    inquiry_id=COALESCE(?, inquiry_id)
                WHERE id=?
                """,
                (inquiry_id, int(session_id)),
            )
        result = self._message_row(row)
        assert result is not None
        return result

    def messages(self, session_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT * FROM gpt_chat_messages
                    WHERE session_id=?
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """,
                (int(session_id), max(1, min(int(limit), 300))),
            ).fetchall()
        return [self._message_row(row) for row in rows if row is not None]

    def search_messages(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        tokens = [token for token in str(query or "").split() if len(token) >= 2]
        if not tokens:
            return []
        clauses = " OR ".join("m.content LIKE ?" for _ in tokens[:8])
        params = [f"%{token}%" for token in tokens[:8]]
        params.append(max(1, min(int(limit), 30)))
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT m.*, s.title AS session_title, s.user_name
                FROM gpt_chat_messages m
                JOIN gpt_chat_sessions s ON s.id=m.session_id
                WHERE m.role='assistant' AND ({clauses})
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._message_row(row) for row in rows if row is not None]
