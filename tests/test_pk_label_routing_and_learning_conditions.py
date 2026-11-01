"""PK provenance label, antenna/AUX field routing, and Learning conditions.

Three failure paths found in the 10-30 server backup audit, each fixed in the
existing structure:

1. The prompt described PRODUCT_CATALOG_JSON as "검증된 사양" while 93% of its
   facts carry ``verification: CANDIDATE_NOT_APPROVED``. GPT ② read the label
   as a ban and left 11180 / 11261 / 11166 unresolved with the answering fact
   in the prompt. The stored status is unchanged; the prompt now says the
   label is provenance. Only ``safe_facts`` are rendered, which these tests
   pin, so the explanation never reaches an unusable fact.
2. 동축/안테나 routed to ``rf_terminal``, a name the integrated records do not
   use, so 11344's ``analog_tuner`` was cut by the prompt budget; "AUX" routed
   nowhere, so 11279's ``headphone_out`` was cut. Output fields are routed for
   an outward AUX question only, never for "AUX 입력".
3. 10640 / 11396 / 11397 cited the bracket-reuse Learning and dropped its
   procedure (add-on purchase, on-site check, partial return). The Learning
   reached the prompt whole; the prompt now says its conditions are part of
   the answer.

What these tests can show is what reaches GPT ②. Whether GPT ② then answers
better can only be measured against the real provider.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from repositories.product_catalog_repository import ProductCatalogRepository
from services.product_knowledge_service import (
    AUX_OUTPUT_FIELDS,
    PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS,
    ProductFact,
    ProductKnowledgeResult,
    ProductKnowledgeService,
    _resolve_exact_fact_conflicts,
    attribute_families,
    fields_for_question,
    select_prompt_facts,
)

RF_FIELDS = {
    "rf_in", "rf_antenna_input", "port_antenna", "analog_tuner",
    "digital_broadcast_standard", "broadcast_system",
}


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def service() -> ProductKnowledgeService:
    return ProductKnowledgeService(ProductCatalogRepository())


def _knowledge(service, *, product_id, product_name, question):
    return service.facts_for_inquiry(
        product_id=product_id, questions=(question,), question=question,
        model_code=None, product_name=product_name,
        include_all_catalog_fields=True,
    )


def _prompt_fields(knowledge, question):
    selected, _facts, _info = select_prompt_facts(
        knowledge.safe_facts,
        requested_fields=knowledge.requested_fields,
        topics=knowledge.topics, question=question,
    )
    return selected


def _fact(field_key, value, *, model="LH43BEHHLGFXKR", index=0, **extra):
    return ProductFact(
        product_id="P1", listing_id="P1", model_code=model,
        field_key=field_key, value=value, raw_value=value, unit=None,
        scope="EXACT_MODEL", scope_key=model, component_scope="BASE_DEVICE",
        volatility="STATIC_PRODUCT_FACT",
        verification_status="CANDIDATE_NOT_APPROVED",
        resolution_status="RESOLVED", lifecycle_status="ACTIVE",
        canonical_fact_id=f"integrated:model_facts:{index}", value_id=None,
        safe_for_answer=True, **extra,
    )


def _draft_prompt(learning):
    from answer.facts import AnswerFacts
    from answer.hybrid_models import Emotion, IntentResult
    from services.draft_generation_service import DraftGenerationService

    captured = {}

    class Provider:
        def generate_json(self, *, task, prompt, context):
            captured["prompt"] = prompt
            return {"answer": "답변", "confidence": 0.9, "used_facts": [],
                    "missing_information": [], "requires_review": False,
                    "warnings": []}

    DraftGenerationService(
        Provider(), learning_context_provider=lambda *_: learning,
    ).generate(
        AnswerFacts(inquiry={"question": "기존 브라켓에 설치 가능한가요?"}),
        IntentResult("GENERAL", ("기존 브라켓에 설치 가능한가요?",),
                     Emotion.NORMAL, "NORMAL", 0.9, False, ""),
    )
    return captured["prompt"]


def _sources():
    prompt = _draft_prompt({})
    return json.loads(prompt)["input"]["context_sources"]


# ==========================================================================
# 1. the verification label is provenance, and only safe facts carry it
# ==========================================================================

def test_the_catalog_block_says_verification_is_provenance_not_a_ban() -> None:
    text = _sources()["PRODUCT_CATALOG_JSON"]

    assert "CANDIDATE_NOT_APPROVED" in text
    assert "사용 허가나 금지가 아니다" in text
    # Applicability is still the model's to judge, against the fact's scope.
    assert "product_scope" in text and "subject" in text
    # The unconfirmed-identity block keeps its own meaning.
    assert "product_candidates" in text
    # A description, not a ranking against Learning.
    assert "우선" not in text


def test_the_label_explanation_matches_the_learning_label_pattern() -> None:
    sources = _sources()

    assert "사용 허가나" in sources["APPROVED_LEARNING"]
    assert "사용 허가나" in sources["PRODUCT_CATALOG_JSON"]


def test_a_safe_candidate_fact_reaches_the_block_with_its_stored_label(
    service,
) -> None:
    """11261: LH50BEHHLGFXKR's ``bluetooth_audio`` is safe and is delivered."""

    question = "TV와 블루투스로 무선 헤드폰을 연결해 소리를 들을 수 있나요?"
    knowledge = _knowledge(
        service, product_id="13684610101",
        product_name=("삼성 4K UHD 스마트 비즈니스 TV LH50BEHHLGFXKR 1등급"
                      " 125.7cm(50인치), 스탠드"),
        question=question,
    )
    selected = _prompt_fields(knowledge, question)
    bluetooth = [item for item in selected if item.field_key == "bluetooth_audio"]

    assert bluetooth, [item.field_key for item in selected]
    assert bluetooth[0].safe_for_answer is True
    # The stored status is not renamed to make it look approved.
    assert bluetooth[0].verification_status == "CANDIDATE_NOT_APPROVED"
    assert "verification: CANDIDATE_NOT_APPROVED" in knowledge.prompt_block(selected)


