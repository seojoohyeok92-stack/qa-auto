from __future__ import annotations

from typing import Any


LEARNING_STATUS_LABELS = {
    "APPROVED": "승인",
    "AUTO": "자동",
    "EXCLUDED": "제외",
    "CORRECTED": "교정",
    "NONE": "-",
}


def is_explicitly_approved_learning(row: dict[str, Any]) -> bool:
    metadata = row.get("metadata_json")
    metadata = metadata if isinstance(metadata, dict) else {}
    return bool(
        row.get("human_verified")
        or bool(metadata.get("human_verified"))
        or row.get("approval_history_id") is not None
        or (
            str(metadata.get("source_origin") or "").upper()
            == "HISTORICAL_PROMOTED"
            and str(metadata.get("promoted_by") or "").strip()
        )
        or str(row.get("validator_result") or "").upper()
        in {"HUMAN_VERIFIED_NAVER_POSTED", "HISTORICAL_ADMIN_APPROVED"}
    )


def resolve_learning_lifecycle(flags: dict[str, Any]) -> dict[str, Any]:
    """Resolve the current inquiry lifecycle without promoting old signals."""

    approved = bool(flags.get("has_approved"))
    automatic = bool(flags.get("has_auto"))
    excluded = bool(flags.get("has_excluded"))
    corrected = bool(flags.get("has_corrected"))
    supplied = str(flags.get("effective_status") or "").upper()
    if supplied in LEARNING_STATUS_LABELS:
        primary = supplied
    elif excluded:
        primary = "EXCLUDED"
    elif corrected:
        primary = "CORRECTED"
    elif approved:
        primary = "APPROVED"
    elif automatic:
        primary = "AUTO"
    else:
        primary = "NONE"
    # The list badge represents current state, not the entire audit history.
    statuses = [primary]
    labels = [LEARNING_STATUS_LABELS[status] for status in statuses]
    provenance = [
        value
        for value in (
            "Human Verified / explicit approval Positive" if approved else "",
            "Auto/non-human Positive" if automatic else "",
            "Negative/Excluded/soft revoke" if excluded else "",
            "Intent Correction" if corrected else "",
        )
        if value
    ]
    return {
        "learning_status": primary,
        "learning_statuses": statuses,
        "learning_labels": labels,
        "learning_tooltip": " · ".join(provenance) or "Learning 이력 없음",
    }
