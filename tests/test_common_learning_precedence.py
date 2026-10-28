"""운영자가 승인한 공통 Learning 이 적용 범위 안에서 PK 보다 앞선다.

확정된 운영 정책이다. 전에는 Learning 이 카탈로그를 이길 수 있는 조건이
``product_match == EXACT_MODEL`` 하나였고, 그 시험은 "이 행이 바로 이 제품에
대한 것인가" 를 확인하려는 것이다. 공통 Learning 은 운영자가 "이 범위의 모든
제품에 대한 것" 이라고 결정한 행이므로 그 시험을 통과할 수 없고, 동시에 통과할
필요도 없다 -- 범위(활성·유효기간·마켓·모델·제품군)는 ``candidates()`` 와
compatibility 가 이미 걸렀고, 여기 도달한 행은 그것을 통과한 행이다.

우선권은 **명시적 승인에만** 준다. ``is_common_learning`` 은
``metadata_json.common_learning`` 을 읽고 그 키는 두 승인 경로만 쓰므로, 어휘
분류기가 POLICY 로 적어 둔 559 행과 일반 Positive 의 우선순위는 그대로다.

두 공통 Learning 이 서로 모순되면 어느 쪽도 이기지 않는다. 둘 다 카탈로그를
앞서므로 한쪽을 고르는 것은 운영자의 결정을 코드가 대신하는 일이고, 그래서
충돌이 그대로 흘러가 기존 CONFLICT 검토 경로를 탄다.
"""
from __future__ import annotations

import pytest

from services.learning_evidence_policy import (
    LEARNING_WINS,
    PRODUCT_FACT_WINS,
    fact_conflict_winner,
)


APPROVED_AT = "2026-10-01T00:00:00+00:00"
ANSWERED_AT_RAW = "2026-10-20T00:00:00+00:00"


def _answered_at():
    from datetime import datetime

    return datetime.fromisoformat(ANSWERED_AT_RAW)


def _row(
    *,
    common: bool = False,
    scope: str = "POLICY",
    answer: str = "이 라인업은 화면 터치를 지원합니다.",
    model_code: str = "",
    product_match: str = "POLICY_COMPATIBLE",
    provenance: str = "STAFF_EDITED",
    approved_at: str | None = APPROVED_AT,
    source: str = "DIRECT_INQUIRY",
) -> dict:
    """검색 파이프라인이 ``approved`` 목록에 넣는 모양의 행."""

    metadata: dict[str, object] = {
        "facts_authority": "APPROVED_REFERENCE",
        "learning_signal_type": "POSITIVE",
        "answer_provenance": provenance,
        "human_verified": True,
        "product_scope": scope,
        "market_applicability": "COMMON",
    }
    if approved_at:
        metadata["verified_at"] = approved_at
    if common:
        metadata["common_learning"] = True
        metadata["common_learning_source"] = source
        metadata["common_learning_approved_by"] = "op"
        metadata["common_learning_approved_at"] = approved_at
    return {
        "learning_example_id": 1,
        "answer": answer,
        "authority": "APPROVED",
        "model_code": model_code,
        "metadata_json": metadata,
        "compatibility": {"product_match": product_match},
        "matched_subquestion": "화면 터치 되나요?",
    }


def _winner(row, *, fact_value="미지원"):
    return fact_conflict_winner(
        row,
        fact_value=fact_value,
        answered_at=_answered_at(),
        asked_product_id=None,
        exact_model_for=lambda *_a, **_k: None,
    )


# ===========================================================================
# 공통 Learning 이 PK 를 앞선다
# ===========================================================================

@pytest.mark.parametrize("source", ["DIRECT_INQUIRY", "APPROVED_LEARNING"])
def test_an_operator_common_learning_outranks_the_catalogue(source):
    """두 승인 경로 모두 동일하게 적용된다."""

    assert _winner(_row(common=True, source=source)) == LEARNING_WINS


