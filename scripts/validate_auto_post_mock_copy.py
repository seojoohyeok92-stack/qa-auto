from __future__ import annotations

import argparse
import json

from api.naver_answer_client import NaverAnswerResponse
from answer.models import AnswerResult, AnswerStatus
from config import NaverPostSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.post_review_repository import PostReviewRepository
from repositories.workflow_repository import WorkflowRepository
from services.auto_post_pipeline_service import AutoPostPipelineService
from services.naver_post_service import NaverPostService
from services.similar_answer_service import SimilarAnswerService


class MockNaverClient:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request, *, access_token: str) -> NaverAnswerResponse:
        assert access_token == "mock-token"
        self.calls += 1
        return NaverAnswerResponse(204, f"MOCK-ANSWER-{self.calls}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    database = Database(args.database)
    before_learning = LearningRepository(database).count()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "PRODUCT_INQUIRY",
            "source_question_id": "MOCK-AUTO-POST-UAT-1",
            "external_inquiry_id": "MOCK-AUTO-POST-UAT-1",
            "inquiry_type": "PRODUCT_INQUIRY",
            "title": "자동등록 Mock 검증 문의",
            "content": "화면 설정 방법을 안내해 주세요.",
            "product_name": "검증용 상품",
            "registered_at": "2000-01-01T00:00:00+00:00",
            "source_answered": False,
            "answer_status": "UNANSWERED",
            "post_status": "NOT_POSTED",
            "raw_json": {"validation_fixture": True},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="일반",
            reason="운영 DB 복사본 Mock 검증",
            answer="문의하신 화면 설정은 설정 메뉴에서 변경하실 수 있습니다.",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
            metadata={
                "selected_answer_route": "TEMPLATE",
                "generation_mode": "TEMPLATE",
                "validator_result": {"status": "PASS", "passed": True},
            },
        ),
    )
    client = MockNaverClient()
    post_service = NaverPostService(
        database,
        settings=NaverPostSettings(enabled=True),
        store_resolver=lambda _: StoreConfig(
            "OJE_PLUS", "Mock Store", "mock-id", "mock-secret"
        ),
        token_provider=lambda **_: "mock-token",
        client=client,
    )
    posted = AutoPostPipelineService(
        database, post_service=post_service
    ).run_pending(
        run_id="MOCK-COPY-RUN-1",
        owner_id="MOCK-COPY-OWNER",
        max_retries=1,
        limit=1,
    )
    correction = post_service.correct(
        inquiry_id,
        edited_answer="직원 확인 결과 화면 설정은 설정 메뉴에서 변경하실 수 있습니다.",
        actor="MOCK_STAFF",
    )
    search = SimilarAnswerService(LearningRepository(database)).search(
        "화면 설정 방법을 안내해 주세요.",
        store_code="OJE_PLUS",
        minimum_relevance=0.1,
    )
    versions = PostReviewRepository(database).versions(inquiry_id)
    with database.connection() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    report = {
        "status": "PASS",
        "mock_network_calls": client.calls,
        "actual_naver_calls": 0,
        "auto_post_result": posted.to_dict(),
        "correction_status": correction.status,
        "version_numbers": [row["version_number"] for row in versions],
        "review_status": PostReviewRepository(database).get(inquiry_id)["status"],
        "learning_added": LearningRepository(database).count() - before_learning,
        "top_learning_source": search[0]["learning_source"] if search else None,
        "integrity_check": integrity,
    }
    if not (
        posted.succeeded_count == 1
        and correction.status == "CORRECTED_AND_REPOSTED"
        and report["version_numbers"] == [1, 2, 3]
        and report["top_learning_source"] == "AUTO_POST_CORRECTED"
        and integrity == "ok"
    ):
        report["status"] = "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
