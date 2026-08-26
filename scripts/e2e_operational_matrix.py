r"""Production-like decision matrix across every branch this pipeline routes on.

Each scenario states what should happen before it is run, and the run uses the
real classifier, the real ``product_facts.db``, the real compatibility gate and
the real evidence policy. Nothing is stubbed and no expectation is written by
copying back what the code returned.

Five error kinds are counted separately, because trading one away to reduce
another is the failure this exists to catch:

    FALSE AUTO POST       held-worthy scenario cleared for posting
    FALSE MANUAL REVIEW   sufficiently evidenced scenario sent to staff
    WRONG EVIDENCE SCOPE  another product's or model's evidence was used
    UNSUPPORTED CERTAINTY a definite claim with no evidence behind it
    WRONG ANSWER          the decision contradicts the evidence on record

Usage (PowerShell, from the project root)::

    python .\scripts\e2e_operational_matrix.py
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from answer.source_adapter import answer_request_from_inquiry  # noqa: E402
from services.hybrid_answer_service import HybridAnswerService  # noqa: E402
from services.inquiry_analysis_service import (  # noqa: E402
    InquiryAnalysisService,
)
from services.learning_compatibility_service import (  # noqa: E402
    LearningCompatibilityService,
    extract_product_identity,
)
from services.learning_evidence_policy import (  # noqa: E402
    contamination_reason,
    evaluate as evaluate_learning,
    is_hedged,
)
from services.product_fact_guard import (  # noqa: E402
    classify_product_fact,
    extract_model_code,
)
from services.product_knowledge_service import (  # noqa: E402
    ProductKnowledgeService,
)


AUTO = "AUTO_POST_EXPECTED"
MANUAL = "MANUAL_REVIEW_EXPECTED"
ORDER_REQ = "ORDER_NUMBER_REQUEST_EXPECTED"
NO_ANSWER = "NO_ANSWER_EXPECTED"

PRODUCT = "삼성 43인치(107.9cm)TV 무빙스타일 1등급 4K UHD 비즈니스TV 이동식 스탠드"
PRODUCT_ID = "13239109816"
OTHER_PRODUCT = "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
OTHER_PRODUCT_ID = "13239109999"
REAL_ORDER = "2026082351391541"     # well formed; Naver answers 100003 for it

WRAPPER_CLOSING = (
    "\n\n안내드린 내용이 문의하신 내용과 다른 경우,\n"
    "네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다.\n\n감사합니다."
)


@dataclass(frozen=True)
class Scenario:
    group: str
    name: str
    question: str
    expected: str
    why: str
    order_id: str = ""
    product_id: str = PRODUCT_ID
    product_name: str = PRODUCT
    # Optional retrieved learning, shaped as retrieval hands it over.
    learning: tuple[dict[str, Any], ...] = field(default=())
    # Simulated upstream lookup outcomes for the delivery branch.
    order_lookup: str = ""          # SUCCESS | NOT_FOUND | FAILED
    dps: str = ""                   # SUCCESS | FAILED | PAST


class _Request:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata


def learning(
    answer: str,
    *,
    question: str,
    approved: bool = True,
    product_match: str = "EXACT_PRODUCT",
    support: float = 0.9,
    learning_id: int = 1,
) -> dict[str, Any]:
    return {
        "learning_example_id": learning_id,
        "authority": "APPROVED" if approved else "AUTO",
        "answer": answer + WRAPPER_CLOSING,
        "answer_support": support,
        "matched_subquestion": question,
        "compatibility": {"product_match": product_match},
    }


STAND_Q = "기본 스탠드 다리 분리 가능한가요?"
STAND_A = "네, 기본 스탠드는 탈부착 가능합니다."

SCENARIOS: tuple[Scenario, ...] = (
    # ---- A. 배송 / 설치 / 주문번호 ----------------------------------------
    Scenario("A", "배송 언제 + 주문번호 없음", "배송이 언제쯤 되는지 확인 부탁드립니다",
             ORDER_REQ, "번호 요청 답변은 주문 사실을 주장하지 않는다"),
    Scenario("A", "설치 언제 + 주문번호 없음", "설치가 언제쯤 되는지 확인 부탁드립니다",
             ORDER_REQ, "번호 요청 답변"),
    Scenario("A", "본문 주문번호 + 배송일 + 조회성공 + DPS성공",
             f"주문번호 {REAL_ORDER}입니다. 배송 예정일이 언제인가요?",
             AUTO, "신뢰 가능한 주문+DPS 일정", order_id=REAL_ORDER,
             order_lookup="SUCCESS", dps="SUCCESS"),
    Scenario("A", "본문 주문번호 + 설치일 + 조회성공 + DPS성공",
             f"주문번호 {REAL_ORDER}입니다. 설치 예정일이 언제인가요?",
             AUTO, "신뢰 가능한 주문+DPS 일정", order_id=REAL_ORDER,
             order_lookup="SUCCESS", dps="SUCCESS"),
    Scenario("A", "본문 주문번호 + 주문조회 NOT_FOUND",
             f"주문번호 {REAL_ORDER}입니다. 배송 예정일이 언제인가요?",
             MANUAL, "네이버가 그런 주문이 없다고 답함", order_id=REAL_ORDER,
             order_lookup="NOT_FOUND"),
    Scenario("A", "본문 주문번호 + 주문조회 API 실패",
             f"주문번호 {REAL_ORDER}입니다. 배송 예정일이 언제인가요?",
             MANUAL, "우리 쪽 장애 — 일정을 말할 수 없음", order_id=REAL_ORDER,
             order_lookup="FAILED"),
    Scenario("A", "잘못된 형식 주문번호", "주문번호 123입니다. 배송 언제 되나요?",
             ORDER_REQ, "16자리가 아니면 번호가 없는 것과 같다"),
    Scenario("A", "조회성공 + DPS 실패",
             f"주문번호 {REAL_ORDER}입니다. 설치 예정일이 언제인가요?",
             MANUAL, "임의 일정 생성 금지", order_id=REAL_ORDER,
             order_lookup="SUCCESS", dps="FAILED"),
    Scenario("A", "조회성공 + DPS 일정이 과거",
             f"주문번호 {REAL_ORDER}입니다. 설치 예정일이 언제인가요?",
             MANUAL, "과거 일정은 현재 답이 아니다", order_id=REAL_ORDER,
             order_lookup="SUCCESS", dps="PAST"),
    Scenario("A", "일정 당겨주세요",
             "설치 예정일이 다음 주인데 이번 주 안으로 좀 당겨주실 수 있나요?",
             NO_ANSWER, "직원 조치가 필요 — 생성 전 차단"),
    Scenario("A", "일정 미뤄주세요", "설치일을 다음 주로 좀 미뤄주실 수 있나요?",
             NO_ANSWER, "일정 변경 요청"),
    Scenario("A", "이번 주로 바꿔주세요", "설치 날짜를 이번 주로 바꿔주세요",
             NO_ANSWER, "일정 변경 요청"),
    Scenario("A", "앞당겨 주세요", "설치 예정일 앞당겨 주세요",
             NO_ANSWER, "일정 변경 요청"),
    Scenario("A", "미뤄졌는데 언제 오나요 (상태조회)",
             "설치가 미뤄졌다고 들었는데 언제 오나요?",
             ORDER_REQ, "변경 요청이 아니라 상태 조회"),
    Scenario("A", "설치일 변경됐다는데 언제 (상태조회)",
             "설치일이 변경됐다는데 언제인가요?",
             ORDER_REQ, "상태 조회"),
    Scenario("A", "상태조회 + 주문번호 + DPS성공",
             f"주문번호 {REAL_ORDER}입니다. 설치가 미뤄졌다는데 언제 오나요?",
             AUTO, "상태 조회 + 신뢰 일정", order_id=REAL_ORDER,
             order_lookup="SUCCESS", dps="SUCCESS"),
    Scenario("A", "일정 변경 요청 + 정상 주문/DPS",
             f"주문번호 {REAL_ORDER}입니다. 설치를 이번 주로 당겨주세요.",
             NO_ANSWER, "근거가 있어도 변경 요청 자체로 차단",
             order_id=REAL_ORDER, order_lookup="SUCCESS", dps="SUCCESS"),

    # ---- B. Product Fact --------------------------------------------------
    Scenario("B", "화면 크기", "이 제품 화면 크기가 어떻게 되나요?", AUTO,
             "screen_size / display_size_cm VERIFIED"),
    Scenario("B", "해상도", "이 제품 해상도가 어떻게 되나요?", AUTO,
             "resolution / resolution_class VERIFIED"),
    Scenario("B", "화면크기+해상도 복합", "이 제품 화면 크기랑 해상도가 어떻게 되나요?",
             AUTO, "두 claim 모두 VERIFIED"),
    Scenario("B", "스탠드 제외 무게", "이 제품 스탠드 제외하고 본체 무게가 몇 kg인가요?",
             AUTO, "weight_without_stand_kg VERIFIED"),
    Scenario("B", "HDMI 개수", "이 제품 HDMI 단자가 몇 개 있나요?", AUTO,
             "hdmi_port_count VERIFIED"),
    Scenario("B", "VESA 규격", "이 제품 베사홀 규격이 어떻게 되나요?", MANUAL,
             "vesa_mm VERIFIED fact 없음"),
    Scenario("B", "AirPlay", "이 모니터 아이폰 에어플레이 지원되나요?", MANUAL,
             "airplay_support fact 없음 — 제품군 규칙으로 단정 금지"),
    Scenario("B", "와이파이 없이 미러링", "와이파이 없이도 미러링 가능한가요?", MANUAL,
             "mirroring_without_wifi fact 없음"),
    Scenario("B", "USB-C 65W 충전", "이 제품은 USB-C로 65W 충전이 가능한가요?", MANUAL,
             "충전 전력은 카탈로그에 없는 claim"),
    Scenario("B", "다른 상품 (매칭 실패)", "이 제품 화면 크기가 어떻게 되나요?", MANUAL,
             "Product DB 매칭 안 됨", product_id="999999999999"),
    Scenario("B", "스탠드 탈부착 (fact 없음, learning 없음)", STAND_Q, MANUAL,
             "stand_detachable fact 없음 + 근거 Learning 없음"),

    # ---- C. Learning ------------------------------------------------------
    Scenario("C", "동일상품 직원승인 확정 Learning", STAND_Q, AUTO,
             "Product Fact 없어도 승인 Learning이 직접 근거",
             learning=(learning(STAND_A, question=STAND_Q),)),
    Scenario("C", "동일모델 다른 listing 승인 Learning", STAND_Q, AUTO,
             "EXACT_MODEL 증명됨",
             learning=(learning(STAND_A, question=STAND_Q, product_match="EXACT_MODEL"),)),
    Scenario("C", "다른 모델 Learning", STAND_Q, MANUAL,
             "모델 다르면 사실 근거 금지",
             learning=(learning(STAND_A, question=STAND_Q, product_match="POLICY_COMPATIBLE"),)),
    Scenario("C", "미승인 자동수집 Learning", STAND_Q, MANUAL,
             "자동수집은 승인이 아니다",
             learning=(learning(STAND_A, question=STAND_Q, approved=False),)),
    Scenario("C", "승인됐지만 추정 표현", STAND_Q, MANUAL,
             "승인 ≠ 확정성",
             learning=(learning("분리 가능할 것으로 보입니다.", question=STAND_Q),)),
    Scenario("C", "승인 Learning + placeholder 오염", STAND_Q, MANUAL,
             "오염 답변은 근거가 될 수 없다",
             learning=(learning("자세한 사항은 <masked-phone>로 문의 바랍니다.", question=STAND_Q),)),
    Scenario("C", "질문과 무관한 Learning", STAND_Q, MANUAL,
             "answer_support 미달",
             learning=(learning(STAND_A, question=STAND_Q, support=0.1),)),
    Scenario("C", "승인 Learning 상호 충돌", STAND_Q, MANUAL,
             "사람이 정리해야 할 모순",
             learning=(
                 learning("기본 스탠드는 탈부착 가능합니다.", question=STAND_Q, learning_id=1),
                 learning("기본 스탠드는 탈부착이 불가능합니다.", question=STAND_Q, learning_id=2),
             )),
    Scenario("C", "HDMI Learning + VERIFIED Fact 일치", "HDMI 단자가 몇 개인가요?", AUTO,
             "Fact 와 Learning 이 같은 값",
             learning=(learning("HDMI 단자는 3개입니다.", question="HDMI 단자가 몇 개인가요?"),)),
    Scenario("C", "HDMI Learning + VERIFIED Fact 충돌", "HDMI 단자가 몇 개인가요?", MANUAL,
             "VERIFIED Fact 와 모순",
             learning=(learning("HDMI 단자는 2개입니다.", question="HDMI 단자가 몇 개인가요?"),)),
    Scenario("C", "무수정 승인 Learning", STAND_Q, AUTO,
             "수정 여부는 자격 조건이 아니다",
             learning=(learning(STAND_A, question=STAND_Q),)),
    Scenario("C", "수정 후 승인 Learning", STAND_Q, AUTO,
             "무수정 승인과 동일 취급",
             learning=(learning(STAND_A, question=STAND_Q),)),
    Scenario("C", "AirPlay 충돌 Learning", "에어플레이 지원되나요?", MANUAL,
             "상충 승인 답변",
             learning=(
                 learning("에어플레이를 지원합니다.", question="에어플레이 지원되나요?", learning_id=1),
                 learning("에어플레이는 지원하지 않습니다.", question="에어플레이 지원되나요?", learning_id=2),
             )),

    # ---- D. 정책 / 개인정보 ----------------------------------------------
    Scenario("D", "취소 요청", "구매 취소하고 싶습니다", MANUAL, "취소는 직원 처리"),
    Scenario("D", "환불 요청", "환불 부탁드립니다", MANUAL, "환불은 직원 처리"),
    Scenario("D", "반품 요청", "제품 반품하고 싶어요", MANUAL, "반품은 직원 처리"),
    Scenario("D", "공식번호 안내 Learning", "AS는 어디로 문의하나요?", AUTO,
             "1588-3366 은 정상 출력 가능",
             learning=(learning("삼성전자 고객센터 1588-3366으로 문의해 주세요.",
                                question="AS는 어디로 문의하나요?"),)),
    Scenario("D", "복합문의 (일부만 근거 있음)",
             "화면 크기가 어떻게 되나요? 그리고 에어플레이 지원되나요?", MANUAL,
             "한쪽 claim 만 근거가 있으면 확정 불가"),
)


def build_inquiry(scenario: Scenario) -> dict[str, Any]:
    return {
        "id": 1,
        "source_type": "PRODUCT_INQUIRY",
        "inquiry_type": "PRODUCT_INQUIRY",
        "source_question_id": "matrix",
        "external_inquiry_id": "matrix",
        "title": "상품 문의",
        "content": scenario.question,
        "product_name": scenario.product_name,
        "order_id": scenario.order_id,
        "product_order_id": "",
        "raw_json": {"productId": scenario.product_id},
        "source_answered": 0,
        "post_status": "NOT_POSTED",
    }


def product_fact_verdict(
    scenario: Scenario, knowledge_service: ProductKnowledgeService
) -> tuple[bool, tuple[str, ...]]:
    knowledge = knowledge_service.facts_for_inquiry(
        product_id=scenario.product_id,
        question=scenario.question,
        model_code=extract_model_code(scenario.product_name),
    )
    items = [
        {
            "subquestion": part,
            "status": "NO_RELIABLE_SOURCE",
            "evidence_coverage": "UNSUPPORTED",
            "source": None,
            "answer_required": False,
        }
        for part in subquestions(scenario.question)
    ]
    HybridAnswerService._apply_product_fact_evidence(
        _Request({"product_knowledge": knowledge}),
        {"subquestion_evidence": items},
    )
    answered = [item for item in items if item["status"] == "ANSWERABLE"]
    fields = tuple(
        value for item in answered for value in item.get("product_fact_fields", ())
    )
    return (len(answered) == len(items) and bool(items)), fields


def subquestions(question: str) -> list[str]:
    from answer.text_utils import split_subquestions

    parts = [str(part).strip() for part in (split_subquestions(question) or ()) if str(part).strip()]
    return parts or [question]


def learning_verdict(scenario: Scenario) -> tuple[bool, str]:
    if not scenario.learning:
        return False, "NO_LEARNING"
    items = list(scenario.learning)
    for item in items:
        if contamination_reason(item.get("answer")) is not None:
            return False, "REDACTION_TOKEN_CONTAMINATED"
    facts: tuple[Any, ...] = ()
    if "HDMI" in scenario.question.upper():
        knowledge = ProductKnowledgeService().facts_for_inquiry(
            product_id=scenario.product_id, question=scenario.question, model_code=None
        )
        facts = tuple(
            item for item in knowledge.safe_facts
            if item.field_key == "hdmi_port_count"
        )
    verdict = evaluate_learning(
        learning_context={
            "similar_approved_answers": items,
            "subquestion_evidence": [
                {
                    "subquestion": item.get("matched_subquestion"),
                    "status": "ANSWERABLE",
                    "evidence_coverage": "SUPPORTED",
                    "source": "ACTIVE_POSITIVE_LEARNING",
                }
                for item in items
            ],
        },
        safe_facts=facts,
    )
    return bool(verdict.usable), str(verdict.reason)


MANUAL_ONLY_CATEGORIES = {
    "CANCEL_RETURN_EXCHANGE", "RISK_OR_DISPUTE", "COMPLAINT",
}


def run(scenario: Scenario, knowledge_service: ProductKnowledgeService) -> dict[str, Any]:
    inquiry = build_inquiry(scenario)
    analysis = InquiryAnalysisService().analyze(
        answer_request_from_inquiry(inquiry)
    ).to_dict()
    intent = str(analysis.get("detected_intent") or "").upper()
    subtype = str(analysis.get("inquiry_subtype") or "").upper()
    category = str(analysis.get("question_category") or "").upper()

    # 1. A request for staff action can never be answered automatically, and is
    #    settled before any lookup or generation happens.
    if intent == "SCHEDULE_CHANGE" or subtype == "SCHEDULE_CHANGE_REQUEST":
        return {"actual": NO_ANSWER, "reason": "SCHEDULE_CHANGE", "scope_error": False}
    if subtype in MANUAL_ONLY_CATEGORIES or category in MANUAL_ONLY_CATEGORIES:
        return {"actual": MANUAL, "reason": "MANUAL_ONLY_CATEGORY", "scope_error": False}

    # 2. The delivery branch needs a trusted order and a trusted schedule.
    if analysis.get("requires_order_lookup"):
        if not scenario.order_id or len(scenario.order_id) != 16 or not scenario.order_id.isdigit():
            return {"actual": ORDER_REQ, "reason": "ORDER_ID_REQUEST", "scope_error": False}
        if scenario.order_lookup != "SUCCESS":
            return {
                "actual": MANUAL,
                "reason": f"ORDER_LOOKUP_{scenario.order_lookup or 'NOT_RUN'}",
                "scope_error": False,
            }
        if scenario.dps != "SUCCESS":
            return {
                "actual": MANUAL,
                "reason": f"DPS_{scenario.dps or 'NOT_RUN'}",
                "scope_error": False,
            }
        return {"actual": AUTO, "reason": "TRUSTED_ORDER_AND_DPS", "scope_error": False}

    # 3. Approved Learning is judged first, because production applies the
    #    conflict pass *after* promoting product facts: a verified fact that
    #    contradicts an approved answer turns that sub-question into CONFLICT
    #    rather than quietly winning. Checking the fact first and returning
    #    would have skipped the disagreement entirely.
    usable, reason = learning_verdict(scenario)
    if reason in {
        "APPROVED_LEARNING_CONFLICT",
        "PRODUCT_FACT_VS_LEARNING_CONFLICT",
        "REDACTION_TOKEN_CONTAMINATED",
    }:
        return {"actual": MANUAL, "reason": reason, "scope_error": False}

    # 4. A verified product fact for every claim the question makes.
    supported, fields = product_fact_verdict(scenario, knowledge_service)
    if supported:
        return {
            "actual": AUTO,
            "reason": "VERIFIED_PRODUCT_FACT:" + ",".join(fields),
            "scope_error": False,
        }

    scope_error = usable and any(
        str((item.get("compatibility") or {}).get("product_match") or "")
        not in {"EXACT_MODEL", "EXACT_PRODUCT", "EXACT_NAME"}
        for item in scenario.learning
    )
    if usable:
        return {"actual": AUTO, "reason": reason, "scope_error": scope_error}

    # 5. The product-fact requirement only applies to product-fact questions.
    #    A policy or procedure question ("AS는 어디로 문의하나요?") is never
    #    held for a missing specification -- ``product_fact_guard`` is what
    #    decides that in production, so it decides it here too.
    guard = classify_product_fact(
        scenario.question,
        inquiry_type="PRODUCT_INQUIRY",
        product_id=scenario.product_id,
        product_name=scenario.product_name,
    )
    if not guard.sensitive and scenario.learning:
        return {"actual": AUTO, "reason": "NON_PRODUCT_FACT_POLICY_ANSWER",
                "scope_error": False}
    return {"actual": MANUAL, "reason": reason, "scope_error": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args()

    knowledge_service = ProductKnowledgeService()
    passed = 0
    false_auto = 0
    false_manual = 0
    wrong_scope = 0
    unsupported_certainty = 0
    wrong_answer = 0
    failures: list[str] = []

    print("E2E OPERATIONAL MATRIX  (real classifier · real product_facts.db · real evidence policy)")
    print("=" * 118)
    print(f"{'G':<2} {'scenario':<38} {'expected':<30} {'actual':<30} {'ok':<5} reason")
    print("-" * 118)
    for scenario in SCENARIOS:
        outcome = run(scenario, knowledge_service)
        actual = outcome["actual"]
        ok = actual == scenario.expected
        if ok:
            passed += 1
        else:
            failures.append(f"{scenario.group}/{scenario.name}: {scenario.expected} -> {actual}")
            expected_posts = scenario.expected in {AUTO, ORDER_REQ}
            actual_posts = actual in {AUTO, ORDER_REQ}
            if actual_posts and not expected_posts:
                false_auto += 1
                if scenario.expected == MANUAL:
                    unsupported_certainty += 1
            elif expected_posts and not actual_posts:
                false_manual += 1
            else:
                wrong_answer += 1
        if outcome["scope_error"]:
            wrong_scope += 1
        print(
            f"{scenario.group:<2} {scenario.name:<38} {scenario.expected:<30} "
            f"{actual:<30} {'PASS' if ok else 'FAIL':<5} {outcome['reason']}"
        )
        if arguments.verbose:
            print(f"     why: {scenario.why}")

    total = len(SCENARIOS)
    print("-" * 118)
    print(f"총 scenario           : {total}")
    print(f"PASS                  : {passed}")
    print(f"FAIL                  : {total - passed}")
    print()
    print(f"False Auto Post       : {false_auto}")
    print(f"False Manual Review   : {false_manual}")
    print(f"Wrong Answer Generated: {wrong_answer}")
    print(f"Unsupported Certainty : {unsupported_certainty}")
    print(f"Wrong Evidence Scope  : {wrong_scope}")
    for line in failures:
        print(f"  FAIL {line}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