def test_every_rendered_fact_is_safe_and_no_excluded_fact_is_rendered(
    service,
) -> None:
    question = "동축케이블로만 시청이 가능한가요?"
    knowledge = _knowledge(
        service, product_id="13684605198",
        product_name=("삼성 4K UHD 스마트 비즈니스 TV LH43BEHHLGFXKR 1등급"
                      " 107.9cm(43인치), 스탠드"),
        question=question,
    )
    selected = _prompt_fields(knowledge, question)
    block = knowledge.prompt_block(selected)

    assert selected and all(item.safe_for_answer for item in selected)
    for item in knowledge.excluded_facts:
        assert f"evidence_id: {item.canonical_fact_id}\n" not in block + "\n"


def test_a_fact_withheld_as_unsafe_is_never_rendered() -> None:
    safe = _fact("analog_tuner", "있음", index=1)
    unsafe = replace(_fact("rf_in", "1/1/0", index=2), safe_for_answer=False,
                     exclusion_reason="UNCONFIRMED_MARKET_MAPPING")
    result = ProductKnowledgeResult(
        product_id="P1", listing_id="P1", matched=True,
        safe_facts=(safe,), excluded_facts=(unsafe,),
    )
    block = result.prompt_block()

    assert "analog_tuner" in block
    assert "rf_in" not in block


def test_an_unresolved_conflict_is_withheld_before_it_can_be_labelled() -> None:
    first = _fact("analog_tuner", "있음", index=1)
    second = _fact("analog_tuner", "없음", index=2)

    safe, excluded = _resolve_exact_fact_conflicts([first, second])

    assert not [item for item in safe if item.field_key == "analog_tuner"]
    assert {item.field_key for item in excluded} == {"analog_tuner"}
    assert all(not item.safe_for_answer for item in excluded)


def test_another_models_facts_never_enter_this_listings_block(service) -> None:
    question = "동축케이블로만 시청이 가능한가요?"
    knowledge = _knowledge(
        service, product_id="13684605198",
        product_name=("삼성 4K UHD 스마트 비즈니스 TV LH43BEHHLGFXKR 1등급"
                      " 107.9cm(43인치), 스탠드"),
        question=question,
    )
    models = {
        str(item.model_code or "") for item in knowledge.safe_facts
        if item.scope == "EXACT_MODEL"
    }

    assert models and all("43BEH" in model for model in models), models


# ==========================================================================
# 2. antenna / broadcast routing
# ==========================================================================

@pytest.mark.parametrize("question", [
    "동축케이블로만 시청이 가능한가요?",
    "안테나 연결해서 볼 수 있나요?",
    "지상파 방송 수신 되나요?",
    "공중파 나오나요?",
])
def test_antenna_wording_asks_for_the_stored_reception_fields(question) -> None:
    fields, _topics = fields_for_question(question)

    assert RF_FIELDS <= set(fields)
    # The registry's own name is kept; nothing is renamed or merged.
    assert "rf_terminal" in fields


