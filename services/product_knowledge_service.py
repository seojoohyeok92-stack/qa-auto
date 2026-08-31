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
from repositories.product_catalog_repository import ProductCatalogRepository


VERIFIED = "VERIFIED"
UNUSABLE_RESOLUTIONS = frozenset({"CONFLICT", "NEEDS_REVIEW"})
ACTIVE = "ACTIVE"

# Price, stock, review counts and delivery fees change without the product
# changing. They are real listing data but they are not what "product fact"
# means here, and quoting a cached price to a customer is its own hazard, so
# they never become answer evidence.
UNUSABLE_VOLATILITY = frozenset({"DYNAMIC_LISTING_FACT"})

# ``listings.collection_status`` is written by the collector as exactly one of
# two values (see the Product DB collector: a page that answers with a product
# body is COLLECTION_SUCCESS, anything else is COLLECTION_FAILED). Only the
# first means "this listing was read as it stands today".
COLLECTION_SUCCESS = "COLLECTION_SUCCESS"

# What a listing that could not be read today may still be quoted for.
# A panel's size, ports and dimensions do not change when the listing stops
# being collectible, so static product facts survive and are still judged by
# every other condition. The listing's *own* terms do not survive: delivery
# cutoffs, return windows, service phone numbers and partner status describe an
# offer that is no longer confirmed to exist, and stating a stale one to a
# customer is a promise the seller may not be able to keep.
STALE_WHEN_NOT_CURRENT = frozenset({
    "DYNAMIC_LISTING_FACT", "SEMI_STATIC_POLICY_FACT",
})

# Who made it, who brands it, where it was made, and what it is called. These
# answer questions about identity, and identity is the one thing a package
# listing cannot lend to the things bundled inside it. The model fields joined
# the set once "리모컨 모델명이 뭐예요?" was seen returning the television's
# model_name -- the right field for the wrong subject.
IDENTITY_FIELDS = frozenset({
    "brand", "manufacturer", "country_of_origin",
    "model_name", "model_code", "part_number",
})

# Words that name something bundled with the display rather than the display.
# Deliberately short and literal: this list only decides whether to *withhold*
# an identity value, never what to answer.
COMPONENT_TERMS = (
    "셋톱박스", "셋탑박스", "set-top", "stb", "스탠드", "거치대", "받침대",
    "모니터암", "브라켓", "브래킷", "액세서리", "악세서리", "부속품", "구성품",
    "리모컨", "리모콘",
)

# The customer pointing at the listing itself ("이 스탠드", "본 상품"). A
# stand-only listing's own brand question must keep working, and this is the
# wording that distinguishes it from asking about a bundled part.
SELF_REFERENCE_MARKERS = ("이 ", "본 ", "해당 ", "이번 ")

# Samsung sells these as product lines, and the Product DB stores them in
# ``brand`` beside real makers -- brand is "오디세이" for 8 listings and "삼성"
# for 63. A line name in brand therefore proves the line, never the maker.
PRODUCT_LINE_TERMS = (
    "오디세이", "스마트모니터", "스마트 모니터", "무빙스타일", "무빙 스타일",
)

# Fields whose name marks them as belonging to the bundled accessory rather
# than the display itself. A package listing carries both, and answering a
# question about the monitor with the stand's VESA value (or the reverse) is
# exactly the contamination this scope split exists to prevent.
ACCESSORY_FIELD_PREFIX = "accessory_"

# A reference to an installer can describe either the installation method or
# the customer's appointment.  Date/time wording belongs to DPS/order routing
# and must not request a static installation-method fact.
INSTALLATION_SCHEDULE_MARKERS = (
    "언제", "몇 시", "몇시", "날짜", "예정일", "내일", "오늘",
    "방문 시간", "방문시간",
)

