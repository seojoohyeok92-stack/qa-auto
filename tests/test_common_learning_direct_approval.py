"""일반 Learning 승인 없이 공통 Learning 만 승인한다.

두 승인은 서로 선행 조건이 아니다. 전에는 공통 Learning 을 등록하려면 먼저 그
문의의 답변을 Positive Learning 으로 승인해야 했고, 그러면 삼성감사제처럼 기간이
정해진 정책을 적으려고 코퍼스에 넣고 싶지 않은 답변을 승인하게 됐다.

그래서 저장 행이 필요한 맥락 -- ``inquiry_id``, ``store_code``,
``inquiry_type``, ``product_name`` -- 을 Learning 대신 문의에서 읽는
``add_common_learning_for_inquiry`` 를 더했다. 임시 Positive Learning 을 만들어
원본으로 쓰거나 일반 승인으로 표시하는 편법은 쓰지 않는다. 그 다음은 기존
경로와 같은 것을 쓴다 -- 적용 범위, 유효기간, 중복 검사, 승인 키, 검색 규칙.

이 파일은 저장 성공만 보지 않는다. 직접 승인한 행이 실제 Production 검색
경로에서 나오는지, 그리고 그 과정에서 일반 승인 상태가 움직이지 않는지까지
확인한다.
"""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from answer.models import AnswerResult, AnswerStatus
from repositories.answer_repository import AnswerRepository
from repositories.approval_repository import ApprovalRepository
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import (
    LearningRepository,
    is_common_learning,
)
from repositories.workflow_repository import WorkflowRepository
from services.approval_service import ApprovalService
from ui.review_workspace import approval_learning_trace


TV_50 = "삼성 4K UHD 스마트 비즈니스 TV LH50BEHHLGFXKR 125.7cm(50인치)"
TV_43 = "삼성 4K UHD 스마트 비즈니스 TV LH43BEDHLGFXKR 108cm(43인치)"
MONITOR = "삼성 모니터 LS32DM501EKXKR 80cm(32인치)"

EVENT_QUESTION = "삼성감사제 기간에 추가 할인이 있나요?"
EVENT_ANSWER = (
    "삼성감사제 기간에는 카드사 청구 할인이 추가 적용됩니다. "
    "다만 법인카드와 상품권 결제는 대상에서 제외됩니다."
)
SPEC_QUESTION = "화면 터치 되나요?"
SPEC_ANSWER = "이 비즈니스 TV 라인업은 화면 터치 기능을 지원하지 않습니다."

BUTTON_LABEL = "공통 Learning 직접 승인"
SAVE_LABEL = "공통 Learning 저장"
CANCEL_LABEL = "취소"
QUESTION_LABEL = "공통으로 사용할 질문"
ANSWER_LABEL = "공통으로 사용할 답변"


@pytest.fixture()
def database(tmp_path) -> Database:
    value = Database(tmp_path / "direct.db")
    value.initialize()
    return value


def _inquiry(
    database: Database,
    *,
    external_id: str = "D-1",
    content: str = EVENT_QUESTION,
    product_name: str = TV_50,
    store_code: str = "OJE_PLUS",
) -> int:
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": store_code,
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": external_id,
        "inquiry_type": "PRODUCT_GENERAL",
        "title": "상품문의",
        "content": content,
        "product_name": product_name,
        "product_id": "p1",
        "raw_json": {},
    }).inquiry_id
    WorkflowRepository(database).initialize_steps(inquiry_id)
    return inquiry_id


def _draft(database: Database, inquiry_id: int, answer: str) -> dict:
    return AnswerRepository(database).create_program_draft(
        inquiry_id,
        AnswerResult(
            status=AnswerStatus.GENERATED, category="GENERAL", reason="test",
            answer=answer, provider="rules", auto_answerable=True,
            needs_review=False,
        ),
    )


def _found(
    repository: LearningRepository,
    *,
    question: str,
    product_name: str = MONITOR,
    store_code: str = "OJE_PLUS",
) -> list[dict]:
    """Production 이 쓰는 검색 경로."""

    from services.similar_answer_service import SimilarAnswerService

    return SimilarAnswerService(repository).search(
        question, store_code=store_code, product_name=product_name,
        inquiry_type="PRODUCT_INQUIRY", limit=5,
    )


def _approval_state(database: Database, inquiry_id: int) -> dict:
    return ApprovalRepository(database).get_inquiry_approval(inquiry_id)


