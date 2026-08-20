from __future__ import annotations

from pathlib import Path

import pytest

from answer.answer_format import extract_answer_body, format_final_answer
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from dps.chrome_tab_manager import ChromeTabManager, RuntimeConnection
from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.answer_repository import AnswerRepository
from repositories.learning_provenance_repository import LearningProvenanceRepository
from repositories.learning_repository import LearningRepository
from services.historical_case_service import HistoricalCaseService
from services.inquiry_analysis_service import InquiryAnalysisService
from services.learning_lifecycle_service import resolve_learning_lifecycle


ORDER_ID = "2026081392706071"
CURRENT_COORDINATION = (
    "배송중이라고뜨는데 기사님이랑 통화는 안한상태라 "
    "약속 날짜 시간을 못정했는데 계속 기다려야 되나요"
)


@pytest.mark.parametrize(
    "question",
    (
        CURRENT_COORDINATION,
        "배송중인데 기사님 연락이 아직 없어요.",
        "설치기사님한테 언제 연락이 오나요?",
        "배송기사와 방문시간을 아직 못 정했는데 기다리면 되나요?",
        "배송중으로 나오는데 방문 일정이 안 잡혔습니다.",
        "기사님과 통화하지 못했는데 설치 날짜는 어떻게 확인하나요?",
        "배송중이라고만 나오고 연락이 없는데 언제 받을 수 있나요?",
    ),
)
def test_current_order_agent_coordination_requires_dps(question: str) -> None:
    result = InquiryAnalysisService().analyze(
        AnswerRequest(
            inquiry_id=1,
            question_id="CURRENT-COORDINATION",
            store_code="OJE_PLUS",
            inquiry_type="CUSTOMER_INQUIRY",
            question=question,
            order_id=ORDER_ID,
        )
    )

    assert result.inquiry_type.value == "DELIVERY_INSTALLATION_STATUS"
    assert result.detected_intent in {
        "DELIVERY_STATUS", "DELIVERY_DATE", "INSTALLATION_DATE"
    }
    assert result.answer_strategy.value == "DIRECT_FACT_ANSWER"
    assert result.order_id_status.value == "VALIDATED"
    assert result.requires_order_lookup is True
    assert result.requires_dps_lookup is True
    assert result.can_execute_dps_lookup is True


@pytest.mark.parametrize(
    "question",
    (
        "설치기사님이 벽걸이 설치도 해주시나요?",
        "기사님 설치 방법이 궁금합니다.",
    ),
)
def test_general_installation_method_does_not_require_dps(question: str) -> None:
    result = InquiryAnalysisService().analyze(
        AnswerRequest(
            inquiry_id=1,
            question_id="METHOD-CONTROL",
            store_code="OJE_PLUS",
            inquiry_type="CUSTOMER_INQUIRY",
            question=question,
            order_id=ORDER_ID,
        )
    )
    assert result.detected_intent == "INSTALLATION_METHOD"
    assert result.requires_order_lookup is False
    assert result.requires_dps_lookup is False


@pytest.mark.parametrize(
    "body",
    (
        "♣♧안녕하세요♧♣\n문의하신 상품을 안내드립니다.",
        "안녕하세요 고객님.\n문의하신 상품을 안내드립니다.",
        "안녕하세요 오제앤에스 입니다.\n문의하신 상품을 안내드립니다.",
        "오제 챗봇(Chat Bot)이 답변드립니다.\n문의하신 상품을 안내드립니다.",
        "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
        "문의하신 상품을 안내드립니다.\n\n감사합니다.",
    ),
)
def test_presentation_normalization_owns_greeting_and_footer_once(body: str) -> None:
    final = format_final_answer(body)
    assert final.count("♣♧안녕하세요♧♣") == 1
    assert final.count("오제 챗봇(Chat Bot)이 답변드립니다.") == 1
    assert final.count("감사합니다.") == 1
    assert extract_answer_body(final) == "문의하신 상품을 안내드립니다."


def test_presentation_normalization_preserves_body_greeting() -> None:
    body = "첫 문장입니다.\n안녕하세요 고객님.\n감사합니다.\n마지막 문장입니다."
    assert extract_answer_body(format_final_answer(body)) == body


def _learning(repository: LearningRepository, inquiry_id: int, key: str, *, human: bool) -> None:
    repository.upsert(
        {
            "source_key": key,
            "inquiry_id": inquiry_id,
            "learning_source": "SELLER_ANSWER",
            "question_original_masked": key,
            "question_normalized": key,
            "store_code": "OJE_PLUS",
            "inquiry_type": "CUSTOMER_INQUIRY",
            "intent": "GENERAL",
            "final_answer": "재사용 가능한 안내입니다.",
            "rating": 5,
            "edit_ratio": 0.0,
            "quality_score": 1.0,
            "style_only": False,
            "version": 1,
            "metadata_json": {
                "learning_signal_type": "POSITIVE",
                "human_verified": human,
            },
            "active": True,
        }
    )


