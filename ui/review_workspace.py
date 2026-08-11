from __future__ import annotations

from html import escape
import json
import logging
import os
import re
from typing import Any
import uuid

import streamlit as st
import streamlit.components.v1 as components

from answer.answer_format import format_final_answer
from answer.learning_feedback import (
    CORRECTION_REASON_BY_LABEL,
    CORRECTION_REASON_LABELS,
    INTENT_OPTIONS,
    CorrectionReason,
)
from answer.exceptions import AnswerAlreadyPostedError
from config import NaverPostSettings
from core.time_utils import format_datetime_kst, format_datetime_minute_kst
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.log_repository import LogRepository
from repositories.naver_post_repository import NaverPostRepository
from repositories.naver_posted_answer_repository import (
    NaverPostedAnswerRepository,
)
from repositories.post_review_repository import PostReviewRepository
from repositories.gpt_provider_run_repository import GptProviderRunRepository
from repositories.workflow_repository import WorkflowRepository
from services.answer_service import AnswerService, is_valid_draft
from services.automatic_draft_service import AutomaticDraftService
from services.approval_service import ApprovalError, ApprovalService
from services.dps_lookup_orchestrator import DpsLookupOrchestrator
from services.dps_agent_client import get_dps_agent_status
from services.local_auth_service import Permission
from services.inquiry_processing_plan_service import (
    InquiryProcessingPlanService,
)
from services.naver_post_dry_run_service import NaverPostDryRunService
from services.naver_post_service import NaverPostService
from services.post_review_service import PostReviewService
from services.runtime_diagnostics import (
    record_runtime_exception,
    runtime_snapshot,
)
from services.uat_order_service import UatOrderService
from services.work_queue_service import WorkItem, parse_registered_at
from ui.components import display_value
from ui.dps_presenter import (
    installation_date_display,
    installation_date_value,
)
from ui.session_identity import can, current_actor
from ui.uat_presenters import answer_source_label


LOGGER = logging.getLogger(__name__)


def _processing_plan_for_inquiry(
    database: Database,
    inquiry: dict[str, Any],
    *,
    template_preferred: bool = True,
):
    return InquiryProcessingPlanService(database).create(
        inquiry,
        template_preferred=template_preferred,
    )


def program_answer_widget_key(
    inquiry_id: int,
    draft_id: int | str | None,
) -> str:
    return f"program_answer_{int(inquiry_id)}_{draft_id or 'empty'}"


def template_preference_key(inquiry: dict[str, Any]) -> str:
    """Return an inquiry-scoped Streamlit key for template preference."""

    store_id = str(
        inquiry.get("store_id") or inquiry.get("store_code") or "store"
    )
    inquiry_type = str(inquiry.get("inquiry_type") or "inquiry")
    return f"use_template_{store_id}_{inquiry_type}_{int(inquiry['id'])}"


def _ensure_initial_program_answer(
    database: Database,
    inquiry: dict[str, Any],
) -> bool:
    """Lazily repair historical unanswered rows that predate auto generation."""

    inquiry_id = int(inquiry["id"])
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    if active and is_valid_draft(active.get("original_answer")):
        return True
    attempt_key = f"automatic_initial_draft_attempted_{inquiry_id}"
    if st.session_state.get(attempt_key):
        return False
    st.session_state[attempt_key] = True
    with st.spinner("문의 유형을 분석해 초기 답변을 자동 생성하고 있습니다."):
        outcome = AutomaticDraftService(database).ensure_for_inquiry(
            inquiry_id,
            correlation_id=str(uuid.uuid4()),
        )
    if outcome.status in {"CREATED", "EXISTING"}:
        st.session_state[f"automatic_initial_draft_created_{inquiry_id}"] = {
            "draft_id": outcome.draft_id,
            "route": outcome.route,
        }
        st.rerun()
    return False


def load_program_answer_view(
    database: Database,
    inquiry_id: int,
) -> dict[str, Any]:
    """Build body and metadata from one selected draft identity."""

    answers = AnswerRepository(database)
    draft = (
        answers.active_for_inquiry(inquiry_id)
        or answers.latest_for_inquiry(inquiry_id)
    )
    provider_run = (
        GptProviderRunRepository(database).latest_for_draft(
            int(draft["id"])
        )
        if draft
        else None
    )
    mismatch = bool(
        provider_run
        and int(provider_run.get("draft_id") or 0)
        != int(draft["id"])
    )
    return {
        "draft": draft,
        "provider_run": None if mismatch else provider_run,
        "draft_id": draft.get("id") if draft else None,
        "provider_run_id": (
            provider_run.get("id") if provider_run and not mismatch else None
        ),
        "answer_version": (
            f"{draft.get('id')}:{draft.get('updated_at')}"
            if draft
            else None
        ),
        "mismatch": mismatch,
    }


def _record_ui_event(
    database: Database,
    inquiry_id: int,
    event_code: str,
    message: str,
    *,
    level: str = "INFO",
    details: dict[str, Any] | None = None,
) -> None:
    try:
        LogRepository(database).record_inquiry(
            inquiry_id,
            event_code,
            message,
            level=level,
            details=details or {},
        )
    except Exception:
        LOGGER.exception(
            "Activity event write failed: inquiry_id=%s event=%s",
            inquiry_id,
            event_code,
        )