@pytest.mark.parametrize(
    "product_match",
    ["POLICY_COMPATIBLE", "CATEGORY_UNCERTAIN", "EXACT_NAME", "EXACT_MODEL"],
)
def test_the_exact_model_test_is_no_longer_required(product_match):
    """적용 범위는 검색 단계가 이미 걸렀으므로 모델 일치를 다시 묻지 않는다."""

    assert _winner(
        _row(common=True, product_match=product_match)
    ) == LEARNING_WINS


def test_a_common_learning_without_a_model_code_still_wins():
    assert _winner(_row(common=True, model_code="")) == LEARNING_WINS


def test_an_approval_time_is_not_required_for_a_common_learning():
    """승인 시각 창(90일)은 일반 Learning 의 조건이고 공통 승인에는 적용되지 않는다."""

    assert _winner(
        _row(common=True, approved_at=None)
    ) == LEARNING_WINS


def test_an_old_common_learning_still_wins():
    assert _winner(
        _row(common=True, approved_at="2024-01-01T00:00:00+00:00")
    ) == LEARNING_WINS


# ===========================================================================
# 우선순위를 바꾸지 않는 것들
# ===========================================================================

def test_an_inferred_policy_scope_does_not_get_the_new_precedence():
    """어휘 분류기가 적어 둔 POLICY 는 승인이 아니다 -- 기존 동작 그대로."""

    assert _winner(
        _row(common=False, scope="POLICY", product_match="POLICY_COMPATIBLE")
    ) is None


def test_an_ordinary_positive_keeps_the_existing_rule():
    """일반 Positive 는 EXACT_MODEL 이 아니면 여전히 판정 보류다."""

    assert _winner(
        _row(common=False, scope="MODEL", product_match="POLICY_COMPATIBLE")
    ) is None


def test_an_ordinary_positive_on_the_exact_model_is_unchanged():
    """EXACT_MODEL + 사람 승인 + 승인 시각이면 기존대로 Learning 이 이긴다."""

    assert _winner(
        _row(
            common=False, scope="MODEL", model_code="LH50BEHHLGFXKR",
            product_match="EXACT_MODEL",
        )
    ) == LEARNING_WINS


def test_an_ordinary_positive_outside_the_window_still_loses():
    assert _winner(
        _row(
            common=False, scope="MODEL", model_code="LH50BEHHLGFXKR",
            product_match="EXACT_MODEL",
            approved_at="2024-01-01T00:00:00+00:00",
        )
    ) == PRODUCT_FACT_WINS


# ===========================================================================
# 측정 조건은 공통 승인에도 그대로 적용된다
# ===========================================================================

def test_a_measurement_condition_still_blocks_a_winner():
    """같은 조건으로 측정된 수치인지 알 수 없으면 아무도 이기지 않는다."""

    assert _winner(
        _row(common=True, answer="가로 1110mm 입니다."),
        fact_value="1110.8 x 643.8 x 76.4 mm (스탠드포함)",
    ) is None


def test_the_same_condition_on_both_sides_lets_the_common_learning_win():
    assert _winner(
        _row(common=True, answer="스탠드포함 가로 1110mm 입니다."),
        fact_value="1110.8 mm (스탠드포함)",
    ) == LEARNING_WINS


# ===========================================================================
# 두 가지 행 모양 -- 투영된 근거 행과 저장소 행
# ===========================================================================

def test_both_row_shapes_are_recognised():
    """``evaluate`` 가 실제로 받는 행에는 ``metadata_json`` 이 없다.

    처음 구현은 metadata 만 읽어서, 단위 테스트는 통과하는데 실제
    파이프라인에서는 우선권이 적용되지 않았다. 두 모양을 모두 읽는다.
    """

    from services.learning_evidence_policy import is_operator_common_row

    projected = {"operator_common_policy": True, "answer": "x"}
    repository_row = _row(common=True)
    assert is_operator_common_row(projected) is True
    assert is_operator_common_row(repository_row) is True
    assert is_operator_common_row({"answer": "x"}) is False
    assert is_operator_common_row(_row(common=False)) is False


