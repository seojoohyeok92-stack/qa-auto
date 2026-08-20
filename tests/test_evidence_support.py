from __future__ import annotations

from answer.evidence_support import (
    answer_support_recall,
    apply_answer_support,
    content_stems,
    coverage_label,
)
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from services.historical_case_service import HistoricalCaseService
from services.similar_answer_service import SimilarAnswerService


def test_content_stems_strips_particles_not_meaning() -> None:
    assert "구매처" in content_stems("구매처를 뭐라고 입력해야하나요")
    assert "구매처" in content_stems("구매처 네이버 선택 후 확인해주시면 감사하겠습니다")


def test_answer_support_recall_rewards_actual_coverage() -> None:
    question = "온누리 신청 시 구매처를 무엇으로 입력해야 하나요"
    correct_answer = "구매처는 네이버로 선택 후 혜택신청 가이드를 확인해주시면 됩니다"
    wrong_answer = "고객님께서 구매하신 제품의 주문번호는 2026123입니다"

    assert answer_support_recall(question, correct_answer) >= 0.25
    assert answer_support_recall(question, wrong_answer) == 0.0


def test_coverage_label_thresholds() -> None:
    assert coverage_label(0.0) == "UNSUPPORTED"
    assert coverage_label(0.2) == "PARTIALLY_SUPPORTED"
    assert coverage_label(0.6) == "SUPPORTED"


def test_apply_answer_support_is_purely_additive_when_no_overlap() -> None:
    boosted, support = apply_answer_support(0.42, "설치 가능한가요", "네 가능합니다 방문설치입니다")
    # "설치"/"가능"이 두 문장에 공통으로 남는 정상적인 케이스는 보너스가 생길 수
    # 있으므로, 완전 무관 답변으로 보너스가 전혀 없는 경우만 검증한다.
    boosted_none, support_none = apply_answer_support(0.42, "배송 언제 오나요", "환불 절차 안내입니다")
    assert support_none == 0.0
    assert boosted_none == 0.42


def make_database(tmp_path):
    database = Database(tmp_path / "evidence-support.db")
    database.initialize()
    return database


def add_learning_row(
    repository: LearningRepository,
    *,
    inquiry_id: int,
    source_key: str,
    question: str,
    answer: str,
    product_name: str = "삼성 삼탠바이미 32인치 M5 스마트 모니터",
) -> int:
    row = repository.upsert(
        {
            "source_key": source_key,
            "learning_source": "SELLER_ANSWER",
            "inquiry_id": inquiry_id,
            "answer_draft_id": None,
            "approval_history_id": None,
            "question_original_masked": question,
            "question_normalized": question.lower(),
            "store_code": "OJE_PLUS",
            "inquiry_type": "PRODUCT_INQUIRY",
            "intent": None,
            "product_name": product_name,
            "model_code": None,
            "final_answer": answer,
            "rating": 5,
            "quality_score": 1.0,
            "generation_mode": "TEST",
            "template_id": None,
            "processing_route": "TEST",
            "validator_result": "HUMAN_VERIFIED_NAVER_POSTED",
            "posted": True,
            "auto_posted": False,
            "edit_ratio": 1.0,
            "style_only": False,
            "version": 1,
            "style_features_json": {},
            "metadata_json": {
                "human_verified": True,
                "learning_signal_type": "POSITIVE",
            },
            "active": True,
            "validity_type": "PERMANENT",
            "validity_active": True,
        }
    )
    return int(row["id"])