def _developer_mode() -> bool:
    return os.getenv("QNA_DEVELOPER_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _masked_order_id(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) <= 8:
        return "<masked-order>"
    return f"{text[:4]}****{text[-4:]}"


PROGRESS_STAGES = (
    ("1", "문의 수신", "INQUIRY_COLLECTED"),
    ("2", "주문 조회", "NAVER_ORDER_LOOKUP"),
    ("3", "DPS 조회", "DPS_LOOKUP"),
    ("4", "답변 초안", "ANSWER_GENERATED"),
    ("5", "직원 검토", "STAFF_REVIEW"),
    ("6", "승인", "APPROVAL"),
    ("7", "네이버 등록", "NAVER_POST"),
)


def _item_key(item: WorkItem) -> str:
    return "|".join(
        str(item.get(name) or "")
        for name in ("store_code", "source", "inquiry_id", "registered_at")
    )


def _database_inquiry(
    database: Database, item: WorkItem
) -> dict[str, Any] | None:
    return InquiryRepository(database).get_by_source(
        str(item.get("store_code") or ""),
        str(item.get("source") or item.get("source_type") or ""),
        str(item.get("inquiry_id") or item.get("source_question_id") or ""),
    )


def _masked_customer(value: Any) -> str:
    text = display_value(value)
    return text if text == "-" or len(text) <= 1 else f"{text[0]}*{text[-1]}"


def _field(label: str, value: Any) -> str:
    return (
        '<div class="official-field"><span>'
        f"{escape(label)}</span><strong>{escape(display_value(value))}</strong></div>"
    )


def _status_tone(status: str) -> str:
    normalized = str(status or "PENDING").upper()
    if normalized in {"COMPLETED", "SUCCESS", "APPROVED", "POSTED"}:
        return "done"
    if normalized in {"FAILED", "TIMEOUT", "AGENT_OFFLINE", "PARSE_ERROR"}:
        return "error"
    if normalized in {"NEEDS_REVIEW", "NEEDS_ATTENTION", "NOT_FOUND"}:
        return "warning"
    if normalized == "RUNNING":
        return "running"
    return "pending"


def truncate_single_line(value: Any, maximum: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return "-"
    return text if len(text) <= maximum else text[: maximum - 1].rstrip() + "…"


def inquiry_list_summary(item: WorkItem) -> str:
    analysis = item.get("analysis")
    stored = item.get("summary") or item.get("inquiry_summary")
    if not stored and isinstance(analysis, dict):
        stored = analysis.get("summary")
    return truncate_single_line(
        stored or item.get("content") or item.get("title"), 46
    )


def paginate_items(
    items: list[WorkItem], page: int, page_size: int
) -> tuple[list[WorkItem], int, int]:
    safe_size = page_size if page_size in {10, 15, 20, 30} else 15
    total_pages = max(1, (len(items) + safe_size - 1) // safe_size)
    safe_page = min(max(1, int(page)), total_pages)
    start = (safe_page - 1) * safe_size
    return items[start : start + safe_size], safe_page, total_pages


def _render_pagination(current_page: int, total_pages: int) -> None:
    previous, page_label, next_page = st.columns(
        [1, 2.2, 1], gap="small", vertical_alignment="center"
    )
    if previous.button(
        "이전",
        disabled=current_page <= 1,
        key="dashboard_page_previous",
        width="stretch",
    ):
        st.session_state["dashboard_page"] = current_page - 1
        st.rerun()
    page_label.markdown(
        f'<div class="pagination-label"><b>{current_page}</b> / '
        f'{total_pages} 페이지</div>',
        unsafe_allow_html=True,
    )
    if next_page.button(
        "다음",
        disabled=current_page >= total_pages,
        key="dashboard_page_next",
        width="stretch",
    ):
        st.session_state["dashboard_page"] = current_page + 1
        st.rerun()


INQUIRY_LIST_WIDTHS = [1.15, 0.78, 1.55, 2.3, 1.2, 0.82, 1.7]


def _render_list_header(total_count: int) -> None:
    st.markdown(
        f'<div class="official-section-title"><div><h3>문의 리스트</h3>'
        f"<span>{total_count}건의 문의</span></div></div>",
        unsafe_allow_html=True,
    )
    headers = st.columns(INQUIRY_LIST_WIDTHS, gap="small")
    for column, label in zip(
        headers,
        ("문의 ID", "문의유형", "상품 정보", "문의 내용 요약", "주문번호", "상태", "접수 시간"),
    ):
        column.markdown(
            f'<div class="official-table-head">{escape(label)}</div>',
            unsafe_allow_html=True,
        )


def _render_list(items: list[WorkItem], total_count: int) -> WorkItem | None:
    if not items:
        st.info("현재 조건에 맞는 문의가 없습니다.")
        return None

    available_keys = {_item_key(item) for item in items}
    if st.session_state.get("selected_inquiry_key") not in available_keys:
        st.session_state["selected_inquiry_key"] = _item_key(items[0])
    selected_item: WorkItem | None = None
    for index, item in enumerate(items):
        key = _item_key(item)
        selected = st.session_state["selected_inquiry_key"] == key
        if selected:
            selected_item = item
        time_text = format_datetime_minute_kst(item.get("registered_at"))
        source = (
            "상품문의"
            if item.get("source") == "PRODUCT_INQUIRY"
            else "고객문의"
        )
        status = "답변완료" if item.get("answered") else (
            "생성가능"
            if item.get("queue") == "AUTO_PROCESSABLE"
            else "검토대기"
        )
        content = inquiry_list_summary(item)
        product_name = truncate_single_line(item.get("product_name"), 30)
        order_id = truncate_single_line(item.get("order_id"), 18)
        inquiry_id = truncate_single_line(
            item.get("inquiry_id") or item.get("source_question_id"), 17
        )
        row = st.container(
            key=(
                f"official_inquiry_row_{'selected' if selected else 'normal'}_"
                f"{index}_{abs(hash(key))}"
            )
        )
        with row:
            columns = st.columns(INQUIRY_LIST_WIDTHS, gap="small", vertical_alignment="center")
            if columns[0].button(
                inquiry_id,
                key=f"official_select_{index}_{abs(hash(key))}",
                type="primary" if selected else "secondary",
                width="stretch",
            ):
                st.session_state["selected_inquiry_key"] = key
                st.rerun()
            columns[1].markdown(
                f'<div class="official-cell"><span class="official-badge">{source}</span></div>',
                unsafe_allow_html=True,
            )
            columns[2].markdown(
                f'<div class="official-cell truncate" title="{escape(product_name)}">'
                f"{escape(product_name)}</div>",
                unsafe_allow_html=True,
            )
            columns[3].markdown(
                f'<div class="official-cell truncate" title="{escape(content)}">{escape(content)}</div>',
                unsafe_allow_html=True,
            )
            columns[4].markdown(
                f'<div class="official-cell truncate" title="{escape(order_id)}">'
                f"{escape(order_id)}</div>",
                unsafe_allow_html=True,
            )
            columns[5].markdown(
                f'<div class="official-cell"><span class="official-badge">'
                f"{escape(status)}</span></div>",
                unsafe_allow_html=True,
            )
            columns[6].markdown(
                f'<div class="official-cell received-time">{escape(time_text)}</div>',
                unsafe_allow_html=True,
            )

    if selected_item is None:
        selected_item = items[0]
        st.session_state["selected_inquiry_key"] = _item_key(selected_item)
    return selected_item


def _autosave_staff_edit(
    database_path: str,
    inquiry_id: int,
    draft_id: int,
    state_key: str,
    actor: str,
) -> None:
    try:
        database = Database(database_path)
        database.initialize()
        ApprovalService(database).save_edited_answer(
            inquiry_id=inquiry_id,
            draft_id=draft_id,
            edited_answer=str(st.session_state.get(state_key) or ""),
            actor=actor,
            autosave=True,
        )
        st.session_state["approval_ui_notice"] = (
            "success",
            "직원 수정본이 자동 저장되었습니다.",
        )
    except Exception as error:
        st.session_state["approval_ui_notice"] = ("error", str(error))


def _show_notice() -> None:
    notice = st.session_state.pop("approval_ui_notice", None)
    if not notice:
        return
    level, message = notice
    getattr(st, level if level in {"success", "error", "warning"} else "info")(
        message
    )


def build_gpt_diagnostics(
    draft: dict[str, Any] | None,
    provider_run: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    metadata = (
        draft.get("metadata_json")
        if draft and isinstance(draft.get("metadata_json"), dict)
        else {}
    )
    hybrid = (
        metadata.get("hybrid")
        if isinstance(metadata.get("hybrid"), dict)
        else {}
    )
    governance = (
        metadata.get("governance")
        if isinstance(metadata.get("governance"), dict)
        else {}
    )
    if not hybrid and not governance:
        return None
    intent = hybrid.get("intent") if isinstance(hybrid.get("intent"), dict) else {}
    gpt_draft = hybrid.get("draft") if isinstance(hybrid.get("draft"), dict) else {}
    validation = (
        hybrid.get("validation")
        if isinstance(hybrid.get("validation"), dict)
        else {}
    )
    facts = hybrid.get("facts") if isinstance(hybrid.get("facts"), dict) else {}
    return {
        "hybrid": hybrid,
        "intent": intent,
        "draft": gpt_draft,
        "validation": validation,
        "facts": facts,
        "confidence": gpt_draft.get(
            "confidence", intent.get("confidence")
        ),
        "governance": governance,
        "provider_run": provider_run or {},
    }


def _render_gpt_diagnostics(
    draft: dict[str, Any] | None,
    provider_run: dict[str, Any] | None = None,
) -> None:
    diagnostics = build_gpt_diagnostics(draft, provider_run)
    if diagnostics is None:
        st.caption("GPT 진단 정보가 없는 기존 Program Answer입니다.")
        return
    hybrid = diagnostics["hybrid"]
    intent = diagnostics["intent"]
    gpt_draft = diagnostics["draft"]
    validation = diagnostics["validation"]
    facts = diagnostics["facts"]
    confidence = diagnostics["confidence"]
    governance = diagnostics["governance"]
    run = diagnostics["provider_run"]
    columns = st.columns(4, gap="small")
    columns[0].metric("출처", answer_source_label(draft, provider_run))
    columns[1].metric(
        "사용 모델", governance.get("model") or run.get("model") or "-"
    )
    columns[2].metric(
        "생성 시간",
        format_datetime_kst(run.get("completed_at") or run.get("created_at")),
    )
    columns[3].metric(
        "응답 시간",
        f'{int(run.get("duration_ms") or 0):,} ms' if run else "-",
    )
    usage_columns = st.columns(4, gap="small")
    usage_columns[0].metric(
        "입력 토큰",
        f'{int(run.get("input_tokens") or 0):,}' if run.get("input_tokens") is not None else "-",
    )
    usage_columns[1].metric(
        "출력 토큰",
        f'{int(run.get("output_tokens") or 0):,}' if run.get("output_tokens") is not None else "-",
    )
    usage_columns[2].metric(
        "전체 토큰",
        f'{int(run.get("total_tokens") or 0):,}' if run.get("total_tokens") is not None else "-",
    )
    cost = run.get("estimated_cost_krw")
    usage_columns[3].metric(
        "예상 비용", f"약 {float(cost):,.2f}원" if cost is not None else "계산 전"
    )
    quality_columns = st.columns(4, gap="small")
    quality_columns[0].metric("GPT Confidence", f"{float(confidence or 0):.0%}")
    quality_columns[1].metric("Emotion", intent.get("emotion") or "-")
    quality_columns[2].metric("Intent", intent.get("category") or "-")
    quality_columns[3].metric(
        "Validator",
        (
            "RULE FALLBACK"
            if hybrid.get("fallback_used")
            else ("PASS" if validation.get("passed") else "FAIL")
        ),
    )
    governance_columns = st.columns(3, gap="small")
    governance_columns[0].metric(
        "실행 모드", governance.get("mode") or run.get("mode") or "-"
    )
    governance_columns[1].metric(
        "Provider",
        governance.get("provider") or run.get("provider") or "-",
    )
    governance_columns[2].metric("실행 결과", "Fallback" if hybrid.get("fallback_used") else "완료")
    with st.expander("GPT Facts · Questions · Validation", expanded=False):
        left, right = st.columns(2, gap="medium")
        with left:
            st.markdown("**Questions**")
            questions = intent.get("questions") or []
            st.write(questions if questions else ["-"])
            st.markdown("**Used Facts**")
            st.write(gpt_draft.get("used_facts") or ["-"])
            st.markdown("**Missing Facts**")
            st.write(gpt_draft.get("missing_information") or ["없음"])
        with right:
            st.markdown("**Warnings**")
            warnings = [
                *(gpt_draft.get("warnings") or []),
                *(facts.get("warnings") or []),
                *(validation.get("warnings") or []),
            ]
            st.write(list(dict.fromkeys(warnings)) or ["없음"])
            st.markdown("**Validator 결과**")
            st.write(
                {
                    "passed": validation.get("passed"),
                    "errors": validation.get("errors") or [],
                    "checked_facts": validation.get("checked_facts") or [],
                    "fallback_reason": hybrid.get("fallback_reason"),
                }
            )
            st.markdown("**Governance**")
            st.write(
                {
                    "prompt_version": governance.get("prompt_version")
                    or run.get("prompt_version"),
                    "privacy_safe": (
                        governance.get("privacy") or {}
                    ).get("safe_to_send"),
                    "privacy_removed_count": run.get(
                        "privacy_removed_count", 0
                    ),
                    "retry_count": run.get("retry_count", 0),
                    "fallback_reason": governance.get("fallback_reason"),
                    "shadow": governance.get("shadow"),
                    "canary_selected": governance.get(
                        "canary_selected", False
                    ),
                }
            )


def _render_answer_panel(database: Database, inquiry: dict[str, Any]) -> None:
    inquiry_id = int(inquiry["id"])
    view_key = f"answer_workspace_view_{inquiry_id}"
    pending_view_key = f"answer_pending_program_view_{inquiry_id}"
    pending_success_key = f"pending_generation_success_{inquiry_id}"
    draft_session_key = f"draft_text_{inquiry_id}"
    pending_draft_text_key = f"pending_draft_text_{inquiry_id}"
    draft_identity_key = f"draft_text_identity_{inquiry_id}"
    view_model = load_program_answer_view(database, inquiry_id)
    draft = view_model["draft"]
    source_answered = bool(inquiry.get("source_answered"))
    posted_answer_record = NaverPostedAnswerRepository(database).current(
        inquiry_id
    )
    posted_answer_available = bool(
        posted_answer_record
        and posted_answer_record.get("fetch_status") == "AVAILABLE"
        and str(posted_answer_record.get("answer_body") or "").strip()
    )
    posted_answer_body = (
        str(posted_answer_record.get("answer_body") or "")
        if posted_answer_available and posted_answer_record is not None
        else ""
    )
    pending_draft_text = st.session_state.pop(
        pending_draft_text_key, None
    )
    current_draft_id = int(draft["id"]) if draft else None
    if is_valid_draft(pending_draft_text):
        st.session_state[draft_session_key] = pending_draft_text
        st.session_state[draft_identity_key] = current_draft_id
    elif (
        draft_identity_key not in st.session_state
        or st.session_state[draft_identity_key] != current_draft_id
    ):
        st.session_state[draft_session_key] = (
            str(draft.get("original_answer") or "")
            if draft
            else "답변 생성 버튼을 눌러 초안을 생성하세요."
        )
        st.session_state[draft_identity_key] = current_draft_id
    pending_draft_id = st.session_state.pop(pending_view_key, None)
    if (
        pending_draft_id is not None
        and draft
        and int(pending_draft_id) == int(draft["id"])
    ):
        # This executes before segmented_control is instantiated. Mutating the
        # widget key after its creation raises StreamlitAPIException and was the
        # reason a saved GPT draft appeared not to refresh.
        st.session_state[view_key] = "Program Answer"
    answers = AnswerRepository(database)
    approvals = ApprovalRepository(database)
    provider_run = view_model["provider_run"]
    state = approvals.get_inquiry_approval(inquiry_id)
    posted = answers.is_inquiry_posted(inquiry_id)
    approved = state["approval_status"] == "APPROVED"
    actor = current_actor()
    can_edit = can(Permission.STAFF_EDIT)
    can_approve = can(Permission.APPROVE)
    diagnostics = build_gpt_diagnostics(draft, provider_run)
    program_answer = str(draft.get("original_answer") or "") if draft else ""
    final_answer = str(draft.get("final_answer") or "") if draft else ""
    validator_passed = bool(
        diagnostics
        and diagnostics["validation"].get("passed")
        and not diagnostics["hybrid"].get("fallback_used")
    )
    if approved:
        workspace_status = "승인 완료"
    elif diagnostics and not validator_passed:
        workspace_status = "Validator 확인 필요"
    elif draft:
        workspace_status = "검토 대기"
    else:
        workspace_status = "초안 없음"

    st.markdown(
        '<div class="official-section-title answer workspace-title">'
        '<div><h3>답변 검토 및 승인</h3>'
        '<span>분석 · 답변 · 검증 · 승인을 한 곳에서 처리합니다.</span></div>'
        f'<span class="official-state {_status_tone(state["approval_status"])}">'
        f"{escape(workspace_status)}</span></div>",
        unsafe_allow_html=True,
    )
    _show_notice()
    if draft:
        from ui.learning_performance import render_answer_learning_provenance
        review = PostReviewRepository(database).get(inquiry_id)
        outcome = None
        if review:
            status = str(review.get("status") or "")
            if status == "CORRECTED_AND_REPOSTED":
                outcome = "직원 수정"
            elif status == "REVIEWED_NO_CHANGE":
                outcome = "수정 없음"
            elif str(inquiry.get("post_status") or "") == "POSTED":
                outcome = "자동등록 성공 · 관찰 중"
        render_answer_learning_provenance(
            database, draft_id=int(draft["id"]), outcome=outcome
        )

    generating_key = f"gpt_generation_running_{inquiry_id}"
    use_template_key = template_preference_key(inquiry)
    use_template = st.checkbox(
        "기존 템플릿 우선 사용",
        value=True,
        key=use_template_key,
    )
    inquiry_analysis = _processing_plan_for_inquiry(
        database,
        inquiry,
        template_preferred=use_template,
    )
    latest_delivery_dps = None
    if inquiry_analysis.can_execute_dps_lookup:
        latest_delivery_dps = DpsRepository(
            database
        ).get_preferred_for_inquiry_and_order(
            inquiry_id, str(inquiry.get("order_id") or "")
        )
    latest_delivery_normalized = (
        latest_delivery_dps.get("normalized_result_json")
        if latest_delivery_dps
        and isinstance(latest_delivery_dps.get("normalized_result_json"), dict)
        else {}
    )
    if not inquiry_analysis.requires_order_lookup:
        st.info(
            "이 문의는 배송·설치 문의가 아니므로 주문 및 DPS 조회 없이 "
            "답변을 생성합니다."
        )
    elif not inquiry_analysis.order_id_validated:
        st.info(
            "주문번호가 없어 배송 일정을 확인할 수 없습니다. "
            "주문번호 요청 답변을 생성합니다."
        )
    elif (
        latest_delivery_normalized.get("installation_date")
        or latest_delivery_normalized.get("required_delivery_date")
    ):
        st.info("확인된 설치예정일을 사용해 배송 답변을 생성합니다.")
    if use_template:
        st.caption(
            "일치하는 운영 템플릿이 있으면 우선 사용하고,\n"
            "일치하는 템플릿이 없으면 GPT가 새로운 초안을 생성합니다."
        )
    else:
        st.caption(
            "기존 템플릿을 사용하지 않고\n"
            "GPT가 새로운 답변 초안을 생성합니다."
        )
    if draft and not posted:
        st.warning(
            "현재 작성 중인 초안이 있습니다. 새 답변을 생성하면 기존 "
            "초안은 이력으로 보존되고 새 초안이 활성화됩니다."
        )
    top_actions = st.columns([1.25, 0.8, 0.95, 3.7], gap="small")
    generate_label = (
        "주문번호 요청 답변 생성"
        if inquiry_analysis.delivery_question
        and not inquiry_analysis.order_id_validated
        else "확인된 설치예정일로 배송 답변 생성"
        if inquiry_analysis.delivery_question
        and latest_delivery_normalized.get("installation_date")
        else "배송 안전 답변 생성"
        if inquiry_analysis.delivery_question
        else "템플릿 우선 답변 생성"
        if use_template
        else "GPT 새 답변 생성"
    )
    generate = top_actions[0].button(
        generate_label,
        disabled=posted or bool(st.session_state.get(generating_key)),
        type="primary",
        width="stretch",
        key=f"review_generate_{inquiry_id}",
    )
    reset = top_actions[1].button(
        "초기화",
        disabled=(
            not draft
            or not can_edit
            or (not source_answered and (posted or approved))
        ),
        width="stretch",
        key=f"review_reset_{inquiry_id}",
    )
    save = top_actions[2].button(
        "임시 저장",
        disabled=(
            not draft
            or not can_edit
            or (not source_answered and (posted or approved))
        ),
        width="stretch",
        key=f"review_save_{inquiry_id}",
    )
    top_actions[3].markdown(
        '<div class="workspace-lock-note">승인은 Final Answer만 생성합니다.'
        " <b>네이버 등록 잠금</b></div>",
        unsafe_allow_html=True,
    )

    analysis_column, answer_column = st.columns(
        [0.7, 3.3], gap="medium"
    )
    source = answer_source_label(draft, provider_run)
    governance = diagnostics["governance"] if diagnostics else {}
    intent = diagnostics["intent"] if diagnostics else {}
    phase9_analysis = (
        dict(draft.get("inquiry_analysis_json") or {})
        if draft
        else {}
    )
    phase9_selected = (
        dict(draft.get("selected_facts_json") or {})
        if draft
        else {}
    )
    phase9_validator = (
        dict(draft.get("validator_result_json") or {})
        if draft
        else {}
    )
    warnings = []
    if diagnostics:
        warnings = list(
            dict.fromkeys(
                [
                    *(diagnostics["draft"].get("warnings") or []),
                    *(diagnostics["facts"].get("warnings") or []),
                    *(diagnostics["validation"].get("warnings") or []),
                ]
            )
        )
    with analysis_column:
        st.markdown(
            '<div class="compact-analysis-card"><h4>분석 결과</h4>'
            f'{_field("문의 유형", intent.get("category") or inquiry.get("inquiry_type"))}'
            f'{_field("답변 출처", source)}'
            f'{_field("자동 답변", "가능" if validator_passed else "검토 필요")}'
            f'{_field("직원 검토", "필요" if not approved else "완료")}'
            f'{_field("Provider", governance.get("provider") or (provider_run or {}).get("provider"))}'
            f'{_field("사용 Rule", (diagnostics or {}).get("hybrid", {}).get("rule_id"))}'
            f'{_field("경고", f"{len(warnings)}건" if warnings else "없음")}'
            "</div>",
            unsafe_allow_html=True,
        )
        if phase9_analysis:
            st.caption(
                " · ".join(
                    (
                        f"유형: {phase9_analysis.get('inquiry_type') or '-'}",
                        f"전략: {phase9_analysis.get('answer_strategy') or '-'}",
                        f"주문번호: {phase9_analysis.get('order_id_status') or '-'}",
                        "직원 검토 필요"
                        if phase9_analysis.get("manual_review_required")
                        or phase9_validator.get("status") == "REVIEW_REQUIRED"
                        else "자동 답변 가능",
                    )
                )
            )
    with answer_column:
        rendered_program_text: str | None = None
        answer_views = ["Program Answer", "직원 수정본"]
        if source_answered:
            answer_views.append("네이버 실제 등록 답변")
        answer_views.append("Final Answer")
        selected_view = st.segmented_control(
            "답변 보기",
            answer_views,
            default=(
                None
                if view_key in st.session_state
                else "네이버 실제 등록 답변"
                if source_answered
                else "직원 수정본"
                if draft and not approved
                else "Final Answer"
                if approved
                else "Program Answer"
            ),
            key=view_key,
            label_visibility="collapsed",
            width="stretch",
        )
        edit_key = (
            f"staff_edit_{inquiry_id}_{draft['id']}"
            if draft
            else f"staff_edit_{inquiry_id}_empty"
        )
        if selected_view == "직원 수정본" and draft:
            if edit_key not in st.session_state:
                st.session_state[edit_key] = str(
                    draft.get("edited_answer")
                    or posted_answer_body
                    or draft.get("original_answer")
                    or ""
                )
            st.text_area(
                "직원 수정본",
                height=360,
                key=edit_key,
                disabled=(
                    not can_edit
                    or (not source_answered and (posted or approved))
                ),
                label_visibility="collapsed",
                on_change=_autosave_staff_edit,
                args=(
                    str(database.path),
                    inquiry_id,
                    int(draft["id"]),
                    edit_key,
                    actor,
                ),
            )
            st.caption(
                "내부 교정 및 Learning용 수정입니다. 네이버에 재등록되지 않습니다."
                if source_answered
                else "변경 내용은 자동 저장되며 임시 저장으로 즉시 확정할 수 있습니다."
            )
        elif selected_view == "네이버 실제 등록 답변":
            if posted_answer_available:
                st.text_area(
                    "네이버 실제 등록 답변",
                    value=posted_answer_body,
                    height=360,
                    disabled=True,
                    label_visibility="collapsed",
                    key=f"naver_posted_answer_{inquiry_id}_{posted_answer_record['id']}",
                )
                posted_metadata = ["NAVER_POSTED", "Source of Truth"]
                if posted_answer_record.get("posted_at"):
                    posted_metadata.append(
                        "등록 "
                        + format_datetime_kst(posted_answer_record.get("posted_at"))
                    )
                if posted_answer_record.get("answer_id"):
                    posted_metadata.append(
                        f"답변 ID {posted_answer_record['answer_id']}"
                    )
                if posted_answer_record.get("source_api"):
                    posted_metadata.append(
                        f"Source {posted_answer_record['source_api']}"
                    )
                st.caption(" · ".join(posted_metadata))
            else:
                st.warning(
                    "네이버에서는 답변완료 상태이지만 현재 조회 응답에 "
                    "실제 답변 본문이 없어 NOT_FETCHED로 기록했습니다. "
                    "Program Answer를 대신 표시하지 않습니다."
                )
        elif selected_view == "Final Answer":
            st.text_area(
                "Final Answer",
                value=final_answer or "승인 후 Final Answer가 생성됩니다.",
                height=360,
                disabled=True,
                label_visibility="collapsed",
                key=(
                    f"final_answer_{inquiry_id}_"
                    f"{draft['id'] if draft else 'empty'}_{approved}"
                ),
            )
        else:
            rendered_program_text = st.text_area(
                "Program Answer",
                height=360,
                disabled=True,
                label_visibility="collapsed",
                key=draft_session_key,
            )

        correction_reason = ""
        correction_note = ""
        corrected_intent = ""
        edited_value = (
            str(st.session_state.get(edit_key) or "")
            if draft
            else ""
        )
        original_value = (
            posted_answer_body
            if posted_answer_available
            else str(draft.get("original_answer") or "")
            if draft
            else ""
        )
        answer_was_corrected = bool(
            draft
            and format_final_answer(edited_value)
            and format_final_answer(edited_value)
            != format_final_answer(original_value)
        )
        if answer_was_corrected:
            with st.expander("수정 피드백", expanded=False):
                st.caption(
                    "자동답변에서 잘못된 부분을 기록하면 직원 수정본과 함께 "
                    "높은 우선순위의 교정 신호로 저장됩니다."
                )
                stored_feedback = LearningFeedbackRepository(
                    database
                ).for_inquiry(inquiry_id)
                if stored_feedback:
                    latest_feedback = stored_feedback[-1]
                    st.caption(
                        "저장된 교정 · "
                        f"{latest_feedback.get('correction_reason')}"
                        + (
                            f" · {latest_feedback.get('correction_note')}"
                            if latest_feedback.get("correction_note")
                            else ""
                        )
                    )
                reason_labels = [
                    CORRECTION_REASON_LABELS[reason]
                    for reason in CorrectionReason
                ]
                selected_reason = st.selectbox(
                    "수정 사유",
                    ["선택하지 않음", *reason_labels],
                    key=f"staff_correction_reason_{inquiry_id}_{draft['id']}",
                )
                selected = CORRECTION_REASON_BY_LABEL.get(selected_reason)
                correction_reason = selected.value if selected else ""
                if selected is CorrectionReason.ROUTING_ERROR:
                    intent_labels = list(INTENT_OPTIONS.values())
                    intent_label = st.selectbox(
                        "올바른 문의 유형",
                        intent_labels,
                        key=f"staff_corrected_intent_{inquiry_id}_{draft['id']}",
                    )
                    corrected_intent = next(
                        code
                        for code, label in INTENT_OPTIONS.items()
                        if label == intent_label
                    )
                correction_note = st.text_input(
                    "상세 메모 (선택)",
                    key=f"staff_correction_note_{inquiry_id}_{draft['id']}",
                )

        run = (diagnostics or {}).get("provider_run") or {}
        confirmed_facts = (
            (diagnostics or {}).get("hybrid", {}).get(
                "confirmed_facts"
            )
            or {}
        )
        st.markdown(
            '<div class="answer-meta-strip">'
            f'<span>출처 <b>{escape(source)}</b></span>'
            f'<span>모델 <b>{escape(str(governance.get("model") or run.get("model") or "-"))}</b></span>'
            f'<span>응답 <b>{int(run.get("duration_ms") or 0):,} ms</b></span>'
            f'<span>토큰 <b>{int(run.get("total_tokens") or 0):,}</b></span>'
            f'<span>비용 <b>{escape(str(run.get("estimated_cost_krw") or "-"))}</b></span>'
            f'<span>Draft <b>{escape(str(draft.get("id") if draft else "-"))}</b></span>'
            f'<span>설치예정일 <b>{escape(str(confirmed_facts.get("installation_date") or "-"))}</b></span>'
            "</div>",
            unsafe_allow_html=True,
        )
        if _developer_mode():
            with st.expander("개발자용 Draft 추적", expanded=False):
                st.write(
                    {
                        "draft_id": view_model.get("draft_id"),
                        "provider_run_id": view_model.get(
                            "provider_run_id"
                        ),
                        "answer_version": view_model.get(
                            "answer_version"
                        ),
                        "inquiry_id": inquiry_id,
                        "masked_order_id": _masked_order_id(
                            inquiry.get("order_id")
                        ),
                        "created_at": format_datetime_kst(
                            draft.get("created_at") if draft else None
                        ),
                        "widget_key": program_answer_widget_key(
                            inquiry_id,
                            draft["id"] if draft else None,
                        ),
                        "phase9_analysis": phase9_analysis,
                        "selected_fact_keys": phase9_selected.get(
                            "keys", []
                        ),
                        "validator_rules": phase9_validator.get(
                            "rules", []
                        ),
                    }
                )
        if draft and draft.get("stale"):
            st.warning(
                "DPS 설치예정일이 변경되어 GPT 답변을 다시 생성해야 합니다."
            )
        if draft:
            render_log_key = (
                f"gpt_program_answer_rendered_{inquiry_id}_{draft['id']}"
            )
            if not st.session_state.get(render_log_key):
                render_details = {
                    "draft_id": draft["id"],
                    "provider_run_id": (
                        provider_run or {}
                    ).get("id"),
                    "dps_lookup_id": draft.get("dps_lookup_id"),
                    "status": "RENDERED",
                    "model": (provider_run or {}).get("model"),
                    "widget_key": program_answer_widget_key(
                        inquiry_id, draft["id"]
                    ),
                }
                _record_ui_event(
                    database,
                    inquiry_id,
                    "GPT_PROGRAM_ANSWER_RENDERED",
                    "활성 Program Answer를 화면에 표시했습니다.",
                    details=render_details,
                )
                _record_ui_event(
                    database,
                    inquiry_id,
                    "PROGRAM_ANSWER_REFRESHED",
                    "활성 draft와 동일한 Program Answer를 갱신했습니다.",
                    details={
                        **render_details,
                        "status": "MATCHED",
                    },
                )
                st.session_state[render_log_key] = True
        pending_success = st.session_state.get(pending_success_key)
        if isinstance(pending_success, dict):
            expected_draft_id = pending_success.get("draft_id")
            expected_text = pending_success.get("draft_text")
            render_verified = bool(
                draft
                and draft.get("is_active")
                and expected_draft_id is not None
                and int(draft["id"]) == int(expected_draft_id)
                and is_valid_draft(expected_text)
                and is_valid_draft(program_answer)
                and expected_text == program_answer
                and selected_view == "Program Answer"
                and is_valid_draft(rendered_program_text)
                and rendered_program_text == program_answer
            )
            if render_verified:
                metadata = dict(draft.get("metadata_json") or {})
                generation_mode = str(
                    metadata.get("generation_mode") or ""
                ).upper()
                completion_details = {
                    "inquiry_id": inquiry_id,
                    "answer_type": metadata.get("answer_type"),
                    "answer_source": metadata.get("answer_source"),
                    "generation_mode": metadata.get("generation_mode"),
                    "template_preferred": bool(
                        metadata.get("template_preferred")
                    ),
                    "template_override": bool(
                        metadata.get("template_override")
                    ),
                    "template_id": metadata.get("template_id"),
                    "order_id_present": bool(
                        metadata.get("order_id_present")
                    ),
                    "delivery_question": bool(
                        metadata.get("delivery_question")
                    ),
                    "dps_lookup_attempted": bool(
                        metadata.get("dps_lookup_attempted")
                    ),
                    "delivery_date_found": bool(
                        metadata.get("delivery_date_found")
                    ),
                    "gpt_called": bool(metadata.get("gpt_called")),
                    "draft_id": int(draft["id"]),
                    "draft_length": len(program_answer.strip()),
                    "draft_saved": True,
                    "active_draft_id": int(draft["id"]),
                    "rendered_draft_id": int(draft["id"]),
                }
                _record_ui_event(
                    database,
                    inquiry_id,
                    "ANSWER_GENERATION_RENDERED",
                    "새 활성 Draft를 Program Answer에 표시했습니다.",
                    details=completion_details,
                )
                _record_ui_event(
                    database,
                    inquiry_id,
                    "DRAFT_RENDERED",
                    "저장된 Draft 본문을 Program Answer에 표시했습니다.",
                    details=completion_details,
                )
                st.session_state.pop(pending_success_key, None)
                if generation_mode == "GPT_FALLBACK":
                    st.success(
                        "적용 가능한 기존 템플릿이 없어 GPT로 새 답변을 "
                        f"생성했습니다. Draft ID: {int(draft['id'])}"
                    )
                elif generation_mode == "TEMPLATE":
                    st.success(
                        "기존 템플릿으로 답변을 생성했습니다. "
                        f"Draft ID: {int(draft['id'])}"
                    )
                elif generation_mode == "GPT_DIRECT":
                    st.success(
                        "기존 템플릿을 건너뛰고 GPT로 새 답변을 "
                        f"생성했습니다. Draft ID: {int(draft['id'])}"
                    )
                else:
                    st.success(
                        f"답변 초안이 작성되었습니다. 생성 방식: "
                        f"{generation_mode or 'UNKNOWN'} · "
                        f"Draft ID: {int(draft['id'])}"
                    )
            else:
                _record_ui_event(
                    database,
                    inquiry_id,
                    "ANSWER_GENERATION_RENDER_VERIFICATION_FAILED",
                    "새 Draft의 Program Answer 렌더링 검증에 실패했습니다.",
                    level="ERROR",
                    details={
                        "inquiry_id": inquiry_id,
                        "draft_id": expected_draft_id,
                        "active_draft_id": (
                            draft.get("id")
                            if draft and draft.get("is_active")
                            else None
                        ),
                        "rendered_draft_id": None,
                        "draft_saved": bool(draft),
                    },
                )
                st.error(
                    "새 초안은 저장되었지만 Program Answer 표시를 확인하지 "
                    "못해 성공 처리하지 않았습니다."
                )

    validator_message = (
        "사실 일치 · PII 없음 · 금지 표현 없음"
        if validator_passed
        else "검증 결과를 확인하고 직원 검토를 진행하세요."
    )
    st.markdown(
        f'<div class="validator-status-bar {"passed" if validator_passed else "warning"}">'
        f'<strong>{"✓ Validator 통과" if validator_passed else "! Validator 확인 필요"}</strong>'
        f"<span>{escape(validator_message)}</span></div>",
        unsafe_allow_html=True,
    )
    if diagnostics:
        with st.expander("Validator 및 GPT 상세", expanded=False):
            _render_gpt_diagnostics(draft, provider_run)

    bottom_left, bottom_actions = st.columns([3.1, 2.0], gap="medium")
    with bottom_left:
        cancel_reason = st.text_input(
            "승인 취소 사유",
            placeholder="승인 취소 시 사유를 입력해 주세요.",
            disabled=posted or not approved,
            key=f"cancel_reason_{inquiry_id}",
            label_visibility="collapsed",
        )
    action_columns = bottom_actions.columns(3, gap="small")
    copy = action_columns[0].button(
        "복사",
        disabled=not bool(
            final_answer
            or (draft and draft.get("edited_answer"))
            or program_answer
        ),
        width="stretch",
        key=f"review_copy_{inquiry_id}",
    )
    cancel = action_columns[1].button(
        "승인 취소",
        disabled=not draft or posted or not approved or not can_approve,
        width="stretch",
        key=f"review_cancel_{inquiry_id}",
    )
    approve = action_columns[2].button(
        "승인",
        disabled=(
            not can_approve
            or (
                not posted_answer_available
                if source_answered
                else not draft or posted or approved
            )
        ),
        type="primary",
        width="stretch",
        key=f"review_approve_{inquiry_id}",
    )

    generation_stage = "button"
    generation_correlation_id: str | None = None
    generated_draft_id: int | None = None
    try:
        if generate:
            generation_correlation_id = str(uuid.uuid4())
            _record_ui_event(
                database,
                inquiry_id,
                "GPT_BUTTON_CLICKED",
                "GPT 답변 생성 버튼 클릭을 확인했습니다.",
                details={
                    "correlation_id": generation_correlation_id,
                    "masked_order_id": _masked_order_id(
                        inquiry.get("order_id")
                    ),
                    "stage": "button",
                    "button_key": f"review_generate_{inquiry_id}",
                    "template_preferred": bool(use_template),
                    "template_override": not bool(use_template),
                },
            )
            st.session_state[generating_key] = True
            try:
                with st.status(
                    "Facts 준비 중",
                    expanded=True,
                ) as generation_status:
                    generation_stage = "facts"
                    generation_status.write("문의 정보 준비 중")
                    generation_status.write("주문 및 설치정보 확인 중")
                    generation_stage = "routing"
                    generation_status.write("답변 우선순위 확인 중")
                    outcome = AnswerService(
                        database
                    ).generate_for_inquiry(
                        inquiry_id,
                        prefer_template=use_template,
                        correlation_id=generation_correlation_id,
                        processing_plan=inquiry_analysis,
                    )
                    generated_draft_id = int(outcome.draft["id"])
                    generated_text = outcome.draft.get("original_answer")
                    if not is_valid_draft(generated_text):
                        raise RuntimeError("EMPTY_GENERATED_DRAFT")
                    generation_stage = "validator"
                    generation_status.write("답변 검증 완료")
                    generation_stage = "repository_reload"
                    generation_status.write("초안 저장 및 repository 재조회 중")
                    reloaded_view = load_program_answer_view(
                        database, inquiry_id
                    )
                    active_draft = reloaded_view.get("draft")
                    if outcome.draft.get("is_active"):
                        if (
                            not active_draft
                            or int(active_draft["id"])
                            != generated_draft_id
                        ):
                            raise RuntimeError(
                                "GPT_ACTIVE_DRAFT_RELOAD_MISMATCH"
                            )
                        reloaded_text = active_draft.get("original_answer")
                        if (
                            not is_valid_draft(reloaded_text)
                            or reloaded_text != generated_text
                        ):
                            raise RuntimeError(
                                "ACTIVE_DRAFT_TEXT_RELOAD_MISMATCH"
                            )
                    generation_stage = "ui_refresh"
                    generation_status.write("Program Answer 갱신 중")
                    generation_status.update(
                        label="화면 갱신 완료",
                        state="complete",
                    )
            finally:
                st.session_state[generating_key] = False
            if outcome.draft.get("is_active"):
                st.session_state[pending_view_key] = generated_draft_id
                st.session_state[pending_draft_text_key] = generated_text
                st.session_state[
                    f"active_program_draft_id_{inquiry_id}"
                ] = generated_draft_id
                st.session_state[pending_success_key] = {
                    "draft_id": generated_draft_id,
                    "draft_text": generated_text,
                    "generation_mode": outcome.result.metadata.get(
                        "generation_mode"
                    ),
                }
                _record_ui_event(
                    database,
                    inquiry_id,
                    "GPT_ACTIVE_DRAFT_UPDATED",
                    "새 GPT 초안을 활성 Program Answer로 지정했습니다.",
                    details={
                        "correlation_id": generation_correlation_id,
                        "draft_id": generated_draft_id,
                        "provider_run_id": reloaded_view.get(
                            "provider_run_id"
                        ),
                        "stage": "active_reload",
                        "status": "ACTIVE",
                    },
                )
                _record_ui_event(
                    database,
                    inquiry_id,
                    "GPT_REPOSITORY_RELOADED",
                    "활성 GPT 초안을 repository에서 다시 확인했습니다.",
                    details={
                        "correlation_id": generation_correlation_id,
                        "draft_id": generated_draft_id,
                        "provider_run_id": reloaded_view.get(
                            "provider_run_id"
                        ),
                        "stage": "repository_reload",
                        "status": "MATCHED",
                    },
                )
            if not outcome.draft.get("is_active"):
                st.session_state["approval_ui_notice"] = (
                    "warning",
                    "새 답변 초안을 이력으로 저장했습니다. 승인 완료된 "
                    "Final Answer와 활성 Draft는 변경하지 않았습니다.",
                )
            st.rerun()
        if reset and draft:
            ApprovalService(database).reset_edited_answer(
                inquiry_id=inquiry_id,
                draft_id=int(draft["id"]),
                actor=actor,
            )
            st.session_state.pop(edit_key, None)
            st.session_state["approval_ui_notice"] = (
                "success",
                "직원 수정본을 초기화했습니다.",
            )
            st.rerun()
        if save and draft:
            ApprovalService(database).save_edited_answer(
                inquiry_id=inquiry_id,
                draft_id=int(draft["id"]),
                edited_answer=str(st.session_state.get(edit_key) or ""),
                actor=actor,
                correction_reason=correction_reason,
                correction_note=correction_note,
                corrected_intent=corrected_intent,
            )
            st.session_state["approval_ui_notice"] = (
                "success",
                "직원 수정본을 저장했습니다.",
            )
            st.rerun()
        if approve and source_answered:
            ApprovalService(database).approve_posted_answer(
                inquiry_id=inquiry_id,
                actor=actor,
            )
            st.session_state["approval_ui_notice"] = (
                "success",
                "네이버 실제 등록 답변을 Positive Learning으로 승인했습니다. "
                "네이버 재등록은 수행하지 않았습니다.",
            )
            st.rerun()
        if approve and draft and not source_answered:
            ApprovalService(database).approve(
                inquiry_id=inquiry_id,
                draft_id=int(draft["id"]),
                actor=actor,
                correction_reason=correction_reason,
                correction_note=correction_note,
                corrected_intent=corrected_intent,
            )
            st.session_state["approval_ui_notice"] = (
                "success",
                "승인 완료했습니다. 네이버 등록은 잠금 상태입니다.",
            )
            st.rerun()
        if cancel and draft:
            ApprovalService(database).cancel_approval(
                inquiry_id=inquiry_id,
                draft_id=int(draft["id"]),
                reason=cancel_reason,
                actor=actor,
            )
            st.session_state["approval_ui_notice"] = (
                "success",
                "승인을 취소했습니다.",
            )
            st.rerun()
        if copy:
            copy_text = (
                final_answer
                or str(draft.get("edited_answer") or "")
                or program_answer
            )
            encoded = json.dumps(copy_text, ensure_ascii=False)
            components.html(
                f"""
                <script>
                const text = {encoded};
                const fallback = () => {{
                  const area = document.createElement("textarea");
                  area.value = text;
                  document.body.appendChild(area);
                  area.select();
                  document.execCommand("copy");
                  area.remove();
                }};
                if (navigator.clipboard && window.isSecureContext) {{
                  navigator.clipboard.writeText(text).catch(fallback);
                }} else {{
                  fallback();
                }}
                </script>
                """,
                height=0,
                width=0,
            )
            st.toast("답변 본문을 클립보드에 복사했습니다.")
    except SystemExit as error:
        record_runtime_exception(
            "STREAMLIT_RUNTIME_EXCEPTION",
            error,
            inquiry_id=inquiry_id,
            correlation_id=generation_correlation_id,
            stage=generation_stage,
        )
        _record_ui_event(
            database,
            inquiry_id,
            "GPT_UI_REFRESH_FAILED",
            "GPT 실행 경계에서 종료 요청을 안전하게 차단했습니다.",
            level="ERROR",
            details={
                "correlation_id": generation_correlation_id,
                "draft_id": generated_draft_id,
                "stage": generation_stage,
                "error_category": "SystemExit",
            },
        )
        st.error(
            "요청 처리 중 오류가 발생했습니다. 저장된 초안은 새로고침 후 "
            "다시 확인할 수 있습니다."
        )
    except (ApprovalError, AnswerAlreadyPostedError, ValueError) as error:
        st.error(str(error))
    except Exception as error:
        record_runtime_exception(
            "STREAMLIT_RUNTIME_EXCEPTION",
            error,
            inquiry_id=inquiry_id,
            correlation_id=generation_correlation_id,
            stage=generation_stage,
        )
        _record_ui_event(
            database,
            inquiry_id,
            (
                "GPT_UI_REFRESH_FAILED"
                if generation_correlation_id
                else "PROGRAM_ANSWER_RENDER_FAILED"
            ),
            "Program Answer 처리 중 오류가 발생했습니다.",
            level="ERROR",
            details={
                "correlation_id": generation_correlation_id,
                "draft_id": generated_draft_id,
                "stage": generation_stage,
                "error_category": error.__class__.__name__,
            },
        )
        if generated_draft_id:
            st.error(
                "초안은 저장됐지만 화면 갱신에 실패했습니다. "
                f"저장된 Draft ID: {generated_draft_id}"
            )
        else:
            st.error(
                "요청을 처리하지 못했습니다. 활동 로그에서 상세 상태를 "
                "확인해 주세요."
            )


def _render_inquiry_detail(
    inquiry: dict[str, Any], database: Database | None = None
) -> None:
    detail_plan = (
        _processing_plan_for_inquiry(database, inquiry)
        if database is not None and inquiry.get("id") is not None
        else None
    )
    st.markdown(
        '<div class="official-section-title compact"><div><h3>문의 상세</h3>'
        "<span>선택한 문의 정보</span></div></div>",
        unsafe_allow_html=True,
    )
    time_text = format_datetime_kst(inquiry.get("registered_at"))
    raw = (
        inquiry.get("raw_json")
        if isinstance(inquiry.get("raw_json"), dict)
        else {}
    )
    order_snapshot = (
        raw.get("order_lookup")
        if isinstance(raw.get("order_lookup"), dict)
        else raw.get("order_snapshot")
        if isinstance(raw.get("order_snapshot"), dict)
        else {}
    )
    posted_answer = (
        NaverPostedAnswerRepository(database).current(int(inquiry["id"]))
        if database is not None and inquiry.get("id") is not None
        else None
    )
    legacy_posted_answer = (
        raw.get("existing_answer")
        if database is None
        else None
    )
    if isinstance(legacy_posted_answer, dict):
        legacy_posted_answer = (
            legacy_posted_answer.get("content")
            or legacy_posted_answer.get("answerContent")
        )
    st.markdown(
        '<div class="inquiry-detail-layout"><div class="official-fields two">'
        + _field("문의 ID", inquiry.get("source_question_id"))
        + _field("문의시간", time_text)
        + _field("문의유형", inquiry.get("inquiry_type"))
        + _field(
            "주문번호",
            detail_plan.order_id if detail_plan else inquiry.get("order_id"),
        )
        + _field("상품명", inquiry.get("product_name"))
        + _field("상품 옵션", inquiry.get("option_name"))
        + _field("고객정보", _masked_customer(inquiry.get("customer_display")))
        + _field("스토어", inquiry.get("store_code"))
        + _field(
            "주문일",
            inquiry.get("order_date") or order_snapshot.get("order_date"),
        )
        + _field(
            "주문상태",
            order_snapshot.get("order_status")
            or order_snapshot.get("status"),
        )
        + _field(
            "배송정보",
            order_snapshot.get("delivery_status")
            or order_snapshot.get("delivery_method"),
        )
        + "</div>"
        + '<div class="official-copy-block inquiry-content-scroll"><span>문의 내용</span><p>'
        + escape(display_value(inquiry.get("content")))
        + "</p></div></div>",
        unsafe_allow_html=True,
    )
    if posted_answer and posted_answer.get("fetch_status") == "AVAILABLE":
        with st.expander("네이버 실제 등록 답변", expanded=False):
            st.markdown(
                '<div class="existing-answer-scroll">'
                f"{escape(str(posted_answer.get('answer_body') or ''))}</div>",
                unsafe_allow_html=True,
            )
            st.caption("고객에게 실제 노출된 답변 · NAVER_POSTED")
    elif inquiry.get("source_answered"):
        st.caption(
            "네이버 답변완료 · 실제 답변 본문 NOT_FETCHED "
            "(Program Answer로 대체하지 않음)"
        )
    elif legacy_posted_answer:
        with st.expander("기존 네이버 답변", expanded=False):
            st.markdown(
                '<div class="existing-answer-scroll">'
                f"{escape(str(legacy_posted_answer))}</div>",
                unsafe_allow_html=True,
            )
    if database is not None and inquiry.get("id") is not None:
        analysis = detail_plan or _processing_plan_for_inquiry(database, inquiry)
        if not analysis.requires_order_lookup:
            st.info("이 문의의 주문 조회 단계는 해당 없음(SKIPPED)입니다.")
            return
        result_key = f"dashboard_order_result_{inquiry['id']}"
        order_columns = st.columns([1.15, 1.85], gap="small")
        lookup_requested = order_columns[0].button(
            "주문 재조회",
            key=f"workspace_order_lookup_{inquiry['id']}",
            type=(
                "primary"
                if analysis.order_lookup_action == "FETCH"
                else "secondary"
            ),
            width="stretch",
            disabled=analysis.order_id_status != "VALID",
        )
        result = st.session_state.get(result_key)
        order_columns[1].caption(
            "조회 완료"
            if analysis.order_lookup_status == "SUCCESS"
            or (isinstance(result, dict) and result.get("success"))
            else "주문번호 요청 필요"
            if analysis.order_id_status != "VALID"
            else "답변 생성 시 주문 조회를 함께 시도합니다."
        )
        if lookup_requested:
            with st.spinner("네이버 주문을 조회하고 있습니다..."):
                result = UatOrderService(database).lookup_for_inquiry(
                    int(inquiry["id"]), force_refresh=True
                )
            st.session_state[result_key] = result
            st.session_state[f"uat_order_result_{inquiry['id']}"] = result
            if result.get("success"):
                saved = result.get("selected_order") or {}
                st.session_state["selected_inquiry_id"] = int(inquiry["id"])
                st.session_state["selected_order_id"] = saved.get("order_id")
                st.session_state["selected_order_date"] = saved.get(
                    "order_date"
                )
                st.toast("주문 정보를 확인했습니다.")
            else:
                st.warning(
                    result.get("error_message")
                    or "네이버 주문 정보를 확인하지 못했습니다."
                )
            st.rerun()


def _inquiry_session_map(name: str) -> dict[int, Any]:
    value = st.session_state.get(name)
    if not isinstance(value, dict):
        value = {}
        st.session_state[name] = value
    return value


def _dps_status_label(
    latest: dict[str, Any] | None,
    *,
    in_progress: bool,
    has_order_id: bool,
    has_order_date: bool,
) -> str:
    if in_progress:
        return "조회 중"
    if not has_order_id or not has_order_date:
        return "주문 조회 필요"
    if latest is None:
        return "조회 전"
    status = str(latest.get("lookup_status") or "").upper()
    error_code = str(latest.get("error_code") or "").upper()
    if status == "SUCCESS":
        return "조회 성공"
    if status == "NOT_FOUND":
        return "조회 성공 / 결과 없음"
    if status == "AGENT_OFFLINE":
        return "Agent 연결 실패"
    if status == "TIMEOUT":
        return "timeout"
    if any(
        marker in error_code
        for marker in ("LOGIN", "OTP", "DPS_TAB", "CHROME")
    ):
        return "로그인 필요"
    return "오류"


def _dps_error_message(
    *,
    error: Exception | None = None,
    latest: dict[str, Any] | None = None,
) -> str:
    if isinstance(error, ValueError):
        return str(error)
    code = str((latest or {}).get("error_code") or "").upper()
    if code in {
        "AGENT_CONNECTION_FAILED",
        "AGENT_CONNECT_TIMEOUT",
        "AGENT_REQUEST_FAILED",
    }:
        return "DPS Agent에 연결할 수 없습니다."
    if "TIMEOUT" in code:
        return "DPS 조회 응답 시간이 초과되었습니다."
    if any(marker in code for marker in ("LOGIN", "OTP")):
        return "Chrome에서 DPS 로그인을 먼저 완료해 주세요."
    if any(marker in code for marker in ("CHROME", "DPS_TAB")):
        return "DPS가 열린 Chrome 창을 먼저 연결해 주세요."
    if code in {"NO_DPS_RESULT", "LOOKUP_RESULT_NOT_FOUND"}:
        return "주문번호에 해당하는 DPS 결과가 없습니다."
    if error is not None:
        return "DPS 조회를 완료하지 못했습니다. 활동 로그를 확인해 주세요."
    return str((latest or {}).get("error_message") or "")


def _render_dps(database: Database, inquiry: dict[str, Any]) -> None:
    inquiry_id = int(inquiry["id"])
    analysis = _processing_plan_for_inquiry(database, inquiry)
    lookup_not_required = not analysis.requires_dps_lookup
    if lookup_not_required:
        st.markdown(
            '<div class="official-section-title compact"><div><h3>DPS 정보</h3>'
            '<span>삼성 설치·배송 조회</span></div>'
            '<span class="official-state">해당 없음</span></div>',
            unsafe_allow_html=True,
        )
        st.info("이 문의의 DPS 조회 단계는 해당 없음(SKIPPED)입니다.")
    repository = DpsRepository(database)
    progress_by_inquiry = _inquiry_session_map("dps_lookup_in_progress")
    result_by_inquiry = _inquiry_session_map("dps_result")
    error_by_inquiry = _inquiry_session_map("dps_error")
    correlation_by_inquiry = _inquiry_session_map("dps_correlation_id")
    stage_by_inquiry = _inquiry_session_map("dps_last_success_stage")
    error_stage_by_inquiry = _inquiry_session_map("dps_last_error_stage")
    in_progress = bool(progress_by_inquiry.get(inquiry_id))
    order_id = analysis.order_id
    latest = repository.get_preferred_for_inquiry_and_order(
        inquiry_id, order_id
    )
    normalized = (
        latest.get("normalized_result_json")
        if latest and isinstance(latest.get("normalized_result_json"), dict)
        else {}
    )
    order_date = str(inquiry.get("order_date") or "").strip()
    product_order_id = analysis.product_order_id
    order_is_product = bool(order_id and order_id == product_order_id)
    status = _dps_status_label(
        latest,
        in_progress=in_progress,
        has_order_id=bool(order_id) and not order_is_product,
        has_order_date=bool(order_date),
    )
    cache_used = (
        bool(latest.get("cached"))
        if latest and "cached" in latest
        else normalized.get("cache_used")
        if normalized
        else None
    )
    elapsed = (
        latest.get("duration_seconds")
        if latest and latest.get("duration_seconds") is not None
        else normalized.get("elapsed_seconds")
        if normalized
        else None
    )
    st.markdown(
        '<div class="official-section-title compact"><div><h3>DPS 정보</h3>'
        "<span>삼성 설치·배송 조회</span></div>"
        f'<span class="official-state {_status_tone(status)}">{escape(status)}</span></div>',
        unsafe_allow_html=True,
    )
    if latest is None and status == "조회 전":
        st.info("아직 DPS 조회를 실행하지 않았습니다.")
    st.markdown(
        '<div class="official-fields two">'
        + _field("조회상태", status)
        + _field("판매번호", normalized.get("sales_number"))
        + _field("배송상태", normalized.get("delivery_status"))
        + _field("설치상태", normalized.get("installation_status"))
        + _field(
            "설치예정일",
            installation_date_value(normalized)
            or installation_date_display(
                normalized, queried=latest is not None
            ),
        )
        + _field("설치유형", normalized.get("installation_type"))
        + _field(
            "최근조회",
            format_datetime_kst(latest.get("queried_at"))
            if latest else None,
        )
        + _field("캐시여부", "사용" if cache_used else "미사용")
        + _field(
            "조회시간",
            format_datetime_kst(latest.get("created_at"))
            if latest else None,
        )
        + _field("조회소요시간", f"{elapsed}초" if elapsed is not None else None)
        + _field("주문일", order_date)
        + "</div>",
        unsafe_allow_html=True,
    )
    st.caption("DPS 품목상세내역의 요구납기일 기준")
    with st.expander("설치예정일 데이터 상세", expanded=False):
        st.json(
            {
                "원본 필드": "요구납기일",
                "원본 값": normalized.get("raw_required_delivery_date"),
                "정규화 값": normalized.get("required_delivery_date"),
                "파싱 상태": normalized.get("date_parse_status"),
                "데이터 출처": normalized.get(
                    "installation_date_source"
                ),
            }
        )
    if latest:
        ui_bound_key = (
            f"installation_date_ui_bound_{inquiry_id}_{latest['id']}"
        )
        if not st.session_state.get(ui_bound_key):
            LogRepository(database).record_inquiry(
                inquiry_id,
                "INSTALLATION_DATE_UI_BOUND",
                "문의별 최신 DPS 설치예정일을 Dashboard에 연결했습니다.",
                details={
                    "masked_order_id": _masked_order_id(order_id),
                    "dps_lookup_id": latest["id"],
                    "correlation_id": correlation_by_inquiry.get(
                        inquiry_id
                    ),
                    "status": normalized.get("date_parse_status"),
                    "normalized_date": installation_date_value(
                        normalized
                    ),
                },
            )
            st.session_state[ui_bound_key] = True
    session_error = error_by_inquiry.get(inquiry_id)
    if session_error:
        st.error(str(session_error))
    elif latest and latest.get("lookup_status") == "NOT_FOUND":
        st.info("조회는 완료됐지만 해당 주문의 DPS 결과가 없습니다.")
    elif latest and (
        latest.get("error_message") or normalized.get("warnings")
    ):
        st.warning(_dps_error_message(latest=latest))
    if _developer_mode():
        session_marker = st.session_state.setdefault(
            "runtime_session_marker", str(uuid.uuid4())
        )
        try:
            agent_status = get_dps_agent_status()
        except Exception as diagnostic_error:
            agent_status = {
                "agent_running": False,
                "last_error": diagnostic_error.__class__.__name__,
            }
        with st.expander("개발자용 Runtime 상태", expanded=False):
            st.write(
                runtime_snapshot(
                    session_marker=session_marker,
                    last_correlation_id=correlation_by_inquiry.get(
                        inquiry_id
                    ),
                    last_success_stage=stage_by_inquiry.get(inquiry_id),
                    last_error_stage=error_stage_by_inquiry.get(
                        inquiry_id
                    ),
                    agent_status=agent_status,
                )
            )
    can_lookup = bool(
        order_id and order_date and not order_is_product and not in_progress
    )
    refresh = st.button(
        "DPS 재조회",
        disabled=not can_lookup,
        width="stretch",
        key=f"official_dps_refresh_{inquiry_id}",
    )
    if not order_id or order_is_product:
        st.caption(
            "DPS 조회에는 일반 네이버 주문번호가 필요합니다. "
            "일반 주문번호가 없어 DPS 외부 호출을 실행하지 않습니다. "
            "답변 생성 시 주문번호 요청 답변이 만들어집니다."
        )
    elif not order_date:
        st.caption(
            "DPS 조회에는 실제 주문일이 필요합니다. "
            "먼저 주문 조회를 실행해 주세요."
        )
    if refresh:
        correlation_id = str(uuid.uuid4())
        current_stage = "button"
        st.session_state["selected_inquiry_id"] = inquiry_id
        st.session_state["selected_order_id"] = order_id
        st.session_state["selected_order_date"] = order_date
        progress_by_inquiry[inquiry_id] = True
        error_by_inquiry.pop(inquiry_id, None)
        error_stage_by_inquiry.pop(inquiry_id, None)
        correlation_by_inquiry[inquiry_id] = correlation_id
        _record_ui_event(
            database,
            inquiry_id,
            "DPS_LOOKUP_BUTTON_CLICKED",
            "DPS 조회 버튼 클릭을 확인했습니다.",
            details={
                "correlation_id": correlation_id,
                "masked_order_id": _masked_order_id(order_id),
                "stage": current_stage,
                "status": "CLICKED",
            },
        )
        try:
            with st.status("DPS 조회 준비 중", expanded=True) as dps_status:
                current_stage = "agent_connect"
                dps_status.write("DPS Agent 연결 중")
                dps_status.write("구매요청리스트 이동 중")
                dps_status.write("주문번호 및 기간 설정 중")
                current_stage = "lookup"
                dps_status.write("주문 조회 중")
                dps_status.write("DPS판매번호 상세 진입 중")
                dps_status.write("품목상세내역 확인 중")
                outcome = DpsLookupOrchestrator(database).lookup(
                    inquiry_id,
                    force_refresh=bool(refresh),
                    correlation_id=correlation_id,
                )
                current_stage = "database_save"
                dps_status.write("요구납기일 저장 확인 중")
                written = repository.get_latest_by_inquiry_id(inquiry_id)
                if written is None:
                    raise RuntimeError("DPS_RESULT_NOT_PERSISTED")
                if (
                    outcome.lookup_row is not None
                    and int(written["id"])
                    != int(outcome.lookup_row["id"])
                ):
                    _record_ui_event(
                        database,
                        inquiry_id,
                        "DPS_RESULT_RELOAD_MISMATCH",
                        "DPS 저장 결과와 Dashboard 재조회 결과가 다릅니다.",
                        level="WARNING",
                        details={
                            "correlation_id": correlation_id,
                            "stage": "ui_repository_reload",
                        },
                    )
                persisted = repository.get_preferred_for_inquiry_and_order(
                    inquiry_id, order_id
                )
                if persisted is None:
                    raise RuntimeError("DPS_PREFERRED_RESULT_NOT_FOUND")
                current_stage = "ui_refresh"
                dps_status.write("화면 갱신 중")
                result_by_inquiry[inquiry_id] = persisted
                stage_by_inquiry[inquiry_id] = "completed"
                _record_ui_event(
                    database,
                    inquiry_id,
                    "DPS_UI_REFRESHED",
                    "Dashboard DPS 카드를 DB 결과로 갱신했습니다.",
                    details={
                        "correlation_id": correlation_id,
                        "dps_lookup_id": persisted.get("id"),
                        "stage": "ui_refresh",
                        "status": persisted.get("lookup_status"),
                        "duration": persisted.get("duration_seconds"),
                    },
                )
                detail_lookup = (
                    persisted.get("normalized_result_json") or {}
                ).get("detail_lookup") or {}
                if (
                    detail_lookup.get("parsed")
                    and not detail_lookup.get("closed")
                ):
                    _record_ui_event(
                        database,
                        inquiry_id,
                        "DPS_WINDOW_RESTORE_WARNING",
                        "DPS 상세정보는 저장했지만 창 정리 상태를 확인해야 합니다.",
                        level="WARNING",
                        details={
                            "correlation_id": correlation_id,
                            "dps_lookup_id": persisted.get("id"),
                            "stage": "window_cleanup",
                            "status": detail_lookup.get("status"),
                        },
                    )
                dps_status.update(label="DPS 조회 완료", state="complete")
        except SystemExit as error:
            record_runtime_exception(
                "DPS_RUNTIME_EXCEPTION",
                error,
                inquiry_id=inquiry_id,
                correlation_id=correlation_id,
                stage=current_stage,
            )
            error_stage_by_inquiry[inquiry_id] = current_stage
            error_by_inquiry[inquiry_id] = (
                "조회 중 오류가 발생했습니다. Streamlit은 계속 사용할 수 "
                "있으며 DPS 조회를 다시 시도할 수 있습니다."
            )
            _record_ui_event(
                database,
                inquiry_id,
                "DPS_RUNTIME_EXCEPTION",
                "DPS 실행 경계에서 종료 요청을 안전하게 차단했습니다.",
                level="ERROR",
                details={
                    "correlation_id": correlation_id,
                    "stage": current_stage,
                    "error_category": "SystemExit",
                    "status": "RECOVERABLE",
                },
            )
        except Exception as error:
            record_runtime_exception(
                "DPS_RUNTIME_EXCEPTION",
                error,
                inquiry_id=inquiry_id,
                correlation_id=correlation_id,
                stage=current_stage,
            )
            error_stage_by_inquiry[inquiry_id] = current_stage
            try:
                reloaded = repository.get_preferred_for_inquiry_and_order(
                    inquiry_id, order_id
                )
            except Exception as reload_error:
                record_runtime_exception(
                    "DPS_RUNTIME_EXCEPTION",
                    reload_error,
                    inquiry_id=inquiry_id,
                    correlation_id=correlation_id,
                    stage="repository_recovery",
                )
                reloaded = None
            error_by_inquiry[inquiry_id] = _dps_error_message(
                error=error, latest=reloaded
            )
            _record_ui_event(
                database,
                inquiry_id,
                "DPS_RUNTIME_EXCEPTION",
                "DPS 조회 중 오류가 발생했으며 UI 세션을 유지했습니다.",
                level="ERROR",
                details={
                    "correlation_id": correlation_id,
                    "stage": current_stage,
                    "error_category": error.__class__.__name__,
                    "status": "RECOVERABLE",
                },
            )
        finally:
            progress_by_inquiry[inquiry_id] = False
            st.rerun()


@st.dialog("Activity Log", width="large")
def _activity_dialog(
    database_path: str, inquiry_id: int, stage_label: str
) -> None:
    database = Database(database_path)
    rows = LogRepository(database).recent_for_inquiry(inquiry_id, limit=100)
    st.caption(f"{stage_label} · 최신 활동 100건 · 민감정보 마스킹 적용")
    table: list[dict[str, Any]] = []
    for row in rows:
        details = (
            row.get("details_json")
            if isinstance(row.get("details_json"), dict)
            else {}
        )
        table.append(
            {
                "시간": format_datetime_kst(row.get("created_at")),
                "사용자": details.get("actor") or "시스템",
                "동작": details.get("action") or row.get("event_code"),
                "상태": details.get("status") or row.get("level"),
                "메시지": row.get("message"),
                "오류": details.get("error_code") or details.get("reason") or "",
            }
        )
    if table:
        st.dataframe(table, width="stretch", hide_index=True)
    else:
        st.info("기록된 활동이 없습니다.")


def _render_progress(database: Database, inquiry: dict[str, Any]) -> None:
    inquiry_id = int(inquiry["id"])
    plan = _processing_plan_for_inquiry(database, inquiry)
    steps = {
        row["step_code"]: row
        for row in WorkflowRepository(database).list_steps(inquiry_id)
    }
    approval = ApprovalRepository(database).get_inquiry_approval(inquiry_id)
    st.markdown(
        '<div class="official-section-title compact"><div><h3>진행 단계</h3>'
        "<span>단계를 클릭하면 Activity Log가 열립니다.</span></div></div>",
        unsafe_allow_html=True,
    )
    selected_stage: str | None = None
    for row_start in range(0, len(PROGRESS_STAGES), 2):
        columns = st.columns(2, gap="small")
        for column, (number, label, code) in zip(
            columns, PROGRESS_STAGES[row_start : row_start + 2]
        ):
            if code == "APPROVAL":
                status = approval["approval_status"]
            elif code == "NAVER_POST":
                post_status = str(approval.get("post_status") or "")
                status = {
                    "POSTED": "COMPLETED",
                    "POSTING": "RUNNING",
                    "POST_FAILED": "FAILED",
                    "POST_UNKNOWN": "NEEDS_REVIEW",
                }.get(post_status, "LOCKED")
            else:
                status = steps.get(code, {}).get("step_status", "PENDING")
                if code == "NAVER_ORDER_LOOKUP":
                    status = {
                        "CUSTOMER_INFORMATION_REQUIRED": "NEEDS_REVIEW",
                        "READY": "PENDING",
                    }.get(
                        plan.workflow_order_status,
                        plan.workflow_order_status,
                    )
                elif code == "DPS_LOOKUP":
                    status = {
                        "READY": "PENDING",
                    }.get(
                        plan.workflow_dps_status,
                        plan.workflow_dps_status,
                    )
            with column:
                st.markdown(
                    f'<div class="progress-dot {_status_tone(status)}">{escape(number)}</div>',
                    unsafe_allow_html=True,
                )
                if st.button(
                    label,
                    key=f"progress_{inquiry_id}_{code}",
                    width="stretch",
                ):
                    selected_stage = label
                label_status = {
                    "COMPLETED": "완료",
                    "APPROVED": "완료",
                    "RUNNING": "진행 중",
                    "FAILED": "오류",
                    "NEEDS_REVIEW": "오류",
                    "SKIPPED": "해당 없음",
                    "LOCKED": "잠금",
                }.get(str(status).upper(), "대기")
                if (
                    code == "NAVER_ORDER_LOOKUP"
                    and plan.workflow_order_status
                    == "CUSTOMER_INFORMATION_REQUIRED"
                ):
                    label_status = "고객정보 필요"
                st.caption(label_status)
    if selected_stage:
        _activity_dialog(str(database.path), inquiry_id, selected_stage)


def _render_auto_post_review(
    database: Database, inquiry: dict[str, Any], *, settings: NaverPostSettings,
) -> None:
    inquiry_id = int(inquiry["id"])
    repository = PostReviewRepository(database)
    review = repository.get(inquiry_id)
    if review is None:
        return
    versions = repository.versions(inquiry_id)
    initial = next(
        (item for item in versions if item["version_kind"] == "AUTO_POST_INITIAL"),
        versions[0] if versions else {},
    )
    latest_version = versions[-1] if versions else {}
    current = next(
        (
            item for item in reversed(versions)
            if str(item.get("naver_status") or "").upper() == "POSTED"
        ),
        latest_version,
    )
    st.markdown("### 자동등록 사후검토")
    metrics = st.columns(4, gap="small")
    for column, (label, value) in zip(
        metrics,
        (
            ("검토 상태", review.get("status")),
            ("최초 Route", review.get("route")),
            ("현재 버전", latest_version.get("version_number") or "-"),
            ("Learning", "저장됨" if latest_version.get("learning_saved") else "미저장"),
        ),
    ):
        column.metric(label, value or "-")
    answer_columns = st.columns(2, gap="medium")
    answer_columns[0].caption("최초 자동등록 답변")
    answer_columns[0].code(str(initial.get("answer_body") or ""), language=None)
    answer_columns[1].caption("현재 네이버 반영 답변")
    answer_columns[1].code(str(current.get("answer_body") or ""), language=None)

    edit_key = f"auto_post_correction_text_{inquiry_id}"
    correction = st.text_area(
        "직원 수정 입력",
        value=str(current.get("answer_body") or ""),
        key=edit_key,
        height=220,
    )
    actions = st.columns([1.5, 1.5, 4], gap="small")
    apply_correction = actions[0].button(
        "네이버 수정 반영",
        key=f"auto_post_correction_apply_{inquiry_id}",
        type="primary",
        disabled=(
            not settings.enabled
            or not can(Permission.APPROVE)
            or str(review.get("status")) == "POST_UNKNOWN"
        ),
        width="stretch",
    )
    no_change = actions[1].button(
        "수정 없음 검토 완료",
        key=f"auto_post_no_change_{inquiry_id}",
        disabled=(
            not can(Permission.APPROVE)
            or str(review.get("status")) in {
                "CORRECTION_PENDING", "POST_UNKNOWN", "CORRECTED_AND_REPOSTED",
            }
        ),
        width="stretch",
    )
    if not settings.enabled:
        actions[2].caption(
            "NAVER_POST_ENABLED=false이므로 네이버 수정 API도 잠겨 있습니다."
        )
    if apply_correction:
        result = NaverPostService(database).correct(
            inquiry_id,
            edited_answer=correction,
            actor=current_actor(),
        )
        st.session_state[f"auto_post_correction_result_{inquiry_id}"] = result.to_dict()
        st.rerun()
    if no_change:
        PostReviewService(database).complete_without_change(
            inquiry_id=inquiry_id, actor=current_actor()
        )
        st.session_state[f"auto_post_reviewed_{inquiry_id}"] = True
        st.rerun()
    result = st.session_state.get(f"auto_post_correction_result_{inquiry_id}")
    if isinstance(result, dict):
        if result.get("status") == "CORRECTED_AND_REPOSTED":
            st.success("직원 수정본을 네이버에 반영하고 Learning에 저장했습니다.")
        elif result.get("status") == "POST_UNKNOWN":
            st.error("수정 도달 여부가 불명확합니다. 자동 재시도하지 않습니다.")
        else:
            st.warning(
                "네이버 수정 실패 · 기존 자동등록 답변은 유지됩니다. · "
                f"{result.get('error_code') or result.get('status')}"
            )
    if versions:
        with st.expander("답변 버전 및 수정 이력", expanded=False):
            st.dataframe(
                [
                    {
                        "Version": item["version_number"],
                        "종류": item["version_kind"],
                        "작성 주체": item["actor"],
                        "Route": item.get("route"),
                        "네이버 상태": item["naver_status"],
                        "이전 버전": item.get("previous_version_id"),
                        "답변 hash": item["answer_hash"],
                        "등록 시각": format_datetime_kst(item.get("posted_at"), empty="-"),
                        "수정 시각": format_datetime_kst(item.get("modified_at"), empty="-"),
                    }
                    for item in versions
                ],
                hide_index=True,
                width="stretch",
            )
    event_codes = {
        "AUTO_POST_STARTED", "AUTO_POST_SUCCEEDED", "AUTO_POST_FAILED",
        "AUTO_POST_UNKNOWN", "POST_REVIEW_CREATED",
        "POST_CORRECTION_REQUESTED", "POST_CORRECTION_STARTED",
        "POST_CORRECTION_SUCCEEDED", "POST_CORRECTION_FAILED",
        "AUTO_POST_CORRECTION_LEARNED",
    }
    events = [
        item for item in LogRepository(database).recent_for_inquiry(inquiry_id, limit=100)
        if item.get("event_code") in event_codes
    ]
    if events:
        with st.expander("자동등록·수정 Activity Log", expanded=False):
            st.dataframe(
                [
                    {
                        "시각": format_datetime_kst(item.get("created_at")),
                        "이벤트": item.get("event_code"),
                        "내용": item.get("message"),
                    }
                    for item in events
                ],
                hide_index=True,
                width="stretch",
            )


def _render_naver_post_prepare(
    database: Database, inquiry: dict[str, Any]
) -> None:
    inquiry_id = int(inquiry["id"])
    result_key = f"naver_post_dry_run_result_{inquiry_id}"
    post_result_key = f"naver_post_result_{inquiry_id}"
    confirm_key = f"naver_post_confirm_{inquiry_id}"
    settings = NaverPostSettings.from_environment()
    _render_auto_post_review(database, inquiry, settings=settings)
    st.markdown("### 네이버 등록 준비")
    approval = ApprovalRepository(database).get_inquiry_approval(inquiry_id)
    draft = (
        AnswerRepository(database).active_for_inquiry(inquiry_id)
        or AnswerRepository(database).latest_for_inquiry(inquiry_id)
        or {}
    )
    latest_attempt = NaverPostRepository(database).latest(inquiry_id)
    post_status = str(approval.get("post_status") or "NOT_POSTED").upper()
    fields = st.columns(6, gap="small")
    values = (
        ("문의번호", inquiry.get("external_inquiry_id") or inquiry.get("source_question_id")),
        ("문의 유형", inquiry.get("source_type")),
        ("스토어", inquiry.get("store_code")),
        ("Final Answer 길이", len(str(draft.get("final_answer") or ""))),
        ("승인 여부", approval.get("approval_status")),
        ("등록 상태", post_status),
    )
    for column, (label, value) in zip(fields, values):
        column.metric(label, "-" if value in (None, "") else value)

    dry_result = st.session_state.get(result_key)
    already_posted = post_status in {
        "POSTED",
        "POSTING",
        "POST_UNKNOWN",
    }
    actions = st.columns([1.2, 4.6], gap="small")
    actual = actions[0].button(
        "네이버 실제 등록",
        key=f"naver_post_actual_{inquiry_id}",
        disabled=(
            not settings.enabled
            or already_posted
            or not can(Permission.APPROVE)
        ),
        width="stretch",
    )
    if settings.enabled:
        actions[1].caption(
            "수동 등록 모드 · 내부 preflight 통과 후 명시적 확인이 필요합니다."
        )
    else:
        actions[1].caption(
            "NAVER_POST_ENABLED=false · 네이버 실제 등록 기능이 잠겨 있습니다."
        )
    if actual:
        preflight = NaverPostDryRunService(database).run(inquiry_id).to_dict()
        st.session_state[result_key] = preflight
        if preflight.get("eligible"):
            st.session_state[confirm_key] = True
        else:
            st.session_state.pop(confirm_key, None)
        st.rerun()

    result = st.session_state.get(result_key)
    if not isinstance(result, dict):
        st.info("실제 등록을 누르면 내부 preflight 후 등록 가능 여부를 확인합니다.")
    else:
        if result.get("eligible"):
            st.success("등록 가능 · Validation PASS")
        else:
            st.warning(
                "등록 불가 · "
                + ", ".join(
                    str(reason) for reason in result.get("reasons") or []
                )
            )
        st.json(
            {
                "method": result.get("method"),
                "endpoint": result.get("endpoint"),
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer <NAVER_ACCESS_TOKEN>",
                },
                "payload": result.get("payload"),
                "postLocked": result.get("post_locked"),
                "networkCallCount": 0,
            },
            expanded=True,
        )

    if st.session_state.get(confirm_key):
        st.warning("승인된 Final Answer를 네이버에 실제 등록합니다.")
        confirm_columns = st.columns(2, gap="small")
        cancel = confirm_columns[0].button(
            "취소",
            key=f"naver_post_cancel_{inquiry_id}",
            width="stretch",
        )
        confirm = confirm_columns[1].button(
            "실제 등록 확인",
            key=f"naver_post_confirm_action_{inquiry_id}",
            type="primary",
            width="stretch",
        )
        if cancel:
            st.session_state.pop(confirm_key, None)
            st.rerun()
        if confirm:
            outcome = NaverPostService(database).post(
                inquiry_id,
                actor=current_actor(),
                confirmed=True,
                retry_requested=post_status == "POST_FAILED",
            )
            st.session_state[post_result_key] = outcome.to_dict()
            st.session_state.pop(confirm_key, None)
            st.session_state.pop(result_key, None)
            st.rerun()

    post_result = st.session_state.get(post_result_key)
    if isinstance(post_result, dict):
        if post_result.get("status") == "POSTED":
            st.success("네이버 등록 완료")
        elif post_result.get("status") == "POST_UNKNOWN":
            st.error(
                "등록 결과를 확정할 수 없습니다. 자동 재시도하지 말고 "
                "네이버 상태를 확인해 주세요."
            )
        elif post_result.get("status") not in {"BLOCKED", None}:
            st.error(
                "네이버 등록 실패 · "
                f"{post_result.get('error_code') or 'UNKNOWN'}"
            )
        elif post_result.get("status") == "BLOCKED":
            st.warning(str(post_result.get("message") or "등록 차단"))
    if latest_attempt:
        st.caption(
            "최근 등록 시도 "
            f"{format_datetime_kst(latest_attempt.get('started_at'))} · "
            f"결과 {latest_attempt.get('status')}"
        )


def render_review_workspace(
    items: list[WorkItem],
    total_count: int,
    database: Database | None,
    *,
    page_size: int = 15,
    current_page: int | None = None,
    total_pages: int | None = None,
) -> None:
    if database is None:
        st.warning("DB 연결을 사용할 수 없어 검토·승인 화면을 표시할 수 없습니다.")
        return
    if current_page is None or total_pages is None:
        page_items, resolved_page, resolved_total_pages = paginate_items(
            items,
            int(st.session_state.get("dashboard_page", 1)),
            page_size,
        )
    else:
        page_items = items
        resolved_page = max(1, int(current_page))
        resolved_total_pages = max(1, int(total_pages))
    st.session_state["dashboard_page"] = resolved_page
    st.markdown(
        '<div class="dashboard-main-grid-marker desktop-operations-layout"></div>',
        unsafe_allow_html=True,
    )
    list_column, detail_column = st.columns([2.05, 0.95], gap="medium")
    with list_column:
        with st.container(
            border=True, height=680, key="official_inquiry_list_panel"
        ):
            first = (resolved_page - 1) * page_size + 1 if total_count else 0
            last = min(resolved_page * page_size, total_count)
            st.caption(
                f"검색 결과 {total_count:,}건 · 현재 {first:,}–{last:,}건 표시 · "
                f"{resolved_page} / {resolved_total_pages} 페이지"
            )
            _render_list_header(total_count)
            with st.container(
                height=500, key="official_inquiry_rows_scroll"
            ):
                selected = _render_list(page_items, total_count)
            _render_pagination(resolved_page, resolved_total_pages)
    if selected is None:
        with detail_column:
            with st.container(border=True, height=680, key="official_detail_empty_panel"):
                st.markdown("### 문의 상세")
                st.info("문의 목록에서 항목을 선택하면 상세 정보가 표시됩니다.")
        return
    inquiry = _database_inquiry(database, selected)
    if inquiry is None:
        with detail_column:
            st.warning("선택한 문의가 아직 DB에 동기화되지 않았습니다.")
        return
    _ensure_initial_program_answer(database, inquiry)
    st.session_state["selected_inquiry_id"] = int(inquiry["id"])
    st.session_state["selected_order_id"] = inquiry.get("order_id")
    st.session_state["selected_order_date"] = inquiry.get("order_date")

    with detail_column:
        with st.container(
            border=True, height=680, key="official_detail_panel"
        ):
            _render_inquiry_detail(inquiry, database)

    review_column, dps_column = st.columns([2.05, 0.95], gap="medium")
    with review_column:
        with st.container(
            border=True, height=760, key="official_answer_panel"
        ):
            _render_answer_panel(database, inquiry)
        with st.container(border=True, key="official_naver_post_prepare_panel"):
            _render_naver_post_prepare(database, inquiry)
    with dps_column:
        with st.container(
            border=True, height=760, key="official_dps_panel"
        ):
            _render_dps(database, inquiry)
