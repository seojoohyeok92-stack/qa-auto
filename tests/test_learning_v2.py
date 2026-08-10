from __future__ import annotations

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services.learning_privacy_service import LearningPrivacyService
from services.learning_quality_service import LearningQualityService
from services.learning_service import LearningService
from services.similar_answer_service import SimilarAnswerService


def make_database(tmp_path) -> Database:
    database = Database(tmp_path / "learning.db")
    database.initialize()
    return database


def make_inquiry(database: Database, *, raw_json=None, answered=False) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "LEARN-1",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "TV 사용 문의",
            "content": "tv로도 사용하려면 어떻게 해야 하나요? 주문번호 2026070448206811",
            "product_name": "삼성 스마트모니터 M7",
            "customer_display": "홍길동",
            "source_answered": answered,
            "post_status": "POSTED" if answered else "NOT_POSTED",
            "raw_json": raw_json or {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


def make_draft(database: Database, inquiry_id: int) -> dict:
    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="제품 기능",
            reason="GPT fallback",
            answer="안녕하세요, 고객님. HDMI로 연결해 TV처럼 이용할 수 있습니다. 감사합니다.",
            provider="openai",
            auto_answerable=True,
            needs_review=False,
            metadata={"generation_mode": "GPT_FALLBACK", "selected_answer_route": "GPT_FALLBACK"},
        ),
    )


def test_privacy_masks_learning_fields() -> None:
    masked = LearningPrivacyService().mask(
        "홍길동 010-1234-5678 a@b.com 서울 강남구 테헤란로 123 2026070448206811",
        customer_names=["홍길동"],
    )
    assert "홍길동" not in masked
    assert "010-1234-5678" not in masked
    assert "a@b.com" not in masked
    assert "2026070448206811" not in masked
    assert "테헤란로 123" not in masked


def test_quality_priority_and_edit_ratio() -> None:
    service = LearningQualityService()
    assert service.score("APPROVED_UNEDITED", "같음", "같음").rating == 4
    assert service.score("SELLER_ANSWER", "", "판매자 답변").rating == 3
    minimal = service.score("APPROVED_EDITED", "안녕하세요 고객님", "안녕하세요, 고객님")
    rewritten = service.score("APPROVED_EDITED", "짧은 답", "완전히 새롭게 길게 작성한 직원 답변입니다")
    assert minimal.rating > rewritten.rating
    assert minimal.edit_ratio < rewritten.edit_ratio


