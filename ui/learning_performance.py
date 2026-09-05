from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from answer.learning_signal import SIGNAL_KIND_LABELS
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.feedback_signal_provenance_repository import (
    FeedbackSignalProvenanceRepository,
)
from repositories.learning_provenance_repository import LearningProvenanceRepository
from repositories.learning_signal_repository import LearningSignalRepository
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
SYSTEM_VERIFIED_LABELS = {
    "CONFIRMED": "시스템 확인됨",
    "UNCONFIRMED": "시스템 미확인",
    "NOT_EVALUATED": "평가 전",
}
CORRECTION_LABELS = {
    "INQUIRY_CLASSIFICATION_CORRECTION": "문의 유형 교정",
    "REQUIRED_ACTION_CORRECTION": "필요한 조회/작업 교정",
    "ANSWER_POLICY_CORRECTION": "답변 정책 교정",
    "RESPONSE_CORRECTION": "답변 내용 교정",
}


VERDICT_LABELS = {
    "SUPPORTED": "근거 확인됨",
    "PARTIALLY_SUPPORTED": "일부만 해당",
    "NOT_SUPPORTED": "질문에 답하지 않음",
    "CONTEXT_INCOMPATIBLE": "상황이 다름",
}


def _verdict_by_reference(metadata: dict) -> dict[int, str]:
    """Which stored answers the verifier accepted for this draft, by row id.

    Verdicts are keyed by the candidate id the verifier was given, which is a
    ``learning_example_id`` for one kind of row and a ``historical_case_id``
    for the other. Those come from different tables and can collide, and a
    verdict shown against the wrong source is worse than no verdict at all --
    so a colliding id is dropped rather than guessed.
    """

    payload = metadata.get("evidence_verification")
    if not isinstance(payload, dict):
        return {}
    verdicts: dict[int, str] = {}
    collisions: set[int] = set()
    for item in payload.get("verified") or ():
        if not isinstance(item, dict) or item.get("candidate_id") is None:
            continue
        try:
            identifier = int(item["candidate_id"])
        except (TypeError, ValueError):
            continue
        verdict = str(item.get("verdict") or "")
        if identifier in verdicts and verdicts[identifier] != verdict:
            collisions.add(identifier)
        verdicts[identifier] = verdict
    for identifier in collisions:
        verdicts.pop(identifier, None)
    return verdicts


ANSWER_PREVIEW_LIMIT = 60


def _answer_preview(answer: str | None) -> str:
    """One scannable line per source; the full text lives in the expander below.

    Stored answers are Korean support replies that open with the same greeting
    and run several lines, so a raw cell makes the table unreadable and hides
    the columns beside it. Newlines collapse to spaces because a dataframe cell
    renders them as one run anyway, and a cut is marked with a trailing
    ellipsis so nobody mistakes a truncated answer for a short one.
    """

    text = " ".join(str(answer or "").split())
    if not text:
        return "-"
    if len(text) <= ANSWER_PREVIEW_LIMIT:
        return text
    return text[:ANSWER_PREVIEW_LIMIT] + "…"


def _percent(value: float | None) -> str:
    return "측정 데이터 부족" if value is None else f"{value:.1f}%"


def _sample_caption(sample: int) -> str:
    if not sample:
        return "판정 완료 표본 없음"
    return f"표본 {sample}건" + (" · 참고용" if sample < 10 else "")


def _metric_delta(
    current: float | None,
    previous: float | None,
    *,
    higher_is_better: bool,
) -> tuple[str | None, str]:
    if current is None or previous is None:
        return "이전 기간 데이터 부족", "off"
    difference = round(current - previous, 1)
    if difference == 0:
        return "변화 없음", "off"
    improved = difference > 0 if higher_is_better else difference < 0
    label = "개선" if improved else "악화"
    return f"{difference:+.1f}%p · {label}", (
        "normal" if higher_is_better else "inverse"
    )


def _correction_summary(
    current: float | None, previous: float | None
) -> str:
    if current is None:
        return "현재 측정 데이터 부족"
    if previous is None:
        return f"현재 {current:.1f}% · 이전 기간 측정 데이터 부족"
    difference = round(current - previous, 1)
    if difference == 0:
        comparison = "변화 없음"
    else:
        comparison = f"{abs(difference):.1f}%p " + (
            "개선" if difference < 0 else "악화"
        )
    return f"현재 {current:.1f}% · 이전 기간 {previous:.1f}% · {comparison}"


