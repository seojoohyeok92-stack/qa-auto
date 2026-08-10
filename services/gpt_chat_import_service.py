from __future__ import annotations

import hashlib
import json
from typing import Any

from repositories.database import Database
from repositories.gpt_chat_repository import GptChatRepository
from repositories.project_knowledge_repository import ProjectKnowledgeRepository
from services.learning_privacy_service import LearningPrivacyService


class GptChatImportService:
    """Idempotent conversations.json/TXT importer for chat history and knowledge."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.chats = GptChatRepository(database)
        self.knowledge = ProjectKnowledgeRepository(database)
        self.privacy = LearningPrivacyService()

    @staticmethod
    def _fingerprint(file_name: str, raw: bytes) -> str:
        return hashlib.sha256(file_name.encode("utf-8") + b"\0" + raw).hexdigest()

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            return ""
        return "\n".join(
            str(part) for part in parts if isinstance(part, (str, int, float))
        ).strip()

    def _parse_json(self, payload: Any) -> list[dict[str, Any]]:
        conversations = payload if isinstance(payload, list) else [payload]
        result: list[dict[str, Any]] = []
        for conversation in conversations:
            if not isinstance(conversation, dict):
                continue
            messages: list[dict[str, str]] = []
            mapping = conversation.get("mapping")
            nodes = list(mapping.values()) if isinstance(mapping, dict) else []
            nodes.sort(key=lambda node: (((node or {}).get("message") or {}).get("create_time") or 0) if isinstance(node, dict) else 0)
            for node in nodes:
                message = node.get("message") if isinstance(node, dict) else None
                if not isinstance(message, dict):
                    continue
                author = message.get("author") or {}
                role = str(author.get("role") or "system").lower() if isinstance(author, dict) else "system"
                if role not in {"user", "assistant", "system"}:
                    role = "system"
                text = self._message_text(message)
                if text:
                    messages.append({"role": role, "content": text})
            if messages:
                result.append({
                    "title": str(conversation.get("title") or "ChatGPT 가져온 대화")[:120],
                    "messages": messages,
                })
        return result

    def import_bytes(
        self, *, file_name: str, raw: bytes, user_name: str,
    ) -> dict[str, Any]:
        fingerprint = self._fingerprint(str(file_name), raw)
        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT * FROM gpt_chat_imports WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
        if existing is not None:
            return {**dict(existing), "duplicate": True, "sessions_created": 0, "messages_created": 0}

        text = raw.decode("utf-8", errors="replace")
        is_json = str(file_name).lower().endswith(".json")
        conversations: list[dict[str, Any]] = []
        if is_json:
            try:
                conversations = self._parse_json(json.loads(text))
            except json.JSONDecodeError:
                conversations = []
        if not conversations:
            conversations = [{
                "title": str(file_name)[:120],
                "messages": [{"role": "assistant", "content": text}],
            }]

        sessions_created = messages_created = 0
        knowledge_texts: list[str] = []
        for conversation in conversations:
            session = self.chats.create_session(
                user_name=str(user_name or "imported-user"),
                title=f"[가져옴] {conversation['title']}",
            )
            sessions_created += 1
            transcript: list[str] = []
            for message in conversation["messages"]:
                masked = self.privacy.mask(message.get("content"))
                if not masked:
                    continue
                self.chats.add_message(
                    session_id=int(session["id"]), role=message["role"],
                    content=masked,
                    metadata={"import_fingerprint": fingerprint, "source_file": str(file_name)},
                )
                messages_created += 1
                transcript.append(f"{message['role']}: {masked}")
            if transcript:
                knowledge_texts.append(
                    f"## {conversation['title']}\n\n" + "\n\n".join(transcript)
                )

        chunks_created = 0
        for index, transcript in enumerate(knowledge_texts, start=1):
            chunks_created += self.knowledge.import_text(
                title=f"{file_name} · 대화 {index}", text=transcript,
                source="CHAT_EXPORT_IMPORT", category="CHAT_HISTORY",
            )
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO gpt_chat_imports(
                    fingerprint, file_name, import_format,
                    conversation_count, knowledge_chunk_count, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint, str(file_name), "JSON" if is_json else "TXT",
                    sessions_created, chunks_created,
                    json.dumps({"messages_created": messages_created}, ensure_ascii=False),
                ),
            )
        return {
            "fingerprint": fingerprint, "duplicate": False,
            "sessions_created": sessions_created,
            "messages_created": messages_created,
            "knowledge_chunk_count": chunks_created,
        }
