from __future__ import annotations

from dataclasses import asdict, dataclass
import traceback
from typing import Any, Callable, Iterable

from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository, mask_sensitive_data
from repositories.workflow_repository import WorkflowRepository
from repositories.post_review_repository import PostReviewRepository
from services.automatic_draft_service import AutomaticDraftService
from services.learning_service import LearningService
from workflow.models import StepCode


SENSITIVE_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "otp",
)


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if any(marker in normalized_key for marker in SENSITIVE_KEY_MARKERS):
                continue
            clean[str(key)] = _without_secrets(item)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [_without_secrets(item) for item in value]
    return value


def _first_value(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _first_order_value(work_item: dict[str, Any], field: str) -> Any:
    orders = work_item.get("orders")
    if isinstance(orders, list):
        for order in orders:
            if isinstance(order, dict) and order.get(field) not in (None, ""):
                return order[field]
    return None


def _private_flag(
    work_item: dict[str, Any],
    original: dict[str, Any],
) -> bool | None:
    value = _first_value(
        work_item.get("is_private"),
        work_item.get("isPrivate"),
        work_item.get("secret"),
        work_item.get("isSecret"),
        original.get("isPrivate"),
        original.get("secret"),
        original.get("isSecret"),
    )
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "y", "yes", "private", "secret"}:
        return True
    if normalized in {"false", "0", "n", "no", "public"}:
        return False
    return None