def test_11344_keeps_the_reception_facts_inside_the_budget(service) -> None:
    question = "지금 현재 동축케이블로만 티비를 시청하고 있는데 이제품도 동축케이블로만 시청이 가능한가요?"
    knowledge = _knowledge(
        service, product_id="13684605198",
        product_name=("삼성 4K UHD 스마트 비즈니스 TV LH43BEHHLGFXKR 1등급"
                      " 107.9cm(43인치), 스탠드"),
        question=question,
    )
    selected = _prompt_fields(knowledge, question)
    kept = {item.field_key for item in selected}

    assert {"rf_in", "analog_tuner"} <= kept, sorted(kept)
    # The budget did not grow to make room.
    assert len(knowledge.prompt_block(selected)) <= PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS
    assert len(selected) < len(knowledge.safe_facts)


@pytest.mark.parametrize("question", [
    "케이블 지상파 kbs나 mbc 볼 수 있나요?",          # 10843
    "티비 시청료 내야하나요. 지상파 안볼건데요.",      # 10919
])
def test_an_antenna_question_keeps_the_facts_it_carried_before(
    service, question,
) -> None:
    """10843/10919: asking for broadcast_system / digital_broadcast_standard
    lifted operating_system and quality_warranty_standard (shared words
    "system", "standard") over tv_plus and multi_device."""

    knowledge = _knowledge(
        service, product_id="9775146473",
        product_name="삼성 삼탠바이미 32인치(80cm) M5 스마트 모니터 IPTV+2in1 이동식 거치대",
        question=question,
    )
    assert {"broadcast_system", "digital_broadcast_standard"} <= set(
        knowledge.requested_fields)
    kept = {item.field_key for item in _prompt_fields(knowledge, question)}

    assert {"tv_plus", "multi_device"} <= kept, sorted(kept)


def test_a_shared_word_in_an_asked_field_name_lifts_no_other_field() -> None:
    facts = [
        _fact("broadcast_system", "ATSC 3.0", index=0),
        _fact("operating_system", "Tizen", index=1),
        _fact("safety_standard", "KC 안전인증", index=2),
        _fact("tv_plus", "지원", index=3),
    ]
    ordered, _facts, _info = select_prompt_facts(
        facts, requested_fields=("broadcast_system",), question="지상파 되나요?",
        budget_chars=PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS,
    )
    keys = [item.field_key for item in ordered]

    # The asked field still ranks first; the others fall back to record order
    # instead of being promoted by "system" / "standard".
    assert keys[0] == "broadcast_system"
    assert keys.index("tv_plus") < keys.index("operating_system")
    assert keys.index("tv_plus") < keys.index("safety_standard")


def test_a_shared_word_still_counts_when_the_customer_writes_it() -> None:
    """Only the word borrowed from a field name is dropped, not the question's."""

    facts = [_fact("tv_plus", "지원", index=0),
             _fact("operating_system", "Tizen", index=1)]
    ordered, _facts, _info = select_prompt_facts(
        facts, question="operating system 이 뭔가요?")

    assert ordered[0].field_key == "operating_system"


def test_a_record_without_reception_fields_gains_none(service) -> None:
    """Routing names fields; it never makes a value the record lacks."""

    question = "동축케이블 연결 되나요?"
    knowledge = _knowledge(
        service, product_id="12601323000",
        product_name="삼성 오디세이 G5 LS32FG500 80.1cm(32인치) 게이밍 모니터 180Hz",
        question=question,
    )
    stored = {item.field_key for item in knowledge.safe_facts}

    assert not (stored & {"analog_tuner", "broadcast_system",
                          "digital_broadcast_standard"})


def test_attribute_families_are_unchanged_by_the_new_route() -> None:
    """Learning compatibility reads families; this route must not touch them."""

    assert attribute_families("동축케이블로만 시청이 가능한가요?") == frozenset()
    assert attribute_families("지상파 방송 수신 되나요?") == frozenset()


@pytest.mark.parametrize("question, expected", [
    ("HDMI 몇 개인가요?", {"hdmi_port_count"}),
    ("베사 규격 알려주세요", {"vesa_mm"}),
    ("무게가 얼마나 되나요?", {"weight_with_stand_kg", "weight_without_stand_kg"}),
    ("블루투스 되나요?", {"bluetooth_present", "bluetooth_version"}),
])
def test_other_specification_routes_are_untouched(question, expected) -> None:
    fields, _topics = fields_for_question(question)

    assert expected <= set(fields)
    assert not (RF_FIELDS & set(fields))
    assert "headphone_out" not in fields
    assert "headphone_out" in AUX_OUTPUT_FIELDS


