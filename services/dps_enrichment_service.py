from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

from answer.models import AnswerRequest
from repositories.database import Database
from repositories.answer_repository import AnswerRepository
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.workflow_repository import WorkflowRepository
from services.dps_agent_client import lookup_dps_order
from services.dps_lookup_policy import (
    DpsLookupDecision,
    DpsLookupPolicy,
    DpsLookupStatus,
    DpsSettings,
)
from services.dps_result_normalizer import (
    normalize_dps_result,
    sanitize_raw_result,
    user_message_for_status,
)
from workflow.models import InquiryStatus, StepCode, StepStatus


LOGGER = logging.getLogger(__name__)
DpsClient = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class DpsEnrichmentOutcome:
    decision: DpsLookupDecision
    metadata: dict[str, Any]
    lookup_row: dict[str, Any] | None = None


class DpsEnrichmentService:
    def __init__(
        self,
        database: Database,
        *,
        client: DpsClient | None = None,
        policy: DpsLookupPolicy | None = None,
        settings: DpsSettings | None = None,
    ) -> None:
        self.database = database
        self.client = client or lookup_dps_order
        self.policy = policy or DpsLookupPolicy()
        self.settings = settings or DpsSettings.from_environment()
        self.dps = DpsRepository(database)
        self.answers = AnswerRepository(database)
        self.inquiries = InquiryRepository(database)
        self.workflows = WorkflowRepository(database)
        self.logs = LogRepository(database)

    @staticmethod
    def _base_metadata(
        decision: DpsLookupDecision,
    ) -> dict[str, Any]:
        return {
            "lookup_required": decision.lookup_required,
            "lookup_status": decision.status.value,
            "source": None,
            "order_id": decision.order_id,
            "sales_number": None,
            "delivery_status": None,
            "installation_status": None,
            "required_delivery_date": None,
            "installation_date": None,
            "installation_date_source": None,
            "raw_required_delivery_date": None,
            "date_parse_status": None,
            "requires_human_review": False,
            "required_delivery_date_row_count": 0,
            "installation_time_text": None,
            "installation_type": None,
            "product_name": None,
            "customer_region": None,
            "queried_at": None,
            "cache_used": False,
            "cache_age_seconds": 0,
            "elapsed_seconds": 0.0,
            "error_code": None,
            "error_message": None,
            "warnings": [],
            "change_request": decision.change_request,
            "general_segments": list(decision.general_segments),
            "dps_segments": list(decision.dps_segments),
        }

    def skip_for_phase9(
        self,
        request: AnswerRequest,
        *,
        reason: str,
    ) -> DpsEnrichmentOutcome:
        decision = DpsLookupDecision(
            lookup_required=False,
            status=DpsLookupStatus.NOT_REQUIRED,
            change_request=False,
            order_id=(
                str(request.order_id).strip()
                if str(request.order_id or "").strip()
                else None
            ),
            general_segments=(request.question,) if request.question else (),
            dps_segments=(),
            reason=str(reason),
        )
        metadata = self._base_metadata(decision)
        request.metadata["dps"] = metadata
        return DpsEnrichmentOutcome(decision, metadata)

    def _record(
        self,
        inquiry_id: int,
        event_code: str,
        message: str,
        *,
        level: str = "INFO",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.logs.record_inquiry(
            inquiry_id,
            event_code,
            message,
            level=level,
            details=dict(details or {}),
        )

    @staticmethod
    def _masked_order_id(value: str) -> str:
        text = str(value or "").strip()
        if len(text) <= 4:
            return "*" * len(text)
        if len(text) <= 8:
            return f"{text[:2]}****{text[-2:]}"
        return f"{text[:4]}****{text[-4:]}"

    def _record_required_date_events(
        self,
        inquiry_id: int,
        *,
        order_id: str,
        correlation_id: str | None,
        normalized: Mapping[str, Any],
    ) -> None:
        parse_status = str(
            normalized.get("date_parse_status") or "MISSING"
        ).upper()
        event = {
            "PARSED": "DPS_REQUIRED_DATE_FOUND",
            "MISSING": "DPS_REQUIRED_DATE_MISSING",
            "PARSE_FAILED": "DPS_REQUIRED_DATE_PARSE_FAILED",
            "CONFLICT": "DPS_REQUIRED_DATE_CONFLICT",
            "PARTIAL": "DPS_REQUIRED_DATE_PARTIAL",
        }.get(parse_status, "DPS_REQUIRED_DATE_MISSING")
        details = {
            "masked_order_id": self._masked_order_id(order_id),
            "correlation_id": correlation_id,
            "status": parse_status,
            "normalized_date": normalized.get("installation_date"),
            "source": normalized.get("installation_date_source"),
            "row_count": normalized.get(
                "required_delivery_date_row_count", 0
            ),
        }
        self._record(
            inquiry_id,
            event,
            event,
            level=(
                "WARNING"
                if parse_status in {"PARSE_FAILED", "CONFLICT", "PARTIAL"}
                else "INFO"
            ),
            details=details,
        )
        if normalized.get("installation_date"):
            self._record(
                inquiry_id,
                "DPS_INSTALLATION_DATE_SAVED",
                "DPS_INSTALLATION_DATE_SAVED",
                details=details,
            )

    def _start_step(self, inquiry_id: int, *, force_refresh: bool) -> None:
        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(inquiry_id, StepCode.DPS_LOOKUP)
        status = StepStatus(step["step_status"])
        metadata = {"force_refresh": force_refresh}
        if status is StepStatus.SKIPPED:
            # SKIPPED describes the classification decision made for an
            # earlier answer attempt.  An explicit/manual lookup is a new
            # attempt and must reopen the durable step before it can finish.
            self.workflows.reopen_skipped_step(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                metadata={
                    **metadata,
                    "reason": "EXPLICIT_DPS_LOOKUP_AFTER_SKIP",
                },
            )
            status = StepStatus.PENDING
        if status is StepStatus.PENDING:
            self.workflows.start_step(
                inquiry_id, StepCode.DPS_LOOKUP, metadata=metadata
            )
        elif status in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
            self.workflows.retry_step(
                inquiry_id, StepCode.DPS_LOOKUP, metadata=metadata
            )
        elif status is StepStatus.COMPLETED:
            self.workflows.restart_completed_step(
                inquiry_id, StepCode.DPS_LOOKUP, metadata=metadata
            )
        elif status is StepStatus.RUNNING:
            raise RuntimeError("DPS lookup is already running.")

    def _complete_step(
        self, inquiry_id: int, metadata: Mapping[str, Any]
    ) -> None:
        self.workflows.initialize_steps(inquiry_id)
        step = self.workflows.get_step(inquiry_id, StepCode.DPS_LOOKUP)
        status = StepStatus(step["step_status"])
        if status is StepStatus.COMPLETED:
            return
        if status is StepStatus.SKIPPED:
            self.workflows.reopen_skipped_step(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                metadata={
                    **dict(metadata),
                    "reason": "DPS_RESULT_AVAILABLE_AFTER_SKIP",
                },
            )
            self.workflows.start_step(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                metadata=dict(metadata),
            )
        if status in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
            self.workflows.retry_step(
                inquiry_id, StepCode.DPS_LOOKUP, metadata=dict(metadata)
            )
        self.workflows.complete_step(
            inquiry_id, StepCode.DPS_LOOKUP, metadata=dict(metadata)
        )

    def _cache_metadata(
        self, row: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = dict(row["normalized_result_json"])
        metadata["cache_used"] = True
        try:
            queried = datetime.fromisoformat(
                str(row["queried_at"]).replace("Z", "+00:00")
            )
            if queried.tzinfo is None:
                queried = queried.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - queried.astimezone(UTC)).total_seconds()
        except (TypeError, ValueError):
            age = 0
        metadata["cache_age_seconds"] = max(0, int(age))
        metadata["elapsed_seconds"] = 0.0
        return metadata

    def enrich(
        self,
        request: AnswerRequest,
        *,
        force_refresh: bool = False,
        explicit_lookup: bool = False,
        correlation_id: str | None = None,
    ) -> DpsEnrichmentOutcome:
        decision = self.policy.decide(request)
        if explicit_lookup and request.order_id:
            decision = DpsLookupDecision(
                lookup_required=True,
                status=DpsLookupStatus.PENDING,
                change_request=decision.change_request,
                order_id=str(request.order_id).strip(),
                general_segments=decision.general_segments,
                dps_segments=decision.dps_segments,
                reason="Dashboard에서 사용자가 DPS 조회를 요청했습니다.",
            )
        metadata = self._base_metadata(decision)
        inquiry_id = request.inquiry_id
        if not decision.lookup_required:
            request.metadata["dps"] = metadata
            return DpsEnrichmentOutcome(decision, metadata)
        if inquiry_id is None:
            raise ValueError("DPS enrichment requires inquiry_id.")

        self._record(
            inquiry_id,
            "DPS_LOOKUP_REQUESTED",
            "배송·설치 문의의 DPS 조회 정책을 적용했습니다.",
            details={
                "order_id_present": bool(decision.order_id),
                "status": decision.status.value,
                "force_refresh": force_refresh,
                "correlation_id": correlation_id,
            },
        )
        if force_refresh:
            self._record(
                inquiry_id,
                "DPS_RETRY_REQUESTED",
                "DPS 강제 재조회를 요청했습니다.",
                details={"order_id_present": bool(decision.order_id)},
            )

        if decision.status is DpsLookupStatus.WAITING_FOR_ORDER_ID:
            message = user_message_for_status(decision.status)
            metadata.update(
                {
                    "error_code": decision.status.value,
                    "error_message": message,
                    "warnings": [message],
                }
            )
            self.workflows.initialize_steps(inquiry_id)
            step = self.workflows.get_step(inquiry_id, StepCode.DPS_LOOKUP)
            current = StepStatus(step["step_status"])
            if current is StepStatus.SKIPPED:
                self.workflows.reopen_skipped_step(
                    inquiry_id,
                    StepCode.DPS_LOOKUP,
                    metadata={
                        **metadata,
                        "reason": "DPS_REVIEW_REQUIRED_AFTER_SKIP",
                    },
                )
                self.workflows.start_step(
                    inquiry_id,
                    StepCode.DPS_LOOKUP,
                    metadata=metadata,
                )
                current = StepStatus.RUNNING
            if current in {StepStatus.FAILED, StepStatus.NEEDS_REVIEW}:
                self.workflows.retry_step(
                    inquiry_id, StepCode.DPS_LOOKUP, metadata=metadata
                )
            elif current is StepStatus.COMPLETED:
                self.workflows.restart_completed_step(
                    inquiry_id, StepCode.DPS_LOOKUP, metadata=metadata
                )
            self.workflows.mark_needs_review(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                error_code=decision.status.value,
                message=message,
                metadata=metadata,
            )
            self.inquiries.update_status(
                inquiry_id, InquiryStatus.NEEDS_ATTENTION
            )
            request.metadata["dps"] = metadata
            self._record(
                inquiry_id,
                "ANSWER_REQUIRES_REVIEW_DUE_TO_DPS",
                message,
                level="WARNING",
                details={"status": decision.status.value},
            )
            return DpsEnrichmentOutcome(decision, metadata)

        order_id = decision.order_id
        assert order_id is not None
        if not force_refresh:
            cached = self.dps.get_latest_success_by_order_id(
                order_id, valid_only=True
            )
            if cached is not None and cached.get(
                "_normalized_result_json_corrupt"
            ):
                self._record(
                    inquiry_id,
                    "DPS_CACHE_CORRUPTED",
                    "손상된 DPS 캐시를 사용하지 않고 새 조회로 전환합니다.",
                    level="WARNING",
                    details={
                        "cache_result_id": cached.get("id"),
                        "order_id_present": True,
                        "safe_error_code": "CACHE_CORRUPTION",
                    },
                )
                cached = None
            if cached is not None:
                LOGGER.info("DPS_DATA_REFRESH_SKIPPED reason=FRESH_CACHE")
                metadata = self._cache_metadata(cached)
                metadata["change_request"] = decision.change_request
                metadata["general_segments"] = list(decision.general_segments)
                metadata["dps_segments"] = list(decision.dps_segments)
                request.metadata["dps"] = metadata
                self._complete_step(
                    inquiry_id,
                    {"lookup_status": "SUCCESS", "cache_used": True},
                )
                self._record(
                    inquiry_id,
                    "DPS_CACHE_HIT",
                    "유효한 DPS 조회 캐시를 사용했습니다.",
                    details={
                        "order_id_present": True,
                        "status": "SUCCESS",
                        "cache_used": True,
                        "cache_age_seconds": metadata["cache_age_seconds"],
                    },
                )
                self._record(
                    inquiry_id,
                    "DPS_RESULT_INJECTED_TO_ANSWER",
                    "DPS 캐시 결과를 답변 요청에 반영했습니다.",
                    details={"status": "SUCCESS", "cache_used": True},
                )
                if int(cached["inquiry_id"]) == int(inquiry_id):
                    inquiry_cache_row = cached
                else:
                    cache_time = datetime.now(UTC).isoformat(
                        timespec="milliseconds"
                    )
                    inquiry_cache_row = self.dps.create_lookup_result(
                        inquiry_id=inquiry_id,
                        order_id=order_id,
                        lookup_status=DpsLookupStatus.SUCCESS,
                        raw_result=cached.get("raw_result_json") or {},
                        normalized_result=metadata,
                        queried_at=cache_time,
                        expires_at=cached.get("expires_at"),
                        correlation_id=correlation_id,
                        lookup_started_at=cache_time,
                        lookup_completed_at=cache_time,
                        duration_seconds=0.0,
                        cached=True,
                    )
                    self._record(
                        inquiry_id,
                        "DPS_RESULT_SAVED",
                        "DPS 캐시 결과를 현재 문의에 저장했습니다.",
                        details={
                            "correlation_id": correlation_id,
                            "lookup_result_id": inquiry_cache_row["id"],
                            "status": "SUCCESS",
                            "duration": 0.0,
                            "cached": True,
                        },
                    )
                return DpsEnrichmentOutcome(
                    decision, metadata, inquiry_cache_row
                )
            self._record(
                inquiry_id,
                "DPS_CACHE_MISS",
                "사용 가능한 DPS 성공 캐시가 없습니다.",
                details={"order_id_present": True, "cache_used": False},
            )

        self._start_step(inquiry_id, force_refresh=force_refresh)
        previous_success = self.dps.get_latest_success_by_inquiry_and_order(
            inquiry_id, order_id
        )
        if previous_success and previous_success.get(
            "_normalized_result_json_corrupt"
        ):
            previous_success = None
        self._record(
            inquiry_id,
            "DPS_LOOKUP_STARTED",
            "DPS Agent 조회를 시작했습니다.",
            details={
                "order_id_present": True,
                "status": "RUNNING",
                "correlation_id": correlation_id,
            },
        )
        lookup_started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        started = time.monotonic()
        LOGGER.info("DPS_DATA_REFRESH_START")
        try:
            raw = self.client(
                request_id=correlation_id,
                selected_inquiry_id=str(inquiry_id),
                order_id=order_id,
                dps_query_value=order_id,
                dps_query_value_type="order_id",
                order_date=request.metadata.get("order_date"),
                order_created_at=request.metadata.get("order_created_at"),
                payment_date=request.metadata.get("payment_date"),
                payment_completed_at=request.metadata.get(
                    "payment_completed_at"
                ),
                place_order_date=request.metadata.get("place_order_date"),
                shipping_due_date=request.metadata.get("shipping_due_date"),
                force_refresh=force_refresh,
            )
            if not isinstance(raw, dict):
                raw = {
                    "success": False,
                    "code": "AGENT_RESPONSE_INVALID",
                    "message": "DPS Agent 응답 형식이 올바르지 않습니다.",
                }
        except Exception as error:
            LOGGER.exception(
                "DPS client error: inquiry_id=%s error_type=%s",
                inquiry_id,
                error.__class__.__name__,
            )
            raw = {
                "success": False,
                "code": "DPS_CLIENT_EXCEPTION",
                "message": "DPS Agent 호출 중 오류가 발생했습니다.",
            }
        elapsed = time.monotonic() - started
        raw_code = str(raw.get("code") or raw.get("error_code") or "")
        if raw_code not in {
            "AGENT_CONNECTION_FAILED",
            "AGENT_CONNECT_TIMEOUT",
            "AGENT_START_FAILED",
            "AGENT_START_TIMEOUT",
        }:
            self._record(
                inquiry_id,
                "DPS_AGENT_CONNECTED",
                "DPS Agent가 조회 요청을 수신했습니다.",
                details={
                    "correlation_id": correlation_id,
                    "stage": raw.get("stage"),
                },
            )
        normalized = normalize_dps_result(
            raw, order_id=order_id, elapsed_seconds=elapsed
        )
        self._record_required_date_events(
            inquiry_id,
            order_id=order_id,
            correlation_id=correlation_id,
            normalized=normalized,
        )
        status = DpsLookupStatus(normalized["lookup_status"])
        if status is DpsLookupStatus.SUCCESS:
            LOGGER.info("DPS_DATA_REFRESH_SUCCESS")
        elif status is DpsLookupStatus.NOT_FOUND:
            LOGGER.info("DPS_DATA_REFRESH_SUCCESS result=ORDER_NOT_FOUND")
        elif status is DpsLookupStatus.TIMEOUT:
            LOGGER.warning("DPS_TIMEOUT error_type=%s", normalized.get("error_code"))
        else:
            LOGGER.warning(
                "DPS_DATA_REFRESH_FAILED error_type=%s",
                normalized.get("error_code") or status.value,
            )
        normalized["change_request"] = decision.change_request
        normalized["general_segments"] = list(decision.general_segments)
        normalized["dps_segments"] = list(decision.dps_segments)
        now = datetime.now(UTC)
        ttl = (
            self.settings.success_ttl_seconds
            if status is DpsLookupStatus.SUCCESS
            else self.settings.not_found_ttl_seconds
            if status is DpsLookupStatus.NOT_FOUND
            else 0
        )
        expires_at = (
            (now + timedelta(seconds=ttl)).isoformat(timespec="milliseconds")
            if ttl
            else None
        )
        error_message = (
            None
            if status is DpsLookupStatus.SUCCESS
            else user_message_for_status(status)
            or str(normalized.get("error_message") or "DPS 조회에 실패했습니다.")
        )
        if error_message:
            normalized["error_message"] = error_message
            normalized["warnings"] = list(normalized["warnings"]) + [
                error_message
            ]
        row = self.dps.create_lookup_result(
            inquiry_id=inquiry_id,
            order_id=order_id,
            lookup_status=status,
            raw_result=sanitize_raw_result(raw),
            normalized_result=normalized,
            error_code=normalized.get("error_code"),
            error_message=error_message,
            queried_at=str(normalized["queried_at"]),
            expires_at=expires_at,
            correlation_id=correlation_id,
            lookup_started_at=lookup_started_at,
            lookup_completed_at=datetime.now(UTC).isoformat(
                timespec="milliseconds"
            ),
            duration_seconds=round(elapsed, 3),
            cached=False,
        )
        previous_normalized = (
            previous_success.get("normalized_result_json")
            if previous_success
            and isinstance(
                previous_success.get("normalized_result_json"), dict
            )
            else {}
        )
        previous_date = previous_normalized.get("installation_date")
        current_date = normalized.get("installation_date")
        if previous_date and current_date and previous_date != current_date:
            self.answers.mark_unposted_drafts_stale(
                inquiry_id,
                reason="DPS_INSTALLATION_DATE_CHANGED",
            )
            self._record(
                inquiry_id,
                "GPT_DRAFT_STALE_AFTER_DPS_REFRESH",
                "설치예정일 변경으로 기존 GPT 초안을 다시 생성해야 합니다.",
                level="WARNING",
                details={
                    "masked_order_id": self._masked_order_id(order_id),
                    "dps_lookup_id": row["id"],
                    "correlation_id": correlation_id,
                    "status": "STALE",
                    "normalized_date": current_date,
                },
            )
        self._record(
            inquiry_id,
            "DPS_RESULT_SAVED",
            "DPS 조회 결과를 저장하고 다시 확인했습니다.",
            details={
                "correlation_id": correlation_id,
                "lookup_result_id": row["id"],
                "status": status.value,
                "duration": round(elapsed, 3),
            },
        )
        request.metadata["dps"] = normalized

        event = {
            DpsLookupStatus.SUCCESS: "DPS_LOOKUP_SUCCEEDED",
            DpsLookupStatus.NOT_FOUND: "DPS_LOOKUP_NOT_FOUND",
            DpsLookupStatus.TIMEOUT: "DPS_LOOKUP_TIMEOUT",
        }.get(status, "DPS_LOOKUP_FAILED")
        level = (
            "INFO"
            if status in {DpsLookupStatus.SUCCESS, DpsLookupStatus.NOT_FOUND}
            else "WARNING"
        )
        self._record(
            inquiry_id,
            event,
            (
                "DPS 조회를 완료했습니다."
                if status is DpsLookupStatus.SUCCESS
                else error_message or "DPS 조회에 실패했습니다."
            ),
            level=level,
            details={
                "order_id_present": True,
                "status": status.value,
                "elapsed_seconds": round(elapsed, 3),
                "cache_used": False,
                "error_code": normalized.get("error_code"),
                "correlation_id": correlation_id,
            },
        )
        if status is DpsLookupStatus.SUCCESS:
            self.workflows.complete_step(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                metadata={
                    "lookup_result_id": row["id"],
                    "lookup_status": status.value,
                    "cache_used": False,
                },
            )
            self._record(
                inquiry_id,
                "DPS_RESULT_INJECTED_TO_ANSWER",
                "DPS 조회 결과를 답변 요청에 반영했습니다.",
                details={"status": status.value, "cache_used": False},
            )
        elif status is DpsLookupStatus.NOT_FOUND:
            self.workflows.mark_needs_review(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                error_code=status.value,
                message=error_message,
                metadata={"lookup_result_id": row["id"]},
            )
            self.inquiries.update_status(
                inquiry_id, InquiryStatus.NEEDS_ATTENTION
            )
        else:
            self.workflows.fail_step(
                inquiry_id,
                StepCode.DPS_LOOKUP,
                normalized.get("error_code") or status.value,
                error_message or "DPS 조회에 실패했습니다.",
                metadata={"lookup_result_id": row["id"], "status": status.value},
            )
            self.inquiries.update_status(
                inquiry_id, InquiryStatus.NEEDS_ATTENTION
            )
        if status is not DpsLookupStatus.SUCCESS or decision.change_request:
            self._record(
                inquiry_id,
                "ANSWER_REQUIRES_REVIEW_DUE_TO_DPS",
                "DPS 결과로 인해 직원 검토가 필요합니다.",
                level="WARNING",
                details={"status": status.value},
            )
        return DpsEnrichmentOutcome(decision, normalized, row)
