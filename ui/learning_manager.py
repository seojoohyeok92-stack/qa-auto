from __future__ import annotations

from typing import Any

import streamlit as st

from core.time_utils import format_datetime_kst
from repositories.database import Database
from repositories.learning_feedback_repository import LearningFeedbackRepository
from repositories.learning_repository import LearningRepository


def _search_blob(row: dict[str, Any]) -> str:
    metadata = row.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    return " ".join(
        str(value or "")
        for value in (
            row.get("id"),
            row.get("source_key"),
            row.get("inquiry_id"),
            row.get("answer_draft_id"),
            row.get("original_answer_reference_id"),
            row.get("question_original_masked"),
            row.get("question_masked"),
            row.get("final_answer"),
            row.get("original_answer_masked"),
            row.get("learning_source"),
            row.get("source"),
            row.get("provenance"),
            row.get("original_answer_source"),
            row.get("signal_type"),
            row.get("learning_signal_type"),
            metadata.get("answer_provenance"),
            metadata.get("answer_reference_id"),
            metadata.get("verified_by"),
            metadata.get("positive_reason"),
            metadata.get("positive_note"),
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
) -> list[dict[str, Any]]:
    needle = str(query or "").strip().lower()
    result: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.get("metadata_json")
        metadata = metadata if isinstance(metadata, dict) else {}
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
            or "POSITIVE"
        )
        verified = bool(row.get("human_verified") or metadata.get("human_verified"))
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
        result.append(row)
    return result


