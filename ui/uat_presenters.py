from __future__ import annotations

from typing import Any


def answer_source_label(
    draft: dict[str, Any] | None,
    provider_run: dict[str, Any] | None = None,
) -> str:
    draft = draft or {}
    metadata = (
        draft.get("metadata_json")
        if isinstance(draft.get("metadata_json"), dict)
        else {}
    )
    governance = (
        metadata.get("governance")
        if isinstance(metadata.get("governance"), dict)
        else {}
    )
    hybrid = (
        metadata.get("hybrid")
        if isinstance(metadata.get("hybrid"), dict)
        else {}
    )
    run = provider_run or {}
    mode = str(governance.get("mode") or run.get("mode") or "").upper()
    provider = str(
        governance.get("provider") or run.get("provider") or draft.get("provider") or ""
    ).lower()
    fallback = bool(
        governance.get("fallback_reason")
        or hybrid.get("fallback_used")
        or run.get("fallback_used")
    )
    if fallback:
        return "RULE_FALLBACK"
    if provider in {"rules", "rule", "rule_provider"}:
        return "RULE"
    if provider in {"fake", "fake_gpt", "fake_gpt_hybrid"}:
        return "FAKE_PROVIDER"
    if provider == "openai":
        return {
            "SHADOW": "OPENAI_SHADOW",
            "CANARY": "OPENAI_CANARY",
            "ACTIVE": "OPENAI_ACTIVE",
        }.get(mode, "OPENAI_ACTIVE")
    return "RULE" if not provider else provider.upper()


def external_ai_called(
    draft: dict[str, Any] | None,
    provider_run: dict[str, Any] | None = None,
) -> bool:
    return answer_source_label(draft, provider_run).startswith("OPENAI_")

