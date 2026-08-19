from __future__ import annotations

import json
import time
from unittest.mock import Mock, patch

import pytest

from dps import agent_server
from services import dps_agent_client


def _agent() -> agent_server.DpsWindowsAgent:
    store = Mock()
    store.load.return_value = {
        "tab_title_keywords": ["DPS"],
        "allowed_hosts": ["dps2u.co.kr"],
    }
    store.load_agent_state.return_value = {}
    return agent_server.DpsWindowsAgent(
        store=store,
        tab_manager=Mock(),
        ui_automation=Mock(),
    )


def _response(body: dict, status: int = 200) -> Mock:
    response = Mock()
    response.status = status
    response.read.return_value = json.dumps(body).encode("utf-8")
    return response


@pytest.mark.parametrize(
    ("connect_error", "expected"),
    [
        (TimeoutError(), "AGENT_CONNECT_TIMEOUT"),
        (ConnectionRefusedError(), "AGENT_CONNECTION_FAILED"),
    ],
)
def test_request_classifies_connect_failures(
    connect_error: BaseException,
    expected: str,
) -> None:
    connection = Mock()
    connection.connect.side_effect = connect_error
    with patch.object(
        dps_agent_client.http.client,
        "HTTPConnection",
        return_value=connection,
    ):
        result = dps_agent_client._request("/status", timeout=(5, 8))
    assert result["code"] == expected


def test_request_classifies_read_timeout_separately() -> None:
    connection = Mock()
    connection.sock = Mock()
    connection.getresponse.side_effect = TimeoutError()
    with patch.object(
        dps_agent_client.http.client,
        "HTTPConnection",
        return_value=connection,
    ):
        result = dps_agent_client._request("/lookup", {}, timeout=(5, 90))
    assert result["code"] == "AGENT_READ_TIMEOUT"
    assert result["agent_running"] is True
    assert "연결하지" not in result["message"]


def test_request_rejects_invalid_json_response() -> None:
    connection = Mock()
    connection.sock = Mock()
    response = Mock()
    response.read.return_value = b"not-json"
    connection.getresponse.return_value = response
    with patch.object(
        dps_agent_client.http.client,
        "HTTPConnection",
        return_value=connection,
    ):
        result = dps_agent_client._request("/status")
    assert result["code"] == "AGENT_RESPONSE_INVALID"


def test_request_uses_separate_connect_and_read_timeouts() -> None:
    connection = Mock()
    connection.sock = Mock()
    connection.getresponse.return_value = _response({"success": True})
    with patch.object(
        dps_agent_client.http.client,
        "HTTPConnection",
        return_value=connection,
    ) as constructor:
        result = dps_agent_client._request(
            "/lookup", {}, timeout=(5.0, 90.0)
        )
    assert result["success"] is True
    assert constructor.call_args.kwargs["timeout"] == 5.0
    connection.sock.settimeout.assert_called_once_with(90.0)


def test_poll_recovers_completed_result_after_timeout() -> None:
    completed = {
        "success": True,
        "job_status": "COMPLETED",
        "stage": "COMPLETED",
        "status": "DETAIL_DATE_CONFLICT",
        "result_source": "request_state",
    }
    with patch.object(
        dps_agent_client,
        "get_dps_lookup_status",
        side_effect=[
            {
                "success": True,
                "code": "LOOKUP_RUNNING",
                "job_status": "RUNNING",
                "stage": "DETAIL_PARSING",
            },
            completed,
        ],
    ):
        result = dps_agent_client.poll_dps_lookup(
            "request-1",
            timeout=1,
            interval=0,
            recovered_after_timeout=True,
        )
    assert result["success"] is True
    assert result["recovered_after_timeout"] is True
    assert result["diagnostics"]["original_request_id"] == "request-1"
    assert result["status"] == "DETAIL_DATE_CONFLICT"


def test_poll_returns_failed_job_without_restarting() -> None:
    failed = {
        "success": False,
        "job_status": "FAILED",
        "stage": "FAILED",
        "code": "DETAIL_PARSE_FAILED",
    }
    with patch.object(
        dps_agent_client,
        "get_dps_lookup_status",
        return_value=failed,
    ):
        result = dps_agent_client.poll_dps_lookup(
            "request-1", timeout=1, interval=0
        )
    assert result == failed


def test_poll_has_total_deadline() -> None:
    with patch.object(
        dps_agent_client,
        "get_dps_lookup_status",
        return_value={
            "success": True,
            "code": "LOOKUP_RUNNING",
            "job_status": "RUNNING",
            "stage": "DETAIL_PARSING",
        },
    ):
        result = dps_agent_client.poll_dps_lookup(
            "request-1", timeout=0, interval=0
        )
    assert result["code"] == "AGENT_READ_TIMEOUT"
    assert "제한시간" in result["message"]


def test_lookup_read_timeout_polls_same_request_id() -> None:
    with (
        patch.object(
            dps_agent_client,
            "start_dps_agent",
            return_value={"agent_running": True},
        ),
        patch.object(
            dps_agent_client,
            "_request",
            return_value={"code": "AGENT_READ_TIMEOUT"},
        ),
        patch.object(
            dps_agent_client,
            "poll_dps_lookup",
            return_value={"success": True},
        ) as poll,
    ):
        result = dps_agent_client.lookup_dps_order(
            request_id="request-1",
            order_id="order-1",
            dps_period_start="2026-07-01",
            dps_period_end="2026-07-28",
        )
    assert result["success"] is True
    assert poll.call_args.args[0] == "request-1"
    assert poll.call_args.kwargs["recovered_after_timeout"] is True