# The quantities a package listing measures twice, once per subject: the
# display has a weight and a VESA pattern, and so does the stand bundled with
# it. The field names keep them apart, but the customer's wording is what
# decides which subject was asked about, so these fields may only be offered
# when the question's subject matches the field's scope. "거치대가 몇 kg까지
# 버티나요?" answered with the television's 5.5 kg is the failure this
# prevents -- and the reverse, a plain "무게 알려주세요" answered with the
# stand's load rating, is the same mistake pointing the other way.
SUBJECT_SENSITIVE_FIELDS = frozenset({
    "vesa_mm", "weight_with_stand_kg", "weight_without_stand_kg",
    "package_weight_kg",
    "accessory_vesa_mm", "accessory_weight_kg",
    "accessory_package_weight_kg", "accessory_max_load_kg",
})

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
    # "전기 얼마나 먹나요" is the same question as "소비전력이 얼마인가요"; the
    # phrasing is kept whole rather than keyed on "전기" alone, which would also
    # catch 전기 케이블 and 전기 코드. power_consumption_dpms_w (standby) stays
    # out: it answers a different question than typical/max draw.
    (("소비전력", "전력", "전기세", "소비 전력", "전기 얼마나", "전기 많이",
      "전기 요금", "전기요금"),
     ("power_consumption_typical_w", "power_consumption_max_w"), ()),
    # Which cable is in the box is not how much electricity the panel uses.
    (("전원 케이블", "전원케이블", "파워 케이블", "전원선"),
     ("power_cable_included", "power_cable_length_m"), ()),
    (("hdmi 케이블", "hdmi케이블"), ("hdmi_cable_included",), ()),
    # "리모컨 포함인가요" is answerable; "리모컨이 안 왔어요" is a missing-item
    # report and is refused upstream. Only the inclusion phrasings are keyed,
    # never the bare word, so the two never share a route.
    (("리모컨 포함", "리모콘 포함", "리모컨 들어", "리모콘 들어",
      "리모컨 동봉", "리모콘 동봉", "리모컨도 주", "리모컨도 오",
      "리모컨도 같이", "리모콘도 같이", "리모컨도 배송", "리모컨 같이 오"),
     ("remote_control_included",), ()),
    # "휴대폰이랑 연결돼요?" does not name a method. Bluetooth, mirroring and
    # wireless display are all legitimate readings, so every connection fact
    # the product actually has is offered rather than one guessed for it.
    (("휴대폰 연결", "휴대폰이랑 연결", "휴대폰과 연결", "핸드폰 연결",
      "스마트폰 연결", "폰 연결", "휴대폰 연동", "핸드폰 연동"),
     ("bluetooth_present", "bluetooth_version", "screen_mirroring",
      "wireless_display", "mobile_wireless_connection"), ()),
    (("폰 화면", "휴대폰 화면", "핸드폰 화면", "스마트폰 화면"),
     ("screen_mirroring", "mirroring_without_wifi", "wireless_display"), ()),
    # A named service and the category are different claims. "OTT 지원" must
    # never be read as "YouTube 지원", so each keys only the field that can
    # actually say so.
    (("유튜브", "youtube"), ("youtube_supported",), ()),
    (("넷플릭스", "넷플"), ("ott_supported_services",), ()),
    (("ott", "오티티"), ("ott_supported", "ott_supported_services"), ()),
    (("tv플러스", "tv 플러스", "티비플러스", "티비 플러스"), ("tv_plus",), ()),
    # How the product is installed is a product fact. When it is installed is
    # the customer's own order, which stays with DPS -- so the phrasings here
    # all name the method, never a date or a visit.
    # installation_method holds PROFESSIONAL_TECHNICIAN_REQUIRED for 35
    # products, which is exactly what "기사님이 설치해주시나요?" asks. The
    # phrasings name who installs or how -- never when, so "기사님 언제
    # 오나요?" keeps going to DPS.
    (("설치 방법", "설치방법", "어떻게 설치", "설치는 어떻게", "설치 방식",
      "자가 설치", "자가설치", "직접 설치", "혼자 설치", "혼자서 설치",
      "설치 어떻게",
      "기사님이 설치", "기사가 설치", "기사님이 해주", "기사님 설치해",
      "기사님이 오셔서 설치", "설치기사", "설치 기사"),
     ("installation_method", "package_professional_installation"), ()),
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
    # brand, manufacturer and country_of_origin answer three different
    # questions and are kept apart. "브랜드가 뭐예요" must not be answered with
    # the manufacturing company, and -- the reason this split exists -- "삼성
    # 제품인가요" must not be answered from brand, because brand holds product
    # lines like 오디세이 as often as it holds a maker's name.
    (("브랜드",), ("brand",), ()),
    (("제조사", "제조원", "만든 곳", "만든곳", "made in", "제조업체"),
     ("manufacturer",), ()),
    (("원산지", "제조국", "생산지", "어디서 만든", "어디서 생산"),
     ("country_of_origin",), ()),
    # "삼성 제품인가요" / "삼성전자에서 나온 건가요" -- an identity question
    # about the maker, which only manufacturer can settle.
    (("삼성 제품", "삼성제품", "삼성전자 제품", "삼성전자제품",
      "삼성에서 만든", "삼성전자에서 만든", "삼성 정품", "삼성정품"),
     ("manufacturer",), ()),
    # Product-line questions ("오디세이 맞나요"). brand and model_name are the
    # only two places a line name is recorded; whether either actually carries
    # the asked line is checked after retrieval, in _product_line_reason.
    (PRODUCT_LINE_TERMS, ("brand", "model_name"), ()),
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
    # Which listing state the facts were judged against, and whether the
    # question was read as being about a bundled component. Both are recorded
    # so a diagnostic can say *why* a fact was withheld without re-deriving it.
    collection_status: str | None = None
    component_subject: bool = False

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
            "PRODUCT_CATALOG_JSON (exact matched product catalog evidence):\n"
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
            "collection_status": self.collection_status,
            "component_subject": self.component_subject,
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
        if (
            "installation_method" in base_fields
            and any(marker in text for marker in INSTALLATION_SCHEDULE_MARKERS)
        ):
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
    # The JSON catalog has literal RF and stand-spacing descriptions even
    # though the retired Product Facts ontology did not model them.
    if any(word in text for word in ("rf", "coax", "antenna", "\ub3d9\ucd95", "\uc548\ud14c\ub098")):
        fields.append("rf_terminal")
        topics.append("rf")
    if any(word in text for word in ("stand spacing", "leg spacing", "stand gap", "\ub2e4\ub9ac \uac04\uaca9", "\uc2a4\ud0e0\ub4dc \uac04\uaca9", "\ubc1b\uce68\ub300 \uac04\uaca9")):
        fields.append("stand_spacing")
        topics.append("stand_spacing")
    if any(word in text for word in ("lan", "ethernet", "\uc720\uc120\ub79c")):
        fields.append("ethernet_present")
        topics.append("ethernet")
    return tuple(dict.fromkeys(fields)), tuple(dict.fromkeys(topics))


