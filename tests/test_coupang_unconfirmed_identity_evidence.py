"""An unconfirmed Coupang mapping, and what may still be answered from it.

Phase 2-B replayed three real model-specific Coupang inquiries whose option
mapping is not confirmed -- 10838 (MANUAL_REQUIRED/FAMILY_ONLY, "삼성 정품이며
공식 as가 가능한 제품인가요? 무상as 기간"), 10315 and 10847
(UNRESOLVED/NO_MODEL_EVIDENCE, "해외(일본)에서도 사용가능한가요?" and a
firmware fault) -- and the gate let all three through.  Two separate faults
were behind that, and they are fixed separately because they are separate
questions.

**Product Knowledge reached an identity the mapping had refused.**  Asked for
the whole catalogue record, ``facts_for_inquiry`` reads an identity out of the
listing's own name, and on "샥스 SHAKS G1 / 일반 01" that produced
``identity_status`` EXACT with ``model_code`` "샥스" -- a brand token in a
model's place -- and six MODEL_CATALOG facts including ``resolution`` 2160p
and ``model_name`` SHAKS G1.  The model-code gate could not see it because
the name never passed through ``extract_model_code``; it is Hangul, and that
pattern is ASCII.  Model-bound facts are now withheld whenever the mapping is
unconfirmed, whatever the question was, and withheld into
``excluded_facts`` so the screen shows them as withheld rather than absent.

**The refusal asked Product Knowledge which model the question was about.**
``inquiry_target_models`` is Product Knowledge's answer to the identity
question, so requiring it meant the gate could only fire once the identity it
protects already existed -- on all three real inquiries it was empty.  The
gate now reads GPT ①'s per-atom ``action`` against
``COUPANG_IDENTITY_REQUIRED_ACTIONS`` -- the shared ``PRODUCT_FACT_ACTIONS``
that Learning identity enforcement uses for "a property of one identified
product", plus REPAIR, which is a statement about one device too.  The
extension lives on this gate so the shared set keeps deciding Naver Learning
identity unchanged.

The two must stay separate: a seller or policy question on an unconfirmed
listing still gets its answer, and only loses facts about a model nobody
confirmed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer.exceptions import AutoAnswerProhibitedError
from repositories.coupang_product_mapping_repository import (
    AMBIGUOUS, AUTO_EXACT, FAMILY_ONLY, MANUAL_REQUIRED, NO_MODEL_EVIDENCE,
    UNRESOLVED,
)
from repositories.product_catalog_repository import ProductCatalogRepository
from services.answer_service import (
    COUPANG_IDENTITY_REQUIRED_ACTIONS, AnswerService,
)
from services.learning_context_service import PRODUCT_FACT_ACTIONS
from services.market_policy import (
    is_store_automatic_post_enabled, is_store_dps_enabled,
    is_store_manual_post_enabled,
)
from services.product_knowledge_service import (
    MODEL_BOUND_SCOPES, UNCONFIRMED_MARKET_MAPPING, ProductKnowledgeService,
)

COUPANG_STORE = "COUPANG_OJE_NS"
ACCOUNT = "OJE_NS"
VENDOR_ITEM = "92531933693"

# The three real listings, as the Coupang catalogue enrichment supplies them.
SHAKS_LISTING = "샥스 SHAKS G1 / 일반 01"
SHAKS_SETTOP = "샥스 SHAKS G1 셋톱박스"
SIGNAGE_LISTING = "사이니지 패키지 BEF / 모음전 / 메인코드 / O"


class FakeMappingRepository:
    row: dict | None = None

    def __init__(self, database=None) -> None:
        self.database = database

    def get(self, *, account_code, vendor_item_id):
        if self.row is None:
            return None
        if (str(account_code) != ACCOUNT
                or str(vendor_item_id) != VENDOR_ITEM):
            return None
        return dict(self.row)

    def get_confirmed(self, *, account_code, vendor_item_id):
        row = self.get(account_code=account_code, vendor_item_id=vendor_item_id)
        return row if row and row.get("mapping_status") == "CONFIRMED" else None


@pytest.fixture
def mapping(monkeypatch):
    import services.answer_service as answer_service

    repository = FakeMappingRepository
    repository.row = None
    monkeypatch.setattr(
        answer_service, "CoupangProductMappingRepository", repository)

    def set_row(*, resolution, evidence_class=None, confirmed=False,
                canonical="S32DM501"):
        repository.row = {
            "account_code": ACCOUNT, "vendor_item_id": VENDOR_ITEM,
            "canonical_model": canonical if confirmed else None,
            "resolution": resolution,
            "model_evidence_class": evidence_class,
            "mapping_status": "CONFIRMED" if confirmed else "NEEDS_REVIEW",
        }

    return set_row


@pytest.fixture
def service():
    built = AnswerService.__new__(AnswerService)
    built.database = None
    built.product_knowledge = SimpleNamespace(catalog_repository=None)
    return built


def request_for(service, *, question, actions, product_name=SHAKS_LISTING):
    row = {
        "store_code": COUPANG_STORE,
        "source_type": "COUPANG_ONLINE_INQUIRY",
        "source_question_id": "160959847",
        "raw_json": {"vendorItemId": VENDOR_ITEM},
        "source_metadata_json": {"account_code": ACCOUNT},
        "product_name": product_name,
    }
    request = SimpleNamespace(
        store_code=COUPANG_STORE, product_name=product_name,
        question=question, metadata={},
    )
    service._attach_market_identity(request, row)
    # Exactly the shape ``_attach_semantic_routing`` leaves behind.
    request.metadata["gpt_understanding"] = {
        "usable": True,
        "questions": [{"text": question, "action": action,
                       "requested_information": ""} for action in actions],
    }
    return request


#: What Product Knowledge really hands back for an unconfirmed mapping.
#: Present in every gate call so the gate is seen not to consult it.
NO_PK_TARGETS = SimpleNamespace(inquiry_target_models=())


# --- 1-3. every unconfirmed resolution blocks a model-specific question ---

@pytest.mark.parametrize("resolution,evidence", [
    pytest.param(MANUAL_REQUIRED, FAMILY_ONLY, id="manual-required-family"),
    pytest.param(UNRESOLVED, NO_MODEL_EVIDENCE, id="unresolved-no-evidence"),
    pytest.param(MANUAL_REQUIRED, AMBIGUOUS, id="manual-required-ambiguous"),
    pytest.param(None, None, id="no-mapping-row"),
])
@pytest.mark.parametrize(
    "action", sorted(COUPANG_IDENTITY_REQUIRED_ACTIONS))
def test_a_model_specific_question_is_blocked_with_no_pk_targets(
    service, mapping, resolution, evidence, action
) -> None:
    """The real shape: Product Knowledge names no target model at all."""

    if resolution is not None:
        mapping(resolution=resolution, evidence_class=evidence)
    request = request_for(
        service, question="이 제품 해외(일본)에서도 사용가능한가요?",
        actions=(action,))
    assert NO_PK_TARGETS.inquiry_target_models == ()

    with pytest.raises(AutoAnswerProhibitedError) as raised:
        service._require_confirmed_identity_for_model_question(
            request, NO_PK_TARGETS)

    assert raised.value.policy_reason == "COUPANG_MODEL_MAPPING_UNCONFIRMED"
    assert raised.value.policy_details["blocked_identity_actions"] == [action]
    assert raised.value.policy_details["blocked_mapping_resolution"] == (
        resolution or "NONE")


def test_the_gate_reads_the_understanding_not_product_knowledge(
    service, mapping
) -> None:
    """A target model offered by Product Knowledge changes nothing.

    The old gate fired on this and only on this, which is why it never fired
    on real data.  Here the understanding says the question is not about a
    product property, and the inferred model does not override that.
    """

    mapping(resolution=UNRESOLVED, evidence_class=NO_MODEL_EVIDENCE)
    request = request_for(
        service, question="환불은 언제 이루어지나요?",
        actions=("CANCEL_RETURN",))

    service._require_confirmed_identity_for_model_question(
        request, SimpleNamespace(inquiry_target_models=("샥스", "SHAKS G1")))


# --- 4. a guessed identity delivers no model-bound fact -------------------

def test_a_brand_token_identity_delivers_no_model_fact(
    service, mapping
) -> None:
    """The real 10315/10847 record, through the real Product Knowledge call.

    Not a fixture: the catalogue is read and the listing name is the one the
    Coupang enrichment produces, so this fails if that second identity route
    ever stops being withheld.
    """

    mapping(resolution=UNRESOLVED, evidence_class=NO_MODEL_EVIDENCE)
    question = "안녕하세요 이 제품 해외(일본)에서도 사용가능한가요?"
    knowledge = ProductKnowledgeService(
        ProductCatalogRepository()
    ).facts_for_inquiry(
        product_id=None, question=question, questions=(question,),
        model_code=None, product_name=SHAKS_LISTING, option_name=None,
        include_all_catalog_fields=True,
    )
    # The inference this is about: an identity the mapping never confirmed.
    assert knowledge.identity_status == "EXACT"
    assert knowledge.has_safe_facts
    assert {fact.scope for fact in knowledge.safe_facts} <= MODEL_BOUND_SCOPES
    assert knowledge.inquiry_target_models == ()

    request = request_for(service, question=question,
                          actions=("PRODUCT_SPEC",))
    withheld = service._withhold_unconfirmed_model_facts(request, knowledge)

    assert withheld.safe_facts == (), "model-bound evidence reached the prompt"
    assert withheld.has_safe_facts is False
    assert withheld.listing_model is None
    assert {fact.exclusion_reason for fact in withheld.excluded_facts} == {
        UNCONFIRMED_MARKET_MAPPING}
    # Withheld, not vanished: the screen reads ``excluded_facts``.
    assert len(withheld.excluded_facts) == len(knowledge.safe_facts)
    recorded = request.metadata["model_bound_facts_withheld"]
    assert recorded["reason"] == UNCONFIRMED_MARKET_MAPPING
    assert recorded["mapping_resolution"] == UNRESOLVED
    assert "model_name" in recorded["withheld_fields"]


def test_a_listing_scoped_fact_is_not_withheld(service, mapping) -> None:
    """Only facts that depend on a model identity are withheld.

    PRODUCT_LISTING is what the listing says about itself and names no model,
    so withholding it would remove evidence the mapping has no bearing on.
    """

    mapping(resolution=MANUAL_REQUIRED, evidence_class=FAMILY_ONLY)
    listing_fact = SimpleNamespace(
        scope="PRODUCT_LISTING", field_key="delivery_type",
        exclusion_reason=None, safe_for_answer=True)
    knowledge = SimpleNamespace(
        safe_facts=(listing_fact,), excluded_facts=(),
        without_model_bound_facts=lambda *_a, **_k: SimpleNamespace(
            safe_facts=(listing_fact,), excluded_facts=()),
    )
    request = request_for(service, question="택배 배송인가요?",
                          actions=("DELIVERY_POLICY",))

    kept = service._withhold_unconfirmed_model_facts(request, knowledge)
    assert kept.safe_facts == (listing_fact,)
    assert "model_bound_facts_withheld" not in request.metadata


# --- 5. identity-free questions keep their answer -------------------------

@pytest.mark.parametrize("question,action", [
    pytest.param("폐가전도 무상으로 수거해주시나요?", "COLLECTION",
                 id="10728-collection"),
    pytest.param("물건은 회수 된듯한데 환불은 언제 이루어지나요?",
                 "CANCEL_RETURN", id="7426-refund"),
    pytest.param("거래명세서 발급 받을 수 있나요?", "FORM_FIELD_GUIDANCE",
                 id="document"),
    pytest.param("반품 절차가 어떻게 되나요?", "CANCEL_RETURN", id="return"),
    pytest.param("설치는 어떻게 진행되나요?", "INSTALLATION_METHOD",
                 id="installation"),
])
def test_a_question_without_a_product_property_is_still_answered(
    service, mapping, question, action
) -> None:
    mapping(resolution=MANUAL_REQUIRED, evidence_class=FAMILY_ONLY)
    request = request_for(service, question=question, actions=(action,),
                          product_name=SIGNAGE_LISTING)

    service._require_confirmed_identity_for_model_question(
        request, NO_PK_TARGETS)
    assert action not in COUPANG_IDENTITY_REQUIRED_ACTIONS


def test_a_compound_inquiry_blocks_on_its_product_atom(
    service, mapping
) -> None:
    """One product atom among policy atoms is still a product question."""

    mapping(resolution=MANUAL_REQUIRED, evidence_class=FAMILY_ONLY)
    request = request_for(
        service,
        question="삼성 정품인가요? 무상 AS 기간은요? 반품도 가능한가요?",
        actions=("CANCEL_RETURN", "PRODUCT_CONCEPT"),
        product_name=SIGNAGE_LISTING)

    with pytest.raises(AutoAnswerProhibitedError) as raised:
        service._require_confirmed_identity_for_model_question(
            request, NO_PK_TARGETS)
    assert raised.value.policy_details["blocked_identity_actions"] == [
        "PRODUCT_CONCEPT"]


def test_no_understanding_blocks_nothing(service, mapping) -> None:
    """GPT ① being unavailable is not a reason to start refusing inquiries."""

    mapping(resolution=UNRESOLVED, evidence_class=NO_MODEL_EVIDENCE)
    request = request_for(service, question="해상도가 어떻게 되나요?",
                          actions=())
    request.metadata.pop("gpt_understanding")

    service._require_confirmed_identity_for_model_question(
        request, NO_PK_TARGETS)


# --- REPAIR: a fault diagnosis is about one device ------------------------

def test_the_coupang_set_extends_the_shared_one_without_changing_it() -> None:
    """REPAIR is added for this gate only.

    ``PRODUCT_FACT_ACTIONS`` decides which stored answers a Naver inquiry may
    reuse. Adding REPAIR there would change that, so the Coupang gate carries
    its own superset instead.
    """

    assert PRODUCT_FACT_ACTIONS < COUPANG_IDENTITY_REQUIRED_ACTIONS
    assert COUPANG_IDENTITY_REQUIRED_ACTIONS - PRODUCT_FACT_ACTIONS == {
        "REPAIR"}
    assert "REPAIR" not in PRODUCT_FACT_ACTIONS


@pytest.mark.parametrize("resolution,evidence", [
    pytest.param(MANUAL_REQUIRED, FAMILY_ONLY, id="manual-required-family"),
    pytest.param(MANUAL_REQUIRED, AMBIGUOUS, id="manual-required-ambiguous"),
    pytest.param(UNRESOLVED, NO_MODEL_EVIDENCE, id="unresolved-no-evidence"),
    pytest.param(None, None, id="no-mapping-row"),
])
def test_a_repair_question_is_blocked_while_the_mapping_is_unconfirmed(
    service, mapping, resolution, evidence
) -> None:
    """The real 10847: a firmware fault on an UNRESOLVED option.

    It was answered before this, because REPAIR was in neither set. The
    model-bound facts were already withheld; what was missing was the refusal
    to write a repair procedure for a device nobody had identified.
    """

    if resolution is not None:
        mapping(resolution=resolution, evidence_class=evidence)
    request = request_for(
        service,
        question="업데이트후 화면에 shaks 로고만 나오고 진행이 안되는데요",
        actions=("REPAIR",), product_name=SHAKS_SETTOP)

    with pytest.raises(AutoAnswerProhibitedError) as raised:
        service._require_confirmed_identity_for_model_question(
            request, NO_PK_TARGETS)

    assert raised.value.policy_reason == "COUPANG_MODEL_MAPPING_UNCONFIRMED"
    assert raised.value.policy_details["blocked_identity_actions"] == ["REPAIR"]
    assert raised.value.policy_details["blocked_mapping_resolution"] == (
        resolution or "NONE")


def test_a_repair_question_on_a_confirmed_mapping_is_answered(
    service, mapping
) -> None:
    """Confirmed means the device is identified, so the fault is answerable."""

    mapping(resolution=AUTO_EXACT, confirmed=True, canonical="S32DM501")
    request = request_for(
        service, question="채널 넘길때마다 검은화면이 뜨는데 어떻게 해결하나요?",
        actions=("REPAIR",))

    service._require_confirmed_identity_for_model_question(
        request, NO_PK_TARGETS)


def test_a_repair_question_still_has_its_model_facts_withheld(
    service, mapping
) -> None:
    """Both halves hold at once, and neither replaces the other.

    The withholding runs whatever the action is, so an inquiry blocked for
    REPAIR never had model-bound evidence to lose in the first place -- and
    one that is not blocked still loses it.
    """

    mapping(resolution=UNRESOLVED, evidence_class=NO_MODEL_EVIDENCE)
    question = "업데이트후 화면에 shaks 로고만 나오고 진행이 안되는데요"
    knowledge = ProductKnowledgeService(
        ProductCatalogRepository()
    ).facts_for_inquiry(
        product_id=None, question=question, questions=(question,),
        model_code=None, product_name=SHAKS_SETTOP, option_name=None,
        include_all_catalog_fields=True,
    )
    assert knowledge.has_safe_facts, "the brand inference still happens"

    request = request_for(service, question=question, actions=("REPAIR",),
                          product_name=SHAKS_SETTOP)
    withheld = service._withhold_unconfirmed_model_facts(request, knowledge)
    assert withheld.safe_facts == ()

    with pytest.raises(AutoAnswerProhibitedError):
        service._require_confirmed_identity_for_model_question(
            request, NO_PK_TARGETS)


def test_naver_repair_questions_are_untouched(service, mapping) -> None:
    """Naver has no market mapping, so REPAIR there is unaffected."""

    mapping(resolution=UNRESOLVED, evidence_class=NO_MODEL_EVIDENCE)
    request = SimpleNamespace(
        store_code="OJE_PLUS", product_name="삼성 32인치 모니터 S32DM501",
        question="검은화면이 뜨는데 어떻게 해결하나요?", metadata={})
    request.metadata["gpt_understanding"] = {
        "usable": True,
        "questions": [{"text": request.question, "action": "REPAIR"}],
    }

    service._require_confirmed_identity_for_model_question(
        request, NO_PK_TARGETS)


# --- 6. a confirmed mapping is unaffected --------------------------------

def test_a_confirmed_mapping_keeps_its_facts_and_its_answer(
    service, mapping
) -> None:
    mapping(resolution=AUTO_EXACT, confirmed=True, canonical="S32DM501")
    request = request_for(service, question="해상도가 어떻게 되나요?",
                          actions=("PRODUCT_SPEC",))
    assert request.metadata["model_identity_source"] == (
        "COUPANG_CONFIRMED_MAPPING")

    model_fact = SimpleNamespace(
        scope="EXACT_MODEL", field_key="resolution",
        exclusion_reason=None, safe_for_answer=True)
    knowledge = SimpleNamespace(
        safe_facts=(model_fact,), excluded_facts=(),
        without_model_bound_facts=lambda *_a, **_k: pytest.fail(
            "a confirmed mapping must not have its facts withheld"),
    )

    kept = service._withhold_unconfirmed_model_facts(request, knowledge)
    assert kept.safe_facts == (model_fact,)
    service._require_confirmed_identity_for_model_question(
        request, NO_PK_TARGETS)


def test_a_naver_inquiry_is_untouched_by_both(service, mapping) -> None:
    mapping(resolution=UNRESOLVED, evidence_class=NO_MODEL_EVIDENCE)
    request = SimpleNamespace(
        store_code="OJE_PLUS", product_name="삼성 32인치 모니터 S32DM501",
        question="해상도가 어떻게 되나요?", metadata={})
    request.metadata["gpt_understanding"] = {
        "usable": True,
        "questions": [{"text": request.question, "action": "PRODUCT_SPEC"}],
    }
    sentinel = SimpleNamespace(
        safe_facts=("kept",), excluded_facts=(),
        without_model_bound_facts=lambda *_a, **_k: pytest.fail(
            "Naver has no market mapping and must not be withheld"))

    assert service._withhold_unconfirmed_model_facts(
        request, sentinel) is sentinel
    service._require_confirmed_identity_for_model_question(
        request, NO_PK_TARGETS)


# --- 7-9. the boundaries Phase 2-A closed stay closed --------------------

def test_the_withholding_runs_before_the_refusal() -> None:
    """A blocked inquiry leaves no draft, so order decides what a kept one
    carries: facts are withheld whatever the question, then the question is
    asked whether it needed an identity.
    """

    import inspect

    source = inspect.getsource(AnswerService.generate_for_inquiry)
    withhold = source.index("_withhold_unconfirmed_model_facts")
    refuse = source.index("_require_confirmed_identity_for_model_question")
    assert withhold < refuse
    assert 'request.metadata["product_knowledge"] = product_knowledge' in (
        source[withhold:refuse])


def test_both_product_knowledge_lookups_are_withheld() -> None:
    """The fallback lookup rebuilds the record, guess included."""

    import inspect

    source = inspect.getsource(AnswerService)
    assert source.count("_withhold_unconfirmed_model_facts(") >= 3


def test_coupang_still_has_no_dps_path() -> None:
    """Registering is open now; order and schedule lookups are not.

    The identity gate above exists because an unconfirmed mapping must not
    answer a model question -- and that matters more, not less, once the
    answer can be registered without a person reading it.
    """

    assert is_store_manual_post_enabled(COUPANG_STORE) is True
    assert is_store_automatic_post_enabled(COUPANG_STORE) is True
    assert is_store_dps_enabled(COUPANG_STORE) is False
