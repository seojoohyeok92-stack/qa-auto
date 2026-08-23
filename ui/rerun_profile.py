"""Stage timing for a single Streamlit script run.

The dashboard re-executes this whole application on every ``st.rerun()``, so
"the page took N seconds" is only actionable once N is split across the
stages that produced it. This module records that split.

It is off unless ``OJE_RERUN_PROFILE`` is set: when disabled :func:`stage` is
a context manager that reads one module-level boolean and yields, so an
un-profiled production rerun pays no clock reads and allocates nothing.
Timings are kept per thread because Streamlit serves each browser session on
its own script thread, and they are cleared by :func:`begin` at the top of
every run so one run never reports another's stages.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

ENV_FLAG = "OJE_RERUN_PROFILE"

_TRUTHY = {"1", "true", "yes", "on"}
_local = threading.local()


def enabled() -> bool:
    """Whether stage timing is switched on for this process."""

    return str(os.getenv(ENV_FLAG, "")).strip().lower() in _TRUTHY


def begin() -> None:
    """Start a new run, discarding any stages left by the previous one."""

    if not enabled():
        return
    _local.stages = []
    _local.started = time.perf_counter()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Record the wall-clock time spent inside this block."""

    if not enabled():
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        stages = getattr(_local, "stages", None)
        if stages is None:
            stages = _local.stages = []
        stages.append((name, elapsed))


def snapshot() -> dict[str, Any]:
    """The stages recorded so far, with elapsed and cumulative seconds.

    ``other`` is whatever the run spent outside any named stage -- module
    import, Streamlit's own bookkeeping, and any code not yet instrumented --
    so the reported stages always add up to the measured total.
    """

    if not enabled():
        return {}
    stages = getattr(_local, "stages", None)
    started = getattr(_local, "started", None)
    if not stages or started is None:
        return {}
    total = time.perf_counter() - started
    rows: list[dict[str, float | str]] = []
    cumulative = 0.0
    for name, elapsed in stages:
        cumulative += elapsed
        rows.append(
            {
                "stage": name,
                "elapsed_seconds": round(elapsed, 3),
                "cumulative_seconds": round(cumulative, 3),
            }
        )
    accounted = sum(float(row["elapsed_seconds"]) for row in rows)
    rows.append(
        {
            "stage": "other",
            "elapsed_seconds": round(max(0.0, total - accounted), 3),
            "cumulative_seconds": round(total, 3),
        }
    )
    return {"total_seconds": round(total, 3), "stages": rows}


SESSION_KEY = "rerun_profile"


def publish() -> dict[str, Any]:
    """Hand this run's stages to the session so the UI and tests can read them.

    Accumulation stays thread-local because it happens in the hot path; the
    finished snapshot is copied into ``st.session_state`` once per run so it
    survives past the script thread that produced it.
    """

    if not enabled():
        return {}
    result = snapshot()
    if not result:
        return {}
    try:
        import streamlit as st

        st.session_state[SESSION_KEY] = result
    except Exception:  # pragma: no cover - no script context (bare mode)
        pass
    return result