def render_learning_manager(database: Database | None) -> None:
    st.title("Learning Manager")
    st.caption(
        "현재 저장된 Positive Learning과 Negative/Intent 피드백의 상태를 "
        "조회하고, Dashboard 승인 건을 문의 ID와 provenance로 추적하는 화면입니다."
    )
    if database is None:
        st.warning("Learning Repository DB를 사용할 수 없습니다.")
        return
    repository = LearningRepository(database)
    feedback_repository = LearningFeedbackRepository(database)
    summary = repository.manager_summary()
    feedback_summary = feedback_repository.manager_summary()
    metrics = st.columns(5, gap="small")
    metric_values = (
        (
            "저장된 Positive",
            summary["total"],
            "learning_examples에 저장된 전체 Positive/legacy 호환 레코드 수",
        ),
        (
            "활성 Positive",
            summary["positive_active"],
            "현재 답변 검색 후보로 사용할 수 있는 Positive 수",
        ),
        (
            "Human Verified",
            summary["human_verified"],
            "직원이 직접 검증한 NAVER_POSTED 등 고신뢰 사례",
        ),
        (
            "Negative",
            feedback_summary.get("NEGATIVE", 0),
            "좋은 답변 예제가 아니라 피해야 할 판단/표현으로 저장된 신호",
        ),
        (
            "Intent Correction",
            feedback_summary.get("INTENT_CORRECTION", 0),
            "직원이 문의 유형 또는 route를 교정한 신호",
        ),
    )
    for column, (label, value, help_text) in zip(metrics, metric_values):
        column.metric(label, value, help=help_text)
    st.caption(
        "최근 Learning 생성: "
        + format_datetime_kst(summary.get("recent"), empty="없음")
        + " · Negative/Intent는 Positive 검색 후보에 포함되지 않습니다."
    )

    positive_rows = repository.manager_rows()
    feedback_rows = feedback_repository.manager_rows()
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
                or "UNKNOWN"
            )
            for row in all_rows
        }
    )
    filters = st.columns([2.2, 1.2, 1.2, 1.0], gap="small")
    query = filters[0].text_input(
        "문의/참조 검색",
        placeholder="문의 ID, 문의 원문, Learning reference 검색",
        key="learning_manager_query",
    )
    selected_source = filters[1].selectbox(
        "Learning source",
        ["ALL", *source_options],
        key="learning_manager_source",
    )
    selected_provenance = filters[2].selectbox(
        "Provenance",
        ["ALL", *provenance_options],
        key="learning_manager_provenance",
    )
    selected_verified = filters[3].selectbox(
        "Human verified",
        ["ALL", "YES", "NO"],
        key="learning_manager_verified",
    )

    positive = _filter_rows(
        positive_rows,
        query=query,
        source=selected_source,
        provenance=selected_provenance,
        human_verified=selected_verified,
        signal_type="POSITIVE",
    )
    st.subheader("Positive Learning")
    st.caption(
        "답변 생성/RAG의 Positive 후보입니다. Dashboard 승인 건은 문의 ID와 "
        "Learning ID로 검색할 수 있습니다."
    )
    if not positive:
        st.info("조건에 맞는 Positive Learning이 없습니다.")
    else:
        st.dataframe(
            [
                {
                    "Learning ID": row["id"],
                    "문의 ID": row.get("inquiry_id"),
                    "Draft ID": row.get("answer_draft_id"),
                    "질문": row["question_original_masked"],
                    "학습 답변": row["final_answer"],
                    "Source": row["learning_source"],
                    "Provenance": row.get("provenance") or "UNKNOWN",
                    "Answer Reference": (row.get("metadata_json") or {}).get(
                        "answer_reference_id"
                    )
                    or row.get("answer_draft_id"),
                    "Signal": row.get("signal_type") or "POSITIVE",
                    "Human Verified": "YES" if row.get("human_verified") else "NO",
                    "Positive Reason": (row.get("metadata_json") or {}).get(
                        "positive_reason"
                    )
                    or "-",
                    "Positive Note": (row.get("metadata_json") or {}).get(
                        "positive_note"
                    )
                    or "-",
                    "Verified At": format_datetime_kst(
                        (row.get("metadata_json") or {}).get("verified_at"),
                        empty="-",
                    ),
                    "품질": row["quality_score"],
                    "사용": row["usage_count"],
                    "마지막 사용": format_datetime_kst(
                        row.get("last_used_at"), empty="없음"
                    ),
                    "상태": "활성" if row["active"] else "비활성",
                    "생성/승격": format_datetime_kst(row.get("created_at")),
                }
                for row in positive
            ],
            width="stretch",
            hide_index=True,
        )

    signal_options = ["ALL", "NEGATIVE", "INTENT_CORRECTION", "EXCLUDED"]
    selected_signal = st.selectbox(
        "Feedback signal",
        signal_options,
        key="learning_manager_signal",
    )
    feedback = _filter_rows(
        feedback_rows,
        query=query,
        source=selected_source,
        provenance=selected_provenance,
        human_verified=selected_verified,
        signal_type=selected_signal,
    )
    st.subheader("Negative / Intent Feedback")
    st.caption(
        "이 표의 답변은 Positive 예제가 아니며, 잘못된 이유와 intent 교정 추적에만 사용됩니다."
    )
    if not feedback:
        st.info("조건에 맞는 Negative/Intent 피드백이 없습니다.")
    else:
        st.dataframe(
            [
                {
                    "Feedback ID": row["id"],
                    "문의 ID": row.get("inquiry_id"),
                    "원본 Reference": row.get("original_answer_reference_id"),
                    "질문": row.get("question_masked") or "",
                    "평가 원본": row.get("original_answer_masked") or "",
                    "Source": row.get("source") or "",
                    "Provenance": row.get("original_answer_source") or "UNKNOWN",
                    "Signal": row.get("learning_signal_type") or "",
                    "사유": row.get("correction_reason") or "",
                    "올바른 Intent": row.get("corrected_intent") or "",
                    "메모": row.get("correction_note") or "",
                    "상태": "활성" if row.get("active") else "비활성",
                    "생성": format_datetime_kst(row.get("created_at")),
                }
                for row in feedback
            ],
            width="stretch",
            hide_index=True,
        )
