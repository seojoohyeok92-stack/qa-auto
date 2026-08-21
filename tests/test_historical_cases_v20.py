from __future__ import annotations

import json
from datetime import UTC, datetime

from repositories.database import Database
from repositories.historical_case_repository import HistoricalCaseRepository
from repositories.learning_repository import LearningRepository
from services.gpt_chat_import_service import GptChatImportService
from services.gpt_copilot_service import GptCopilotService
from services.historical_case_service import HistoricalCaseService
from services.learning_context_service import LearningContextService
from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from streamlit.testing.v1 import AppTest


def _database(tmp_path) -> Database:
    database = Database(tmp_path / "historical.db")
    database.initialize()
    return database


def _insert_inquiry(database: Database, external_id: str, *, answer: str = "") -> int:
    raw = {"questionId": external_id, "sellerAnswer": answer, "answered": bool(answer)}
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO inquiries(
                store_code, source_type, source_question_id, inquiry_type,
                title, content, product_name, registered_at, source_created_at,
                source_updated_at, source_answered, answer_status, raw_json
            ) VALUES (
                'OJE_PLUS', 'PRODUCT_INQUIRY', ?, 'PRODUCT_INQUIRY',
                '배송 문의', '언제 설치되나요?', '테스트 상품',
                '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00',
                '2026-07-02T00:00:00+00:00', ?, ?, ?
            )
            """,
            (external_id, int(bool(answer)), "ANSWERED" if answer else "UNANSWERED", json.dumps(raw, ensure_ascii=False)),
        )
    return int(cursor.lastrowid)


def test_local_import_is_idempotent_versioned_and_never_enqueues_or_posts(tmp_path) -> None:
    database = _database(tmp_path)
    inquiry_id = _insert_inquiry(database, "history-1", answer="현재 확인되는 설치예정일을 안내드립니다.")
    _insert_inquiry(database, "history-no-answer")
    service = HistoricalCaseService(database)
    with database.connection() as connection:
        events_before = connection.execute("SELECT COUNT(*) FROM auto_sync_events").fetchone()[0]
        posts_before = connection.execute("SELECT COUNT(*) FROM naver_post_attempts").fetchone()[0]
        status_before = connection.execute("SELECT post_status FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()[0]

    first = service.import_local(require_seller_answer=True, answered_only=True, batch_size=10)
    assert first["inserted_count"] == 1
    assert first["no_answer_count"] == 1
    second = service.import_local(require_seller_answer=True, answered_only=True, batch_size=10)
    assert second["inserted_count"] == 0
    assert second["duplicate_count"] == 1

    with database.transaction() as connection:
        raw = {"questionId": "history-1", "sellerAnswer": "설치예정일은 변경될 수 있어 현재 주문 정보 확인이 필요합니다.", "answered": True}
        connection.execute("UPDATE inquiries SET raw_json=? WHERE id=?", (json.dumps(raw, ensure_ascii=False), inquiry_id))
    changed = service.import_local(require_seller_answer=True, answered_only=True, batch_size=10)
    assert changed["updated_count"] == 1
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM historical_cases").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM historical_case_versions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM auto_sync_events").fetchone()[0] == events_before
        assert connection.execute("SELECT COUNT(*) FROM naver_post_attempts").fetchone()[0] == posts_before
        assert connection.execute("SELECT post_status FROM inquiries WHERE id=?", (inquiry_id,)).fetchone()[0] == status_before


def test_quality_blocks_stale_fact_from_safe_search_and_promotion_is_deduplicated(tmp_path) -> None:
    database = _database(tmp_path)
    service = HistoricalCaseService(database)
    risky = service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "risk-1", "title": "배송", "content": "언제 오나요?",
        "seller_answer": "반드시 내일 배송됩니다. 가격은 10,000원입니다.",
        "answered": True, "source_created_at": "2020-01-01T00:00:00+00:00",
    }, source_reference="TEST:risk")
    risky_row, _ = service.repository.upsert(risky)
    assert risky["policy_risk"] in {"HIGH", "BLOCKED"}
    assert risky["active"] is True
    assert risky_row["learning_enabled"] is True
    assert service.search("배송 언제 오나요", store_code="OJE_PLUS") == []
    assert service.search(
        "배송 언제 오나요", store_code="OJE_PLUS", include_risky=True,
    ) == []

    safe = service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "safe-1", "title": "제품 사용 문의", "content": "사용법이 궁금해요",
        "seller_answer": "제품 설명서의 순서에 따라 사용해 주세요. 어려움이 있으면 다시 문의해 주세요.",
        "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="TEST:safe")
    row, _ = service.repository.upsert(safe)
    promoted = service.promote(int(row["id"]), actor="tester")
    repeated = service.promote(int(row["id"]), actor="tester")
    assert promoted["id"] == repeated["id"]
    assert promoted["metadata_json"]["source_origin"] == "HISTORICAL_PROMOTED"


def test_chat_export_import_is_persistent_searchable_and_idempotent(tmp_path) -> None:
    database = _database(tmp_path)
    payload = [{
        "title": "장애 해결 기록",
        "mapping": {
            "a": {"message": {"create_time": 1, "author": {"role": "user"}, "content": {"parts": ["자동등록이 멈췄어요"]}}},
            "b": {"message": {"create_time": 2, "author": {"role": "assistant"}, "content": {"parts": ["자동등록 스위치와 최근 처리 상태를 확인하세요."]}}},
        },
    }]
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    importer = GptChatImportService(database)
    first = importer.import_bytes(file_name="conversations.json", raw=raw, user_name="tester")
    second = importer.import_bytes(file_name="conversations.json", raw=raw, user_name="tester")
    assert first["sessions_created"] == 1
    assert first["messages_created"] == 2
    assert second["duplicate"] is True
    assert importer.chats.search_messages("자동등록 스위치")
    assert importer.knowledge.search("장애 자동등록")


def test_gpt_context_has_safe_historical_priority_and_plain_language_prompt(tmp_path) -> None:
    database = _database(tmp_path)
    service = HistoricalCaseService(database)
    case = service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "context-1", "title": "사용법", "content": "어떻게 쓰나요",
        "seller_answer": "설명서 순서에 따라 사용해 주세요.", "answered": True,
        "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="TEST:context")
    service.repository.upsert(case)
    assert case.get("promoted_learning_id") is None
    copilot = GptCopilotService(database)
    context = copilot._historical_context("사용법 어떻게 쓰나요")
    assert context and context[0]["usage_notice"].startswith("과거 표현")
    prompt = copilot._system_prompt()
    assert "자동처리 대기 중" in prompt
    assert "## 기술 정보" in prompt
    assert "현재 Rule/안전정책" in prompt


def test_unreviewed_case_is_automatic_answer_context_but_low_blocked_are_excluded(tmp_path) -> None:
    database = _database(tmp_path)
    inquiry_id = _insert_inquiry(database, "current-question")
    service = HistoricalCaseService(database)
    safe = service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "auto-context-safe", "title": "제품 사용 문의",
        "content": "사용 방법을 알려주세요",
        "seller_answer": "제품 설명서의 순서에 따라 사용해 주세요. 연결 환경을 확인하면 더 정확히 안내할 수 있습니다.",
        "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="TEST:auto-safe")
    safe["inquiry_id"] = inquiry_id
    safe_row, _ = service.repository.upsert(safe)
    assert safe_row["promoted_learning_id"] is None

    low = service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "auto-context-low", "title": "제품 사용 문의",
        "content": "사용 방법을 알려주세요", "seller_answer": "네",
        "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="TEST:auto-low")
    assert low["quality_score"] < 0.50
    service.repository.upsert(low)

    blocked = dict(safe)
    blocked.update({
        "external_inquiry_id": "auto-context-blocked",
        "case_key": service._digest("blocked-case"),
        "fingerprint": service._digest("blocked-case", "answer"),
        "policy_risk": "BLOCK",
    })
    service.repository.upsert(blocked)

    context = LearningContextService(database).build(
        AnswerFacts(
            inquiry={"inquiry_id": inquiry_id, "question": "제품 사용 방법을 알려주세요", "type": "PRODUCT_INQUIRY"},
            product={"name": "테스트 상품"},
        ),
        IntentResult("GENERAL", ("사용 방법",), Emotion.NORMAL, "NORMAL", 0.9, False, ""),
    )
    historical = context["historical_cases"]
    assert len(historical) == 1
    assert "설명서의 순서" in historical[0]["answer_style_reference"]
    assert context["historical_case_policy"]["current_authority_order"][:3] == [
        "RULE_AND_SAFETY", "CURRENT_ORDER", "CURRENT_DPS"
    ]
    with database.connection() as connection:
        provenance = connection.execute(
            """
            SELECT historical_case_id, source_label, included_in_prompt
            FROM answer_learning_provenance WHERE reference_kind='HISTORICAL'
            """
        ).fetchall()
    assert [(row[0], row[1], row[2]) for row in provenance] == [
        (safe_row["id"], "HISTORICAL_VERIFIED_LEARNING", 1)
    ]


def test_auto_reference_can_be_disabled_and_reenabled_without_learning_promotion(tmp_path) -> None:
    database = _database(tmp_path)
    service = HistoricalCaseService(database)
    case = service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "CUSTOMER_INQUIRY",
        "external_inquiry_id": "active-toggle", "title": "사용 문의",
        "content": "연결 방법이 궁금합니다",
        "seller_answer": "연결할 기기의 종류를 확인한 뒤 설명서의 연결 순서대로 진행해 주세요.",
        "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="TEST:toggle")
    row, _ = service.repository.upsert(case)
    assert service.search("연결 방법이 궁금합니다", store_code="OJE_PLUS")
    service.repository.set_learning_enabled(
        int(row["id"]), False, reason="오래된 정책", actor="tester",
    )
    assert service.search("연결 방법이 궁금합니다", store_code="OJE_PLUS") == []
    assert GptCopilotService(database)._historical_context("연결 방법이 궁금합니다") == []
    excluded = service.repository.get(int(row["id"]))
    assert excluded["learning_enabled"] is False
    assert excluded["metadata_json"]["learning_exclusion_reason"] == "오래된 정책"

    # A later import/update cannot silently undo an explicit opt-out.
    changed = dict(case)
    changed["seller_answer"] = "변경된 답변이지만 관리자의 제외 상태는 유지되어야 합니다."
    changed["fingerprint"] = service._digest(changed["case_key"], changed["seller_answer"])
    refreshed, outcome = service.repository.upsert(changed)
    assert outcome == "updated"
    assert refreshed["learning_enabled"] is False
    assert refreshed["metadata_json"]["learning_exclusion_reason"] == "오래된 정책"

    service.repository.set_learning_enabled(int(row["id"]), True, actor="tester")
    assert service.search("연결 방법이 궁금합니다", store_code="OJE_PLUS")
    assert GptCopilotService(database)._historical_context("연결 방법이 궁금합니다")
    reenabled = service.repository.get(int(row["id"]))
    assert reenabled["learning_enabled"] is True
    assert "learning_exclusion_reason" not in reenabled["metadata_json"]
    assert reenabled["promoted_learning_id"] is None


def test_migration_20_integrity(tmp_path) -> None:
    database = _database(tmp_path)
    assert max(database.migration_versions()) == 28
    with database.connection() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert HistoricalCaseRepository(database).count() == 0


def test_historical_manager_apptest_shows_auto_reference_controls(tmp_path) -> None:
    database = _database(tmp_path)
    service = HistoricalCaseService(database)
    case = service.prepare_case({
        "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "ui-case", "title": "사용 문의", "content": "연결 방법",
        "seller_answer": "기기 종류를 확인한 뒤 설명서의 연결 순서대로 진행해 주세요.",
        "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="TEST:ui")
    service.repository.upsert(case)
    app = AppTest.from_string(
        f'''
from repositories.database import Database
from ui.historical_case_manager import render_historical_case_manager
db=Database(r"{database.path}")
db.initialize()
render_historical_case_manager(db)
'''
    ).run(timeout=30)
    assert not app.exception
    assert {item.label for item in app.metric} >= {
        "전체 사례", "Context 사용 가능", "검증 Learning", "학습 제외",
        "기존 승격 보존", "최근 Import"
    }
    assert {item.label for item in app.button} >= {
        "기본 검증 Learning", "학습 제외"
    }
    assert next(
        item for item in app.button if item.label == "기본 검증 Learning"
    ).disabled


def test_historical_manager_opt_out_keeps_page_filters_selection_and_allows_next(tmp_path) -> None:
    database = _database(tmp_path)
    service = HistoricalCaseService(database)
    for external_id, content in (("ux-a", "연결 방법 A"), ("ux-b", "연결 방법 B")):
        case = service.prepare_case({
            "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
            "external_inquiry_id": external_id, "title": "사용 문의", "content": content,
            "seller_answer": "기기 종류를 확인한 뒤 설명서의 연결 순서대로 진행해 주세요.",
            "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
        }, source_reference=f"TEST:{external_id}")
        service.repository.upsert(case)

    app = AppTest.from_string(
        f'''
import streamlit as st
from repositories.database import Database
from ui.historical_case_manager import render_historical_case_manager
st.session_state.setdefault("current_page", "historical")
st.session_state.setdefault("production_admin_mode", True)
if st.session_state.get("current_page") == "historical":
    db=Database(r"{database.path}")
    db.initialize()
    render_historical_case_manager(db)
else:
    st.title("Dashboard home")
'''
    ).run(timeout=30)
    assert not app.exception
    search = next(item for item in app.text_input if item.label == "검색")
    search.set_value("연결 방법")
    next(
        item for item in app.selectbox
        if item.key == "historical_manage_store"
    ).set_value("OJE_PLUS")
    next(
        item for item in app.selectbox
        if item.key == "historical_manage_type"
    ).set_value("PRODUCT_INQUIRY")
    next(
        item for item in app.selectbox
        if item.key == "historical_risk_filter"
    ).set_value("NONE")
    next(
        item for item in app.slider
        if item.key == "historical_min_quality"
    ).set_value(0.50)
    app = app.run(timeout=30)
    detail = next(item for item in app.selectbox if item.label == "상세 사례")
    first_label, second_label = detail.options[:2]
    first_id, second_id = [
        int(str(value).split("#", 1)[1].split(" ", 1)[0])
        for value in (first_label, second_label)
    ]
    detail.set_value(first_label)
    app = app.run(timeout=30)

    with database.connection() as connection:
        learning_before = connection.execute(
            "SELECT COUNT(*) FROM learning_examples"
        ).fetchone()[0]
    assert not app.exception
    assert app.session_state["current_page"] == "historical"
    assert app.session_state["production_admin_mode"] is True
    assert app.session_state["historical_selected_case_id"] == first_id
    assert app.session_state["historical_active_section"] == "case_manager"
    assert app.session_state["historical_filter_state"]["historical_search"] == "연결 방법"
    assert app.session_state["historical_filter_state"]["historical_manage_store"] == "OJE_PLUS"
    assert app.session_state["historical_filter_state"]["historical_manage_type"] == "PRODUCT_INQUIRY"
    assert app.session_state["historical_filter_state"]["historical_risk_filter"] == "NONE"
    assert app.session_state["historical_filter_state"]["historical_min_quality"] == 0.50
    assert "historical_manage_start" in app.session_state["historical_filter_state"]
    assert "historical_manage_end" in app.session_state["historical_filter_state"]
    assert next(item for item in app.text_input if item.label == "검색").value == "연결 방법"
    verified_button = next(
        item for item in app.button if item.label == "기본 검증 Learning"
    )
    assert verified_button.disabled is True
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples"
        ).fetchone()[0] == learning_before
    assert HistoricalCaseRepository(database).get(first_id)["active"] is True
    assert next(
        item for item in app.metric if item.label == "검증 Learning"
    ).value == "2"

    next(item for item in app.button if item.label == "다음 사례").click()
    app = app.run(timeout=30)
    assert not app.exception
    assert app.session_state["current_page"] == "historical"
    assert app.session_state["historical_selected_case_id"] == second_id
    assert str(second_id) in str(
        next(item for item in app.selectbox if item.label == "상세 사례").value
    )
    assert not any("Dashboard home" in str(item.value) for item in app.title)

    next(item for item in app.button if item.label == "학습 제외").click()
    app = app.run(timeout=30)
    assert app.session_state["current_page"] == "historical"
    assert app.session_state["historical_selected_case_id"] == second_id
    assert HistoricalCaseRepository(database).get(second_id)["active"] is False
    assert next(item for item in app.text_input if item.label == "검색").value == "연결 방법"
    next(item for item in app.button if item.label == "학습 다시 사용").click()
    app = app.run(timeout=30)
    assert app.session_state["current_page"] == "historical"
    assert app.session_state["historical_selected_case_id"] == second_id
    assert HistoricalCaseRepository(database).get(second_id)["active"] is True


def test_copilot_technical_information_is_collapsed_in_apptest() -> None:
    app = AppTest.from_string(
        '''
from ui.gpt_copilot import _render_assistant_message
content = chr(10).join(["## 결론", "자동처리 대기 중입니다.", "", "## 기술 정보", "PENDING / Event"])
_render_assistant_message(content)
'''
    ).run(timeout=30)
    assert not app.exception
    technical = next(item for item in app.expander if item.label == "기술 정보")
    assert technical.proto.expanded is False
    assert any("자동처리 대기 중" in str(item.value) for item in app.markdown)