def _trace(database: Database, inquiry_id: int) -> dict:
    return approval_learning_trace(
        database,
        inquiry_id=inquiry_id,
        draft=AnswerRepository(database).active_for_inquiry(inquiry_id),
        approval_state=_approval_state(database, inquiry_id),
        source_answered=False,
    )


def _positives(database: Database, inquiry_id: int) -> list[dict]:
    """공통 Learning 을 뺀, 그 문의의 일반 Positive Learning."""

    return [
        row for row in LearningRepository(database).for_inquiry(inquiry_id)
        if not is_common_learning(row.get("metadata_json") or {})
    ]


# ===========================================================================
# 1, 2. 일반 승인 없이 저장되고, 승인 상태는 움직이지 않는다
# ===========================================================================

def test_a_common_learning_is_stored_without_any_approval(database):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)

    created = repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    assert is_common_learning(created["metadata_json"])
    metadata = created["metadata_json"]
    assert metadata["common_learning_source"] == "DIRECT_INQUIRY"
    assert metadata["common_learning_inquiry_id"] == inquiry_id
    assert metadata["common_learning_store_code"] == "OJE_PLUS"
    assert metadata["common_learning_approved_by"] == "op"
    # 승인된 원본 Learning 이 없으므로 그 키는 아예 없다 -- 임시 원본을
    # 만들어 끼워 넣지 않는다.
    assert "common_learning_origin_id" not in metadata


def test_no_positive_learning_is_created_by_a_direct_approval(database):
    inquiry_id = _inquiry(database)
    _draft(database, inquiry_id, "확인 후 안내드리겠습니다.")
    repository = LearningRepository(database)
    assert _positives(database, inquiry_id) == []

    repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    assert _positives(database, inquiry_id) == [], "일반 Positive 가 생겼다"
    assert len(repository.common_learnings()) == 1


def test_the_approval_state_is_untouched(database):
    inquiry_id = _inquiry(database)
    _draft(database, inquiry_id, "확인 후 안내드리겠습니다.")
    before = dict(_approval_state(database, inquiry_id))

    LearningRepository(database).add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    after = dict(_approval_state(database, inquiry_id))
    assert after == before
    trace = _trace(database, inquiry_id)
    assert trace["approval_complete"] is False
    assert trace["positive_learning"] is False
    # 공통 Learning 은 이 문의 답변의 승인이 아니므로 승인 카드의 읽기에
    # 들어오지 않는다.
    assert list(trace["approved_learning_ids"]) == []


def test_the_inquiry_row_is_untouched(database):
    inquiry_id = _inquiry(database)
    columns = (
        "workflow_status", "post_status", "source_answered", "content",
        "product_name",
    )
    with database.connection() as connection:
        before = dict(connection.execute(
            f"SELECT {', '.join(columns)} FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone())

    LearningRepository(database).add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    with database.connection() as connection:
        after = dict(connection.execute(
            f"SELECT {', '.join(columns)} FROM inquiries WHERE id=?",
            (inquiry_id,),
        ).fetchone())
    assert after == before


# ===========================================================================
# 3. 두 승인이 각각 독립적으로 동작한다
# ===========================================================================

def test_both_approvals_can_be_made_independently(database):
    inquiry_id = _inquiry(database)
    draft = _draft(database, inquiry_id, "감사제 할인은 카드사 조건에 따릅니다.")
    repository = LearningRepository(database)

    # 공통만 먼저.
    repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )
    assert _positives(database, inquiry_id) == []

    # 그 다음 일반 승인. 공통 Learning 이 방해하지 않는다.
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="staff",
    )

    positives = _positives(database, inquiry_id)
    assert len(positives) == 1
    assert len(repository.common_learnings()) == 1
    trace = _trace(database, inquiry_id)
    assert trace["approval_complete"] is True
    # 승인 카드가 가리키는 것은 일반 Positive 쪽이다.
    assert int(trace["positive_learning_id"]) == int(positives[0]["id"])
    assert list(trace["approved_learning_ids"]) == [int(positives[0]["id"])]


def test_the_order_of_the_two_approvals_does_not_matter(database):
    inquiry_id = _inquiry(database)
    draft = _draft(database, inquiry_id, "감사제 할인은 카드사 조건에 따릅니다.")
    repository = LearningRepository(database)

    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="staff",
    )
    repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    assert len(_positives(database, inquiry_id)) == 1
    assert len(repository.common_learnings()) == 1


