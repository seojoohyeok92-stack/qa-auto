from __future__ import annotations

from datetime import date
from typing import Any

import streamlit as st

from core.time_utils import format_datetime_kst, format_datetime_minute_kst, to_kst
from repositories.database import Database
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository
from services.learning_validity_service import validity_summary


INQUIRY_TYPE_LABELS = {
    "PRODUCT_INQUIRY": "상품문의",
    "CUSTOMER_INQUIRY": "고객문의",
    "GENERAL_INQUIRY": "일반문의",
    "PRODUCT_GENERAL": "상품 일반문의",
    "DELIVERY": "배송문의",
    "DELIVERY_INQUIRY": "배송문의",
    "INSTALLATION": "설치문의",
    "INSTALLATION_INQUIRY": "설치문의",
    "RETURN": "반품문의",
    "EXCHANGE": "교환문의",
    "CANCEL": "취소문의",
}

SIGNAL_FILTER_LABELS = {
    "ALL": "전체",
    "NEGATIVE": "Negative",
    "INTENT_CORRECTION": "의도 교정",
    "EXCLUDED": "학습 제외",
}

DEFAULT_COLUMNS = (
    "문의일시",
    "학습상태",
    "네이버 문의번호",
    "문의유형",
    "유효성",
    "질문",
    "학습답변",
)


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata_json")
    return value if isinstance(value, dict) else {}


def _search_blob(row: dict[str, Any]) -> str:
    metadata = _metadata(row)
    return " ".join(
        str(value or "")
        for value in (
            row.get("id"),
            row.get("source_key"),
            row.get("inquiry_id"),
            row.get("answer_draft_id"),
            row.get("original_answer_reference_id"),
            row.get("source_question_id"),
            row.get("external_inquiry_id"),
            row.get("question_original_masked"),
            row.get("question_masked"),
            row.get("inquiry_title"),
            row.get("inquiry_content"),
            row.get("product_name"),
            row.get("inquiry_product_name"),
            row.get("final_answer"),
            row.get("corrected_answer_masked"),
            row.get("original_answer_masked"),
            row.get("learning_source"),
            row.get("source"),
            row.get("provenance"),
            row.get("original_answer_source"),
            row.get("signal_type"),
            row.get("learning_signal_type"),
            row.get("correction_reason"),
            row.get("correction_note"),
            row.get("event_name"),
            row.get("validity_note"),
            metadata.get("answer_provenance"),
            metadata.get("answer_reference_id"),
            metadata.get("verified_by"),
            metadata.get("positive_reason"),
            metadata.get("positive_note"),
            metadata.get("learning_status"),
            metadata.get("revoke_reason"),
            metadata.get("revoked_by"),
        )
    ).lower()


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    query: str = "",
    source: str = "ALL",
    provenance: str = "ALL",
    human_verified: str = "ALL",
    signal_type: str = "ALL",
    validity_type: str = "ALL",
    validity_state: str = "ALL",
) -> list[dict[str, Any]]:
    needle = str(query or "").strip().lower()
    result: list[dict[str, Any]] = []
    for row in rows:
        metadata = _metadata(row)
        row_source = str(row.get("learning_source") or row.get("source") or "")
        row_provenance = str(
            row.get("provenance")
            or row.get("original_answer_source")
            or metadata.get("answer_provenance")
            or "UNKNOWN"
        )
        row_signal = str(
            row.get("signal_type")
            or row.get("learning_signal_type")
            or metadata.get("learning_signal_type")
            or ("POSITIVE" if row.get("learning_source") else "")
        )
        verified = _is_human_verified(row)
        if needle and needle not in _search_blob(row):
            continue
        if source != "ALL" and row_source != source:
            continue
        if provenance != "ALL" and row_provenance != provenance:
            continue
        if signal_type != "ALL" and row_signal != signal_type:
            continue
        if human_verified == "YES" and not verified:
            continue
        if human_verified == "NO" and verified:
            continue
        if validity_type != "ALL" and str(
            row.get("validity_type") or "PERMANENT"
        ).upper() != validity_type:
            continue
        if validity_state != "ALL" and str(
            row.get("validity_status") or "ACTIVE"
        ).upper() != validity_state:
            continue
        result.append(row)
    return result


def _is_human_verified(row: dict[str, Any]) -> bool:
    metadata = _metadata(row)
    return bool(
        row.get("human_verified")
        or metadata.get("human_verified")
        or str(row.get("validator_result") or "").upper()
        == "HUMAN_VERIFIED_NAVER_POSTED"
    )


