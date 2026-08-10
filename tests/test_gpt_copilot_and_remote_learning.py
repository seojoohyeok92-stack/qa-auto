from __future__ import annotations

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.gpt_chat_repository import GptChatRepository
from repositories.inquiry_repository import InquiryRepository
from repositories.post_review_repository import PostReviewRepository
from services.gpt_copilot_service import GptCopilotService
from services.learning_service import LearningService


def _inquiry(database: Database) -> int:
    result = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "remote-edit-1",
            "external_inquiry_id": "remote-edit-1",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "배송문의",
            "content": "언제 배송되나요?",
            "product_id": "p1",
            "product_name": "테스트 상품",
            "option_name": None,
            "customer_display": None,
            "masked_writer_id": None,
            "order_id": "2026080712345678",
            "product_order_id": None,
            "registered_at": "2026-08-07T01:00:00Z",
            "source_answered": False,
            "source_status": "WAITING",
            "source_created_at": "2026-08-07T01:00:00Z",
            "source_updated_at": "2026-08-07T01:00:00Z",
            "is_private": False,
            "source_metadata_json": {},
            "workflow_status": "NEW",
            "answer_status": "UNANSWERED",
            "post_status": "NOT_POSTED",
            "raw_json": {},
        }
    )
    return result.inquiry_id


def test_remote_naver_edit_becomes_corrected_learning(tmp_path):
    database = Database(tmp_path / "test.db")
    database.initialize()
    inquiry_id = _inquiry(database)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="배송",
            reason="test",
            answer="기존 자동 답변입니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            metadata={"selected_answer_route": "SAFE_RULE"},
        ),
    )
    reviews = PostReviewRepository(database)
    version, _ = reviews.finalize_auto(
        inquiry_id=inquiry_id, draft_id=int(draft["id"]), run_id="run-1"
    )
    reviews.create_review_after_post(
        inquiry_id=inquiry_id,
        draft_id=int(draft["id"]),
        version_id=int(version["id"]),
        run_id="run-1",
        route="SAFE_RULE",
        needs_staff_review=False,
        posted_at="2026-08-07T01:10:00Z",
    )

    remote, changed = reviews.capture_remote_naver_edit(
        inquiry_id=inquiry_id,
        answer_body="네이버에서 직원이 직접 수정한 최종 답변입니다.",
    )
    assert changed is True
    assert remote is not None
    assert remote["version_kind"] == "NAVER_CORRECTION_APPLIED"
    assert remote["finalization_source"] == "NAVER_DIRECT_EDIT_SYNC"

    saved = LearningService(database).capture_auto_post_version(
        inquiry_id=inquiry_id,
        version_id=int(remote["id"]),
        source="AUTO_POST_CORRECTED",
    )
    assert saved is not None
    assert saved["learning_source"] == "AUTO_POST_CORRECTED"
    assert saved["final_answer"] == "네이버에서 직원이 직접 수정한 최종 답변입니다."
    assert "기존 자동 답변입니다." in saved["metadata_json"]["original_auto_post_answer"]
    assert saved["metadata_json"]["staff_edited_final_answer"] == "네이버에서 직원이 직접 수정한 최종 답변입니다."
    assert saved["metadata_json"]["edit_detected_at"]

    same, changed_again = reviews.capture_remote_naver_edit(
        inquiry_id=inquiry_id,
        answer_body="네이버에서 직원이 직접 수정한 최종 답변입니다.",
    )
    assert changed_again is False
    assert same is not None
    assert int(same["id"]) == int(remote["id"])


def test_gpt_copilot_saves_chat_and_uses_project_knowledge(tmp_path, monkeypatch):
    database = Database(tmp_path / "test.db")
    database.initialize()
    monkeypatch.setenv("QNA_GPT_PROVIDER", "openai")
    monkeypatch.setenv("QNA_GPT_MODE", "ACTIVE")
    monkeypatch.setenv("QNA_GPT_MODEL", "test-model")
    monkeypatch.setenv("QNA_GPT_ALLOWED_MODELS", "test-model")
    monkeypatch.setenv("QNA_GPT_COMPANY_APPROVED", "true")
    monkeypatch.setenv("QNA_GPT_ENABLED", "true")
    monkeypatch.setenv("QNA_GPT_PRIVACY_ENABLED", "true")
    monkeypatch.setenv("QNA_GPT_API_KEY", "dummy-key")

    captured = {}

    def fake_transport(**kwargs):
        captured.update(kwargs)
        return "프로젝트 지식을 참고한 테스트 응답입니다."

    service = GptCopilotService(database, transport=fake_transport)
    assert service.status()["ready"] is True
    assert service.status()["knowledge_count"] > 0
    session = GptChatRepository(database).create_session(user_name="tester")
    result = service.ask(
        session_id=int(session["id"]),
        message="Auto Post 안전 조건을 설명해줘",
        include_inquiry=False,
        include_learning=False,
        include_knowledge=True,
        include_past_chats=True,
    )
    assert result["status"] == "SUCCESS"
    assert "테스트 응답" in result["answer"]
    assert "Project Knowledge" in captured["messages"][-1]["content"] or "project_knowledge" in captured["messages"][-1]["content"]
    messages = GptChatRepository(database).messages(int(session["id"]))
    assert [row["role"] for row in messages] == ["user", "assistant"]