def test_learning_lifecycle_and_dashboard_filter_are_batch_resolved(tmp_path: Path) -> None:
    database = Database(tmp_path / "lifecycle.db")
    database.initialize()
    inquiries = InquiryRepository(database)
    ids = {}
    for index, status in enumerate(("APPROVED", "AUTO", "EXCLUDED", "CORRECTED", "NONE")):
        ids[status] = inquiries.upsert_work_item(
            {
                "store_code": "OJE_PLUS",
                "source_type": "CUSTOMER_INQUIRY",
                "source_question_id": f"LIFECYCLE-{status}",
                "inquiry_type": "CUSTOMER_INQUIRY",
                "content": f"lifecycle {status}",
                "registered_at": f"2026-08-1{index}T09:00:00+09:00",
                "raw_json": {"queue": "AUTO_PROCESSABLE", "priority": "MEDIUM"},
            }
        ).inquiry_id
    learning = LearningRepository(database)
    _learning(learning, ids["APPROVED"], "approved", human=True)
    _learning(learning, ids["AUTO"], "automatic", human=False)
    with database.transaction() as connection:
        for status, signal in (("EXCLUDED", "EXCLUDED"), ("CORRECTED", "INTENT_CORRECTION")):
            connection.execute(
                """
                INSERT INTO learning_feedback(
                    source_key, feedback_type, correction_reason,
                    learning_signal_type, source, inquiry_id, metadata_json
                ) VALUES (?, 'STAFF_CORRECTION', 'OTHER', ?, 'TEST', ?, '{}')
                """,
                (f"feedback-{status}", signal, ids[status]),
            )

    states = inquiries.learning_states()
    assert {status: states[value]["learning_status"] for status, value in ids.items()} == {
        status: status for status in ids
    }
    for status in ids:
        rows, total, pages = inquiries.dashboard_page(
            store_codes=["OJE_PLUS"], source="ALL",
            queues=["AUTO_PROCESSABLE"], priorities=["MEDIUM"],
            answer_status="ALL", delivery_only=False, search_query="lifecycle",
            start_date="2026-08-01", end_date="2026-08-31",
            kpi_filter=None, page=1, page_size=10, learning_status=status,
        )
        assert total == pages == 1
        assert rows[0]["id"] == ids[status]


def test_lifecycle_can_show_approved_and_correction_without_old_state_spam() -> None:
    result = resolve_learning_lifecycle(
        {"has_approved": 1, "has_auto": 1, "has_excluded": 1, "has_corrected": 1}
    )
    assert result["learning_statuses"] == ["APPROVED", "CORRECTED"]
    assert result["learning_labels"] == ["승인", "교정"]


def test_selected_prompt_learning_is_counted_only_after_actual_use(tmp_path: Path) -> None:
    database = Database(tmp_path / "actual-usage.db")
    database.initialize()
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "ACTUAL-USAGE",
            "inquiry_type": "CUSTOMER_INQUIRY",
            "content": "정책 문의",
            "registered_at": "2026-08-20T09:00:00+09:00",
            "raw_json": {"queue": "AUTO_PROCESSABLE", "priority": "MEDIUM"},
        }
    ).inquiry_id
    learning = LearningRepository(database)
    _learning(learning, inquiry_id, "used-learning", human=True)
    _learning(learning, inquiry_id, "unused-learning", human=False)
    rows = learning.candidates(store_code="OJE_PLUS")
    by_key = {str(row["source_key"]): row for row in rows}
    provenance = LearningProvenanceRepository(database)
    provenance.record_context(
        inquiry_id=inquiry_id,
        learning=[
            {
                "learning_example_id": int(row["id"]),
                "learning_source": row["learning_source"],
                "relevance": 0.9,
            }
            for row in rows
        ],
        historical=[],
    )
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="POLICY",
            reason="evidence-backed",
            answer="검증된 정책에 따라 안내드립니다.",
            provider="fake_gpt_hybrid",
            auto_answerable=True,
            needs_review=False,
            metadata={
                "hybrid": {
                    "fallback_used": False,
                    "validation": {"passed": True, "status": "PASSED"},
                    "draft": {
                        "learning_usage": [
                            {
                                "learning_id": int(by_key["used-learning"]["id"]),
                                "answer_supported": True,
                                "matched_subquestion": "정책 문의",
                                "reason": "VERIFIED_POLICY_USED",
                            },
                            {
                                "learning_id": int(by_key["unused-learning"]["id"]),
                                "answer_supported": False,
                                "matched_subquestion": "정책 문의",
                                "reason": "LOW_CONFIDENCE",
                            },
                        ]
                    },
                }
            },
        ),
    )
    outcomes = provenance.for_draft(int(draft["id"]))
    statuses = {int(row["learning_example_id"]): row["usage_status"] for row in outcomes}
    assert statuses[int(by_key["used-learning"]["id"])] == "USED"
    assert statuses[int(by_key["unused-learning"]["id"])] == "NOT_USED"
    refreshed = {str(row["source_key"]): row for row in learning.candidates(store_code="OJE_PLUS")}
    assert refreshed["used-learning"]["usage_count"] == 1
    assert refreshed["unused-learning"]["usage_count"] == 0


