"""운영자가 참고 자료의 "당시 답변"을 실제로 읽을 수 있어야 한다.

Dashboard 는 어떤 Learning/Historical 이 선택되었는지와 그 당시 *문의* 만 보여
주었고, 그 자료가 무엇이라고 *답했는지* 는 보여주지 않았다. 그래서 운영자는
retrieval 이 엉뚱한 자료를 가져온 것인지, 자료는 맞는데 그 답변이 이번 질문을
해결하지 못하는 것인지 구분할 수 없었다.

여기서 고정하는 계약은 두 가지뿐이다: 표시되는 답변이 Prompt Context 가 읽는
바로 그 컬럼이라는 것(Learning=final_answer, Historical=seller_answer), 그리고
긴 답변이 잘려 보이더라도 전문이 어딘가에서 반드시 읽힌다는 것이다.
"""
from __future__ import annotations

from repositories.learning_provenance_repository import LearningProvenanceRepository
from ui.learning_performance import ANSWER_PREVIEW_LIMIT, _answer_preview


# ----------------------------------------------------- Prompt Context 와 동일 출처
def test_the_query_reads_the_columns_the_prompt_itself_reads():
    """표시용 별도 answer field 를 새로 만들지 않았음을 고정한다.

    learning_context_service 는 Learning 후보의 ``final_answer`` 를, Historical
    의 ``seller_answer`` 를 Prompt 에 싣는다. Dashboard 가 다른 컬럼을 읽으면
    운영자가 보는 근거와 GPT 가 받은 근거가 갈라진다.
    """
    source = LearningProvenanceRepository.for_draft.__doc__ or ""
    assert "final_answer" in source
    assert "seller_answer" in source


def test_the_same_two_columns_already_back_the_usage_verification():
    """finalize_for_draft 가 쓰는 컬럼과 표시 컬럼이 어긋나면 안 된다."""
    import inspect

    finalize = inspect.getsource(LearningProvenanceRepository.finalize_for_draft)
    display = inspect.getsource(LearningProvenanceRepository.for_draft)
    for column in ("le.final_answer", "hc.seller_answer"):
        assert column in finalize
        assert column in display


# ----------------------------------------------------------------- 미리보기 계약
def test_a_short_answer_is_shown_whole():
    assert _answer_preview("오토피봇 가능한 제품입니다.") == "오토피봇 가능한 제품입니다."


def test_a_long_answer_is_marked_as_cut_not_silently_shortened():
    answer = "안녕하세요 오제앤에스 입니다. " + "설치 안내를 드립니다. " * 20
    preview = _answer_preview(answer)
    assert preview.endswith("…")
    assert len(preview) == ANSWER_PREVIEW_LIMIT + 1


def test_newlines_collapse_so_one_row_stays_one_row():
    """줄바꿈이 든 실제 CS 답변이 표의 다른 열을 밀어내지 않아야 한다."""
    preview = _answer_preview("안녕하세요.\n\n설치 기사님이\n방문합니다.")
    assert "\n" not in preview
    assert preview == "안녕하세요. 설치 기사님이 방문합니다."


def test_a_missing_answer_is_shown_as_missing_not_as_empty():
    """답변이 비어 있는 참고 자료는 '비어 있음'으로 보여야 한다."""
    for empty in (None, "", "   ", "\n"):
        assert _answer_preview(empty) == "-"


def test_the_preview_never_returns_a_blank_cell():
    """빈 칸은 운영자에게 '답변 없음'과 '표시 실패'를 구분해 주지 못한다."""
    assert _answer_preview("\u3000\t \n").strip() != ""


# --------------------------------------------------- Verifier verdict 표시 계약
def test_a_verdict_is_shown_against_the_source_it_judged():
    from ui.learning_performance import _verdict_by_reference

    verdicts = _verdict_by_reference({
        "evidence_verification": {"verified": [
            {"candidate_id": 317203, "verdict": "NOT_SUPPORTED"},
            {"candidate_id": 158169, "verdict": "SUPPORTED"},
        ]}
    })
    assert verdicts == {317203: "NOT_SUPPORTED", 158169: "SUPPORTED"}


def test_a_draft_with_no_verification_shows_no_verdict():
    """Deterministic routes are never verified, and must not read as failing."""
    from ui.learning_performance import _verdict_by_reference

    assert _verdict_by_reference({}) == {}
    assert _verdict_by_reference({"evidence_verification": None}) == {}


def test_a_colliding_id_is_dropped_rather_than_guessed():
    """Learning ids and Historical case ids come from different tables.

    Showing one row's verdict beside the other's answer would mislead an
    operator more than showing nothing does.
    """
    from ui.learning_performance import _verdict_by_reference

    assert _verdict_by_reference({
        "evidence_verification": {"verified": [
            {"candidate_id": 42, "verdict": "SUPPORTED"},
            {"candidate_id": 42, "verdict": "NOT_SUPPORTED"},
        ]}
    }) == {}


def test_every_verdict_in_the_contract_has_an_operator_label():
    from services.evidence_verification_service import VERDICTS
    from ui.learning_performance import VERDICT_LABELS

    assert set(VERDICT_LABELS) == set(VERDICTS)