def test_a_projected_row_wins_against_the_catalogue():
    assert _winner(
        {
            "learning_example_id": 1,
            "answer": "이 라인업은 화면 터치를 지원합니다.",
            "authority": "APPROVED",
            "operator_common_policy": True,
            "compatibility": {"product_match": "POLICY_COMPATIBLE"},
        }
    ) == LEARNING_WINS


# ===========================================================================
# 실제 파이프라인 -- 적용 범위 안과 밖
# ===========================================================================

TV_50 = ("삼성 4K UHD 스마트 비즈니스 TV LH50BEHHLGFXKR 125.7cm(50인치)")
MONITOR = "삼성 모니터 LS32DM501EKXKR 80cm(32인치)"
SPEC_Q = "화면 터치 되나요?"
SPEC_A = "이 라인업은 화면 터치를 지원합니다."
SPEC_A_OTHER = "이 라인업 전 모델이 화면 터치를 지원합니다."
SPEC_NO = "이 라인업은 화면 터치를 지원하지 않습니다."


def _ordinary_row(repository, *, key, inquiry_id, product_name, answer):
    """운영자 공통 승인이 아닌 평범한 승인 Positive Learning."""

    return repository.upsert({
        "source_key": f"ord-{key}",
        "inquiry_id": inquiry_id,
        "learning_source": "APPROVED_EDITED",
        "question_original_masked": SPEC_Q,
        "question_normalized": SPEC_Q,
        "product_name": product_name,
        "final_answer": answer,
        "style_only": False, "rating": 5, "edit_ratio": 0.0,
        "quality_score": 0.9, "version": 1, "style_features_json": {},
        "metadata_json": {
            "facts_authority": "APPROVED_REFERENCE",
            "learning_signal_type": "POSITIVE",
            "answer_provenance": "STAFF_EDITED",
            "human_verified": True,
            "product_scope": "MODEL",
        },
        "active": True,
    })


def _pipeline(tmp_path, monkeypatch, name, *, common=True, store="OJE_PLUS",
              market="COMMON", validity=None, disable=False,
              ask_product=MONITOR, ask_store=None, commons=None,
              ordinary=None, atoms=None):
    """실제 생성 경로를 한 번 돌리고 Learning 근거 판정을 돌려준다.

    ``ask_product`` 의 카탈로그에는 ``touch_support_spec = 미지원`` 이 실제로
    들어 있다 (``_catalogue_touch_facts`` 가 그것을 고정한다). 그래서 공통
    Learning 이 "지원합니다" 라고 말하면 PK 충돌이 꾸며진 fact 없이 실제로
    만들어진다.
    """

    from repositories.database import Database
    from repositories.inquiry_repository import InquiryRepository
    from repositories.learning_repository import LearningRepository
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )
    from tests.test_v4_evidence_generalization import (
        _EvidenceReadingStub, _run,
    )

    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    repository = LearningRepository(database)
    inquiries = InquiryRepository(database)
    inquiry_id = inquiries.upsert_work_item({
        "store_code": store, "source_type": "PRODUCT_INQUIRY",
        "source_question_id": name, "inquiry_type": "PRODUCT_GENERAL",
        "title": "상품문의", "content": SPEC_Q, "product_name": TV_50,
        "product_id": "p-origin", "raw_json": {},
    }).inquiry_id
    answers = list(commons) if commons is not None else ([SPEC_A] if common
                                                         else [])
    for index, answer in enumerate(answers):
        origin = inquiry_id if index == 0 else inquiries.upsert_work_item({
            "store_code": store, "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"{name}-c{index}",
            "inquiry_type": "PRODUCT_GENERAL", "title": "상품문의",
            "content": SPEC_Q, "product_name": TV_50,
            "product_id": "p-origin", "raw_json": {},
        }).inquiry_id
        created = repository.add_common_learning_for_inquiry(
            origin, question=SPEC_Q, answer=answer,
            product_scope="POLICY", market_applicability=market,
            actor="op", **(validity or {}),
        )
        if disable:
            row = repository.get(int(created["id"]))
            repository.update_validity(
                int(created["id"]), validity_type="PERMANENT",
                validity_active=False, condition={},
                expected_updated_at=str(row["updated_at"]),
            )
    if ordinary is not None:
        product_name, answer = ordinary
        origin = inquiries.upsert_work_item({
            "store_code": store, "source_type": "PRODUCT_INQUIRY",
            "source_question_id": f"{name}-ord",
            "inquiry_type": "PRODUCT_GENERAL", "title": "상품문의",
            "content": SPEC_Q, "product_name": product_name,
            "product_id": "p-ord", "raw_json": {},
        }).inquiry_id
        _ordinary_row(repository, key=name, inquiry_id=origin,
                      product_name=product_name, answer=answer)
    result = _run(
        tmp_path, monkeypatch, name=name, question=SPEC_Q,
        product_name=ask_product,
        atoms=atoms or [{
            "text": SPEC_Q, "action": "PRODUCT_SPEC",
            "requested_information": "터치 지원",
            "requested_attribute": "SPEC_VALUE",
        }],
        stub=_EvidenceReadingStub(),
    )
    draft = dict(getattr(result.outcome, "draft", None) or {})
    metadata = draft.get("metadata_json") or {}
    guard = metadata.get("product_fact_guard") or {}
    eligibility = None
    if draft:
        asked = inquiries.get(int(draft.get("inquiry_id") or 0)) or {}
        eligibility = AutoProcessingEligibilityService().evaluate(
            inquiry=dict(asked), draft=draft,
            route=str(metadata.get("selected_answer_route") or "").upper()
            or "HYBRID",
        )
    return {
        "evidence": guard.get("approved_learning_evidence") or {},
        "prompt": result.prompt,
        "draft": draft,
        "hybrid": metadata.get("hybrid") or {},
        "eligibility": eligibility,
    }