def test_approved_final_is_saved_deduplicated_and_cancelled_is_inactive(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    draft = make_draft(database, inquiry_id)
    approval = ApprovalService(database)
    approval.save_edited_answer(
        inquiry_id=inquiry_id, draft_id=draft["id"],
        edited_answer="안녕하세요, 고객님. HDMI 연결 후 입력 소스를 선택해 이용해 주세요. 감사합니다.",
    )
    approval.approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    repository = LearningRepository(database)
    assert repository.count() == 1
    item = repository.candidates(store_code="OJE_PLUS")[0]
    assert item["learning_source"] == "APPROVED_EDITED"
    assert "2026070448206811" not in item["question_original_masked"]
    LearningService(database).import_existing_approved()
    assert repository.count() == 1
    approval.cancel_approval(inquiry_id=inquiry_id, draft_id=draft["id"], reason="재검토")
    assert repository.candidates(store_code="OJE_PLUS") == []


def test_existing_seller_answer_is_style_only_and_stale_policy_is_excluded(tmp_path) -> None:
    database = make_database(tmp_path)
    make_inquiry(database, answered=True, raw_json={"commentContent": "안녕하세요, 고객님. 확인 후 안내드리겠습니다. 감사합니다."})
    result = LearningService(database).import_existing_seller_answers()
    assert result["saved"] == 1
    item = LearningRepository(database).candidates(store_code="OJE_PLUS")[0]
    assert item["learning_source"] == "SELLER_ANSWER"
    assert item["style_only"] is True


def test_similar_search_limits_results_and_separates_factual_authority(tmp_path) -> None:
    database = make_database(tmp_path)
    inquiry_id = make_inquiry(database)
    draft = make_draft(database, inquiry_id)
    ApprovalService(database).approve(inquiry_id=inquiry_id, draft_id=draft["id"])
    context = SimilarAnswerService(LearningRepository(database)).context(
        "TV로 사용하려면 어떻게 하나요?", store_code="OJE_PLUS",
        inquiry_type="PRODUCT_INQUIRY", minimum_relevance=0.1,
    )
    assert len(context["similar_approved_answers"]) <= 3
    assert context["similar_approved_answers"]
    assert context["oje_style_rules"]["seller_examples_are_style_only"] is True
    assert context["oje_style_rules"]["facts_priority"][0] == "PRODUCT_DB"


def test_similar_search_prefers_approved_then_legacy_rule_then_legacy_gpt(tmp_path) -> None:
    database = make_database(tmp_path)
    repository = LearningRepository(database)
    base = {
        "inquiry_id": None, "answer_draft_id": None, "approval_history_id": None,
        "question_original_masked": "TV 사용 문의", "question_normalized": "tv 사용 문의",
        "store_code": "OJE_PLUS", "inquiry_type": "PRODUCT_INQUIRY", "intent": "GENERAL",
        "product_name": None, "model_code": None, "generation_mode": "TEST",
        "template_id": None, "processing_route": "TEST", "validator_result": "PASSED",
        "seller_answer": None, "gpt_draft": None, "edited_answer": None,
        "posted": True, "posted_at": None, "auto_posted": False, "edit_ratio": 0.0,
        "style_only": False, "version": 1, "style_features_json": {}, "active": True,
    }
    for key, source, legacy, rating in (
        ("edited", "APPROVED_EDITED", "", 3),
        ("final", "APPROVED_UNEDITED", "", 4),
        ("rule", "SELLER_ANSWER", "LEGACY_RULE", 4),
        ("gpt", "SELLER_ANSWER", "LEGACY_GPT", 3),
    ):
        repository.upsert({
            **base, "source_key": key, "learning_source": source,
            "final_answer": key, "rating": rating, "quality_score": rating / 5,
            "style_only": source == "SELLER_ANSWER",
            "seller_answer": key if source == "SELLER_ANSWER" else None,
            "metadata_json": {"legacy_source": legacy} if legacy else {},
        })
    found = SimilarAnswerService(repository).search(
        "TV 사용 문의", store_code="OJE_PLUS", minimum_relevance=0.1, limit=3,
    )
    assert [item["final_answer"] for item in found] == ["edited", "final", "rule"]


def test_learning_failure_never_blocks_gpt_context() -> None:
    from answer.facts import AnswerFacts
    from answer.hybrid_models import Emotion, IntentResult
    from services.draft_generation_service import DraftGenerationService

    class Provider:
        def generate_json(self, *, task, prompt, context):
            assert "learning" not in str(context).lower()
            return {"answer": "안전 답변", "confidence": 0.8, "used_facts": [], "missing_information": [], "requires_review": False, "warnings": []}

    service = DraftGenerationService(Provider(), learning_context_provider=lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
    result = service.generate(
        AnswerFacts(inquiry={"question": "일반 문의"}),
        IntentResult("GENERAL", ("일반 문의",), Emotion.NORMAL, "NORMAL", 0.9, False, ""),
    )
    assert result.answer == "안전 답변"


def test_gpt_prompt_receives_approved_style_context_in_declared_priority() -> None:
    import json
    from answer.facts import AnswerFacts
    from answer.hybrid_models import Emotion, IntentResult
    from services.draft_generation_service import DraftGenerationService

    captured = {}

    class Provider:
        def generate_json(self, *, task, prompt, context):
            captured["prompt"] = json.loads(prompt)
            captured["context"] = context
            return {"answer": "문체가 적용된 답변", "confidence": 0.9, "used_facts": [], "missing_information": [], "requires_review": False, "warnings": []}

    learning = {
        "similar_approved_answers": [{"question": "TV 사용법", "answer": "승인 답변", "rating": 5}],
        "seller_style_examples": [{"question": "사용 문의", "answer": "문체 참고", "rating": 3}],
        "oje_style_rules": {"seller_examples_are_style_only": True},
    }
    service = DraftGenerationService(Provider(), learning_context_provider=lambda *_: learning)
    service.generate(
        AnswerFacts(inquiry={"question": "TV 사용 문의"}),
        IntentResult("GENERAL", ("TV 사용 문의",), Emotion.NORMAL, "NORMAL", 0.9, False, ""),
    )
    prompt_input = captured["prompt"]["input"]
    assert prompt_input["context_priority"] == [
        "CURRENT_INQUIRY", "PRODUCT_DB", "POLICY", "FIXED_TEMPLATE",
        "SIMILAR_APPROVED_ANSWERS", "SELLER_STYLE_EXAMPLES",
        "HISTORICAL_CASES_REFERENCE_ONLY", "OJE_STYLE_RULES",
    ]
    assert prompt_input["similar_approved_answers"][0]["answer"] == "승인 답변"
    assert prompt_input["oje_style_rules"]["seller_examples_are_style_only"] is True
