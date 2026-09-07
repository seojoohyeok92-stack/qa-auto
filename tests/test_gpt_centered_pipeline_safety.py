"""The other half of moving the judgement to GPT ②.

Handing the model the candidates instead of a verdict widens what it may
consider. These check it did not widen what may reach a customer: product
identity, order scope, validity and publication all still turn on facts the
code can settle -- a model code, a purchase state, a date, a boolean the model
itself reported -- and none of them on how the customer phrased the question.
"""
from __future__ import annotations

from datetime import UTC, datetime

from answer.facts import AnswerFacts
from answer.hybrid_models import Emotion, IntentResult
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.historical_case_service import HistoricalCaseService
from services.learning_context_service import LearningContextService
from services.semantic_analysis import (
    AtomicQuestion,
    DELIVERY_STATUS,
    INSTALLATION_METHOD,
    PRODUCT_SPEC,
    SemanticAnalysis,
)


PRODUCT_43 = "삼성 4K UHD 스마트 사이니지 TV LH43BEDH 기사님 방문설치 107.9cm(43인치)"
PRODUCT_55 = "삼성 4K UHD 스마트 사이니지 TV LH55BECH 기사님 방문설치 138cm(55인치)"
INSTALLER_ANSWER = "해당 상품은 삼성 기사님이 방문하여 설치하는 상품입니다."
INSTALLER_QUESTION = "삼성기사분이 설치하러 오시나요"


def _database(tmp_path, name):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    return database


def _seed_case(database, *, product_name, question, answer, external):
    service = HistoricalCaseService(database)
    case = service.prepare_case({
        "store_code": "OJE_PLUS",
        "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": external,
        "title": "상품 문의",
        "content": question,
        "product_name": product_name,
        "seller_answer": answer,
        "answered": True,
        "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference=f"FIXTURE:{external}")
    row, _ = service.repository.upsert(case)
    return int(row["id"])


def _inquiry(database, *, question, product_name, order_id=""):
    return InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "TEST",
        "source_question_id": f"safety-{abs(hash(question)) % 10**8}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": question,
        "product_id": "9645661432",
        "product_name": product_name,
        "option_name": "107.9cm(43인치), 스탠드",
        "order_id": order_id,
        "raw_json": {},
    }).inquiry_id


def _context(database, *, inquiry_id, question, product_name, semantic, order_id=""):
    return LearningContextService(database, hard_conflicts_only=True).build(
        AnswerFacts(
            inquiry={"inquiry_id": inquiry_id, "question": question},
            product={"name": product_name},
            order={"order_id": order_id} if order_id else {},
        ),
        IntentResult(
            "PRODUCT", (question,), Emotion.NORMAL, "NORMAL", 0.9, False, "",
        ),
        semantic_analysis=semantic,
    )


def _installer_semantic(question):
    return SemanticAnalysis(
        primary_action=INSTALLATION_METHOD,
        atomic_questions=(AtomicQuestion(
            text=question,
            action=INSTALLATION_METHOD,
            requested_information="설치를 수행하는 주체",
            requested_attribute="ACTOR",
        ),),
        purchase_state="UNKNOWN",
        confidence=0.95,
        source="GPT",
    )


def test_the_same_product_installer_case_is_offered_as_a_candidate(tmp_path):
    """The control. Without this the exclusion tests below prove nothing."""

    database = _database(tmp_path, "sameproduct")
    case_id = _seed_case(
        database,
        product_name=PRODUCT_43,
        question="설치 2시간만에 tv가 꺼졌어요. 삼성 기사님 오셔서 설치했고",
        answer=INSTALLER_ANSWER,
        external="same-product",
    )
    inquiry_id = _inquiry(
        database, question=INSTALLER_QUESTION, product_name=PRODUCT_43,
    )
    context = _context(
        database,
        inquiry_id=inquiry_id,
        question=INSTALLER_QUESTION,
        product_name=PRODUCT_43,
        semantic=_installer_semantic(INSTALLER_QUESTION),
    )
    attached = {
        int(item["historical_case_id"]) for item in context["historical_cases"]
    }
    assert case_id in attached, context["historical_cases"]
    evidence = context["subquestion_evidence"]
    assert [item["status"] for item in evidence] == ["CANDIDATE"], evidence
    assert case_id in set(evidence[0]["historical_case_ids"]), evidence


