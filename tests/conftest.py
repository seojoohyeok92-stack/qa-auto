"""Safe defaults for every test run, applied before any project import.

``config`` calls ``load_dotenv(.env)`` at import time, and ``load_dotenv`` does
not overwrite variables that already exist.  That is the whole mechanism: an
operator's real ``.env`` -- ``QNA_GPT_MODE=ACTIVE`` with a live API key and
``OJE_SEMANTIC_ANALYZER_ENABLED=true`` -- was reaching pytest, and a full suite
run made real, billable calls to the GPT provider.  Live answers are also
non-deterministic, so the run was not a usable regression gate either.

Setting the variables here wins, because dotenv leaves an existing value alone.
Every one is a *default* via ``setdefault``, so a test that needs a different
value can still monkeypatch it, and an explicitly exported shell variable still
takes precedence for a deliberate integration run.
"""
from __future__ import annotations

import ipaddress
import os
import socket

import pytest


# --- outbound integrations -------------------------------------------------
#
# Each of these reaches something outside the process. None of them may fire
# from an ordinary test run, whatever the developer's .env happens to say.

# 테스트가 운영 카카오 공통 대기열에 메시지를 넣지 않도록 기본 차단한다.
os.environ.setdefault("KAKAO_NOTIFY_ENABLED", "0")

# The fake provider is the code default too; .env is what overrode it.
os.environ.setdefault("QNA_GPT_MODE", "FAKE")
os.environ.setdefault("QNA_GPT_ENABLED", "false")
# The semantic stage is off by default in production code as well. Tests that
# exercise it monkeypatch this themselves (see test_semantic_pipeline_*).
os.environ.setdefault("OJE_SEMANTIC_ANALYZER_ENABLED", "0")
# No credential should be readable from a test process at all: a fake provider
# that somehow reached the transport would otherwise authenticate for real.
os.environ["QNA_GPT_API_KEY"] = ""

# Naver: neither a single post nor the auto-post loop may reach the platform.
os.environ.setdefault("NAVER_POST_ENABLED", "false")
os.environ.setdefault("NAVER_AUTO_POST_ENABLED", "false")
os.environ.setdefault("NAVER_SYNC_ENABLED", "false")
os.environ.setdefault("NAVER_AUTO_SYNC_ENABLED", "false")

# DPS drives a real browser session against a real vendor UI.
os.environ.setdefault("DPS_SESSION_MONITOR_ENABLED", "false")
os.environ.setdefault("DPS_SESSION_KEEPALIVE_ENABLED", "false")
os.environ.setdefault("DPS_PASSIVE_IDLE_ENABLED", "false")


# --- network egress guard --------------------------------------------------
#
# Configuration alone is not proof. The env defaults above stop the paths we
# know about; this stops the ones we do not, and turns "a test quietly called
# an API" from an invisible cost into a loud failure.
#
# Loopback and private addresses stay open: the DPS agent, and any test that
# binds a local socket, are legitimate.
#
# Opt out for a deliberate integration run with
# ``OJE_TEST_ALLOW_NETWORK=1``, which is also what the
# ``integration`` marker below is for.

_ALLOW_NETWORK_ENV = "OJE_TEST_ALLOW_NETWORK"

#: Every non-loopback connection attempted during the session, for reporting.
BLOCKED_EGRESS: list[tuple[str, object]] = []

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_current_test = {"id": "<import/collection>"}


def _network_allowed() -> bool:
    return str(os.environ.get(_ALLOW_NETWORK_ENV, "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _is_local(address: object) -> bool:
    """Whether this address stays on the machine or its private network."""

    if not isinstance(address, tuple) or not address:
        # AF_UNIX and friends never leave the host.
        return True
    host = address[0]
    if host in {"localhost", "::1", ""}:
        return True
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        # A hostname that never resolved cannot be dialled anyway; let the
        # real call raise its own error rather than inventing one.
        return False
    return parsed.is_loopback or parsed.is_private or parsed.is_link_local


def _guard(original):
    def connect(self, address, *args, **kwargs):
        if not _is_local(address) and not _network_allowed():
            BLOCKED_EGRESS.append((_current_test["id"], address))
            raise OSError(
                f"외부 네트워크 호출이 차단되었습니다: {address} "
                f"({_current_test['id']}). 테스트는 fake/mock을 사용해야 "
                f"합니다. 실제 호출이 필요한 통합 테스트라면 "
                f"{_ALLOW_NETWORK_ENV}=1 로 실행하세요."
            )
        return original(self, address, *args, **kwargs)

    return connect


socket.socket.connect = _guard(_real_connect)
socket.socket.connect_ex = _guard(_real_connect_ex)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: 실제 외부 provider를 호출하는 테스트. 기본 실행에서 "
        f"제외되며 {_ALLOW_NETWORK_ENV}=1 일 때만 실행된다.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Keep provider-calling tests out of the ordinary run."""

    if _network_allowed():
        return
    skip = pytest.mark.skip(
        reason=f"실제 provider 호출 테스트: {_ALLOW_NETWORK_ENV}=1 필요"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    _current_test["id"] = item.nodeid
    yield


def pytest_terminal_summary(terminalreporter, *args, **kwargs) -> None:
    """Report egress rather than letting a blocked call pass unnoticed."""

    if not BLOCKED_EGRESS:
        terminalreporter.write_sep("=", "외부 네트워크 호출 시도: 0")
        return
    terminalreporter.write_sep(
        "=", f"차단된 외부 네트워크 호출: {len(BLOCKED_EGRESS)}건"
    )
    seen: dict[str, int] = {}
    for nodeid, _address in BLOCKED_EGRESS:
        seen[nodeid] = seen.get(nodeid, 0) + 1
    for nodeid, count in sorted(seen.items(), key=lambda row: -row[1]):
        terminalreporter.write_line(f"  {count:>4}  {nodeid}")
