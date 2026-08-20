from __future__ import annotations

from typing import Any


LEARNING_STATUS_LABELS = {
    "APPROVED": "승인",
    "AUTO": "자동",
    "EXCLUDED": "제외",
    "CORRECTED": "교정",
    "NONE": "-",
}


def resolve_learning_lifecycle(flags: dict[str, Any]) -> dict[str, Any]:
    """Resolve the current inquiry lifecycle without promoting old signals."""

    approved = bool(flags.get("has_approved"))
    automatic = bool(flags.get("has_auto"))
    excluded = bool(flags.get("has_excluded"))
    corrected = bool(flags.get("has_corrected"))
    if approved:
        primary = "APPROVED"
    elif automatic:
        primary = "AUTO"
    elif excluded:
        primary = "EXCLUDED"
    elif corrected:
        primary = "CORRECTED"
    else:
        primary = "NONE"
    statuses = [primary]
    if corrected and primary not in {"CORRECTED", "NONE"}:
        statuses.append("CORRECTED")
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
