from __future__ import annotations

from datetime import datetime
from logging.handlers import RotatingFileHandler
import logging
import os
from pathlib import Path
import threading
import traceback
from typing import Any


RUNTIME_STARTED_AT = datetime.now().astimezone().isoformat(timespec="seconds")
RUNTIME_LOG_PATH = Path("logs") / "streamlit_runtime.log"


from services.semantic_analysis import is_enabled as semantic_analyzer_enabled


def _runtime_logger() -> logging.Logger:
    logger = logging.getLogger("qna.streamlit_runtime")
    if not any(
        getattr(handler, "_qna_runtime_handler", False)
        for handler in logger.handlers
    ):
        RUNTIME_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            RUNTIME_LOG_PATH,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler._qna_runtime_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s - %(message)s"
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def record_runtime_exception(
    event_code: str,
    error: BaseException,
    *,
    inquiry_id: int | None = None,
    correlation_id: str | None = None,
    stage: str | None = None,
) -> None:
    """Persist stack frames without serializing request/prompt/answer payloads."""

    stack = "".join(traceback.format_tb(error.__traceback__))
    _runtime_logger().error(
        "event=%s inquiry_id=%s correlation_id=%s stage=%s "
        "exception_type=%s thread=%s\n%s",
        event_code,
        inquiry_id,
        correlation_id,
        stage,
        error.__class__.__name__,
        threading.current_thread().name,
        stack,
    )


def runtime_snapshot(
    *,
    session_marker: str,
    last_correlation_id: str | None = None,
    last_success_stage: str | None = None,
    last_error_stage: str | None = None,
    agent_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = agent_status if isinstance(agent_status, dict) else {}
    return {
        "streamlit_pid": os.getpid(),
        "streamlit_started_at": RUNTIME_STARTED_AT,
        "streamlit_port": int(os.getenv("STREAMLIT_SERVER_PORT", "8501")),
        "session_marker": session_marker,
        "last_correlation_id": last_correlation_id,
        "last_success_stage": last_success_stage,
        "last_error_stage": last_error_stage,
        "agent_pid": status.get("agent_pid"),
        "agent_connected": bool(status.get("agent_running")),
        "agent_last_stage": status.get("last_lookup_stage"),
        "agent_restore_warning": status.get(
            "last_window_restore_warning"
        ),
        "heartbeat_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        # Whether GPT ① is running. The pipeline is built around it -- it
        # decides what the question means and which evidence to fetch -- and
        # with it off every route falls back to the keyword classifier. That
        # fallback still drafts for staff, but it can no longer publish on its
        # own (UNDERSTANDING_UNAVAILABLE), so an operator needs to be able to
        # see the state rather than infer it from a hold reason.
        "gpt_understanding_enabled": semantic_analyzer_enabled(),
    }
