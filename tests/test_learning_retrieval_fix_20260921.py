"""Regressions for the 2026-09-21 Learning retrieval audit fixes.

Source: .verification/learning_final_audit_20260921/REPORT.md (RC1, RC4, RC5)
and the official-number omission traced to the draft prompt.

A. Runtime quality: a greeting line break and a product-type relative clause
   ("...설치 진행해주시는 상품입니다") were read as one customer's order fact,
   hard-rejecting reusable Human Verified installation Learning (L41224/41225).
B. Prompt duplicate: a Historical case carrying exactly the same reply for the
   same product as a delivered Learning reached the prompt twice.
D. Model identity: the operator-confirmed Coupang model was not handed to the
   Learning search, so the display option code scored the same product as a
   MODEL_MISMATCH (-0.20).
E. Official numbers: the draft prompt forbade every phone number, so the model
   dropped 1588-3366 / 02-706-2678 even when the evidence carried them.
"""
from __future__ import annotations

import itertools
import json

import pytest

from answer.answer_format import format_final_answer
from answer.facts import build_answer_facts
from answer.hybrid_models import Emotion, IntentResult
from answer.models import AnswerRequest, AnswerResult, AnswerStatus
from answer.prompt_builder import PromptBuilder
from answer.text_utils import OFFICIAL_CONTACT_NUMBERS, contains_personal_phone
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.auto_post_validation_service import AutoPostTechnicalValidator
from services.historical_learning_quality_service import (
    HistoricalLearningQualityService,
    is_data_unsafe,
)
from services.learning_context_service import LearningContextService
from services.learning_privacy_service import LearningPrivacyService
from services.prompt_privacy_service import PromptPrivacyService
from services.semantic_analysis import parse

_key = itertools.count()
PRODUCT = "삼성 107.9cm(43인치) 4K UHD LH43B 스마트 비즈니스TV 1등급 스탠드형"
SAMSUNG = "1588-3366"
OJE = "02-706-2678"
PERSONAL = "010-1234-5678"


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "learning-fix.db")
    value.initialize()
    return value


def _quality(answer: str, question: str = "설치는 자가 설치인가요?"):
    return HistoricalLearningQualityService().assess(question=question, answer=answer)


# ======================================================================
# A. ORDER_SPECIFIC false reject
# ======================================================================

# Server rows 41224 / 41225, verbatim.
L41224 = (
    "안녕하세요 고객님\n해당 스탠드형 제품의 경우 삼성전자 기사님께서 설치해주시는 상품이며,\n"
    "추가상품 제품만 자가설치로 생각해주시면 됩니다.\n"
    "본품은 삼성기사님께서 배송 설치 진행해주시는 상품입니다."
)
L41225 = (
    "안녕하세요 고객님\n상품 대표이미지와 동일하게 탁상형 다리까지 설치해주시는 상품입니다.\n"
    "동축케이블 또는 별도의 통신사향 셋톱박스 등 이용 시 티비 시청 가능합니다.\n감사합니다."
)


@pytest.mark.parametrize(
    "question,answer",
    [
        ("설치는 자가 설치 인거지요? 일반 TV거실장에 올려 놓을건데요", L41224),
        ("기본 스탠드가 포함인 상품인거죠? 기사님이 스탠드까지 세워서 설치해주시나요? "
         "통신사 기사님 불러서 유선 연결하면 티비로 볼 수 있나요?", L41225),
    ],
)
def test_A_reusable_installation_learning_is_not_order_specific(question, answer):
    result = _quality(answer, question)
    assert result.status != "ORDER_SPECIFIC", result.reasons
    assert not is_data_unsafe(result)


@pytest.mark.parametrize(
    "answer",
    [
        "고객님 주문은 9월 25일 설치 예정입니다.",
        "안녕하세요 고객님\n오늘 출고됐습니다.",
        "금일 출고 진행되었습니다.",
        "고객님 상품은 금일 출고 진행되었습니다.",
        "본품 발송 진행하였습니다.",
        "고객님 상품은 내일 도착 예정입니다.",
        "해당 건은 9/25 설치 예정입니다.",
        "고객님의 주문번호는 2026****1251 입니다.",
        "스탠드 누락된 것으로 확인되어 재발송 진행하겠습니다.",
    ],
)
def test_A_real_order_and_date_facts_stay_blocked(answer):
    result = _quality(answer, "제 주문 언제 설치되나요?")
    assert is_data_unsafe(result), (result.status, result.reasons)