def test_a_common_learning_now_qualifies_in_the_real_pipeline(
    tmp_path, monkeypatch,
):
    """바뀐 지점이 실제로 닿는다는 증거.

    전에는 ``_qualifying`` 이 ``EXACT_PRODUCT_MATCHES`` 로 먼저 걸러서
    교차 모델 공통 Learning 이 비교에 들어오지 못했고, 판정은
    ``NO_QUALIFYING_APPROVED_LEARNING`` 이었다.
    """

    info = _pipeline(tmp_path, monkeypatch, "prec_in")

    evidence = info["evidence"]
    assert evidence.get("usable") is True, evidence
    assert evidence.get("reason") == "APPROVED_LEARNING_SUPPORTED", evidence
    assert evidence.get("learning_ids"), evidence


def test_without_a_common_learning_nothing_changes(tmp_path, monkeypatch):
    info = _pipeline(tmp_path, monkeypatch, "prec_none", common=False)

    evidence = info["evidence"]
    assert evidence.get("usable") is False
    assert evidence.get("reason") == "NO_APPROVED_LEARNING"


def test_gpt2_is_told_the_precedence(tmp_path, monkeypatch):
    info = _pipeline(tmp_path, monkeypatch, "prec_prompt")

    payload = (info["prompt"] or {}).get("input") or {}
    policy = payload.get("subquestion_answer_policy") or {}
    assert "OPERATOR_COMMON_POLICY" in policy, sorted(policy)
    assert "outrank" in policy["OPERATOR_COMMON_POLICY"]
    rows = [
        item for item in (payload.get("similar_approved_answers") or ())
        if isinstance(item, dict)
    ]
    assert rows, "공통 Learning 이 근거로 전달되지 않았다"
    assert [item.get("operator_common_policy") for item in rows] == [True]


