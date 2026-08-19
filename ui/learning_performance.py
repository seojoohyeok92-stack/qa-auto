from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.learning_provenance_repository import LearningProvenanceRepository
from services.learning_performance_service import LearningPerformanceService


SOURCE_LABELS = {
    "STAFF_EDITED": "직원 수정",
    "STAFF_POST_CORRECTION": "자동답변 후 직원 수정",
    "NAVER_DIRECT_EDIT": "네이버에서 직접 수정",
    "REVIEWED_NO_CHANGE": "수정 없음 수동 확인",
    "POSITIVE_LEARNING": "자동 Positive Learning",
    "HISTORICAL_PROMOTED": "Historical 승격",
    "HISTORICAL_REFERENCE": "Historical Reference (legacy)",
    "HISTORICAL_VERIFIED_LEARNING": "Historical Verified Learning",
    "COPILOT_CORRECTION": "Copilot 교정",
    "APPROVED_UNEDITED": "승인된 무수정 답변",
    "SELLER_ANSWER": "과거 판매자 답변(문체 참고)",
    "TEMPLATE": "Template",
    "AUTO_POST_CORRECTED": "자동답변 후 직원 수정",
    "AUTO_POST_REVIEWED_NO_CHANGE": "수정 없이 확인된 답변",
    "APPROVED_EDITED": "직원 수정",
}
CORRECTION_LABELS = {
    "INQUIRY_CLASSIFICATION_CORRECTION": "문의 유형 교정",
    "REQUIRED_ACTION_CORRECTION": "필요한 조회/작업 교정",
    "ANSWER_POLICY_CORRECTION": "답변 정책 교정",
    "RESPONSE_CORRECTION": "답변 내용 교정",
}


def _percent(value: float | None) -> str:
    return "측정 데이터 부족" if value is None else f"{value:.1f}%"


def _sample_caption(sample: int) -> str:
    if not sample:
        return "판정 완료 표본 없음"
    return f"표본 {sample}건" + (" · 참고용" if sample < 10 else "")


def render_learning_performance(database: Database) -> None:
    data = LearningPerformanceService(database).snapshot()
    period = st.session_state.setdefault("learning_performance_period", "최근 30일")
    period_key = "current_7" if period == "최근 7일" else "current_30"
    selected = data[period_key]
    delta = data["unchanged_delta_30"] if period_key == "current_30" else None

    st.markdown('<div class="learning-performance-anchor"></div>', unsafe_allow_html=True)
    header, selector = st.columns([5, 1.2], vertical_alignment="bottom")
    header.markdown("### Learning 성과 · 답변 품질")
    selector.selectbox(
        "품질 기간", ["최근 7일", "최근 30일"],
        key="learning_performance_period", label_visibility="collapsed",
    )
    cards = st.columns([1.35, 1.05, 1, 1, 1.15], gap="small")
    cards[0].metric(
        "자동답변 무수정률", _percent(selected["unchanged_rate"]),
        None if delta is None else f"{delta:+.1f}%p · 이전 30일 대비",
    )
    cards[1].metric("직원 수정률", _percent(selected["correction_rate"]))
    cards[2].metric("활성 Learning", data["learning"]["active"])
    cards[3].metric("최근 30일 신규", data["learning"]["new_30"])
    cards[4].metric(
        "Learning 참고 답변", data["provenance"]["generated_with_learning"],
        f"Historical Verified Learning {data['provenance']['generated_with_historical']}건",
    )
    st.caption(
        "무수정률은 관찰 또는 직원 확인이 끝난 자동등록 답변 중 수정 없이 사용된 비율입니다. "
        + _sample_caption(selected["known"])
    )

    with st.expander("Learning 성과 상세", expanded=False):
        st.markdown("#### 기간 비교")
        st.dataframe([
            {
                "기간": label,
                "무수정률": _percent(data[key]["unchanged_rate"]),
                "직원 수정률": _percent(data[key]["correction_rate"]),
                "판정 완료": data[key]["known"],
                "아직 관찰/확인 중": data[key]["pending"],
            }
            for label, key in (
                ("최근 7일", "current_7"),
                ("최근 30일", "current_30"),
                ("이전 30일", "previous_30"),
            )
        ], hide_index=True, width="stretch")

        trend = [row for row in data["trend"] if row["unchanged_rate"] is not None]
        if len(trend) >= 2:
            st.line_chart(
                pd.DataFrame(trend).set_index("period")[["unchanged_rate"]],
                y_label="무수정률(%)",
            )
        else:
            st.info("기간별 추세를 그리기 위한 판정 완료 데이터가 아직 부족합니다.")

        st.markdown("#### Learning 참고 효과")
        used, unused = data["provenance"]["used"], data["provenance"]["not_used"]
        comparison = st.columns(2, gap="medium")
        comparison[0].metric(
            "Learning 참고 답변 무수정률", _percent(used["unchanged_rate"]),
            _sample_caption(used["sample"]),
        )
        comparison[1].metric(
            "Learning 미참고 답변 무수정률", _percent(unused["unchanged_rate"]),
            _sample_caption(unused["sample"]),
        )
        st.caption(
            "Migration 21 이후 실제 생성 Context에 포함된 Learning만 ‘참고’로 집계합니다. "
            "표본이 없으면 효과를 추정하지 않습니다."
        )

        st.markdown("#### 문의 유형별 품질")
        if data["types"]:
            st.dataframe([
                {
                    "문의 유형": row["inquiry_type"],
                    "무수정률": _percent(row["unchanged_rate"]),
                    "표본": row["sample"],
                    "해석": "참고용" if row["sample"] < 10 else "",
                }
                for row in data["types"]
            ], hide_index=True, width="stretch")
        else:
            st.info("문의 유형별 품질을 계산할 판정 완료 데이터가 없습니다.")

        st.markdown("#### Learning 출처별 현황")
        st.dataframe([
            {
                "출처": SOURCE_LABELS.get(row["source_group"], row["source_group"]),
                "활성 사례": row["active_count"],
                "최근 30일 신규": row["new_30"],
                "실제 참고 답변": row["referenced_answers"],
                "참고 후 무수정률": _percent(row["unchanged_rate"]),
            }
            for row in data["sources"]
        ], hide_index=True, width="stretch")

        positive = data["positive"]
        st.markdown("#### Positive Learning 관찰 현황")
        st.caption(f"현재 관찰기간: {positive['observation_days']}일")
        positive_columns = st.columns(4, gap="small")
        for column, (label, value) in zip(positive_columns, (
            ("관찰 중", positive["observing"]),
            ("판정 시점 도달", positive["due"]),
            ("자동 Learning 전환", positive["converted"]),
            ("확인 보류/제외", positive["corrected"] + positive["unknown"] + positive["validator"] + positive["unconfirmed_or_other"]),
        )):
            column.metric(label, value)
        st.caption(
            "확인 보류/제외: 직원·네이버 수정 {corrected} · 전송 불명확 {unknown} · "
            "검증 조건 {validator} · 원격 확인 대기/기타 {other}".format(
                corrected=positive["corrected"], unknown=positive["unknown"],
                validator=positive["validator"], other=positive["unconfirmed_or_other"],
            )
        )

        corrections = data["corrections"]
        st.markdown("#### Copilot 교정 Learning")
        st.dataframe([
            {
                "교정 유형": CORRECTION_LABELS.get(row["correction_type"], row["correction_type"]),
                "전체": row["total"], "최근 30일": row["new_30"],
            }
            for row in corrections["types"]
        ], hide_index=True, width="stretch")
        total_classification = next(
            row["new_30"] for row in corrections["types"]
            if row["correction_type"] == "INQUIRY_CLASSIFICATION_CORRECTION"
        )
        total_action = next(
            row["new_30"] for row in corrections["types"]
            if row["correction_type"] == "REQUIRED_ACTION_CORRECTION"
        )
        denominator = corrections["inquiries_30"]
        if denominator:
            st.caption(
                f"최근 30일 문의 대비 명시적 문의 유형 교정 {total_classification / denominator * 100:.2f}% · "
                f"필요한 조회/작업 교정 {total_action / denominator * 100:.2f}%"
            )
        else:
            st.caption("교정률을 계산할 최근 문의 데이터가 없습니다.")

        with st.expander("기술 정보", expanded=False):
            st.json(data)


