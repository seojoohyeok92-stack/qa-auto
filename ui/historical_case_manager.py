from __future__ import annotations

from datetime import UTC, date, datetime, time

import streamlit as st

from answer.learning_feedback import (
    CORRECTION_REASON_BY_LABEL,
    CORRECTION_REASON_LABELS,
    INTENT_OPTIONS,
    CorrectionReason,
)
from config import get_configured_stores
from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.learning_feedback_repository import LearningFeedbackRepository
from services.historical_case_service import HistoricalCaseService
from services.learning_feedback_service import LearningFeedbackService
from ui.session_identity import current_identity


HISTORICAL_FILTER_KEYS = (
    "historical_manage_store",
    "historical_manage_type",
    "historical_min_quality",
    "historical_active_filter",
    "historical_risk_filter",
    "historical_search",
    "historical_manage_start",
    "historical_manage_end",
)


def _keep_historical_page(case_id: int | None = None) -> None:
    """Keep navigation and the selected detail stable across Streamlit reruns."""

    st.session_state["production_admin_mode"] = True
    st.session_state["current_page"] = "historical"
    st.session_state["historical_active_section"] = "case_manager"
    saved_filters = st.session_state.setdefault("historical_filter_state", {})
    for key in HISTORICAL_FILTER_KEYS:
        if key in st.session_state:
            saved_filters[key] = st.session_state[key]
        elif key in saved_filters:
            st.session_state[key] = saved_filters[key]
    if case_id is not None:
        st.session_state["historical_selected_case_id"] = int(case_id)


def _select_historical_case(case_id: int) -> None:
    st.session_state["historical_case_detail_widget"] = int(case_id)
    _keep_historical_page(int(case_id))


def _action_notice(
    *, status: str, message: str, case_id: int, details: str = "",
) -> None:
    st.session_state["historical_action_notice"] = {
        "status": str(status),
        "message": str(message),
        "case_id": int(case_id),
        "details": str(details),
    }
    _keep_historical_page(int(case_id))


def _render_action_notice() -> None:
    notice = st.session_state.get("historical_action_notice")
    if not isinstance(notice, dict):
        return
    text = str(notice.get("message") or "")
    details = str(notice.get("details") or "").strip()
    if details:
        text = f"{text}\n\n{details}"
    if notice.get("status") == "success":
        st.success(text)
    elif notice.get("status") == "info":
        st.info(text)
    else:
        st.error(text)


def _run_metrics(run: dict) -> None:
    columns = st.columns(7, gap="small")
    values = (
        ("조회", "total_fetched"), ("신규 저장", "inserted_count"),
        ("갱신", "updated_count"), ("중복", "duplicate_count"),
        ("답변 없음", "no_answer_count"), ("건너뜀", "skipped_count"),
        ("실패", "failed_count"),
    )
    for column, (label, key) in zip(columns, values):
        column.metric(label, int(run.get(key) or 0))


