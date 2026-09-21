"""Safe answer scope expansion (2026-09-21).

PART A -- a Naver DAMAGE_REPORT that only asks how a suspected defect is
checked may publish the standard A/S route (10635 was held by the action
alone). Case-specific follow-ups (when, who pays, do it for me, any
CANCEL_RETURN), answers that settle the case, and every Coupang inquiry keep
the hold.

PART B -- compatibility with a product the catalogue does not identify may be
answered from a Learning GPT ② used, or by our product's verified conditions
alone. A definite yes/no nobody stands behind, or a measurement that disagrees
with the current catalogue, is held as PRODUCT_COMPATIBILITY_NOT_VERIFIED.
"""
from __future__ import annotations

import itertools
import json

import pytest

from answer.prompt_builder import PromptBuilder
from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.auto_processing_eligibility_service import (
    AutoProcessingEligibilityService,
)

NAVER = "OJE_PLUS"
COUPANG = "COUPANG_OJE_NS"
PASS = {"status": "PASS", "passed": True, "errors": [], "review_signals": [],
        "warnings": []}

GUIDANCE = (
    "말씀하신 증상은 먼저 삼성전자 서비스센터 1588-3366으로 접수하여 기사 점검을 "
    "받아주시기 바랍니다. 기사 점검 후 제품 불량으로 확인되는 경우 판매처 "
    "02-706-2678로 다시 연락해 주시면 후속 처리 안내를 도와드리겠습니다."
)
# The Product Knowledge as_guide sentence, quoted as the model tends to.
AS_GUIDE_QUOTE = (
    "제품에 하자가 있는 경우 삼성전자 서비스센터(1588-3366)로 접수하시면 A/S 기사의 "
    "판정을 거친 후 소비자분쟁해결기준에 의거하여 판매점에서 교환/환불해 드립니다."
)


