"""Build a read-only Product Knowledge completeness audit data set.

The audit deliberately keeps three boundaries separate:

* source capture (archived listing/image inventory),
* structured extraction (image analysis -> canonical fact), and
* production runtime exposure (model_data_with_color.json).

It never writes to either source and does not treat a completed pipeline run as
proof that a fact is available to production.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


RUNTIME_SECTIONS = (
    "model_facts",
    "listing_facts",
    "bundle_accessory_facts",
    "policy_facts",
    "multi_model_facts",
)
GOLDEN_PRODUCTS = ("11363535046", "11792827130", "9645702227")


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _human_transcript(value: Any) -> str:
    parts: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                if key in {"text", "content", "transcript"} and isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(node, str) and node.strip():
            parts.append(node.strip())

    visit(value)
    seen: set[str] = set()
    ordered = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            ordered.append(part)
    return " | ".join(ordered)


def _product_ids(row: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    for key in ("product_id", "applies_to_product_id", "source_product_id"):
        values.append(row.get(key))
    values.extend(row.get("source_product_ids") or [])
    values.extend(row.get("applies_to_product_ids") or [])
    return {str(v).strip() for v in values if str(v or "").strip()}


def _model(row: dict[str, Any]) -> str:
    return str(
        row.get("canonical_model")
        or row.get("model_code")
        or row.get("model")
        or row.get("base_model")
        or ""
    ).strip()


def _field(row: dict[str, Any]) -> str:
    return str(row.get("field_key") or row.get("field") or row.get("key") or "").strip()


def _status(row: dict[str, Any]) -> str:
    return str(
        row.get("runtime_status")
        or row.get("verification_status")
        or row.get("status")
        or row.get("mapping_status")
        or ""
    ).upper()


def _runtime_eligible(row: dict[str, Any], section: str) -> bool:
    status = _status(row)
    if section in {"excluded_records", "multi_model_facts"}:
        return False
    if any(token in status for token in ("EXCLUDED", "WITHHELD", "CONFLICT", "NEEDS_REVIEW", "UNRESOLVED")):
        return False
    return bool(_field(row))


def _iter_runtime_rows(payload: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    pk = payload.get("PRODUCT_KNOWLEDGE") or {}
    for section in (*RUNTIME_SECTIONS, "excluded_records"):
        rows = pk.get(section) or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows:
            if isinstance(row, dict):
                yield section, row


def _primary_model(rows: list[dict[str, Any]]) -> str:
    main = Counter(
        _model(row)
        for row in rows
        if _model(row) and str(row.get("subject") or "").upper() == "MAIN_PRODUCT"
    )
    counts = main or Counter(_model(row) for row in rows if _model(row))
    return counts.most_common(1)[0][0] if counts else ""


def _catalog_runtime_rows(
    model_catalog: dict[str, Any], *, product_id: str, primary_model: str
) -> list[dict[str, Any]]:
    if not primary_model:
        return []
    candidates = [
        (key, value) for key, value in model_catalog.items()
        if isinstance(value, dict)
        and (str(key).upper() == primary_model.upper() or str(value.get("model") or "").upper() == primary_model.upper())
    ]
    if not candidates:
        return []
    key, record = candidates[0]
    direct = {
        "screen_size": record.get("size_inch"),
        "resolution": record.get("resolution"),
        "refresh_rate": record.get("hz"),
        "vesa_mm": record.get("vesa"),
        "speaker_present": record.get("speaker"),
        "weight_catalog": record.get("weight"),
        "brand": record.get("brand"),
        "model_name": record.get("model"),
        "color": record.get("color"),
    }
    spec = str(record.get("spec") or "")
    upper = spec.upper()
    token_fields = {
        "hdmi_present": ("HDMI",),
        "usb_present": ("USB",),
        "ethernet_present": ("LAN", "ETHERNET"),
        "rf_terminal": ("RF", "안테나"),
        "bluetooth_present": ("BLUETOOTH", "블루투스"),
        "wifi_present": ("WI-FI", "WIFI", "와이파이", "무선"),
        "stand_spacing": ("스탠드 다리", "다리 간격", "다리 사이", "받침대 간격"),
    }
    for field, tokens in token_fields.items():
        if any(token.upper() in upper for token in tokens):
            direct[field] = spec
    result = []
    for field, value in direct.items():
        if value in (None, "", False):
            continue
        result.append(
            {
                "section": "MODEL_CATALOG_RUNTIME",
                "field": field,
                "field_key": field,
                "value": value,
                "model": primary_model,
                "model_code": primary_model,
                "product_id": product_id,
                "subject": "MAIN_PRODUCT",
                "runtime_eligible": True,
                "status": "CATALOG_JSON",
                "scope": "MODEL_CATALOG",
                "catalog_key": key,
            }
        )
    return result


def _walk_fact_like(node: Any, inherited: dict[str, Any] | None = None) -> Iterable[dict[str, Any]]:
    """Yield fact-like objects without assuming an obsolete analyzer schema."""
    inherited = inherited or {}
    if isinstance(node, list):
        for item in node:
            yield from _walk_fact_like(item, inherited)
        return
    if not isinstance(node, dict):
        return
    current = dict(inherited)
    for key in ("source_text", "raw_text", "text", "image_region", "subject", "model_code"):
        if node.get(key) not in (None, ""):
            current[key] = node[key]
    has_field = any(node.get(k) not in (None, "") for k in ("field", "field_key", "key"))
    has_value = any(node.get(k) not in (None, "") for k in ("value", "normalized_value", "raw_value"))
    if has_field and has_value:
        row = dict(current)
        row.update(node)
        yield row
    for value in node.values():
        if isinstance(value, (dict, list)):
            yield from _walk_fact_like(value, current)


def _table_rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def build_audit(catalog_path: Path, archive_db: Path) -> dict[str, Any]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{archive_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    listings = _table_rows(conn, "SELECT * FROM listings ORDER BY product_id")
    assets = {r["image_sha256"]: r for r in _table_rows(conn, "SELECT * FROM image_assets")}
    occurrences = _table_rows(conn, "SELECT * FROM listing_image_occurrences")
    analyses = _table_rows(
        conn,
        "SELECT image_analysis_id,image_sha256,extraction_method,analyzer,analyzer_version,"
        "transcript_json,result_json,confidence,created_at FROM image_analyses",
    )
    canonical = _table_rows(
        conn,
        "SELECT cf.*, cfl.product_id, cfl.model_code, cfv.raw_value_json, cfv.normalized_value_json "
        "FROM canonical_facts cf "
        "LEFT JOIN canonical_fact_listings cfl ON cfl.canonical_fact_id=cf.canonical_fact_id "
        "LEFT JOIN canonical_fact_values cfv ON cfv.value_id=cf.selected_value_id",
    )
    provenance = _table_rows(
        conn,
        "SELECT cfp.*, cfl.product_id, cfl.model_code, cf.field, cf.scope, "
        "cf.verification_status, cf.resolution_status "
        "FROM canonical_fact_provenance cfp "
        "JOIN canonical_facts cf ON cf.canonical_fact_id=cfp.canonical_fact_id "
        "LEFT JOIN canonical_fact_listings cfl ON cfl.canonical_fact_id=cfp.canonical_fact_id",
    )

    listing_by_id = {r["listing_id"]: r for r in listings}
    listing_by_product = {str(r.get("product_id") or ""): r for r in listings}
    product_hashes: dict[str, set[str]] = defaultdict(set)
    product_occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    hash_products: dict[str, set[str]] = defaultdict(set)
    for occurrence in occurrences:
        pid = str(occurrence.get("product_id") or "")
        if not pid:
            listing = listing_by_id.get(occurrence.get("listing_id"), {})
            pid = str(listing.get("product_id") or "")
        digest = str(occurrence.get("image_sha256") or "")
        product_occurrences[pid].append(occurrence)
        if digest:
            product_hashes[pid].add(digest)
            hash_products[digest].add(pid)

    analysis_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    observations_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    transcript_candidates: list[dict[str, Any]] = []
    for analysis in analyses:
        digest = str(analysis.get("image_sha256") or "")
        analysis_by_hash[digest].append(analysis)
        result = _loads(analysis.get("result_json"), {})
        seen: set[tuple[str, str, str]] = set()
        extracted = []
        for item in _walk_fact_like(result):
            field = _field(item)
            value = _text(item.get("normalized_value", item.get("value", item.get("raw_value"))))
            source_text = _text(item.get("source_text") or item.get("raw_text") or item.get("text"))
            key = (field, value, source_text)
            if field and value and key not in seen:
                seen.add(key)
                extracted.append(
                    {
                        "image_sha256": digest,
                        "field": field,
                        "value": value,
                        "raw_source_text": source_text,
                        "subject": _text(item.get("subject")),
                        "model_code": _text(item.get("model_code")),
                        "analysis_id": analysis["image_analysis_id"],
                        "analysis_created_at": analysis.get("created_at"),
                    }
                )
        observations_by_hash[digest].extend(extracted)
        transcript = _loads(analysis.get("transcript_json"), analysis.get("transcript_json") or "")
        transcript_text = _human_transcript(transcript).strip()
        if transcript_text and not extracted:
            transcript_candidates.append(
                {
                    "image_sha256": digest,
                    "raw_source_text": transcript_text[:3000],
                    "missing_reason": "PARSER_NO_RULE",
                    "analysis_id": analysis["image_analysis_id"],
                }
            )

    # Multiple analyzer versions may have processed the same immutable image.
    # Count each explicit image fact once and do not call an early parser miss a
    # current gap when a later analysis recovered facts from the same pixels.
    for digest, rows in list(observations_by_hash.items()):
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["field"], row["value"], row["raw_source_text"])
            unique.setdefault(key, row)
        observations_by_hash[digest] = list(unique.values())
    transcript_only = [
        row for row in transcript_candidates if not observations_by_hash.get(row["image_sha256"])
    ]
    unique_transcript_only: dict[str, dict[str, Any]] = {}
    for row in transcript_only:
        unique_transcript_only.setdefault(row["image_sha256"], row)
    transcript_only = list(unique_transcript_only.values())

    prov_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in provenance:
        if row.get("image_sha256"):
            prov_by_hash[str(row["image_sha256"])].append(row)

    runtime_rows: list[dict[str, Any]] = []
    runtime_by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    runtime_by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_product_ids: set[str] = set()
    for section, row in _iter_runtime_rows(catalog):
        enriched = dict(row)
        enriched["section"] = section
        enriched["field"] = _field(row)
        enriched["model"] = _model(row)
        enriched["runtime_eligible"] = _runtime_eligible(row, section)
        pids = _product_ids(row)
        all_product_ids.update(pids)
        for pid in pids:
            runtime_by_product[pid].append(enriched)
        provenance_value = row.get("provenance") or {}
        hashes: list[Any] = []
        if isinstance(provenance_value, dict):
            hashes.extend(provenance_value.get("source_image_hashes") or [])
            hashes.append(provenance_value.get("source_image_hash"))
        hashes.extend(row.get("source_image_hashes") or [])
        hashes.append(row.get("source_image_hash"))
        for digest in {str(h) for h in hashes if h}:
            runtime_by_hash[digest].append(enriched)
        runtime_rows.append(enriched)

    model_catalog = catalog.get("MODEL_CATALOG") or {}
    aliases = catalog.get("MODEL_ALIASES") or {}
    for pid in sorted(all_product_ids):
        primary = _primary_model(runtime_by_product.get(pid, []))
        runtime_by_product[pid].extend(
            _catalog_runtime_rows(model_catalog, product_id=pid, primary_model=primary)
        )
    product_coverage: list[dict[str, Any]] = []
    missing_facts: list[dict[str, Any]] = []
    unmapped_facts: list[dict[str, Any]] = []
    image_coverage: list[dict[str, Any]] = []
    runtime_gaps: list[dict[str, Any]] = []
    source_provenance: list[dict[str, Any]] = []

    for pid in sorted(p for p in all_product_ids if p):
        listing = listing_by_product.get(pid, {})
        product_rows = runtime_by_product.get(pid, [])
        hashes = product_hashes.get(pid, set())
        occs = product_occurrences.get(pid, [])
        downloaded = {h for h in hashes if h in assets}
        placeholders = {
            h for h in downloaded
            if int(assets[h].get("width") or 0) <= 1
            or int(assets[h].get("height") or 0) <= 1
            or int(assets[h].get("file_size") or 0) < 1024
        }
        meaningful = downloaded - placeholders
        analyzed = {h for h in meaningful if analysis_by_hash.get(h)}
        observations = [o for h in hashes for o in observations_by_hash.get(h, [])]
        canonical_rows = [r for r in canonical if str(r.get("product_id") or "") == pid]
        verified_rows = [
            r for r in canonical_rows
            if str(r.get("verification_status") or "").upper() in {"VERIFIED", "HUMAN_VERIFIED", "APPROVED"}
            and str(r.get("resolution_status") or "").upper() not in {"CONFLICT", "UNRESOLVED"}
        ]
        eligible = [r for r in product_rows if r["runtime_eligible"]]
        runtime_hashes = {h for h in hashes if runtime_by_hash.get(h)}
        transcript_misses = [r for r in transcript_only if r["image_sha256"] in hashes]
        observation_keys = {(o["image_sha256"], o["field"]) for o in observations}
        canonical_keys = {
            (str(r.get("image_sha256") or ""), str(r.get("field") or ""))
            for h in hashes for r in prov_by_hash.get(h, [])
        }
        unmapped = [o for o in observations if (o["image_sha256"], o["field"]) not in canonical_keys]
        runtime_mapped = [o for o in observations if runtime_by_hash.get(o["image_sha256"])]
        denom = len(observations) + len(transcript_misses)
        primary_model = _primary_model(product_rows)
        model_counts = Counter(_model(r) for r in product_rows if _model(r))
        models = sorted(model_counts)
        title = str(listing.get("input_listing_name") or "")
        product_coverage.append(
            {
                "product_id": pid,
                "url": listing.get("product_url") or "",
                "listing_title": title,
                "option": "",
                "identified_base_model": primary_model,
                "canonical_model": aliases.get(primary_model, primary_model) if primary_model else "",
                "collection_status": listing.get("collection_status") or "NO_PAGE_ARCHIVE",
                "current_page_capture_at": "NOT_RECAPTURED_2026-09-22",
                "archive_exists": bool(listing),
                "discovered_image_count": len(occs),
                "unique_image_count": len(hashes),
                "downloaded_count": len(downloaded),
                "global_dedup_reused_count": max(0, len(occs) - len(hashes)),
                "meaningful_source_images": len(meaningful),
                "analyzed_count": len(analyzed),
                "skipped_count": len(meaningful - analyzed),
                "failed_count": len(hashes - downloaded) if listing else 1,
                "placeholder_empty_count": len(placeholders),
                "unknown_review_image_count": len(transcript_misses),
                "explicit_factual_observations": len(observations),
                "structured_candidate_facts": len(observation_keys & canonical_keys),
                "verified_facts": len(verified_rows),
                "runtime_eligible_facts": len(eligible),
                "runtime_mapped_observations": len(runtime_mapped),
                "missed_facts": len(transcript_misses) + len(unmapped),
                "wrong_value_facts": 0,
                "conflicts": sum(str(r.get("resolution_status") or "").upper() in {"CONFLICT", "UNRESOLVED"} for r in canonical_rows),
                "unresolved_identity": sum("IDENTITY" in _status(r) or "UNRESOLVED" in _status(r) for r in product_rows),
                "unresolved_scope": sum("SCOPE" in _status(r) for r in product_rows),
                "unmapped_facts": len(unmapped),
                "source_capture_coverage": (len(downloaded) / len(hashes)) if hashes else 0.0,
                "extraction_coverage": (len(observations) / denom) if denom else 0.0,
                "structured_mapping_coverage": ((len(observations) - len(unmapped)) / len(observations)) if observations else 0.0,
                "verified_coverage": (min(len(verified_rows), len(observations)) / len(observations)) if observations else 0.0,
                "runtime_coverage": (len(runtime_mapped) / len(observations)) if observations else 0.0,
                "current_source_comparison": "FAILED_TO_CAPTURE" if not listing else "ARCHIVE_ONLY_CURRENT_PAGE_UNVERIFIED",
            }
        )

        for digest in sorted(hashes):
            asset = assets.get(digest, {})
            rows = [r for r in occs if r.get("image_sha256") == digest]
            image_coverage.append(
                {
                    "product_id": pid,
                    "image_hash": digest,
                    "image_url": rows[0].get("image_url", "") if rows else "",
                    "source_locator": rows[0].get("source_locator", "") if rows else "",
                    "width": asset.get("width", ""),
                    "height": asset.get("height", ""),
                    "file_size": asset.get("file_size", ""),
                    "downloaded": digest in downloaded,
                    "meaningful": digest in meaningful,
                    "placeholder": digest in placeholders,
                    "analyzed": bool(analysis_by_hash.get(digest)),
                    "observation_count": len(observations_by_hash.get(digest, [])),
                    "canonical_provenance_count": len(prov_by_hash.get(digest, [])),
                    "runtime_fact_count": len(runtime_by_hash.get(digest, [])),
                    "archive_first_seen_at": asset.get("first_seen_at", ""),
                    "archive_last_seen_at": asset.get("last_seen_at", ""),
                    "current_page_status": "NOT_RECAPTURED",
                }
            )
        for miss in transcript_misses:
            missing_facts.append(
                {
                    "product_id": pid,
                    "url": listing.get("product_url") or "",
                    "model": primary_model,
                    "image_hash_file": miss["image_sha256"],
                    "image_category": "UNKNOWN",
                    "raw_source_text": miss["raw_source_text"],
                    "expected_field": "UNDETERMINED",
                    "current_status": "EXTRACTION_MISSED",
                    "missing_reason": "PARSER_NO_RULE",
                    "recommended_action": "Human review; add a narrow parser/ontology rule only when identity, subject and scope are explicit.",
                }
            )
        for item in unmapped:
            row = {
                "product_id": pid,
                "url": listing.get("product_url") or "",
                "model": item.get("model_code") or primary_model,
                "image_hash_file": item["image_sha256"],
                "image_category": "FACTUAL_IMAGE",
                "raw_source_text": item["raw_source_text"],
                "expected_field": item["field"],
                "current_status": "UNMAPPED",
                "missing_reason": "ONTOLOGY_UNMAPPED",
                "recommended_action": "Map only after exact model/product, subject and scope verification.",
            }
            missing_facts.append(row)
            unmapped_facts.append(row)

        for row in product_rows:
            if row["runtime_eligible"]:
                continue
            runtime_gaps.append(
                {
                    "product_id": pid,
                    "model": row["model"],
                    "section": row["section"],
                    "field": row["field"],
                    "value": _text(row.get("value") or row.get("normalized_value") or row.get("raw_value")),
                    "status": _status(row),
                    "reason": _text(row.get("reason") or row.get("exclusion_reason") or row.get("withheld_reason")),
                }
            )

    conflict_rows = []
    seen_conflict_ids: set[str] = set()
    for row in canonical:
        resolution = str(row.get("resolution_status") or "").upper()
        conflict_id = str(row.get("canonical_fact_id") or "")
        if (
            resolution in {"CONFLICT", "UNRESOLVED"}
            and conflict_id not in seen_conflict_ids
        ):
            seen_conflict_ids.add(conflict_id)
            conflict_rows.append(
                {
                    "canonical_fact_id": conflict_id,
                    "product_id": str(row.get("product_id") or ""),
                    "model": str(row.get("model_code") or ""),
                    "field": row.get("field") or "",
                    "scope": row.get("scope") or "",
                    "scope_key": row.get("scope_key") or "",
                    "resolution_status": resolution,
                    "verification_status": row.get("verification_status") or "",
                    "selected_value": _text(_loads(row.get("normalized_value_json"), row.get("normalized_value_json"))),
                    "last_verified_at": row.get("last_verified_at") or "",
                    "source_recency_note": "Only trusted source timestamps may resolve an exact-identity conflict.",
                }
            )

    for row in provenance:
        source_provenance.append(
            {
                "product_id": str(row.get("product_id") or ""),
                "model": str(row.get("model_code") or ""),
                "field": row.get("field") or "",
                "canonical_fact_id": row.get("canonical_fact_id") or "",
                "image_hash": row.get("image_sha256") or "",
                "source_type": row.get("source_type") or "",
                "source_url": row.get("source_url") or "",
                "source_locator": row.get("source_locator") or "",
                "source_text": row.get("source_text") or "",
                "extraction_method": row.get("extraction_method") or "",
                "confidence": row.get("confidence") or "",
                "collected_at": row.get("collected_at") or "",
                "verification_status": row.get("verification_status") or "",
                "resolution_status": row.get("resolution_status") or "",
            }
        )

    golden_rows = []
    golden_expectations = {
        "11363535046": {
            "dimensions": "967.5 x 561.4 x 59.7 mm",
            "stand_top_height": "609.7 mm",
            "stand_bottom_height": "634.7 mm",
            "leg_spacing": "674.9 mm",
            "weight": "7.1 kg",
            "packaged_weight": "9.70 kg",
        },
        "11792827130": {
            "leg_spacing_mm": "REVIEW_CURRENT_STATE",
            "battery_count": "REVIEW_CURRENT_STATE",
            "antenna_isolator_included": "REVIEW_CURRENT_STATE",
            "power_cable_included": "REVIEW_CURRENT_STATE",
            "installation_method": "REVIEW_CURRENT_STATE",
            "smart_feature": "REVIEW_CURRENT_STATE",
        },
        "9645702227": {
            "leg_spacing_mm": "REVIEW_CURRENT_STATE",
            "battery_count": "REVIEW_CURRENT_STATE",
            "antenna_isolator_included": "REVIEW_CURRENT_STATE",
            "power_cable_included": "REVIEW_CURRENT_STATE",
            "installation_method": "REVIEW_CURRENT_STATE",
            "smart_feature": "REVIEW_CURRENT_STATE",
        },
    }
    for pid in GOLDEN_PRODUCTS:
        rows = runtime_by_product.get(pid, [])
        for expected_field, expected_value in golden_expectations[pid].items():
            expected_compact = "".join(expected_value.lower().split())
            semantic_fields = {
                "dimensions": ("dimensions_labelled", "dimensions_product", "dimensions_with_stand", "dimensions_without_stand"),
                "stand_top_height": ("stand_top_height", "stand_combined_top_height", "stand_upper_height"),
                "stand_bottom_height": ("stand_bottom_height", "stand_combined_bottom_height", "stand_lower_height"),
                "leg_spacing": ("stand_spacing", "leg_spacing_mm", "stand_leg_spacing_mm"),
                "weight": ("weight_catalog", "product_weight", "weight_kg"),
                "packaged_weight": ("packaged_weight", "package_weight", "weight_box"),
            }.get(expected_field, (expected_field,))
            matches = [
                r for r in rows
                if expected_field.lower() in r["field"].lower()
                or expected_field.lower() in _text(r.get("value")).lower()
                or (
                    expected_value != "REVIEW_CURRENT_STATE"
                    and expected_compact
                    in "".join(_text(r.get("value") or r.get("normalized_value")).lower().split())
                )
            ]
            runtime_matches = [
                r for r in matches
                if any(token in r["field"].lower() for token in semantic_fields)
                and r["runtime_eligible"]
            ]
            golden_rows.append(
                {
                    "product_id": pid,
                    "expected_field": expected_field,
                    "expected_value_or_check": expected_value,
                    "runtime_match_count": len(runtime_matches),
                    "all_match_count": len(matches),
                    "matched_fields": ", ".join(sorted({r["field"] for r in matches if r["field"]})),
                    "matched_values": " | ".join(_text(r.get("value") or r.get("normalized_value")) for r in matches[:5]),
                    "status": "RUNTIME_AVAILABLE" if runtime_matches else ("RUNTIME_NOT_EXPOSED" if matches else "NOT_FOUND"),
                }
            )

    coverage_counts = Counter()
    for row in product_coverage:
        if not row["archive_exists"]:
            coverage_counts["NO_PAGE_ARCHIVE"] += 1
        elif row["runtime_coverage"] >= 0.9 and row["missed_facts"] == 0:
            coverage_counts["HIGH"] += 1
        elif row["runtime_coverage"] >= 0.5:
            coverage_counts["MEDIUM"] += 1
        else:
            coverage_counts["LOW"] += 1

    current_hashes = {h for pid in all_product_ids for h in product_hashes.get(pid, set())}
    current_assets = {h for h in current_hashes if h in assets}
    current_placeholders = {
        h for h in current_assets
        if int(assets[h].get("width") or 0) <= 1
        or int(assets[h].get("height") or 0) <= 1
        or int(assets[h].get("file_size") or 0) < 1024
    }
    current_meaningful = current_assets - current_placeholders
    current_observations = [o for h in current_hashes for o in observations_by_hash.get(h, [])]
    current_transcript_misses = [r for r in transcript_only if r["image_sha256"] in current_hashes]
    current_unmapped = {
        (r["image_hash_file"], r["expected_field"], r["raw_source_text"])
        for r in unmapped_facts
    }
    totals = {
        "unique_products": len(product_coverage),
        "archive_products_total_historical": len(listings),
        "current_products_with_archive": sum(bool(listing_by_product.get(pid)) for pid in all_product_ids),
        "historical_archive_only_products": len(set(listing_by_product) - all_product_ids),
        "source_image_occurrences": sum(len(product_occurrences.get(pid, [])) for pid in all_product_ids),
        "unique_source_images": len(current_hashes),
        "meaningful_source_images": len(current_meaningful),
        "successfully_analyzed_images": sum(bool(analysis_by_hash.get(h)) for h in current_meaningful),
        "capture_missing": sum(r["failed_count"] for r in product_coverage),
        "explicit_factual_observations": len(current_observations),
        "structured_candidate_facts": sum(r["structured_candidate_facts"] for r in product_coverage),
        "verified_facts": sum(r["verified_facts"] for r in product_coverage),
        "runtime_eligible_facts": sum(r["runtime_eligible_facts"] for r in product_coverage),
        "extraction_missing": len(current_transcript_misses),
        "parser_ontology_missing": len(current_transcript_misses) + len(current_unmapped),
        "identity_scope_unresolved": sum(r["unresolved_identity"] + r["unresolved_scope"] for r in product_coverage),
        "conflicts": len(conflict_rows),
        "runtime_unexposed_fact_links": len(runtime_gaps),
        "wrong_value_facts": 0,
        "facts_corrected_this_audit": 0,
        "unresolved_review_records": len(missing_facts) + len(conflict_rows) + len(runtime_gaps),
    }
    totals["product_distribution"] = dict(coverage_counts)
    totals["production_readiness"] = "NOT_READY"
    totals["readiness_reason"] = (
        "Current public pages were not recaptured; archive-only source coverage and unresolved extraction/runtime gaps prevent a full-readiness claim."
    )

    return {
        "audit_metadata": {
            "audit_date": "2026-09-22",
            "catalog_source": str(catalog_path),
            "archive_source": str(archive_db),
            "runtime_source_of_truth": "data/model_data_with_color.json",
            "current_public_page_capture": "NOT_PERFORMED_READ_ONLY_COLLECTOR_NOT_PRESENT",
            "metric_note": "Coverage is based on Q&A factual observations, never OCR word count.",
        },
        "summary": totals,
        "product_coverage": product_coverage,
        "missing_facts": missing_facts,
        "wrong_values": [],
        "conflicts": conflict_rows,
        "unmapped_facts": unmapped_facts,
        "image_coverage": image_coverage,
        "runtime_gaps": runtime_gaps,
        "golden_products": golden_rows,
        "source_provenance": source_provenance,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("data/model_data_with_color.json"))
    parser.add_argument(
        "--archive-db",
        type=Path,
        default=Path("archive/data_cleanup_20260913/data/서버pc_data/product_facts.db"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_audit(args.catalog, args.archive_db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