def test_A_same_line_customer_reference_is_still_theirs():
    """Only the greeting line break is exempt, not "고객님 상품" itself."""

    assert _quality("고객님 상품은 9월 3일 도착 예정입니다.").status in {
        "ORDER_SPECIFIC", "TEMPORARY_OR_EXPIRED",
    }
    assert _quality("안녕하세요 고객님 상품은 금일 출고 진행되었습니다.").status == "ORDER_SPECIFIC"


# ======================================================================
# B. Prompt duplicate evidence / D. confirmed model identity
# ======================================================================


def _learning(database, *, question, answer, product=PRODUCT, model_code=None, store="OJE_PLUS"):
    metadata = {"learning_signal_type": "POSITIVE", "human_verified": True,
                "answer_provenance": "NAVER_POSTED"}
    if store.startswith("COUPANG_"):
        metadata["market_applicability"] = "COUPANG_ONLY"
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                model_code, final_answer, seller_answer, posted, rating,
                edit_ratio, quality_score, style_only, version, metadata_json,
                active, usage_count, created_at, updated_at, validity_type,
                validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (f"lf-{next(_key)}", "SELLER_ANSWER", question, question, store,
             "PRODUCT_INQUIRY", product, model_code, answer, answer, 1, 5, 0.0,
             1.0, 0, 1, json.dumps(metadata, ensure_ascii=False), 1, 0,
             "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z", "PERMANENT", 1),
        )
        return int(cursor.lastrowid)


