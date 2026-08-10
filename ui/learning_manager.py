from __future__ import annotations

import streamlit as st

from core.time_utils import format_datetime_kst
from repositories.database import Database
from repositories.learning_repository import LearningRepository


def render_learning_manager(database: Database | None) -> None:
    st.title("Learning Manager")
    if database is None:
        st.warning("Learning Repository DB를 사용할 수 없습니다.")
        return
    repository = LearningRepository(database)
    summary = repository.manager_summary()
    sources = summary["sources"]
    metrics = st.columns(9, gap="small")
    values = (
        ("총 레코드", summary["total"]),
        ("직원 수정", sources.get("APPROVED_EDITED", 0)),
        ("자동등록 후 수정", sources.get("AUTO_POST_CORRECTED", 0)),
        ("무수정 수동 확인", max(0, sources.get("AUTO_POST_REVIEWED_NO_CHANGE", 0) - summary["automatic_positive"])),
        ("무수정 자동 Positive", summary["automatic_positive"]),
        ("Legacy", sources.get("SELLER_ANSWER", 0)),
        ("검색 사용", summary["searches"]),
        ("활성", summary["active"]),
        ("비활성", summary["inactive"]),
    )
    for column, (label, value) in zip(metrics, values):
        column.metric(label, value)
    st.caption(
        "최근 학습: " + format_datetime_kst(summary.get("recent"), empty="없음")
    )
    rows = repository.manager_rows()
    if not rows:
        st.info("저장된 Learning 레코드가 없습니다.")
        return
    st.dataframe(
        [
            {
                "ID": row["id"],
                "마스킹 문의": row["question_original_masked"],
                "최초 AI 답변": row.get("gpt_draft") or "",
                "직원 수정본": row.get("edited_answer") or "",
                "최종 네이버 반영본": row["final_answer"],
                "Learning source": row["learning_source"],
                "품질 점수": row["quality_score"],
                "사용 횟수": row["usage_count"],
                "마지막 사용": format_datetime_kst(row.get("last_used_at"), empty="없음"),
                "상태": "활성" if row["active"] else "비활성",
            }
            for row in rows
        ],
        width="stretch",
        hide_index=True,
    )