@pytest.mark.parametrize(
    "name, kwargs",
    [
        ("prec_market", {"market": "COUPANG_ONLY"}),
        ("prec_expired", {"validity": {
            "validity_type": "TEMPORARY", "event_name": "끝난 행사",
            "valid_from": "2026-01-01", "valid_until": "2026-06-30",
        }}),
        ("prec_future", {"validity": {
            "validity_type": "TEMPORARY", "event_name": "다가올 행사",
            "valid_from": "2026-12-01", "valid_until": "2026-12-31",
        }}),
        ("prec_disabled", {"disable": True}),
    ],
)
def test_outside_its_range_a_common_learning_gets_no_precedence(
    tmp_path, monkeypatch, name, kwargs,
):
    """적용 범위 밖에서는 기존 PK 검증과 우선순위가 그대로다.

    범위는 이 모듈이 다시 확인하지 않는다 -- ``candidates()`` 와
    compatibility 가 먼저 걸러 행이 아예 도달하지 않는다.
    """

    info = _pipeline(tmp_path, monkeypatch, name, **kwargs)

    evidence = info["evidence"]
    assert evidence.get("usable") is not True, evidence
    assert evidence.get("learning_ids") in (None, [], ()), evidence


# ===========================================================================
# A. 충돌이 실제로 만들어지는가 -- 진짜 카탈로그 fact 로
# ===========================================================================

def _projected(learning_id, answer, *, common, product_match=None):
    """``evaluate`` 가 실제로 받는 투영 행. metadata_json 은 여기에 없다."""

    return {
        "learning_example_id": learning_id,
        "answer": answer,
        "authority": "APPROVED",
        "evidence_authority": "APPROVED",
        "model_code": "" if common else "LS32DM501EKXKR",
        "operator_common_policy": bool(common),
        "compatibility": {
            "product_match": product_match or (
                "POLICY_COMPATIBLE" if common else "EXACT_MODEL"
            ),
        },
        "matched_subquestion": SPEC_Q,
        "answer_support": 0.6,
        "relevance": 1.2,
        "style_only": False,
    }


def _catalogue_facts(question):
    """운영 카탈로그가 이 모니터에 대해 내놓는 fact (꾸민 값이 아니다)."""

    from services.product_knowledge_service import ProductKnowledgeService

    result = ProductKnowledgeService().facts_for_inquiry(
        product_id=None, question=question, model_code=None,
        product_name=MONITOR, option_name="",
    )
    assert result.matched is True
    return tuple(result.facts_in_question_scope(question))


def test_the_catalogue_really_says_this_model_has_no_touch():
    """아래 충돌 사례의 전제. 카탈로그가 바뀌면 조용히 통과하지 않고 여기서 깨진다."""

    facts = _catalogue_facts(SPEC_Q)
    assert [(item.field_key, item.value) for item in facts] == [
        ("touch_support_spec", "미지원")
    ], facts


def test_a_common_answer_against_the_catalogue_forms_a_real_conflict():
    """``fact_conflicts`` 의 속성·조건 요건을 실제로 충족한다."""

    from services.learning_evidence_policy import fact_conflicts

    found = fact_conflicts(
        [_projected(1, SPEC_A, common=True)], _catalogue_facts(SPEC_Q)
    )
    assert [
        (item["learning_id"], item["field_key"], item["fact_value"])
        for item in found
    ] == [(1, "touch_support_spec", "미지원")], found


def test_an_agreeing_common_answer_forms_no_conflict():
    from services.learning_evidence_policy import fact_conflicts

    assert fact_conflicts(
        [_projected(1, SPEC_NO, common=True)], _catalogue_facts(SPEC_Q)
    ) == ()


# ===========================================================================
# B. 감지된 충돌을 누가 가져가는가
# ===========================================================================

def _decide(rows, *, facts=None):
    from services.learning_evidence_policy import evaluate

    return evaluate(
        learning_context={
            "similar_approved_answers": list(rows),
            "historical_cases": [],
            "subquestion_evidence": [{
                "subquestion": SPEC_Q, "status": "CANDIDATE",
                "source": "ACTIVE_POSITIVE_LEARNING",
            }],
        },
        safe_facts=facts if facts is not None else _catalogue_facts(SPEC_Q),
        answered_at=_answered_at(),
        exact_model_for=lambda *_a, **_k: None,
    ).to_dict()


def test_the_operator_row_settles_the_catalogue_conflict():
    decision = _decide([_projected(1, SPEC_A, common=True)])

    assert decision["usable"] is True, decision
    assert decision["reason"] == "APPROVED_LEARNING_SUPPORTED"
    assert decision["conflict"] is False
    assert decision["learning_ids"] == [1]


