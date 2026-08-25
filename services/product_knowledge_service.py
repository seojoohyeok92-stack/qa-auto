"""Decides which Product Facts may be used as evidence in a customer answer.

The Product Knowledge database is a *candidate* source, not a trusted one.
This service is the single place that turns a stored row into a fact the
pipeline may quote, and it fails closed: a fact is unusable unless every
condition below is positively satisfied.

    1. verification_status = VERIFIED
    2. resolution_status is neither CONFLICT nor NEEDS_REVIEW
    3. the canonical fact is ACTIVE (not SUPERSEDED by a later run)
    4. a selected canonical value exists and is non-empty
    5. at least one ACTIVE, VERIFIED provenance row backs *that* value
    6. the fact is attached to the product the inquiry is about

The most important rule this module enforces is that **a missing fact is not
a negative fact**. The Product DB's coverage is incomplete by design -- VESA,
weight, Bluetooth, speaker and port counts are extracted for only a fraction
of products -- so "no row" means "unknown", never "not supported". Nothing
here ever produces a negative claim from an absent value; unknown fields are
simply not offered as evidence, and the existing review gates continue to
hold the answer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Sequence

from repositories.product_fact_repository import (
    ProductFactRepository,
    ProductFactsUnavailableError,
)


VERIFIED = "VERIFIED"
UNUSABLE_RESOLUTIONS = frozenset({"CONFLICT", "NEEDS_REVIEW"})
ACTIVE = "ACTIVE"

# Price, stock, review counts and delivery fees change without the product
# changing. They are real listing data but they are not what "product fact"
# means here, and quoting a cached price to a customer is its own hazard, so
# they never become answer evidence.
UNUSABLE_VOLATILITY = frozenset({"DYNAMIC_LISTING_FACT"})

# Fields whose name marks them as belonging to the bundled accessory rather
# than the display itself. A package listing carries both, and answering a
# question about the monitor with the stand's VESA value (or the reverse) is
# exactly the contamination this scope split exists to prevent.
ACCESSORY_FIELD_PREFIX = "accessory_"

BASE_DEVICE_SCOPE = "BASE_DEVICE"
ACCESSORY_SCOPE = "ACCESSORY"

# Question wording -> canonical field keys. Kept explicit rather than derived
# so that a new field cannot silently start answering questions nobody
# reviewed it for. Each entry lists the base-device fields and, where the
# bundled accessory has its own equivalent, the accessory fields separately.
FIELD_TOPICS: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    # (question keywords, base-device fields, accessory fields)
    (("hdmi", "에이치디엠아이"),
     ("hdmi_port_count", "hdmi_present", "hdmi_version"), ()),
    (("displayport", "디스플레이포트", "dp단자", "dp 단자"),
     ("displayport_present", "displayport_version"), ()),
    (("usb", "유에스비"),
     ("usb_port_count", "usb_present", "usb_version"), ()),
    (("랜포트", "랜 포트", "이더넷", "유선랜"),
     ("ethernet_port_count",), ()),
    (("베사", "vesa", "벽걸이", "브라켓", "브래킷"),
     ("vesa_mm",), ("accessory_vesa_mm",)),
    (("스피커", "소리", "사운드", "음량"),
     ("speaker_present", "speaker_output_watts", "speaker_channels"), ()),
    (("블루투스", "bluetooth"),
     ("bluetooth_version",), ()),
    (("와이파이", "wifi", "wi-fi", "무선인터넷", "무선 인터넷"),
     ("wifi_standard",), ()),
    (("무게", "중량", "kg", "몇키로", "몇 키로"),
     ("weight_with_stand_kg", "weight_without_stand_kg"),
     ("accessory_package_weight_kg", "accessory_max_load_kg")),
    (("해상도", "resolution", "fhd", "qhd", "uhd", "4k"),
     ("resolution", "resolution_class"), ()),
    (("주사율", "hz", "헤르츠", "refresh"),
     ("refresh_rate",), ()),
    (("응답속도", "응답 속도", "ms"),
     ("response_time_ms",), ()),
    (("인치", "화면크기", "화면 크기", "사이즈", "크기", "cm"),
     ("screen_size", "display_size_cm", "dimensions_with_stand_mm",
      "dimensions_without_stand_mm"), ()),
    (("패널", "ips", "va", "tn"),
     ("panel_type",), ()),
    (("명암", "명암비", "contrast"), ("contrast_ratio",), ()),
    (("밝기", "휘도", "니트", "cd"), ("brightness_typical_cd_m2",), ()),
    (("시야각",), ("viewing_angle_degrees",), ()),
    (("색재현", "색영역", "ntsc", "srgb", "색상표현"),
     ("color_gamut_ntsc_percent",), ()),
    (("hdr",), ("hdr_standard", "hdr10_plus"), ()),
    (("소비전력", "전력", "전기세", "소비 전력"),
     ("power_consumption_typical_w", "power_consumption_max_w"), ()),
    (("에너지", "등급", "1등급"), ("energy_efficiency_grade",), ()),
    (("플리커", "깜빡"), ("flicker_free",), ()),
    (("눈부심", "아이세이버", "시력보호", "블루라이트"),
     ("eye_saver_mode",), ()),
    (("높낮이", "높이조절", "높이 조절", "엘리베이션"),
     (), ("accessory_height_adjustment_mm",)),
    (("피벗", "회전", "세로", "pivot"),
     (), ("accessory_pivot_degrees", "accessory_swivel_range_degrees")),
    (("틸트", "각도조절", "각도 조절"),
     ("tilt_range_degrees",), ("accessory_tilt_range_degrees",)),
    (("스탠드", "거치대", "받침대"),
     ("stand_type", "stand_detachable"),
     ("accessory_materials", "accessory_max_load_kg", "accessory_color",
      "accessory_shelf_included", "accessory_base_plate_options_mm")),
    (("airplay", "에어플레이"), ("airplay_support",), ()),
    (("미러링", "screen mirroring", "screen_mirroring"),
     ("screen_mirroring", "mirroring_without_wifi"), ()),
    (("운영체제", "os", "타이젠", "tizen"), ("operating_system",), ()),
    (("웹브라우저", "브라우저", "인터넷 사용"), ("web_browser",), ()),
    (("모델명", "모델코드", "모델 코드", "품번", "모델번호"),
     ("model_name", "model_code", "part_number"), ()),
    (("제조사", "브랜드", "made in", "원산지", "제조국"),
     ("manufacturer", "brand", "country_of_origin"), ()),
    (("인증", "kc", "인증번호"), ("certification_number",), ()),
    (("출시", "연식", "언제 나온"), ("release_month", "manufacture_date"),
     ("accessory_release_month",)),
)


@dataclass(frozen=True)
class ProductFact:
    """One canonical fact plus the verdict on whether it may be quoted."""

    product_id: str
    listing_id: str
    model_code: str | None
    field_key: str
    value: Any
    raw_value: Any
    unit: str | None
    scope: str
    scope_key: str
    component_scope: str
    volatility: str
    verification_status: str
    resolution_status: str
    lifecycle_status: str
    canonical_fact_id: str
    value_id: str | None
    provenance: tuple[dict[str, Any], ...] = ()
    safe_for_answer: bool = False
    exclusion_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "listing_id": self.listing_id,
            "model_code": self.model_code,
            "field_key": self.field_key,
            "value": self.value,
            "unit": self.unit,
            "scope": self.scope,
            "component_scope": self.component_scope,
            "verification_status": self.verification_status,
            "resolution_status": self.resolution_status,
            "canonical_fact_id": self.canonical_fact_id,
            "value_id": self.value_id,
            "provenance_ids": [
                str(item.get("canonical_provenance_id"))
                for item in self.provenance
            ],
            "safe_for_answer": self.safe_for_answer,
            "exclusion_reason": self.exclusion_reason,
        }

    def as_prompt_line(self) -> str:
        """One evidence line for the provider prompt."""

        return (
            f"- field: {self.field_key}\n"
            f"  value: {_render_value(self.value)}\n"
            f"  verification: {self.verification_status}\n"
            f"  product_scope: {self.component_scope}"
            f" ({self.model_code or self.product_id})\n"
            f"  evidence_id: {self.canonical_fact_id}"
        )


@dataclass(frozen=True)
class ProductKnowledgeResult:
    """What the Product DB could and could not support for this inquiry."""

    product_id: str | None
    listing_id: str | None
    matched: bool
    requested_fields: tuple[str, ...] = ()
    safe_facts: tuple[ProductFact, ...] = ()
    excluded_facts: tuple[ProductFact, ...] = ()
    unavailable_reason: str | None = None
    topics: tuple[str, ...] = dataclass_field(default=())

    @property
    def has_safe_facts(self) -> bool:
        return bool(self.safe_facts)

    def safe_field_keys(self) -> frozenset[str]:
        return frozenset(item.field_key for item in self.safe_facts)

    def covers_all(self, fields: Iterable[str]) -> bool:
        wanted = {str(item) for item in fields if str(item).strip()}
        return bool(wanted) and wanted <= set(self.safe_field_keys())

    def supports_question(self, question: object) -> bool:
        """Whether safe facts cover every material claim in ``question``.

        ``has_safe_facts`` only says retrieval found something.  It must not
        let a Wi-Fi standard, size or weight vouch for an AirPlay claim, nor
        let an accessory's VESA range answer the display's own VESA holes.
        Each tuple is an alternative field group; every detected claim must
        have at least one safe field from its group.
        """

        groups = required_fact_groups(question)
        if not groups:
            return self.has_safe_facts
        safe = self.safe_field_keys()
        return all(bool(safe.intersection(group)) for group in groups)

    def evidence_text(self) -> str:
        """Flat text for the deterministic grounding check.

        The grounding check looks for the quantity *as written in the answer*
        ("2개", "180Hz"), so each value is emitted both bare and with its unit
        attached. Without the unit form, a correct answer quoting a verified
        count would be reported as ungrounded.
        """

        lines: list[str] = []
        for item in self.safe_facts:
            rendered = _render_value(item.value)
            line = f"{item.field_key}: {rendered}"
            if item.unit:
                line += f" {rendered}{item.unit}"
            lines.append(line)
        return "\n".join(lines)

    def prompt_block(self) -> str:
        if not self.safe_facts:
            return ""
        lines = "\n".join(item.as_prompt_line() for item in self.safe_facts)
        return (
            "PRODUCT_FACTS (verified for this exact product):\n"
            f"{lines}\n"
            "RULES:\n"
            "- Only the fields listed above may be stated as product fact.\n"
            "- A field that is not listed is UNKNOWN. Never say a feature is "
            "absent, unsupported or missing because it is not listed.\n"
            "- Never infer a value from another model, size or package.\n"
            "- If the customer asks for something not listed, say the exact "
            "specification needs checking instead of estimating it."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "listing_id": self.listing_id,
            "matched": self.matched,
            "requested_fields": list(self.requested_fields),
            "topics": list(self.topics),
            "safe_facts": [item.to_dict() for item in self.safe_facts],
            "excluded_facts": [item.to_dict() for item in self.excluded_facts],
            "safe_count": len(self.safe_facts),
            "excluded_count": len(self.excluded_facts),
            "unavailable_reason": self.unavailable_reason,
        }


def _render_value(value: Any) -> str:
    if isinstance(value, dict):
        if "inch" in value:
            return f"{value['inch']}인치"
        if "width" in value and "height" in value:
            return f"{value['width']}x{value['height']}"
        if "horizontal" in value and "vertical" in value:
            return f"{value['horizontal']}x{value['vertical']}mm"
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "YES" if value else "NO"
    return str(value)


def _decode(value: object) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return str(value)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().upper() in {
            "UNKNOWN", "NOT_FOUND", "SOURCE_NOT_PRESENT", "N/A", "NULL",
        }
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def fields_for_question(question: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(fields, matched topic labels) relevant to one question.

    Returns nothing when the question matches no topic, so an inquiry about
    delivery or returns never drags product specifications into its prompt.
    """

    text = " ".join(str(question or "").lower().split())
    if not text:
        return (), ()
    fields: list[str] = []
    topics: list[str] = []
    for group in required_fact_groups(text):
        fields.extend(group)
    for keywords, base_fields, accessory_fields in FIELD_TOPICS:
        if not any(keyword in text for keyword in keywords):
            continue
        topics.append(keywords[0])
        fields.extend(base_fields)
        # A bare "this product's VESA holes" means the base display even when
        # the customer says they plan to install a wall bracket.  Accessory
        # VESA is selected only when the accessory itself owns the requested
        # support range/specification.
        if base_fields == ("vesa_mm",):
            if _explicit_accessory_vesa_scope(text):
                fields.extend(accessory_fields)
            continue
        fields.extend(accessory_fields)
    return tuple(dict.fromkeys(fields)), tuple(dict.fromkeys(topics))


