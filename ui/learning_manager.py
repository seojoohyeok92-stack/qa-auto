from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from core.time_utils import format_datetime_kst, format_datetime_minute_kst, to_kst
from answer.learning_feedback import (
    CORRECTION_REASON_LABELS,
    EXCLUSION_REASON_LABELS,
    CorrectionReason,
    ExclusionReason,
)
from answer.learning_signal import SIGNAL_KIND_LABELS
from repositories.database import Database
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_manager_query import (
    LearningManagerPage,
    manager_search_matches,
)
from repositories.learning_repository import (
    LearningRepository,
    is_common_learning,
)
from repositories.learning_signal_repository import LearningSignalRepository
from services.learning_validity_service import validity_summary
from services.learning_lifecycle_service import is_explicitly_approved_learning
from ui.session_identity import current_actor


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

LEARNING_STATE_BADGES = (
    ("Positive", "positive"),
    ("Negative", "negative"),
    ("학습 제외", "excluded"),
)

DEFAULT_COLUMNS = (
    "문의일시",
    "학습상태",
    "원래상태",
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
            row.get("seller_answer"),
            row.get("edited_answer"),
            row.get("gpt_draft"),
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
            row.get("valid_from"),
            row.get("valid_until"),
            row.get("condition_json"),
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
        if needle and not manager_search_matches(needle, _search_blob(row)):
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
    effective = str(row.get("effective_learning_status") or "").upper()
    if effective == "EXCLUDED":
        return "제외"
    if effective == "CORRECTED":
        return "교정"
    if effective == "APPROVED":
        return "Positive 승인"
    if effective == "AUTO":
        return "Positive 자동"
    if effective == "NONE":
        return "-"
    return _history_status_label(row)


def _history_status_label(row: dict[str, Any]) -> str:
    metadata = _metadata(row)
    signal = str(
        row.get("signal_type")
        or row.get("learning_signal_type")
        or metadata.get("learning_signal_type")
        or ("POSITIVE" if row.get("learning_source") else "")
    ).upper()
    active = bool(row.get("active"))
    if signal == "POSITIVE":
        authority = "승인" if is_explicitly_approved_learning(row) else "자동"
        return f"Positive {authority}" if active else f"Positive {authority} (비활성)"
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


def _feedback_reason_summary(row: dict[str, Any]) -> str:
    code = str(row.get("correction_reason") or "").strip()
    signal = str(row.get("learning_signal_type") or "").upper()
    label = code or "-"
    try:
        if signal == "EXCLUDED":
            label = EXCLUSION_REASON_LABELS[ExclusionReason(code)]
        else:
            label = CORRECTION_REASON_LABELS[CorrectionReason(code)]
    except ValueError:
        pass
    note = str(row.get("correction_note") or "").strip()
    return f"{label} ({code})" + (f" · {note}" if note else "")


def _display_row(row: dict[str, Any]) -> dict[str, str]:
    return {
        "문의일시": format_datetime_minute_kst(_inquiry_datetime(row)),
        "학습상태": _learning_status_label(row),
        "원래상태": _history_status_label(row),
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
    st.session_state["production_admin_mode"] = True
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
            "원래상태": st.column_config.TextColumn(width="small"),
            "네이버 문의번호": st.column_config.TextColumn(width="medium"),
            "문의유형": st.column_config.TextColumn(width="small"),
            "유효성": st.column_config.TextColumn(width="small"),
            "질문": st.column_config.TextColumn(width=320),
            "학습답변": st.column_config.TextColumn(width=480),
        },
    )


APPLICATION_SCOPE_LABELS = {
    "MODEL": "현재 모델",
    "PRODUCT_FAMILY": "동일 제품군",
    "POLICY": "전 모델 공통 정책",
}

MARKET_APPLICABILITY_LABELS = {
    "NAVER_ONLY": "네이버",
    "COUPANG_ONLY": "쿠팡",
    "COMMON": "공통",
}