@pytest.mark.parametrize(
    ("question", "answer", "expected"),
    (
        (
            "반품 박스를 기사님이 회수해 직접 반품할 수 없습니다.",
            "OTT 기능은 셋탑박스를 연결해야 하며 단순변심 반품은 어렵습니다.",
            "QUESTION_ANSWER_MISMATCH",
        ),
        (
            "TV 스탠드 반품 절차를 알려주세요.",
            "[8/3~8/4] 하계 휴가 기간입니다. 스탠드 개봉상태를 확인해 주세요.",
            "TEMPORARY_OR_EXPIRED",
        ),
        (
            "배송 완료인데 받지 못했습니다. 언제 오나요?",
            "스탠드 발송이 누락되어 익일 출고하겠습니다.",
            "ORDER_SPECIFIC",
        ),
    ),
)
def test_135_166_183_bad_historical_answers_are_retrieved_then_rejected(
    tmp_path: Path, question: str, answer: str, expected: str
) -> None:
    database = Database(tmp_path / f"historical-{expected}.db")
    database.initialize()
    service = HistoricalCaseService(database)
    prepared = service.prepare_case(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "external_inquiry_id": expected,
            "content": question,
            "seller_answer": answer,
            "answered": True,
            "source_created_at": "2026-08-01T09:00:00+09:00",
        },
        source_reference="REGRESSION",
    )
    saved, _ = HistoricalCaseRepository(database).upsert(prepared)
    trace = service.search_detailed(question, store_code="OJE_PLUS")
    assert trace["candidate_count"] == 1
    assert trace["selected"] == []
    assert trace["rejection_counts"][expected] == 1
    assert trace["rejected_samples"][0]["historical_case_id"] == saved["id"]


class _Tab:
    element_info = type("ElementInfo", (), {"control_type": "TabItem"})()

    def __init__(self, title: str, exists: bool = True) -> None:
        self.title = title
        self._exists = exists

    def exists(self, timeout: int = 0) -> bool:
        return self._exists


def test_dps_cached_tab_handle_fast_path_avoids_descendant_scan(monkeypatch) -> None:
    manager = ChromeTabManager(desktop_factory=None)
    tab = _Tab("Samsung DPS 2.0")
    window = object()
    monkeypatch.setattr(manager, "is_window", lambda _: True)
    monkeypatch.setattr(manager, "window_from_handle", lambda _: window)
    monkeypatch.setattr(manager, "element_name", lambda item: item.title)
    monkeypatch.setattr(manager, "window_title", lambda _: "Samsung DPS 2.0 - Chrome")
    monkeypatch.setattr(
        manager, "tabs_in_window",
        lambda _: (_ for _ in ()).throw(AssertionError("full scan used")),
    )
    candidate = manager.candidate_for_connection(
        RuntimeConnection(1, "Chrome", tab.title, "now", tab, "https://dps2u.co.kr")
    )
    assert candidate is not None
    assert candidate.discovery_mode == "CACHED_HANDLE_FAST_PATH"


def test_dps_stale_cached_tab_safely_falls_back_to_full_scan(monkeypatch) -> None:
    manager = ChromeTabManager(desktop_factory=None)
    stale = _Tab("Samsung DPS 2.0", exists=False)
    fresh = _Tab("Samsung DPS 2.0")
    window = object()
    monkeypatch.setattr(manager, "is_window", lambda _: True)
    monkeypatch.setattr(manager, "window_from_handle", lambda _: window)
    monkeypatch.setattr(manager, "element_name", lambda item: item.title)
    monkeypatch.setattr(manager, "window_title", lambda _: "Samsung DPS 2.0 - Chrome")
    monkeypatch.setattr(manager, "tabs_in_window", lambda _: [fresh])
    monkeypatch.setattr(manager, "is_tab_selected", lambda _: True)
    candidate = manager.candidate_for_connection(
        RuntimeConnection(1, "Chrome", stale.title, "now", stale, "https://dps2u.co.kr")
    )
    assert candidate is not None
    assert candidate.tab is fresh
    assert candidate.discovery_mode == "FULL_DISCOVERY"
