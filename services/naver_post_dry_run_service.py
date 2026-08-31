from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from config import (
    NaverPostSettings,
    StoreConfig,
    get_store_config,
)
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.log_repository import LogRepository
from repositories.naver_sync_repository import NaverSyncRepository
from services.naver_post_payload_builder import (
    MAX_COMMENT_LENGTH,
    NaverPostPayloadBuilder,
    NaverPostTargetError,
)
from services.auto_post_validation_service import AutoPostTechnicalValidator
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)


NAVER_POST_LOCK = True


@dataclass(frozen=True)
class NaverPostDryRunResult:
    status: str
    eligible: bool
    inquiry_id: int
    external_id: str | None
    store: str | None
    source_type: str | None
    approval_status: str
    post_status: str
    final_answer_length: int
    method: str | None
    endpoint: str | None
    payload: dict[str, Any] | None
    authorization: dict[str, Any]
    validations: tuple[dict[str, Any], ...]
    reasons: tuple[str, ...]
    post_locked: bool = True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["validations"] = [dict(item) for item in self.validations]
        value["reasons"] = list(self.reasons)
        return value


class NaverPostDryRunService:
    """Build a masked registration preview without an HTTP write path."""

    def __init__(
        self,
        database: Database,
        *,
        store_resolver: Callable[[str | None], StoreConfig] = get_store_config,
        settings: NaverPostSettings | None = None,
        payload_builder: NaverPostPayloadBuilder | None = None,
    ) -> None:
        self.database = database
        self.inquiries = InquiryRepository(database)
        self.answers = AnswerRepository(database)
        self.approvals = ApprovalRepository(database)
        self.logs = LogRepository(database)
        self.store_resolver = store_resolver
        self.settings = settings or NaverPostSettings.from_environment()
        self.payload_builder = payload_builder or NaverPostPayloadBuilder()
        self.technical_validator = AutoPostTechnicalValidator()
        self.eligibility = AutoProcessingEligibilityService()

    @staticmethod
    def _route(draft: dict[str, Any]) -> str:
        metadata = draft.get("metadata_json")
        data = metadata if isinstance(metadata, dict) else {}
        selected = str(data.get("selected_answer_route") or "").upper()
        if selected:
            return selected
        if str(data.get("answer_type") or "").lower() == "order_id_required":
            return "ORDER_ID_REQUEST"
        return str(data.get("generation_mode") or draft.get("source") or "").upper()

    def run(self, inquiry_id: int) -> NaverPostDryRunResult:
        inquiry = self.inquiries.get(int(inquiry_id))
        if inquiry is None:
            raise LookupError(f"Inquiry not found: {inquiry_id}")
        draft = (
            self.answers.active_for_inquiry(int(inquiry_id))
            or self.answers.latest_for_inquiry(int(inquiry_id))
        )
        approval = self.approvals.get_inquiry_approval(int(inquiry_id))
        external_id = str(
            inquiry.get("external_inquiry_id")
            or inquiry.get("source_question_id")
            or ""
        ).strip()
        store = str(inquiry.get("store_code") or "").strip()
        source_type = str(inquiry.get("source_type") or "").strip().upper()
        final_answer = str((draft or {}).get("final_answer") or "").strip()
        approval_status = str(
            approval.get("approval_status") or "PENDING"
        ).upper()
        post_status = str(
            approval.get("post_status")
            or inquiry.get("post_status")
            or "NOT_POSTED"
        ).upper()

        checks: list[dict[str, Any]] = []
        reasons: list[str] = []

        def check(name: str, passed: bool, reason: str) -> None:
            checks.append(
                {"name": name, "status": "PASS" if passed else "FAIL"}
            )
            if not passed:
                reasons.append(reason)

        check("approval", approval_status == "APPROVED", "승인되지 않음")
        check("final_answer", bool(final_answer), "Final Answer 없음")
        check(
            "post_status",
            post_status in {"NOT_POSTED", "POST_FAILED"}
            and not bool((draft or {}).get("posted")),
            "이미 등록됨",
        )
        check("inquiry_id", int(inquiry_id) > 0, "inquiry_id 없음")
        check("external_id", bool(external_id), "external_id 없음")
        check("store", bool(store), "store 없음")
        check(
            "comment_length",
            0 < len(final_answer) <= MAX_COMMENT_LENGTH,
            "Validation 실패",
        )
        technical = self.technical_validator.validate_answer(final_answer)
        check(
            "validator",
            technical.passed,
            technical.errors[0] if technical.errors else "VALIDATOR_NOT_PASS",
        )
        if draft is not None and final_answer:
            current_draft = dict(draft)
            current_draft["original_answer"] = final_answer
            eligibility = self.eligibility.evaluate(
                inquiry=inquiry,
                draft=current_draft,
                route=self._route(current_draft),
            )
            # Manual approval may override only the fact that a safe route was
            # not configured for automatic posting. Every factual, Validator,
            # Missing Item, order and DPS blocker remains mandatory.
            manual_blockers = tuple(
                reason
                for reason in eligibility.reasons
                if reason != "INTENT_NOT_AUTO_POSTABLE"
            )
            check(
                "current_safety",
                not manual_blockers,
                manual_blockers[0] if manual_blockers else "CURRENT_SAFETY_BLOCKED",
            )

        authorization = {
            "scheme": "Bearer",
            "header": "Authorization",
            "value": "Bearer <NAVER_ACCESS_TOKEN>",
            "prepared": False,
            "network_call": False,
        }
        auth_failure_reason = "Authorization 준비 실패"
        try:
            store_config = self.store_resolver(store or None)
            store_matches = str(store_config.code).upper() == store.upper()
            if not store_matches:
                auth_failure_reason = "STORE_CREDENTIAL_MISMATCH"
            auth_ready = bool(
                store_config.enabled
                and store_config.client_id
                and store_config.client_secret
                and store_matches
            )
        except (TypeError, ValueError):
            auth_ready = False
        authorization["prepared"] = auth_ready
        check("authorization", auth_ready, auth_failure_reason)

        method: str | None = None
        endpoint: str | None = None
        payload: dict[str, Any] | None = None
        built = None
        if not reasons:
            try:
                target = self.payload_builder.resolve_target(
                    inquiry,
                    require_remote_snapshot=True,
                    latest_sync_started_at=str(
                        (
                            NaverSyncRepository(self.database).latest_success_for(
                                store_code=store,
                                inquiry_type=source_type,
                            )
                            or {}
                        ).get("started_at")
                        or ""
                    ),
                )
                built = self.payload_builder.build_for_target(
                    target=target,
                    final_answer=final_answer,
                )
                method, endpoint, payload = (
                    built.method,
                    built.endpoint,
                    dict(built.payload),
                )
                check(
                    "payload",
                    bool(payload),
                    "Payload 생성 실패",
                )
            except NaverPostTargetError as error:
                reasons.append(error.code)
                checks.append({"name": "target", "status": "FAIL"})
            except (KeyError, TypeError, ValueError):
                reasons.append("Payload 생성 실패")
                checks.append({"name": "payload", "status": "FAIL"})

        details = {
            "status": "READY" if not reasons else "NOT_READY",
            "approval_status": approval_status,
            "post_status": post_status,
            "external_id": external_id or None,
            "store": store or None,
            "source_type": source_type or None,
            "final_answer_length": len(final_answer),
            "method": method,
            "endpoint": endpoint,
            "validation_count": len(checks),
            "reasons": reasons,
            "post_locked": not self.settings.enabled,
            "network_call_count": 0,
        }
        self.logs.record_inquiry(
            int(inquiry_id),
            (
                "POST_LOCKED"
                if not self.settings.enabled
                else "POST_GATE_READY"
            ),
            (
                "네이버 실제 등록 잠금이 유지되었습니다."
                if not self.settings.enabled
                else "네이버 실제 등록 전역 설정이 활성화되어 있습니다."
            ),
            details=details,
        )
        if payload is not None:
            self.logs.record_inquiry(
                int(inquiry_id),
                "PAYLOAD_CREATED",
                "네이버 등록 Payload Preview를 생성했습니다.",
                details=details,
            )
        if reasons:
            self.logs.record_inquiry(
                int(inquiry_id),
                "VALIDATION_FAILED",
                "네이버 등록 Dry Run 검증을 통과하지 못했습니다.",
                level="WARNING",
                details=details,
            )
            self.logs.record_inquiry(
                int(inquiry_id),
                "DRY_RUN_FAILED",
                "네이버 등록 Dry Run 결과는 등록 불가입니다.",
                level="WARNING",
                details=details,
            )
        else:
            self.logs.record_inquiry(
                int(inquiry_id),
                "DRY_RUN_SUCCESS",
                "네이버 등록 Dry Run 결과는 등록 가능입니다.",
                details=details,
            )

        return NaverPostDryRunResult(
            status="READY" if not reasons else "NOT_READY",
            eligible=not reasons,
            inquiry_id=int(inquiry_id),
            external_id=external_id or None,
            store=store or None,
            source_type=source_type or None,
            approval_status=approval_status,
            post_status=post_status,
            final_answer_length=len(final_answer),
            method=method,
            endpoint=endpoint,
            payload=payload,
            authorization=authorization,
            validations=tuple(checks),
            reasons=tuple(reasons),
            post_locked=not self.settings.enabled,
        )
