from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from repositories.log_repository import LogRepository, mask_sensitive_data


SYNC_LOG_PATH = Path("logs") / "naver_inquiry_sync.log"


def _logger() -> logging.Logger:
    logger = logging.getLogger("qna.naver_inquiry_sync")
    if not any(
        getattr(handler, "_qna_naver_sync_handler", False)
        for handler in logger.handlers
    ):
        SYNC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            SYNC_LOG_PATH,
            maxBytes=2_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler._qna_naver_sync_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s - %(message)s"
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


class InquirySyncTrace:
    """Write privacy-safe sync stages to both the file and activity log."""

    def __init__(
        self,
        logs: LogRepository,
        correlation_id: str,
    ) -> None:
        self.logs = logs
        self.correlation_id = correlation_id

    def emit(
        self,
        event_code: str,
        details: dict[str, Any] | None = None,
        *,
        level: str = "INFO",
        persist: bool = True,
    ) -> None:
        safe_details = mask_sensitive_data(details or {})
        # Correlation IDs are generated UUIDs, not customer data. Reinsert the
        # exact value after numeric masking so a UUID containing a long digit
        # run remains usable for end-to-end trace lookup.
        safe_details["correlation_id"] = self.correlation_id
        _logger().log(
            getattr(logging, str(level).upper(), logging.INFO),
            "event=%s details=%s",
            event_code,
            json.dumps(
                safe_details,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )
        if persist:
            self.logs.record_system(
                event_code,
                "네이버 문의 동기화 단계가 기록되었습니다.",
                level=level,
                details=safe_details,
            )