def _render_scope_editor(
    row: dict[str, Any], repository: LearningRepository, *, key_prefix: str
) -> None:
    """Choose which products and which markets one Learning row answers for.

    The row's existing values are shown as-is and nothing is written until the
    operator submits. A scope the lexical profiler inferred -- VARIANT,
    CATEGORY or GLOBAL, none of which is offered here -- is named in the
    caption and left alone, so opening this panel cannot silently reclassify
    a row that nobody decided about.
    """

    learning_id = int(row["id"])
    metadata = _metadata(row)
    stored_scope = str(metadata.get("product_scope") or "").strip().upper()
    stored_market = str(metadata.get("market_applicability") or "").strip().upper()
    common = is_common_learning(metadata)
    # "전 모델 공통" is not offered on an original row. That row is the whole
    # customer inquiry, and approving it whole would publish every part of it.
    # It is reachable through "공통 Learning 추가", which stores the part the
    # operator selected as its own row.
    scopes = [
        value for value in APPLICATION_SCOPE_LABELS
        if value != "POLICY" or common
    ]
    markets = list(MARKET_APPLICABILITY_LABELS)

    st.markdown("**적용 범위 관리**")
    st.caption(
        "저장된 적용 대상: {scope} · 적용 마켓: {market} · 공통 Learning: {common}".format(
            scope=APPLICATION_SCOPE_LABELS.get(stored_scope, stored_scope or "미지정"),
            market=MARKET_APPLICABILITY_LABELS.get(
                stored_market, stored_market or "미지정(출처 마켓 기준)"
            ),
            common="예" if common else "아니오",
        )
    )
    if not common:
        st.caption(
            "전 모델 공통 적용은 아래 '공통 Learning 추가'에서 공통으로 사용할 "
            "질문과 답변만 선택해 별도 항목으로 등록합니다. 원본 문의 전체는 "
            "공통으로 승격되지 않습니다."
        )
    with st.form(f"{key_prefix}_scope_form_{learning_id}"):
        product_scope = st.radio(
            "적용 대상",
            scopes,
            index=scopes.index(stored_scope) if stored_scope in scopes else 0,
            format_func=lambda value: APPLICATION_SCOPE_LABELS[value],
            horizontal=True,
        )
        market_applicability = st.radio(
            "적용 마켓",
            markets,
            index=markets.index(stored_market) if stored_market in markets else 2,
            format_func=lambda value: MARKET_APPLICABILITY_LABELS[value],
            horizontal=True,
            help="마켓 전용 정책은 반대 마켓의 답변 근거에서 제외됩니다.",
        )
        st.caption(
            "행사 기간은 아래 '학습 유효성 관리'에서 지정합니다. "
            "카드 종류·결제수단 등 적용 조건은 학습답변 본문에 남아 있어야 합니다."
        )
        submitted = st.form_submit_button("적용 범위 저장", type="primary")
    if submitted:
        try:
            repository.update_application_scope(
                learning_id,
                product_scope=product_scope,
                market_applicability=market_applicability,
                actor=current_actor(),
                expected_updated_at=str(row.get("updated_at") or ""),
            )
        except (ValueError, LookupError, RuntimeError) as error:
            st.error(str(error))
        else:
            st.toast("Learning 적용 범위를 저장했습니다.")
            st.session_state["current_page"] = "learning"
            st.rerun()


def _clear_editor_inputs(key_prefix: str, slot: str) -> None:
    """Drop this editor's widget state when it closes.

    Streamlit keeps a widget's value under its key for as long as the key is
    in session state, so a reopened editor would otherwise show whatever was
    typed the previous time instead of the origin's text. Keys are scoped by
    ``key_prefix`` and the Learning id, so clearing one editor cannot touch
    another inquiry's.
    """

    for suffix in (
        "q", "a", "scope", "market", "validity", "event", "from", "until",
    ):
        st.session_state.pop(f"{key_prefix}_common_{suffix}_{slot}", None)


