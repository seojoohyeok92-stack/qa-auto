from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from repositories.database import Database


TOKEN = re.compile(r"[가-힣A-Za-z0-9_]{2,}")


class ProjectKnowledgeRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["active"] = bool(value.get("active"))
        return value

    @staticmethod
    def _source_key(category: str, title: str, content: str, source: str) -> str:
        return hashlib.sha256(
            f"{source}|{category}|{title}|{content}".encode("utf-8")
        ).hexdigest()

    def upsert(
        self,
        *,
        category: str,
        title: str,
        content: str,
        source: str,
        active: bool = True,
        source_key: str | None = None,
    ) -> dict[str, Any]:
        clean_category = str(category or "GENERAL").strip()[:80] or "GENERAL"
        clean_title = str(title or "Project Knowledge").strip()[:200]
        clean_content = str(content or "").strip()
        clean_source = str(source or "MANUAL").strip()[:80] or "MANUAL"
        if not clean_content:
            raise ValueError("Project Knowledge content is required.")
        key = source_key or self._source_key(
            clean_category, clean_title, clean_content, clean_source
        )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO project_knowledge(
                    source_key, category, title, content, source, active
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    category=excluded.category,
                    title=excluded.title,
                    content=excluded.content,
                    source=excluded.source,
                    active=excluded.active,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (key, clean_category, clean_title, clean_content, clean_source, int(active)),
            )
            row = connection.execute(
                "SELECT * FROM project_knowledge WHERE source_key=?",
                (key,),
            ).fetchone()
        result = self._row(row)
        assert result is not None
        return result

    def count(self, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM project_knowledge"
        if active_only:
            sql += " WHERE active=1"
        with self.database.connection() as connection:
            return int(connection.execute(sql).fetchone()[0])

    def rows(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM project_knowledge
                ORDER BY active DESC, updated_at DESC, id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def search(self, query: str, *, limit: int = 6) -> list[dict[str, Any]]:
        query_tokens = set(TOKEN.findall(str(query or "").lower()))
        candidates = [row for row in self.rows(limit=500) if row.get("active")]
        ranked: list[tuple[float, dict[str, Any]]] = []
        for row in candidates:
            haystack = " ".join(
                [str(row.get("category") or ""), str(row.get("title") or ""), str(row.get("content") or "")]
            ).lower()
            tokens = set(TOKEN.findall(haystack))
            overlap = len(query_tokens & tokens)
            score = overlap / max(len(query_tokens), 1)
            if overlap or not query_tokens:
                ranked.append((score, row))
        ranked.sort(
            key=lambda item: (item[0], str(item[1].get("updated_at") or "")),
            reverse=True,
        )
        return [row for _, row in ranked[: max(1, min(int(limit), 20))]]

    @staticmethod
    def _chunks(text: str, *, max_chars: int = 3500) -> list[str]:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        chunks: list[str] = []
        buffer = ""
        for paragraph in paragraphs or [text.strip()]:
            if len(paragraph) > max_chars:
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                for start in range(0, len(paragraph), max_chars):
                    chunks.append(paragraph[start:start + max_chars].strip())
                continue
            candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
            if len(candidate) > max_chars and buffer:
                chunks.append(buffer)
                buffer = paragraph
            else:
                buffer = candidate
        if buffer:
            chunks.append(buffer)
        return [chunk for chunk in chunks if chunk]

    def import_text(
        self,
        *,
        title: str,
        text: str,
        source: str = "CHAT_EXPORT_IMPORT",
        category: str = "CHAT_HISTORY",
    ) -> int:
        saved = 0
        for index, chunk in enumerate(self._chunks(str(text or "")), start=1):
            self.upsert(
                category=category,
                title=f"{title} · {index}",
                content=chunk,
                source=source,
            )
            saved += 1
        return saved

    def seed_from_json(self, path: str | Path) -> int:
        source_path = Path(path)
        if not source_path.exists():
            return 0
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        items: Iterable[Any] = payload if isinstance(payload, list) else payload.get("items", []) if isinstance(payload, dict) else []
        saved = 0
        for item in items:
            if not isinstance(item, dict) or not str(item.get("content") or "").strip():
                continue
            self.upsert(
                category=str(item.get("category") or "PROJECT_DECISION"),
                title=str(item.get("title") or "Q&A Auto Project Knowledge"),
                content=str(item.get("content") or ""),
                source=str(item.get("source") or "QNA_AUTO_CHAT_SUMMARY"),
                source_key=str(item.get("source_key") or "") or None,
            )
            saved += 1
        return saved
