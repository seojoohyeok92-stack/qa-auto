from __future__ import annotations

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.draft_generation_service import DraftGenerationService
from services.learning_service import LearningService
from services.similar_answer_service import SimilarAnswerService


def _candidate(*, primary_action: str | None = None) -> dict:
    semantic = ({"primary_action": primary_action} if primary_action else {})
    return {
        "id": 101,
        "question_original_masked": (
            "Can I use a certified repair center for A/S warranty repair?"
        ),
        "question_normalized": (
            "can i use a certified repair center for a/s warranty repair"
        ),
        "final_answer": (
            "A certified repair center accepts A/S warranty repairs."
        ),
        "store_code": "OJE_PLUS",
        "inquiry_type": "PRODUCT_INQUIRY",
        "intent": "REPAIR",
        "product_name": "Example television MODEL-1",
        "source_product_name": "Example television MODEL-1",
        "source_product_id": "MODEL-1",
        "model_code": "MODEL-1",
        "rating": 5,
        "created_at": "2026-01-01T00:00:00Z",
        "learning_source": "APPROVED_UNEDITED",
        "active": True,
        "validity_active": True,
        "metadata_json": {
            "human_verified": True,
            "learning_signal_type": "POSITIVE",
            "learning_topics": ["AS_SUPPORT"],
            **({"semantic": semantic} if semantic else {}),
        },
    }


def _service(tmp_path) -> SimilarAnswerService:
    database = Database(tmp_path / "semantic-evidence.db")
    database.initialize()
    return SimilarAnswerService(LearningRepository(database))


def test_verified_exact_product_semantic_learning_is_evidence_when_wording_differs(
    tmp_path,
) -> None:
    service = _service(tmp_path)

    selected = service.search(
        "Is A/S service available for this model at a repair location?",
        store_code="OJE_PLUS",
        product_id="MODEL-1",
        product_name="Example television MODEL-1",
        model_code="MODEL-1",
        semantic_goal={
            "customer_goal": "REPAIR",
            "requested_information": "service availability",
            "atomic_question": "Can this model receive A/S repair?",
        },
        candidate_pool=[_candidate()],
        minimum_relevance=0.0,
    )

    assert len(selected) == 1
    assert selected[0]["answer_support"] >= 0.5
    assert (
        selected[0]["answer_support_reason"]
        == "SEMANTIC_EXACT_PRODUCT_HUMAN_VERIFIED"
    )


def test_semantic_action_mismatch_is_not_promoted_to_evidence(tmp_path) -> None:
    service = _service(tmp_path)

    selected = service.search(
        "Is A/S service available for this model at a repair location?",
        store_code="OJE_PLUS",
        product_id="MODEL-1",
        product_name="Example television MODEL-1",
        model_code="MODEL-1",
        semantic_goal={
            "customer_goal": "REPAIR",
            "requested_information": "service availability",
            "atomic_question": "Can this model receive A/S repair?",
        },
        candidate_pool=[_candidate(primary_action="BENEFIT")],
        minimum_relevance=0.0,
    )

    assert selected == []
    assert service.last_trace["rejection_counts"]["SEMANTIC_GOAL_MISMATCH"] == 1
    assert service.last_trace["candidate_diagnostics"] == [
        {
            "learning_id": 101,
            "lifecycle": "APPROVED",
            "human_verified": True,
            "eligible": False,
            "similarity": None,
            "reject_reason": "SEMANTIC_GOAL_MISMATCH",
            "required_action": "REPAIR",
            "candidate_action": "BENEFIT",
        }
    ]