def test_acceptance_case_e_low_similarity_correct_evidence_outranks_high_similarity_wrong_evidence(
    tmp_path,
) -> None:
    """685858235 규모 재현: candidate A(질문 유사도 높음, 답변 무관) vs
    candidate B(질문 유사도 낮음, 답변이 실제 정답)."""

    database = make_database(tmp_path)
    inquiries = InquiryRepository(database)
    source_id = inquiries.upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "EVIDENCE-SOURCE",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품 문의",
            "content": "질문 A",
            "product_id": "MONITOR-FAMILY",
            "product_name": "삼성 삼탠바이미 32인치 M5 스마트 모니터",
            "source_answered": True,
            "raw_json": {},
        }
    ).inquiry_id
    repository = LearningRepository(database)
    wrong_but_similar_id = add_learning_row(
        repository,
        inquiry_id=source_id,
        source_key="onnuri-order-number",
        question="온누리상품권신청 후 보완사유로 주문번호를 재입력하라고 반려됐습니다 주문번호를 뭐라고 입력해야하나요",
        answer="고객님께서 구매하신 제품의 주문번호는 2026****2541 입니다.",
    )
    correct_but_dissimilar_id = add_learning_row(
        repository,
        inquiry_id=source_id,
        source_key="onnuri-purchase-channel",
        question="온누리 상품권 신청용입니다 거래명세서 발급 신청합니다",
        answer="구매처 네이버 선택 후 좌측 하단 구매처별 혜택신청 가이드 확인해주시면 감사하겠습니다.",
    )

    query = "온누리 신청 시 구매처를 무엇으로 입력해야 하나요"
    results = SimilarAnswerService(repository).search(
        query,
        store_code="OJE_PLUS",
        product_name="삼성 삼탠바이미 32인치 M5 스마트 모니터",
        limit=3,
        minimum_relevance=0.0,
    )
    ids_in_order = [int(item["id"]) for item in results]
    assert correct_but_dissimilar_id in ids_in_order
    assert ids_in_order.index(correct_but_dissimilar_id) < ids_in_order.index(
        wrong_but_similar_id
    )
    winner = next(item for item in results if int(item["id"]) == correct_but_dissimilar_id)
    assert winner["answer_support"] > 0.0


def test_historical_case_reranking_also_prefers_answer_support(tmp_path) -> None:
    database = make_database(tmp_path)
    service = HistoricalCaseService(database)
    now_case_similar_wrong = {
        "source": "NAVER_HISTORY", "store_code": "OJE_PLUS",
        "external_inquiry_id": "H1", "inquiry_type": "PRODUCT_INQUIRY",
        "question": "온누리상품권신청 후 보완사유로 주문번호를 재입력하라고 반려됐습니다 주문번호를 뭐라고 입력해야하나요",
        "question_normalized": "온누리상품권신청 보완사유로 주문번호를 재입력하라고 반려됐습니다 주문번호를 뭐라고 입력해야하나요",
        "seller_answer": "고객님께서 구매하신 제품의 주문번호는 2026****2541 입니다.",
        "product_name": "삼성 삼탠바이미 32인치 M5 스마트 모니터",
        "product_id": None, "order_reference": None, "source_answered": True,
        "inquiry_created_at": None, "answer_updated_at": None,
        "imported_at": None, "source_payload_reference": "TEST",
        "raw_json": {}, "classification": "PRODUCT_INQUIRY",
        "policy_risk": "NONE", "quality_score": 0.7, "confidence": 0.7,
        "active": True, "metadata_json": {},
        "case_key": "case-wrong", "fingerprint": "fp-wrong",
    }
    dissimilar_but_correct = dict(now_case_similar_wrong)
    dissimilar_but_correct.update({
        "question": "온누리 상품권 신청용입니다 거래명세서 발급 신청합니다",
        "question_normalized": "온누리 상품권 신청용입니다 거래명세서 발급 신청합니다",
        "seller_answer": "구매처 네이버 선택 후 좌측 하단 구매처별 혜택신청 가이드 확인해주시면 감사하겠습니다.",
        "case_key": "case-correct", "fingerprint": "fp-correct",
    })
    service.repository.upsert(now_case_similar_wrong)
    service.repository.upsert(dissimilar_but_correct)

    detailed = service.search_detailed(
        "온누리 신청 시 구매처를 무엇으로 입력해야 하나요",
        store_code="OJE_PLUS",
        product_name="삼성 삼탠바이미 32인치 M5 스마트 모니터",
        limit=2,
    )
    selected = detailed["selected"]
    assert selected, "expected at least one historical candidate"
    top = selected[0]
    assert "구매처" in str(top["seller_answer"])
    assert top.get("answer_support", 0) > 0.0
