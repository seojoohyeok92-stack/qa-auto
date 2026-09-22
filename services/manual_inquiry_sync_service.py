from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from config import COUPANG_OJE_NS, COUPANG_OJE_PLUS
from repositories.database import Database
from services.coupang_inquiry_sync_service import CoupangInquirySyncService
from services.inquiry_sync_orchestrator import InquirySyncOrchestrator


@dataclass(frozen=True)
class ManualSyncPlatformResult:
    key: str
    label: str
    status: str
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ManualInquirySyncService:
    """One operator action over the existing Naver and Coupang read syncs."""

    def __init__(
        self,
        database: Database,
        *,
        naver_factory: Callable[[Database], Any] = InquirySyncOrchestrator,
        coupang_factory: Callable[[Database], Any] = CoupangInquirySyncService,
    ) -> None:
        self.database = database
        self.naver_factory = naver_factory
        self.coupang_factory = coupang_factory

    def run(self, *, stores: Iterable[Any]) -> dict[str, Any]:
        results: list[ManualSyncPlatformResult] = []
        try:
            raw = self.naver_factory(self.database).run(
                stores=list(stores), sync_type="MANUAL"
            )
            value = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
            failed = int(value.get("failed_count") or value.get("failed") or 0)
            status = str(value.get("status") or "SUCCESS").upper()
            results.append(ManualSyncPlatformResult(
                key="NAVER",
                label="네이버",
                status="SUCCESS" if status == "SUCCESS" and not failed else status,
                fetched=int(value.get("fetched_count") or 0),
                new=int(value.get("inserted_count") or value.get("created_count") or value.get("new") or 0),
                updated=int(value.get("updated_count") or value.get("updated") or 0),
                unchanged=int(value.get("unchanged_count") or value.get("unchanged") or 0),
                failed=failed,
                error=str(value.get("error_message") or "") or None,
            ))
        except Exception as error:  # keep Coupang results visible
            results.append(ManualSyncPlatformResult(
                key="NAVER", label="네이버", status="FAILED", failed=1,
                error=error.__class__.__name__,
            ))

        try:
            coupang_results = self.coupang_factory(self.database).sync_accounts(
                (COUPANG_OJE_NS, COUPANG_OJE_PLUS)
            )
            by_account = {
                str(getattr(item, "account_code", "")): item
                for item in coupang_results
            }
            for account in (COUPANG_OJE_NS, COUPANG_OJE_PLUS):
                item = by_account.get(account)
                if item is None:
                    results.append(ManualSyncPlatformResult(
                        key=f"COUPANG_{account}", label=f"쿠팡 {account}",
                        status="FAILED", failed=1, error="RESULT_MISSING",
                    ))
                    continue
                error = getattr(item, "error", None)
                failed = int(getattr(item, "failed", 0) or 0)
                results.append(ManualSyncPlatformResult(
                    key=f"COUPANG_{account}",
                    label=f"쿠팡 {account}",
                    status="FAILED" if error else "PARTIAL" if failed else "SUCCESS",
                    fetched=int(getattr(item, "fetched", 0) or 0),
                    new=int(getattr(item, "new", 0) or 0),
                    updated=int(getattr(item, "updated", 0) or 0),
                    unchanged=int(getattr(item, "unchanged", 0) or 0),
                    failed=failed + int(bool(error) and not failed),
                    error=str(error) if error else None,
                ))
        except Exception as error:
            for account in (COUPANG_OJE_NS, COUPANG_OJE_PLUS):
                results.append(ManualSyncPlatformResult(
                    key=f"COUPANG_{account}", label=f"쿠팡 {account}",
                    status="FAILED", failed=1, error=error.__class__.__name__,
                ))

        return {
            "status": (
                "SUCCESS"
                if all(item.status == "SUCCESS" for item in results)
                else "PARTIAL"
            ),
            "platforms": [item.to_dict() for item in results],
        }