def test_an_ordinary_row_in_the_same_conflict_does_not_get_the_win():
    """같은 fact, 같은 문장, 승인 출처만 다르다.

    이 투영 행에는 승인 이력이 없다. ``fact_conflict_winner`` 의 기존 규칙대로
    불분명한 승인 이력은 카탈로그를 앞에 두므로 (``PRODUCT_FACT``) 이 행은
    근거에서 빠진다. 바뀐 우선순위는 이 행에 닿지 않는다.
    """

    decision = _decide([_projected(1, SPEC_A, common=False)])

    assert decision["usable"] is False, decision
    assert decision["reason"] == "NO_QUALIFYING_APPROVED_LEARNING"
    assert decision["learning_ids"] == []


# ===========================================================================
# 공통끼리 모순 -- 어느 쪽도 이기지 않는다
# ===========================================================================

def test_two_operator_rows_that_contradict_each_other_are_a_standoff():
    """운영자의 결정을 코드가 대신하지 않고 CONFLICT 로 보낸다."""

    decision = _decide([
        _projected(1, SPEC_A, common=True),
        _projected(2, SPEC_NO, common=True),
    ])

    assert decision["usable"] is False, decision
    assert decision["conflict"] is True
    assert decision["learning_ids"] == []


def test_two_operator_rows_that_agree_in_different_words_still_win():
    """문구가 다른 것은 모순이 아니다. 충돌 목록을 세던 방식이 이것을 놓쳤다."""

    decision = _decide([
        _projected(1, SPEC_A, common=True),
        _projected(2, SPEC_A_OTHER, common=True),
    ])

    assert decision["usable"] is True, decision
    assert decision["conflict"] is False
    assert sorted(decision["learning_ids"]) == [1, 2]


def test_an_ordinary_row_does_not_create_the_standoff():
    """대립 판정은 운영자 승인 행끼리만 한다.

    반대로 말하는 일반 Learning 이 옆에 있어도 공통 Learning 의 우선권은
    유지된다. 그 일반 행은 카탈로그와 같은 말을 하므로 충돌 자체가 없고,
    근거에 그대로 남는다 -- 이것도 기존 동작이다.
    """

    decision = _decide([
        _projected(1, SPEC_A, common=True),
        _projected(2, SPEC_NO, common=False),
    ])

    assert decision["usable"] is True, decision
    assert decision["conflict"] is False
    assert 1 in decision["learning_ids"], decision


def test_the_standoff_is_only_about_the_same_attribute():
    """다른 속성에 대한 두 공통 답변은 서로 모순이 아니다."""

    decision = _decide([
        _projected(1, SPEC_A, common=True),
        _projected(2, "이 라인업은 스피커를 내장하지 않습니다.", common=True),
    ])

    assert decision["usable"] is True, decision
    assert decision["conflict"] is False
    assert sorted(decision["learning_ids"]) == [1, 2]


def test_two_operator_rows_with_different_measurements_are_a_standoff():
    """측정값 축도 같다 -- 같은 단위로 다른 수를 말하면 운영자 검토로 간다."""

    decision = _decide([
        _projected(1, "스탠드를 포함한 무게는 7kg 입니다.", common=True),
        _projected(2, "스탠드를 포함한 무게는 9kg 입니다.", common=True),
    ], facts=_catalogue_facts("무게가 어떻게 되나요?"))

    assert decision["usable"] is False, decision
    assert decision["conflict"] is True


# ===========================================================================
# 파이프라인 -- Validator 와 Eligibility 까지
# ===========================================================================