# Wording that names a component in order to take it *out* of the question:
# "스탠드 제외하고 본체 무게가 몇 kg인가요?" is asked about the display, and
# the word 스탠드 appears only to say which part is not being weighed.
EXCLUSION_MARKERS = ("제외", "빼고", "빼면", "없이", "미포함", "제거")


def _is_excluded_mention(text: str, term: str) -> bool:
    """Whether every mention of ``term`` is one the customer excluded."""

    start = 0
    while (index := text.find(term, start)) != -1:
        tail = text[index + len(term):index + len(term) + 6]
        if not any(marker in tail for marker in EXCLUSION_MARKERS):
            return False
        start = index + len(term)
    return True


def asks_about_a_bundled_component(question: object) -> bool:
    """Whether the question's subject is something bundled, not the listing.

    A package listing carries one brand and one manufacturer, and they describe
    what the seller lists -- not what is in the box beside it. Listing
    11848813000 is sold as "삼성 85인치 4K UHD 스마트 비즈니스TV+OTT 구글TV
    셋탑박스" with brand 삼성 / manufacturer 삼성전자, while the set-top box
    that ships with it is SHAKS, made by 이노피아테크. Answering "셋톱박스도
    삼성인가요?" from the listing's own identity states the wrong maker.

    The listing's product type cannot decide this: three of the four listings
    classified SETTOP_ACCESSORY are television packages, not set-top boxes. So
    the decision is made from the question, and it is made fail-closed --
    naming a component withholds the listing identity unless the customer
    points at the listing itself ("이 스탠드", "본 상품"), which is how a
    stand-only or set-top-only listing keeps answering its own brand question.
    """

    text = " ".join(str(question or "").lower().split())
    if not text:
        return False
    mentioned = [term for term in COMPONENT_TERMS
                 if term in text and not _is_excluded_mention(text, term)]
    if not mentioned:
        return False
    for term in mentioned:
        for marker in SELF_REFERENCE_MARKERS:
            if marker + term in text:
                return False
    return True


def _product_line_terms_in(question: object) -> tuple[str, ...]:
    text = " ".join(str(question or "").lower().split())
    return tuple(term for term in PRODUCT_LINE_TERMS if term in text)


def _mentions_line(value: Any, terms: Iterable[str]) -> bool:
    """Whether a stored value spells out one of the asked product lines."""

    rendered = _render_value(value).lower().replace(" ", "")
    return any(str(term).lower().replace(" ", "") in rendered for term in terms)


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
                frozenset({"weight_with_stand_kg", "weight_without_stand_kg", "weight_catalog"})
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
    # English model/port spellings are also accepted by the Korean support
    # flow.  The catalog only supplies literal matching JSON evidence.
    if any(word in text for word in ("rf", "coax", "antenna")):
        groups.append(frozenset({"rf_terminal"}))
    if "lan" in text or "ethernet" in text:
        groups.append(frozenset({"ethernet_port_count", "ethernet_present"}))
    if any(word in text for word in ("stand spacing", "leg spacing", "stand gap")):
        groups.append(frozenset({"stand_spacing"}))
    return tuple(dict.fromkeys(groups))