def _render_common_learning_editor(
    *,
    key_prefix: str,
    slot: str,
    origin_question: str,
    origin_answer: str,
    save: Callable[..., dict[str, Any]],
    caption: str,
    button_label: str,
    button_help: str,
    context: dict[str, Any],
) -> None:
    """One editor, two entry points.

    The form is the same whether the operator starts from an approved
    Learning or from the inquiry itself; only where the saved row gets its
    context differs, and that is the ``save`` callable's business. Keeping one
    form means the range, the validity window and the preview cannot drift
    apart between the two paths.

    Nothing here writes ``current_page`` or ``production_admin_mode``, so the
    caller's screen -- Dashboard or Learning Manager -- stays where it is.
    """

    state_key = f"{key_prefix}_common_open_{slot}"
    if not st.session_state.get(state_key):
        created_id = st.session_state.get(f"{state_key}_saved")
        if created_id:
            st.success(f"공통 Learning {created_id} 을 추가했습니다.")
        st.caption(caption)
        if st.button(
            button_label,
            key=f"{key_prefix}_common_start_{slot}",
            help=button_help,
        ):
            st.session_state[state_key] = True
            st.session_state.pop(f"{state_key}_saved", None)
            st.rerun()
        return

    st.caption(caption)
    scopes = list(APPLICATION_SCOPE_LABELS)
    markets = list(MARKET_APPLICABILITY_LABELS)
    question = st.text_area(
        "공통으로 사용할 질문",
        value=origin_question,
        key=f"{key_prefix}_common_q_{slot}",
        help="복합 문의라면 공통으로 쓸 질문만 남기고 나머지는 지웁니다.",
    )
    answer = st.text_area(
        "공통으로 사용할 답변",
        value=origin_answer,
        height=160,
        key=f"{key_prefix}_common_a_{slot}",
        help="적용 조건(카드 종류·마켓·기간 등)은 본문에 남겨 두세요.",
    )
    scope_columns = st.columns(2, gap="small")
    product_scope = scope_columns[0].radio(
        "적용 모델",
        scopes,
        index=scopes.index("POLICY"),
        format_func=lambda value: APPLICATION_SCOPE_LABELS[value],
        key=f"{key_prefix}_common_scope_{slot}",
    )
    market_applicability = scope_columns[1].radio(
        "적용 마켓",
        markets,
        index=markets.index("COMMON"),
        format_func=lambda value: MARKET_APPLICABILITY_LABELS[value],
        key=f"{key_prefix}_common_market_{slot}",
    )
    validity_type = st.radio(
        "유효기간",
        ["PERMANENT", "TEMPORARY"],
        format_func=lambda value: "기간 지정" if value == "TEMPORARY" else "상시",
        horizontal=True,
        key=f"{key_prefix}_common_validity_{slot}",
    )
    event_name = ""
    valid_from: date | None = None
    valid_until: date | None = None
    if validity_type == "TEMPORARY":
        period = st.columns(3, gap="small")
        event_name = period[0].text_input(
            "이벤트명", key=f"{key_prefix}_common_event_{slot}"
        )
        valid_from = period[1].date_input(
            "유효 시작일", value=date.today(),
            key=f"{key_prefix}_common_from_{slot}",
        )
        valid_until = period[2].date_input(
            "유효 종료일", value=date.today(),
            key=f"{key_prefix}_common_until_{slot}",
        )

    st.markdown("**저장 전 확인**")
    st.json(
        {
            "질문": question.strip(),
            "답변": answer.strip(),
            "적용 모델": APPLICATION_SCOPE_LABELS[product_scope],
            "적용 마켓": MARKET_APPLICABILITY_LABELS[market_applicability],
            "유효기간": (
                "상시" if validity_type == "PERMANENT"
                else f"{event_name or '(이벤트명 없음)'} · {valid_from} ~ {valid_until}"
            ),
            **context,
        },
        expanded=True,
    )
    actions = st.columns(2, gap="small")
    if actions[0].button(
        "공통 Learning 저장",
        key=f"{key_prefix}_common_save_{slot}",
        type="primary",
        width="stretch",
    ):
        try:
            created = save(
                question=question,
                answer=answer,
                product_scope=product_scope,
                market_applicability=market_applicability,
                validity_type=validity_type,
                event_name=event_name,
                valid_from=valid_from,
                valid_until=valid_until,
                actor=current_actor(),
            )
        except (ValueError, LookupError, RuntimeError) as error:
            # 입력은 위젯 key 에 남아 있고, rerun 하지 않으므로 그대로 보인다.
            st.error(str(error))
        else:
            st.session_state.pop(state_key, None)
            st.session_state[f"{state_key}_saved"] = int(created["id"])
            _clear_editor_inputs(key_prefix, slot)
            st.rerun()
    if actions[1].button(
        "취소", key=f"{key_prefix}_common_cancel_{slot}", width="stretch"
    ):
        st.session_state.pop(state_key, None)
        st.session_state.pop(f"{state_key}_saved", None)
        _clear_editor_inputs(key_prefix, slot)
        st.rerun()


def render_common_learning_add(
    row: dict[str, Any], repository: LearningRepository, *, key_prefix: str
) -> None:
    """Add the part of this approved answer that applies beyond one model.

    The original question and answer are loaded as the starting text and the
    operator edits them down to the part that generalises, so a composite
    inquiry contributes only the question they selected. Nothing is written
    to the original row.
    """

    if is_common_learning(_metadata(row)):
        return
    learning_id = int(row["id"])
    st.markdown("**공통 Learning 추가**")
    _render_common_learning_editor(
        key_prefix=key_prefix,
        slot=str(learning_id),
        origin_question=str(
            _question(row) or row.get("question_original_masked") or ""
        ),
        origin_answer=str(
            _learning_answer(row) or row.get("final_answer") or ""
        ),
        save=lambda **values: repository.add_common_learning(
            learning_id, **values
        ),
        caption=(
            "이 답변에서 다른 모델에도 적용되는 질문과 답변만 골라 별도 항목으로 "
            "등록합니다. 원본 Learning 은 변경되지 않습니다."
        ),
        button_label="공통 Learning 추가",
        button_help="원본 질문과 승인된 최종 답변을 불러와 편집합니다.",
        context={"원본 Learning ID": learning_id},
    )