def test_in_range_the_pipeline_keeps_the_learning_and_stays_safe(
    tmp_path, monkeypatch,
):
    """우선순위가 GPT ② 지침에 적혀 있다는 것과 답변 검증을 통과한다는 것은 다르다."""

    info = _pipeline(tmp_path, monkeypatch, "prec_safe")

    evidence = info["evidence"]
    assert evidence.get("usable") is True, evidence
    assert evidence.get("conflict") is not True, evidence
    statuses = [
        item.get("status")
        for item in (info["hybrid"].get("subquestion_evidence") or [])
        if isinstance(item, dict)
    ]
    assert "CONFLICT" not in statuses, statuses
    assert info["draft"].get("validation_status") == "PASS", info["draft"]
    assert info["eligibility"].decision == "SAFE", info["eligibility"]
    assert "EVIDENCE_CONFLICT" not in list(info["eligibility"].reasons)


def test_the_same_conflict_with_an_ordinary_learning_goes_to_review(
    tmp_path, monkeypatch,
):
    """대조군. 같은 fact·같은 문장이고 승인 출처만 다르다."""

    info = _pipeline(
        tmp_path, monkeypatch, "prec_ordinary", common=False,
        ordinary=(MONITOR, SPEC_A),
    )

    evidence = info["evidence"]
    assert evidence.get("conflict") is True, evidence
    assert evidence.get("reason") == "PRODUCT_FACT_VS_LEARNING_CONFLICT"
    assert info["draft"].get("validation_status") == "REVIEW_REQUIRED"
    assert info["eligibility"].safe is False
    assert "EVIDENCE_CONFLICT" in list(info["eligibility"].reasons)


def test_two_contradicting_common_learnings_go_to_review(
    tmp_path, monkeypatch,
):
    info = _pipeline(
        tmp_path, monkeypatch, "prec_standoff", commons=[SPEC_A, SPEC_NO],
    )

    assert info["evidence"].get("conflict") is True, info["evidence"]
    assert info["eligibility"].safe is False
    assert "EVIDENCE_CONFLICT" in list(info["eligibility"].reasons)


def test_two_agreeing_common_learnings_stay_safe(tmp_path, monkeypatch):
    info = _pipeline(
        tmp_path, monkeypatch, "prec_agree", commons=[SPEC_A, SPEC_A_OTHER],
    )

    assert info["evidence"].get("usable") is True, info["evidence"]
    assert info["evidence"].get("conflict") is not True
    assert info["eligibility"].decision == "SAFE"


# ===========================================================================
# 복합 문의 -- 공통 Learning 과 무관한 사양을 함께 묻는 경우
# ===========================================================================

def _compound(tmp_path, monkeypatch, name, **kwargs):
    """터치(공통 Learning)와 해상도(카탈로그)를 함께 묻는다."""

    return _pipeline(
        tmp_path, monkeypatch, name,
        atoms=[
            {"text": SPEC_Q, "action": "PRODUCT_SPEC",
             "requested_information": "터치 지원",
             "requested_attribute": "SPEC_VALUE"},
            {"text": "해상도는 어떻게 되나요?", "action": "PRODUCT_SPEC",
             "requested_information": "해상도",
             "requested_attribute": "SPEC_VALUE"},
        ],
        **kwargs,
    )


def test_a_compound_inquiry_keeps_both_subquestions_answerable(
    tmp_path, monkeypatch,
):
    """공통 Learning 이 터치를 가져가도 해상도 질문이 CONFLICT 로 끌려가지 않는다."""

    info = _compound(tmp_path, monkeypatch, "prec_compound")

    entries = [
        item for item in (info["hybrid"].get("subquestion_evidence") or [])
        if isinstance(item, dict)
    ]
    assert len(entries) == 2, entries
    assert [item.get("status") for item in entries] == [
        "CANDIDATE", "CANDIDATE"
    ], entries
    assert info["eligibility"].decision == "SAFE", info["eligibility"]


def test_the_common_row_is_only_weighed_against_its_own_attribute(
    tmp_path, monkeypatch,
):
    """해상도 fact 는 터치 답변의 충돌이 되지 않는다 -- ``scope_for`` 가 거른다."""

    info = _compound(tmp_path, monkeypatch, "prec_compound_scope")

    conflicts = (info["evidence"].get("conflicts") or ())
    assert [item.get("field_key") for item in conflicts] == [], conflicts
    assert info["evidence"].get("usable") is True, info["evidence"]