# ===========================================================================
# 4. 기존 승인 후 추가 경로는 그대로다
# ===========================================================================

def test_the_approval_based_path_still_works(database):
    inquiry_id = _inquiry(database)
    draft = _draft(database, inquiry_id, "감사제 할인은 카드사 조건에 따릅니다.")
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="staff",
    )
    repository = LearningRepository(database)
    origin = _positives(database, inquiry_id)[0]

    created = repository.add_common_learning(
        int(origin["id"]), question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    metadata = created["metadata_json"]
    assert metadata["common_learning_source"] == "APPROVED_LEARNING"
    assert metadata["common_learning_origin_id"] == int(origin["id"])
    assert metadata["common_learning_inquiry_id"] == inquiry_id
    assert is_common_learning(metadata)


def test_the_two_paths_are_separate_decisions_even_for_the_same_text(database):
    """같은 문장을 두 경로로 승인하는 것은 서로 중복이 아니다."""

    inquiry_id = _inquiry(database)
    draft = _draft(database, inquiry_id, "감사제 할인은 카드사 조건에 따릅니다.")
    ApprovalService(database).approve(
        inquiry_id=inquiry_id, draft_id=draft["id"], actor="staff",
    )
    repository = LearningRepository(database)
    origin = _positives(database, inquiry_id)[0]

    first = repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )
    second = repository.add_common_learning(
        int(origin["id"]), question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    assert first["id"] != second["id"]
    assert len(repository.common_learnings()) == 2


# ===========================================================================
# 5, 6. 복합 문의의 일부 / 운영자가 확인한 공통 사양
# ===========================================================================

def test_only_the_selected_part_of_a_composite_inquiry_is_stored(database):
    inquiry_id = _inquiry(
        database,
        content="동봉되는 코드 길이가 어떻게 되나요? 화면 터치 되나요?",
    )

    created = LearningRepository(database).add_common_learning_for_inquiry(
        inquiry_id, question=SPEC_QUESTION, answer=SPEC_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    assert created["question_original_masked"] == SPEC_QUESTION
    assert "코드 길이" not in created["question_original_masked"]
    assert "1.5m" not in created["final_answer"]


def test_a_verified_common_specification_can_be_approved_directly(database):
    """제품 사양이라는 이유만으로 막지 않는다."""

    inquiry_id = _inquiry(database, content=SPEC_QUESTION)

    created = LearningRepository(database).add_common_learning_for_inquiry(
        inquiry_id, question=SPEC_QUESTION, answer=SPEC_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    assert created["metadata_json"]["product_scope"] == "POLICY"
    assert created["metadata_json"]["cross_model_policy"] is True


def test_the_model_code_is_derived_from_the_inquiry_product(database):
    """원본 Learning 이 없어도 모델 코드를 문의 상품명에서 읽는다."""

    inquiry_id = _inquiry(database, product_name=TV_50)

    created = LearningRepository(database).add_common_learning_for_inquiry(
        inquiry_id, question=SPEC_QUESTION, answer=SPEC_ANSWER,
        product_scope="MODEL", market_applicability="COMMON", actor="op",
    )

    assert created["model_code"] == "LH50BEHHLGFXKR"
    assert created["product_name"] == TV_50
    assert created["store_code"] == "OJE_PLUS"


# ===========================================================================
# 7, 8. 기간성 행사
# ===========================================================================

def test_a_seasonal_policy_can_be_approved_directly(database):
    inquiry_id = _inquiry(database)

    created = LearningRepository(database).add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON",
        validity_type="TEMPORARY", event_name="삼성감사제",
        valid_from="2026-10-01", valid_until="2026-12-31", actor="op",
    )

    assert created["validity_type"] == "TEMPORARY"
    assert created["event_name"] == "삼성감사제"
    assert created["valid_from"] and created["valid_until"]
    assert created["validity_active"] is True


@pytest.mark.parametrize(
    "valid_from, valid_until, expected",
    [
        ("2026-10-01", "2026-12-31", True),
        ("2026-01-01", "2026-06-30", False),
        ("2026-12-01", "2026-12-31", False),
    ],
)
def test_a_seasonal_policy_is_evidence_only_inside_its_window(
    database, valid_from, valid_until, expected,
):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON",
        validity_type="TEMPORARY", event_name="삼성감사제",
        valid_from=valid_from, valid_until=valid_until, actor="op",
    )

    found = _found(repository, question=EVENT_QUESTION)

    assert bool(found) is expected, (valid_from, valid_until)


def test_an_expired_policy_stays_in_the_management_list(database):
    """기간이 끝나도 삭제하지 않고 이력을 보존한다."""

    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    created = repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON",
        validity_type="TEMPORARY", event_name="끝난 감사제",
        valid_from="2026-01-01", valid_until="2026-06-30", actor="op",
    )

    assert not _found(repository, question=EVENT_QUESTION)
    listed = repository.common_learnings()
    assert [int(row["id"]) for row in listed] == [int(created["id"])]
    assert listed[0]["event_name"] == "끝난 감사제"