def render_common_learning_direct(
    inquiry: dict[str, Any],
    repository: LearningRepository,
    *,
    key_prefix: str,
    answer: str = "",
) -> None:
    """Approve a common Learning straight from the inquiry, with no approval first.

    The general Positive approval and this one are separate decisions, and
    neither is a precondition for the other. A seasonal policy -- 삼성감사제 and
    the like -- can be recorded without first approving an answer nobody
    wanted in the corpus, and approving one here creates no Positive Learning
    and changes no approval state.
    """

    inquiry_id = int(inquiry["id"])
    st.markdown("**공통 Learning 직접 승인**")
    _render_common_learning_editor(
        key_prefix=key_prefix,
        slot=f"inq{inquiry_id}",
        origin_question=str(inquiry.get("content") or ""),
        origin_answer=str(answer or ""),
        save=lambda **values: repository.add_common_learning_for_inquiry(
            inquiry_id, **values
        ),
        caption=(
            "이 문의와 답변에서 다른 문의에도 적용되는 판매·결제·배송 정책이나 "
            "운영자가 확인한 공통 사양만 골라 공통 Learning 으로 등록합니다. "
            "일반 Learning 승인과는 별개이며, 이 저장으로 일반 Positive "
            "Learning 이 만들어지거나 승인 상태가 바뀌지 않습니다."
        ),
        button_label="공통 Learning 직접 승인",
        button_help="현재 문의와 답변을 불러와 편집합니다. 일반 승인은 필요하지 않습니다.",
        context={"원본 문의 ID": inquiry_id},
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
    # A radio inside a Streamlit form does not rerun until submit, so the
    # TEMPORARY-only fields could never become visible on first selection.
    # Keep the selector outside while retaining one atomic save form.
    validity_type = st.radio(
        "학습 유효성",
        ["PERMANENT", "TEMPORARY"],
        index=1 if current_type == "TEMPORARY" else 0,
        format_func=lambda value: "기간성" if value == "TEMPORARY" else "영구",
        horizontal=True,
        key=f"{key_prefix}_validity_type_{learning_id}",
        on_change=lambda: st.session_state.update(
            current_page="learning", production_admin_mode=True
        ),
    )
    with st.form(f"{key_prefix}_validity_form_{learning_id}"):
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
        if row.get("learning_signal_type"):
            st.markdown("**Feedback Reason**")
            st.write(_feedback_reason_summary(row))
            if not row.get("active"):
                st.write(
                    "취소 사유: "
                    + str(metadata.get("revoke_reason") or "기록 없음")
                )
        st.markdown("**질문 원문**")
        st.write(_question(row) or "-")
        st.markdown("**실제 답변**")
        st.write(row.get("seller_answer") or row.get("edited_answer") or "-")
        st.markdown("**학습답변 원문**")
        st.write(_learning_answer(row) or "-")
        if repository is not None:
            _render_scope_editor(row, repository, key_prefix=key_prefix)
            render_common_learning_add(row, repository, key_prefix=key_prefix)
            _render_validity_editor(row, repository, key_prefix=key_prefix)
        if repository is not None and row.get("inquiry_id") is not None:
            signals = LearningSignalRepository(repository.database).for_inquiry(
                int(row["inquiry_id"])
            )
            if signals:
                st.markdown("**구조화된 Learning Signal**")
                st.dataframe(
                    [
                        {
                            "유형": SIGNAL_KIND_LABELS.get(
                                item["signal_kind"], item["signal_kind"]
                            ),
                            "내용": item.get("content_text") or "-",
                            "적용 범위": item.get("product_scope") or "-",
                            "상태": "활성" if item.get("active") else "취소됨",
                            "등록일": item.get("created_at"),
                        }
                        for item in signals
                    ],
                    hide_index=True,
                    width="stretch",
                )
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
                "적용 대상": APPLICATION_SCOPE_LABELS.get(
                    str(metadata.get("product_scope") or "").upper(),
                    metadata.get("product_scope") or "미지정",
                ),
                "적용 마켓": MARKET_APPLICABILITY_LABELS.get(
                    str(metadata.get("market_applicability") or "").upper(),
                    metadata.get("market_applicability") or "미지정(출처 마켓 기준)",
                ),
                "전 모델 공통 정책 승인": bool(metadata.get("cross_model_policy")),
                "공통 정책 승인자": metadata.get("cross_model_policy_approved_by") or "-",
                "공통 정책 승인 시각": metadata.get("cross_model_policy_approved_at") or "-",
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
                "Feedback Reason": (
                    _feedback_reason_summary(row)
                    if row.get("learning_signal_type") else "-"
                ),
                "revoke 사유": metadata.get("revoke_reason") or "-",
                "revoke 처리자": metadata.get("revoked_by") or "-",
                "운영 메모": row.get("validity_note") or "-",
                "조건 metadata": row.get("condition_json") or {},
            },
            expanded=False,
        )


