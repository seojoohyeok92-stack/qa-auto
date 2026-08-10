from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from dps.dps_ui_automation import DpsUiAutomation
from services import dps_agent_client
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.dps_repository import DpsRepository
from repositories.gpt_provider_run_repository import (
    GptProviderRunRepository,
)
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from ui.review_workspace import (
    load_program_answer_view,
    program_answer_widget_key,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Element:
    def __init__(
        self,
        name: str,
        control_type: str = "Text",
        *,
        class_name: str = "",
        automation_id: str = "",
    ) -> None:
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            class_name=class_name,
            automation_id=automation_id,
            runtime_id=(id(self),),
            handle=0,
            framework_id="Chrome",
        )
        self.invoke = Mock()
        self.click_input = Mock()

    def window_text(self):
        return self.element_info.name


class Window:
    def __init__(self, handle: int, elements=None) -> None:
        self.handle = handle
        self.element_info = SimpleNamespace(handle=handle)
        self.elements = list(elements or [])
        self.close = Mock()

    def descendants(self, control_type=None):
        if control_type is None:
            return list(self.elements)
        return [
            value
            for value in self.elements
            if value.element_info.control_type == control_type
        ]


def _inquiry(database: Database, suffix: str, *, with_order=True) -> int:
    result = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "STORE",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": f"PHASE86-{suffix}",
            "inquiry_type": "배송",
            "title": "배송 문의",
            "content": "배송은 얼마나 걸리나요?",
            "order_id": "2026073000000001" if with_order else None,
            "product_order_id": "2026073000000099",
            "raw_json": {},
        }
    )
    if with_order:
        with database.transaction() as connection:
            connection.execute(
                "UPDATE inquiries SET order_date = ? WHERE id = ?",
                ("2026-07-25", result.inquiry_id),
            )
    WorkflowRepository(database).initialize_steps(result.inquiry_id)
    return result.inquiry_id


def _gpt(answer: str) -> AnswerResult:
    return AnswerResult(
        status=AnswerStatus.GENERATED,
        category="기타",
        reason="validated",
        answer=answer,
        provider="openai_hybrid",
        auto_answerable=True,
        needs_review=False,
        metadata={
            "hybrid": {
                "fallback_used": False,
                "validation": {"passed": True},
            }
        },
    )


def test_preexisting_streamlit_window_is_never_detail_candidate():
    automation = DpsUiAutomation()
    purchase = Window(10)
    streamlit_window = Window(
        20, [Element("고객정보"), Element("품목상세내역")]
    )
    link = Element("SALE-1", "Hyperlink")
    automation.find_dps_sales_link = Mock(return_value=(link, {}))
    automation._detail_markers = Mock(
        side_effect=lambda window, **_: (
            (
                ["고객정보", "품목상세내역"],
                "localhost:8501",
            )
            if window.handle == 20
            else ([], "dps2u.co.kr/dpsweb/main.do")
        )
    )

    result = automation.lookup_sales_detail(
        purchase_window=purchase,
        list_snapshot={},
        list_data={
            "dps_sales_number": "SALE-1",
            "dps_query_value": "ORDER-1",
        },
        list_diagnostics={
            "matched_row_index": 0,
            "raw_rows": [["ORDER-1", "SALE-1"]],
        },
        expected_order_id="ORDER-1",
        window_provider=lambda: [purchase, streamlit_window],
        timeout=0.01,
    )

    assert result["detail_lookup"]["status"] == "DETAIL_OPEN_TIMEOUT"
    streamlit_window.close.assert_not_called()


def test_chrome_caption_close_is_excluded():
    automation = DpsUiAutomation()
    caption = Element(
        "닫기",
        "Button",
        class_name="ChromeCaptionButton",
        automation_id="Close",
    )
    detail = Window(20, [caption])
    closed, method = automation.close_sales_detail(
        detail,
        purchase_window=detail,
        was_new_window=False,
    )
    assert closed is False
    assert method == "DETAIL_CLOSE_FAILED"
    caption.invoke.assert_not_called()


def test_parser_success_survives_window_cleanup_exception():
    automation = DpsUiAutomation()
    purchase = Window(10)
    detail = Window(30)
    link = Element("SALE-1", "Hyperlink")
    automation.find_dps_sales_link = Mock(return_value=(link, {}))
    calls = 0

    def windows():
        nonlocal calls
        calls += 1
        return [purchase] if calls == 1 else [purchase, detail]

    automation._detail_markers = Mock(
        side_effect=lambda window, **_: (
            (["고객정보", "품목상세내역"], "dps2u.co.kr/detail")
            if window.handle == 30
            else ([], "dps2u.co.kr/dpsweb/main.do")
        )
    )
    automation.collect_sales_detail_snapshot = Mock(
        return_value={
            "parsed": {
                "customer_info": {"masked": True},
                "detail_items": [{"required_delivery_date": "2026-08-03"}],
                "parse_warnings": [],
            },
            "headers": [],
            "rows": [],
        }
    )
    automation.close_sales_detail = Mock(
        side_effect=RuntimeError("restore failed")
    )

    result = automation.lookup_sales_detail(
        purchase_window=purchase,
        list_snapshot={},
        list_data={
            "dps_sales_number": "SALE-1",
            "dps_query_value": "ORDER-1",
        },
        list_diagnostics={
            "matched_row_index": 0,
            "raw_rows": [["ORDER-1", "SALE-1"]],
        },
        expected_order_id="ORDER-1",
        window_provider=windows,
        timeout=0.1,
    )

    assert result["detail_lookup"]["parsed"] is True
    assert result["detail_lookup"]["status"] == "DETAIL_CLOSE_FAILED"
    assert result["detail"]["detail_items"]
    assert result["diagnostics"]["detail_close_warning"] == "RuntimeError"


