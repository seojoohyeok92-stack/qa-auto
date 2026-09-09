"""Run one Golden case through the real production decision chain.

Safety, enforced rather than documented:

* the production copy is opened ``mode=ro`` and duplicated with SQLite's backup
  API; every write the pipeline performs lands on the throwaway copy;
* order lookup, DPS and the Naver client are recorders that raise on use, so a
  replay that would have touched the outside world fails loudly instead of
  quietly succeeding;
* the semantic index is read from the same production copy, so retrieval sees
  what the server saw.

What is *not* substituted is the part being measured: ``AnswerService``,
``InquiryAnalysisService``, the processing plan, Template/Rule, the whole
Learning/Historical/Product retrieval stack, the prompt builder, the validator
and the auto-post eligibility gate all run exactly as they do in production.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from golden.providers import (
    BlockedOrderLookup,
    CapturingProvider,
    NaverPostRecorder,
    PolicyDraftProvider,
    ReplaySemanticProvider,
    blocked_dps,
)
from golden.schema import CaseObservation, CaseResult, GoldenCase


def copy_database(source: Path, destination: Path) -> None:
    """Back up the production copy without ever opening it for writing."""

    read_only = sqlite3.connect(
        f"file:{Path(source).as_posix()}?mode=ro", uri=True
    )
    try:
        target = sqlite3.connect(str(destination))
        try:
            read_only.backup(target)
        finally:
            target.close()
    finally:
        read_only.close()


def reset_inquiry(connection: sqlite3.Connection, inquiry_id: int) -> None:
    """Return one row on the *copy* to "collected, not yet answered".

    Without this the pipeline reuses or protects the draft the production run
    already wrote, and the replay measures storage rather than behaviour.
    """

    for table in ("answer_drafts", "workflow_steps", "answer_versions",
                  "answer_learning_provenance",
                  "answer_feedback_signal_provenance", "gpt_provider_runs",
                  "naver_post_attempts", "post_reviews"):
        connection.execute(
            f"DELETE FROM {table} WHERE inquiry_id=?", (inquiry_id,)
        )
    connection.execute(
        """
        UPDATE inquiries
           SET workflow_status='NEW', answer_status='UNANSWERED',
               post_status='NOT_POSTED', approval_status='PENDING',
               source_answered=0, posted_at=NULL, post_error_code=NULL
         WHERE id=?
        """,
        (inquiry_id,),
    )
    connection.commit()


class GoldenReplayWorkspace:
    """One throwaway database copy, shared by every case in a run."""

    def __init__(self, source: Path, workspace: Path) -> None:
        self.source = Path(source)
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "golden_replay.db"
        copy_database(self.source, self.db_path)
        self.index_path = self.source.parent / "learning_semantic_index.json"

    def install_semantic_index(self) -> bool:
        """Point retrieval at the index that shipped with this data copy.

        ``load_cached`` binds the default path at definition time, so replacing
        the module constant is not enough.
        """

        from services.learning_semantic_index import LearningSemanticIndex

        if not self.index_path.exists():
            return False
        original = LearningSemanticIndex.load_cached
        if getattr(original, "_golden_patched", False):
            return True
        path = self.index_path

        def _load(cls, _path=path):
            return original.__func__(cls, _path)

        _load._golden_patched = True  # type: ignore[attr-defined]
        LearningSemanticIndex.load_cached = classmethod(_load)  # type: ignore[assignment]
        return True


def _prompt_of(provider: Any) -> dict[str, Any]:
    drafts = getattr(provider, "drafts", [])
    if not drafts:
        return {}
    try:
        return json.loads(drafts[-1]["prompt"])
    except (TypeError, ValueError, KeyError):
        return {}


def _ids(values: Any, key: str) -> list[int]:
    out: list[int] = []
    for item in values or []:
        if isinstance(item, dict) and item.get(key) is not None:
            try:
                out.append(int(item[key]))
            except (TypeError, ValueError):
                continue
    return out


def _check_fidelity(
    observed: CaseObservation, reference: dict[str, Any]
) -> None:
    """Did the replay reproduce the production run it claims to stand for?

    GPT ① is rebuilt from a compacted contract, so this is not a formality. A
    case that cannot reproduce its own stored retrieval numbers is not evidence
    about anything and is reported as DRIFT instead of being scored.
    """

    if not reference:
        observed.fidelity = "NO_REFERENCE"
        return

    # Two different things can differ, and only one of them invalidates a case.
    #
    # CONTRACT drift means the rebuilt GPT ① is not the understanding the
    # production run had, so the case is not evidence about anything.
    #
    # CORPUS drift means the understanding matches and the Learning corpus has
    # simply grown since -- measured here as 880 active rows at run time versus
    # 899 in the export. The case is still perfectly usable for before/after
    # comparison, because both sides of that comparison read today's corpus.
    contract: list[str] = []
    corpus: list[str] = []
    for field in ("need_template", "need_product", "need_learning",
                  "need_order", "need_dps", "product_identity_status"):
        expected = reference.get(field)
        actual = getattr(observed, field)
        if expected is not None and actual is not None and expected != actual:
            contract.append(f"{field}: stored={expected} replay={actual}")
    for field in ("learning_pool", "learning_hard_valid"):
        expected = reference.get(field)
        actual = getattr(observed, field)
        if expected is not None and actual is not None and expected != actual:
            corpus.append(f"{field}: stored={expected} replay={actual}")

    observed.fidelity_notes = contract + corpus
    if contract:
        observed.fidelity = "CONTRACT_DRIFT"
    elif corpus:
        observed.fidelity = "CORPUS_DRIFT"
    else:
        observed.fidelity = "OK"


def run_case(
    case: GoldenCase,
    workspace: GoldenReplayWorkspace,
    *,
    mode: str = "fast",
    live_provider_factory: Any = None,
) -> CaseResult:
    """Replay one case. ``mode`` is ``fast`` (policy GPT ②) or ``full`` (live)."""

    from answer.governance_models import GptProviderSettings
    from repositories.database import Database
    import services.answer_service as answer_module
    from services.answer_service import AnswerService
    from services.gpt_governance_service import GovernedHybridAnswerService
    from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService

    observed = CaseObservation()
    connection = sqlite3.connect(str(workspace.db_path))
    try:
        reset_inquiry(connection, case.inquiry_id)
    finally:
        connection.close()

    semantic = ReplaySemanticProvider(
        case.baseline_reference.get("understanding") or {}
    )
    order = BlockedOrderLookup()
    post = NaverPostRecorder()

    if mode == "full":
        if live_provider_factory is None:
            raise ValueError("full mode needs a live provider factory")
        draft_provider: Any = CapturingProvider(live_provider_factory())
        settings = GptProviderSettings.from_environment()
    else:
        draft_provider = PolicyDraftProvider()
        settings = GptProviderSettings(provider_name="fake")

    database = Database(workspace.db_path)
    dps = blocked_dps(database)
    governed = GovernedHybridAnswerService(
        database, provider=draft_provider, settings=settings,
    )
    original_notify = getattr(answer_module, "notify_qna_safely", None)
    answer_module.notify_qna_safely = lambda **_kwargs: False
    started = time.monotonic()
    try:
        outcome = AnswerService(
            database,
            hybrid_service=governed,
            order_lookup_service=order,
            dps_enrichment=dps,
            semantic_analyzer=GptSemanticAnalyzerService(semantic),
        ).generate_for_inquiry(case.inquiry_id)
        observed.ran = True
    except Exception as error:  # noqa: BLE001 - a failed case is an observation
        observed.error = f"{type(error).__name__}: {error}"[:400]
        outcome = None
    finally:
        observed.duration_seconds = round(time.monotonic() - started, 3)
        if original_notify is not None:
            answer_module.notify_qna_safely = original_notify

    observed.naver_post_calls = post.calls
    observed.dps_calls = int(getattr(dps, "golden_calls", 0))
    observed.order_lookup_calls = order.calls
    observed.gpt_calls = getattr(draft_provider, "calls", 0)

    if outcome is None:
        return CaseResult(case=case, observed=observed)

    metadata = outcome.result.metadata or {}
    hybrid = metadata.get("hybrid") if isinstance(metadata.get("hybrid"), dict) else {}
    draft = hybrid.get("draft") if isinstance(hybrid.get("draft"), dict) else {}
    trace = metadata.get("pipeline_trace") if isinstance(metadata.get("pipeline_trace"), dict) else {}
    retrieval = trace.get("retrieval") if isinstance(trace.get("retrieval"), dict) else {}
    understanding = (
        (metadata.get("semantic_routing") or {}).get("understanding") or {}
    )
    prompt = _prompt_of(draft_provider)
    prompt_input = prompt.get("input") or {}
    facts = prompt.get("allowed_facts") or {}

    observed.semantic_usable = understanding.get("usable")
    observed.semantic_actions = [
        str(item.get("action") or "")
        for item in (understanding.get("questions") or [])
    ]
    observed.atoms = [
        str(item.get("text") or "")
        for item in (understanding.get("questions") or [])
    ]
    observed.atom_count = len(observed.atoms)
    for field in ("need_template", "need_product", "need_learning",
                  "need_order", "need_dps"):
        setattr(observed, field, understanding.get(field))

    observed.learning_pool = retrieval.get("learning_pool")
    observed.learning_hard_valid = retrieval.get("learning_hard_valid")
    observed.learning_selected = retrieval.get("learning_selected")
    observed.product_identity_status = retrieval.get("product_identity_status")
    observed.verified_product_facts = retrieval.get("verified_product_facts")
    observed.template_candidates = retrieval.get("template_candidates")
    observed.subquestion_evidence = [
        dict(item) for item in (hybrid.get("subquestion_evidence") or [])
        if isinstance(item, dict)
    ]

    observed.prompt_fact_keys = sorted(facts)
    observed.prompt_learning_ids = _ids(
        prompt_input.get("similar_approved_answers"), "learning_example_id"
    )
    observed.prompt_historical_ids = _ids(
        prompt_input.get("historical_cases"), "historical_case_id"
    )
    drafts = getattr(draft_provider, "drafts", [])
    observed.prompt_chars = len(drafts[-1]["prompt"]) if drafts else None

    observed.used_learning_ids = [int(v) for v in (draft.get("used_learning_ids") or [])]
    observed.used_historical_ids = [int(v) for v in (draft.get("used_historical_ids") or [])]
    observed.used_template_ids = [str(v) for v in (draft.get("used_template_ids") or [])]
    observed.unresolved = [str(v) for v in (draft.get("unresolved") or [])]
    observed.requires_review = draft.get("requires_review")
    observed.can_auto_post = draft.get("can_auto_post")
    observed.answer = str(outcome.result.answer or "")

    validation = hybrid.get("validation") if isinstance(hybrid.get("validation"), dict) else {}
    observed.validator_status = validation.get("status")

    observed.eligibility_decision, observed.eligibility_reasons, observed.eligibility_soft_reasons = (
        _evaluate_eligibility(workspace.db_path, case.inquiry_id, metadata)
    )
    _check_fidelity(observed, case.baseline_reference)
    return CaseResult(case=case, observed=observed)


def _evaluate_eligibility(
    db_path: Path, inquiry_id: int, metadata: dict[str, Any]
) -> tuple[str | None, list[str], list[str]]:
    """Ask the real gate about the draft this run just wrote."""

    from services.auto_processing_eligibility_service import (
        AutoProcessingEligibilityService,
    )

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        inquiry = connection.execute(
            "SELECT * FROM inquiries WHERE id=?", (inquiry_id,)
        ).fetchone()
        draft = connection.execute(
            "SELECT * FROM answer_drafts WHERE inquiry_id=? AND is_active=1"
            " ORDER BY id DESC LIMIT 1",
            (inquiry_id,),
        ).fetchone()
    finally:
        connection.close()
    if inquiry is None or draft is None:
        return None, [], []
    route = str(metadata.get("selected_answer_route") or "")
    verdict = AutoProcessingEligibilityService().evaluate(
        inquiry=dict(inquiry), draft=dict(draft), route=route,
    )
    return verdict.decision, list(verdict.reasons), list(verdict.soft_reasons)