def test_a_different_models_spec_never_becomes_a_candidate(tmp_path):
    """Strict identity compares model codes, and it keeps its authority.

    A product-fact question is where a sibling model's answer is actually
    dangerous: the 55-inch panel has its own resolution, weight and VESA
    pattern. The compatibility gate rejects it during retrieval, so GPT ② is
    never offered the choice -- widening what the model may judge must not
    widen which products it may judge across.
    """

    database = _database(tmp_path, "othermodelspec")
    _seed_case(
        database,
        product_name=PRODUCT_55,
        question="해상도가 어떻게 되나요?",
        answer="해당 상품의 해상도는 4K UHD입니다.",
        external="other-model-spec",
    )
    question = "이 제품 해상도가 어떻게 되나요?"
    inquiry_id = _inquiry(
        database, question=question, product_name=PRODUCT_43,
    )
    semantic = SemanticAnalysis(
        primary_action=PRODUCT_SPEC,
        atomic_questions=(AtomicQuestion(
            text=question,
            action=PRODUCT_SPEC,
            requested_information="해상도",
            requested_attribute="SPEC_VALUE",
        ),),
        purchase_state="UNKNOWN",
        confidence=0.95,
        source="GPT",
    )
    context = _context(
        database,
        inquiry_id=inquiry_id,
        question=question,
        product_name=PRODUCT_43,
        semantic=semantic,
    )
    attached = [
        str(item.get("answer_reference") or "")
        for item in context["historical_cases"]
    ]
    assert not any("4K UHD" in item for item in attached), attached


def test_a_sibling_models_policy_answer_arrives_labelled_for_the_model(tmp_path):
    """Not every cross-model candidate is a spec claim.

    "누가 설치하나요" is answered by a standing operational policy, and a
    sibling listing's reply to it is often the right evidence -- which is why
    the identity gate is strict for product facts and advisory here. That is
    unchanged. What this checks is that the distinction reaches GPT ②: the
    candidate carries the product it came from and the gate's own verdict, so
    the model can weigh a cross-model reuse instead of being handed an
    unlabelled sentence.
    """

    database = _database(tmp_path, "siblingpolicy")
    _seed_case(
        database,
        product_name=PRODUCT_55,
        question="설치 2시간만에 tv가 꺼졌어요. 삼성 기사님 오셔서 설치했고",
        answer=INSTALLER_ANSWER,
        external="sibling-policy",
    )
    inquiry_id = _inquiry(
        database, question=INSTALLER_QUESTION, product_name=PRODUCT_43,
    )
    context = _context(
        database,
        inquiry_id=inquiry_id,
        question=INSTALLER_QUESTION,
        product_name=PRODUCT_43,
        semantic=_installer_semantic(INSTALLER_QUESTION),
    )
    cases = context["historical_cases"]
    assert cases, "a policy candidate should still be offered"
    candidate = cases[0]
    assert candidate["source_product_name"]
    assert PRODUCT_43 not in str(candidate["source_product_name"])
    compatibility = candidate["compatibility"]
    assert compatibility.get("product_match") not in {
        "EXACT_MODEL", "EXACT_PRODUCT",
    }, compatibility
    assert candidate["source_question"]