def render_learning_performance(database: Database) -> None:
    period = st.session_state.setdefault("learning_performance_period", "최근 30일")
    st.markdown('<div class="learning-performance-anchor"></div>', unsafe_allow_html=True)
    header, selector = st.columns([5, 1.2], vertical_alignment="bottom")
    header.markdown("### Learning 성과 · 답변 품질")
    period = selector.selectbox(
        "품질 기간", ["최근 7일", "최근 30일", "최근 90일"],
        key="learning_performance_period", label_visibility="collapsed",
    )
    period_days = {"최근 7일": 7, "최근 30일": 30, "최근 90일": 90}[period]
    data = LearningPerformanceService(database).snapshot(
        period_days=period_days
    )
    current = data["quality"]["current"]
    previous = data["quality"]["previous"]
    card_specs = (
        ("자동 등록률", "auto_post_rate", True),
        ("직원 수정률", "correction_rate", False),
        ("직원 검토 필요율", "review_required_rate", False),
    )
    cards = st.columns(3, gap="small")
    for card, (label, key, higher_is_better) in zip(cards, card_specs):
        delta, delta_color = _metric_delta(
            current[key], previous[key],
            higher_is_better=higher_is_better,
        )
        card.metric(
            label,
            _percent(current[key]),
            delta,
            delta_color=delta_color,
        )

    st.markdown("#### 직원 수정률 추이")
    correction_trend = [
        row for row in data["quality"]["correction_trend"]
        if row["correction_rate"] is not None
    ]
    if len(correction_trend) >= 2:
        st.line_chart(
            pd.DataFrame(correction_trend).set_index("period")[["correction_rate"]],
            y_label="직원 수정률(%)",
        )
        st.caption(
            _correction_summary(
                current["correction_rate"], previous["correction_rate"]
            )
        )
    else:
        st.info("직원 수정률 추이를 측정할 수 있는 데이터가 아직 부족합니다.")

    with st.expander("상세 분석", expanded=False):
        st.markdown("#### 기간 비교")
        st.caption(
            "자동 답변 생성률은 진단용 상세 지표이며, 메인 운영 KPI에는 표시하지 않습니다."
        )
        st.dataframe([
            {
                "기간": label,
                "자동 답변 생성률": _percent(data["quality"]["current"]["generation_rate"])
                if key == "current_30" else _percent(data["quality"]["previous"]["generation_rate"])
                if key == "previous_30" else "-",
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
        st.markdown("#### Positive Learning 수동 판단 현황")
        st.caption(
            f"기존 관찰기간 기록: {positive['observation_days']}일 · "
            "관찰기간이 지나도 자동 전환하지 않으며 관리자 명시 판단만 반영합니다."
        )
        positive_columns = st.columns(4, gap="small")
        for column, (label, value) in zip(positive_columns, (
            ("관찰 이력", positive["observing"]),
            ("판정 시점 도달 · 자동전환 없음", positive["due"]),
            ("과거 자동 Learning 이력", positive["converted"]),
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
    historical_usage = generated.get("historical_usage")
    historical_usage = historical_usage if isinstance(historical_usage, list) else []
    supported_historical_ids = {
        int(item["historical_case_id"])
        for item in historical_usage
        if isinstance(item, dict)
        and item.get("historical_case_id") is not None
        and item.get("answer_supported")
    }
    usage_reasons = {
        ("HISTORICAL", int(item["historical_case_id"])): str(
            item.get("reason") or "Provider 미선택"
        )
        for item in historical_usage
        if isinstance(item, dict) and item.get("historical_case_id") is not None
    }
    usage_reasons.update({
        ("LEARNING", int(item["learning_id"])): str(
            item.get("reason") or "Provider 미선택"
        )
        for item in usage
        if isinstance(item, dict) and item.get("learning_id") is not None
    })
    signal_rows = FeedbackSignalProvenanceRepository(database).for_draft(
        int(draft_id)
    )
    persisted_used = sum(
        1 for row in rows if str(row.get("usage_status") or "") == "USED"
    )
    used_count = persisted_used or len(supported_ids) + len(supported_historical_ids)
    signal_note = (
        f" · Feedback Signal {len(signal_rows)}건" if signal_rows else ""
    )
    st.caption(
        (
            f"Learning 참고: 선택 {len(rows)}건 · 답변 근거 사용 "
            f"{used_count}건{signal_note}"
        )
        if rows or signal_rows
        else "Learning 참고: 없음"
    )
    if not rows and not signal_rows:
        return
    signals_repo = LearningSignalRepository(database)
    with st.expander("이번 답변에 사용된 Learning", expanded=False):
        if not rows and not signal_rows:
            st.info("이 답변에는 기록된 Learning/Historical Context가 없습니다.")
            return
        result_labels = {
            "USED": "사용",
            "NOT_USED": "미사용",
            "REJECTED_CONFLICT": "현재 사실과 충돌",
            "REJECTED_LOW_CONFIDENCE": "신뢰도 부족",
            "BLOCKED_BY_CURRENT_FACT": "현재 주문정보 우선",
            "NOT_APPLICABLE": "사실 근거 아님(스타일 안내)",
            "PENDING": "기존 기록 미평가",
        }
        display = []
        full_texts: list[dict[str, Any]] = []
        verdicts = _verdict_by_reference(metadata)
        for row in rows:
            is_historical = row["reference_kind"] == "HISTORICAL"
            reference_id = (
                int(row["historical_case_id"])
                if is_historical
                else int(row["learning_example_id"])
            )
            persisted_status = str(row.get("usage_status") or "PENDING")
            used = persisted_status == "USED" or (
                persisted_status == "PENDING"
                and (
                    reference_id in supported_historical_ids
                    if is_historical
                    else reference_id in supported_ids
                )
            )
            # Prefer the original-platform inquiry number (what an operator
            # actually recognizes a source by) over the internal PK; many
            # Learning rows have no order number to fall back to instead.
            # The internal id is preserved in "내부 ID" for DB traceability.
            if is_historical:
                internal_label = f"Historical Case #{row['historical_case_id']}"
                external_number = (
                    row.get("historical_source_question_id")
                    or row.get("historical_external_inquiry_id")
                )
                question_snippet = str(row.get("historical_question") or "")
                # What the prompt reads for a Historical case; see
                # learning_context_service's ``answer_reference``.
                stored_answer = str(row.get("historical_answer") or "")
                product_name = row.get("historical_product_name") or "-"
                attached_signals = signals_repo.for_historical_case(reference_id)
            else:
                metadata = {}
                try:
                    metadata = json.loads(row.get("learning_metadata") or "{}")
                except (TypeError, json.JSONDecodeError):
                    pass
                source = metadata.get("source_origin") or row.get("learning_source") or row["source_label"]
                internal_label = (
                    f"{SOURCE_LABELS.get(str(source), str(source))} "
                    f"#{row['learning_example_id']}"
                )
                external_number = (
                    row.get("learning_source_question_id")
                    or row.get("learning_external_inquiry_id")
                )
                question_snippet = str(row.get("learning_question") or "")
                # What the prompt reads for a Learning row; see
                # learning_context_service's candidate ``final_answer``.
                stored_answer = str(row.get("learning_answer") or "")
                product_name = row.get("learning_product_name") or "-"
                attached_signals = signals_repo.for_learning_example(reference_id)
            reference_label = (
                f"네이버 문의 #{external_number}" if external_number else internal_label
            )
            if question_snippet.strip():
                snippet = question_snippet.strip().replace("\n", " ")[:40]
                reference_label = f"{reference_label} · {snippet}"
            feedback_signal_summary = (
                "; ".join(
                    f"{SIGNAL_KIND_LABELS.get(item['signal_kind'], item['signal_kind'])}"
                    for item in attached_signals
                )
                or "-"
            )
            full_texts.append({
                "유형": "Historical" if is_historical else "Learning",
                "참고 자료": reference_label,
                "상품명": product_name,
                "문의": question_snippet.strip(),
                "기존 답변": stored_answer.strip(),
                "사용": used,
            })
            display.append({
                "유형": "Historical" if is_historical else "Learning",
                "참고 자료": reference_label,
                "기존 답변": _answer_preview(stored_answer),
                "근거 검증": VERDICT_LABELS.get(
                    verdicts.get(reference_id, ""), "검증 안 함"
                ),
                "상품명": product_name,
                "내부 ID": internal_label,
                "유사도": round(float(row.get("relevance") or 0), 2),
                "Answer-Support": round(
                    float(row.get("answer_support_score") or 0), 2
                ),
                "Feedback Signal": feedback_signal_summary,
                "답변 근거 사용": (
                    "사용"
                    if used
                    else "미사용"
                ),
                "Provider 자기보고": (
                    "사용 보고"
                    if row.get("provider_claimed_usage")
                    else "미보고"
                ),
                "System 검증": SYSTEM_VERIFIED_LABELS.get(
                    str(row.get("system_verified_usage") or "NOT_EVALUATED"),
                    "평가 전",
                ),
                "결과": (
                    result_labels.get(persisted_status, "미사용")
                    + (
                        ""
                        if used
                        else " - " + str(
                            row.get("usage_reason")
                            or usage_reasons.get(
                                (str(row["reference_kind"]), reference_id),
                                outcome or "Provider 미선택",
                            )
                        )
                    )
                ),
            })
        for row in signal_rows:
            signal_id = int(row["learning_signal_id"])
            internal_label = f"Feedback Signal #{signal_id}"
            external_number = (
                row.get("signal_source_question_id")
                or row.get("signal_external_inquiry_id")
                or row.get("historical_external_inquiry_id")
            )
            reference_label = (
                f"네이버 문의 #{external_number}" if external_number else internal_label
            )
            question_snippet = str(
                row.get("question_masked") or row.get("historical_question") or ""
            ).strip()
            if question_snippet:
                reference_label = (
                    f"{reference_label} · {question_snippet.replace(chr(10), ' ')[:40]}"
                )
            product_identity = {}
            try:
                product_identity = json.loads(
                    row.get("product_identity_json") or "{}"
                )
            except (TypeError, json.JSONDecodeError):
                pass
            product_name = (
                product_identity.get("product_name")
                or row.get("historical_product_name")
                or "-"
            )
            persisted_status = str(row.get("usage_status") or "PENDING")
            display.append({
                "유형": "Feedback Signal",
                "참고 자료": reference_label,
                # A feedback signal carries a correction note, not a stored
                # answer of its own; the column exists to keep one table shape.
                "기존 답변": "-",
                "근거 검증": "-",
                "상품명": product_name,
                "내부 ID": internal_label,
                "유사도": round(float(row.get("relevance") or 0), 2)
                if row.get("relevance") is not None else "-",
                "Answer-Support": round(
                    float(row.get("answer_support_score") or 0), 2
                ) if row.get("answer_support_score") is not None else "-",
                "Feedback Signal": SIGNAL_KIND_LABELS.get(
                    str(row.get("signal_kind") or ""), str(row.get("signal_kind") or "-")
                ) + (" (충돌)" if row.get("conflict_detected") else ""),
                "답변 근거 사용": (
                    "사용" if persisted_status == "USED" else "미사용"
                ),
                "Provider 자기보고": (
                    "사용 보고" if row.get("provider_claimed_usage") else "미보고"
                ),
                "System 검증": SYSTEM_VERIFIED_LABELS.get(
                    str(row.get("system_verified_usage") or "NOT_EVALUATED"),
                    "평가 전",
                ),
                "결과": result_labels.get(persisted_status, "미사용")
                + (
                    ""
                    if persisted_status == "USED"
                    else " - " + str(row.get("usage_reason") or "Provider 미선택")
                ),
            })
        st.dataframe(display, hide_index=True, width="stretch")
        st.caption(
            "실제 답변 생성 Prompt Context에 포함된 자료만 표시합니다. "
            "'참고 자료'는 원본 플랫폼 문의번호를 우선 표시하며, 내부 PK는 "
            "'내부 ID' 열에 그대로 보존됩니다. '기존 답변'은 Prompt에 실제로 "
            "전달된 본문이며, 표에서는 앞부분만 보여줍니다."
        )
        # The preview answers "did retrieval find something plausible"; the full
        # text answers "does it actually settle the question", and only the
        # second one tells an operator whether retrieval or the stored answer is
        # at fault. Never truncated away -- the whole text is one click below.
        for index, item in enumerate(full_texts, 1):
            header = (
                f"{index}. [{item['유형']}] {item['참고 자료']}"
                f"{' · 답변 근거 사용' if item['사용'] else ''}"
            )
            with st.expander(header, expanded=False):
                st.caption(f"상품: {item['상품명']}")
                st.markdown("**당시 문의**")
                st.text(item["문의"] or "(문의 원문이 저장되어 있지 않습니다)")
                st.markdown("**당시 답변**")
                st.text(item["기존 답변"] or "(저장된 답변이 없습니다)")