def _render_section(
    page: LearningManagerPage, *, key_prefix: str,
    repository: LearningRepository | None = None,
) -> None:
    page_key = f"{key_prefix}_page"
    total_pages = max(1, (page.total + page.page_size - 1) // page.page_size)
    st.session_state[page_key] = page.page
    _render_default_table(page.rows)
    _render_pagination(
        total=page.total,
        current_page=page.page,
        total_pages=total_pages,
        key_prefix=key_prefix,
    )
    _render_details(page.rows, key_prefix=key_prefix, repository=repository)


def _structured_signal_external_number(row: dict[str, Any]) -> str:
    return str(
        row.get("source_question_id") or row.get("external_inquiry_id") or "-"
    )


def _render_structured_signal_manager(database: Database) -> None:
    from config import StructuredSignalAutoLearningSettings

    st.subheader("구조화 Learning Signal")
    settings = StructuredSignalAutoLearningSettings.from_environment()
    st.caption(
        (
            "자동 추출: "
            + ("ON" if settings.enabled else "OFF")
            + " · 자동 VERIFIED_FACT/CORRECTION 승격: "
            + "OFF (관리자 수동 승격 전용)"
            + f" · 승격 임계 확인횟수: {settings.min_confirmations_for_promotion}"
        ),
        help=(
            "AUTO_STRUCTURED_LEARNING_ENABLED / "
            "AUTO_VERIFIED_FACT_PROMOTION_ENABLED 환경변수로 제어됩니다."
        ),
    )
    repository = LearningSignalRepository(database)
    filter_columns = st.columns([1.2, 1.2, 1.3, 1.6], gap="small")
    generation_label = filter_columns[0].selectbox(
        "생성 방식", ["전체", "자동 생성", "수동 등록"],
        key="signal_manager_generation",
    )
    generation_mode = {
        "자동 생성": "AUTO_EXTRACTED", "수동 등록": "MANUAL",
    }.get(generation_label)
    kind_label = filter_columns[1].selectbox(
        "Signal 유형",
        ["전체", *[SIGNAL_KIND_LABELS[kind] for kind in SIGNAL_KIND_LABELS if kind.value != "REASON"]],
        key="signal_manager_kind",
    )
    signal_kind = next(
        (kind.value for kind, label in SIGNAL_KIND_LABELS.items() if label == kind_label),
        None,
    )
    status_label = filter_columns[2].selectbox(
        "상태", ["전체", "ACTIVE", "MANUALLY_PROMOTED", "REJECTED", "SUPERSEDED"],
        key="signal_manager_status",
    )
    confirmation_status = None if status_label == "전체" else status_label
    review_only = filter_columns[3].checkbox(
        "검토 필요만 보기 (자동 추출 + 승격 임계 미달)",
        key="signal_manager_review_only",
    )

    rows = repository.manager_rows(
        limit=1_000,
        generation_mode=generation_mode,
        signal_kind=signal_kind,
        confirmation_status=confirmation_status,
    )
    if review_only:
        rows = [
            row for row in rows
            if row.get("generation_mode") == "AUTO_EXTRACTED"
            and row.get("signal_kind") in {"CORRECTION", "VERIFIED_FACT"}
            and row.get("confirmation_status") == "ACTIVE"
            and int(row.get("live_confirmation_count") or 0)
            < settings.min_confirmations_for_promotion
        ]
    if not rows:
        st.info("조건에 맞는 Structured Learning Signal이 없습니다.")
        return

    display = []
    for row in rows:
        metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}
        display.append({
            "원본 문의": _structured_signal_external_number(row),
            "유형": SIGNAL_KIND_LABELS.get(row.get("signal_kind"), row.get("signal_kind")),
            "내용": str(row.get("content_text") or "")[:60],
            "적용범위": row.get("product_scope") or "-",
            "생성방식": "자동" if row.get("generation_mode") == "AUTO_EXTRACTED" else "수동",
            "상태": row.get("confirmation_status") or "ACTIVE",
            "확인횟수": (
                f"{int(row.get('live_confirmation_count') or 0)}/"
                f"{settings.min_confirmations_for_promotion}"
                if row.get("generation_mode") == "AUTO_EXTRACTED" else "-"
            ),
            "추출근거": row.get("diff_category") or metadata.get("rationale") or "-",
            "내부 Signal ID": row.get("id"),
            "등록일": row.get("created_at"),
        })
    st.dataframe(display, hide_index=True, width="stretch")

    with st.expander("Signal 확인/거부", expanded=False):
        ids = [row["id"] for row in rows]
        selected_id = st.selectbox(
            "대상 Signal", ids,
            format_func=lambda signal_id: next(
                f"#{signal_id} · {row['content_text'][:40]}"
                for row in rows if row["id"] == signal_id
            ),
            key="signal_manager_selected_id",
        )
        selected_row = next(row for row in rows if row["id"] == selected_id)
        st.json({
            "signal_kind": selected_row.get("signal_kind"),
            "content_text": selected_row.get("content_text"),
            "product_scope": selected_row.get("product_scope"),
            "topics": selected_row.get("topics_json"),
            "generation_mode": selected_row.get("generation_mode"),
            "confirmation_status": selected_row.get("confirmation_status"),
            "live_confirmation_count": selected_row.get("live_confirmation_count"),
            "diff_category": selected_row.get("diff_category"),
            "metadata": selected_row.get("metadata_json"),
        })
        action_columns = st.columns(2, gap="small")
        if action_columns[0].button(
            "수동 확인(승격)", key="signal_manager_promote",
            disabled=selected_row.get("confirmation_status") in {"MANUALLY_PROMOTED", "REJECTED"},
        ):
            repository.promote(int(selected_id), actor=current_actor())
            st.success("Signal을 확인 처리했습니다. 즉시 evidence로 사용 가능합니다.")
            st.rerun()
        reject_reason = action_columns[1].text_input(
            "거부 사유", key="signal_manager_reject_reason",
        )
        if action_columns[1].button(
            "거부", key="signal_manager_reject",
            disabled=(
                not reject_reason.strip()
                or selected_row.get("confirmation_status") == "REJECTED"
            ),
        ):
            repository.reject(
                int(selected_id), actor=current_actor(), reason=reject_reason,
            )
            st.success("Signal을 거부했습니다.")
            st.rerun()