def test_a_past_orders_delivery_date_still_defers_to_the_current_order(tmp_path):
    """NEEDS_DPS survives, and it empties the candidate ids on purpose.

    "9월 3일 배송 예정입니다" was written about somebody else's order. No amount
    of relevance makes it a fact about this one, and that is settled by the
    purchase state and the DPS result rather than by reading the sentence.
    """

    database = _database(tmp_path, "pastdate")
    _seed_case(
        database,
        product_name=PRODUCT_43,
        question="주문한 상품 언제 배송되나요?",
        answer="고객님 주문 건은 9월 3일 배송 예정입니다.",
        external="past-date",
    )
    question = "제 주문 언제 배송되나요?"
    order_id = "2024010112345678"
    inquiry_id = _inquiry(
        database, question=question, product_name=PRODUCT_43, order_id=order_id,
    )
    semantic = SemanticAnalysis(
        primary_action=DELIVERY_STATUS,
        atomic_questions=(AtomicQuestion(
            text=question,
            action=DELIVERY_STATUS,
            requested_information="현재 주문의 배송 예정일",
            requested_attribute="TIMING",
        ),),
        requires_delivery_schedule=True,
        requires_order_context=True,
        asks_delivery_schedule=True,
        asks_delivery_outcome=True,
        purchase_state="CURRENT_ORDER",
        confidence=0.95,
        source="GPT",
    )
    context = _context(
        database,
        inquiry_id=inquiry_id,
        question=question,
        product_name=PRODUCT_43,
        semantic=semantic,
        order_id=order_id,
    )
    evidence = context["subquestion_evidence"]
    assert [item["status"] for item in evidence] == ["NEEDS_DPS"], evidence
    assert all(not item["historical_case_ids"] for item in evidence), evidence
    assert all(not item["learning_ids"] for item in evidence), evidence


def test_a_pre_purchase_delivery_question_is_still_held_for_staff(tmp_path):
    """No order exists, so no candidate may settle when it arrives."""

    database = _database(tmp_path, "prepurchase")
    _seed_case(
        database,
        product_name=PRODUCT_43,
        question="주문하면 배송 얼마나 걸리나요?",
        answer="결제 확인 후 1~2주 정도 소요됩니다.",
        external="pre-purchase",
    )
    question = "아직 주문 안 했는데 배송 얼마나 걸릴까요?"
    inquiry_id = _inquiry(
        database, question=question, product_name=PRODUCT_43,
    )
    semantic = SemanticAnalysis(
        primary_action=DELIVERY_STATUS,
        atomic_questions=(AtomicQuestion(
            text=question,
            action=DELIVERY_STATUS,
            requested_information="배송 소요기간",
            requested_attribute="TIMING",
        ),),
        requires_delivery_schedule=True,
        asks_delivery_schedule=True,
        asks_delivery_outcome=True,
        purchase_state="PRE_PURCHASE",
        confidence=0.95,
        source="GPT",
    )
    context = _context(
        database,
        inquiry_id=inquiry_id,
        question=question,
        product_name=PRODUCT_43,
        semantic=semantic,
    )
    evidence = context["subquestion_evidence"]
    assert [item["status"] for item in evidence] == [
        "DELIVERY_SCHEDULE_REVIEW"
    ], evidence
    assert all(not item["historical_case_ids"] for item in evidence), evidence


def test_validity_filtering_runs_before_anything_semantic(tmp_path):
    """An expired TEMPORARY row never reaches the candidate pool."""

    from repositories.learning_repository import LearningRepository
    from services.learning_validity_service import is_learning_usable

    assert callable(is_learning_usable)
    database = _database(tmp_path, "validity")
    diagnostics = LearningRepository(database).candidate_diagnostics(
        store_code="OJE_PLUS"
    )
    assert "filtered_by_validity" in diagnostics


def test_the_models_own_verdicts_are_hard_publication_reasons():
    from answer.hold_reasons import describe_reason
    from services.auto_processing_eligibility_service import (
        GPT_REPORTED_UNRESOLVED,
        GPT_WITHHELD_AUTO_POST,
        SOFT_REASONS,
        UNDERSTANDING_UNAVAILABLE,
    )

    for code in (
        GPT_REPORTED_UNRESOLVED,
        GPT_WITHHELD_AUTO_POST,
        UNDERSTANDING_UNAVAILABLE,
    ):
        assert code not in SOFT_REASONS, code
        # Every hold an operator can see has to say why in their language.
        assert describe_reason(code) != code, code


def _eligibility_draft(*, understanding_usable, draft_payload):
    return {
        "id": 1,
        "original_answer": INSTALLER_ANSWER,
        "validation_status": "PASS",
        "validator_result_json": {"passed": True, "status": "PASS"},
        "review_status": "DRAFT",
        "metadata_json": {
            "semantic_routing": {
                "understanding": {"usable": understanding_usable},
            },
            "hybrid": {
                "answer_pipeline": "GPT_UNDERSTAND_RETRIEVE_ANSWER",
                "draft": draft_payload,
                "subquestion_evidence": [
                    {"status": "CANDIDATE", "evidence_coverage": "UNSUPPORTED"}
                ],
            },
        },
    }