# ===========================================================================
# 9. 마켓 / 모델 범위
# ===========================================================================

@pytest.mark.parametrize(
    "applicability, naver_expected, coupang_expected",
    [
        ("NAVER_ONLY", True, False),
        ("COUPANG_ONLY", False, True),
        ("COMMON", True, True),
    ],
)
def test_the_market_scope_is_honoured(
    database, applicability, naver_expected, coupang_expected,
):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability=applicability, actor="op",
    )

    naver = _found(repository, question=EVENT_QUESTION, store_code="OJE_PLUS")
    coupang = _found(
        repository, question=EVENT_QUESTION, store_code="COUPANG_OJE",
    )

    assert bool(naver) is naver_expected, f"{applicability} / 네이버"
    assert bool(coupang) is coupang_expected, f"{applicability} / 쿠팡"


@pytest.mark.parametrize("product_name", [TV_50, TV_43, MONITOR])
def test_a_directly_approved_policy_reaches_other_models(
    database, product_name,
):
    """직접 승인한 정책이 다른 모델·제품군의 문의에서도 근거가 된다."""

    inquiry_id = _inquiry(database, product_name=TV_50)
    repository = LearningRepository(database)
    repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    found = _found(repository, question=EVENT_QUESTION, product_name=product_name)

    assert found, product_name
    compatibility = found[0].get("compatibility") or {}
    assert compatibility.get("product_match") != "CATEGORY_UNCERTAIN"
    assert "법인카드" in str(found[0].get("final_answer") or "")


# ===========================================================================
# 10. 중복 등록 방지
# ===========================================================================

def test_the_same_text_and_range_is_refused(database):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    created = repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    with pytest.raises(ValueError) as caught:
        repository.add_common_learning_for_inquiry(
            inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
            product_scope="POLICY", market_applicability="COMMON", actor="op",
        )

    assert str(created["id"]) in str(caught.value)
    assert len(repository.common_learnings()) == 1


@pytest.mark.parametrize(
    "scope, market", [("POLICY", "NAVER_ONLY"), ("MODEL", "COMMON")],
)
def test_a_different_range_is_not_a_duplicate(database, scope, market):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope=scope, market_applicability=market, actor="op",
    )

    assert len(repository.common_learnings()) == 2


@pytest.mark.parametrize("question, answer", [("", EVENT_ANSWER), (EVENT_QUESTION, "  ")])
def test_empty_text_is_refused(database, question, answer):
    inquiry_id = _inquiry(database)

    with pytest.raises(ValueError):
        LearningRepository(database).add_common_learning_for_inquiry(
            inquiry_id, question=question, answer=answer,
            product_scope="POLICY", market_applicability="COMMON", actor="op",
        )


@pytest.mark.parametrize("scope", ["GLOBAL", "CATEGORY", ""])
def test_an_unknown_target_is_refused(database, scope):
    inquiry_id = _inquiry(database)

    with pytest.raises(ValueError):
        LearningRepository(database).add_common_learning_for_inquiry(
            inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
            product_scope=scope, market_applicability="COMMON", actor="op",
        )


def test_a_missing_inquiry_is_reported(database):
    with pytest.raises(LookupError):
        LearningRepository(database).add_common_learning_for_inquiry(
            999, question=EVENT_QUESTION, answer=EVENT_ANSWER,
            product_scope="POLICY", market_applicability="COMMON", actor="op",
        )


# ===========================================================================
# 11, 12. 관리 목록 / 수정 / 사용 중지
# ===========================================================================

def test_a_directly_approved_row_is_in_the_management_list(database):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    created = repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    listed = repository.common_learnings()

    assert [int(row["id"]) for row in listed] == [int(created["id"])]


