from __future__ import annotations

import re
from typing import Any

import streamlit as st

from answer.engine import AnswerEngine
from answer.facts import build_answer_facts
from answer.source_adapter import answer_request_from_inquiry
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.gpt_provider_run_repository import GptProviderRunRepository
from repositories.quality_metric_repository import QualityMetricRepository
from services.local_auth_service import Permission
from services.prompt_privacy_service import PromptPrivacyService
from services.uat_order_service import UatOrderService
from ui.session_identity import can
from ui.uat_presenters import answer_source_label, external_ai_called


HIGH_RISK = re.compile(r"(환불|반품|분쟁|법적|소송|보상|신고)")


def _latest_dps(
    database: Database,
    inquiry_id: int,
    order_id: str,
) -> dict[str, Any]:
    row = DpsRepository(database).get_preferred_for_inquiry_and_order(
        inquiry_id, order_id
    )
    return (
        dict(row.get("normalized_result_json") or {})
        if row and isinstance(row.get("normalized_result_json"), dict)
        else {}
    )


def _order_rows(
    inquiry: dict[str, Any],
    session_result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if session_result and isinstance(session_result.get("orders"), list):
        return list(session_result["orders"])
    raw = inquiry.get("raw_json")
    if not isinstance(raw, dict):
        return []
    orders = raw.get("orders")
    return list(orders) if isinstance(orders, list) else []


def render_inquiry_uat_panel(
    database: Database, inquiry: dict[str, Any]
) -> None:
    inquiry_id = int(inquiry["id"])
    draft = (
        AnswerRepository(database).active_for_inquiry(inquiry_id)
        or AnswerRepository(database).latest_for_inquiry(inquiry_id)
    )
    provider_run = (
        GptProviderRunRepository(database).latest_for_draft(
            int(draft["id"])
        )
        if draft
        else None
    )
    dps = _latest_dps(
        database,
        inquiry_id,
        str(inquiry.get("order_id") or ""),
    )
    privacy = PromptPrivacyService().sanitize(
        {
            "question": inquiry.get("content"),
            "customer_display": inquiry.get("customer_display"),
            "order_id": inquiry.get("order_id"),
            "product_order_id": inquiry.get("product_order_id"),
        }
    )

    with st.expander("문의·주문·Facts UAT 정보", expanded=False):
        cols = st.columns(4)
        cols[0].metric(
            "위험도",
            "HIGH" if HIGH_RISK.search(str(inquiry.get("content") or "")) else "NORMAL",
        )
        cols[1].metric(
            "개인정보",
            "검사 필요"
            if privacy.removed_fields or privacy.masked_patterns
            else "패턴 없음",
        )
        source = answer_source_label(draft, provider_run)
        cols[2].metric("답변 출처", source)
        cols[3].metric(
            "외부 AI",
            "호출 기록 있음"
            if external_ai_called(draft, provider_run)
            else "실제 외부 AI 호출 없음",
        )

        st.markdown("#### 네이버 주문정보")
        order_key = f"uat_order_result_{inquiry_id}"
        order_result = st.session_state.get(order_key)
        lookup = st.button(
            "네이버 주문정보 조회",
            key=f"uat_order_lookup_{inquiry_id}",
            disabled=(
                not can(Permission.INQUIRY_VIEW)
                or (
                    not inquiry.get("order_id")
                    and not inquiry.get("product_order_id")
                )
                or str(inquiry.get("post_status")).upper() == "POSTED"
            ),
        )
        if lookup:
            with st.spinner("네이버 주문 API를 조회하고 있습니다."):
                order_result = UatOrderService(database).lookup_for_inquiry(
                    inquiry_id, force_refresh=True
                )
            st.session_state[order_key] = order_result
        if not inquiry.get("order_id") and inquiry.get("product_order_id"):
            st.caption(
                "상품주문번호는 네이버 주문 조회에만 사용할 수 있습니다. "
                "DPS에는 일반 order_id만 전달됩니다."
            )
        rows = _order_rows(inquiry, order_result)
        if isinstance(order_result, dict) and not order_result.get("success"):
            st.warning(
                str(
                    order_result.get("error_message")
                    or "네이버 주문정보를 확인하지 못했습니다."
                )
            )
        if rows:
            safe_rows = [
                {
                    "일반 주문번호": row.get("order_id"),
                    "상품주문번호": row.get("product_order_id"),
                    "상품명": row.get("product_name"),
                    "옵션": row.get("product_option"),
                    "수량": row.get("quantity"),
                    "주문 상태": row.get("product_order_status"),
                    "발주 상태": row.get("place_order_status"),
                    "결제일": row.get("payment_date"),
                    "주문일": row.get("order_date"),
                    "배송 시작": row.get("shipping_start_date"),
                    "배송 예정": row.get("shipping_due_date"),
                }
                for row in rows
            ]
            st.dataframe(safe_rows, hide_index=True, width="stretch")
        else:
            st.info("표시할 주문 조회 결과가 없습니다.")

        st.markdown("#### Rule Answer와 AnswerFacts")
        try:
            request = answer_request_from_inquiry(inquiry)
            if dps:
                request.metadata["dps"] = dps
            rule = AnswerEngine().generate(request)
            facts = build_answer_facts(request, rule)
            st.text_area(
                "Rule Answer",
                value=rule.answer,
                disabled=True,
                height=120,
                key=f"uat_rule_answer_{inquiry_id}",
            )
            st.json(facts.to_dict(), expanded=False)
        except Exception:
            st.warning(
                "Rule Answer 또는 AnswerFacts를 구성하지 못했습니다. "
                "기존 문의와 초안 데이터는 보존됩니다."
            )

        if draft:
            quality = QualityMetricRepository(database).latest_for_draft(
                int(draft["id"])
            )
            if quality:
                st.markdown("#### 품질 보조지표")
                st.write(
                    {
                        "문자 변경 비율": quality["character_change_ratio"],
                        "단어 변경 비율": quality["word_change_ratio"],
                        "문장 추가": quality["sentences_added"],
                        "문장 삭제": quality["sentences_deleted"],
                        "사실 변경": bool(quality["fact_changed"]),
                        "말투 변경": bool(quality["tone_changed"]),
                        "승인": bool(quality["approved"]),
                    }
                )