def _learning_status_label(row: dict[str, Any]) -> str:
    metadata = _metadata(row)
    signal = str(
        row.get("signal_type")
        or row.get("learning_signal_type")
        or metadata.get("learning_signal_type")
        or ("POSITIVE" if row.get("learning_source") else "")
    ).upper()
    active = bool(row.get("active"))
    if signal == "POSITIVE":
        return "Positive 승인" if active else "Positive 승인 취소"
    if signal == "NEGATIVE":
        return "Negative" if active else "Negative 취소"
    if signal == "EXCLUDED":
        return "학습 제외" if active else "학습 제외 취소"
    if signal == "INTENT_CORRECTION":
        return "의도 교정" if active else "의도 교정 취소"
    return "미평가"


def _inquiry_type_label(row: dict[str, Any]) -> str:
    raw = str(
        row.get("inquiry_source_type")
        or row.get("source_inquiry_type")
        or row.get("inquiry_type")
        or ""
    ).strip()
    return INQUIRY_TYPE_LABELS.get(raw.upper(), raw or "-")


def _external_inquiry_number(row: dict[str, Any]) -> str:
    return str(
        row.get("source_question_id") or row.get("external_inquiry_id") or "-"
    )


def _inquiry_datetime(row: dict[str, Any]) -> Any:
    return (
        row.get("source_created_at")
        or row.get("registered_at")
        or row.get("created_at")
        or row.get("updated_at")
    )


def _question(row: dict[str, Any]) -> str:
    return str(
        row.get("question_original_masked")
        or row.get("question_masked")
        or row.get("inquiry_content")
        or row.get("inquiry_title")
        or ""
    )


def _learning_answer(row: dict[str, Any]) -> str:
    return str(
        row.get("final_answer")
        or row.get("corrected_answer_masked")
        or row.get("original_answer_masked")
        or ""
    )


def _display_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "문의일시": format_datetime_minute_kst(_inquiry_datetime(row)),
        "학습상태": _learning_status_label(row),
        "네이버 문의번호": _external_inquiry_number(row),
        "문의유형": _inquiry_type_label(row),
        "유효성": validity_summary(row) if row.get("learning_source") else "-",
        "질문": _question(row),
        "학습답변": _learning_answer(row),
    }


