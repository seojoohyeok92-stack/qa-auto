from __future__ import annotations

import json

from openpyxl import Workbook

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from scripts.backfill_learning_repository import backfill_legacy


HEADERS = [
    "처리시각", "question_id", "action", "posted", "상품", "옵션", "문의",
    "답변여부", "답변", "판단사유", "질문유형", "질문수", "개별질문요약",
    "판단방식", "order_id", "product_order_id", "channel_product_no",
    "origin_product_no", "raw_json",
]


def test_legacy_backfill_only_imports_posted_masked_style_examples(tmp_path) -> None:
    legacy = tmp_path / "legacy"
    output = legacy / "outputs" / "naver_api"
    history = legacy / "outputs" / "learning"
    output.mkdir(parents=True)
    history.mkdir(parents=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "naver_qna_result"
    sheet.append(HEADERS)
    sheet.append([
        "2026-07-10T00:00:00", "Q1", "posted", "Y", "상품", "모델", 
        "홍길동 주문번호 2026070448206811 문의", "답변 가능",
        "홍길동님 010-1234-5678로 안내했습니다.", "", "일반", 1, "", "rules",
        "", "", "", "", json.dumps({"customerName": "홍길동"}, ensure_ascii=False),
    ])
    sheet.append([
        "2026-07-10T00:00:00", "Q2", "dry_run", "N", "상품", "모델",
        "미등록 문의", "답변 가능", "등록되지 않은 답변", "", "일반", 1, "", "gpt",
        "", "", "", "", "{}",
    ])
    workbook.save(output / "run.xlsx")
    (history / "answer_history.jsonl").write_text(
        json.dumps({"question": "로그", "program_answer": "", "action": "no_answer", "posted": "N"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    database = Database(tmp_path / "learning.db")
    database.initialize()
    dry_run = backfill_legacy(database, legacy, apply=False)
    assert dry_run["repository_added"] == 1
    assert LearningRepository(database).count() == 0
    applied = backfill_legacy(database, legacy, apply=True)
    assert applied["repository_added"] == 1
    item = LearningRepository(database).candidates(store_code="OJE_PLUS")[0]
    assert item["style_only"] is True
    assert item["rating"] == 4
    assert item["metadata_json"]["legacy_source"] == "LEGACY_RULE"
    assert "홍길동" not in item["question_original_masked"] + item["final_answer"]
    assert "2026070448206811" not in item["question_original_masked"]
    assert "010-1234-5678" not in item["final_answer"]
    repeated = backfill_legacy(database, legacy, apply=True)
    assert repeated["repository_added"] == 0
    assert repeated["duplicates"] == 1