def normalize_work_item(work_item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(work_item, dict):
        raise TypeError("work item must be a dictionary")

    original = (
        work_item.get("original_data")
        if isinstance(work_item.get("original_data"), dict)
        else {}
    )
    source_type = _first_value(
        work_item.get("source"),
        work_item.get("source_type"),
    )
    source_question_id = _first_value(
        work_item.get("inquiry_id"),
        work_item.get("source_question_id"),
        original.get("questionId"),
        original.get("inquiryNo"),
    )
    store_code = work_item.get("store_code")
    if store_code in (None, ""):
        raise ValueError("work item is missing store_code")
    if source_type in (None, ""):
        raise ValueError("work item is missing source")
    if source_question_id in (None, ""):
        raise ValueError("work item is missing inquiry_id")

    product_order_ids = work_item.get("product_order_ids")
    first_product_order_id = (
        product_order_ids[0]
        if isinstance(product_order_ids, list) and product_order_ids
        else None
    )
    order_id = _first_value(
        work_item.get("order_id"),
        _first_order_value(work_item, "order_id"),
    )
    product_order_id = _first_value(
        work_item.get("product_order_id"),
        first_product_order_id,
        _first_order_value(work_item, "product_order_id"),
    )
    customer_display = _first_value(
        work_item.get("customer_name"),
        work_item.get("customer_id"),
        work_item.get("writer_id"),
    )
    answered = work_item.get("answered")
    is_private = _private_flag(work_item, original)
    raw_payload = _without_secrets(
        work_item.get("raw_payload")
        if isinstance(work_item.get("raw_payload"), dict)
        else {}
    )
    raw_json: dict[str, Any] = {
        **raw_payload,
        "source_payload": raw_payload,
        "store_code": str(store_code),
        "source": str(source_type),
        "inquiry_id": str(source_question_id),
        "registered_at": work_item.get("registered_at"),
    }
    for field in (
        "queue",
        "queue_label",
        "priority",
        "analysis",
        "is_delivery",
    ):
        value = _without_secrets(work_item.get(field))
        if value not in (None, ""):
            raw_json[field] = value
    if not raw_payload:
        # Legacy/injected loaders may not provide a separate source payload.
        # Preserve their already-normalized safe work-item metadata.
        raw_json.update(_without_secrets(work_item))

    return {
        "store_code": str(store_code),
        "source_type": str(source_type),
        "source_question_id": str(source_question_id),
        "external_inquiry_id": str(
            work_item.get("external_inquiry_id") or source_question_id
        ),
        "inquiry_type": _first_value(
            work_item.get("category"),
            work_item.get("inquiry_type"),
            source_type,
        ),
        "title": work_item.get("title"),
        "content": work_item.get("content"),
        "product_id": work_item.get("product_id"),
        "product_name": work_item.get("product_name"),
        "option_name": _first_value(
            work_item.get("product_option"),
            work_item.get("option_name"),
        ),
        "customer_display": customer_display,
        "masked_writer_id": _first_value(
            work_item.get("masked_writer_id"),
            work_item.get("writer_id"),
        ),
        "order_id": str(order_id) if order_id not in (None, "") else None,
        "product_order_id": (
            str(product_order_id)
            if product_order_id not in (None, "")
            else None
        ),
        "registered_at": work_item.get("registered_at"),
        "source_answered": answered,
        "source_status": work_item.get("source_status"),
        "source_created_at": _first_value(
            work_item.get("source_created_at"),
            work_item.get("registered_at"),
        ),
        "source_updated_at": _first_value(
            work_item.get("source_updated_at"),
            work_item.get("registered_at"),
        ),
        "is_private": is_private,
        "source_metadata_json": {
            "is_private": is_private,
            "privacy_source_present": is_private is not None,
            "source": str(source_type),
        },
        "workflow_status": "NEW",
        "answer_status": "ANSWERED" if answered is True else "UNANSWERED",
        "post_status": "POSTED" if answered is True else "NOT_POSTED",
        "raw_json": raw_json,
    }


@dataclass
class SyncResult:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class InquirySyncService:
    def __init__(
        self,
        inquiry_repository: InquiryRepository,
        workflow_repository: WorkflowRepository,
        log_repository: LogRepository,
        *,
        automatic_drafts: AutomaticDraftService | None = None,
        learning: LearningService | None = None,
    ) -> None:
        self.inquiries = inquiry_repository
        self.workflow = workflow_repository
        self.logs = log_repository
        self.automatic_drafts = automatic_drafts
        self.learning = learning or LearningService(inquiry_repository.database)

    def sync(
        self,
        load_result: (
            Iterable[dict[str, Any]]
            | tuple[Iterable[dict[str, Any]], Iterable[Any]]
        ),
        *,
        correlation_id: str | None = None,
        event_callback: (
            Callable[..., None] | None
        ) = None,
    ) -> dict[str, int]:
        work_items: Iterable[dict[str, Any]]
        if isinstance(load_result, tuple):
            work_items = load_result[0]
        else:
            work_items = load_result

        result = SyncResult()
        for index, work_item in enumerate(work_items):
            customer_names = ()
            failed_stage = "NORMALIZE"
            if isinstance(work_item, dict):
                customer_name = work_item.get("customer_name")
                customer_names = (
                    (str(customer_name),)
                    if customer_name not in (None, "")
                    else ()
                )
            try:
                normalized = normalize_work_item(work_item)
                failed_stage = "INQUIRY_UPSERT"
                upsert = self.inquiries.upsert_work_item(normalized)
                failed_stage = "SELLER_ANSWER_LEARNING"
                seller_answer = str(work_item.get("seller_answer") or "").strip()
                if seller_answer:
                    try:
                        review_repository = PostReviewRepository(self.inquiries.database)
                        remote_version, remote_changed = (
                            review_repository.capture_remote_naver_edit(
                                inquiry_id=upsert.inquiry_id,
                                answer_body=seller_answer,
                            )
                        )
                        if remote_changed and remote_version is not None:
                            saved = self.learning.capture_auto_post_version(
                                inquiry_id=upsert.inquiry_id,
                                version_id=int(remote_version["id"]),
                                source="AUTO_POST_CORRECTED",
                            )
                            if saved is not None:
                                review_repository.mark_learning_saved(
                                    int(remote_version["id"])
                                )
                            self.logs.record_inquiry(
                                upsert.inquiry_id,
                                "NAVER_DIRECT_EDIT_LEARNING_SAVED",
                                "네이버에서 직접 수정된 답변을 감지해 사후검토 Learning으로 저장했습니다.",
                                details={
                                    "answer_version_id": int(remote_version["id"]),
                                    "learning_saved": saved is not None,
                                    "learning_source": "AUTO_POST_CORRECTED",
                                },
                            )
                        elif remote_version is None:
                            # Historical seller answers without a Q&A Auto post
                            # history remain style-only legacy Learning data.
                            self.learning.capture_seller_answer(
                                inquiry_id=upsert.inquiry_id,
                                answer=seller_answer,
                            )
                        else:
                            from services.positive_learning_service import PositiveLearningService
                            PositiveLearningService(self.inquiries.database).observe(
                                inquiry_id=upsert.inquiry_id,
                                seller_answer=seller_answer,
                            )
                    except Exception as learning_error:
                        self.logs.record_inquiry(
                            upsert.inquiry_id,
                            "LEARNING_SELLER_IMPORT_FAILED",
                            "판매자 답변 Learning 저장에 실패했지만 동기화는 계속됩니다.",
                            level="WARNING",
                            details={"exception_type": learning_error.__class__.__name__},
                        )
                setattr(result, upsert.outcome, getattr(result, upsert.outcome) + 1)
                if event_callback is not None:
                    failed_stage = "OUTCOME_TRACE"
                    outcome_event = {
                        "new": "NAVER_SYNC_ITEM_INSERTED",
                        "updated": "NAVER_SYNC_ITEM_UPDATED",
                        "unchanged": "NAVER_SYNC_ITEM_UNCHANGED",
                    }[upsert.outcome]
                    event_callback(
                        outcome_event,
                        {
                            "inquiry_id": upsert.inquiry_id,
                            "store_code": normalized["store_code"],
                            "source": normalized["source_type"],
                            "outcome": upsert.outcome,
                            "registered_at": normalized.get("registered_at"),
                        },
                    )
                failed_stage = "WORKFLOW_INITIALIZE"
                self.workflow.initialize_steps(upsert.inquiry_id)
                failed_stage = "WORKFLOW_READ"
                collected_step = self.workflow.get_step(
                    upsert.inquiry_id,
                    StepCode.INQUIRY_COLLECTED,
                )
                if collected_step["step_status"] == "PENDING":
                    failed_stage = "WORKFLOW_COMPLETE"
                    self.workflow.complete_step(
                        upsert.inquiry_id,
                        StepCode.INQUIRY_COLLECTED,
                        metadata={"source": normalized["source_type"]},
                    )
                if upsert.created:
                    self.logs.record_inquiry(
                        upsert.inquiry_id,
                        "INQUIRY_SYNC_CREATED",
                        "문의가 작업 저장소에 추가되었습니다.",
                    )
                if (
                    bool(normalized.get("source_answered"))
                    and upsert.outcome in {"new", "updated"}
                ):
                    self.logs.record_inquiry(
                        upsert.inquiry_id,
                        "AUTO_POST_SKIPPED_ALREADY_ANSWERED",
                        "동기화 결과 네이버에 이미 답변된 문의로 확인했습니다.",
                        details={
                            "source": normalized.get("source_type"),
                            "store_code": normalized.get("store_code"),
                            "network_call_count": 0,
                        },
                    )
                if self.automatic_drafts is not None:
                    # Run every synchronized inquiry through the idempotent
                    # automatic pipeline.  This also recovers an older,
                    # unchanged unanswered inquiry whose Draft was never
                    # created; existing/answered inquiries are safely skipped
                    # by AutomaticDraftService without repeating lookups.
                    failed_stage = "AUTOMATIC_DRAFT"
                    automatic = self.automatic_drafts.ensure_for_inquiry(
                        upsert.inquiry_id,
                        correlation_id=correlation_id,
                    )
                    if event_callback is not None:
                        event_callback(
                            (
                                "AUTOMATIC_DRAFT_COMPLETED"
                                if automatic.status in {"CREATED", "EXISTING"}
                                else "AUTOMATIC_DRAFT_SKIPPED"
                                if automatic.status.startswith("SKIPPED")
                                else "AUTOMATIC_DRAFT_FAILED"
                            ),
                            {
                                "inquiry_id": upsert.inquiry_id,
                                "status": automatic.status,
                                "draft_id": automatic.draft_id,
                                "selected_answer_route": automatic.route,
                                "safe_error_code": automatic.error_code,
                            },
                        )
                if upsert.created and correlation_id:
                    # Durable outbox insertion happens only after the inquiry
                    # upsert transaction has committed. Event processing is
                    # isolated so it can never fail the Auto Sync item.
                    try:
                        from repositories.auto_post_event_repository import (
                            AutoPostEventRepository,
                        )
                        from repositories.auto_post_repository import (
                            AutoPostRepository,
                        )

                        runtime_enabled = bool(
                            AutoPostRepository(self.inquiries.database)
                            .settings()
                            .get("runtime_auto_post_enabled")
                        )
                        event = AutoPostEventRepository(
                            self.inquiries.database
                        ).create(
                            inquiry_id=upsert.inquiry_id,
                            store_code=str(normalized.get("store_code") or ""),
                            external_id=str(
                                normalized.get("external_inquiry_id")
                                or normalized.get("source_question_id")
                                or ""
                            ),
                            source_sync_id=correlation_id,
                            runtime_enabled=runtime_enabled,
                        )
                        if event is not None:
                            self.logs.record_inquiry(
                                upsert.inquiry_id,
                                "AUTO_SYNC_EVENT_CREATED",
                                "신규 문의 Auto Sync Event를 생성했습니다.",
                                details={
                                    "event_id": event["id"],
                                    "status": event["status"],
                                    "source_sync_id": correlation_id,
                                },
                            )
                            if event["status"] == "PENDING":
                                from services.naver_auto_post_scheduler import (
                                    ensure_auto_post_scheduler,
                                )

                                ensure_auto_post_scheduler(
                                    self.inquiries.database
                                ).notify_event(upsert.inquiry_id)
                    except Exception as event_error:
                        self.logs.record_inquiry(
                            upsert.inquiry_id,
                            "AUTO_SYNC_EVENT_FAILED",
                            "Event 생성에 실패했지만 문의 동기화는 완료했습니다.",
                            level="ERROR",
                            details={
                                "error_type": event_error.__class__.__name__,
                                "source_sync_id": correlation_id,
                            },
                        )
            except Exception as error:
                result.failed += 1
                stack_trace = "".join(
                    traceback.format_exception(
                        error.__class__, error, error.__traceback__
                    )
                )
                safe_context = mask_sensitive_data(
                    {
                        "index": index,
                        "store_code": (
                            work_item.get("store_code")
                            if isinstance(work_item, dict)
                            else None
                        ),
                        "source": (
                            work_item.get("source")
                            if isinstance(work_item, dict)
                            else None
                        ),
                        "inquiry_id": (
                            work_item.get("inquiry_id")
                            if isinstance(work_item, dict)
                            else None
                        ),
                        "error_type": error.__class__.__name__,
                        "error": str(error),
                        "failed_stage": failed_stage,
                        "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
                        "sqlite_errorname": getattr(error, "sqlite_errorname", None),
                        "stack_trace": stack_trace,
                    },
                    customer_names=customer_names,
                )
                self.logs.record_system(
                    "INQUIRY_SYNC_ITEM_FAILED",
                    "문의 동기화 항목 처리에 실패했습니다.",
                    level="ERROR",
                    details=safe_context,
                    customer_names=customer_names,
                )
                if event_callback is not None:
                    event_callback(
                        "NAVER_SYNC_DB_UPSERT_FAILED",
                        {
                            "index": index,
                            "error_type": error.__class__.__name__,
                            "error": safe_context.get("error"),
                            "failed_stage": failed_stage,
                            "sqlite_errorcode": safe_context.get("sqlite_errorcode"),
                            "sqlite_errorname": safe_context.get("sqlite_errorname"),
                            "stack_trace": safe_context.get("stack_trace"),
                        },
                        level="ERROR",
                    )

        summary = result.to_dict()
        self.logs.record_system(
            "INQUIRY_SYNC_COMPLETED",
            "문의 목록 DB 동기화가 완료되었습니다.",
            details={
                **summary,
                "correlation_id": correlation_id,
            },
        )
        return summary
