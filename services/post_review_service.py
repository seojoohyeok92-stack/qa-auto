from __future__ import annotations

from repositories.database import Database
from repositories.log_repository import LogRepository
from repositories.post_review_repository import PostReviewRepository


class PostReviewService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.reviews = PostReviewRepository(database)
        self.logs = LogRepository(database)

    def complete_without_change(
        self, *, inquiry_id: int, actor: str,
    ) -> dict:
        version = self.reviews.reviewed_no_change(
            inquiry_id=int(inquiry_id), actor=actor
        )
        self.logs.record_inquiry(
            int(inquiry_id), "POST_REVIEW_COMPLETED",
            "자동등록 답변을 수정 없음으로 사후검토 완료했습니다.",
            details={
                "actor": actor,
                "version_id": version["id"],
                "learning_saved": False,
                "learning_policy": "MANUAL_DECISION_ONLY",
            },
        )
        return version