def render_answer_learning_provenance(
    database: Database, *, draft_id: int, outcome: str | None = None,
) -> None:
    rows = LearningProvenanceRepository(database).for_draft(int(draft_id))
    draft = AnswerRepository(database).get(int(draft_id)) or {}
    metadata = draft.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    hybrid = metadata.get("hybrid")
    hybrid = hybrid if isinstance(hybrid, dict) else {}
    generated = hybrid.get("draft")
    generated = generated if isinstance(generated, dict) else {}
    usage = generated.get("learning_usage")
    usage = usage if isinstance(usage, list) else []
    supported_ids = {
        int(item["learning_id"])
        for item in usage
        if isinstance(item, dict)
        and item.get("learning_id") is not None
        and item.get("answer_supported")
    }
    st.caption(
        (
            f"Learning 참고: 선택 {len(rows)}건 · 답변 근거 사용 "
            f"{len(supported_ids)}건"
        )
        if rows
        else "Learning 참고: 없음"
    )
    with st.expander("이번 답변에 사용된 Learning", expanded=False):
        if not rows:
            st.info("이 답변에는 기록된 Learning/Historical Context가 없습니다.")
            return
        display = []
        for row in rows:
            if row["reference_kind"] == "HISTORICAL":
                label = f"Historical Case #{row['historical_case_id']}"
            else:
                metadata = {}
                try:
                    metadata = json.loads(row.get("learning_metadata") or "{}")
                except (TypeError, json.JSONDecodeError):
                    pass
                source = metadata.get("source_origin") or row.get("learning_source") or row["source_label"]
                label = f"{SOURCE_LABELS.get(str(source), str(source))} #{row['learning_example_id']}"
            display.append({
                "참고 자료": label,
                "유사도": round(float(row.get("relevance") or 0), 2),
                "답변 근거 사용": (
                    "사용"
                    if row.get("learning_example_id") in supported_ids
                    else "미사용"
                ),
                "결과": outcome or "결과 확인 중",
            })
        st.dataframe(display, hide_index=True, width="stretch")
        st.caption("실제 답변 생성 Prompt Context에 포함된 자료만 표시합니다.")