def _evaluate(draft):
    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    return AutoProcessingEligibilityService().evaluate(
        inquiry={
            "id": 1,
            "content": INSTALLER_QUESTION,
            "title": "",
            "source_answered": 0,
            "post_status": "NONE",
        },
        draft=draft,
        route="GPT_FALLBACK",
    )


def test_a_gpt_composed_answer_needs_gpt_understanding_to_publish():
    """GPT ① is required for the automatic path, never for drafting.

    With the semantic stage unavailable the pipeline still writes a draft and
    staff still send it. What it may not do is compose an answer with GPT ② --
    from candidates the keyword splitter retrieved, for a question nobody
    established the meaning of -- and publish it unread.

    Deterministic routes are outside this: they compose nothing, and where
    GPT ① is absent they keep every gate they had.
    """

    from services.auto_processing_eligibility_service import (
        UNDERSTANDING_UNAVAILABLE,
    )

    eligibility = _evaluate(_eligibility_draft(
        understanding_usable=False,
        draft_payload={"can_auto_post": True, "unresolved": []},
    ))
    assert eligibility.decision == "REVIEW_REQUIRED"
    assert UNDERSTANDING_UNAVAILABLE in eligibility.reasons


def test_an_unresolved_item_reported_by_the_model_holds_the_answer():
    from services.auto_processing_eligibility_service import (
        GPT_REPORTED_UNRESOLVED,
    )

    eligibility = _evaluate(_eligibility_draft(
        understanding_usable=True,
        draft_payload={
            "can_auto_post": True,
            "unresolved": ["설치 비용은 확인이 필요합니다"],
        },
    ))
    assert eligibility.decision == "REVIEW_REQUIRED"
    assert GPT_REPORTED_UNRESOLVED in eligibility.reasons


def test_the_model_may_withhold_publication_on_its_own():
    from services.auto_processing_eligibility_service import (
        GPT_WITHHELD_AUTO_POST,
    )

    eligibility = _evaluate(_eligibility_draft(
        understanding_usable=True,
        draft_payload={"can_auto_post": False, "unresolved": []},
    ))
    assert eligibility.decision == "REVIEW_REQUIRED"
    assert GPT_WITHHELD_AUTO_POST in eligibility.reasons


def test_a_clean_gpt_verdict_leaves_no_hard_reason():
    """The control for the three above: none of them fires on a clean draft."""

    eligibility = _evaluate(_eligibility_draft(
        understanding_usable=True,
        draft_payload={"can_auto_post": True, "unresolved": []},
    ))
    assert eligibility.decision == "SAFE", eligibility.reasons


def test_retrieval_candidates_no_longer_carry_a_binding_instruction():
    """The prompt contract itself, checked directly.

    Both halves matter: the binding key must be gone, and the replacement must
    actually tell the model the candidates are its to judge. A prompt that says
    neither would leave the behaviour to chance.
    """

    import json

    from answer.prompt_builder import PromptBuilder

    prompt = PromptBuilder().build(
        task="DRAFT",
        facts=AnswerFacts(
            inquiry={"inquiry_id": 1, "question": INSTALLER_QUESTION},
            product={"name": PRODUCT_43},
        ),
        extra={"historical_cases": [{"answer_reference": INSTALLER_ANSWER}]},
    )
    payload = json.loads(prompt)
    policy = payload["learning_usage_policy"]
    assert "subquestion_evidence_is_binding" not in policy
    assert policy["retrieval_candidates_are_not_approved_evidence"] is True
    assert policy["relevance_and_answer_support_are_hints_not_permission"] is True
    assert payload["evidence_judgement_rules"]
    # The two claims that must survive: a past order's facts are not this
    # order's, and stable knowledge is still usable.
    assert policy["historical_learning_forbidden_for_current_order_facts"] is True
    assert policy["safe_historical_learning_allowed_for_stable_knowledge"] is True