def test_program_answer_view_binds_metadata_to_same_draft(tmp_path):
    database = Database(tmp_path / "view.db")
    database.initialize()
    inquiry_id = _inquiry(database, "VIEW")
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        _gpt("새 Program Answer"),
        order_id="2026073000000001",
    )
    run = GptProviderRunRepository(database).create_run(
        inquiry_id=inquiry_id,
        draft_id=draft["id"],
        correlation_id="corr-safe",
        provider="openai",
        model="model",
        mode="ACTIVE",
        started_at="2026-07-30T10:00:00+09:00",
        completed_at="2026-07-30T10:00:01+09:00",
        success=True,
        validator_passed=True,
    )

    view = load_program_answer_view(database, inquiry_id)

    assert view["draft_id"] == draft["id"]
    assert view["provider_run_id"] == run["id"]
    assert view["provider_run"]["draft_id"] == view["draft"]["id"]


def test_program_widget_key_changes_with_active_draft():
    assert program_answer_widget_key(7, 10) != (
        program_answer_widget_key(7, 11)
    )


def test_transient_agent_status_error_is_not_mislabeled_as_legacy(
    monkeypatch,
):
    monkeypatch.setattr(
        dps_agent_client,
        "_request",
        lambda *args, **kwargs: {
            "success": False,
            "agent_running": True,
            "code": "AGENT_REQUEST_FAILED",
        },
    )
    status = dps_agent_client.get_dps_agent_status()
    assert status["code"] == "AGENT_REQUEST_FAILED"


def test_confirmed_legacy_agent_mode_still_requires_restart(monkeypatch):
    monkeypatch.setattr(
        dps_agent_client,
        "_request",
        lambda *args, **kwargs: {
            "success": True,
            "agent_running": True,
            "mode": "OLD_MODE",
        },
    )
    status = dps_agent_client.get_dps_agent_status()
    assert status["code"] == "AGENT_RESTART_REQUIRED"


def test_gpt_button_reruns_and_displays_new_program_answer(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "app.db"
    database = Database(database_path)
    database.initialize()
    inquiry_id = _inquiry(database, "APPTEST", with_order=True)
    DpsRepository(database).create_lookup_result(
        inquiry_id=inquiry_id,
        order_id="2026073000000001",
        lookup_status="SUCCESS",
        raw_result={},
        normalized_result={
            "lookup_status": "SUCCESS",
            "required_delivery_date": "2026-08-03",
            "installation_date": "2026-08-03",
            "installation_date_source": (
                "DPS_ITEM_DETAIL_REQUIRED_DELIVERY_DATE"
            ),
            "date_parse_status": "PARSED",
            "requires_human_review": False,
        },
        expires_at="2099-01-01T00:00:00+09:00",
    )
    monkeypatch.setenv("OJE_AUTOMATION_DB_PATH", str(database_path))
    monkeypatch.setenv("PHASE86_INQUIRY_ID", str(inquiry_id))
    monkeypatch.setenv("PHASE86_PANEL", "answer")
    monkeypatch.setenv(
        "PHASE86_FAKE_ANSWER",
        "현재 확인되는 설치예정일은 2026년 8월 3일입니다.",
    )
    monkeypatch.setenv("QNA_GPT_PROVIDER", "fake")

    app = AppTest.from_file(
        str(PROJECT_ROOT / "uat" / "phase86_streamlit_probe.py")
    ).run(timeout=30)
    generate = next(
        button
        for button in app.button
        if button.label.endswith("답변 생성")
    )
    generate.click()
    app.run(timeout=30)

    assert not app.exception
    active = AnswerRepository(database).active_for_inquiry(inquiry_id)
    assert active is not None
    expected_widget_key = f"draft_text_{inquiry_id}"
    program = [
        area
        for area in app.text_area
        if area.label == "Program Answer"
        and area.key == expected_widget_key
    ]
    assert program
    assert program[0].value == active["original_answer"]
    assert str(active["id"]) in " ".join(
        str(item.value or "") for item in app.markdown
    )


def test_dps_system_exit_is_recoverable_in_streamlit(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "dps-app.db"
    database = Database(database_path)
    database.initialize()
    inquiry_id = _inquiry(database, "DPS-EXIT")
    monkeypatch.setenv("OJE_AUTOMATION_DB_PATH", str(database_path))
    monkeypatch.setenv("PHASE86_INQUIRY_ID", str(inquiry_id))
    monkeypatch.setenv("PHASE86_PANEL", "dps")
    monkeypatch.setenv("PHASE86_DPS_FAILURE", "system_exit")

    app = AppTest.from_file(
        str(PROJECT_ROOT / "uat" / "phase86_streamlit_probe.py")
    ).run(timeout=30)
    lookup = next(
        button for button in app.button if button.label == "DPS 재조회"
    )
    lookup.click()
    app.run(timeout=30)

    assert not app.exception
    assert any(
        "다시 시도" in str(error.value) for error in app.error
    )
    assert any(button.label == "DPS 재조회" for button in app.button)
