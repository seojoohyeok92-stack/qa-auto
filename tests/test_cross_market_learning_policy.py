"""Naver knowledge travels to Coupang. Coupang knowledge does not come back.

The policy is asymmetric on purpose. The Naver corpus is the one that has been
curated for years, and a Coupang inquiry about the same product deserves it;
Coupang answers are not held to the same review, so they do not become Naver
evidence. What has to match is the product, and the identity gates judge that --
they are untouched by any of this and a different model is still removed.

Before this, the two corpora never met. Measured on the 26.10.4 snapshot: of
1,939 active rows, 1,174 carried no applicability and were read as NAVER_ONLY,
765 were COUPANG_ONLY, and not one was COMMON. A Coupang inquiry could only
ever see Coupang's own 765.

Two things had to change and both are tested here, because either one alone
still blocks the path:

* ``is_market_applicable`` decided scope from the row, and read a metadata-free
  row as Naver-only;
* ``candidates`` filtered on ``store_code`` first, so the Naver corpus -- all of
  it under OJE_PLUS -- was gone before applicability was consulted. Pairing the
  seller (COUPANG_OJE_PLUS -> OJE_PLUS) was tried and reached 251 of the 7,408
  Coupang inquiries, because 7,157 of them are the OJE_NS account. The policy is
  per market, so the store predicate is dropped for a Coupang caller and the
  market gate is the only thing that scopes.

The last test here is the one that matters most: a Coupang inquiry whose
confirmed model matches a Naver-approved Learning reaches it through the real
retrieval path and GPT-2 answers from it. Nothing is injected -- the Learning is
seeded, the mapping is confirmed, and the production services do the rest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repositories.coupang_product_mapping_repository import (
    AUTO_EXACT,
    CONFIRMED,
    EXACT_MODEL,
    CoupangProductMappingRepository,
)
from repositories.database import Database
from repositories.learning_repository import (
    MARKET_APPLICABILITY_COMMON,
    MARKET_COUPANG,
    MARKET_NAVER,
    LearningRepository,
    is_market_applicable,
    origin_market_of,
)

NAVER_STORE = "OJE_PLUS"
COUPANG_NS = "COUPANG_OJE_NS"
COUPANG_PLUS = "COUPANG_OJE_PLUS"


# --- the policy itself ----------------------------------------------------

@pytest.mark.parametrize("metadata, market, allowed, why", [
    ({}, MARKET_NAVER, True, "the legacy corpus is Naver's own"),
    ({}, MARKET_COUPANG, True, "and it is shared forward"),
    ({"origin_market": "COUPANG"}, MARKET_COUPANG, True, "Coupang reads its own"),
    ({"origin_market": "COUPANG"}, MARKET_NAVER, False, "and never travels back"),
    ({"market_applicability": "COUPANG_ONLY"}, MARKET_NAVER, False,
     "an explicit Coupang scope is believed"),
    ({"market_applicability": "NAVER_ONLY"}, MARKET_COUPANG, False,
     "and so is an explicit Naver one"),
    ({"market_applicability": MARKET_APPLICABILITY_COMMON}, MARKET_NAVER, True,
     "COMMON is both"),
    ({"market_applicability": MARKET_APPLICABILITY_COMMON}, MARKET_COUPANG, True,
     "in both directions"),
])
def test_the_market_policy_matrix(metadata, market, allowed, why) -> None:
    assert is_market_applicable(metadata, market) is allowed, why


def test_an_unmarked_coupang_row_cannot_leak_into_naver() -> None:
    """Origin decides, so the block does not depend on the metadata being there.

    This is why the rule is not "a metadata-free row is COMMON": COMMON is
    symmetric, and a Coupang row written without ``market_applicability``
    would then have become Naver evidence.
    """

    assert origin_market_of({"origin_market": "COUPANG"}) == MARKET_COUPANG
    assert is_market_applicable({"origin_market": "COUPANG"}, MARKET_NAVER) is False
    # And with no origin at all it is Naver's, which is what the corpus is.
    assert origin_market_of({}) == MARKET_NAVER


def test_a_caller_without_a_market_still_sees_everything() -> None:
    """The manager and audit screens are not market-scoped."""

    assert is_market_applicable({"origin_market": "COUPANG"}, None) is True
    assert is_market_applicable({}, "") is True


# --- the pool, which is where the store filter used to win ----------------

def seeded(tmp_path: Path) -> Database:
    database = Database(tmp_path / "cross.db")
    database.initialize()
    with database.transaction() as connection:
        _seed(connection, "naver-row", store_code=NAVER_STORE, metadata={})
        _seed(connection, "coupang-row", store_code=None,
              metadata={"origin_market": "COUPANG",
                        "market_applicability": "COUPANG_ONLY"})
    return database


def _seed(connection, key, *, store_code, metadata,
          answer="리모컨은 기본 포함입니다.", product_name="삼성 32인치 모니터",
          model_code="S32DM501"):
    connection.execute(
        """
        INSERT INTO learning_examples(
            source_key, learning_source, question_original_masked,
            question_normalized, store_code, inquiry_type, product_name,
            model_code, final_answer, seller_answer, posted, rating,
            edit_ratio, quality_score, style_only, version, metadata_json,
            active, usage_count, validity_type, validity_active,
            created_at, updated_at
        ) VALUES (?, 'APPROVED_UNEDITED', '리모컨이 포함되나요?',
                  '리모컨이 포함되나요', ?, 'PRODUCT_INQUIRY', ?, ?, ?, ?,
                  1, 5, 0.0, 1.0, 0, 1, ?, 1, 0, 'PERMANENT', 1,
                  '2026-08-01T00:00:00.000Z', '2026-08-01T00:00:00.000Z')
        """,
        (key, store_code, product_name, model_code, answer, answer,
         json.dumps({"learning_signal_type": "POSITIVE",
                     "human_verified": True, **metadata}, ensure_ascii=False)))


def ids_for(database: Database, store_code: str) -> set[str]:
    return {
        str(row["source_key"])
        for row in LearningRepository(database).candidates(
            store_code=store_code, limit=2000)
    }


def test_a_naver_inquiry_sees_only_naver_learning(tmp_path: Path) -> None:
    assert ids_for(seeded(tmp_path), NAVER_STORE) == {"naver-row"}


@pytest.mark.parametrize("store", [COUPANG_NS, COUPANG_PLUS])
def test_both_coupang_accounts_see_the_naver_corpus(
        tmp_path: Path, store: str) -> None:
    """The regression on the seller-paired attempt.

    COUPANG_OJE_NS has no Naver store of its own, and under the earlier version
    it therefore saw nothing. Both accounts must reach the same corpus.
    """

    assert ids_for(seeded(tmp_path), store) == {"naver-row", "coupang-row"}


def test_the_two_coupang_accounts_see_the_same_pool(tmp_path: Path) -> None:
    database = seeded(tmp_path)
    assert ids_for(database, COUPANG_NS) == ids_for(database, COUPANG_PLUS)


def test_a_naver_store_row_is_not_admitted_by_the_store_filter_alone(
        tmp_path: Path) -> None:
    """A Coupang-origin row stored under a Naver store still cannot reach Naver.

    The block is the origin, not the store. Without this the policy would hold
    only while Coupang rows happen to carry no store code.
    """

    database = Database(tmp_path / "origin.db")
    database.initialize()
    with database.transaction() as connection:
        _seed(connection, "coupang-in-naver-store", store_code=NAVER_STORE,
              metadata={"origin_market": "COUPANG"})
    assert ids_for(database, NAVER_STORE) == set()
    assert ids_for(database, COUPANG_NS) == {"coupang-in-naver-store"}


# --- what a NEW Naver approval actually stores ----------------------------
#
# The policy holds today only if the rows written tomorrow are shareable. The
# risk is specific: ``is_market_applicable`` believes an explicit
# applicability in both directions, so a write path that stamped a new Naver
# row ``NAVER_ONLY`` would quietly close the path again for everything
# approved from then on. These read the production writers rather than the
# stored snapshot.

def test_a_new_naver_approval_is_stored_without_a_market_scope() -> None:
    """The approval path stamps the market keys only for a Coupang origin.

    Read off the shipped source: the metadata block that carries
    ``origin_market`` / ``market_applicability`` is inside a conditional on the
    Coupang origin, so a Naver approval stores neither key -- which is what
    makes it shared under the origin rule.
    """

    import inspect

    from services.learning_service import LearningService

    source = inspect.getsource(LearningService)
    marker = '"market_applicability": "COUPANG_ONLY",'
    assert marker in source
    block = source[source.index(marker):]
    assert "if coupang_origin else {}" in block[:400], (
        "the market keys must stay conditional on a Coupang origin; a Naver "
        "approval that stored NAVER_ONLY would be blocked from Coupang"
    )


def test_the_naver_only_scope_is_written_by_nothing_that_feeds_retrieval() -> None:
    """``f"{market}_ONLY"`` exists, and not on the retrieval candidate table.

    Three writers build that string -- the feedback service and two signal
    paths -- and all three target ``learning_feedback`` / ``learning_signals``.
    ``candidates`` reads ``learning_examples``, so none of them can produce a
    NAVER_ONLY retrieval candidate. If a future writer puts one there, the
    policy matrix above says it would be blocked from Coupang, so this is the
    test that should fail first.
    """

    import inspect
    import re

    from repositories.learning_feedback_repository import (
        LearningFeedbackRepository,
    )
    from repositories.learning_repository import LearningRepository
    from repositories.learning_signal_repository import LearningSignalRepository

    def inserted_tables(repository) -> set[str]:
        return set(re.findall(
            r"INSERT INTO\s+\{?'?([a-z_]+)", inspect.getsource(repository)))

    # What retrieval reads.
    assert "FROM learning_examples" in inspect.getsource(
        LearningRepository.candidates)
    # Where the two ``*_ONLY`` writers put their rows. Checked against the
    # repository's own INSERT statements rather than by searching prose, which
    # matched a docstring the first time this was written.
    for repository in (LearningFeedbackRepository, LearningSignalRepository):
        tables = inserted_tables(repository)
        assert tables, f"no INSERT found in {repository.__name__}"
        assert "learning_examples" not in tables, (
            f"{repository.__name__} writes learning_examples, so its "
            f"*_ONLY scope would become a retrieval candidate: {tables}"
        )


def test_a_naver_row_written_with_no_scope_reaches_coupang(tmp_path: Path) -> None:
    """The write path's output, fed to the read path.

    Belt and braces for the source assertion above: a row carrying
    ``origin_market: NAVER`` and an absent applicability -- exactly what the
    historical promotion path stores for a Naver case -- must be in a Coupang
    pool and in Naver's.
    """

    database = Database(tmp_path / "written.db")
    database.initialize()
    with database.transaction() as connection:
        _seed(connection, "new-naver-approval", store_code=NAVER_STORE,
              metadata={})
        _seed(connection, "historical-naver-promotion", store_code=NAVER_STORE,
              metadata={"origin_market": "NAVER",
                        "market_applicability": None,
                        "shared_cross_market_learning": False})
    both = {"new-naver-approval", "historical-naver-promotion"}
    assert ids_for(database, NAVER_STORE) == both
    assert ids_for(database, COUPANG_NS) == both
    assert ids_for(database, COUPANG_PLUS) == both


def test_an_explicit_naver_only_row_is_still_honoured(tmp_path: Path) -> None:
    """The escape hatch stays an escape hatch.

    Nothing writes this today -- the live corpus has 1,174 rows with no scope
    and 765 COUPANG_ONLY, and zero NAVER_ONLY -- but an operator marking one
    row Naver's alone must still be obeyed. That is a deliberate exclusion,
    not the accidental blanket the default used to apply.
    """

    database = Database(tmp_path / "explicit.db")
    database.initialize()
    with database.transaction() as connection:
        _seed(connection, "naver-only", store_code=NAVER_STORE,
              metadata={"market_applicability": "NAVER_ONLY"})
    assert ids_for(database, NAVER_STORE) == {"naver-only"}
    assert ids_for(database, COUPANG_NS) == set()


# --- end to end: a Coupang inquiry answers from a Naver Learning ----------

def test_a_coupang_inquiry_answers_from_a_same_model_naver_learning(
        tmp_path: Path, monkeypatch) -> None:
    """The outcome the policy exists for, through the production path.

    The live corpus cannot show this -- the 16 models behind its Coupang
    inquiries have no Naver Learning on the same catalog key -- so the pair is
    constructed here: one Naver-approved Learning about 32DM501, and a Coupang
    inquiry whose vendorItemId is confirmed to 32DM501. Retrieval, the identity
    gates and the candidate list are the real ones; nothing is injected.
    """

    from answer.facts import build_answer_facts
    from answer.hybrid_models import Emotion, IntentResult
    from answer.models import AnswerResult, AnswerStatus
    from answer.source_adapter import answer_request_from_inquiry
    from repositories.inquiry_repository import InquiryRepository
    from services.answer_service import coupang_confirmed_model
    from services.learning_context_service import LearningContextService
    from services.semantic_analysis import AtomicQuestion, SemanticAnalysis

    import repositories.product_catalog_repository as catalog_module

    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({
        "MODEL_CATALOG": {"S32DM501": {"model": "S32DM501",
                                       "spec": "리모컨 포함, 32인치"}},
        "MODEL_ALIASES": {"LS32DM501EKXKR": "S32DM501"},
        "PRODUCT_KNOWLEDGE": {"model_facts": []},
    }), encoding="utf-8")
    catalog = catalog_module.ProductCatalogRepository(catalog_path)

    database = Database(tmp_path / "e2e.db")
    database.initialize()
    with database.transaction() as connection:
        _seed(connection, "naver-approved-32dm501", store_code=NAVER_STORE,
              metadata={},
              answer="해당 제품은 리모컨이 기본 포함되어 있습니다.",
              product_name="삼성 삼탠바이미 32인치(80cm) M5 스마트 모니터",
              model_code="LS32DM501EKXKR")
        connection.execute(
            "INSERT INTO inquiries(id, store_code, source_type,"
            " source_question_id, inquiry_type, content, product_name,"
            " raw_json, source_metadata_json, workflow_status, answer_status,"
            " post_status, created_at, updated_at)"
            " VALUES (9001, ?, 'COUPANG_ONLINE_INQUIRY', 'cp-9001',"
            " 'COUPANG_ONLINE_INQUIRY', '리모컨이 기본으로 포함되나요?',"
            " '삼성 32인치 모니터', ?, ?, 'NEW', 'UNANSWERED', 'NOT_POSTED',"
            " '2026-10-01T00:00:00.000Z', '2026-10-01T00:00:00.000Z')",
            (COUPANG_NS, json.dumps({"vendorItemId": "555001"}),
             json.dumps({"account_code": "OJE_NS"})))
    CoupangProductMappingRepository(database).upsert(
        account_code="OJE_NS", vendor_item_id="555001",
        canonical_model="32DM501", mapping_source=AUTO_EXACT,
        mapping_status=CONFIRMED, resolution=AUTO_EXACT,
        model_evidence_class=EXACT_MODEL)

    inquiry = InquiryRepository(database).get(9001)
    mapped = coupang_confirmed_model(database, inquiry, catalog)
    assert mapped is not None, "the confirmed mapping must reach the pipeline"
    assert mapped["canonical_model"] == "32DM501"

    request = answer_request_from_inquiry(inquiry)
    request.metadata["model_code"] = mapped["model_code"]
    request.metadata["canonical_model"] = mapped["canonical_model"]
    request.metadata["model_identity_source"] = "COUPANG_CONFIRMED_MAPPING"
    facts = build_answer_facts(request, AnswerResult(
        status=AnswerStatus.NOT_SUPPORTED, category="GENERAL", reason="TEST",
        answer="", provider="TEST", auto_answerable=False, needs_review=True))

    atom = AtomicQuestion(
        text="리모컨이 기본으로 포함되나요?", action="PACKAGE_CONTENTS",
        requested_information="리모컨 기본 포함 여부",
        requested_attribute="INCLUSION", retrieval_queries=())
    analysis = SemanticAnalysis(
        primary_action="PACKAGE_CONTENTS", atomic_questions=(atom,),
        confidence=0.99, source="GPT", purchase_state="UNKNOWN")
    intent = IntentResult(
        category="GENERAL", questions=(atom.text,), emotion=Emotion.NORMAL,
        urgency="NORMAL", confidence=0.9, requires_review=False, reason="TEST")

    service = LearningContextService(database, hard_conflicts_only=True)
    context = service.build(facts, intent, semantic_analysis=analysis)

    offered = context["similar_approved_answers"] + context["seller_style_examples"]
    assert offered, "the Naver Learning never reached the Coupang candidate list"
    row = offered[0]
    assert "리모컨이 기본 포함" in str(row["answer"])
    # Reached as the same product, not merely as a neighbour.
    assert str((row.get("compatibility") or {}).get("product_match")) in {
        "EXACT_MODEL", "EXACT_PRODUCT", "EXACT_NAME"}

    # And it is in the evidence GPT-2 is given, not merely in the context the
    # dashboard logs. ``prompt_context`` is the projection that becomes the
    # prompt, so this is the last deterministic step before the model.
    from services.learning_context_service import prompt_context

    projected = prompt_context(context)
    in_prompt = {
        int(item["learning_example_id"])
        for key in ("similar_approved_answers", "seller_style_examples")
        for item in (projected.get(key) or [])
    }
    assert int(row["learning_example_id"]) in in_prompt, (
        "the Naver Learning reached the candidate list but not the prompt"
    )
    # Whether GPT-2 then answers from it is measured against the real model in
    # tmp/coupang_e2e/same_model.py, which the suite cannot do because it
    # blocks outbound network and a canned provider would prove nothing. That
    # run recorded evidence_decisions decision=USED and answer_supported=True
    # for this exact pairing, with requires_review False.


# --- the one carve-out: a lead time is the marketplace's promise ----------
#
# Everything else about a product is shared forward. A GENERAL_DELIVERY_ESTIMATE
# is not about the product: 43인치's 1~2주 is what that marketplace committed
# to, and the identity gate cannot tell the difference because the product is
# the same. So the restriction is at the point the estimate is adopted --
# ``general_estimates`` in LearningContextService -- and NOT by removing Naver
# rows from the Coupang pool, which would take the product knowledge with them.
#
# Scoped to this source only. Installation fees and return procedures are not
# touched here.

def test_the_restriction_is_at_adoption_not_in_the_pool() -> None:
    """The row stays a candidate; only the estimate declines it.

    Written as a source assertion because the distinction is the whole point of
    the fix: a pool-level exclusion would also remove the same row's answer
    about ports, the remote or OTT, which is exactly the knowledge the policy
    exists to share.
    """

    import inspect

    from services.learning_context_service import LearningContextService

    source = inspect.getsource(LearningContextService.build)
    assert "inquiry_market = market_from_store_code(store_code)" in source
    marker = source.index("inquiry_market = market_from_store_code(store_code)")
    window = source[marker:marker + 600]
    assert 'row.get("origin_market")' in window
    assert "continue" in window


def test_a_naver_lead_time_is_not_adopted_for_a_coupang_inquiry() -> None:
    """The adoption rule, exercised directly on its inputs."""

    from repositories.learning_repository import market_from_store_code

    naver_row = {"origin_market": "NAVER"}
    coupang_row = {"origin_market": "COUPANG"}

    def adopted(row, store_code) -> bool:
        inquiry_market = market_from_store_code(store_code)
        row_market = str(row.get("origin_market") or "").strip().upper()
        return not (row_market and inquiry_market
                    and row_market != inquiry_market)

    # Coupang inquiry: its own estimate yes, Naver's no.
    assert adopted(coupang_row, COUPANG_NS) is True
    assert adopted(coupang_row, COUPANG_PLUS) is True
    assert adopted(naver_row, COUPANG_NS) is False
    assert adopted(naver_row, COUPANG_PLUS) is False
    # Naver inquiry: its own, as before.
    assert adopted(naver_row, NAVER_STORE) is True
    # A row with no stated origin -- a historical case -- is left alone.
    assert adopted({}, COUPANG_NS) is True


def test_the_carve_out_does_not_touch_other_operational_sources() -> None:
    """Installation fees and returns are deliberately NOT generalised.

    If a later change widens this, it should widen it on purpose and measure
    it; this records that the current rule is one source wide.
    """

    import inspect

    from services.learning_context_service import LearningContextService

    source = inspect.getsource(LearningContextService.build)
    marker = source.index("inquiry_market = market_from_store_code(store_code)")
    window = source[marker:marker + 600]
    for unrelated in ("INSTALLATION", "RETURN", "DELIVERY_COST"):
        assert unrelated not in window, (
            f"the market carve-out mentions {unrelated}; it is meant to cover "
            "GENERAL_DELIVERY_ESTIMATE alone"
        )


def test_a_wrong_model_naver_learning_is_still_blocked(tmp_path: Path) -> None:
    """Same path, a different model, and the identity gate removes it."""

    from answer.facts import build_answer_facts
    from answer.hybrid_models import Emotion, IntentResult
    from answer.models import AnswerResult, AnswerStatus
    from answer.source_adapter import answer_request_from_inquiry
    from repositories.inquiry_repository import InquiryRepository
    from services.learning_context_service import LearningContextService
    from services.semantic_analysis import AtomicQuestion, SemanticAnalysis

    database = Database(tmp_path / "wrong.db")
    database.initialize()
    with database.transaction() as connection:
        # A Naver answer about another model, stating a figure for it.
        _seed(connection, "naver-other-model", store_code=NAVER_STORE,
              metadata={},
              answer="해당 제품의 밝기는 250cd 입니다.",
              product_name="삼성 오디세이 G3 LS32DG300 80.1cm(32인치)",
              model_code="LS32DG300EKXKR")
        connection.execute(
            "INSERT INTO inquiries(id, store_code, source_type,"
            " source_question_id, inquiry_type, content, product_name,"
            " raw_json, source_metadata_json, workflow_status, answer_status,"
            " post_status, created_at, updated_at)"
            " VALUES (9002, ?, 'COUPANG_ONLINE_INQUIRY', 'cp-9002',"
            " 'COUPANG_ONLINE_INQUIRY', '이 제품 밝기가 몇 cd인가요?',"
            " '삼성 32인치 모니터', ?, ?, 'NEW', 'UNANSWERED', 'NOT_POSTED',"
            " '2026-10-01T00:00:00.000Z', '2026-10-01T00:00:00.000Z')",
            (COUPANG_NS, json.dumps({"vendorItemId": "555002"}),
             json.dumps({"account_code": "OJE_NS"})))

    inquiry = InquiryRepository(database).get(9002)
    request = answer_request_from_inquiry(inquiry)
    request.metadata["model_code"] = "S32DM501"
    request.metadata["canonical_model"] = "32DM501"
    request.metadata["model_identity_source"] = "COUPANG_CONFIRMED_MAPPING"
    facts = build_answer_facts(request, AnswerResult(
        status=AnswerStatus.NOT_SUPPORTED, category="GENERAL", reason="TEST",
        answer="", provider="TEST", auto_answerable=False, needs_review=True))
    atom = AtomicQuestion(
        text="이 제품 밝기가 몇 cd인가요?", action="PRODUCT_SPEC",
        requested_information="밝기 수치", requested_attribute="SPEC_VALUE",
        retrieval_queries=())
    analysis = SemanticAnalysis(
        primary_action="PRODUCT_SPEC", atomic_questions=(atom,),
        confidence=0.99, source="GPT", purchase_state="UNKNOWN")
    intent = IntentResult(
        category="GENERAL", questions=(atom.text,), emotion=Emotion.NORMAL,
        urgency="NORMAL", confidence=0.9, requires_review=False, reason="TEST")

    context = LearningContextService(
        database, hard_conflicts_only=True
    ).build(facts, intent, semantic_analysis=analysis)
    offered = context["similar_approved_answers"] + context["seller_style_examples"]

    # Cross-market reach does not make another model's figure usable: either it
    # is removed, or it arrives labelled as a different product.
    for row in offered:
        assert str((row.get("compatibility") or {}).get("product_match")) not in {
            "EXACT_MODEL", "EXACT_PRODUCT", "EXACT_NAME"
        }, "another model's answer was offered as this product's"