def _historical(database, *, question, answer, product=PRODUCT):
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO historical_cases (
                source, store_code, external_inquiry_id, inquiry_type, question,
                question_normalized, seller_answer, product_name,
                source_answered, quality_score, confidence, active, case_key,
                fingerprint, created_at, updated_at
            ) VALUES ('NAVER','OJE_PLUS',?,'PRODUCT_INQUIRY',?,?,?,?,1,1.0,1.0,1,
                      ?,?, '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')
            """,
            (f"ext-{next(_key)}", question, question, answer, product,
             f"hc-{next(_key)}", f"fp-{next(_key)}"),
        )
        return int(cursor.lastrowid)


def _semantic(action, question):
    return parse({
        "primary_action": action, "secondary_actions": [], "request_type": "QUESTION",
        "objects": [], "atomic_questions": [{"text": question, "action": action}],
        "deadline": None, "constraints": [], "negation": False, "conditional": False,
        "requires_order_context": False, "requires_delivery_schedule": False,
        "purchase_state": "UNKNOWN", "asks_delivery_schedule": False, "confidence": 0.95,
    })


def _context(database, question, *, action="PACKAGE_CONTENTS", product=PRODUCT,
             option=None, metadata=None, store="OJE_PLUS", source_type="NAVER"):
    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": store, "source_type": source_type,
        "source_question_id": f"lf-i-{next(_key)}", "inquiry_type": "PRODUCT_INQUIRY",
        "title": "문의", "content": question, "product_name": product,
        "option_name": option, "product_id": "p1", "raw_json": {},
    }).inquiry_id
    facts = build_answer_facts(
        AnswerRequest(inquiry_type="PRODUCT_INQUIRY", question=question,
                      product_name=product, option_name=option,
                      store_code=store, metadata=dict(metadata or {})),
        AnswerResult(status=AnswerStatus.NOT_SUPPORTED, category="기타", reason="t",
                     answer="", provider="test", auto_answerable=False, needs_review=True),
    )
    facts.inquiry["inquiry_id"] = inquiry_id
    service = LearningContextService(database, hard_conflicts_only=True)
    seen: list[dict] = []
    original = service.search.search

    def record(q, **kw):
        seen.append({"question": q, "model_code": kw.get("model_code")})
        return original(q, **kw)

    service.search.search = record
    context = service.build(
        facts,
        IntentResult("GENERAL", (question,), Emotion.NORMAL, "NORMAL", 0.9, False, "t"),
        semantic_analysis=_semantic(action, question),
    )
    return context, seen


BRACKET_Q = "벽걸이 브라켓은 따로 구매해야 하나요?"
BRACKET_A = "벽걸이 브라켓은 벽걸이 설치 추가상품으로 별도 구매해 주셔야 합니다."


def test_B_same_reply_same_product_reaches_the_prompt_once(database):
    learning_id = _learning(database, question=BRACKET_Q, answer=BRACKET_A)
    case_id = _historical(database, question=BRACKET_Q, answer="  " + BRACKET_A.replace(" ", "  "))

    context, _ = _context(database, "벽걸이 브라켓 별도로 사야 하나요?")

    approved = [x["learning_example_id"] for x in context["similar_approved_answers"]]
    historical = [x["historical_case_id"] for x in context["historical_cases"]]
    assert learning_id in approved
    assert case_id not in historical
    # The omission is recorded, not silent: provenance stays traceable.
    duplicates = context["historical_retrieval"]["duplicate_of_delivered_evidence"]
    assert {"historical_case_id": case_id, "same_as": {"learning_example_id": learning_id}} in duplicates
    # The rows themselves are untouched.
    with database.connection() as conn:
        assert conn.execute("select count(*) from historical_cases where id=?", (case_id,)).fetchone()[0] == 1


def test_B_same_sentence_for_another_product_is_kept(database):
    """Whether another model's identical sentence applies is the reader's call."""

    _learning(database, question=BRACKET_Q, answer=BRACKET_A)
    other = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
    case_id = _historical(database, question=BRACKET_Q, answer=BRACKET_A, product=other)

    context, _ = _context(database, "벽걸이 브라켓 별도로 사야 하나요?")

    assert case_id in [x["historical_case_id"] for x in context["historical_cases"]]


def test_B_different_replies_are_never_merged(database):
    _learning(database, question=BRACKET_Q, answer=BRACKET_A)
    case_id = _historical(
        database, question=BRACKET_Q,
        answer="벽걸이 브라켓은 벽걸이 옵션 구매 시 기본 제공됩니다.",
    )

    context, _ = _context(database, "벽걸이 브라켓 별도로 사야 하나요?")

    assert case_id in [x["historical_case_id"] for x in context["historical_cases"]]


def test_B_redacted_historical_reply_never_reaches_the_prompt(database):
    case_id = _historical(
        database, question=BRACKET_Q,
        answer="벽걸이 브라켓 구매 문의는 <masked-phone>로 연락 부탁드립니다.",
    )

    context, _ = _context(database, "벽걸이 브라켓 별도로 사야 하나요?")

    assert case_id not in [x["historical_case_id"] for x in context["historical_cases"]]
    assert context["learning_retrieval"]["rejection_counts"][
        "HISTORICAL_REDACTION_TOKEN_CONTAMINATED"
    ] >= 1


LEG_Q = "탁상형 받침 다리는 분리할 수 있나요?"


def test_D_confirmed_model_reaches_the_learning_search(database):
    _learning(database, question=LEG_Q, answer="네 다리 분리 가능합니다.",
              product=None, model_code="LH43BEDHLGFXKR", store="COUPANG_OJE_NS")
    context, seen = _context(
        database, "탁상형 받침 다리 떼어낼 수 있나요?", action="PRODUCT_SPEC",
        product="사이니지 BED / 모음전 / 메인코드 / O",
        option="벽걸이형 방문설치 LH43BEDH 43인치",
        metadata={"market": "COUPANG", "model_code": "LH43BEDHLGFXKR",
                  "canonical_model": "LH43BEDHLGFXKR",
                  "model_identity_source": "COUPANG_CONFIRMED_MAPPING"},
        store="COUPANG_OJE_NS", source_type="COUPANG_ONLINE_INQUIRY",
    )

    assert seen and all(call["model_code"] == "LH43BEDHLGFXKR" for call in seen)
    item = context["similar_approved_answers"][0]
    assert item["compatibility"].get("reject_reason") != "MODEL_MISMATCH"
    assert item["evidence_origin"]["identity"] == "SAME_PRODUCT"


def test_D_unconfirmed_model_is_never_invented(database):
    """A title/option-read model is not a fact; nothing is passed for it."""

    _, seen = _context(database, "벽걸이 브라켓 별도로 사야 하나요?",
                       metadata={"model_code": "LH43BEDHLGFXKR"})
    assert seen and all(call["model_code"] != "LH43BEDHLGFXKR" for call in seen)


def test_D_confirmed_model_does_not_merge_different_models(database):
    _learning(database, question=LEG_Q, answer="네 다리 분리 가능합니다.",
              product=None, model_code="LH50BEDHLGFXKR", store="COUPANG_OJE_NS")
    context, _ = _context(
        database, "탁상형 받침 다리 떼어낼 수 있나요?", action="PRODUCT_SPEC",
        product="사이니지 BEF / 모음전", option="스탠드형 방문설치 LH50BE-H 50인치",
        metadata={"market": "COUPANG", "model_code": "LH50BEHHLGFXKR",
                  "canonical_model": "LH50BEHHLGFXKR",
                  "model_identity_source": "COUPANG_CONFIRMED_MAPPING"},
        store="COUPANG_OJE_NS", source_type="COUPANG_ONLINE_INQUIRY",
    )

    for item in context["similar_approved_answers"]:
        assert item["evidence_origin"]["identity"] != "SAME_PRODUCT"


# ======================================================================
# E. Official public numbers
# ======================================================================


def test_E_prompt_rule_allows_only_the_listed_official_numbers():
    rule = PromptBuilder.PROHIBITIONS
    for number in OFFICIAL_CONTACT_NUMBERS:
        assert number in rule
    assert SAMSUNG in rule and OJE in rule
    # Personal numbers stay forbidden, and no number may be invented.
    assert "고객·주문자·수령자의 전화번호" in rule
    assert "근거에 없는 번호를 만들거나" in rule
    assert "공식 공개 업무번호" in rule


def _draft_prompt(evidence_answer: str, question: str) -> str:
    from services.draft_generation_service import DraftGenerationService

    class Capture:
        def generate_json(self, **kw):
            self.kw = kw
            raise RuntimeError("captured")

    facts = build_answer_facts(
        AnswerRequest(inquiry_type="PRODUCT_INQUIRY", question=question, product_name=PRODUCT),
        AnswerResult(status=AnswerStatus.NOT_SUPPORTED, category="기타", reason="t",
                     answer="", provider="test", auto_answerable=False, needs_review=True),
    )
    provider = Capture()
    context = {"similar_approved_answers": [{
        "learning_example_id": 1, "question": "A/S 어디로 접수하나요?",
        "answer": evidence_answer, "relevance": 0.9, "answer_support": 0.5,
    }]}
    with pytest.raises(RuntimeError, match="captured"):
        DraftGenerationService(provider).generate(
            facts, IntentResult("GENERAL", (question,), Emotion.NORMAL, "NORMAL", 0.9, False, "t"),
            learning_context=context, gpt_judged_evidence=True,
        )
    return provider.kw["prompt"]


@pytest.mark.parametrize(
    "evidence,number",
    [
        (f"삼성전자 서비스센터 {SAMSUNG}로 접수해 주세요.", SAMSUNG),        # CASE A
        (f"판매처 고객센터 {OJE}로 연락해 주세요.", OJE),                      # CASE B
    ],
)
def test_E_official_number_in_evidence_reaches_prompt_and_passes(evidence, number):
    prompt = _draft_prompt(evidence, "A/S는 어디로 접수하나요?")
    evidence_part = prompt.split("similar_approved_answers", 1)[1]
    assert number in evidence_part
    assert "<masked-phone>" not in prompt

    final = format_final_answer(f"제품 점검은 {evidence}")
    assert number in final
    assert "<masked-phone>" not in final
    assert contains_personal_phone(final) is False
    validator = AutoPostTechnicalValidator()
    assert validator.validate_answer(final).passed
    # Naver post payload (product inquiry) -- fake payload, no network.
    assert validator.validate_payload(
        final_answer=final, payload={"commentContent": final}, source_type="PRODUCT_INQUIRY",
    ).passed
    # Coupang renders with its own wrapper and posts through validate_answer.
    coupang = format_final_answer(f"제품 점검은 {evidence}", market="COUPANG")
    assert number in coupang
    assert validator.validate_answer(coupang).passed


def test_E_case_C_mixed_numbers():
    text = f"삼성 {SAMSUNG}, 판매처 {OJE}, 제 번호 {PERSONAL}"
    for masked in (
        LearningPrivacyService().mask(text),
        str(PromptPrivacyService().sanitize({"q": text}).sanitized_payload),
    ):
        assert SAMSUNG in masked and OJE in masked
        assert PERSONAL not in masked and "<masked-phone>" in masked
    prompt = _draft_prompt(f"삼성전자 서비스센터 {SAMSUNG}, 판매처 {OJE}", f"제 번호 {PERSONAL}로 연락 주세요")
    assert PERSONAL not in prompt
    assert AutoPostTechnicalValidator().validate_answer(f"{SAMSUNG} / {PERSONAL}").passed is False


@pytest.mark.parametrize("number", ["1588-1234", "02-706-2679", "02-706-26780"])
def test_E_case_D_look_alike_numbers_are_not_official(number):
    from answer.text_utils import is_official_contact_number

    assert is_official_contact_number(number) is False
    text = f"연락처 {number}로 문의해 주세요."
    # The official exemption does not reach a neighbour: phone-shaped look-alikes
    # are still masked and still block posting.
    assert "<masked-phone>" in LearningPrivacyService().mask("연락처 02-706-2679로 문의")
    assert AutoPostTechnicalValidator().validate_answer("연락처 02-706-2679로 문의").passed is False
    # Whatever the existing PII rule says about the others, they are never kept
    # *as the official number*.
    for masked in (LearningPrivacyService().mask(text),):
        assert SAMSUNG not in masked and OJE not in masked.replace("02-706-26780", "")