def test_a_directly_approved_row_can_be_edited(database):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    created = repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    repository.update_common_learning_text(
        int(created["id"]),
        question="감사제 카드 할인 되나요?",
        answer="감사제 기간 카드 할인이 적용되며 법인카드는 제외됩니다.",
        expected_updated_at=str(created["updated_at"]),
    )

    found = _found(repository, question="감사제 카드 할인 되나요?")
    assert found
    assert "법인카드는 제외" in str(found[0].get("final_answer") or "")


def test_a_directly_approved_row_can_be_stopped_and_resumed(database):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    created = repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )
    learning_id = int(created["id"])
    assert _found(repository, question=EVENT_QUESTION)

    row = repository.get(learning_id)
    repository.update_validity(
        learning_id, validity_type="PERMANENT", validity_active=False,
        condition={}, expected_updated_at=str(row["updated_at"]),
    )
    assert not _found(repository, question=EVENT_QUESTION)
    # 목록에는 남는다.
    assert len(repository.common_learnings()) == 1

    row = repository.get(learning_id)
    repository.update_validity(
        learning_id, validity_type="PERMANENT", validity_active=True,
        condition={}, expected_updated_at=str(row["updated_at"]),
    )
    assert _found(repository, question=EVENT_QUESTION)


def test_a_directly_approved_row_can_be_rescoped(database):
    inquiry_id = _inquiry(database)
    repository = LearningRepository(database)
    created = repository.add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    updated = repository.update_application_scope(
        int(created["id"]), product_scope="POLICY",
        market_applicability="NAVER_ONLY", actor="op",
    )

    assert updated["metadata_json"]["market_applicability"] == "NAVER_ONLY"
    assert not _found(
        repository, question=EVENT_QUESTION, store_code="COUPANG_OJE",
    )


# ===========================================================================
# 14. GPT ① -> Learning -> GPT ② 근거 전달
# ===========================================================================

def _policy_atom():
    return {
        "text": EVENT_QUESTION,
        "action": "OTHER",
        "requested_information": "감사제 할인 조건",
        "requested_attribute": "PERMISSION_OR_OPTION",
    }


def test_a_directly_approved_policy_reaches_the_gpt2_prompt(
    tmp_path, monkeypatch,
):
    """검색만 확인하고 끝내지 않는다 -- 실제 생성 경로의 프롬프트를 읽는다."""

    from tests.test_v4_evidence_generalization import _EvidenceReadingStub, _run

    database = Database(tmp_path / "direct_prompt.db")
    database.initialize()
    inquiry_id = _inquiry(database, external_id="D-PROMPT")
    LearningRepository(database).add_common_learning_for_inquiry(
        inquiry_id, question=EVENT_QUESTION, answer=EVENT_ANSWER,
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    stub = _EvidenceReadingStub()
    result = _run(
        tmp_path, monkeypatch, name="direct_prompt",
        question=EVENT_QUESTION, product_name=MONITOR,
        atoms=[_policy_atom()], stub=stub,
    )

    assert result.prompt is not None, f"{result.error!r}"
    assert "법인카드" in result.raw_prompt, "직접 승인한 정책이 전달되지 않았다"
    block = (result.prompt.get("input") or {}).get("similar_approved_answers")
    rows = [item for item in (block or []) if isinstance(item, dict)]
    item = next(
        (row for row in rows if "법인카드" in str(row.get("answer") or "")), None
    )
    assert item is not None, rows
    origin = item.get("evidence_origin")
    origin = origin if isinstance(origin, dict) else {}
    assert origin.get("identity") == "POLICY_OR_GENERAL", origin
    assert origin.get("reason") == "OPERATOR_APPROVED_CROSS_MODEL_POLICY"


# ===========================================================================
# 대시보드 UI -- 승인 없이 열리고, 상태를 유지한다
# ===========================================================================

def _panel(database: Database, inquiry_id: int) -> AppTest:
    return AppTest.from_string(
        f'''
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from ui.review_workspace import _render_answer_panel
db=Database(r"{database.path}")
db.initialize()
_render_answer_panel(db, InquiryRepository(db).get({inquiry_id}))
'''
    ).run(timeout=40)


def _labels(app: AppTest) -> set[str]:
    return {button.label for button in app.button}


def _area(app: AppTest, label: str):
    return next(area for area in app.text_area if area.label == label)


def _has_area(app: AppTest, label: str) -> bool:
    return label in {area.label for area in app.text_area}


def _click(app: AppTest, label: str) -> AppTest:
    next(button for button in app.button if button.label == label).click()
    app.run(timeout=40)
    return app


def test_the_direct_button_is_offered_without_any_approval(database):
    inquiry_id = _inquiry(database)
    _draft(database, inquiry_id, "확인 후 안내드리겠습니다.")

    app = _panel(database, inquiry_id)

    assert not app.exception
    assert BUTTON_LABEL in _labels(app)
    assert any(
        "공통 Learning 직접 승인" in item.value for item in app.markdown
    ), [item.value[:40] for item in app.markdown]
    assert not _has_area(app, QUESTION_LABEL)


def test_the_direct_editor_opens_in_place_with_the_inquiry_text(database):
    inquiry_id = _inquiry(database)
    _draft(database, inquiry_id, "감사제 할인은 카드사 조건에 따릅니다.")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)

    assert not app.exception
    assert _area(app, QUESTION_LABEL).value == EVENT_QUESTION
    assert {"적용 모델", "적용 마켓", "유효기간"} <= {
        radio.label for radio in app.radio
    }
    assert SAVE_LABEL in _labels(app)
    # 페이지를 떠나지 않는다.
    assert "current_page" not in app.session_state
    assert "production_admin_mode" not in app.session_state