class ProductKnowledgeService:
    """Turns stored canonical facts into evidence the pipeline may quote."""

    def __init__(
        self,
        repository: ProductFactRepository | None = None,
        catalog_repository: ProductCatalogRepository | None = None,
    ) -> None:
        # An explicitly supplied ProductFactRepository remains available for
        # historical diagnostics/tests only.  The production default never
        # constructs or reads product_facts.db.
        self.repository = repository
        self.catalog_repository = catalog_repository or ProductCatalogRepository()

    # ------------------------------------------------------------------
    def facts_for_inquiry(
        self,
        *,
        product_id: object,
        questions: Sequence[object] | None = None,
        question: object = "",
        model_code: object = None,
        product_name: object = "",
        option_name: object = "",
    ) -> ProductKnowledgeResult:
        """Verified facts for the fields this inquiry actually asks about."""

        key = str(product_id or "").strip()
        texts = [str(item) for item in (questions or ()) if str(item).strip()]
        if str(question or "").strip():
            texts.append(str(question))
        combined = " ".join(texts)
        fields, topics = fields_for_question(combined)

        if self.repository is None:
            return self._catalog_facts_for_inquiry(
                product_id=key,
                product_name=product_name,
                option_name=option_name,
                model_code=model_code,
                fields=fields,
                topics=topics,
                combined=combined,
            )

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
        collection_status = str(listing.get("collection_status") or "") or None
        component_subject = asks_about_a_bundled_component(combined)
        product_lines = _product_line_terms_in(combined)
        for row in rows:
            fact = self._judge(
                row, provenance, expected_model=expected_model,
                collection_status=collection_status,
                component_subject=component_subject,
                product_lines=product_lines,
            )
            (safe if fact.safe_for_answer else excluded).append(fact)
        return ProductKnowledgeResult(
            product_id=key,
            listing_id=str(listing.get("listing_id") or "") or None,
            matched=True,
            requested_fields=fields,
            topics=topics,
            safe_facts=tuple(safe),
            excluded_facts=tuple(excluded),
            collection_status=collection_status,
            component_subject=component_subject,
        )

    def _catalog_facts_for_inquiry(
        self,
        *,
        product_id: str,
        product_name: object,
        option_name: object,
        model_code: object,
        fields: tuple[str, ...],
        topics: tuple[str, ...],
        combined: str,
    ) -> ProductKnowledgeResult:
        if not fields:
            return ProductKnowledgeResult(
                product_id=product_id or None, listing_id=None, matched=False,
                requested_fields=(), topics=(),
                unavailable_reason="NO_PRODUCT_CATALOG_TOPIC",
            )
        match = self.catalog_repository.match(
            product_name=product_name, option_name=option_name,
            model_code=model_code,
        )
        if not match.record or not match.model_key:
            return ProductKnowledgeResult(
                product_id=product_id or None, listing_id=None, matched=False,
                requested_fields=fields, topics=topics,
                unavailable_reason=match.reason,
            )
        if asks_about_a_bundled_component(combined):
            return ProductKnowledgeResult(
                product_id=product_id or match.model_key, listing_id=match.model_key,
                matched=True, requested_fields=fields, topics=topics,
                collection_status="CATALOG_JSON", component_subject=True,
                unavailable_reason="COMPONENT_SUBJECT_UNRESOLVED",
            )
        facts = self._catalog_facts(
            product_id=product_id or match.model_key,
            model_key=match.model_key, record=match.record, fields=fields,
        )
        return ProductKnowledgeResult(
            product_id=product_id or match.model_key, listing_id=match.model_key,
            matched=True, requested_fields=fields, topics=topics,
            safe_facts=tuple(facts), collection_status="CATALOG_JSON",
        )

    @staticmethod
    def _catalog_facts(
        *, product_id: str, model_key: str, record: dict[str, Any],
        fields: Sequence[str],
    ) -> list[ProductFact]:
        """Expose literal JSON values only; absent fields remain UNKNOWN."""
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
        token_fields = {
            "hdmi_present": ("HDMI",), "usb_present": ("USB",),
            "ethernet_present": ("LAN", "랜", "ETHERNET"),
            "rf_terminal": ("RF", "동축", "안테나"),
            "bluetooth_present": ("BLUETOOTH", "블루투스"),
            "wifi_present": ("WI-FI", "WIFI", "와이파이", "무선랜"),
            "stand_spacing": ("스탠드 간격", "다리 간격", "다리 사이", "받침대 간격"),
        }
        upper_spec = spec.upper()
        for field_key, tokens in token_fields.items():
            if any(token.upper() in upper_spec for token in tokens):
                direct[field_key] = spec
        requested = set(fields)
        # Generic catalog weight is deliberately not used for explicit
        # stand-included/excluded questions because its scope is not encoded.
        if "weight_catalog" not in requested:
            direct.pop("weight_catalog", None)
        result: list[ProductFact] = []
        for field_key in requested:
            value = direct.get(field_key)
            if _is_empty(value):
                continue
            result.append(ProductFact(
                product_id=product_id, listing_id=model_key, model_code=model_key,
                field_key=field_key, value=value, raw_value=value,
                unit=_unit_for(field_key), scope="MODEL_CATALOG",
                scope_key=model_key, component_scope=BASE_DEVICE_SCOPE,
                volatility="STATIC_PRODUCT_FACT", verification_status="CATALOG_JSON",
                resolution_status="CATALOG_JSON", lifecycle_status=ACTIVE,
                canonical_fact_id=f"catalog:{model_key}:{field_key}", value_id=None,
                provenance=(), safe_for_answer=True,
            ))
        return result

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
        collection_status: str | None = None,
        component_subject: bool = False,
        product_lines: tuple[str, ...] = (),
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
            collection_status=collection_status,
            component_scope=component_scope,
            component_subject=component_subject,
            product_lines=product_lines,
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
        collection_status: str | None = None,
        component_scope: str = BASE_DEVICE_SCOPE,
        component_subject: bool = False,
        product_lines: tuple[str, ...] = (),
    ) -> str | None:
        """The first condition this fact fails, or None when usable."""

        if str(row.get("lifecycle_status") or "") != ACTIVE:
            return "SUPERSEDED_BY_LATER_RUN"
        if str(row.get("verification_status") or "").upper() != VERIFIED:
            return f"VERIFICATION_{row.get('verification_status') or 'UNKNOWN'}"
        resolution = str(row.get("resolution_status") or "").upper()
        if resolution in UNUSABLE_RESOLUTIONS:
            return f"RESOLUTION_{resolution}"
        volatility = str(row.get("volatility") or "")
        if volatility in UNUSABLE_VOLATILITY:
            return "VOLATILE_LISTING_FACT"
        # The listing could not be read as it stands today -- it was delisted,
        # blocked or otherwise unreadable at collection time. Anything that
        # describes the listing rather than the product is no longer current.
        # Unknown, empty and unexpected statuses take this branch too: a status
        # we cannot recognise is not evidence that the listing is live.
        if str(collection_status or "").strip().upper() != COLLECTION_SUCCESS:
            if volatility in STALE_WHEN_NOT_CURRENT:
                return "COLLECTION_STATUS_NOT_CURRENT"
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
        # The conditions above ask "is this fact sound?". The two below ask
        # "does this fact answer *this* question?", so they run last, on a fact
        # already known to be verified and backed.
        field_key = str(row.get("field") or "")
        # A bundled component's maker is not the listing's maker. Withheld, not
        # denied: the field simply becomes unknown for this question.
        if (
            component_subject
            and component_scope == BASE_DEVICE_SCOPE
            and field_key in IDENTITY_FIELDS | SUBJECT_SENSITIVE_FIELDS
        ):
            return "COMPONENT_SUBJECT_UNRESOLVED"
        # And the same boundary from the other side. Nothing here was asked
        # about the stand, so the stand's own weight -- or the load it is rated
        # to carry, which is not a weight at all -- may not stand in for the
        # display's. Withheld, never denied.
        if (
            not component_subject
            and component_scope == ACCESSORY_SCOPE
            and field_key in SUBJECT_SENSITIVE_FIELDS
        ):
            return "ACCESSORY_SUBJECT_NOT_ASKED"
        # A product-line question may only be grounded by a stored value that
        # actually spells the line out. Absence stays unknown and never becomes
        # "this is not an 오디세이".
        if (
            product_lines
            and field_key in {"brand", "model_name"}
            and not _mentions_line(value, product_lines)
        ):
            return "PRODUCT_LINE_NOT_IN_VALUE"
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
