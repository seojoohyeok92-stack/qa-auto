from __future__ import annotations

import hashlib
from typing import Any

from answer.answer_provenance import AnswerProvenance
from repositories.database import Database
from repositories.inquiry_repository import deserialize_json, serialize_json, utc_now


class NaverPostedAnswerRepository:
    """Append-only observations of the answer currently visible on Naver."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["metadata_json"] = deserialize_json(result.get("metadata_json"))
        result["is_current"] = bool(result["is_current"])
        return result

    @staticmethod
    def _source_key(
        inquiry_id: int, *, body: str, answer_id: str, fetch_status: str
    ) -> str:
        identity = body if body else answer_id if answer_id else fetch_status
        return hashlib.sha256(
            f"NAVER_POSTED|{int(inquiry_id)}|{identity}".encode("utf-8")
        ).hexdigest()

    def current(self, inquiry_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM naver_posted_answers
                WHERE inquiry_id=? AND is_current=1
                ORDER BY id DESC LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
        return self._row(row)

    def history(self, inquiry_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM naver_posted_answers
                WHERE inquiry_id=? ORDER BY id
                """,
                (int(inquiry_id),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def observe(
        self,
        *,
        inquiry_id: int,
        answer_body: str | None,
        answer_id: str | None = None,
        posted_at: str | None = None,
        author_type: str | None = None,
        source_api: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = str(answer_body or "").strip()
        remote_id = str(answer_id or "").strip()
        fetch_status = "AVAILABLE" if body else "NOT_FETCHED"
        now = utc_now()
        with self.database.transaction() as connection:
            current = connection.execute(
                """
                SELECT * FROM naver_posted_answers
                WHERE inquiry_id=? AND is_current=1
                ORDER BY id DESC LIMIT 1
                """,
                (int(inquiry_id),),
            ).fetchone()
            # A list response may omit its answer body. Never let that weaker
            # observation erase an answer body captured by an earlier sync.
            if (
                not body
                and current is not None
                and str(current["fetch_status"]) == "AVAILABLE"
            ):
                connection.execute(
                    """
                    UPDATE naver_posted_answers
                    SET last_observed_at=?, answer_id=COALESCE(NULLIF(?, ''), answer_id),
                        posted_at=COALESCE(?, posted_at),
                        author_type=COALESCE(?, author_type), metadata_json=?
                    WHERE id=?
                    """,
                    (
                        now, remote_id, posted_at, author_type,
                        serialize_json(metadata or {}), int(current["id"]),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM naver_posted_answers WHERE id=?",
                    (int(current["id"]),),
                ).fetchone()
                result = self._row(row)
                assert result is not None
                return result

            source_key = self._source_key(
                int(inquiry_id), body=body, answer_id=remote_id,
                fetch_status=fetch_status,
            )
            connection.execute(
                "UPDATE naver_posted_answers SET is_current=0 WHERE inquiry_id=?",
                (int(inquiry_id),),
            )
            connection.execute(
                """
                INSERT INTO naver_posted_answers(
                    source_key, inquiry_id, answer_body, fetch_status,
                    answer_id, posted_at, author_type, provenance, source_api,
                    metadata_json, is_current, first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    answer_id=COALESCE(NULLIF(excluded.answer_id, ''), answer_id),
                    posted_at=COALESCE(excluded.posted_at, posted_at),
                    author_type=COALESCE(excluded.author_type, author_type),
                    source_api=excluded.source_api,
                    metadata_json=excluded.metadata_json,
                    is_current=1,
                    last_observed_at=excluded.last_observed_at
                """,
                (
                    source_key, int(inquiry_id), body or None, fetch_status,
                    remote_id or None, posted_at, author_type,
                    AnswerProvenance.NAVER_POSTED.value, str(source_api),
                    serialize_json(metadata or {}), now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM naver_posted_answers WHERE source_key=?",
                (source_key,),
            ).fetchone()
        result = self._row(row)
        assert result is not None
        return result