def test_repair_fixture_uses_direct_verified_learning_without_text_special_case(
    tmp_path,
) -> None:
    service = _service(tmp_path)
    candidate = _candidate()
    candidate.update({
        "question_original_masked": (
            "\uc81c\ud488 A/S \uc811\uc218\ub294 \uacf5\uc2dd "
            "\uc11c\ube44\uc2a4\uc13c\ud130\uc5d0\uc11c \uac00\ub2a5\ud55c\uac00\uc694?"
        ),
        "question_normalized": (
            "\uc81c\ud488 a/s \uc811\uc218\ub294 \uacf5\uc2dd "
            "\uc11c\ube44\uc2a4\uc13c\ud130\uc5d0\uc11c \uac00\ub2a5\ud55c\uac00\uc694"
        ),
        "final_answer": (
            "\uacf5\uc2dd \uc11c\ube44\uc2a4\uc13c\ud130\uc5d0\uc11c A/S "
            "\uc810\uac80\uacfc \uc218\ub9ac\ub97c \ubc1b\uc744 \uc218 \uc788\uc2b5\ub2c8\ub2e4."
        ),
    })

    selected = service.search(
        "\uad6d\ub0b4 \uc0bc\uc131\uc804\uc9c0\uc13c\ud130\uc5d0\uc11c "
        "AS\ubc1b\uc744 \uc218 \uc788\ub098\uc694?",
        store_code="OJE_PLUS",
        product_id="MODEL-1",
        product_name="Example television MODEL-1",
        model_code="MODEL-1",
        semantic_goal={
            "customer_goal": "REPAIR",
            "requested_information": "repair availability",
            "atomic_question": "Can this model receive A/S repair?",
        },
        candidate_pool=[candidate],
        minimum_relevance=0.0,
    )

    assert selected[0]["answer_support"] >= 0.5
    assert selected[0]["answer_support_reason"] == (
        "SEMANTIC_EXACT_PRODUCT_HUMAN_VERIFIED"
    )


def test_verified_mapped_learning_recovers_provider_avoidance_with_usage() -> None:
    learning_context = {
        "similar_approved_answers": [
            {
                "learning_example_id": 101,
                "matched_subquestion": "Can this model receive A/S repair?",
                "answer": "A certified repair center accepts A/S warranty repairs.",
                "authority": "APPROVED",
                "compatibility": {"product_match": "EXACT_PRODUCT"},
            }
        ],
        "subquestion_evidence": [
            {
                "subquestion": "Can this model receive A/S repair?",
                "status": "ANSWERABLE",
                "learning_ids": [101],
                "evidence_coverage": "SUPPORTED",
            }
        ],
    }
    provider_raw = {
        "answer": (
            "\ud604\uc7ac \ud655\uc778\ub41c \uc815\ubcf4\ub9cc\uc73c\ub85c "
            "\uc548\ub0b4\ud558\uae30 \uc5b4\ub824\uc6b4 \uc0ac\ud56d\uc73c\ub85c "
            "\ucd94\uac00 \ud655\uc778 \ud544\uc694"
        ),
        "learning_usage": [],
    }

    recovered = DraftGenerationService._apply_learning_grounded_recovery(
        provider_raw, learning_context
    )

    assert "A certified repair center" in recovered["answer"]
    assert recovered["learning_usage"] == [
        {
            "learning_id": 101,
            "matched_subquestion": "Can this model receive A/S repair?",
            "answer_supported": True,
            "reason": "ACTIVE_POSITIVE_LEARNING_GROUNDED_RECOVERY",
            "authority": "APPROVED",
            "compatibility": {"product_match": "EXACT_PRODUCT"},
        }
    ]


def test_new_learning_preserves_canonical_semantic_metadata(tmp_path) -> None:
    database = Database(tmp_path / "learning-semantic-metadata.db")
    database.initialize()
    row = LearningService(database)._build(
        inquiry={
            "id": 1,
            "store_code": "OJE_PLUS",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "Question",
            "content": "Can this model receive A/S repair?",
            "product_id": "MODEL-1",
            "product_name": "Example television MODEL-1",
        },
        draft={
            "id": 1,
            "source": "GPT",
            "original_answer": "A certified repair center accepts A/S warranty repairs.",
            "metadata_json": {
                "processing_plan": {
                    "semantic_routing": {
                        "semantic": {
                            "primary_action": "REPAIR",
                            "atomic_questions": [
                                {
                                    "text": "Can this model receive A/S repair?",
                                    "action": "REPAIR",
                                }
                            ],
                        }
                    }
                }
            },
        },
        learning_source="APPROVED_UNEDITED",
        answer="A certified repair center accepts A/S warranty repairs.",
    )

    assert row is not None
    assert row["metadata_json"]["semantic"] == {
        "primary_action": "REPAIR",
        "atomic_questions": [
            {
                "text": "Can this model receive A/S repair?",
                "action": "REPAIR",
            }
        ],
    }
