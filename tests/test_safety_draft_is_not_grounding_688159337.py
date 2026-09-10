"""안전 실패 초안은 GPT ② 의 근거가 아니다 -- 688159337.

서버 문의 688159337 "새 티비 설치하러 오실 때 집에 있는 오래된 티비도 같이
가져가 주실 수 있나요?" 는 관련 근거를 모두 찾아놓고도 직원 검토로 끝났다.

측정된 흐름은 이렇다.

* GPT ① 은 ``action=COLLECTION`` / ``need_template=false`` / ``need_learning=true``
  로 정확히 이해했다.
* 고정 rule "설치상품/공통안내" 가 어휘로 매칭됐지만 의미가 달라
  (``COLLECTION`` vs ``INSTALLATION_METHOD/PRODUCT_CONCEPT/REPAIR``)
  ``_exclude_semantic_rule_mismatch`` 가 정상적으로 폐기하고
  ``REVIEW_REQUIRED_SAFE_DRAFT`` 로 치환했다.
* Learning/Historical 6건이 검색되어 6건 모두 ``included_in_prompt=1`` 이었다.
* 그런데 그 안전 초안이 ``rule.answer`` 라는 Fact 로 GPT ② 에게 함께 전달됐다.
  provider 는 6건을 전부 IGNORED 로 처리하면서 그 이유를 스스로 적었다:
  "현재 적용되는 우선 답변에서 정확한 확인이 필요하다고 명시하고 있어..."

원인은 중립화 조건이 ``template_candidate_requested`` 를 첫 항으로 요구한 것이다.
GPT ① 이 Template 을 요청하지 않았으므로 그 값은 False 였고, 조건 전체가
자기 항을 하나도 읽기 전에 short-circuit 되어 안전 초안이 그대로 넘어갔다.

이 파일이 지키는 것은 그 경계다. 함수 반환값 하나가 아니라 실제
``AnswerService.generate_for_inquiry`` 를 태우고, GPT ② 가 실제로 받은 prompt 를
캡처해서 확인한다. 대조군은 같은 고정 rule 이 의미까지 맞는 문의로, 정상
Template 이 여전히 근거로 전달되는지를 같은 방식으로 확인한다.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from answer.engine import AnswerEngine
from answer.governance_models import GptProviderSettings
from answer.models import AnswerRequest
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.gpt_governance_service import GovernedHybridAnswerService
from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService

# 서버에서 복사해 둔 production data. READ ONLY 로만 연다.
SERVER_DB = Path("data/서버pc_data/data/oje_automation.db")

QUESTION_688159337 = (
    "테)새 티비 설치하러 오실 때 집에 있는 오래된 티비도 같이 가져가 주실 수"
    " 있나요? 고장난 건 아니고 새 제품 오면 필요가 없어서요."
)
PRODUCT_688159337 = (
    "삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 이동식 거치대"
)
SUBQUESTION = "새 TV 설치 방문 시 기존의 오래된 TV도 함께 수거해 주실 수 있나요?"

# 서버 forensic 에서 이 문의에 실제로 검색된 Learning. 값이 아니라 "근거가
# 프롬프트에 남아 있는가" 를 보기 위한 fixture 이므로 id 로만 가져온다.
RETRIEVED_LEARNING_IDS = (23755, 167848)

# 같은 고정 rule 이 매칭되는 대조군 문의. 이쪽은 의미도 rule 과 일치한다.
REPAIR_QUESTION = "설치 후 제품에 고장이나 불량이 있으면 어떻게 해야 하나요?"


def _semantic_payload(atoms, *, primary_action):
    return {
        "primary_action": primary_action,
        "secondary_actions": [],
        "request_type": "QUESTION",
        "objects": [],
        "atomic_questions": [dict(item) for item in atoms],
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": False,
        "requires_delivery_schedule": False,
        "asks_delivery_schedule": False,
        "asks_delivery_outcome": False,
        "purchase_state": "UNKNOWN",
        "confidence": 0.96,
    }


class _SemanticStub:
    """GPT ①. 서버가 실제로 낸 이해 결과를 재생한다."""

    name = "semantic_stub"

    def __init__(self, payload):
        self.payload = payload

    def generate_json(self, *, task, prompt, context):
        return dict(self.payload)


class _DraftStub:
    """GPT ②. production 이 만든 prompt 를 캡처하고 계약대로 답한다."""

    name = "draft_stub"

    def __init__(self, *, answer="설치 기사님 방문 시 요청하시면 수거가 진행됩니다."):
        self.captured: list[dict] = []
        self.answer = answer

    def generate_json(self, *, task, prompt, context):
        self.captured.append(
            {"task": task, "prompt": prompt, "context": context}
        )
        return {
            "answer": self.answer,
            "confidence": 0.9,
            "used_facts": [],
            "missing_information": [],
            "requires_review": False,
            "warnings": [],
            "learning_usage": [],
            "historical_usage": [],
            "subquestion_results": [],
            "evidence_decisions": [],
            "used_template_ids": [],
            "used_product_facts": [],
            "used_learning_ids": [],
            "used_historical_ids": [],
            "ignored_evidence": [],
            "unresolved": [],
            "can_auto_post": True,
            "reason": "replay",
        }


class _OrderSpy:
    def __init__(self):
        self.calls = 0

    def lookup_for_inquiry(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("주문 조회가 실행되면 안 됩니다")


def _copy_learning(target: Database, learning_ids: tuple[int, ...]) -> int:
    """서버 DB 의 Learning 행을 임시 DB 로 복사한다 (원본은 READ ONLY)."""

    if not SERVER_DB.exists():
        return 0
    source = sqlite3.connect(f"file:{SERVER_DB.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in learning_ids)
        rows = source.execute(
            f"SELECT * FROM learning_examples WHERE id IN ({marks})",
            learning_ids,
        ).fetchall()
        if not rows:
            return 0
        # learning_examples.inquiry_id 는 inquiries 를 참조한다. 참조를 끊으면
        # Learning 의 출처를 잃으므로 원본 문의도 함께 복사한다.
        inquiry_ids = sorted({
            int(row["inquiry_id"]) for row in rows
            if row["inquiry_id"] is not None
        })
        inquiries = []
        if inquiry_ids:
            marks = ",".join("?" for _ in inquiry_ids)
            inquiries = source.execute(
                f"SELECT * FROM inquiries WHERE id IN ({marks})", inquiry_ids,
            ).fetchall()
        with target.transaction() as connection:
            for table, table_rows in (
                ("inquiries", inquiries), ("learning_examples", rows),
            ):
                if not table_rows:
                    continue
                columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                names = [
                    name for name in table_rows[0].keys() if name in columns
                ]
                connection.executemany(
                    f"INSERT OR IGNORE INTO {table} ({','.join(names)})"
                    f" VALUES ({','.join('?' for _ in names)})",
                    [tuple(row[name] for name in names) for row in table_rows],
                )
        return len(rows)
    finally:
        source.close()


def _run(tmp_path, monkeypatch, *, name, question, atoms, primary_action,
         learning_ids=()):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    copied = _copy_learning(database, tuple(learning_ids)) if learning_ids else 0

    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "TEST",
        "source_question_id": f"safety-{name}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": question,
        "product_id": "11815213767",
        "product_name": PRODUCT_688159337,
        "option_name": None,
        "raw_json": {},
    }).inquiry_id

    draft_stub = _DraftStub()
    governed = GovernedHybridAnswerService(
        database, provider=draft_stub,
        settings=GptProviderSettings(provider_name="fake"),
    )
    order = _OrderSpy()

    import services.answer_service as answer_module

    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(answer_module, "notify_qna_safely", lambda **_k: False)

    outcome = None
    error = None
    try:
        outcome = AnswerService(
            database, hybrid_service=governed, order_lookup_service=order,
            semantic_analyzer=GptSemanticAnalyzerService(
                _SemanticStub(
                    _semantic_payload(atoms, primary_action=primary_action)
                )
            ),
        ).generate_for_inquiry(inquiry_id)
    except Exception as exc:  # noqa: BLE001 - fallback 경로도 관찰 대상
        error = exc

    drafts = [
        item for item in draft_stub.captured
        if str(item["task"]).upper() == "DRAFT"
    ]
    return SimpleNamespace(
        database=database, inquiry_id=inquiry_id, outcome=outcome, error=error,
        prompt=json.loads(drafts[-1]["prompt"]) if drafts else None,
        raw_prompt=drafts[-1]["prompt"] if drafts else "",
        order=order, copied_learning=copied,
    )


def _run_688159337(tmp_path, monkeypatch, *, learning_ids=()):
    return _run(
        tmp_path, monkeypatch, name="collection",
        question=QUESTION_688159337, primary_action="COLLECTION",
        atoms=[{
            "text": SUBQUESTION,
            "action": "COLLECTION",
            "requested_information": "새 TV 설치 방문 시 기존 TV 동시 수거 가능 여부",
            "requested_attribute": "PERMISSION_OR_OPTION",
        }],
        learning_ids=learning_ids,
    )


# ---------------------------------------------------------------------------
# 이 문의가 실제로 그 경계에 도달하는지 먼저 고정한다.
# ---------------------------------------------------------------------------

def test_the_fixed_rule_still_matches_this_inquiry_lexically():
    """어휘 매칭 자체는 유지된다 -- 폐기는 의미 판정이 하는 일이다."""

    result = AnswerEngine().generate(AnswerRequest(
        inquiry_id=1, question_id="688159337",
        inquiry_type="PRODUCT_INQUIRY",
        question=QUESTION_688159337, product_name=PRODUCT_688159337,
    ))
    assert result.matched_rule == "설치상품/공통안내"
    assert (result.metadata or {}).get("template_match_kind") == "FIXED_POLICY_INSTALL"


def test_the_boundary_is_reached_with_template_not_requested(
    tmp_path, monkeypatch,
):
    """COLLECTION 은 template action 이 아니므로 need_template 은 False 다.

    이 값이 True 로 바뀌면 이번 회귀는 다른 경로를 타게 되고, 아래 두
    assertion 은 버그가 살아 있어도 통과해 버린다. 그래서 먼저 고정한다.
    """

    run = _run_688159337(tmp_path, monkeypatch)
    assert run.prompt is not None, run.error
    understanding = (
        run.outcome.result.metadata["semantic_routing"]["understanding"]
    )
    assert understanding["usable"] is True
    assert understanding["need_template"] is False
    assert understanding["need_learning"] is True
    assert len(understanding["questions"]) == 1
    assert understanding["questions"][0]["action"] == "COLLECTION"

    # 고정 rule 이 의미 불일치 분류기로 폐기되던 경로는 제거됐다. 지금 지켜야
    # 하는 것은 더 강한 불변식이다: 그 rule 은 어떤 경우에도 최종 답변이 되지
    # 않고, 근거 후보로만 전달된다.
    metadata = run.outcome.result.metadata
    assert "semantic_rule_rejected" not in metadata
    assert str(
        metadata.get("selected_answer_route") or ""
    ).upper() not in {"TEMPLATE", "SAFE_RULE", "PRODUCT_DB"}
    facts = run.prompt.get("allowed_facts") or {}
    assert "rule.answer" not in facts

    assert run.order.calls == 0


# ---------------------------------------------------------------------------
# 1. 안전 초안은 authoritative Fact 로 전달되지 않는다.
# ---------------------------------------------------------------------------

def test_the_safety_draft_is_not_handed_to_gpt_as_rule_answer(
    tmp_path, monkeypatch,
):
    """REVIEW_REQUIRED_SAFE_DRAFT 는 allowed_facts 에 들어가지 않는다.

    서버에서는 이 초안 228자가 ``selected_facts`` 를 거쳐 프롬프트의
    ``allowed_facts`` / ``facts`` 에 그대로 실렸다 (측정: 두 블록 모두 365자,
    ``selected_facts`` JSON 과 바이트 일치).
    """

    run = _run_688159337(tmp_path, monkeypatch)
    assert run.prompt is not None, run.error

    facts = run.prompt.get("allowed_facts") or {}
    assert "rule.answer" not in facts
    assert "rule.answer" not in (run.prompt.get("facts") or {})

    selected = run.outcome.result.metadata["phase9"]["selected_facts"]
    assert "rule.answer" not in selected["keys"]
    assert "rule.answer" not in selected["values"]

    # 어떤 Fact 값으로도 되살아나면 안 된다. 문구를 비교하는 것이 아니라
    # 안전 초안이 지목하는 상태 -- "직원 검토가 필요하다" -- 가 Fact 로
    # 남아 있지 않은지 본다.
    assert not any(
        "직원 검토가 필요한 상태" in str(value) for value in facts.values()
    )

    # 상품 context 는 그대로 남아야 한다. 안전 초안을 걷어내는 일이
    # "어떤 상품인지"까지 지우면 안 된다.
    assert facts.get("product.name") == PRODUCT_688159337
    assert facts.get("product.product_id")


# ---------------------------------------------------------------------------
# 2. 안전 초안 때문에 관련 Learning 이 무시되지 않는다.
# ---------------------------------------------------------------------------

def test_related_learning_reaches_gpt_without_a_competing_rule_answer(
    tmp_path, monkeypatch,
):
    """검색된 근거가 프롬프트에 남고, 그 위에 우선 답변이 얹히지 않는다.

    서버에서 provider 는 6건을 전부 IGNORED 하면서 그 이유를 "현재 적용되는
    우선 답변에서 정확한 확인이 필요하다고 명시하고 있어" 라고 적었다.
    그 "우선 답변" 이 없어야 판단이 근거 쪽으로 열린다.
    """

    if not SERVER_DB.exists():
        pytest.skip("서버 data 사본이 없어 실제 Learning 으로 검증할 수 없습니다")

    run = _run_688159337(
        tmp_path, monkeypatch, learning_ids=RETRIEVED_LEARNING_IDS,
    )
    assert run.prompt is not None, run.error
    assert run.copied_learning == len(RETRIEVED_LEARNING_IDS)

    approved = run.prompt["input"].get("similar_approved_answers") or []
    assert approved, "검색된 승인 답변이 프롬프트에 남아야 합니다"

    # 근거는 남고, 근거보다 높은 우선순위를 가지는 rule.answer 는 없다.
    assert "rule.answer" not in (run.prompt.get("allowed_facts") or {})

    # 그 근거가 이 질문에 붙어 있어야 한다 -- 프롬프트에 떠 있기만 하면
    # 서버에서처럼 "관련은 있으나 적용하지 않았다" 로 흘려보낼 수 있다.
    evidence = run.prompt["input"].get("subquestion_evidence") or []
    assert evidence and any(
        (item.get("learning_ids") or item.get("historical_case_ids"))
        for item in evidence
    )


# ---------------------------------------------------------------------------
# 3. 정상 Fixed Template 은 그대로 근거로 전달된다 (대조군).
# ---------------------------------------------------------------------------

def test_a_semantically_matching_fixed_template_still_grounds_generation(
    tmp_path, monkeypatch,
):
    """같은 고정 rule, 같은 상품, need_template=False -- 다른 것은 의미뿐이다.

    "설치상품/공통안내" 의 answer_actions 에는 REPAIR 가 들어 있으므로 이
    문의는 의미 불일치가 아니다. 안전 초안으로 치환되지 않고, 따라서
    ``rule.answer`` 가 GPT ② 의 Fact 로 남아야 한다. 이번 수정이 정상
    Template 까지 걷어내지 않는다는 것을 같은 경계에서 확인한다.
    """

    run = _run(
        tmp_path, monkeypatch, name="repair",
        question=REPAIR_QUESTION, primary_action="REPAIR",
        atoms=[{
            "text": REPAIR_QUESTION,
            "action": "REPAIR",
            "requested_information": "고장 시 처리 방법",
            "requested_attribute": "PROCEDURE",
        }],
    )
    assert run.prompt is not None, run.error

    understanding = (
        run.outcome.result.metadata["semantic_routing"]["understanding"]
    )
    # 대조군도 같은 경계를 지난다 -- 달라지는 것은 의미 판정뿐이다.
    assert understanding["need_template"] is False

    metadata = run.outcome.result.metadata
    assert "semantic_rule_rejected" not in metadata
    assert metadata.get("safe_failure_code") is None

    # 이 대조군의 의미는 일치한다. 그래도 ``rule.answer`` 로는 전달되지 않는다:
    # 일치하는 Template 과 일치하지 않는 Template 을 분류기가 갈라내는 대신,
    # 둘 다 후보로 전달하고 판단을 GPT 에게 남긴다. 하나의 substring 일치가
    # 승인된 답변을 누르던 권한은 좋은 경우에서도 회수된다.
    facts = run.prompt.get("allowed_facts") or {}
    assert "rule.answer" not in facts
    candidates = run.prompt["input"].get("template_candidates") or []
    assert candidates, "일치하는 Template 이 후보로도 전달되지 않았다"


# ---------------------------------------------------------------------------
# 판별은 문구가 아니라 상태로 한다.
# ---------------------------------------------------------------------------

def test_no_draft_has_to_be_identified_because_none_is_grounding():
    """판별 자체가 필요 없어졌다.

    ``_is_review_required_safe_draft`` 는 "이건 우리 파이프라인이 스스로 쓴
    확인 요청 문구이니 근거로 쓰지 말라"를 상태로 판별하던 함수였다. 판별이
    필요했던 이유는 중립화가 조건부였기 때문이고, 그 조건이 사라졌으므로
    판별할 대상도 없다. 지켜야 하는 것은 조건이 돌아오지 않는 것이다.
    """
    import inspect

    import services.answer_service as module

    assert not hasattr(module, "_is_review_required_safe_draft")
    assert not hasattr(module, "_is_safe_rule_result")
    source = inspect.getsource(module.AnswerService.generate_for_inquiry)
    assert "gpt_rule_context = _neutral_gpt_context(" in source
    assert "else base_rule_result" not in source