def _explicit_accessory_vesa_scope(text: str) -> bool:
    return any(
        phrase in text
        for phrase in (
            "브라켓의 vesa", "브라켓 vesa 지원", "브라켓 지원 vesa",
            "거치대의 vesa", "거치대 vesa 지원", "스탠드의 vesa",
            "받침대의 vesa", "지원 베사 범위", "지원 vesa 범위",
        )
    )


def required_fact_groups(question: object) -> tuple[frozenset[str], ...]:
    """Material product claims and the fields allowed to support each one."""

    text = " ".join(str(question or "").lower().split())
    groups: list[frozenset[str]] = []
    if "airplay" in text or "에어플레이" in text:
        groups.append(frozenset({"airplay_support"}))
    if any(word in text for word in ("미러링", "screen mirroring")):
        groups.append(frozenset({"screen_mirroring"}))
        if any(word in text for word in ("와이파이 없이", "wifi 없이", "wi-fi 없이")):
            groups.append(frozenset({"mirroring_without_wifi"}))
    if "hdmi" in text or "에이치디엠아이" in text:
        if any(word in text for word in ("몇 개", "몇개", "개수", "갯수", "포트 수", "단자 수")):
            groups.append(frozenset({"hdmi_port_count"}))
        else:
            groups.append(frozenset({
                "hdmi_port_count", "hdmi_present", "hdmi_version",
            }))
    if "vesa" in text or "베사" in text:
        groups.append(frozenset({
            "accessory_vesa_mm" if _explicit_accessory_vesa_scope(text)
            else "vesa_mm"
        }))
    if any(word in text for word in ("스탠드", "받침대", "다리")) and any(
        word in text
        for word in ("탈부착", "탈착", "분리", "떼었다", "떼고", "다시 장착")
    ):
        groups.append(frozenset({"stand_detachable"}))
    # Weight is the clearest case of one topic holding several facts that are
    # not interchangeable: the panel alone, the panel on its stand, and the
    # stand's own shipping carton are three different numbers. Naming the
    # scope in the question picks exactly one of them, so a body-weight
    # question can never be satisfied by the accessory's package weight.
    if any(word in text for word in ("무게", "중량", "kg", "몇키로", "몇 키로")):
        excludes_stand = any(
            word in text
            for word in (
                "스탠드 제외", "스탠드제외", "스탠드 빼고", "스탠드빼고",
                "스탠드 없이", "스탠드없이", "본체만", "패널만", "tv만",
            )
        )
        includes_stand = any(
            word in text
            for word in (
                "스탠드 포함", "스탠드포함", "스탠드까지",
                "스탠드 합쳐", "스탠드 달고", "스탠드 장착",
            )
        )
        if excludes_stand:
            groups.append(frozenset({"weight_without_stand_kg"}))
        elif includes_stand:
            groups.append(frozenset({"weight_with_stand_kg"}))
        else:
            # Unqualified "무게": either set weight answers it, but the
            # accessory carton still does not.
            groups.append(
                frozenset({"weight_with_stand_kg", "weight_without_stand_kg"})
            )
    if any(word in text for word in ("해상도", "resolution", "fhd", "qhd", "uhd", "4k")):
        groups.append(frozenset({"resolution", "resolution_class"}))
    if any(
        word in text
        for word in ("화면 크기", "화면크기", "화면 사이즈", "인치", "몇인치", "몇 인치")
    ):
        groups.append(frozenset({"screen_size", "display_size_cm"}))
    if "usb" in text or "유에스비" in text:
        # Port count is catalogued; charging power is not. Naming only the
        # claim that has a field keeps "USB 몇 개인가요?" answerable while
        # "USB-C로 65W 충전이 되나요?" stays a question for a person, instead
        # of being answered by an unrelated port count.
        if any(
            word in text
            for word in ("몇 개", "몇개", "개수", "갯수", "포트 수", "단자 수")
        ):
            groups.append(frozenset({"usb_port_count"}))
        elif any(
            word in text
            for word in ("버전", "규격", "3.0", "2.0", "타입")
        ):
            groups.append(frozenset({"usb_version"}))
    if "블루투스" in text or "bluetooth" in text:
        groups.append(frozenset({"bluetooth_present", "bluetooth_version"}))
    if any(
        word in text
        for word in ("와이파이", "wifi", "wi-fi", "무선인터넷", "무선 인터넷")
    ) and not any(word in text for word in ("미러링", "screen mirroring")):
        # "와이파이 없이도 미러링 되나요?" is a question about mirroring, not
        # about the Wi-Fi radio; the mirroring branch above already names the
        # field that answers it.
        groups.append(frozenset({"wifi_present", "wifi_standard"}))
    if any(word in text for word in ("주사율", "refresh")):
        groups.append(frozenset({"refresh_rate"}))
    return tuple(dict.fromkeys(groups))


