from __future__ import annotations

import json

import answer.answer_format as answer_format
from answer.config_loader import load_answer_wrapper
from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from services.naver_post_payload_builder import NaverPostPayloadBuilder


def test_wrapper_is_loaded_verbatim_from_template_repository() -> None:
    wrapper = load_answer_wrapper()
    assert wrapper.header == (
        "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다."
    )
    assert wrapper.footer == (
        "안내드린 내용이 문의하신 내용과 다른 경우,\n"
        "네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다.\n\n"
        "감사합니다."
    )


def test_template_wrapper_change_reaches_draft_edit_final_and_payload(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "answer_data"
    config_dir = root / "configs"
    config_dir.mkdir(parents=True)
    policy = {
        "wrapper": {
            "header": "♣♧Template 변경 Header♧♣",
            "footer": "Template 변경 Footer\n\n감사합니다.",
            "legacy_headers": [],
            "legacy_footers": [],
        }
    }
    (config_dir / "answer_policy.json").write_text(
        json.dumps(policy, ensure_ascii=False),
        encoding="utf-8",
    )
    changed = load_answer_wrapper(root)
    monkeypatch.setattr(answer_format, "load_answer_wrapper", lambda: changed)

    database = Database(tmp_path / "wrapper.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item(
        {
            "store_code": "OJE_PLUS",
            "source_type": "CUSTOMER_INQUIRY",
            "source_question_id": "WRAPPER-CHANGE",
            "content": "문의",
            "raw_json": {},
        }
    ).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    draft = AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED,
            category="TEMPLATE",
            reason="Template",
            answer="Program 본문",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        ),
    )
    expected_program = "♣♧Template 변경 Header♧♣\n\nProgram 본문\n\nTemplate 변경 Footer\n\n감사합니다."
    assert draft["original_answer"] == expected_program

    edited = ApprovalService(database).save_edited_answer(
        inquiry_id=inquiry_id,
        draft_id=int(draft["id"]),
        edited_answer="직원 수정 본문",
    )
    assert edited["edited_answer"].startswith(changed.header)
    approved = ApprovalService(database).approve(
        inquiry_id=inquiry_id,
        draft_id=int(draft["id"]),
    )
    final_answer = approved.draft["final_answer"]
    assert final_answer.startswith(changed.header)
    assert final_answer.endswith(changed.footer)
    payload = NaverPostPayloadBuilder().build(
        source_type="CUSTOMER_INQUIRY",
        external_id="WRAPPER-CHANGE",
        store="OJE_PLUS",
        final_answer=final_answer,
    )
    assert payload.payload["answerComment"] == final_answer
