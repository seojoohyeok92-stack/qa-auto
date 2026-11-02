"""GPT ② 가 쓰지 않은 승인 Learning 은 답변을 "검증" 하지 않는다 -- 10942.

``approved_learning_evidence`` 는 GPT ② 가 답을 쓰기 전에 정해진다. 승인됐고,
같은 판매 페이지이고, 단정적인 행이 있으면 ``APPROVED_LEARNING_SUPPORTED`` 다.
답변이 그 행을 썼는지, 같은 결론인지는 모른다.

서버 문의 10942 의 기록:

* 질문: "LS32FM700M-2MS 제품과 호환이 될까요??" (삼탠배터리 VIPB01W)
* evidence: usable, learning_ids [190232, 318190] -- 둘 다 43인치 모델에 대한
  "호환 불가" 이고, GPT ② 는 둘 다 IGNORED 로 판정했다.
* 답변: "호환 가능" (8932 사용)
* product_fact_guard: ``current_fact_verified: true``,
  ``current_fact_source: APPROVED_LEARNING``

수정 후 실GPT 재생에서도 같은 기록이 남았다 (답변은 보류, 사용 Learning 없음).
여기서 고정하는 것은 기록이다. GPT ② 가 답을 쓴 경로에서 ``auto_post_allowed``
는 conflict 가 없으면 이미 참이므로 게시 판정은 바뀌지 않는다 -- 그것도 함께
고정한다.
"""
from __future__ import annotations

import pytest

from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from tests.test_v4_evidence_generalization import _EvidenceReadingStub, _run

LISTING = "10358299339"
BATTERY = (
    "삼탠배터리 VIPB01W 무탠바이미 무선 삼탠바이미 만들기 결합식 배터리"
    " 이동식거치대 삼성 M5 80.1cm TV모니터 스탠드"
)
QUESTION_10942 = "LS32FM700M-2MS 제품과 호환이 될까요?"
ATOM_10942 = {
    "text": QUESTION_10942, "action": "PRODUCT_SPEC",
    "requested_information": "LS32FM700M-2MS 제품과의 호환 가능 여부",
    "requested_attribute": "COMPATIBILITY",
}

# 190232 의 모양: 사람이 게시한 답변, 같은 판매 페이지, 43인치 모델 "호환 불가".
NOT_COMPATIBLE_Q = "LS43FM703 모델에 호환 안되나요?"
NOT_COMPATIBLE_A = "안녕하세요 고객님\nLS43FM703 모델은 본 제품과 호환되지 않습니다."
# 8932 의 모양: 챗봇이 게시한 STYLE_ONLY 답변, M7 일반 문장.
M7_Q = "M7 LS32BM701UKXKR 모델이랑 호환 가능한가요?"
M7_A = (
    "M7 모니터는 삼탠배터리와 일반적인 사용 환경에서 호환 가능합니다. 다만, "
    "모니터 뒤쪽의 PD 단자(USB-C 전원공급 단자)는 사용할 수 없습니다."
)


def _seed(database):
    inquiries = InquiryRepository(database)
    learning = LearningRepository(database)
    ids = {}
    for key, question, answer, metadata, style_only in (
        ("no", NOT_COMPATIBLE_Q, NOT_COMPATIBLE_A, {
            "answer_provenance": "NAVER_POSTED",
            "customer_facing_truth": True,
            "facts_authority": "HUMAN_VERIFIED_NAVER_POSTED",
            "human_verified": True,
            "learning_signal_type": "POSITIVE",
            "product_scope": "PRODUCT_FAMILY",
            "verified_at": "2026-08-24T02:20:33.042+00:00",
            "verified_by": "local-admin",
        }, False),
        ("m7", M7_Q, M7_A, {
            "answer_provenance": "NAVER_POSTED",
            "facts_authority": "STYLE_ONLY",
            "learning_signal_type": "POSITIVE",
        }, True),
    ):
        origin = inquiries.upsert_work_item({
            "store_code": "OJE_PLUS", "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"seed-{key}", "inquiry_type": "PRODUCT_INQUIRY",
            "title": "상품 문의", "content": question, "product_name": BATTERY,
            "product_id": LISTING, "raw_json": {},
        }).inquiry_id
        row = learning.upsert({
            "source_key": f"guard-{key}",
            "inquiry_id": origin,
            "learning_source": "SELLER_ANSWER",
            "question_original_masked": question,
            "question_normalized": question,
            "product_name": BATTERY,
            "final_answer": answer,
            "style_only": style_only, "rating": 5, "edit_ratio": 0.0,
            "quality_score": 1.0, "version": 1, "style_features_json": {},
            "metadata_json": metadata,
            "active": True,
        })
        ids[key] = int(row["id"] if isinstance(row, dict) else row)
    return ids