def _paginate_rows(
    rows: list[dict[str, Any]], page: int, page_size: int
) -> tuple[list[dict[str, Any]], int, int]:
    safe_size = max(1, int(page_size))
    total_pages = max(1, (len(rows) + safe_size - 1) // safe_size)
    safe_page = min(max(1, int(page)), total_pages)
    start = (safe_page - 1) * safe_size
    return rows[start : start + safe_size], safe_page, total_pages


def _learning_filter_changed() -> None:
    st.session_state["current_page"] = "learning"
    st.session_state["learning_manager_positive_page"] = 1
    st.session_state["learning_manager_feedback_page"] = 1


def _render_pagination(
    *, total: int, current_page: int, total_pages: int, key_prefix: str
) -> None:
    columns = st.columns([1, 1, 2.2, 1, 1], gap="small")
    if columns[0].button(
        "처음",
        key=f"{key_prefix}_first",
        disabled=current_page == 1,
        width="stretch",
    ):
        st.session_state[f"{key_prefix}_page"] = 1
        st.session_state["current_page"] = "learning"
        st.rerun()
    if columns[1].button(
        "이전",
        key=f"{key_prefix}_previous",
        disabled=current_page == 1,
        width="stretch",
    ):
        st.session_state[f"{key_prefix}_page"] = current_page - 1
        st.session_state["current_page"] = "learning"
        st.rerun()
    columns[2].caption(
        f"총 {total:,}건 · {current_page} / {total_pages} 페이지"
    )
    if columns[3].button(
        "다음",
        key=f"{key_prefix}_next",
        disabled=current_page == total_pages,
        width="stretch",
    ):
        st.session_state[f"{key_prefix}_page"] = current_page + 1
        st.session_state["current_page"] = "learning"
        st.rerun()
    if columns[4].button(
        "마지막",
        key=f"{key_prefix}_last",
        disabled=current_page == total_pages,
        width="stretch",
    ):
        st.session_state[f"{key_prefix}_page"] = total_pages
        st.session_state["current_page"] = "learning"
        st.rerun()


def _render_default_table(rows: list[dict[str, Any]]) -> None:
    st.dataframe(
        [_display_row(row) for row in rows],
        width="stretch",
        hide_index=True,
        row_height=72,
        column_config={
            "문의일시": st.column_config.TextColumn(width="small"),
            "학습상태": st.column_config.TextColumn(width="small"),
            "네이버 문의번호": st.column_config.TextColumn(width="medium"),
            "문의유형": st.column_config.TextColumn(width="small"),
            "유효성": st.column_config.TextColumn(width="small"),
            "질문": st.column_config.TextColumn(width=320),
            "학습답변": st.column_config.TextColumn(width=480),
        },
    )


def _render_validity_editor(
    row: dict[str, Any], repository: LearningRepository, *, key_prefix: str
) -> None:
    learning_id = int(row["id"])
    current_type = str(row.get("validity_type") or "PERMANENT").upper()
    start_at = to_kst(row.get("valid_from"))
    end_at = to_kst(row.get("valid_until"))
    st.markdown("**학습 유효성 관리**")
    st.caption(f"현재 상태: {validity_summary(row)} ({row.get('validity_status', 'ACTIVE')})")
    with st.form(f"{key_prefix}_validity_form_{learning_id}"):
        validity_type = st.radio(
            "학습 유효성",
            ["PERMANENT", "TEMPORARY"],
            index=1 if current_type == "TEMPORARY" else 0,
            format_func=lambda value: "기간성" if value == "TEMPORARY" else "영구",
            horizontal=True,
        )
        event_name = ""
        valid_from: date | None = None
        valid_until: date | None = None
        if validity_type == "TEMPORARY":
            event_name = st.text_input("이벤트명", value=str(row.get("event_name") or ""))
            dates = st.columns(2)
            valid_from = dates[0].date_input(
                "유효 시작일", value=start_at.date() if start_at else date.today()
            )
            valid_until = dates[1].date_input(
                "유효 종료일", value=end_at.date() if end_at else date.today()
            )
        validity_note = st.text_area(
            "운영 메모", value=str(row.get("validity_note") or "")
        )
        submitted = st.form_submit_button("유효성 저장", type="primary")
    if submitted:
        try:
            repository.update_validity(
                learning_id,
                validity_type=validity_type,
                event_name=event_name,
                valid_from=valid_from,
                valid_until=valid_until,
                validity_active=True,
                validity_note=validity_note,
                condition=row.get("condition_json") or {},
                expected_updated_at=str(row.get("updated_at") or ""),
            )
        except (ValueError, LookupError, RuntimeError) as error:
            st.error(str(error))
        else:
            st.toast("Learning 유효성 정보를 저장했습니다.")
            st.session_state["current_page"] = "learning"
            st.rerun()
    if current_type == "TEMPORARY" and bool(row.get("validity_active", True)):
        if st.button(
            "지금 비활성화",
            key=f"{key_prefix}_disable_{learning_id}",
            help="Learning은 삭제하지 않고 신규 답변 후보에서 즉시 제외합니다.",
        ):
            try:
                repository.update_validity(
                    learning_id,
                    validity_type="TEMPORARY",
                    event_name=row.get("event_name"),
                    valid_from=row.get("valid_from"),
                    valid_until=row.get("valid_until"),
                    validity_active=False,
                    validity_note=row.get("validity_note"),
                    condition=row.get("condition_json") or {},
                    expected_updated_at=str(row.get("updated_at") or ""),
                )
            except (ValueError, LookupError, RuntimeError) as error:
                st.error(str(error))
            else:
                st.toast("Learning을 비활성화했습니다. 과거 이력은 보존됩니다.")
                st.session_state["current_page"] = "learning"
                st.rerun()


def _render_details(
    rows: list[dict[str, Any]],
    *,
    key_prefix: str,
    repository: LearningRepository | None = None,
) -> None:
    with st.expander("상세 정보", expanded=False):
        row_by_id = {int(row["id"]): row for row in rows}
        ids = list(row_by_id)
        state_key = f"{key_prefix}_selected_id"
        if st.session_state.get(state_key) not in ids:
            st.session_state[state_key] = ids[0]
        selected_id = st.selectbox(
            "상세 조회 항목",
            ids,
            format_func=lambda learning_id: (
                f"{_external_inquiry_number(row_by_id[learning_id])} · "
                f"{_learning_status_label(row_by_id[learning_id])} · "
                f"{_question(row_by_id[learning_id])[:45]}"
            ),
            key=state_key,
        )
        row = row_by_id[int(selected_id)]
        metadata = _metadata(row)
        st.caption(
            f"Learning ID {row.get('id')} · 문의일시 "
            f"{format_datetime_kst(_inquiry_datetime(row))} · 네이버 문의번호 "
            f"{_external_inquiry_number(row)}"
        )
        st.write(f"상품명: {row.get('inquiry_product_name') or row.get('product_name') or '-'}")
        st.write(f"문의유형: {_inquiry_type_label(row)}")
        st.markdown("**질문 원문**")
        st.write(_question(row) or "-")
        st.markdown("**실제 답변**")
        st.write(row.get("seller_answer") or row.get("edited_answer") or "-")
        st.markdown("**학습답변 원문**")
        st.write(_learning_answer(row) or "-")
        if repository is not None:
            _render_validity_editor(row, repository, key_prefix=key_prefix)
        st.markdown("**고급 정보**")
        st.json(
            {
                "Source": row.get("learning_source") or row.get("source") or "-",
                "Provenance": row.get("provenance")
                or row.get("original_answer_source")
                or metadata.get("answer_provenance")
                or "UNKNOWN",
                "Answer Reference": metadata.get("answer_reference_id")
                or row.get("original_answer_reference_id")
                or row.get("answer_draft_id")
                or "-",
                "내부 Learning/Feedback ID": row.get("id"),
                "내부 Inquiry ID": row.get("inquiry_id"),
                "Draft ID": row.get("answer_draft_id") or "-",
                "Human verified": _is_human_verified(row),
                "학습 상태": _learning_status_label(row),
                "유효성 상태": row.get("validity_status") or "ACTIVE",
                "이벤트명": row.get("event_name") or "-",
                "유효 시작": row.get("valid_from") or "-",
                "유효 종료": row.get("valid_until") or "-",
                "수동 활성": bool(row.get("validity_active", True)),
                "만료/비활성 처리 시각": row.get("expired_at") or "-",
                "생성일": row.get("created_at") or "-",
                "수정일": row.get("updated_at") or "-",
                "revoke 여부": not bool(row.get("active")),
                "운영 메모": row.get("validity_note") or "-",
                "조건 metadata": row.get("condition_json") or {},
            },
            expanded=False,
        )


def _render_section(
    rows: list[dict[str, Any]], *, page_size: int, key_prefix: str,
    repository: LearningRepository | None = None,
) -> None:
    page_key = f"{key_prefix}_page"
    page_rows, current_page, total_pages = _paginate_rows(
        rows, int(st.session_state.get(page_key, 1)), page_size
    )
    st.session_state[page_key] = current_page
    _render_default_table(page_rows)
    _render_pagination(
        total=len(rows),
        current_page=current_page,
        total_pages=total_pages,
        key_prefix=key_prefix,
    )
    _render_details(page_rows, key_prefix=key_prefix, repository=repository)


def render_learning_manager(database: Database | None) -> None:
    st.session_state["current_page"] = "learning"
    st.title("Learning Manager")
    st.caption(
        "Positive Learning과 Negative/의도 교정/학습 제외 상태를 실제 문의 접수시간 "
        "기준으로 조회하고 추적하는 운영 화면입니다."
    )
    if database is None:
        st.warning("Learning Repository DB를 사용할 수 없습니다.")
        return

    repository = LearningRepository(database)
    feedback_repository = LearningFeedbackRepository(database)
    summary = repository.manager_summary()
    feedback_summary = feedback_repository.manager_summary()
    metrics = st.columns(6, gap="small")
    metric_values = (
        ("저장된 Positive", summary["total"], "저장된 전체 Positive/legacy 학습 수"),
        ("활성 Positive", summary["positive_active"], "현재 검색 후보로 사용 가능한 Positive 수"),
        ("Human Verified", summary["human_verified"], "직원이 검증한 Positive 수"),
        ("Negative", feedback_summary.get("NEGATIVE", 0), "활성 Negative 평가 수"),
        ("Intent Correction", feedback_summary.get("INTENT_CORRECTION", 0), "활성 의도 교정 수"),
        ("Excluded", feedback_summary.get("EXCLUDED", 0), "활성 학습 제외 수"),
    )
    for column, (label, value, help_text) in zip(metrics, metric_values):
        column.metric(label, value, help=help_text)
    st.caption(
        "최근 Learning 생성: "
        + format_datetime_kst(summary.get("recent"), empty="없음")
        + " · 목록은 Learning 갱신시각이 아닌 실제 문의 접수시각으로 정렬됩니다."
    )

    positive_rows = repository.manager_rows(limit=2_000)
    feedback_rows = feedback_repository.manager_rows(limit=2_000)
    all_rows = [*positive_rows, *feedback_rows]
    source_options = sorted(
        {
            str(row.get("learning_source") or row.get("source"))
            for row in all_rows
            if row.get("learning_source") or row.get("source")
        }
    )
    provenance_options = sorted(
        {
            str(
                row.get("provenance")
                or row.get("original_answer_source")
                or _metadata(row).get("answer_provenance")
                or "UNKNOWN"
            )
            for row in all_rows
        }
    )
    filters = st.columns([2.4, 1.15, 1.15, 1.0, 0.85], gap="small")
    query = filters[0].text_input(
        "문의/참조 검색",
        placeholder="네이버 문의번호, 질문, 상품명, source/reference 검색",
        key="learning_manager_query",
        on_change=_learning_filter_changed,
    )
    selected_source = filters[1].selectbox(
        "Learning source",
        ["ALL", *source_options],
        key="learning_manager_source",
        on_change=_learning_filter_changed,
    )
    selected_provenance = filters[2].selectbox(
        "Provenance",
        ["ALL", *provenance_options],
        key="learning_manager_provenance",
        on_change=_learning_filter_changed,
    )
    selected_verified = filters[3].selectbox(
        "Human verified",
        ["ALL", "YES", "NO"],
        key="learning_manager_verified",
        on_change=_learning_filter_changed,
    )
    page_size = filters[4].selectbox(
        "페이지 크기",
        [20, 50, 100],
        key="learning_manager_page_size",
        on_change=_learning_filter_changed,
    )
    validity_filters = st.columns(2, gap="small")
    selected_validity = validity_filters[0].selectbox(
        "유효성",
        ["ALL", "PERMANENT", "TEMPORARY"],
        format_func=lambda value: {
            "ALL": "전체", "PERMANENT": "영구", "TEMPORARY": "기간성"
        }[value],
        key="learning_manager_validity",
        on_change=_learning_filter_changed,
    )
    selected_validity_state = validity_filters[1].selectbox(
        "유효 상태",
        ["ALL", "ACTIVE", "SCHEDULED", "EXPIRED", "DISABLED"],
        format_func=lambda value: {
            "ALL": "전체", "ACTIVE": "활성", "SCHEDULED": "시작 전",
            "EXPIRED": "만료", "DISABLED": "수동 비활성",
        }[value],
        key="learning_manager_validity_state",
        on_change=_learning_filter_changed,
    )

    positive = _filter_rows(
        positive_rows,
        query=query,
        source=selected_source,
        provenance=selected_provenance,
        human_verified=selected_verified,
        signal_type="POSITIVE",
        validity_type=selected_validity,
        validity_state=selected_validity_state,
    )
    st.subheader("Positive Learning")
    st.caption("승인된 Positive와 soft revoke 이력을 실제 문의시각 최신순으로 표시합니다.")
    if not positive:
        st.info("조건에 맞는 Positive Learning이 없습니다.")
    else:
        _render_section(
            positive,
            page_size=int(page_size),
            key_prefix="learning_manager_positive",
            repository=repository,
        )

    selected_signal = st.selectbox(
        "Feedback 상태",
        list(SIGNAL_FILTER_LABELS),
        format_func=SIGNAL_FILTER_LABELS.get,
        key="learning_manager_signal",
        on_change=_learning_filter_changed,
    )
    feedback = _filter_rows(
        feedback_rows,
        query=query,
        source=selected_source,
        provenance=selected_provenance,
        human_verified=selected_verified,
        signal_type=selected_signal,
    )
    st.subheader("Negative / 의도 교정 / 학습 제외")
    st.caption("평가와 soft revoke 이력을 실제 문의시각 최신순으로 표시합니다.")
    if not feedback:
        st.info("조건에 맞는 Feedback이 없습니다.")
    else:
        _render_section(
            feedback,
            page_size=int(page_size),
            key_prefix="learning_manager_feedback",
        )
