"""PK ↔ Learning 3개월 우선권이 실제 경로에서 적용되고 GPT ② 까지 전달된다.

재현된 결함 두 가지

  1. GPT ② 로 가는 행(``similar_approved_answers`` 투영)에는 ``metadata_json`` 이
     없다. ``fact_conflict_winner`` 는 승인 여부를 그 메타데이터에서 읽었으므로
     실제 경로에서는 모든 행이 미승인으로 읽혀 PK 가 항상 이겼다. 단위 테스트는
     메타데이터를 직접 넣어서 통과했다.
  2. 승패가 정해져도 판정 기록만 바뀌었다. 양쪽 수치가 그대로 프롬프트에 들어가
     어느 쪽이 그 속성의 답인지 GPT ② 가 알 수 없었다.

승인 범위: 운영자의 "마켓 판매자 답변 Learning 승인" 경로만 쓰는 표시
(``facts_authority=HUMAN_VERIFIED_NAVER_POSTED`` + ``verified_by``) 가 있는 판매자
답변은 APPROVED_EDITED/UNEDITED 와 같은 건별 승인이다. 26.11.12 서버 사본의
SELLER_ANSWER_VERIFIED 708건 모두 이 표시와 개별 승인 이벤트를 갖는다. 표시가
없는 human_verified 행(가져오기·일괄 검증)과 과거 사례 승격은 제외된다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.learning_evidence_policy import (
    LEARNING_WINS,
    PRODUCT_FACT_WINS,
    evaluate,
    fact_conflict_winner,
    operator_approval_class,
)
from services.similar_answer_service import _has_human_conflict_authority

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)
LISTING = "10914781557"  # 카탈로그 EXACT 경로로 LH50BEDHLGFXKR 하나를 판다
NO_SPEAKER = "안녕하세요. 해당 제품은 내장 스피커가 없습니다."
SUBQUESTION = "스피커 있나요?"


def seller_row(*, days_ago=10, per_item=True, **extra):
    metadata = {
        "answer_provenance": "NAVER_POSTED",
        "human_verified": True,
        "verified_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "learning_topics": ["AUDIO"],
    }
    if per_item:
        metadata["facts_authority"] = "HUMAN_VERIFIED_NAVER_POSTED"
        metadata["verified_by"] = "local-admin"
    return {
        "id": 7, "learning_example_id": 7, "learning_source": "SELLER_ANSWER",
        "model_code": "LH50BEDHLGFXKR", "answer": NO_SPEAKER,
        "final_answer": NO_SPEAKER, "matched_subquestion": SUBQUESTION,
        "metadata_json": metadata, "authority": "APPROVED",
        "compatibility": {"product_match": "EXACT_MODEL"}, **extra,
    }


def projected(row):
    """``SimilarAnswerService`` 가 GPT ② 쪽으로 넘기는 모양 -- 메타데이터 없음."""

    out = {key: value for key, value in row.items()
           if key not in {"metadata_json", "model_code", "id"}}
    out["operator_approval"] = operator_approval_class(row)
    out["approved_at"] = row["metadata_json"]["verified_at"]
    out["source_product_id"] = LISTING
    return out


# ------------------------------------------------------------ 승인 범위
def test_a_per_item_approved_seller_answer_is_operator_approved():
    assert operator_approval_class(seller_row()) == "SELLER_ANSWER_VERIFIED"
    # PK 우선권만 받는다. Learning ↔ Learning 삭제 권한은 범위(POLICY/GENERAL)에
    # 상품 경계가 없어 다른 상품 배송 행을 지웠으므로 주지 않는다.
    assert not _has_human_conflict_authority(seller_row())


def test_a_verified_flag_without_the_approval_path_is_not():
    row = seller_row(per_item=False)
    assert operator_approval_class(row) is None
    assert not _has_human_conflict_authority(row)


def test_promoted_history_is_not_operator_approved():
    row = seller_row()
    row["metadata_json"]["source_origin"] = "HISTORICAL_PROMOTED"
    assert operator_approval_class(row) is None


# --------------------------------------------------- 3개월 창 (저장 행 모양)
def test_a_recent_per_item_seller_answer_outranks_the_catalogue():
    assert fact_conflict_winner(
        seller_row(days_ago=30), fact_value="True", answered_at=NOW,
    ) == LEARNING_WINS


def test_an_old_per_item_seller_answer_loses_to_the_catalogue():
    assert fact_conflict_winner(
        seller_row(days_ago=120), fact_value="True", answered_at=NOW,
    ) == PRODUCT_FACT_WINS


# -------------------------------------------- 실제 경로의 투영 행 (결함 1)
def test_the_projected_row_is_judged_like_the_stored_one():
    from services.hybrid_answer_service import _exact_model_for_listing

    for days_ago, expected in ((30, LEARNING_WINS), (120, PRODUCT_FACT_WINS)):
        row = projected(seller_row(days_ago=days_ago))
        assert "metadata_json" not in row and "model_code" not in row
        assert fact_conflict_winner(
            row, fact_value="True", answered_at=NOW,
            asked_product_id=LISTING, exact_model_for=_exact_model_for_listing,
        ) == expected


def test_an_unapproved_projected_row_never_outranks_the_catalogue():
    from services.hybrid_answer_service import _exact_model_for_listing

    row = projected(seller_row(per_item=False))
    assert row["operator_approval"] is None
    assert fact_conflict_winner(
        row, fact_value="True", answered_at=NOW,
        asked_product_id=LISTING, exact_model_for=_exact_model_for_listing,
    ) == PRODUCT_FACT_WINS


# ------------------------------------------------- GPT ② 에 전달 (결함 2)
def _context(*rows):
    return {
        "similar_approved_answers": [dict(row) for row in rows],
        "subquestion_evidence": [{
            "subquestion": SUBQUESTION, "status": "CANDIDATE",
            "source": "ACTIVE_POSITIVE_LEARNING",
        }],
    }


def _apply(context):
    from services.hybrid_answer_service import HybridAnswerService

    fact = SimpleNamespace(field_key="speaker_present", value="True")
    knowledge = SimpleNamespace(facts_in_question_scope=lambda _q: [fact])
    request = SimpleNamespace(
        question=SUBQUESTION,
        metadata={"product_knowledge": knowledge, "product_id": LISTING},
    )
    return HybridAnswerService._apply_evidence_conflicts(request, context)


def test_the_settled_side_is_written_onto_the_row_gpt2_reads():
    recent = projected(seller_row(days_ago=10))
    other = {**projected(seller_row(days_ago=10)), "learning_example_id": 8,
             "answer": "리모컨이 포함되어 있습니다.",
             "final_answer": "리모컨이 포함되어 있습니다."}
    context = _apply(_context(recent, other))

    rows = {row["learning_example_id"]: row
            for row in context["similar_approved_answers"]}
    assert rows[7]["catalog_conflicts"] == [{
        "field_key": "speaker_present", "catalog_value": "True",
        "use": "THIS_ROW",
    }]
    # 충돌하지 않는 근거는 손대지 않는다.
    assert "catalog_conflicts" not in rows[8]
    settled = context["approved_learning_evidence"]["settled"]
    assert [item["resolution"] for item in settled] == [LEARNING_WINS]
    # 정해진 충돌은 CONFLICT 보류가 아니다.
    assert context["subquestion_evidence"][0]["status"] != "CONFLICT"


def test_an_old_row_is_marked_to_use_the_catalogue(monkeypatch):
    import services.hybrid_answer_service as hybrid

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(hybrid, "datetime", _Clock)
    old = projected(seller_row(days_ago=120))
    context = _apply(_context(old))
    row = context["similar_approved_answers"][0]
    assert row["catalog_conflicts"][0]["use"] == "PRODUCT_CATALOG"


def test_the_approval_facts_stay_out_of_the_prompt():
    from services.learning_context_service import prompt_context

    row = projected(seller_row())
    shown = prompt_context({"similar_approved_answers": [row]})
    item = shown["similar_approved_answers"][0]
    assert "operator_approval" not in item and "approved_at" not in item


def test_gpt2_is_told_how_to_read_catalog_conflicts():
    import inspect

    import answer.prompt_builder as builder
    import services.learning_context_service as context_module

    assert "CATALOG_CONFLICTS" in inspect.getsource(context_module)
    assert "catalog_conflicts 에 use THIS_ROW" in inspect.getsource(builder)


def test_evaluate_reports_nothing_settled_without_a_conflict():
    decision = evaluate(
        learning_context=_context(projected(seller_row())),
        safe_facts=[SimpleNamespace(field_key="speaker_present",
                                    value="False")],
        answered_at=NOW,
    )
    assert decision.settled == ()


# --------------------------------- 속성 문장의 극성만 비교 (26.11.12 역검증)
def test_another_sentences_negative_does_not_contradict_this_field():
    """319310: RF단자 문장으로 ethernet 에 닿고, 무선디스플레이 '불가' 로 충돌했다."""

    from services.learning_evidence_policy import fact_conflicts

    row = seller_row()
    row["answer"] = row["final_answer"] = (
        "안녕하세요 고객님\n노트북 무선디스플레이 연결은 불가합니다.\n"
        "RF단자 있는 모델입니다.")
    assert fact_conflicts(
        [row], [SimpleNamespace(field_key="ethernet", value="1")]) == ()


def test_the_fields_own_sentence_still_contradicts():
    from services.learning_evidence_policy import fact_conflicts

    row = seller_row()
    row["answer"] = row["final_answer"] = (
        "안녕하세요. 리모컨은 포함되어 있습니다. 내장 스피커는 없습니다.")
    found = fact_conflicts(
        [row], [SimpleNamespace(field_key="speaker_present", value="True")])
    assert [item["field_key"] for item in found] == ["speaker_present"]
