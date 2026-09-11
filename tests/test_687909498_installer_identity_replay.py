"""Provider-free AnswerService replay: who installs this product?

Server inquiry 687909498 asked "삼성기사분이 설치하러 오시나요". The store's own
approved answer to that question -- "해당 상품은 삼성 기사님이 방문하여 설치하는
상품입니다." -- was retrieved at relevance 0.489 and reached the prompt, and the
customer was still told the installer's affiliation could not be confirmed.

Three separate lexical gates produced that, and each one asked a question about
*meaning* by comparing *wording*:

* ``answer_support_recall`` scored the question against the retrieved answer at
  0.000 -- "삼성기사분" and "삼성 기사님" share no token -- so the evidence ladder
  demoted the candidate to NO_RELIABLE_SOURCE and emptied ``historical_case_ids``
  while leaving the text in the prompt;
* ``_install_extra_paragraphs`` matched none of its four substrings, so the rule
  engine rendered nothing and no Template candidate was created at all;
* ``InquiryAnalysisService`` scored the inquiry UNCLASSIFIED and raised
  ``manual_review_required``, which forced review whatever GPT ② produced.

The number this file protects is not 687909498. It is the invariant that three
different ways of asking the same question reach the same evidence -- which is
what tells a semantic pipeline apart from a keyword one. So the identifier is
only the fixture's traceability label, and the phrasing variants below are the
actual assertion.
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from answer.engine import AnswerEngine
from answer.governance_models import GptProviderSettings
from answer.models import AnswerStatus
from answer.providers.fake_gpt_provider import FakeGptProvider
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from services.answer_service import AnswerService
from services.gpt_governance_service import GovernedHybridAnswerService
from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService
from services.dps_enrichment_service import DpsEnrichmentService
from services.historical_case_service import HistoricalCaseService


PRODUCT = (
    "삼성 4K UHD 스마트 사이니지 TV 1등급 티비 기사님 방문설치"
    " 107.9cm(43인치), 스탠드"
)
OPTION = "107.9cm(43인치), 스탠드"
INSTALLER_ANSWER = "해당 상품은 삼성 기사님이 방문하여 설치하는 상품입니다."
PAST_QUESTION = (
    "상품 문의\n설치 2시간만에 tv가 꺼지고 다시는 안 켜져요.\n"
    "삼성 기사님 오셔서 오전 11시에 설치했고, 2시간 시청했는데,"
    " 갑자기 tv가 꺼졌어요."
)
PAST_ANSWER = (
    f"{INSTALLER_ANSWER}\n"
    "제품 사용 중 고장이나 불량이 의심되는 경우 삼성전자 고객센터"
    " 1588-3366으로 문의해 A/S 접수해 주시면 됩니다."
)

# The three phrasings a customer might use for one question. None of them is a
# substring of the rule engine's installer keywords, and only the second shares
# tokens with the stored answer.
PHRASINGS = (
    "삼성기사분이 설치하러 오시나요",
    "삼성 기사님이 방문해서 설치해주시나요",
    "설치는 누가 해주시나요",
)


class _SemanticProvider:
    """GPT ①. Understands the installer question the same way each time.

    This is the point of the fixture: a semantic pass reads all three phrasings
    as one question about who performs the installation, so anything downstream
    that still keys on wording shows up as a difference between the variants.
    """

    name = "semantic_fixture"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_json(self, *, task, prompt, context):
        self.calls.append({"task": task, "prompt": prompt})
        return {
            "primary_action": "INSTALLATION_METHOD",
            "secondary_actions": [],
            "request_type": "QUESTION",
            "objects": [{"type": "TV", "states": ["NEW"]}],
            "atomic_questions": [{
                "text": "설치 주체 및 기사 방문 설치 여부",
                "action": "INSTALLATION_METHOD",
                "requested_information": "설치를 수행하는 주체",
                "requested_attribute": "ACTOR",
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
            "confidence": 0.95,
        }


class _DraftProvider(FakeGptProvider):
    """GPT ②. Reads the candidates it is given and reports what it used.

    It deliberately answers from whatever installer evidence actually arrived:
    the assertions below check that the candidates reached it, and this returns
    the contract the new pipeline reads back.
    """

    name = "draft_fixture"

    def __init__(self) -> None:
        super().__init__(responses={})
        self.draft_contexts: list[dict] = []

    def generate_json(self, *, task, prompt, context):
        if str(task).upper() != "DRAFT":
            return super().generate_json(task=task, prompt=prompt, context=context)
        self.calls.append({"task": task, "prompt": prompt, "context": context})
        self.draft_contexts.append(context)
        template_ids = [
            str(item.get("template_id"))
            for item in (context.get("template_candidates") or [])
            if INSTALLER_ANSWER in str(item.get("answer") or "")
        ]
        historical_ids = [
            int(item["historical_case_id"])
            for item in (context.get("historical_cases") or [])
            if INSTALLER_ANSWER in str(item.get("answer_reference") or "")
        ]
        return {
            "answer": INSTALLER_ANSWER,
            "confidence": 0.95,
            "used_facts": [],
            "missing_information": [],
            "requires_review": False,
            "warnings": [],
            "learning_usage": [],
            "historical_usage": [
                {
                    "historical_case_id": case_id,
                    "matched_subquestion": "설치 주체 및 기사 방문 설치 여부",
                    "answer_supported": True,
                    "reason": "동일 상품의 안정적 설치 방식 지식",
                }
                for case_id in historical_ids
            ],
            "subquestion_results": [{
                "subquestion": "설치 주체 및 기사 방문 설치 여부",
                "status": "ANSWERABLE",
                "learning_ids": [],
                "answered": True,
            }],
            "evidence_decisions": [
                {
                    "kind": "HISTORICAL",
                    "id": case_id,
                    "decision": "USED",
                    "matched_subquestion": "설치 주체 및 기사 방문 설치 여부",
                    "reason": "동일 상품, 시간 비의존 운영 지식",
                }
                for case_id in historical_ids
            ],
            "used_template_ids": template_ids,
            "used_product_facts": [],
            "used_learning_ids": [],
            "used_historical_ids": historical_ids,
            "ignored_evidence": [],
            "unresolved": [],
            "can_auto_post": True,
            "reason": "설치 주체를 확정 근거로 답변했습니다.",
        }


class _DpsSpy(DpsEnrichmentService):
    """The real enrichment service, with the outbound call made fatal.

    Subclassed rather than faked so the skip path returns the real outcome
    object the pipeline reads; a hand-built stand-in only has the attributes
    someone remembered to add.
    """

    def __init__(self, database) -> None:
        super().__init__(database)
        self.enrich_calls = 0
        self.skip_calls = 0

    def skip_for_phase9(self, request, **kwargs):
        self.skip_calls += 1
        return super().skip_for_phase9(request, **kwargs)

    def enrich(self, request, **_kwargs):
        self.enrich_calls += 1
        raise AssertionError("DPS must not run for an installation-method question")


class _OrderSpy:
    def __init__(self) -> None:
        self.calls = 0

    def lookup_for_inquiry(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("Order lookup must not run for a product question")


def _seed_historical(database: Database, *, product_name: str = PRODUCT) -> int:
    service = HistoricalCaseService(database)
    case = service.prepare_case({
        "store_code": "OJE_PLUS",
        "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": f"installer-{product_name[:12]}",
        "title": "상품 문의",
        "content": PAST_QUESTION,
        "product_name": product_name,
        "seller_answer": PAST_ANSWER,
        "answered": True,
        "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="FIXTURE:installer")
    row, _ = service.repository.upsert(case)
    return int(row["id"])


def _run(tmp_path, monkeypatch, question: str, *, name: str):
    """One full AnswerService generation. Only the two providers are stubs."""

    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    historical_id = _seed_historical(database)

    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "TEST",
        "source_question_id": f"687909498-{name}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": question,
        "product_id": "9645661432",
        "product_name": PRODUCT,
        "option_name": OPTION,
        "raw_json": {},
    }).inquiry_id

    semantic_provider = _SemanticProvider()
    draft_provider = _DraftProvider()
    governed = GovernedHybridAnswerService(
        database,
        provider=draft_provider,
        settings=GptProviderSettings(provider_name="fake"),
    )
    dps, order = _DpsSpy(database), _OrderSpy()
    captured: dict = {}

    import services.answer_service as answer_module
    import services.gpt_governance_service as governance_module

    build = governance_module.LearningContextService.build

    def build_spy(self, *args, **kwargs):
        context = build(self, *args, **kwargs)
        captured["learning_context"] = context
        return context

    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(
        governance_module.LearningContextService, "build", build_spy,
    )
    monkeypatch.setattr(
        answer_module, "notify_qna_safely", lambda **_kwargs: False,
    )

    outcome = AnswerService(
        database,
        engine=AnswerEngine(),
        hybrid_service=governed,
        dps_enrichment=dps,
        order_lookup_service=order,
        semantic_analyzer=GptSemanticAnalyzerService(semantic_provider),
    ).generate_for_inquiry(inquiry_id)

    draft_calls = [
        call for call in draft_provider.calls if call["task"] == "DRAFT"
    ]
    assert draft_calls, "GPT ② was never called"
    return SimpleNamespace(
        database=database,
        inquiry_id=inquiry_id,
        historical_id=historical_id,
        outcome=outcome,
        prompt=draft_calls[-1]["prompt"],
        context=draft_calls[-1]["context"],
        learning_context=captured.get("learning_context") or {},
        dps=dps,
        order=order,
        semantic_calls=semantic_provider.calls,
    )


def test_687909498_installer_identity_full_replay(tmp_path, monkeypatch):
    run = _run(tmp_path, monkeypatch, PHRASINGS[0], name="primary")
    context, prompt = run.context, run.prompt

    # --- retrieval reached GPT ② -------------------------------------------
    template_answers = [
        str(item.get("answer") or "")
        for item in (context.get("template_candidates") or [])
    ]
    assert any(INSTALLER_ANSWER in item for item in template_answers), {
        "template_candidates": template_answers,
    }
    historical = context.get("historical_cases") or []
    assert any(
        INSTALLER_ANSWER in str(item.get("answer_reference") or "")
        for item in historical
    ), {"historical_cases": historical}

    # --- the ids were not emptied by a lexical score -----------------------
    evidence = run.learning_context.get("subquestion_evidence") or []
    assert evidence
    attached_ids = {
        int(case_id)
        for item in evidence
        for case_id in (item.get("historical_case_ids") or [])
    }
    assert run.historical_id in attached_ids, {"subquestion_evidence": evidence}
    assert all(
        str(item.get("status")) != "NO_RELIABLE_SOURCE" for item in evidence
    ), {"subquestion_evidence": evidence}

    # --- no binding instruction survives in the prompt ---------------------
    assert "subquestion_evidence_is_binding" not in prompt
    assert "Only this item may request confirmation" not in prompt
    # This old duplicate marker was removed: it repeated the current GPT②
    # handoff below as a caution rather than granting GPT② the authority to
    # judge relevance and applicability itself.
    assert "retrieval_candidates_are_not_approved_evidence" not in prompt
    assert "relevance_and_answer_support_are_hints_not_permission" in prompt
    assert "you_decide_which_candidates_apply" in prompt

    # --- GPT ① understanding is what GPT ② was asked about -----------------
    assert len(run.semantic_calls) == 1
    intent_questions = [
        str(item.get("subquestion") or "") for item in evidence
    ]
    assert intent_questions == ["설치 주체 및 기사 방문 설치 여부"]

    # --- external actions --------------------------------------------------
    assert run.order.calls == 0
    assert run.dps.enrich_calls == 0

    # --- the answer, and the provenance behind it --------------------------
    result = run.outcome.result
    assert INSTALLER_ANSWER in result.answer
    hybrid = result.metadata.get("hybrid") or {}
    draft = hybrid.get("draft") or {}
    assert draft.get("used_historical_ids") == [run.historical_id]
    assert draft.get("used_template_ids")
    assert draft.get("unresolved") == []
    assert draft.get("can_auto_post") is True
    assert result.status is AnswerStatus.GENERATED
    assert result.needs_review is False

    # The keyword classifier still reads this inquiry exactly as it did -- no
    # keyword was added anywhere -- and it still asks for a person. What
    # changed is that its verdict no longer decides the answer's status.
    from answer.source_adapter import answer_request_from_inquiry
    from services.inquiry_analysis_service import InquiryAnalysisService

    inquiry_row = InquiryRepository(run.database).get(run.inquiry_id)
    legacy = InquiryAnalysisService().analyze(
        answer_request_from_inquiry(inquiry_row)
    )
    assert legacy.manual_review_required is True
    assert str(legacy.inquiry_subtype).upper() == "UNCLASSIFIED"

    # --- dashboard provenance reflects GPT ②'s own choice ------------------
    with run.database.connection() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT reference_kind, historical_case_id, usage_status"
            " FROM answer_learning_provenance WHERE included_in_prompt=1"
        ).fetchall()]
    used = [row for row in rows if row["usage_status"] == "USED"]
    assert used, {"provenance": rows}
    assert {row["historical_case_id"] for row in used} == {run.historical_id}


@pytest.mark.parametrize(
    "index,question", list(enumerate(PHRASINGS)), ids=["A", "B", "C"],
)
def test_every_phrasing_of_one_question_reaches_the_same_evidence(
    tmp_path, monkeypatch, index, question,
):
    """No keyword was added for any of these; they must not diverge.

    Variant A is the wording the server actually received and the one every
    lexical gate missed. B shares tokens with the stored answer and used to be
    the only one that worked. C names neither the brand nor the word 기사.
    """

    run = _run(tmp_path, monkeypatch, question, name=f"variant{index}")
    context = run.context

    assert any(
        INSTALLER_ANSWER in str(item.get("answer") or "")
        for item in (context.get("template_candidates") or [])
    ), {"question": question}
    assert any(
        INSTALLER_ANSWER in str(item.get("answer_reference") or "")
        for item in (context.get("historical_cases") or [])
    ), {"question": question}

    result = run.outcome.result
    assert INSTALLER_ANSWER in result.answer
    assert result.needs_review is False
    assert result.status is AnswerStatus.GENERATED