def render_historical_case_manager(database: Database) -> None:
    _keep_historical_page(
        st.session_state.get("historical_selected_case_id")
    )
    service = HistoricalCaseService(database)
    repository = HistoricalCaseRepository(database)
    stores = get_configured_stores()
    st.title("과거 네이버 상담 사례")
    st.caption(
        "과거 사례는 기본적으로 학습 참고에 사용되며, 안전기준을 통과한 사례만 Context에 포함됩니다. "
        "학습 제외는 원본을 삭제하지 않습니다. 이 화면은 Event Queue와 Auto Post를 호출하지 않습니다."
    )
    _render_action_notice()

    latest = repository.recent_runs(limit=1)
    summary = repository.summary()
    runtime_audit = service.audit_corpus()
    top = st.columns(6, gap="small")
    top[0].metric("전체 사례", summary["total"])
    top[1].metric("Context 사용 가능", runtime_audit["runtime_context_usable"])
    top[2].metric("검증 Learning", summary["learning_enabled"])
    top[3].metric("학습 제외", summary["learning_excluded"])
    top[4].metric("기존 승격 보존", summary["promoted"])
    top[5].metric("최근 Import", (latest[0].get("status") if latest else "없음"))
    st.caption(
        "안전기준 Context 제외 {safety}건 · 품질 분포 · 높음(0.70 이상) {high} · "
        "중간(0.50~0.69) {medium} · 낮음(0.50 미만) {low}".format(
            safety=summary["safety_excluded"], high=summary["quality_high"],
            medium=summary["quality_medium"], low=summary["quality_low"]
        )
    )

    with st.expander("과거 문의 가져오기", expanded=True):
        st.info(
            f"현재 Historical {summary['total']:,}건은 로컬 DB 백필 결과입니다. "
            "네이버 전체 과거 문의 Import 결과가 아닙니다. 상품문의·고객문의, "
            "90일 단위 기간 분할, Pagination, Preview와 중복 방지를 지원하며 "
            "실제 대량 Import는 아래 실행 버튼을 눌렀을 때만 시작됩니다."
        )
        filter_cols = st.columns([1.3, 1.3, 1.4], gap="small")
        start = filter_cols[0].date_input(
            "시작일", value=date.today().replace(month=1, day=1),
            key="historical_import_start",
        )
        end = filter_cols[1].date_input(
            "종료일", value=date.today(), key="historical_import_end",
        )
        store_options = ["ALL", *[store.code for store in stores]]
        selected_store = filter_cols[2].selectbox(
            "Store", store_options, key="historical_import_store",
        )
        option_cols = st.columns(3, gap="small")
        inquiry_types = option_cols[0].multiselect(
            "문의 유형",
            ["PRODUCT_INQUIRY", "CUSTOMER_INQUIRY"],
            default=["PRODUCT_INQUIRY", "CUSTOMER_INQUIRY"],
            key="historical_import_types",
        )
        answered_only = option_cols[1].checkbox(
            "답변 완료 문의만", value=True, key="historical_answered_only",
        )
        require_answer = option_cols[2].checkbox(
            "실제 판매자 답변이 있는 문의만", value=True,
            key="historical_require_answer",
        )
        action_cols = st.columns([1.4, 1.5, 1.8, 4.0], gap="small")
        if action_cols[0].button(
            "로컬 DB에서 가져오기", type="primary", width="stretch",
            key="historical_import_local",
        ):
            with st.spinner("로컬 과거 문의를 배치 단위로 가져오는 중입니다..."):
                result = service.import_local(
                    store_code=None if selected_store == "ALL" else selected_store,
                    inquiry_types=inquiry_types,
                    date_from=datetime.combine(start, time.min, UTC).isoformat(),
                    date_to=datetime.combine(end, time.max, UTC).isoformat(),
                    answered_only=answered_only,
                    require_seller_answer=require_answer,
                )
            st.session_state["historical_last_import"] = result
            _keep_historical_page()
            st.rerun()
        if action_cols[1].button(
            "Import Preview", width="stretch", key="historical_import_preview",
        ):
            with st.spinner("네이버 과거 문의를 읽기 전용으로 조회 중입니다..."):
                preview = service.preview_naver(
                    store_id=None if selected_store == "ALL" else selected_store,
                    inquiry_types=inquiry_types,
                    from_datetime=datetime.combine(start, time.min, UTC),
                    to_datetime=datetime.combine(end, time.max, UTC),
                    answered_only=answered_only,
                    require_seller_answer=require_answer,
                )
            st.session_state["historical_import_preview_result"] = preview
            _keep_historical_page()
            st.rerun()
        if action_cols[2].button(
            "네이버 API에서 가져오기", width="stretch",
            key="historical_import_naver",
        ):
            with st.spinner("네이버 과거 문의를 페이지 단위로 가져오는 중입니다..."):
                result = service.import_naver(
                    store_id=None if selected_store == "ALL" else selected_store,
                    inquiry_types=inquiry_types,
                    from_datetime=datetime.combine(start, time.min, UTC),
                    to_datetime=datetime.combine(end, time.max, UTC),
                    answered_only=answered_only,
                    require_seller_answer=require_answer,
                )
            st.session_state["historical_last_import"] = result
            _keep_historical_page()
            st.rerun()
        action_cols[3].caption(
            "대량 Import는 페이지/배치 단위로 처리되며 진행 상태가 DB에 저장됩니다."
        )
        preview_result = st.session_state.get("historical_import_preview_result")
        if isinstance(preview_result, dict):
            st.info(
                "조회된 페이지 기준 · 총 {total:,}건 · 답변 있음 {answers:,}건 · "
                "답변 없음 {missing:,}건 · 기존 {existing:,}건 · 예상 신규 {new:,}건".format(
                    total=int(preview_result.get("total_fetched", 0)),
                    answers=int(preview_result.get("answer_count", 0)),
                    missing=int(preview_result.get("no_answer_count", 0)),
                    existing=int(preview_result.get("existing_count", 0)),
                    new=int(preview_result.get("expected_new_count", 0)),
                )
            )
        last_result = st.session_state.get("historical_last_import") or (latest[0] if latest else None)
        if isinstance(last_result, dict):
            _run_metrics(last_result)

    st.subheader("Historical Case 관리")
    filters = st.columns([1.1, 1.2, 1.2, 1.1, 1.2, 2.2], gap="small")
    manage_store = filters[0].selectbox(
        "Store", ["ALL", *sorted({row.get("store_code") for row in repository.list_cases(limit=1000) if row.get("store_code")})],
        key="historical_manage_store",
    )
    manage_type = filters[1].selectbox(
        "유형", ["ALL", "PRODUCT_INQUIRY", "CUSTOMER_INQUIRY"],
        key="historical_manage_type",
    )
    quality = filters[2].slider(
        "최소 품질", 0.0, 1.0, 0.0, 0.05, key="historical_min_quality",
    )
    active_label = filters[3].selectbox(
        "학습 상태", ["전체", "사용 중", "제외됨"], key="historical_active_filter",
    )
    risk_label = filters[4].selectbox(
        "정책 위험", ["ALL", "NONE", "LOW", "MEDIUM", "HIGH", "BLOCKED"],
        key="historical_risk_filter",
    )
    search = filters[5].text_input(
        "검색", placeholder="문의·답변·상품·문의번호", key="historical_search",
    )
    period = st.columns(2, gap="small")
    manage_start = period[0].date_input(
        "사례 시작일", value=date.today().replace(year=max(2000, date.today().year - 5), month=1, day=1),
        key="historical_manage_start",
    )
    manage_end = period[1].date_input(
        "사례 종료일", value=date.today(), key="historical_manage_end",
    )
    active = None if active_label == "전체" else active_label == "사용 중"
    cases = repository.list_cases(
        store_code=None if manage_store == "ALL" else manage_store,
        inquiry_type=None if manage_type == "ALL" else manage_type,
        search=search, active=active, min_quality=quality, limit=300,
        policy_risk=None if risk_label == "ALL" else risk_label,
        date_from=datetime.combine(manage_start, time.min, UTC).isoformat(),
        date_to=datetime.combine(manage_end, time.max, UTC).isoformat(),
    )
    st.caption("정렬: 실제 문의 접수시간 최신순 · 동률 ID 내림차순")
    if not cases:
        st.info("조건에 맞는 과거 사례가 없습니다.")
        return
    st.dataframe(
        [
            {
                "ID": row["id"], "Store": row["store_code"],
                "유형": row["inquiry_type"], "문의": str(row["question"])[:100],
                "답변 있음": bool(row.get("seller_answer")),
                "품질": row["quality_score"], "정책 위험": row["policy_risk"],
                "Context 사용 가능": service.quality_policy.assess(
                    question=str(row.get("question") or ""),
                    answer=str(row.get("seller_answer") or ""),
                    stored_quality=float(row.get("quality_score") or 0),
                    policy_risk=str(row.get("policy_risk") or "NONE"),
                    active=bool(row.get("active")),
                ).context_eligible,
                "Runtime 판정": service.quality_policy.assess(
                    question=str(row.get("question") or ""),
                    answer=str(row.get("seller_answer") or ""),
                    stored_quality=float(row.get("quality_score") or 0),
                    policy_risk=str(row.get("policy_risk") or "NONE"),
                    active=bool(row.get("active")),
                ).status,
                "학습 상태": "검증 Learning" if row["active"] else "제외됨",
                "검증 Learning": bool(row.get("active")),
                "작성일": row.get("inquiry_created_at"),
            }
            for row in cases
        ],
        width="stretch", hide_index=True,
    )
    case_ids = [int(row["id"]) for row in cases]
    remembered_id = st.session_state.get("historical_selected_case_id")
    widget_key = "historical_case_detail_widget"
    widget_value = st.session_state.get(widget_key)
    if widget_value not in case_ids:
        restored = int(remembered_id) if remembered_id in case_ids else case_ids[0]
        st.session_state[widget_key] = restored
    selected_id = st.selectbox(
        "상세 사례", case_ids,
        format_func=lambda value: f"#{value} · {next(str(row['question'])[:70] for row in cases if int(row['id']) == value)}",
        key=widget_key,
    )
    st.session_state["historical_selected_case_id"] = int(selected_id)
    case = repository.get(int(selected_id)) or {}
    with st.expander("사례 상세", expanded=True):
        st.markdown("#### 문의 원문")
        st.write(case.get("question") or "-")
        st.markdown("#### 과거 직원 답변")
        st.write(case.get("seller_answer") or "답변 없음")
        st.caption(
            f"상품: {case.get('product_name') or '-'} · 작성일: {case.get('inquiry_created_at') or '-'} · "
            f"분류: {case.get('classification') or '-'} · 품질: {case.get('quality_score') or 0} · "
            f"현재 정책 위험: {case.get('policy_risk') or 'NONE'}"
        )
        if case.get("active"):
            st.success("학습 상태: 검증 Learning")
        else:
            case_metadata = case.get("metadata_json") or {}
            reason = case_metadata.get("learning_exclusion_reason")
            signal = case_metadata.get("learning_signal_type") or "EXCLUDED"
            st.warning(
                f"학습 상태: {signal}"
                + (f" · 사유: {reason}" if reason else "")
            )
        if case.get("promoted_learning_id"):
            st.info(
                "기존 수동 승격 기록을 중복 생성 없이 보존합니다.\n\n"
                f"기존 Learning #{case['promoted_learning_id']}"
            )
        elif case.get("active"):
            st.info(
                "과거 직원답변은 기본 검증 Learning입니다. "
                "현재 사실·Rule·주문·DPS와 안전필터가 항상 우선합니다."
            )
        else:
            st.info("원본 사례는 보존되며 Similar Search와 Prompt Context에서 제외됩니다.")
        with st.expander("잘못된 사례 Learning", expanded=False):
            st.caption(
                "잘못된 답변은 Positive 예제로 사용하지 않고 Negative 또는 "
                "Intent Correction 신호로만 저장합니다."
            )
            existing_feedback = LearningFeedbackRepository(
                database
            ).for_historical_case(int(selected_id))
            if existing_feedback:
                st.dataframe(
                    [
                        {
                            "신호": item.get("learning_signal_type"),
                            "사유": item.get("correction_reason"),
                            "올바른 유형": item.get("corrected_intent") or "-",
                            "상세 메모": item.get("correction_note") or "-",
                            "저장일": item.get("created_at"),
                        }
                        for item in existing_feedback
                    ],
                    hide_index=True,
                    width="stretch",
                )
            feedback_mode = st.radio(
                "학습 신호",
                ["잘못된 답변", "문의 유형/라우팅 오류"],
                horizontal=True,
                key="historical_feedback_mode",
            )
            if feedback_mode == "문의 유형/라우팅 오류":
                feedback_reason = CorrectionReason.ROUTING_ERROR
                corrected_label = st.selectbox(
                    "올바른 문의 유형",
                    list(INTENT_OPTIONS.values()),
                    key="historical_corrected_intent",
                )
                corrected_intent = next(
                    code
                    for code, label in INTENT_OPTIONS.items()
                    if label == corrected_label
                )
            else:
                reason_label = st.selectbox(
                    "잘못된 이유",
                    [
                        CORRECTION_REASON_LABELS[reason]
                        for reason in CorrectionReason
                        if reason is not CorrectionReason.ROUTING_ERROR
                    ],
                    key="historical_correction_reason",
                )
                feedback_reason = CORRECTION_REASON_BY_LABEL[reason_label]
                corrected_intent = ""
            feedback_note = st.text_input(
                "상세 메모 (선택)", key="historical_correction_note"
            )
            if st.button(
                "잘못된 사례로 학습",
                key="historical_save_negative_learning",
                type="primary",
            ):
                try:
                    identity = current_identity()
                    saved = LearningFeedbackService(
                        database
                    ).capture_historical_review(
                        case_id=int(selected_id),
                        correction_reason=feedback_reason,
                        correction_note=feedback_note,
                        corrected_intent=corrected_intent,
                        actor=str(identity.get("username") or "관리자"),
                    )
                    _action_notice(
                        status="success",
                        message=f"✅ 구조화된 교정 신호 {len(saved)}건을 저장했습니다.",
                        case_id=int(selected_id),
                    )
                except Exception as error:
                    _action_notice(
                        status="error",
                        message="❌ 교정 Learning 저장 실패",
                        details=f"원인: {str(error)[:300]}",
                        case_id=int(selected_id),
                    )
                st.rerun()
        exclusion_reason = ""
        if case.get("active"):
            reason_columns = st.columns([1.5, 3.5], gap="small")
            reason_choice = reason_columns[0].selectbox(
                "학습 제외 사유",
                [CORRECTION_REASON_LABELS[reason] for reason in CorrectionReason],
                key="historical_exclusion_reason",
            )
            reason_note = reason_columns[1].text_input(
                "사유 메모 (선택)", key="historical_exclusion_note",
            )
            exclusion_reason = (
                f"{reason_choice}: {reason_note.strip()}"
                if reason_note.strip() else reason_choice
            )
        buttons = st.columns([1.3, 1.5, 1.1, 4.1], gap="small")
        if buttons[0].button(
            "학습 제외" if case.get("active") else "학습 다시 사용",
            key="historical_toggle_active",
        ):
            target_active = not bool(case.get("active"))
            try:
                identity = current_identity()
                actor = str(identity.get("username") or "관리자")
                if target_active:
                    repository.set_learning_enabled(
                        int(selected_id), True, actor=actor
                    )
                    LearningFeedbackRepository(
                        database
                    ).deactivate_for_historical_case(int(selected_id))
                else:
                    LearningFeedbackService(
                        database
                    ).capture_historical_review(
                        case_id=int(selected_id),
                        correction_reason=reason_choice,
                        correction_note=reason_note,
                        actor=actor,
                        excluded=True,
                    )
                refreshed = repository.get(int(selected_id)) or {}
                if bool(refreshed.get("active")) is not target_active:
                    raise RuntimeError("Historical Case 상태가 DB에 반영되지 않았습니다.")
                _action_notice(
                    status="success",
                    message=(
                        "✅ 학습 참고를 다시 사용합니다."
                        if target_active
                        else "✅ 학습 참고에서 제외했습니다. 원본 사례는 보존됩니다."
                    ),
                    case_id=int(selected_id),
                )
            except Exception as error:
                _action_notice(
                    status="error",
                    message="❌ 학습 상태 변경 실패",
                    details=f"원인: {str(error)[:300]}",
                    case_id=int(selected_id),
                )
            st.rerun()
        buttons[1].button(
            "기본 검증 Learning" if case.get("active") else "학습 제외됨",
            disabled=True,
            key="historical_promote",
        )
        current_index = case_ids.index(int(selected_id))
        next_id = (
            case_ids[current_index + 1]
            if current_index + 1 < len(case_ids)
            else None
        )
        buttons[2].button(
            "다음 사례",
            disabled=next_id is None,
            key="historical_next_case",
            on_click=(
                _select_historical_case if next_id is not None else None
            ),
            args=((int(next_id),) if next_id is not None else ()),
        )
        buttons[3].caption(
            "활성 사례는 기본 검증 Learning이며 품질·정책 위험·개인정보 안전필터는 Context 적용 시 유지됩니다."
        )