def _pipeline(tmp_path, monkeypatch, name, *, question, atom, choose):
    """실제 생성 경로. ``choose(learning_rows)`` 가 GPT ② 의 선택을 대신한다."""

    seeded: dict[str, int] = {}

    def picker(context):
        rows = [
            item for item in (context.get("similar_approved_answers") or [])
            if isinstance(item, dict)
        ]
        return choose(rows, seeded)

    stub = _EvidenceReadingStub(picker)

    import tests.test_v4_evidence_generalization as harness

    real_copy = harness._copy_learning

    def seed_instead(database, _products, limit=60):
        seeded.update(_seed(database))
        return len(seeded)

    monkeypatch.setattr(harness, "_copy_learning", seed_instead)
    try:
        run = _run(
            tmp_path, monkeypatch, name=name, question=question,
            product_name=BATTERY, atoms=[atom], stub=stub,
            learning_products=(BATTERY,), product_id=LISTING,
        )
    finally:
        monkeypatch.setattr(harness, "_copy_learning", real_copy)
    assert run.error is None, run.error
    metadata = run.outcome.result.metadata
    guard = metadata.get("product_fact_guard") or {}
    draft = (metadata.get("hybrid") or {}).get("draft") or {}
    return guard, draft, seeded


def _use(key, answer, *, unresolved=()):
    def choose(rows, seeded):
        delivered = {int(item.get("learning_example_id") or 0) for item in rows}
        used = [seeded[key]] if key and seeded[key] in delivered else []
        return {
            "answer": answer, "used_learning_ids": used,
            "unresolved": list(unresolved), "can_auto_post": not unresolved,
        }
    return choose


def _evidence_ids(guard):
    return {
        int(item)
        for item in (guard.get("approved_learning_evidence") or {}).get(
            "learning_ids") or ()
    }


# --- 1. 사고 재현 ------------------------------------------------------------

def test_10942_unused_not_compatible_learning_does_not_verify_the_answer(
    tmp_path, monkeypatch,
):
    guard, draft, seeded = _pipeline(
        tmp_path, monkeypatch, "g10942",
        question=QUESTION_10942, atom=ATOM_10942,
        choose=_use("m7", "LS32FM700M-2MS(M7)는 일반적인 사용 환경에서 호환 가능합니다."),
    )

    # 전제: 반대 결론의 승인 Learning 이 "SUPPORTED" 로 판정돼 있다.
    evidence = guard.get("approved_learning_evidence") or {}
    assert evidence.get("reason") == "APPROVED_LEARNING_SUPPORTED", evidence
    assert seeded["no"] in _evidence_ids(guard)
    assert seeded["no"] not in {int(i) for i in draft.get("used_learning_ids") or ()}

    assert guard.get("current_fact_source") != "APPROVED_LEARNING", guard


def test_a_hedged_answer_using_nothing_is_not_verified_by_approved_learning(
    tmp_path, monkeypatch,
):
    """수정 후 실GPT 재생의 모양: 아무 Learning 도 쓰지 않고 보류."""

    guard, draft, seeded = _pipeline(
        tmp_path, monkeypatch, "g10942_hold",
        question=QUESTION_10942, atom=ATOM_10942,
        choose=_use(None, "현재 확인된 자료만으로는 확정하기 어렵습니다.",
                    unresolved=[QUESTION_10942]),
    )

    assert seeded["no"] in _evidence_ids(guard)
    assert guard.get("current_fact_source") is None, guard
    assert guard.get("current_fact_verified") is False, guard


# --- 2. 그대로여야 하는 것 ---------------------------------------------------

def test_the_publishing_verdict_is_unchanged(tmp_path, monkeypatch):
    """GPT ② 가 답을 쓴 경로의 ``auto_post_allowed`` 는 이 수정과 무관하다."""

    guard, _draft, _seeded = _pipeline(
        tmp_path, monkeypatch, "g10942_post",
        question=QUESTION_10942, atom=ATOM_10942,
        choose=_use(None, "현재 확인된 자료만으로는 확정하기 어렵습니다.",
                    unresolved=[QUESTION_10942]),
    )

    assert guard.get("auto_post_allowed") is True, guard


def test_an_approved_learning_the_answer_used_still_verifies_it(
    tmp_path, monkeypatch,
):
    """정확한 모델의 승인 Learning 을 GPT ② 가 쓰면 기존대로 APPROVED_LEARNING."""

    atom = dict(ATOM_10942, text=NOT_COMPATIBLE_Q,
                requested_information="LS43FM703 모델과의 호환 여부")
    guard, draft, seeded = _pipeline(
        tmp_path, monkeypatch, "g43",
        question=NOT_COMPATIBLE_Q, atom=atom,
        choose=_use("no", "LS43FM703 모델은 본 제품과 호환되지 않습니다."),
    )

    assert seeded["no"] in _evidence_ids(guard)
    assert seeded["no"] in {int(i) for i in draft.get("used_learning_ids") or ()}
    assert guard.get("current_fact_verified") is True, guard
    assert guard.get("current_fact_source") == "APPROVED_LEARNING", guard
    assert guard.get("auto_post_allowed") is True, guard