def test_saving_from_the_dashboard_keeps_the_inquiry_screen(database):
    inquiry_id = _inquiry(database)
    _draft(database, inquiry_id, "감사제 할인은 카드사 조건에 따릅니다.")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    _area(app, ANSWER_LABEL).set_value(EVENT_ANSWER)
    app.run(timeout=40)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    repository = LearningRepository(database)
    listed = repository.common_learnings()
    assert len(listed) == 1
    assert listed[0]["metadata_json"]["common_learning_source"] == (
        "DIRECT_INQUIRY"
    )
    # 일반 승인은 그대로 없다.
    assert _positives(database, inquiry_id) == []
    assert "current_page" not in app.session_state
    assert not _has_area(app, QUESTION_LABEL)
    assert any(
        str(listed[0]["id"]) in item.value for item in app.success
    ), [item.value for item in app.success]


def test_cancelling_from_the_dashboard_saves_nothing(database):
    inquiry_id = _inquiry(database)
    _draft(database, inquiry_id, "감사제 할인은 카드사 조건에 따릅니다.")

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    _area(app, ANSWER_LABEL).set_value(EVENT_ANSWER)
    app.run(timeout=40)
    app = _click(app, CANCEL_LABEL)

    assert not app.exception
    assert LearningRepository(database).common_learnings() == []
    assert "current_page" not in app.session_state
    assert not _has_area(app, QUESTION_LABEL)


def test_a_save_error_keeps_the_typed_values(database):
    inquiry_id = _inquiry(database)
    _draft(database, inquiry_id, "감사제 할인은 카드사 조건에 따릅니다.")
    repository = LearningRepository(database)

    app = _click(_panel(database, inquiry_id), BUTTON_LABEL)
    _area(app, ANSWER_LABEL).set_value(EVENT_ANSWER)
    app.run(timeout=40)
    app = _click(app, SAVE_LABEL)
    assert not app.exception
    assert len(repository.common_learnings()) == 1

    # 같은 내용으로 다시 -> 중복 거부.
    app = _click(app, BUTTON_LABEL)
    _area(app, ANSWER_LABEL).set_value(EVENT_ANSWER)
    app.run(timeout=40)
    app = _click(app, SAVE_LABEL)

    assert not app.exception
    assert "이미 등록" in "\n".join(item.value for item in app.error)
    # 입력과 편집 영역이 유지된다.
    assert _area(app, ANSWER_LABEL).value == EVENT_ANSWER
    assert SAVE_LABEL in _labels(app)
    assert "current_page" not in app.session_state
    assert len(repository.common_learnings()) == 1


def test_two_inquiries_do_not_share_the_direct_editor(database):
    first = _inquiry(database, external_id="D-A", content="첫 문의 내용")
    second = _inquiry(database, external_id="D-B", content="둘째 문의 내용")
    _draft(database, first, "답변 A")
    _draft(database, second, "답변 B")

    opened = _click(_panel(database, first), BUTTON_LABEL)
    assert _area(opened, QUESTION_LABEL).value == "첫 문의 내용"

    other = _panel(database, second)

    assert not other.exception
    assert not _has_area(other, QUESTION_LABEL)
    assert BUTTON_LABEL in _labels(other)