_EXPORT_PATH_KEY = "learning_manager_export_path"
_EXCEL_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def _render_excel_export() -> None:
    """Generate the operational workbook and hand it to the operator.

    The file path -- not the bytes -- is what survives between reruns, so
    clicking Download does not rebuild the workbook and a page rerun does not
    make the button vanish. Generation is read-only: the export opens both
    databases with ``mode=ro`` and verifies afterwards that neither file
    changed.
    """

    with st.expander("Q&A / Learning Excel 내보내기", expanded=False):
        st.caption(
            "현재 운영 데이터를 하나의 Excel 파일로 저장합니다. "
            "데이터를 읽기만 하며 문의·Learning은 변경되지 않습니다."
        )
        if st.button("Excel 파일 만들기", key="learning_manager_export_run"):
            try:
                from scripts.export_qa_learning_excel import export

                with st.spinner("Excel 파일을 만드는 중입니다..."):
                    path, counts = export()
            except Exception as error:  # noqa: BLE001 - shown to the operator
                st.session_state.pop(_EXPORT_PATH_KEY, None)
                st.error(f"Excel 생성에 실패했습니다: {error}")
            else:
                st.session_state[_EXPORT_PATH_KEY] = str(path)
                st.success(
                    "생성 완료 · "
                    + " / ".join(f"{name} {count}행" for name, count in counts.items())
                )

        saved = str(st.session_state.get(_EXPORT_PATH_KEY) or "")
        if not saved:
            return
        path = Path(saved)
        if not path.exists():
            st.session_state.pop(_EXPORT_PATH_KEY, None)
            return
        st.download_button(
            "Excel 내려받기",
            data=path.read_bytes(),
            file_name=path.name,
            mime=_EXCEL_MIME,
            key="learning_manager_export_download",
        )
        st.caption(f"저장 위치: {path}")


def _common_status(row: dict[str, Any]) -> str:
    """Reuse the existing validity status rather than inventing a second one."""

    if not bool(row.get("active")):
        return "사용 중지"
    status = str(row.get("validity_status") or "ACTIVE").upper()
    return {
        "ACTIVE": "사용 중",
        "DISABLED": "사용 중지",
        "EXPIRED": "기간 종료",
        "SCHEDULED": "시작 전",
    }.get(status, status)


