"""삼탠배터리 M7 호환 안내는 배터리 호환 목록에 있는 모델에만 나간다 -- 10942.

서버 문의 10942 (판매 페이지 10358299339, 삼탠배터리 VIPB01W) 의 질문은
"LS32FM700M-2MS 제품과 호환이 될까요??" 였다. 고객은 "M7" 이라고 쓰지 않았다.

* 고정 rule "배터리호환" 은 ``"m7" in compact(question)`` 으로 매칭된다.
  "ls32fm700m-2ms" 는 "fm7" 안에 "m7" 을 품고 있어 rule 이 매칭됐고,
  "M7 모니터는 삼탠배터리와 ... 사용 가능합니다" 가 ``FIXED_PRODUCT_ACCESSORY``
  Template 으로 GPT ② 에게 전달됐다.
* GPT ② 는 그 Template 과 Learning 8932 (LS32BM701, 같은 Template 문구의 챗봇
  답변) 를 근거로 "호환 가능" 을 답했고, SAFE 로 자동등록됐다.
* 직원은 "LS32FM700M-2MS = 삼성 무빙스타일 제품으로 이동스탠드가 본 제품과
  호환 불가능" 으로 정정했다.

카탈로그 ``BATTERY_CATALOG`` 는 이 배터리가 맞는 모니터를 모델코드로 적어 둔다
(M7 32인치 = S32DM70x, S32CM70x, S32BM70x). S32FM700 은 거기에 없고, 직원이
호환 불가로 답한 43인치 M7 (LS43FM703, LS43BM702) 도 없다.

여기서 고정하는 것은 그 경계다.

* 질문이 목록에 없는 모델코드를 적으면 M7 문장은 나가지 않고, rule 사슬의
  다음 규칙 ("32인치 이하 삼성 모니터는 FMS 스탠드에 호환") 으로 새지도 않는다.
* 목록에 있는 모델코드, 모델코드 없이 "M7" 만 적은 질문, M5 문장은 그대로다.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from answer.config_loader import load_answer_config
from answer.engine import AnswerEngine
from answer.governance_models import GptProviderSettings
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.gpt_governance_service import GovernedHybridAnswerService
from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService

BATTERY = (
    "삼탠배터리 VIPB01W 무탠바이미 무선 삼탠바이미 만들기 결합식 배터리"
    " 이동식거치대 삼성 M5 80.1cm TV모니터 스탠드"
)
STAND = "3세대 삼탠바이미 이동식 TV 거치대 삼성 M5 M7 M8 LG 룸앤티비 스마트 모니터"
QUESTION_10942 = "안녕하세요 !\nLS32FM700M-2MS\n\n제품과 호환이 될까요??"
M7_SENTENCE = "M7 모니터는 삼탠배터리와"


@pytest.fixture(scope="module")
def engine():
    return AnswerEngine()


def _answer(engine, product, question):
    return engine.answer(product, question)


# --- 1. 사고 재현 ------------------------------------------------------------

def test_10942_does_not_get_the_m7_compatibility_sentence(engine):
    result = _answer(engine, BATTERY, QUESTION_10942)

    assert M7_SENTENCE not in result.answer
    assert "호환 가능" not in result.answer
    assert result.answer == ""
    assert result.category == "배터리호환/목록외모델"


def test_10942_does_not_fall_through_to_the_stand_sentence(engine):
    """M7 문장만 막으면 다음 규칙이 "32인치 이하는 FMS 스탠드에 호환" 을 낸다."""

    result = _answer(engine, BATTERY, QUESTION_10942)

    assert "FMS 스탠드" not in result.answer
    assert not result.category.startswith("스탠드호환")


@pytest.mark.parametrize("question", [
    "LS43FM703 모델에 호환 안되나요?",           # 190232: 호환 불가
    "LS43BM702UKXKR\n\n이모델도 호환가능할까요?",  # 318190: 호환되지 않습니다
    "삼성 M7 LS43FM701 구매했습니다. 전력소모가 78W라고 나오는데 호환 안되나요?",
    "S43AM702UK 모델도 호환 가능한가요?",
])
def test_models_off_the_battery_list_get_no_m7_sentence(engine, question):
    result = _answer(engine, BATTERY, question)

    assert M7_SENTENCE not in result.answer
    assert result.category == "배터리호환/목록외모델"


# --- 2. 그대로여야 하는 것 ---------------------------------------------------

@pytest.mark.parametrize("question", [
    "M7 LS32BM701UKXKR 모델이랑 호환 가능한가요?",  # 8932 의 원 질문
    "LS32DM703UK 호환되나요",
    "S32CM700 호환되나요?",
])
def test_models_on_the_battery_list_keep_the_m7_sentence(engine, question):
    result = _answer(engine, BATTERY, question)

    assert result.category == "배터리호환"
    assert M7_SENTENCE in result.answer


@pytest.mark.parametrize("question", ["M7 호환되나요?", "m7 32인치 호환되나요?"])
def test_a_question_naming_no_model_code_keeps_the_m7_sentence(engine, question):
    result = _answer(engine, BATTERY, question)

    assert result.category == "배터리호환"
    assert M7_SENTENCE in result.answer


@pytest.mark.parametrize("question", ["S32FM503 호환되나요", "M5 LS32DM501 호환?"])
def test_the_m5_sentence_is_unchanged(engine, question):
    result = _answer(engine, BATTERY, question)

    assert result.category == "배터리호환"
    assert "M5 전 모델" in result.answer


def test_a_stand_listing_without_battery_is_not_touched(engine):
    """배터리 문의가 아니면 이 경계는 관여하지 않는다."""

    result = _answer(engine, STAND, "LS43FM701 호환되나요")

    assert not result.category.startswith("배터리")


def test_the_battery_list_is_loaded_from_the_catalog():
    catalog = load_answer_config().battery_catalog

    assert "VIPB01W" in catalog
    listed = str(catalog["VIPB01W"]["available_monitor"])
    assert "S32BM701" in listed
    assert "S32FM700" not in listed


# --- 3. 실제 파이프라인: GPT ② 가 받는 Template 후보 ---------------------------

class _SemanticStub:
    name = "semantic_stub"

    def __init__(self, question):
        self.question = question

    def generate_json(self, *, task, prompt, context):
        return {
            "primary_action": "PRODUCT_SPEC",
            "secondary_actions": [],
            "request_type": "QUESTION",
            "objects": [],
            "atomic_questions": [{
                "text": self.question,
                "action": "PRODUCT_SPEC",
                "requested_information": "모니터와의 호환 가능 여부",
                "requested_attribute": "COMPATIBILITY",
            }],
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


class _DraftStub:
    name = "draft_stub"

    def __init__(self):
        self.captured: list[dict] = []

    def generate_json(self, *, task, prompt, context):
        self.captured.append({"task": task, "prompt": prompt})
        return {
            "answer": "확인 후 안내드리겠습니다.",
            "confidence": 0.5,
            "used_facts": [],
            "missing_information": [],
            "requires_review": True,
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
            "can_auto_post": False,
            "reason": "replay",
        }


class _OrderSpy:
    def lookup_for_inquiry(self, *_args, **_kwargs):
        raise AssertionError("주문 조회가 실행되면 안 됩니다")


def _run(tmp_path, monkeypatch, *, name, question):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "TEST",
        "source_question_id": f"battery-{name}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": question,
        "product_id": "10358299339",
        "product_name": BATTERY,
        "option_name": None,
        "raw_json": {},
    }).inquiry_id

    draft_stub = _DraftStub()
    governed = GovernedHybridAnswerService(
        database, provider=draft_stub,
        settings=GptProviderSettings(provider_name="fake"),
    )

    import services.answer_service as answer_module

    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(answer_module, "notify_qna_safely", lambda **_k: False)

    AnswerService(
        database, hybrid_service=governed, order_lookup_service=_OrderSpy(),
        semantic_analyzer=GptSemanticAnalyzerService(_SemanticStub(question)),
    ).generate_for_inquiry(inquiry_id)

    drafts = [
        item for item in draft_stub.captured
        if str(item["task"]).upper() == "DRAFT"
    ]
    assert drafts, "GPT ② 가 호출되지 않았다"
    prompt = json.loads(drafts[-1]["prompt"])
    candidates = prompt["input"].get("template_candidates") or []
    return SimpleNamespace(
        candidates=candidates,
        answers=" ".join(str(item.get("answer") or "") for item in candidates),
    )


def test_gpt2_no_longer_receives_the_m7_template_for_10942(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch, name="10942", question=QUESTION_10942)

    assert M7_SENTENCE not in run.answers
    assert "FMS 스탠드에 호환" not in run.answers
    assert all(
        str(item.get("category") or "") != "배터리호환" for item in run.candidates
    )


def test_gpt2_still_receives_the_m7_template_for_a_listed_model(
    tmp_path, monkeypatch,
):
    run = _run(
        tmp_path, monkeypatch, name="8932",
        question="M7 LS32BM701UKXKR 모델이랑 호환 가능한가요?",
    )

    assert M7_SENTENCE in run.answers
