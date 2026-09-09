"""Choosing the Golden set, and refusing to invent labels while doing it.

Selection is stratified and risk-weighted, never uniform random: the axes the
audit found to matter (product identity, compound shape, order/DPS need,
evidence availability) are rare in exactly the places where the pipeline is
weakest, and a proportional sample would lose them.

Two rules:

* **Anchors are not sampled.** The four forensic inquiries are always present,
  whatever the sampler does, because each one watches a specific known defect.
* **Selection never writes a label.** It fills ``stratum`` and
  ``baseline_reference`` from stored data, and leaves ``label`` unlabelled for
  a human -- or for the seller answer that was actually posted -- to supply.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from golden.population import PopulationRow, stratum_of
from golden.schema import GoldenCase, GoldenLabel

# The four forensic specimens, always included. The note says what each one
# exists to detect, so a later reader knows why it may not be dropped.
ANCHORS: dict[str, str] = {
    "688159337": "관련 Learning이 있는데 코드가 억제하는가 (G29/G30, 안전초안 회귀)",
    "688159361": "현재 주문 사실 safety — 근거 없이 날짜를 답하지 않는가",
    "688159391": "Product Knowledge 도달 여부 (G12/G14)",
    "688159421": "sub-question evidence suppression (G26)",
}


def _stable_key(case_id: str, salt: str) -> int:
    digest = hashlib.sha256(f"{salt}:{case_id}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def build_case(
    row: PopulationRow, *, tier: str, catalog_status: str
) -> GoldenCase:
    """One population row as a case, with stratum and reference but no label."""

    understanding = row.understanding
    trace = row.draft_metadata.get("pipeline_trace")
    trace = trace if isinstance(trace, dict) else {}
    retrieval = trace.get("retrieval") if isinstance(trace.get("retrieval"), dict) else {}
    decision = row.draft_metadata.get("production_decision_trace")
    decision = decision if isinstance(decision, dict) else {}
    hybrid = row.draft_metadata.get("hybrid")
    hybrid = hybrid if isinstance(hybrid, dict) else {}
    stored_draft = hybrid.get("draft") if isinstance(hybrid.get("draft"), dict) else {}

    reference: dict[str, Any] = {}
    if row.has_current_pipeline_draft:
        reference = {
            "draft_id": row.draft_id,
            "understanding": dict(understanding),
            "learning_pool": retrieval.get("learning_pool"),
            "learning_hard_valid": retrieval.get("learning_hard_valid"),
            "learning_selected": retrieval.get("learning_selected"),
            "product_identity_status": retrieval.get("product_identity_status"),
            "verified_product_facts": retrieval.get("verified_product_facts"),
            "need_template": understanding.get("need_template"),
            "need_product": understanding.get("need_product"),
            "need_learning": understanding.get("need_learning"),
            "need_order": understanding.get("need_order"),
            "need_dps": understanding.get("need_dps"),
            "stored_unresolved": list(stored_draft.get("unresolved") or []),
            "stored_requires_review": stored_draft.get("requires_review"),
            "stored_can_auto_post": stored_draft.get("can_auto_post"),
            "stored_used_learning_ids": list(
                stored_draft.get("used_learning_ids") or []
            ),
            "stored_eligibility": decision.get("eligibility"),
            "stored_blocking_reasons": list(
                decision.get("blocking_reason_codes") or []
            ),
        }

    label = GoldenLabel()
    # The one label a machine may fill: what a person actually sent the
    # customer. It is evidence about the right answer and it did not come from
    # this program, but it is recorded as material for a human to judge -- it
    # does not by itself set expected_answer_quality.
    if row.production_answer:
        label.production_answer = row.production_answer

    return GoldenCase(
        case_id=f"Q{row.source_question_id}",
        source_question_id=row.source_question_id,
        inquiry_id=row.inquiry_id,
        question=row.question,
        product_name=row.product_name,
        tier=tier,
        stratum=stratum_of(row, catalog_status=catalog_status),
        label=label,
        baseline_reference=reference,
    )


def select(
    rows: list[PopulationRow],
    catalog_status: dict[int, str],
    *,
    target_ratio: float = 0.25,
    salt: str = "golden-v1",
    require_replayable: bool = True,
) -> tuple[list[GoldenCase], dict[str, Any]]:
    """Anchors, then a stratified draw over the rest.

    ``require_replayable`` restricts the draw to rows whose GPT ① contract was
    persisted, because only those can be replayed without paying for a model
    call. The report says how much of the corpus that excludes.
    """

    by_qid = {row.source_question_id: row for row in rows}
    chosen: dict[str, GoldenCase] = {}

    for qid, why in ANCHORS.items():
        row = by_qid.get(qid)
        if row is None:
            continue
        case = build_case(
            row, tier="ANCHOR", catalog_status=catalog_status.get(row.inquiry_id, "UNKNOWN")
        )
        case.label.notes = why
        chosen[qid] = case

    pool = [
        row for row in rows
        if row.source_question_id not in chosen
        and (row.replayable_understanding or not require_replayable)
    ]

    # Strata are the cross-product of the axes that actually change behaviour.
    buckets: dict[tuple, list[PopulationRow]] = {}
    for row in pool:
        stratum = stratum_of(
            row, catalog_status=catalog_status.get(row.inquiry_id, "UNKNOWN")
        )
        key = (
            stratum["primary_action"],
            stratum["product_identity"],
            stratum["atom_bucket"],
            bool(stratum["need_order"]) or bool(stratum["need_dps"]),
        )
        buckets.setdefault(key, []).append(row)

    target = max(0, round(len(pool) * float(target_ratio)))
    picks: list[PopulationRow] = []
    # At least one from every stratum -- that is the oversampling of rare but
    # important shapes -- then fill proportionally by stable hash.
    for key, members in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        members = sorted(
            members, key=lambda r: _stable_key(r.source_question_id, salt)
        )
        take = max(1, round(len(members) * float(target_ratio)))
        picks.extend(members[:take])
    if len(picks) > target and target >= len(buckets):
        picks = sorted(
            picks, key=lambda r: _stable_key(r.source_question_id, salt)
        )[:target]

    for row in picks:
        if row.source_question_id in chosen:
            continue
        chosen[row.source_question_id] = build_case(
            row, tier="CORE",
            catalog_status=catalog_status.get(row.inquiry_id, "UNKNOWN"),
        )

    report = {
        "anchors": len([c for c in chosen.values() if c.tier == "ANCHOR"]),
        "core": len([c for c in chosen.values() if c.tier == "CORE"]),
        "pool_size": len(pool),
        "target_ratio": target_ratio,
        "strata_count": len(buckets),
        "require_replayable": require_replayable,
    }
    ordered = sorted(chosen.values(), key=lambda c: (c.tier != "ANCHOR", c.case_id))
    return ordered, report


def apply_labels(cases: list[GoldenCase], path: Path) -> int:
    """Merge human/policy-sourced labels onto selected cases.

    Kept separate from selection so the answer key has its own file, its own
    history and its own review: a label changing is a decision about what is
    correct, and it must never look like a side effect of resampling.
    """

    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    applied = 0
    fields = set(GoldenLabel().__dict__)
    for case in cases:
        entry = payload.get(case.source_question_id)
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if key in fields and value is not None:
                setattr(case.label, key, value)
        applied += 1
    return applied


def write_cases(cases: Iterable[GoldenCase], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(case.to_json() + "\n")
            count += 1
    return count


def read_cases(path: Path) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                cases.append(GoldenCase.from_dict(json.loads(line)))
    return cases
