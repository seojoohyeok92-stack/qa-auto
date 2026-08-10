from __future__ import annotations

from services.inquiry_sync_orchestrator import (
    InquirySyncOrchestrator,
    InquirySyncRunResult,
)


UatSyncResult = InquirySyncRunResult


class UatInquirySyncService(InquirySyncOrchestrator):
    """기존 UAT import 경로를 유지하는 호환 wrapper."""