class ProductKnowledgeService:
    """Turns stored canonical facts into evidence the pipeline may quote."""

    def __init__(self, repository: ProductFactRepository | None = None) -> None:
        self.repository = repository or ProductFactRepository()

    # ------------------------------------------------------------------
    def facts_for_inquiry(
        self,
        *,
        product_id: object,
        questions: Sequence[object] | None = None,
        question: object = "",
        model_code: object = None,
    ) -> ProductKnowledgeResult:
        """Verified facts for the fields this inquiry actually asks about."""

        key = str(product_id or "").strip()
        texts = [str(item) for item in (questions or ()) if str(item).strip()]
        if str(question or "").strip():
            texts.append(str(question))
        combined = " ".join(texts)
        fields, topics = fields_for_question(combined)

        if not key:
            return ProductKnowledgeResult(
                product_id=None, listing_id=None, matched=False,
                requested_fields=fields, topics=topics,
                unavailable_reason="NO_PRODUCT_ID",
            )
        if not fields:
            # Nothing in the question is a product-specification topic.
            return ProductKnowledgeResult(
                product_id=key, listing_id=None, matched=False,
                requested_fields=(), topics=(),
                unavailable_reason="NO_PRODUCT_FACT_TOPIC",
            )
        if not self.repository.available():
            return ProductKnowledgeResult(
                product_id=key, listing_id=None, matched=False,
                requested_fields=fields, topics=topics,
                unavailable_reason="PRODUCT_FACTS_DB_UNAVAILABLE",
            )

        try:
            listing = self.repository.listing_for_product(key)
            rows = self.repository.facts_for_product(key, fields)
        except (ProductFactsUnavailableError, Exception) as error:  # noqa: BLE001
            # A knowledge source that cannot be read must never break
            # answering; the pipeline simply gets no product evidence.
            return ProductKnowledgeResult(
                product_id=key, listing_id=None, matched=False,
                requested_fields=fields, topics=topics,
                unavailable_reason=f"LOOKUP_FAILED:{type(error).__name__}",
            )
        if listing is None:
            return ProductKnowledgeResult(
                product_id=key, listing_id=None, matched=False,
                requested_fields=fields, topics=topics,
                unavailable_reason="PRODUCT_NOT_IN_PRODUCT_DB",
            )

        provenance = self._provenance_for(rows)
        safe: list[ProductFact] = []
        excluded: list[ProductFact] = []
        expected_model = str(model_code or "").strip().upper() or None
        for row in rows:
            fact = self._judge(row, provenance, expected_model=expected_model)
            (safe if fact.safe_for_answer else excluded).append(fact)
        return ProductKnowledgeResult(
            product_id=key,
            listing_id=str(listing.get("listing_id") or "") or None,
            matched=True,
            requested_fields=fields,
            topics=topics,
            safe_facts=tuple(safe),
            excluded_facts=tuple(excluded),
        )

    # ------------------------------------------------------------------
    def _provenance_for(
        self, rows: Sequence[dict[str, Any]]
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        pairs = [
            (str(row.get("canonical_fact_id")), str(row.get("selected_value_id")))
            for row in rows
            if row.get("canonical_fact_id") and row.get("selected_value_id")
        ]
        if not pairs:
            return {}
        try:
            return self.repository.provenance_for_values(pairs)
        except Exception:  # noqa: BLE001 - absence of provenance blocks anyway
            return {}

    def _judge(
        self,
        row: dict[str, Any],
        provenance: dict[tuple[str, str], list[dict[str, Any]]],
        *,
        expected_model: str | None,
    ) -> ProductFact:
        field_key = str(row.get("field") or "")
        fact_id = str(row.get("canonical_fact_id") or "")
        value_id = row.get("selected_value_id")
        value = _decode(row.get("normalized_value_json"))
        raw_value = _decode(row.get("raw_value_json"))
        rows_provenance = tuple(
            provenance.get((fact_id, str(value_id)), ())
            if value_id else ()
        )
        component_scope = (
            ACCESSORY_SCOPE
            if field_key.startswith(ACCESSORY_FIELD_PREFIX)
            else BASE_DEVICE_SCOPE
        )
        row_model = str(row.get("model_code") or "").strip().upper() or None

        reason = self._exclusion_reason(
            row=row, value=value, provenance=rows_provenance,
            expected_model=expected_model, row_model=row_model,
        )
        return ProductFact(
            product_id=str(row.get("product_id") or ""),
            listing_id=str(row.get("listing_id") or ""),
            model_code=row.get("model_code"),
            field_key=field_key,
            value=value,
            raw_value=raw_value,
            unit=_unit_for(field_key),
            scope=str(row.get("scope") or ""),
            scope_key=str(row.get("scope_key") or ""),
            component_scope=component_scope,
            volatility=str(row.get("volatility") or ""),
            verification_status=str(row.get("verification_status") or ""),
            resolution_status=str(row.get("resolution_status") or ""),
            lifecycle_status=str(row.get("lifecycle_status") or ""),
            canonical_fact_id=fact_id,
            value_id=str(value_id) if value_id else None,
            provenance=rows_provenance,
            safe_for_answer=reason is None,
            exclusion_reason=reason,
        )

    @staticmethod
    def _exclusion_reason(
        *,
        row: dict[str, Any],
        value: Any,
        provenance: Sequence[dict[str, Any]],
        expected_model: str | None,
        row_model: str | None,
    ) -> str | None:
        """The first condition this fact fails, or None when usable."""

        if str(row.get("lifecycle_status") or "") != ACTIVE:
            return "SUPERSEDED_BY_LATER_RUN"
        if str(row.get("verification_status") or "").upper() != VERIFIED:
            return f"VERIFICATION_{row.get('verification_status') or 'UNKNOWN'}"
        resolution = str(row.get("resolution_status") or "").upper()
        if resolution in UNUSABLE_RESOLUTIONS:
            return f"RESOLUTION_{resolution}"
        if str(row.get("volatility") or "") in UNUSABLE_VOLATILITY:
            return "VOLATILE_LISTING_FACT"
        if not row.get("selected_value_id"):
            return "NO_SELECTED_VALUE"
        if _is_empty(value):
            # Explicitly *not* turned into a negative claim: an empty value
            # means the extractor found nothing, not that the feature is
            # absent.
            return "VALUE_EMPTY_OR_UNKNOWN"
        active = [
            item for item in provenance
            if str(item.get("lifecycle_status") or "") == ACTIVE
        ]
        if not active:
            return "NO_ACTIVE_PROVENANCE"
        if not any(
            str(item.get("source_status") or "").upper() == VERIFIED
            for item in active
        ):
            return "PROVENANCE_NOT_VERIFIED"
        if expected_model and row_model and expected_model != row_model:
            return "MODEL_SCOPE_MISMATCH"
        return None


_UNITS = {
    "hdmi_port_count": "개", "usb_port_count": "개", "ethernet_port_count": "개",
    "refresh_rate": "Hz", "response_time_ms": "ms",
    "weight_with_stand_kg": "kg", "weight_without_stand_kg": "kg",
    "accessory_max_load_kg": "kg", "accessory_package_weight_kg": "kg",
    "speaker_output_watts": "W", "power_consumption_typical_w": "W",
    "power_consumption_max_w": "W", "brightness_typical_cd_m2": "cd/m2",
    "accessory_height_adjustment_mm": "mm", "accessory_pivot_degrees": "도",
    "tilt_range_degrees": "도", "accessory_tilt_range_degrees": "도",
    "accessory_swivel_range_degrees": "도", "viewing_angle_degrees": "도",
    "color_gamut_ntsc_percent": "%",
}


def _unit_for(field_key: str) -> str | None:
    return _UNITS.get(field_key)
