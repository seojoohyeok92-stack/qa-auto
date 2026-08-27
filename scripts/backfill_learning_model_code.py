"""Fill ``learning_examples.model_code`` where -- and only where -- it is certain.

Every row in the store has an empty ``model_code``, so the compatibility gate
never reaches ``EXPLICIT_MODEL_CODE_MATCH`` through the column and falls back to
scanning the product name.  The Product Knowledge database already knows the
answer for some of these rows, and this script copies it across.

The whole risk here is filling the column with something that is not a model
code.  ``canonical_fact_listings.model_code`` is not clean: of 181 values, 89
are the numeric ``product_id`` echoed back and several are a display name
("삼성 80.1cm(32인치) 스탠바이미 ..."), because whatever produced that table
fell back to whatever identifier it had.  A product name written into
``model_code`` is worse than an empty column: the compatibility gate treats two
different model codes as a hard rejection, so one bad value silently deletes
that product's Learning from every answer.

So a value is accepted only when the value *is* a model code -- the whole
string matches the pattern the answer pipeline already uses to recognise one.
Nothing is extracted out of a longer string: pulling "S32FG500" out of a
product name would be guessing which token is the model, which is the mistake
this script exists to avoid.  The row is skipped instead, and an empty column
is exactly as safe as it was before.

Read-only by default.  ``--apply`` needs an explicit ``--database`` and refuses
to write to the production store, so a backfill can be rehearsed on a copy and
inspected before anyone decides to run it for real.

Usage:
    python -m scripts.backfill_learning_model_code                  # dry run
    python -m scripts.backfill_learning_model_code --json out.json  # artefact
    python -m scripts.backfill_learning_model_code --apply --database copy.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from repositories.product_fact_repository import (
    DEFAULT_PRODUCT_FACTS_DB_PATH,
    get_product_facts_path,
)
from services.learning_compatibility_service import (
    MODEL_STOPWORDS,
    _is_dimension_token,
    extract_product_identity,
)
from services.product_fact_guard import MODEL_CODE_PATTERN


DEFAULT_AUTOMATION_DB_PATH = Path("data") / "oje_automation.db"

SKIP_NO_PRODUCT_ID = "NO_PRODUCT_ID"
SKIP_NOT_IN_PRODUCT_KNOWLEDGE = "PK_ID_NOT_FOUND"
SKIP_NO_USABLE_MODEL_CODE = "PK_HAS_NO_MODEL_CODE_SHAPED_VALUE"
SKIP_AMBIGUOUS = "AMBIGUOUS_MULTIPLE_MODEL_CODES"
SKIP_ALREADY_SET = "ALREADY_HAS_MODEL_CODE"
SKIP_CONTRADICTS_LISTING = "CONTRADICTS_MODEL_CODE_IN_LISTING_NAME"


def resolve_model_code(value: object) -> str | None:
    """The model code this string *is*, or None.

    Deliberately not "the model code this string contains".  ``findall`` over a
    product name would happily return a token from the middle of it, and the
    caller has no way to tell a real code from a fragment that looks like one.
    """

    text = str(value or "").strip().upper()
    if not text:
        return None
    found = MODEL_CODE_PATTERN.fullmatch(text)
    if found is None:
        return None
    if text in MODEL_STOPWORDS or _is_dimension_token(text):
        return None
    return text


def _product_id(metadata: Mapping[str, Any]) -> str:
    identity = metadata.get("product_identity")
    identity = identity if isinstance(identity, Mapping) else {}
    for value in (
        identity.get("product_id"),
        metadata.get("product_id"),
        metadata.get("source_product_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def product_knowledge_model_codes(
    connection: sqlite3.Connection,
) -> dict[str, set[str]]:
    """product_id -> the model codes Product Knowledge records for it."""

    mapping: dict[str, set[str]] = {}
    rows = connection.execute(
        "SELECT product_id, model_code FROM canonical_fact_listings "
        "WHERE model_code IS NOT NULL AND TRIM(model_code) <> ''"
    )
    for product_id, model_code in rows:
        # The id is recorded even when none of its values survive, so a caller
        # can tell "we have never heard of this product" apart from "we have,
        # and nothing it holds is a model code".
        codes = mapping.setdefault(str(product_id).strip(), set())
        resolved = resolve_model_code(model_code)
        if resolved is not None:
            codes.add(resolved)
    return mapping


@dataclass
class BackfillPlan:
    total: int = 0
    already_set: int = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_learning_rows": self.total,
            "model_code_already_set": self.already_set,
            "backfill_candidates": len(self.candidates),
            "skipped": dict(sorted(self.skipped.items())),
            "candidates": self.candidates,
        }


def build_plan(
    automation: sqlite3.Connection,
    product_knowledge: sqlite3.Connection,
) -> BackfillPlan:
    mapping = product_knowledge_model_codes(product_knowledge)
    plan = BackfillPlan()
    # Joined exactly as LearningRepository.candidates joins it, so the code
    # derived here is the one retrieval will actually compare against.
    rows = automation.execute(
        "SELECT learning_examples.id, learning_examples.model_code, "
        "       learning_examples.metadata_json, learning_examples.product_name, "
        "       learning_examples.learning_source, learning_examples.style_only, "
        "       learning_examples.active, inquiries.product_id, "
        "       inquiries.product_name, inquiries.option_name "
        "  FROM learning_examples "
        "  LEFT JOIN inquiries ON inquiries.id=learning_examples.inquiry_id"
    )
    for row in rows:
        plan.total += 1
        (learning_id, model_code, metadata_json, product_name,
         learning_source, style_only, active,
         source_product_id, source_product_name, source_option_name) = row
        if str(model_code or "").strip():
            # Never overwrite: a value already there was put there by something
            # that knew more about the row than this script does.
            plan.already_set += 1
            plan.skipped[SKIP_ALREADY_SET] += 1
            continue
        try:
            metadata = json.loads(metadata_json or "{}")
        except ValueError:
            metadata = {}
        metadata = metadata if isinstance(metadata, dict) else {}
        product_id = _product_id(metadata)
        if not product_id:
            plan.skipped[SKIP_NO_PRODUCT_ID] += 1
            continue
        codes = mapping.get(product_id)
        if codes is None:
            plan.skipped[SKIP_NOT_IN_PRODUCT_KNOWLEDGE] += 1
            continue
        if not codes:
            plan.skipped[SKIP_NO_USABLE_MODEL_CODE] += 1
            continue
        if len(codes) > 1:
            # Two genuine model codes for one product id is a question about
            # the catalogue, not something to resolve by picking one.
            plan.skipped[SKIP_AMBIGUOUS] += 1
            continue
        resolved = next(iter(codes))
        # The listing and the catalogue do not always use the same name for a
        # product: the listing says "BE85F", the catalogue says
        # "LH85BEFHLGFXKR". Both are real codes for the same television. But
        # the compatibility gate compares model codes by string equality, and
        # the question side derives its code from that same listing name -- so
        # writing the catalogue's spelling into the column makes the listing
        # stop matching its own Learning (EXACT_MODEL becomes MODEL_MISMATCH,
        # measured). Which spelling is canonical is a question for a person.
        derived = extract_product_identity(
            product_id=source_product_id,
            product_name=source_product_name or product_name,
            model_code=None,
            option=source_option_name,
            metadata=metadata,
        ).model_code
        if derived and derived.upper() != resolved:
            plan.skipped[SKIP_CONTRADICTS_LISTING] += 1
            continue
        plan.candidates.append({
            "learning_id": int(learning_id),
            "product_id": product_id,
            "resolved_model_code": resolved,
            "product_knowledge_source": "canonical_fact_listings.model_code",
            "learning_source": learning_source,
            "style_only": bool(style_only),
            "active": bool(active),
            "product_name": product_name,
            "listing_derived_model_code": derived,
        })
    return plan


def apply_plan(
    connection: sqlite3.Connection, candidates: Iterable[Mapping[str, Any]]
) -> int:
    """Write the resolved codes. Only ever called against an explicit copy."""

    updated = 0
    for candidate in candidates:
        cursor = connection.execute(
            "UPDATE learning_examples SET model_code=?, "
            "       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            " WHERE id=? AND (model_code IS NULL OR TRIM(model_code)='')",
            (candidate["resolved_model_code"], int(candidate["learning_id"])),
        )
        updated += cursor.rowcount
    connection.commit()
    return updated


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=None,
                        help="automation database to read (and write with --apply)")
    parser.add_argument("--product-facts", type=Path, default=None,
                        help=f"Product Knowledge db (default {DEFAULT_PRODUCT_FACTS_DB_PATH})")
    parser.add_argument("--apply", action="store_true",
                        help="write the resolved codes; requires --database")
    parser.add_argument("--json", type=Path, default=None,
                        help="write the full plan as an auditable artefact")
    args = parser.parse_args(argv)

    automation_path = args.database or DEFAULT_AUTOMATION_DB_PATH
    facts_path = get_product_facts_path(args.product_facts)
    if not facts_path.is_file():
        parser.error(f"Product Knowledge database not found: {facts_path}")

    with _read_only(automation_path) as automation, \
            _read_only(facts_path) as knowledge:
        plan = build_plan(automation, knowledge)

    print(f"learning rows                 : {plan.total}")
    print(f"model_code already set        : {plan.already_set}")
    print(f"safe backfill candidates      : {len(plan.candidates)}")
    for reason, count in sorted(plan.skipped.items()):
        print(f"  skip {reason:<36} {count}")
    resolved = Counter(c["resolved_model_code"] for c in plan.candidates)
    for code, count in resolved.most_common():
        print(f"  resolved {code:<24} {count}")

    if args.json:
        args.json.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"plan written to {args.json}")

    if not args.apply:
        print("dry run: no database was modified")
        return 0
    if args.database is None:
        parser.error("--apply needs an explicit --database; it never guesses one")
    if automation_path.resolve() == DEFAULT_AUTOMATION_DB_PATH.resolve():
        parser.error(
            "refusing to write to the production store. Rehearse on a copy: "
            "sqlite3 backup the database and point --database at it."
        )
    with sqlite3.connect(automation_path) as connection:
        updated = apply_plan(connection, plan.candidates)
    print(f"applied to {automation_path}: {updated} rows updated")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