def test_agent_same_request_id_returns_completed_without_rerun() -> None:
    agent = _agent()
    with patch.object(
        agent,
        "_execute_lookup",
        return_value={"success": True, "status": "RESULT_FOUND_WITH_DETAIL"},
    ) as execute:
        first = agent.lookup(
            request_id="request-1",
            order_id="order-1",
            dps_query_value="order-1",
            dps_query_value_type="order_id",
        )
        second = agent.lookup(
            request_id="request-1",
            order_id="order-1",
            dps_query_value="order-1",
            dps_query_value_type="order_id",
        )
    assert first["success"] is True
    assert second["job_status"] == "COMPLETED"
    assert second["result_source"] == "request_state"
    execute.assert_called_once()


def test_agent_running_job_returns_state_without_rerun() -> None:
    agent = _agent()
    now = time.time()
    agent.lookup_jobs["request-1"] = {
        "request_id": "request-1",
        "job_status": "RUNNING",
        "stage": "DETAIL_PARSING",
        "started_at": "2026-07-29T10:00:00+09:00",
        "updated_at": "2026-07-29T10:00:01+09:00",
        "updated_at_epoch": now,
        "completed_at": None,
        "result": None,
        "fingerprint": ("order-1", "", "", False),
    }
    with patch.object(agent, "_execute_lookup") as execute:
        result = agent.lookup(
            request_id="request-1",
            order_id="order-1",
        )
    assert result["code"] == "LOOKUP_RUNNING"
    assert result["stage"] == "DETAIL_PARSING"
    execute.assert_not_called()


def test_agent_blocks_same_order_period_with_new_request_id() -> None:
    agent = _agent()
    now = time.time()
    fingerprint = ("order-1", "2026-07-01", "2026-07-28", False)
    agent.lookup_jobs["request-1"] = {
        "request_id": "request-1",
        "job_status": "RUNNING",
        "stage": "DETAIL_OPENED",
        "started_at": "2026-07-29T10:00:00+09:00",
        "updated_at": "2026-07-29T10:00:01+09:00",
        "updated_at_epoch": now,
        "completed_at": None,
        "result": None,
        "fingerprint": fingerprint,
    }
    with patch.object(agent, "_execute_lookup") as execute:
        result = agent.lookup(
            request_id="request-2",
            order_id="order-1",
            dps_period_start="2026-07-01",
            dps_period_end="2026-07-28",
        )
    assert result["request_id"] == "request-1"
    assert result["job_status"] == "RUNNING"
    execute.assert_not_called()


def test_agent_completed_and_failed_states_are_saved() -> None:
    for request_id, success_value, expected in (
        ("ok", True, "COMPLETED"),
        ("fail", False, "FAILED"),
    ):
        agent = _agent()
        with patch.object(
            agent,
            "_execute_lookup",
            return_value={"success": success_value, "code": expected},
        ):
            agent.lookup(request_id=request_id, order_id="order-1")
        assert agent.lookup_jobs[request_id]["job_status"] == expected
        assert agent.lookup_jobs[request_id]["completed_at"]
        checkpoints = agent.lookup_jobs[request_id]["checkpoint_history"]
        assert checkpoints[0]["checkpoint"] == "LOOKUP_REQUEST_RECEIVED"
        assert checkpoints[-1]["checkpoint"] == "LOOKUP_RESPONSE_SENT"
        assert all("elapsed_ms" in item for item in checkpoints)
        assert all("total_elapsed_ms" in item for item in checkpoints)


def test_agent_job_ttl_cleanup_preserves_running() -> None:
    agent = _agent()
    old = time.time() - agent_server.LOOKUP_JOB_TTL_SECONDS - 1
    agent.lookup_jobs = {
        "done": {
            "job_status": "COMPLETED",
            "updated_at_epoch": old,
        },
        "running": {
            "job_status": "RUNNING",
            "updated_at_epoch": old,
        },
    }
    agent._cleanup_lookup_jobs()
    assert "done" not in agent.lookup_jobs
    assert "running" in agent.lookup_jobs


def test_lookup_status_never_uses_other_request_last_result(
    tmp_path,
) -> None:
    agent = _agent()
    last_file = tmp_path / "last.json"
    last_file.write_text(
        json.dumps({"request_id": "other", "success": True}),
        encoding="utf-8",
    )
    with patch.object(agent_server, "LAST_LOOKUP_RESULT_FILE", last_file):
        result = agent.lookup_status("request-1")
    assert result["code"] == "LOOKUP_REQUEST_NOT_FOUND"


def test_status_endpoint_path_contains_encoded_request_and_exact_period() -> None:
    with patch.object(
        dps_agent_client,
        "_request",
        return_value={"success": True},
    ) as request:
        dps_agent_client.get_dps_lookup_status(
            "request/1",
            order_id="order-1",
            period_start="2026-07-01",
            period_end="2026-07-28",
        )
    path = request.call_args.args[0]
    assert path.startswith("/lookup/request%2F1?")
    assert "order_id=order-1" in path
    assert "period_start=2026-07-01" in path
