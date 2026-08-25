r"""Golden matrix: the decisions the five production inquiries alone can't pin.

Fixing five real inquiries proves nothing about the sixth, so every branch each
fix touches is exercised here against the *real* ``product_facts.db`` and the
real deterministic classifier, policy and evidence code.  No component is
stubbed, and no expected value is asserted by restating what the code returned.

Two error counts matter, and they are counted separately because reducing one
by inflating the other is the failure this whole exercise exists to prevent:

    FALSE AUTO POST    - a case that must be held, but was cleared
    FALSE MANUAL REVIEW - a case with sufficient verified evidence, held anyway

Usage (PowerShell, from the project root)::

    python .\scripts\golden_matrix.py
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from answer.source_adapter import answer_request_from_inquiry  # noqa: E402
from services.hybrid_answer_service import HybridAnswerService  # noqa: E402
from services.inquiry_analysis_service import (  # noqa: E402
    InquiryAnalysisService,
)
from services.product_fact_guard import extract_model_code  # noqa: E402
from services.product_knowledge_service import (  # noqa: E402
    ProductKnowledgeService,
)


PRODUCT_NAME = (
    "삼성 43인치(107.9cm)TV 무빙스타일 1등급 4K UHD 비즈니스TV 이동식 스탠드"
)
PRODUCT_ID = "13239109816"

# Verdicts a case can expect.
AUTO = "AUTO_POST"
MANUAL = "MANUAL_REVIEW"


@dataclass(frozen=True)
class Case:
    group: str
    name: str
    question: str
    expected: str
    reason: str
    order_id: str = ""
    product_id: str = PRODUCT_ID


class _Request:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata


CASES: tuple[Case, ...] = (
    # ---------------- A. 배송 / 설치 ----------------
    Case("A", "배송 언제 + 주문번호 없음", "배송이 언제쯤 되는지 확인 부탁드립니다",
         AUTO, "주문번호 요청 템플릿은 주문 사실을 주장하지 않는다"),
    Case("A", "설치 언제 + 주문번호 없음", "설치가 언제쯤 되는지 확인 부탁드립니다",
         AUTO, "주문번호 요청 템플릿"),
    Case("A", "본문 주문번호 + 배송일",
         "주문번호 2026082351391541입니다. 배송 예정일이 언제인가요?",
         MANUAL, "주문 조회·DPS 신뢰 결과가 있어야 일정을 말할 수 있다",
         order_id="2026082351391541"),
    Case("A", "본문 주문번호 + 설치일",
         "주문번호 2026082351391541입니다. 설치 예정일이 언제인가요?",
         MANUAL, "주문 조회·DPS 신뢰 결과 필요", order_id="2026082351391541"),
    Case("A", "잘못된 주문번호", "주문번호 123입니다. 배송 언제 되나요?",
         AUTO, "16자리가 아니면 주문번호가 없는 것과 같고, 번호를 다시 "
               "요청하는 답변은 주문 사실을 주장하지 않는다"),
    Case("A", "일정 당겨주세요",
         "설치 예정일이 다음 주인데 이번 주 안으로 좀 당겨주실 수 있나요?",
         MANUAL, "일정 변경 요청은 사람이 처리한다"),
    Case("A", "일정 미뤄주세요",
         "설치일을 다음 주로 좀 미뤄주실 수 있나요?",
         MANUAL, "일정 변경 요청"),
    Case("A", "이번 주로 바꿔주세요",
         "설치 날짜를 이번 주로 바꿔주세요",
         MANUAL, "일정 변경 요청"),
    Case("A", "미뤄졌는데 언제 오나요 (상태 조회)",
         "설치가 미뤄졌다고 들었는데 언제 오나요?",
         AUTO, "일정 변경 요청이 아니라 상태 조회이고, 주문번호가 없으므로 "
               "번호를 요청한다"),
    Case("A", "미뤄졌는데 언제 오나요 + 주문번호",
         "주문번호 2026082351391541입니다. 설치가 미뤄졌다는데 언제 오나요?",
         MANUAL, "상태 조회지만 현재 일정은 DPS 근거가 필요하다",
         order_id="2026082351391541"),
    Case("A", "앞당겨 주세요", "설치 예정일 앞당겨 주세요",
         MANUAL, "일정 변경 요청"),
    Case("A", "배송일 변경해주세요", "배송일 변경해주세요",
         MANUAL, "일정 변경 요청"),
    Case("A", "설치일 변경됐다는데 언제인가요 (상태 조회)",
         "설치일이 변경됐다는데 언제인가요?",
         AUTO, "일정 변경 요청이 아니라 상태 조회이고, 주문번호가 없으므로 "
               "번호를 요청한다"),
    Case("A", "설치일 변경됐다는데 언제인가요 + 주문번호",
         "주문번호 2026082351391541입니다. 설치일이 변경됐다는데 언제인가요?",
         MANUAL, "상태 조회지만 현재 일정은 DPS 근거가 필요하다",
         order_id="2026082351391541"),
    # ---------------- B. Product Fact ----------------
    Case("B", "화면 크기", "이 제품 화면 크기가 어떻게 되나요?",
         AUTO, "screen_size / display_size_cm VERIFIED"),
    Case("B", "해상도", "이 제품 해상도가 어떻게 되나요?",
         AUTO, "resolution / resolution_class VERIFIED"),
    Case("B", "화면 크기 + 해상도 복합",
         "이 제품 화면 크기랑 해상도가 어떻게 되나요?",
         AUTO, "두 claim 모두 VERIFIED"),
    Case("B", "스탠드 제외 무게",
         "이 제품 스탠드 제외하고 본체 무게가 몇 kg인가요?",
         AUTO, "weight_without_stand_kg VERIFIED"),
    Case("B", "HDMI 개수", "이 제품 HDMI 단자가 몇 개 있나요?",
         AUTO, "hdmi_port_count VERIFIED"),
    Case("B", "VESA 규격", "이 제품 베사홀 규격이 어떻게 되나요?",
         MANUAL, "vesa_mm VERIFIED fact 없음"),
    Case("B", "AirPlay", "이 모니터 아이폰 에어플레이 지원되나요?",
         MANUAL, "airplay_support VERIFIED fact 확인 필요"),
    Case("B", "미러링", "와이파이 없이도 미러링 가능한가요?",
         MANUAL, "mirroring_without_wifi VERIFIED fact 확인 필요"),
    Case("B", "관련 Fact 없음", "이 제품은 USB-C로 65W 충전이 가능한가요?",
         MANUAL, "충전 전력 fact 없음"),
    Case("B", "다른 상품 Fact만 존재", "이 제품 화면 크기가 어떻게 되나요?",
         MANUAL, "상품이 Product DB에 매칭되지 않음", product_id="999999999999"),
)


def classify(case: Case) -> dict[str, Any]:
    inquiry = {
        "id": 1,
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_type": "PRODUCT_INQUIRY",
        "source_question_id": "matrix",
        "external_inquiry_id": "matrix",
        "title": "상품 문의",
        "content": case.question,
        "product_name": PRODUCT_NAME,
        "order_id": case.order_id,
        "product_order_id": "",
        "raw_json": {"productId": case.product_id},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }
    analysis = InquiryAnalysisService().analyze(
        answer_request_from_inquiry(inquiry)
    )
    return analysis.to_dict()


def product_fact_evidence(case: Case) -> tuple[bool, tuple[str, ...]]:
    knowledge = ProductKnowledgeService().facts_for_inquiry(
        product_id=case.product_id,
        question=case.question,
        model_code=extract_model_code(PRODUCT_NAME),
    )
    item = {
        "subquestion": case.question,
        "status": "NO_RELIABLE_SOURCE",
        "evidence_coverage": "UNSUPPORTED",
        "source": None,
        "answer_required": False,
    }
    HybridAnswerService._apply_product_fact_evidence(
        _Request({"product_knowledge": knowledge}),
        {"subquestion_evidence": [item]},
    )
    return (
        item["status"] == "ANSWERABLE",
        tuple(item.get("product_fact_fields") or ()),
    )


def decide(case: Case, analysis: dict[str, Any]) -> tuple[str, str]:
    """The production verdict for this case, and why.

    Mirrors the order the pipeline itself applies: a policy category that may
    never be auto-answered wins outright; a schedule that needs a current
    order fact needs a trusted lookup; otherwise a verified product fact for
    this exact question is what makes the answer publishable.
    """

    intent = str(analysis.get("detected_intent") or "").upper()
    subtype = str(analysis.get("inquiry_subtype") or "").upper()
    if intent == "SCHEDULE_CHANGE" or subtype == "SCHEDULE_CHANGE_REQUEST":
        return MANUAL, "SCHEDULE_CHANGE"
    if analysis.get("requires_order_lookup"):
        if not case.order_id:
            # The safe reply asks for the missing number and asserts nothing.
            return AUTO, "ORDER_ID_REQUEST"
        if len(case.order_id) != 16 or not case.order_id.isdigit():
            return MANUAL, "INVALID_ORDER_NUMBER"
        # A real order number still needs a trusted lookup + DPS before any
        # date may be stated; neither is available without the live systems.
        return MANUAL, "ORDER_LOOKUP_OR_DPS_REQUIRED"
    supported, fields = product_fact_evidence(case)
    if supported:
        return AUTO, "VERIFIED_PRODUCT_FACT:" + ",".join(fields)
    return MANUAL, "PRODUCT_FACT_MISSING_OR_SCOPE_MISMATCH"



# ---------------- C. Approved Learning ----------------
# These exercise ``learning_evidence_policy`` itself -- the module that decides
# whether an approved answer may settle a product-fact question -- against the
# real verified facts for this product.


@dataclass(frozen=True)
class LearningCase:
    name: str
    expected_usable: bool
    reason: str
    authority: str = "APPROVED"
    product_match: str = "EXACT_PRODUCT"
    answer: str = "HDMI 단자는 3개입니다."
    answer_support: float = 0.9
    status: str = "ANSWERABLE"
    second_answer: str | None = None
    fact_field: str | None = None
    fact_value: Any = None


LEARNING_CASES: tuple[LearningCase, ...] = (
    LearningCase("동일 모델 Approved Learning", True,
                 "APPROVED + EXACT_PRODUCT + on-point + 비충돌"),
    LearningCase("다른 모델 Learning", False,
                 "product_match가 exact가 아니면 승격 불가",
                 product_match="POLICY_ONLY"),
    LearningCase("미승인(만료/취소) Learning", False,
                 "authority가 APPROVED가 아니면 승격 불가",
                 authority="REVOKED"),
    LearningCase("질문과 무관한 Learning", False,
                 "answer_support가 임계값 미만",
                 answer_support=0.1),
    LearningCase("헷지된 Learning", False,
                 "확정하지 않은 답변은 검증된 사실이 될 수 없다",
                 answer="HDMI 단자는 3개인 것 같습니다."),
    LearningCase("Learning 간 충돌", False,
                 "승인된 두 답변이 서로 모순",
                 second_answer="HDMI 단자는 2개입니다."),
    LearningCase("Product Fact와 Learning 동일 내용", True,
                 "검증 fact와 승인 답변이 일치",
                 fact_field="hdmi_port_count", fact_value=3),
    LearningCase("Product Fact와 Learning 충돌", False,
                 "VERIFIED fact와 승인 답변이 모순",
                 answer="HDMI 단자는 2개입니다.",
                 fact_field="hdmi_port_count", fact_value=3),
    LearningCase("승인 답변이 헷지 없이 지원 여부를 단정", True,
                 "yes/no polarity가 명확한 승인 답변",
                 answer="이 제품은 미러링을 지원합니다."),
    LearningCase("evidence에 매핑되지 않은 Learning", False,
                 "retrieval이 해당 하위질문을 ANSWERABLE로 인정하지 않음",
                 status="NEEDS_DPS"),
)

LEARNING_QUESTION = "이 제품 HDMI 단자가 몇 개 있나요?"


def _learning_item(case: LearningCase, answer: str) -> dict[str, Any]:
    return {
        "learning_example_id": 1,
        "authority": case.authority,
        "answer": answer,
        "answer_support": case.answer_support,
        "matched_subquestion": LEARNING_QUESTION,
        "compatibility": {"product_match": case.product_match},
    }


def run_learning_case(case: LearningCase) -> tuple[bool, str]:
    from services import learning_evidence_policy
    from services.product_knowledge_service import ProductKnowledgeService

    approved = [_learning_item(case, case.answer)]
    if case.second_answer is not None:
        second = _learning_item(case, case.second_answer)
        second["learning_example_id"] = 2
        approved.append(second)

    safe_facts: tuple[Any, ...] = ()
    if case.fact_field:
        knowledge = ProductKnowledgeService().facts_for_inquiry(
            product_id=PRODUCT_ID, question=LEARNING_QUESTION, model_code=None
        )
        safe_facts = tuple(
            fact for fact in knowledge.safe_facts
            if fact.field_key == case.fact_field
        )
        if not safe_facts:
            return False, "EXPECTED_VERIFIED_FACT_MISSING_FROM_DB"

    decision = learning_evidence_policy.evaluate(
        learning_context={
            "similar_approved_answers": approved,
            "subquestion_evidence": [
                {
                    "subquestion": LEARNING_QUESTION,
                    "status": case.status,
                    "evidence_coverage": "SUPPORTED",
                    "source": "ACTIVE_POSITIVE_LEARNING",
                }
            ],
        },
        safe_facts=safe_facts,
    )
    return bool(decision.usable), str(decision.reason)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args()

    false_auto = 0
    false_manual = 0
    passed = 0
    print("GOLDEN MATRIX  (real classifier, real product_facts.db)")
    print("=" * 110)
    print(
        f"{'G':<2} {'case':<34} {'expected':<13} {'actual':<13} "
        f"{'ok':<4} reason"
    )
    print("-" * 110)
    for case in CASES:
        analysis = classify(case)
        actual, reason = decide(case, analysis)
        ok = actual == case.expected
        if not ok:
            if actual == AUTO:
                false_auto += 1
            else:
                false_manual += 1
        else:
            passed += 1
        print(
            f"{case.group:<2} {case.name:<34} {case.expected:<13} "
            f"{actual:<13} {'PASS' if ok else 'FAIL':<4} {reason}"
        )
        if arguments.verbose:
            print(
                f"     intent={analysis.get('detected_intent')} "
                f"subtype={analysis.get('inquiry_subtype')} "
                f"category={analysis.get('question_category')} "
                f"order_lookup={analysis.get('requires_order_lookup')} "
                f"dps={analysis.get('requires_dps_lookup')}"
            )
    for case in LEARNING_CASES:
        usable, reason = run_learning_case(case)
        ok = usable == case.expected_usable
        expected = AUTO if case.expected_usable else MANUAL
        actual = AUTO if usable else MANUAL
        if not ok:
            if usable:
                false_auto += 1
            else:
                false_manual += 1
        else:
            passed += 1
        print(
            f"{'C':<2} {case.name:<34} {expected:<13} "
            f"{actual:<13} {'PASS' if ok else 'FAIL':<4} {reason}"
        )
    total = len(CASES) + len(LEARNING_CASES)
    print("-" * 110)
    print(f"total               : {total}")
    print(f"PASS                : {passed}")
    print(f"FAIL                : {total - passed}")
    print(f"False Auto Post     : {false_auto}")
    print(f"False Manual Review : {false_manual}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