# ==========================================================================
# 2b. AUX: an output jack is asked for only when the question points outward
# ==========================================================================

@pytest.mark.parametrize("question", [
    "Auxiliary 단자는 따로 없나요  스피커 연결관련",
    "스피커 연결을 위한 AUX(보조) 단자가 별도로 있나요?",
    "AUX 출력 단자 있나요?",
])
def test_an_outward_aux_question_asks_for_the_output_jack(question) -> None:
    fields, topics = fields_for_question(question)

    assert "headphone_out" in fields
    assert "aux" in topics


@pytest.mark.parametrize("question", [
    "AUX 입력 단자로 휴대폰 소리를 넣을 수 있나요?",
    "aux in 단자가 있어서 외부 기기 소리를 스피커로 들을 수 있나요?",
    "AUX 인풋 지원하나요?",
])
def test_an_aux_input_question_never_asks_for_an_output_jack(question) -> None:
    fields, topics = fields_for_question(question)

    assert "headphone_out" not in fields
    assert "aux" not in topics


def test_a_bare_aux_question_names_no_direction_and_asks_for_nothing() -> None:
    fields, _topics = fields_for_question("AUX 단자 있나요?")

    assert "headphone_out" not in fields


def test_a_headphone_question_still_reaches_headphone_out() -> None:
    fields, _topics = fields_for_question("헤드폰 단자 있나요?")

    assert "headphone_out" in fields


def test_11279_delivers_headphone_out_inside_the_budget(service) -> None:
    question = "Auxiliary 단자는 따로 없나요  스피커 연결관련"
    knowledge = _knowledge(
        service, product_id="13331814331",
        product_name=("삼성 오디세이 G9 LS49DG930 124.3cm(49인치) OLED 32:9"
                      " 울트라 와이드 게이밍 모니터"),
        question=question,
    )
    selected = _prompt_fields(knowledge, question)

    assert "headphone_out" in {item.field_key for item in selected}
    assert len(knowledge.prompt_block(selected)) <= PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS


# ==========================================================================
# 3. a Learning's conditions travel with it and are named as part of the answer
# ==========================================================================

BRACKET_PROCEDURE = (
    "기존 브라켓과의 호환 여부에 대해서는 현장 확인이 필요한 사항이 많아 현장에서"
    " 설치 기사님께서 확인해주실 수 있습니다.\n하여 우선 추가상품의 \"벽걸이 설치"
    " 변경\" 항목을 선택하시어 구매해주신 뒤 현장에서 기존 브라켓에 설치가 가능하신"
    " 경우\n기사님에게 구매하신 브라켓은 부분 반품 요청 주실 수 있으시며 이후"
    " 판매자고객센터로 연락주시면 처리 도와드리겠습니다."
)


def test_the_learning_block_says_its_conditions_are_part_of_the_answer() -> None:
    text = _sources()["APPROVED_LEARNING"]

    assert "조건과 절차는 답의 일부" in text
    assert "부분 반품" in text
    # Applicability is still judged, and nothing is promised without it.
    assert "확정하지 마라" in text
    # The existing provenance and unresolved rules are kept.
    assert "사용 허가나" in text and "unresolved" in text


def test_the_bracket_procedure_reaches_the_prompt_whole() -> None:
    prompt = _draft_prompt({
        "similar_approved_answers": [{
            "learning_id": 316844,
            "question": "기존 삼성티비 48인치 벽걸이로 사용중인데 기존 브라켓에 설치 가능한가요?",
            "answer": BRACKET_PROCEDURE,
        }],
    })
    answers = [
        item.get("answer")
        for item in json.loads(prompt)["input"]["similar_approved_answers"]
    ]

    assert BRACKET_PROCEDURE in answers


def test_the_condition_rule_does_not_override_the_atomic_scope_rule() -> None:
    """Unasked attributes are still not invented; only cited conditions travel."""

    from services.draft_generation_service import ATOMIC_QUESTION_INSTRUCTIONS

    joined = " ".join(ATOMIC_QUESTION_INSTRUCTIONS)

    assert "고객이 묻지 않은 속성" in joined
