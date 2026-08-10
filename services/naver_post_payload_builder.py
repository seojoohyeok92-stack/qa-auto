from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from config import BASE_URL


MAX_COMMENT_LENGTH = 4_000
MASKED_ID = re.compile(r"[*•●…]|<[^>]+>")


class NaverPostTargetError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class NaverPostTarget:
    """One authoritative Naver identity/transport decision for a post."""

    inquiry_type: str
    store: str
    external_id: str
    endpoint: str
    method: str
    body_field: str
    expected_success_status: int
    endpoint_kind: str
    identifier_name: str
    external_id_source: str
    remote_snapshot_verified: bool


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NaverPostPayload:
    method: str
    endpoint: str
    endpoint_kind: str
    identifier_name: str
    external_id: str
    store: str
    source_type: str
    payload: dict[str, Any]
    final_answer: str
    final_answer_hash: str
    payload_hash: str
    expected_success_status: int = 200
    target_verified: bool = False

    def preview(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "endpoint": self.endpoint,
            self.identifier_name: self.external_id,
            "store": self.store,
            "sourceType": self.source_type,
            "payload": dict(self.payload),
        }


class NaverPostPayloadBuilder:
    """Single source of truth for Dry Run and live request payloads."""

    @staticmethod
    def _canonical_id(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise NaverPostTargetError("EXTERNAL_ID_REQUIRED")
        if MASKED_ID.search(text):
            raise NaverPostTargetError("MASKED_EXTERNAL_ID")
        return text

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    def resolve_target(
        self,
        inquiry: dict[str, Any],
        *,
        require_remote_snapshot: bool = False,
        latest_sync_started_at: str | None = None,
    ) -> NaverPostTarget:
        """Resolve raw source identity once; never infer it from local id/Route."""

        source = str(inquiry.get("source_type") or "").strip().upper()
        store = str(inquiry.get("store_code") or "").strip()
        raw = inquiry.get("raw_json")
        raw_data = raw if isinstance(raw, dict) else {}
        source_payload = raw_data.get("source_payload")
        source_data = (
            source_payload if isinstance(source_payload, dict) else {}
        )
        raw_source = str(raw_data.get("source") or "").strip().upper()
        if raw_source and raw_source != source:
            raise NaverPostTargetError("INQUIRY_TYPE_ENDPOINT_MISMATCH")

        if source == "PRODUCT_INQUIRY":
            if (
                source_data.get("inquiryNo") not in (None, "")
                or raw_data.get("inquiryNo") not in (None, "")
            ):
                raise NaverPostTargetError("INQUIRY_TYPE_ENDPOINT_MISMATCH")
            raw_value = source_data.get("questionId")
            raw_path = "raw_json.source_payload.questionId"
            if raw_value in (None, ""):
                raw_value = raw_data.get("questionId")
                raw_path = "raw_json.questionId"
            method, body_field, expected = "PUT", "commentContent", 204
            endpoint_kind = "PRODUCT_INQUIRY_ANSWER"
            identifier_name = "questionId"
        elif source == "CUSTOMER_INQUIRY":
            if (
                source_data.get("questionId") not in (None, "")
                or raw_data.get("questionId") not in (None, "")
            ):
                raise NaverPostTargetError("INQUIRY_TYPE_ENDPOINT_MISMATCH")
            raw_value = source_data.get("inquiryNo")
            raw_path = "raw_json.source_payload.inquiryNo"
            if raw_value in (None, ""):
                raw_value = raw_data.get("inquiryNo")
                raw_path = "raw_json.inquiryNo"
            if raw_value in (None, ""):
                raw_value = source_data.get("inquiryId")
                raw_path = "raw_json.source_payload.inquiryId"
            if raw_value in (None, ""):
                raw_value = raw_data.get("inquiryId")
                raw_path = "raw_json.inquiryId"
            method, body_field, expected = "POST", "answerComment", 200
            endpoint_kind = "CUSTOMER_INQUIRY_ANSWER"
            identifier_name = "inquiryNo"
        else:
            raise NaverPostTargetError("UNSUPPORTED_SOURCE_TYPE")

        persisted = self._canonical_id(
            inquiry.get("external_inquiry_id")
            or inquiry.get("source_question_id")
        )
        if str(inquiry.get("id") or "").strip() == persisted:
            raise NaverPostTargetError("TARGET_ID_MAPPING_ERROR")
        last_synced_at = self._timestamp(inquiry.get("last_synced_at"))
        latest_sync = self._timestamp(latest_sync_started_at)
        remote_verified = raw_value not in (None, "")
        if latest_sync is not None:
            remote_verified = bool(
                remote_verified
                and last_synced_at is not None
                and last_synced_at >= latest_sync
            )
        if remote_verified:
            raw_external = self._canonical_id(raw_value)
            if raw_external != persisted:
                raise NaverPostTargetError("TARGET_ID_MAPPING_ERROR")
            external_id = raw_external
            external_id_source = raw_path
        else:
            external_id = persisted
            external_id_source = "inquiries.external_inquiry_id"
        if require_remote_snapshot and not remote_verified:
            raise NaverPostTargetError("TARGET_NOT_FOUND")
        if not store:
            raise NaverPostTargetError("STORE_REQUIRED")

        safe_id = quote(external_id, safe="")
        endpoint = (
            f"{BASE_URL}/v1/contents/qnas/{safe_id}"
            if source == "PRODUCT_INQUIRY"
            else f"{BASE_URL}/v1/pay-merchant/inquiries/{safe_id}/answer"
        )
        return NaverPostTarget(
            inquiry_type=source,
            store=store,
            external_id=external_id,
            endpoint=endpoint,
            method=method,
            body_field=body_field,
            expected_success_status=expected,
            endpoint_kind=endpoint_kind,
            identifier_name=identifier_name,
            external_id_source=external_id_source,
            remote_snapshot_verified=remote_verified,
        )

    def build_for_target(
        self, *, target: NaverPostTarget, final_answer: str
    ) -> NaverPostPayload:
        content = str(final_answer or "").replace("\r\n", "\n").replace(
            "\r", "\n"
        ).strip()
        if not content:
            raise ValueError("FINAL_ANSWER_REQUIRED")
        if len(content) > MAX_COMMENT_LENGTH:
            raise ValueError("FINAL_ANSWER_TOO_LONG")
        payload = {target.body_field: content}
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return NaverPostPayload(
            method=target.method,
            endpoint=target.endpoint,
            endpoint_kind=target.endpoint_kind,
            identifier_name=target.identifier_name,
            external_id=target.external_id,
            store=target.store,
            source_type=target.inquiry_type,
            payload=payload,
            final_answer=content,
            final_answer_hash=_sha256_text(content),
            payload_hash=_sha256_text(payload_json),
            expected_success_status=target.expected_success_status,
            target_verified=target.remote_snapshot_verified,
        )

    def build(
        self,
        *,
        source_type: str,
        external_id: str,
        store: str,
        final_answer: str,
    ) -> NaverPostPayload:
        source = str(source_type or "").strip().upper()
        identifier = str(external_id or "").strip()
        store_code = str(store or "").strip()
        # Naver accepts JSON strings. Preserve Unicode/emoji and internal
        # newlines; only normalize transport newlines and outer whitespace.
        content = str(final_answer or "").replace("\r\n", "\n").replace(
            "\r", "\n"
        ).strip()
        if not identifier:
            raise ValueError("EXTERNAL_ID_REQUIRED")
        if not store_code:
            raise ValueError("STORE_REQUIRED")
        if not content:
            raise ValueError("FINAL_ANSWER_REQUIRED")
        if len(content) > MAX_COMMENT_LENGTH:
            raise ValueError("FINAL_ANSWER_TOO_LONG")

        safe_id = quote(identifier, safe="")
        if source == "PRODUCT_INQUIRY":
            method = "PUT"
            endpoint = f"{BASE_URL}/v1/contents/qnas/{safe_id}"
            endpoint_kind = "PRODUCT_INQUIRY_ANSWER"
            identifier_name = "questionId"
            payload = {"commentContent": content}
        elif source == "CUSTOMER_INQUIRY":
            method = "POST"
            endpoint = (
                f"{BASE_URL}/v1/pay-merchant/inquiries/{safe_id}/answer"
            )
            endpoint_kind = "CUSTOMER_INQUIRY_ANSWER"
            identifier_name = "inquiryNo"
            payload = {"answerComment": content}
        else:
            raise ValueError("UNSUPPORTED_SOURCE_TYPE")

        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return NaverPostPayload(
            method=method,
            endpoint=endpoint,
            endpoint_kind=endpoint_kind,
            identifier_name=identifier_name,
            external_id=identifier,
            store=store_code,
            source_type=source,
            payload=payload,
            final_answer=content,
            final_answer_hash=_sha256_text(content),
            payload_hash=_sha256_text(payload_json),
            expected_success_status=204 if source == "PRODUCT_INQUIRY" else 200,
            target_verified=False,
        )

    def build_correction(
        self,
        *,
        source_type: str,
        external_id: str,
        store: str,
        final_answer: str,
        answer_content_id: str | None = None,
    ) -> NaverPostPayload:
        source = str(source_type or "").strip().upper()
        if source == "PRODUCT_INQUIRY":
            return self.build(
                source_type=source,
                external_id=external_id,
                store=store,
                final_answer=final_answer,
            )
        if source != "CUSTOMER_INQUIRY":
            raise ValueError("UNSUPPORTED_SOURCE_TYPE")
        inquiry_id = str(external_id or "").strip()
        answer_id = str(answer_content_id or "").strip()
        store_code = str(store or "").strip()
        content = str(final_answer or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not inquiry_id:
            raise ValueError("EXTERNAL_ID_REQUIRED")
        if not answer_id:
            raise ValueError("ANSWER_CONTENT_ID_REQUIRED")
        if not store_code:
            raise ValueError("STORE_REQUIRED")
        if not content:
            raise ValueError("FINAL_ANSWER_REQUIRED")
        if len(content) > MAX_COMMENT_LENGTH:
            raise ValueError("FINAL_ANSWER_TOO_LONG")
        payload = {"answerComment": content}
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return NaverPostPayload(
            method="PUT",
            endpoint=(
                f"{BASE_URL}/v1/pay-merchant/inquiries/"
                f"{quote(inquiry_id, safe='')}/answer/{quote(answer_id, safe='')}"
            ),
            endpoint_kind="CUSTOMER_INQUIRY_ANSWER_UPDATE",
            identifier_name="inquiryNo",
            external_id=inquiry_id,
            store=store_code,
            source_type=source,
            payload=payload,
            final_answer=content,
            final_answer_hash=_sha256_text(content),
            payload_hash=_sha256_text(payload_json),
            expected_success_status=200,
            target_verified=True,
        )
