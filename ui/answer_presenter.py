from __future__ import annotations

from typing import Any

from core.time_utils import format_datetime_kst

STATUS_LABELS = {
    "GENERATED": "초안 생성 완료",
    "NEEDS_REVIEW": "직원 검토 필요",
    "NOT_SUPPORTED": "자동답변 미지원",
    "FAILED": "생성 실패",
}


def build_answer_display(
    draft: dict[str, Any],
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result_data = result if isinstance(result, dict) else {}
    status = str(
        result_data.get("status")
        or draft.get("program_status")
        or "FAILED"
    )
    auto_answerable = bool(
        result_data.get("auto_answerable", status == "GENERATED")
    )
    needs_review = bool(
        result_data.get("needs_review", status != "GENERATED")
    )
    warnings = result_data.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    return {
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "category": str(draft.get("category") or "확인 필요"),
        "auto_answerable": auto_answerable,
        "needs_review": needs_review,
        "reason": str(draft.get("reason") or "기록 없음"),
        "provider": str(draft.get("provider") or "rules"),
        "answer": str(draft.get("original_answer") or ""),
        "warnings": [str(item) for item in warnings],
        "created_at": format_datetime_kst(
            draft.get("created_at"), empty="확인 불가"
        ),
        "posted": bool(draft.get("posted")),
    }