def _render_common_learning_manager(repository: LearningRepository) -> None:
    """Only the rows an operator added as common Learnings.

    The list is keyed on ``metadata_json.common_learning``, so the rows whose
    POLICY scope the lexical profiler wrote are not here -- nobody approved
    those for cross-model use.
    """

    st.subheader("공통 Learning 관리")
    st.caption(
        "운영자가 승인한 Learning 에서 직접 추가한 공통 항목만 표시합니다. "
        "어휘 분류로 POLICY 가 된 Learning 은 공통 승인이 아니므로 제외됩니다."
    )
    rows = repository.common_learnings()
    if not rows:
        st.info(
            "등록된 공통 Learning 이 없습니다. Positive Learning 상세의 "
            "'공통 Learning 추가'에서 등록할 수 있습니다."
        )
        return

    filters = st.columns([2.4, 1.2, 1.2], gap="small")
    query = filters[0].text_input(
        "공통 Learning 검색",
        placeholder="질문, 답변, 원본 Learning ID 검색",
        key="common_learning_query",
    )
    status_options = ["ALL", "사용 중", "사용 중지", "기간 종료", "시작 전"]
    selected_status = filters[1].selectbox(
        "상태", status_options,
        format_func=lambda value: "전체" if value == "ALL" else value,
        key="common_learning_status",
    )
    market_options = ["ALL", *MARKET_APPLICABILITY_LABELS]
    selected_market = filters[2].selectbox(
        "적용 마켓", market_options,
        format_func=lambda value: (
            "전체" if value == "ALL" else MARKET_APPLICABILITY_LABELS[value]
        ),
        key="common_learning_market",
    )

    needle = str(query or "").strip().lower()
    visible: list[dict[str, Any]] = []
    for row in rows:
        metadata = _metadata(row)
        status = _common_status(row)
        market = str(metadata.get("market_applicability") or "").upper()
        if selected_status != "ALL" and status != selected_status:
            continue
        if selected_market != "ALL" and market != selected_market:
            continue
        if needle and not manager_search_matches(needle, _search_blob(row)):
            continue
        visible.append(row)
    if not visible:
        st.info("조건에 맞는 공통 Learning 이 없습니다.")
        return

    st.dataframe(
        [
            {
                "공통 ID": row["id"],
                "질문 요약": str(
                    row.get("question_original_masked") or ""
                )[:60],
                "답변": str(row.get("final_answer") or "")[:80],
                "적용 모델": APPLICATION_SCOPE_LABELS.get(
                    str(_metadata(row).get("product_scope") or "").upper(), "-"
                ),
                "적용 마켓": MARKET_APPLICABILITY_LABELS.get(
                    str(_metadata(row).get("market_applicability") or "").upper(),
                    "-",
                ),
                "유효기간": validity_summary(row),
                "상태": _common_status(row),
                "원본 ID": _metadata(row).get("common_learning_origin_id") or "-",
                "승인자": _metadata(row).get("common_learning_approved_by") or "-",
                "승인 시각": format_datetime_minute_kst(
                    _metadata(row).get("common_learning_approved_at"), empty="-"
                ),
            }
            for row in visible
        ],
        width="stretch",
        hide_index=True,
    )

    by_id = {int(row["id"]): row for row in visible}
    selected_id = st.selectbox(
        "상세 관리 항목",
        list(by_id),
        format_func=lambda value: "공통 %s · %s" % (
            value, str(by_id[value].get("question_original_masked") or "")[:40]
        ),
        key="common_learning_selected",
    )
    row = by_id[int(selected_id)]
    metadata = _metadata(row)
    learning_id = int(row["id"])
    st.caption(
        "원본 Learning ID %s · 상태 %s · %s" % (
            metadata.get("common_learning_origin_id") or "-",
            _common_status(row), validity_summary(row),
        )
    )

    with st.form(f"common_learning_text_{learning_id}"):
        question = st.text_area(
            "질문", value=str(row.get("question_original_masked") or "")
        )
        answer = st.text_area(
            "답변", value=str(row.get("final_answer") or ""), height=150
        )
        saved = st.form_submit_button("질문·답변 저장", type="primary")
    if saved:
        try:
            repository.update_common_learning_text(
                learning_id, question=question, answer=answer,
                expected_updated_at=str(row.get("updated_at") or ""),
            )
        except (ValueError, LookupError, RuntimeError) as error:
            st.error(str(error))
        else:
            st.toast("공통 Learning 질문·답변을 저장했습니다.")
            st.session_state["current_page"] = "learning"
            st.rerun()

    # The same two editors the Positive detail uses, so scope, market and
    # period behave here exactly as they do there.
    _render_scope_editor(row, repository, key_prefix="common_learning")
    _render_validity_editor(row, repository, key_prefix="common_learning")

    if bool(row.get("validity_active", True)):
        if st.button(
            "사용 중지",
            key=f"common_learning_disable_{learning_id}",
            help="삭제하지 않고 다음 답변 생성부터 검색 후보에서 제외합니다.",
        ):
            _set_common_active(repository, row, active=False)
    else:
        st.caption(
            "사용 중지된 항목입니다. 다시 사용하면 현재 설정된 마켓·기간 조건이 "
            "그대로 적용됩니다."
        )
        if st.button("다시 사용", key=f"common_learning_enable_{learning_id}"):
            _set_common_active(repository, row, active=True)


