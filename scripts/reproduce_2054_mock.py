from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.naver_answer_client import NaverAnswerResponse
from config import NaverPostSettings, StoreConfig
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.naver_post_repository import NaverPostRepository
from services.naver_post_payload_builder import NaverPostPayloadBuilder
from services.naver_post_service import NaverPostService


class Mock204Client:
    def __init__(self) -> None:
        self.requests = []

    def send(self, request, *, access_token):
        assert access_token == "mock-token"
        self.requests.append(request)
        return NaverAnswerResponse(204, "MOCK-ANSWER-2054")


def main() -> None:
    root = ROOT
    source = Database(root / "data" / "oje_automation.db")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    copy_path = root / "data" / "backups" / f"oje_2054_404_mock_{stamp}.db"
    source.backup_to(copy_path)
    copied = Database(copy_path)
    copied.initialize()

    inquiry = InquiryRepository(copied).get(2054)
    if inquiry is None:
        raise SystemExit("Inquiry 2054 not found")
    builder = NaverPostPayloadBuilder()
    target = builder.resolve_target(inquiry, require_remote_snapshot=True)
    draft = AnswerRepository(copied).active_for_inquiry(2054)
    mapped_request = builder.build_for_target(
        target=target, final_answer=str((draft or {}).get("final_answer") or "")
    )
    previous = NaverPostRepository(copied).latest(2054)
    client = Mock204Client()
    result = NaverPostService(
        copied,
        settings=NaverPostSettings(enabled=True),
        store_resolver=lambda code: StoreConfig(
            str(code), "Mock OJE_PLUS", "mock-id", "mock-secret", True
        ),
        token_provider=lambda **_: "mock-token",
        client=client,
    ).post(
        2054,
        actor="V22_404_MOCK",
        confirmed=True,
        retry_requested=True,
        automatic=True,
        auto_post_run_id="V22-404-MOCK",
    )
    latest = NaverPostRepository(copied).latest(2054)
    with copied.connection() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    print(
        json.dumps(
            {
                "copy_path": str(copy_path.resolve()),
                "inquiry_type": target.inquiry_type,
                "external_id": target.external_id,
                "external_id_source": target.external_id_source,
                "previous_attempt_id": previous["id"] if previous else None,
                "previous_status": previous["status"] if previous else None,
                "previous_error_code": previous["error_code"] if previous else None,
                "method": mapped_request.method,
                "endpoint": mapped_request.endpoint,
                "payload": mapped_request.payload,
                "expected_success_status": mapped_request.expected_success_status,
                "mock_result": result.to_dict(),
                "mock_client_call_count": len(client.requests),
                "new_attempt_created": bool(
                    latest and previous and latest["id"] != previous["id"]
                ),
                "integrity_check": integrity,
                "network_client": "Mock204Client",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
