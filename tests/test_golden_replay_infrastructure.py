"""The Golden harness's own contract. Not a quality gate -- a safety proof.

The quality numbers live in ``scripts/run_golden_replay.py`` and deliberately
do not run under pytest: a green suite must never be able to stand in for a
quality result. What *does* belong here is everything that would make those
numbers untrustworthy or dangerous:

* the replay cannot reach Naver, DPS or a real order lookup;
* the production copy is never opened for writing;
* a label can never be back-filled from what the program did;
* a metric with no label reports its absence instead of a number.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from golden import selection as selection_module
from golden.metrics import FAST_MODE_CAVEATS, LAYER2_ONLY, METRIC_NAMES, score
from golden.providers import (
    BlockedOrderLookup,
    NaverPostRecorder,
    PolicyDraftProvider,
    ReplaySemanticProvider,
)
from golden.runner import copy_database
from golden.schema import CaseObservation, CaseResult, GoldenCase, GoldenLabel

CASES = Path(__file__).parents[1] / "golden" / "cases"
LABELS = Path(__file__).parents[1] / "golden" / "labels" / "anchors.json"
ANCHOR_IDS = ("688159337", "688159361", "688159391", "688159421")


# --------------------------------------------------------------- side effects
def test_the_naver_client_raises_instead_of_posting():
    recorder = NaverPostRecorder()
    with pytest.raises(AssertionError):
        recorder.send(object(), access_token="X")
    assert recorder.calls == 1


def test_order_lookup_raises_instead_of_calling_naver():
    order = BlockedOrderLookup()
    with pytest.raises(AssertionError):
        order.lookup_for_inquiry(1)
    assert order.calls == 1


def test_dps_keeps_its_decision_logic_but_cannot_look_anything_up(tmp_path):
    """Routing correctness is measured, so ``policy.decide`` must stay real."""

    from golden.providers import blocked_dps
    from repositories.database import Database

    database = Database(tmp_path / "d.db")
    database.initialize()
    service = blocked_dps(database)
    assert hasattr(service.policy, "decide")
    with pytest.raises(AssertionError):
        service.enrich(object())
    assert service.golden_calls == 1


def test_the_source_database_is_only_ever_read(tmp_path):
    source = tmp_path / "source.db"
    connection = sqlite3.connect(str(source))
    connection.execute("CREATE TABLE t (id INTEGER)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()
    before = source.read_bytes()

    destination = tmp_path / "copy.db"
    copy_database(source, destination)
    target = sqlite3.connect(str(destination))
    target.execute("INSERT INTO t VALUES (2)")
    target.commit()
    target.close()

    assert source.read_bytes() == before
    assert sqlite3.connect(str(destination)).execute(
        "SELECT COUNT(*) FROM t"
    ).fetchone()[0] == 2


# ------------------------------------------------------------------- labelling
def test_a_label_cannot_be_created_from_an_observation():
    """The one mistake that would make the whole exercise worthless."""

    assert not hasattr(GoldenLabel, "from_observation")
    assert GoldenLabel().label_source == "UNLABELLED"
    assert GoldenLabel().is_labelled() is False


def test_selection_leaves_labels_empty_apart_from_the_real_seller_answer():
    from golden.population import PopulationRow

    row = PopulationRow(
        inquiry_id=1, source_question_id="1", question="q", product_name="p",
        source_type="PRODUCT_INQUIRY", registered_at="",
        production_answer="사람이 실제로 보낸 답변", draft_id=None,
    )
    case = selection_module.build_case(row, tier="CORE", catalog_status="EXACT")
    assert case.label.production_answer == "사람이 실제로 보낸 답변"
    # A real posted answer is material for a human, not a verdict by itself.
    assert case.label.expected_answer_quality is None
    assert case.label.expected_review_required is None
    assert case.label.label_source == "UNLABELLED"


def test_every_anchor_is_present_and_carries_a_sourced_label():
    cases = {c.source_question_id: c for c in
             selection_module.read_cases(CASES / "anchor.jsonl")}
    assert set(cases) == set(ANCHOR_IDS)
    for qid, case in cases.items():
        assert case.tier == "ANCHOR"
        assert case.label.label_source in {
            "FORENSIC_ANALYSIS", "COMPANY_POLICY", "PRODUCTION_ANSWER",
            "HUMAN_REVIEW",
        }, qid
        assert case.label.required_subquestions, qid
        assert case.label.notes.strip(), qid


def test_the_unresolved_anchor_stays_unresolved():
    """688159391 has no established answer; the file must not pretend it does.

    Its remote-control question has zero exact-product Learning behind it and
    the sibling products that do carry one are a different size. Writing a
    verdict here would be inventing the answer key.
    """

    payload = json.loads(LABELS.read_text(encoding="utf-8"))
    entry = payload["688159391"]
    assert entry["expected_answerability"] is None
    assert entry["expected_review_required"] is None
    assert entry["forbidden_evidence_learning_ids"], (
        "what is established about this case -- which candidates are"
        " irrelevant -- should still be recorded"
    )


def test_core_contains_every_anchor():
    core = {c.source_question_id for c in
            selection_module.read_cases(CASES / "core.jsonl")}
    assert set(ANCHOR_IDS) <= core


# --------------------------------------------------------------------- metrics
def _result(**observed) -> CaseResult:
    case = GoldenCase(
        case_id="C", source_question_id="1", inquiry_id=1,
        question="q", product_name="p",
    )
    return CaseResult(case=case, observed=CaseObservation(ran=True, **observed))


def test_an_unlabelled_case_reports_absence_rather_than_a_score():
    report = score([_result()], mode="fast")
    for name in ("semantic_correctness", "unnecessary_review",
                 "missed_required_review", "auto_post_false_positive"):
        assert report["metrics"][name]["value"] is None
        assert "NOT_YET_LABELED" in report["metrics"][name]["render"]


def test_judgement_metrics_are_not_measurable_without_a_real_model():
    report = score([_result()], mode="fast")
    for name in LAYER2_ONLY:
        assert report["metrics"][name]["value"] is None
        assert "NOT_MEASURABLE" in report["metrics"][name]["render"]


def test_every_metric_name_is_scored_and_the_caveats_name_real_metrics():
    report = score([_result()], mode="fast")
    assert set(report["metrics"]) == set(METRIC_NAMES)
    assert set(FAST_MODE_CAVEATS) <= set(METRIC_NAMES)


def test_a_correct_hold_is_not_counted_as_a_coverage_gap():
    """688159361's shape: two questions, both correctly deferred to the order.

    Counting those as uncovered would make the metric fall when safety works.
    """

    result = _result(
        atom_count=2, unresolved=["a", "b"],
        eligibility_decision="REVIEW_REQUIRED",
    )
    result.case.label = GoldenLabel(
        label_source="COMPANY_POLICY",
        expected_answerability="NEEDS_CURRENT_ORDER_FACT",
        expected_review_required=True,
    )
    report = score([result], mode="fast")
    assert report["metrics"]["compound_coverage"]["value"] is None
    assert report["metrics"]["missed_required_review"]["numerator"] == 0
    assert report["metrics"]["unnecessary_review"]["numerator"] == 0


def test_clearing_a_case_a_label_says_needs_a_person_is_critical():
    result = _result(eligibility_decision="SAFE")
    result.case.label = GoldenLabel(
        label_source="HUMAN_REVIEW", expected_review_required=True,
    )
    report = score([result], mode="fast")
    assert report["metrics"]["auto_post_false_positive"]["value"] == 1.0
    assert report["severity"]["CRITICAL"] >= 1


def test_holding_a_case_with_sufficient_evidence_is_only_minor():
    result = _result(eligibility_decision="REVIEW_REQUIRED",
                     eligibility_reasons=["GPT_REPORTED_UNRESOLVED"])
    result.case.label = GoldenLabel(
        label_source="HUMAN_REVIEW", expected_review_required=False,
    )
    report = score([result], mode="fast")
    assert report["metrics"]["unnecessary_review"]["value"] == 1.0
    assert report["severity"]["CRITICAL"] == 0
    assert report["severity"]["MINOR"] >= 1


def test_coverage_is_judged_on_meaning_not_on_the_labeller_s_wording():
    """"1~2일 연기" and "하루 이틀 뒤로 미룰" are the same question."""

    from golden.metrics import _covers

    atoms = ["잡혀있는 배송 날짜가 있다면 하루 이틀 뒤로 미룰 수 있는지 알고 싶습니다."]
    assert _covers(atoms, ["연기|미룰|미루|뒤로|변경"]) is True
    assert _covers(atoms, ["리모컨 포함 여부"]) is False


# ------------------------------------------------------------------- providers
def test_the_replayed_understanding_keeps_the_actions_it_was_given():
    provider = ReplaySemanticProvider({
        "usable": True, "need_order": False, "need_dps": False,
        "purchase_state": "UNKNOWN",
        "questions": [{"text": "기존 TV 수거되나요?", "action": "COLLECTION",
                       "requested_attribute": "PERMISSION_OR_OPTION"}],
    })
    payload = provider.generate_json(task="UNDERSTAND", prompt="", context={})
    assert payload["primary_action"] == "COLLECTION"
    assert payload["requires_order_context"] is False
    assert [q["action"] for q in payload["atomic_questions"]] == ["COLLECTION"]
    assert provider.reconstruction_notes(), "gaps in the replay must be stated"


def test_the_policy_provider_defers_exactly_the_atoms_with_no_evidence():
    provider = PolicyDraftProvider()
    prompt = json.dumps({"input": {"subquestion_evidence": [
        {"subquestion": "A", "status": "CANDIDATE", "learning_ids": [7],
         "historical_case_ids": []},
        {"subquestion": "B", "status": "NEEDS_DPS", "learning_ids": [],
         "historical_case_ids": []},
    ]}})
    raw = provider.generate_json(task="DRAFT", prompt=prompt, context={})
    assert raw["used_learning_ids"] == [7]
    assert raw["unresolved"] == ["B"]
    assert raw["can_auto_post"] is False


def test_the_recorded_baselines_exist_and_report_no_side_effects():
    """The stored BEFORE snapshot is what every later run is compared to."""

    base = Path(__file__).parents[1] / "golden" / "baselines"
    for name in ("CURRENT_BASELINE_V3_POST_688159337_FIX_ANCHOR",
                 "CURRENT_BASELINE_V3_POST_688159337_FIX_CORE"):
        payload = json.loads((base / f"{name}.json").read_text(encoding="utf-8"))
        assert payload["cases_ran"] == payload["cases"]
        effects = payload["side_effects"]
        assert effects["naver_post_calls"] == 0
        assert effects["dps_calls"] == 0
        assert effects["order_lookup_calls"] == 0
        assert effects["live_gpt_calls"] == 0, "FAST baseline must be free"
        assert payload["git"]["commit"]