def _set_common_active(
    repository: LearningRepository, row: dict[str, Any], *, active: bool
) -> None:
    """Stop or resume a common Learning through the existing validity switch."""

    try:
        repository.update_validity(
            int(row["id"]),
            validity_type=str(row.get("validity_type") or "PERMANENT"),
            event_name=row.get("event_name"),
            valid_from=row.get("valid_from"),
            valid_until=row.get("valid_until"),
            validity_active=active,
            validity_note=row.get("validity_note"),
            condition=row.get("condition_json") or {},
            expected_updated_at=str(row.get("updated_at") or ""),
        )
    except (ValueError, LookupError, RuntimeError) as error:
        st.error(str(error))
    else:
        st.toast(
            "공통 Learning 을 다시 사용합니다." if active
            else "공통 Learning 을 사용 중지했습니다. 이력은 보존됩니다."
        )
        st.session_state["current_page"] = "learning"
        st.rerun()


def render_learning_manager(database: Database | None) -> None:
    st.session_state["current_page"] = "learning"
    # The admin-mode toggle lives on Dashboard and Streamlit removes widget
    # state when that widget is not rendered. Keep the authorized manager
    # route stable for all manager-local reruns.
    st.session_state["production_admin_mode"] = True
    st.title("Learning Manager")
    st.caption(
        "Positive Learning과 Negative/의도 교정/학습 제외 상태를 실제 문의 접수시간 "
        "기준으로 조회하고 추적하는 운영 화면입니다."
    )
    st.markdown(
        '<div class="learning-state-badges">'
        + "".join(
            f'<span class="learning-state-badge {tone}">{label}</span>'
            for label, tone in LEARNING_STATE_BADGES
        )
        + "</div>",
        unsafe_allow_html=True,
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

    positive_options = repository.manager_filter_options()
    feedback_options = feedback_repository.manager_filter_options()
    source_options = sorted(
        {*positive_options["sources"], *feedback_options["sources"]}
    )
    provenance_options = sorted(
        {*positive_options["provenance"], *feedback_options["provenance"]}
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

    positive = repository.manager_page(
        query=query,
        source=selected_source,
        provenance=selected_provenance,
        human_verified=selected_verified,
        validity_type=selected_validity,
        validity_state=selected_validity_state,
        page=int(st.session_state.get("learning_manager_positive_page", 1)),
        page_size=int(page_size),
    )
    st.session_state["learning_manager_positive_page"] = positive.page
    st.subheader("Positive Learning")
    st.caption(
        "Positive 자동/승인 이력과 현재 effective lifecycle을 분리해 표시합니다. "
        "soft revoke 이력은 삭제하지 않습니다."
    )
    if not positive.rows:
        st.info("조건에 맞는 Positive Learning이 없습니다.")
    else:
        _render_section(
            positive,
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
    feedback = feedback_repository.manager_page(
        query=query,
        source=selected_source,
        provenance=selected_provenance,
        human_verified=selected_verified,
        signal_type=selected_signal,
        page=int(st.session_state.get("learning_manager_feedback_page", 1)),
        page_size=int(page_size),
    )
    st.session_state["learning_manager_feedback_page"] = feedback.page
    st.subheader("Negative / 의도 교정 / 학습 제외")
    st.caption("평가와 soft revoke 이력을 실제 문의시각 최신순으로 표시합니다.")
    if not feedback.rows:
        st.info("조건에 맞는 Feedback이 없습니다.")
    else:
        _render_section(
            feedback,
            key_prefix="learning_manager_feedback",
        )

    _render_common_learning_manager(repository)
    _render_structured_signal_manager(database)
    _render_excel_export()