def evaluate(answer, questions, *, store=NAVER, content="", draft_extra=None,
             product_catalog=None):
    gpt = {"answer": answer, "can_auto_post": True, "unresolved": [],
           "used_learning_ids": [], "used_product_facts": []}
    gpt.update(draft_extra or {})
    metadata = {
        "selected_answer_route": "GPT_FALLBACK",
        "processing_plan": {"analysis": {}},
        "semantic_routing": {"understanding": {
            "usable": True,
            "questions": [
                {"text": text, "action": action, "requested_attribute": attribute}
                for text, action, attribute in questions
            ],
        }},
        "hybrid": {"answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                   "draft": gpt, "validation": PASS},
    }
    if product_catalog is not None:
        metadata["product_catalog"] = product_catalog
    return AutoProcessingEligibilityService().evaluate(
        inquiry={"id": 1, "store_code": store, "content": content,
                 "source_answered": False, "post_status": "NOT_POSTED"},
        draft={"id": 1, "original_answer": answer, "review_status": "PENDING",
               "validation_status": "PASS", "validator_result_json": PASS,
               "posted": False, "metadata_json": metadata},
        route="GPT_FALLBACK",
    )


# =====================================================================
# PART A -- defect process guidance
# =====================================================================

DEFECT_GUIDANCE_CASES = [
    ("모니터에 세로줄이 있어요", "DAMAGE_REPORT", "UNKNOWN"),               # CASE A
    ("화면이 깜빡여요. 어떻게 하나요?", "DAMAGE_REPORT", "METHOD_OR_PROCEDURE"),
    ("전원이 이상한 것 같아요", "DAMAGE_REPORT", "METHOD_OR_PROCEDURE"),
    ("불량인 것 같은데 불량 맞나요?", "DAMAGE_REPORT", "EXISTENCE_OR_CAPABILITY"),
    ("서비스센터는 어디로 문의하나요?", "DAMAGE_REPORT", "LOCATION_OR_CONTACT"),
    ("A/S 기사 확인을 받아야 하나요?", "DAMAGE_REPORT", "METHOD_OR_PROCEDURE"),
    ("초기불량 같아요. 어떻게 처리하나요?", "REPAIR", "METHOD_OR_PROCEDURE"),
]


@pytest.mark.parametrize("question,action,attribute", DEFECT_GUIDANCE_CASES)
def test_A_naver_process_guidance_is_publishable(question, action, attribute):
    verdict = evaluate(GUIDANCE, [(question, action, attribute)], content=question)
    assert verdict.decision == "SAFE", verdict.reasons


def test_A_the_conditional_as_guide_sentence_is_procedure_not_a_promise():
    verdict = evaluate(AS_GUIDE_QUOTE, [("화면에 줄이 생겼어요", "DAMAGE_REPORT",
                                         "METHOD_OR_PROCEDURE")])
    assert verdict.decision == "SAFE", verdict.reasons


DEFECT_CASE_SPECIFIC = [
    ("기사님이 불량 판정했어요. 새 제품 언제 보내줘요?", "DAMAGE_REPORT", "TIMING"),  # CASE B
    ("교환 언제 발송되나요?", "CANCEL_RETURN", "TIMING"),
    ("제품 언제 회수하나요?", "CANCEL_RETURN", "TIMING"),
    ("환불 언제 되나요?", "CANCEL_RETURN", "TIMING"),
    ("파손 책임은 누구에게 있나요?", "DAMAGE_REPORT", "ACTOR"),
    ("보상해 주세요", "DAMAGE_REPORT", "ACTION_EXECUTION"),
    ("수리비는 얼마인가요?", "DAMAGE_REPORT", "AMOUNT_OR_COST"),
]


@pytest.mark.parametrize("question,action,attribute", DEFECT_CASE_SPECIFIC)
def test_A_case_specific_defect_stays_with_staff(question, action, attribute):
    verdict = evaluate(GUIDANCE, [(question, action, attribute)], content=question)
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "RETURN_OR_DAMAGE_POLICY_REVIEW" in verdict.reasons


def test_A_guidance_beside_a_return_request_is_held():
    verdict = evaluate(GUIDANCE, [
        ("화면에 줄이 있어요 어떻게 하나요", "DAMAGE_REPORT", "METHOD_OR_PROCEDURE"),
        ("환불해 주세요", "CANCEL_RETURN", "ACTION_EXECUTION"),
    ])
    assert "RETURN_OR_DAMAGE_POLICY_REVIEW" in verdict.reasons


@pytest.mark.parametrize(
    "answer",
    [
        "말씀하신 증상은 제품 불량입니다. 서비스센터 1588-3366으로 접수해 주세요.",
        "초기불량으로 확인됩니다. 판매처로 연락해 주세요.",
        "화면 줄 현상은 교환해 드리겠습니다.",
        "바로 환불됩니다. 서비스센터 1588-3366으로 접수해 주세요.",
        "새 제품은 내일 발송 예정입니다.",
    ],
)
def test_A_an_answer_that_settles_the_case_is_held(answer):
    verdict = evaluate(answer, [("화면에 줄이 있어요", "DAMAGE_REPORT",
                                 "METHOD_OR_PROCEDURE")])
    assert "RETURN_OR_DAMAGE_POLICY_REVIEW" in verdict.reasons


@pytest.mark.parametrize("store", [COUPANG, "COUPANG_OJE_PLUS", "", None])
def test_A_coupang_and_unknown_markets_keep_the_hold(store):
    """CASE C: the defect opening is Naver only."""

    verdict = evaluate(GUIDANCE, [("화면에 줄이 생겼어요", "DAMAGE_REPORT",
                                   "METHOD_OR_PROCEDURE")], store=store)
    assert "RETURN_OR_DAMAGE_POLICY_REVIEW" in verdict.reasons


def test_A_other_holds_still_apply_to_guidance():
    verdict = evaluate(GUIDANCE, [("화면에 줄이 있어요", "DAMAGE_REPORT",
                                   "METHOD_OR_PROCEDURE")],
                       draft_extra={"unresolved": ["증상"], "can_auto_post": False})
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "RETURN_OR_DAMAGE_POLICY_REVIEW" not in verdict.reasons
    assert "GPT_REPORTED_UNRESOLVED" in verdict.reasons


def test_A_official_numbers_pass_and_personal_numbers_block():
    ok = evaluate(GUIDANCE, [("화면에 줄", "DAMAGE_REPORT", "METHOD_OR_PROCEDURE")])
    assert ok.decision == "SAFE", ok.reasons
    leaked = evaluate(GUIDANCE + " 기사님 번호 010-1234-5678로 연락하세요.",
                      [("화면에 줄", "DAMAGE_REPORT", "METHOD_OR_PROCEDURE")])
    assert leaked.decision != "SAFE"


def test_A_generation_rule_forbids_a_defect_verdict():
    rules = " ".join(PromptBuilder.EVIDENCE_JUDGEMENT_RULES)
    assert "불량 여부를 판정하지 않는다" in rules
    assert "교환·환불·회수·보상 여부나 그 일정은 약속하지 않는다" in rules


# =====================================================================
# PART B -- external / unregistered product compatibility
# =====================================================================

AVA = "AVA 스마트 PRO QLED 75인치 TV도 호환 가능한가요?"
COMPAT = [(AVA, "PRODUCT_SPEC", "COMPATIBILITY")]
STAND_FACTS = [
    {"field_key": "vesa", "value": "75X75, 100X100"},
    {"field_key": "recommended_screen_size", "value": "17~35인치 TV"},
    {"field_key": "recommended_load", "value": "2~10"},
    {"field_key": "mount_weight_condition",
     "value": "35인치보다 크더라도 무게 10kg을 넘지 않아야 장착가능"},
]
SPEC_GUIDANCE = (
    "문의하신 AVA 스마트 PRO QLED 75인치 제품은 현재 당사 판매 제품 DB에서 확인되지 않아 "
    "호환 여부를 직접 확정해드리기 어렵습니다. 당사 거치대는 17~35인치, VESA 75x75 "
    "또는 100x100, 무게 10kg 이하 제품에 장착하실 수 있습니다. 사용하실 TV의 VESA "
    "규격과 무게가 위 조건에 맞는지 확인 후 구매 부탁드립니다."
)


def test_B1_same_pairing_learning_may_settle_it():            # CASE D
    verdict = evaluate("문의하신 AVA 스마트 PRO QLED 75 제품은 이 거치대와 호환됩니다.",
                       COMPAT, content=AVA,
                       draft_extra={"used_learning_ids": [41]},
                       product_catalog=[])
    assert verdict.decision == "SAFE", verdict.reasons


def test_B2_our_conditions_only_is_spec_guidance():           # CASE E
    verdict = evaluate(SPEC_GUIDANCE, COMPAT, content=AVA,
                       draft_extra={"used_product_facts": ["vesa", "recommended_load"]},
                       product_catalog=STAND_FACTS)
    assert verdict.decision == "SAFE", verdict.reasons


def test_B3_nothing_known_about_our_product_is_held():        # CASE F
    verdict = evaluate("호환 여부는 확인이 필요합니다.", COMPAT, content=AVA,
                       draft_extra={"unresolved": [AVA], "can_auto_post": False},
                       product_catalog=[])
    assert verdict.decision == "REVIEW_REQUIRED"
    assert "GPT_REPORTED_UNRESOLVED" in verdict.reasons


def test_B4_a_past_answer_never_outranks_the_catalogue():      # CASE G
    verdict = evaluate("이 거치대는 최대 50kg까지 지원하여 AVA 75인치와 호환됩니다.",
                       COMPAT, content=AVA,
                       draft_extra={"used_learning_ids": [41]},
                       product_catalog=[{"field_key": "max_load", "value": "40kg"}])
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" in verdict.reasons


@pytest.mark.parametrize(
    "answer",
    [
        "AVA 제품과 호환됩니다.",                                  # other-model Learning ignored
        "AVA 스마트 제품과 장착 가능합니다.",                        # name only similar
        "75인치까지 지원하므로 호환됩니다.",                          # size only
    ],
)
def test_B5_unsupported_yes_or_no_is_held(answer):
    verdict = evaluate(answer, COMPAT, content=AVA, product_catalog=STAND_FACTS)
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" in verdict.reasons


def test_B6_a_size_the_catalogue_does_not_state_is_held():
    verdict = evaluate("당사 거치대는 75인치까지 지원합니다. TV 규격을 비교해 주세요.",
                       [("AVA TV도 호환되나요?", "PRODUCT_SPEC", "COMPATIBILITY")],
                       content="AVA TV도 호환되나요?",
                       draft_extra={"used_product_facts": ["recommended_screen_size"]},
                       product_catalog=STAND_FACTS)
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" in verdict.reasons


def test_B7_compound_install_answer_survives_beside_spec_guidance():
    verdict = evaluate(
        SPEC_GUIDANCE + " 설치는 택배 발송 후 직접 조립하시는 상품입니다.",
        [*COMPAT, ("설치는 기사님이 해주시나요?", "INSTALLATION_METHOD", "ACTOR")],
        content=AVA, draft_extra={"used_product_facts": ["vesa"],
                                  "used_learning_ids": [7]},
        product_catalog=STAND_FACTS,
    )
    assert verdict.decision == "SAFE", verdict.reasons


def test_B8_identity_unclear_stays_unresolved():
    verdict = evaluate("현재 상품의 모델이 확정되지 않아 규격 확인이 필요합니다.", COMPAT,
                       content=AVA,
                       draft_extra={"unresolved": [AVA], "can_auto_post": False})
    assert verdict.decision == "REVIEW_REQUIRED"


def test_B9_non_compatibility_answers_are_untouched():
    """The guard reads only COMPATIBILITY atoms: ordinary spec answers keep
    their previous verdict even when they state a weight."""

    verdict = evaluate("제품 무게는 50kg입니다.",
                       [("무게가 얼마인가요?", "PRODUCT_SPEC", "SPEC_VALUE")],
                       product_catalog=[{"field_key": "max_load", "value": "40kg"}])
    assert "PRODUCT_COMPATIBILITY_NOT_VERIFIED" not in verdict.reasons


def test_B_generation_rule_is_spec_guidance_not_a_guess():
    rules = " ".join(PromptBuilder.EVIDENCE_JUDGEMENT_RULES)
    assert "추측하지 말고" in rules
    assert "unresolved 로 남기지 않되 호환된다/안 된다고 단정하지 않는다" in rules
    assert "Product Catalog 를 따른다" in rules


# ------------------------------------------------ market isolation (B7/B8)
_key = itertools.count()


def _learning(database, *, store, market=None):
    metadata = {"learning_signal_type": "POSITIVE", "human_verified": True}
    if market:
        metadata["market_applicability"] = market
    with database.connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO learning_examples (
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                final_answer, seller_answer, posted, rating, edit_ratio,
                quality_score, style_only, version, metadata_json, active,
                usage_count, created_at, updated_at, validity_type, validity_active
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (f"ext-{next(_key)}", "SELLER_ANSWER", AVA, AVA, store,
             "PRODUCT_INQUIRY", "이동식 거치대", "AVA 75인치는 호환됩니다.",
             "AVA 75인치는 호환됩니다.", 1, 5, 0.0, 1.0, 0, 1,
             json.dumps(metadata), 1, 0, "2026-08-01T00:00:00Z",
             "2026-08-01T00:00:00Z", "PERMANENT", 1),
        )
        return int(cursor.lastrowid)


def test_B_external_learning_keeps_market_isolation(tmp_path):
    database = Database(tmp_path / "ext.db")
    database.initialize()
    naver = _learning(database, store=NAVER)
    coupang = _learning(database, store=None, market="COUPANG_ONLY")
    repo = LearningRepository(database)

    naver_pool = {int(r["id"]) for r in repo.candidates(store_code=NAVER, market="NAVER", limit=2000)}
    ns_pool = {int(r["id"]) for r in repo.candidates(store_code=COUPANG, market="COUPANG", limit=2000)}
    plus_pool = {int(r["id"]) for r in repo.candidates(store_code="COUPANG_OJE_PLUS", market="COUPANG", limit=2000)}

    assert naver in naver_pool and coupang not in naver_pool        # Coupang -> Naver: 0
    assert naver not in ns_pool and naver not in plus_pool          # Naver -> Coupang: 0
    assert coupang in ns_pool and coupang in plus_pool              # shared by both accounts
