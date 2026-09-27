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
import re
import unicodedata
from dataclasses import dataclass, field as dataclass_field, replace
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

from repositories.product_catalog_repository import (
    EXACT,
    UNIQUE_MATCH,
    ProductCatalogRepository,
    StatedModels,
    canonical_model_identity,
    normalize_model,
)


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

# These are the integrated JSON's own operational decisions.  Candidate is
# deliberately not renamed to approved: its stored status travels with every
# prompt fact.  WITHHELD/EXCLUDED rows remain in the JSON for audit only.
_RUNTIME_PRODUCT_KNOWLEDGE_STATUSES = frozenset({
    "CANDIDATE_NOT_APPROVED", "CANDIDATE_REVIEW_RESOLVED",
})
_PRODUCT_KNOWLEDGE_SECTIONS = (
    "model_facts", "listing_facts", "bundle_accessory_facts", "policy_facts",
)
_PRODUCT_KNOWLEDGE_PROMPT_LIMIT = 120

# Rows that exist so the collector can join its own tables, not because a
# customer could ever be told them: surrogate keys, the SEO title, and the
# listing thumbnail's URL and pixel dimensions.
#
# These are recognised by *shape*, never by a list of names. A name list would
# have to grow every time the collector adds a column, and the next column it
# adds would reach the prompt unreviewed in the meantime. Shape also keeps this
# rule honest about what it is: it says nothing about which question is being
# asked, so it can never become the field-selection judgement this module is
# handing to GPT ②. Everything a person could conceivably ask about -- and that
# includes the store's own policy rows -- goes to the model.
_INTERNAL_FIELD_PREFIXES = ("seo_", "representative_image")
_INTERNAL_FIELD_SUFFIXES = ("_id",)
_INTERNAL_VALUE_PREFIXES = ("http://", "https://")


def _is_internal_metadata(field_key: str, value: Any) -> bool:
    key = str(field_key or "")
    if key.startswith(_INTERNAL_FIELD_PREFIXES):
        return True
    if key.endswith(_INTERNAL_FIELD_SUFFIXES):
        return True
    return str(_render_value(value)).strip().lower().startswith(
        _INTERNAL_VALUE_PREFIXES
    )


# What kind of thing a stored row is, said in the model's own terms.
#
# The Product DB already separates these and the distinction matters to an
# answer: a panel's resolution is true of the device wherever it is sold, while
# "무료배송" is this listing's current offer. Both may be read; only the first
# is a property of the product. Labelling is CODE's job here -- deciding which
# one answers the customer is not.
_KNOWLEDGE_KINDS = {
    "STATIC_PRODUCT_FACT": "DEVICE_SPECIFICATION",
    "SEMI_STATIC_POLICY_FACT": "LISTING_POLICY_SNAPSHOT",
}

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
    (("인치", "화면크기", "화면 크기", "사이즈", "크기", "제품 크기",
      "가로", "세로", "높이", "깊이", "두께", "센치", "cm", "mm"),
     ("screen_size", "display_size_cm", "dimensions_labelled",
      "dimensions_product", "dimensions_with_stand", "dimensions_without_stand",
      "dimensions_with_stand_mm", "dimensions_without_stand_mm", "width",
      "screen_height_without_stand", "depth_screen", "total_height_with_stand",
      "stand_depth"), ()),
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
      "기사님이 오셔서 설치", "설치기사", "설치 기사",
      # "설치는 누가 하나요?" names who installs as plainly as the phrasings
      # above and matched none of them, so it reached no installation field.
      # It asks who installs, not what installation costs, so it is routed
      # here and nowhere else.
      "설치는 누가", "누가 설치", "설치 누가", "설치 주체",
      "설치해 주시나요", "설치해주시나요"),
     ("installation_method", "package_professional_installation"), ()),
    # "설치 포함인가요?" is the one phrasing that genuinely asks both: whether
    # the product is installed for the customer, and whether that is included
    # rather than charged. It is the only place the two fields travel together.
    (("설치 포함", "설치포함"),
     ("installation_method", "package_professional_installation",
      "additional_cost"), ()),
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
    (("운영체제", "os", "타이젠", "tizen"), ("operating_system", "os"), ()),
    # What this listing charges on top of the price. The record holds it as
    # additional_cost ("무료설치배송"), and nothing routed a question to it, so
    # "설치비가 추가로 드나요?" read the record without ever asking for the one
    # field that answers it.
    #
    # This is about price, not about who installs. Who installs is answered by
    # the store's own confirmed statement in ``answer/engine.py`` -- "해당
    # 상품은 삼성 기사님이 방문하여 설치하는 상품입니다." -- which GPT (2)
    # receives as a candidate on a product predicate. Routing an
    # installer-identity question to additional_cost would offer a delivery
    # term as evidence for who does the work, which it is not.
    (("설치비", "설치 비용", "설치비용", "추가 비용", "추가비용",
      "추가 금액", "추가금액", "별도 비용", "별도비용"),
     ("additional_cost",), ()),
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


# How the target of a Product Knowledge lookup was decided.
#
# LISTING_FALLBACK          the inquiry named no model; the listing is the subject
# INQUIRY_EXPLICIT_MODEL    the inquiry named one model and the catalogue knows it
# MULTI_MODEL_COMPARISON    the inquiry named more than one; each is kept whole
# UNRESOLVED_EXPLICIT_MODEL the inquiry named a model this catalogue does not
#                           know. Nothing is reported: the listing's record is
#                           not an answer about a model the customer did not ask
#                           about, and substituting it is the failure this
#                           resolution exists to prevent.
LISTING_FALLBACK = "LISTING_FALLBACK"
INQUIRY_EXPLICIT_MODEL = "INQUIRY_EXPLICIT_MODEL"
MULTI_MODEL_COMPARISON = "MULTI_MODEL_COMPARISON"
UNRESOLVED_EXPLICIT_MODEL = "UNRESOLVED_EXPLICIT_MODEL"

# "이 제품이랑 43BEF 차이가 뭐예요?" names one model and points at another.
# Deixis, not a product: the phrase means whichever listing the customer is
# writing from, so it adds the listing as a second target and never elects a
# particular model.
_REFERS_TO_THIS_LISTING = re.compile(
    r"이\s*(제품|상품|모델|기종)|현재\s*(제품|상품|모델)|본\s*(제품|상품)"
    r"|지금\s*보고\s*있는"
)


def _settled_identity(model_key: object, repository: Any) -> str | None:
    """The canonical identity of a model key, in the unit targets are reported in.

    ``listing_model`` and ``resolved_pk_targets`` are read together and compared
    to each other, so both must be identities. Reporting a catalogue key in one
    and an identity in the other made an ordinary listing question look like a
    cross-model one.
    """

    if not model_key:
        return None
    try:
        aliases = repository.catalog().get("aliases")
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        aliases = None
    return canonical_model_identity(model_key, aliases=aliases) or str(model_key)


@dataclass(frozen=True)
class InquiryTarget:
    """One product a lookup should be run for."""

    model_code: str
    identity: str | None
    # The listing id travels only with the listing's own target. A listing's
    # non-model evidence -- its listing_facts, its policy_facts -- is keyed by
    # product id and describes *that* listing, so carrying the id onto another
    # model's lookup would readmit exactly the evidence being excluded.
    product_id: str
    is_listing: bool


@dataclass(frozen=True)
class TargetResolution:
    targets: tuple[InquiryTarget, ...]
    reason: str
    listing_model: str | None
    listing_identity: str | None
    stated: StatedModels

    @property
    def inquiry_target_models(self) -> tuple[str, ...]:
        return tuple(self.stated.tokens)

    @property
    def resolved_pk_targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            target.identity or target.model_code for target in self.targets
        ))


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
    subject: str | None = None
    applies_to_product_id: str | None = None
    source_type: str | None = None

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
            "subject": self.subject,
            "applies_to_product_id": self.applies_to_product_id,
            "source_type": self.source_type,
        }

    @property
    def knowledge_kind(self) -> str:
        """Device specification, or this listing's own current offer."""

        return _KNOWLEDGE_KINDS.get(self.volatility, "PRODUCT_RECORD")

    def as_prompt_line(self, also_reported_as: "Sequence[str] | None" = None) -> str:
        """One evidence line for the provider prompt.

        ``also_reported_as`` names the other fields the record states with this
        same value. They are carried here rather than dropped: the value is
        written once, and which fields it was recorded under stays visible.
        """

        others = (
            f"  also_reported_as: {', '.join(sorted(set(also_reported_as)))}\n"
            if also_reported_as else ""
        )
        return (
            f"- field: {self.field_key}\n"
            f"{others}"
            f"  value: {_render_value(self.value)}\n"
            f"  verification: {self.verification_status}\n"
            f"  kind: {self.knowledge_kind}\n"
            f"  subject: {self.subject or 'MAIN_PRODUCT'}\n"
            f"  product_scope: {self.component_scope}"
            f" ({self.model_code or self.product_id})\n"
            f"  evidence_id: {self.canonical_fact_id}"
        )

    def as_prompt_fact(self) -> dict[str, Any]:
        """The structured mirror of ``as_prompt_line``.

        ``to_dict`` carries the internal identifiers this service needs to
        explain itself -- canonical fact and value ids, every provenance id,
        the flags that are constant across safe facts. Measured over the whole
        Product DB with the record read whole, that shape costs a median of
        25,600 characters per product against 4,700 here, and the prompt was
        already carrying the same values a second time in the text block. The
        identifiers stay in ``to_dict`` for telemetry and the UI; the model
        gets what it can act on.
        """

        fact: dict[str, Any] = {
            "field_key": self.field_key,
            "value": self.value,
            "verification": self.verification_status,
            "kind": self.knowledge_kind,
            "product_scope": self.component_scope,
            "scope": self.scope,
        }
        if self.model_code:
            fact["model_code"] = self.model_code
        if self.unit:
            fact["unit"] = self.unit
        if self.subject:
            fact["subject"] = self.subject
        if self.applies_to_product_id:
            fact["applies_to_product_id"] = self.applies_to_product_id
        if self.source_type:
            fact["source_type"] = self.source_type
        return fact


# How much of the draft prompt this product's verified record may occupy.
#
# The record is not the only evidence the answer needs.  When a listing's
# Product Knowledge became fully reachable, one product's facts grew to 37,225
# characters of a 60,000 character prompt and the budget evicted every
# retrieved Learning answer the inquiry had -- including the ones that
# answered it.  Facts and Learning are ranked separately and then merged, so
# neither can starve the other by arriving first.
PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS = 24_000

# What the rules footer of ``prompt_block`` costs, whatever the fact list is.
_PROMPT_BLOCK_OVERHEAD_CHARS = 2_000

# Two different fields holding the same text are only treated as one fact when
# the text is long enough for the repeat to be what it looks like: a block of
# source copy landing under several field names.  Short values collide by
# coincidence -- ``eco_sensor`` and ``mini_wallmount`` are both "있음", and
# front and stand colour are both BLACK -- and folding those together would
# merge two facts that happen to agree, not one fact said twice.
_MERGEABLE_VALUE_CHARS = 80

# Verification states, most settled first.  Ordering is by how much of the
# record has been confirmed, not by whether a fact may be quoted -- a fact
# that reaches here is already safe.
_VERIFICATION_RANK = (
    "VERIFIED", "APPROVED", "CATALOG_JSON",
    "CANDIDATE_REVIEW_RESOLVED", "CANDIDATE_NOT_APPROVED",
)
# Identity scopes, narrowest first.
_SCOPE_RANK = ("EXACT_MODEL", "PRODUCT_LISTING", "MODEL_CATALOG")

_WORD = re.compile(r"[0-9A-Za-z\uac00-\ud7a3]+")


def _terms(text: object) -> set[str]:
    return {word.lower() for word in _WORD.findall(str(text or "")) if len(word) > 1}


def _normalized_value(value: object) -> str:
    """A fact's value with spacing and case removed, for spotting repeats."""

    return re.sub(r"\s+", "", str(value or "")).lower()


def _rank_index(value: object, order: tuple[str, ...]) -> int:
    text = str(value or "").upper()
    return order.index(text) if text in order else len(order)


def _fact_relevance(
    fact: "ProductFact", *, asked: set[str], wanted: set[str],
) -> tuple:
    """How directly this fact answers the inquiry. Larger sorts first.

    The first three components are about the question, the rest about the
    record.  A fact the inquiry actually asked for outranks a better-verified
    fact it did not, because the prompt's job is to answer this question.
    """

    field_terms = _terms(fact.field_key.replace("_", " "))
    return (
        1 if fact.field_key in asked else 0,
        len(field_terms & wanted),
        len(_terms(fact.value) & wanted),
        -_rank_index(fact.verification_status, _VERIFICATION_RANK),
        -_rank_index(fact.scope, _SCOPE_RANK),
        -len(_normalized_value(fact.value)),
    )


def select_prompt_facts(
    facts: "Sequence[ProductFact]",
    *,
    requested_fields: "Sequence[str]" = (),
    topics: "Sequence[str]" = (),
    question: object = "",
    budget_chars: int = PRODUCT_KNOWLEDGE_PROMPT_BUDGET_CHARS,
) -> tuple[list["ProductFact"], list[dict[str, Any]], dict[str, Any]]:
    """Choose the part of the record this prompt can afford to carry.

    Three steps, in this order.  Repeats are collapsed first, because the same
    value under a second field name costs the full price and adds nothing --
    one listing spent 6,850 characters restating a single catalogue blob under
    five presence flags.  What is left is ranked by how directly it answers
    the inquiry.  Then the list is filled to the budget from the top.

    Every model that appears in the record keeps its best fact before the
    budget is spent on anything else, so a comparison question cannot end up
    holding one side of the comparison.

    Nothing is truncated mid-fact and nothing is cut by position: a fact is
    either carried whole or reported as dropped.
    """

    asked = {str(field) for field in requested_fields or ()}
    wanted = _terms(question) | {
        term for topic in topics or () for term in _terms(topic)
    }
    for field in asked:
        wanted |= _terms(field.replace("_", " "))

    ordered = sorted(
        facts, key=lambda item: _fact_relevance(item, asked=asked, wanted=wanted),
        reverse=True,
    )

    # --- 1. the same value, said twice -------------------------------------
    merged: dict[str, list[str]] = {}
    deduped: list[ProductFact] = []
    seen: dict[tuple[str, str], ProductFact] = {}
    dropped: list[dict[str, Any]] = []
    for fact in ordered:
        normalized = _normalized_value(fact.value)
        key = (str(fact.model_code or fact.product_id), normalized)
        first = seen.get(key)
        if first is None or (
            fact.field_key != first.field_key
            and len(normalized) < _MERGEABLE_VALUE_CHARS
        ):
            seen.setdefault(key, fact)
            deduped.append(fact)
            continue
        if fact.field_key != first.field_key:
            merged.setdefault(first.canonical_fact_id, []).append(fact.field_key)
        dropped.append({
            "field_key": fact.field_key,
            "model_code": fact.model_code,
            "reason": "DUPLICATE_VALUE",
            "same_value_as": first.field_key,
        })

    # --- 1b. one fact per meaning, across scopes ---------------------------
    #
    # A listing states its product's origin and the model record states the same
    # origin, so 690027174 carried both into the prompt: "한국 (베트남,중국,태국,
    # 멕시코,헝가리,슬로바키아)" beside "한국 (베트남,중국,헝가리,멕시코,슬로바키아,
    # 태국)". One fact, two spellings, two entries of evidence, and the model left
    # to wonder which to use.
    #
    # Grouped by canonical model identity, so two models are never collapsed into
    # each other -- a comparison keeps both sides -- and by the field-aware
    # canonical value, so this only merges readings that say the same thing.
    #
    # The listing's own row represents the group when there is one: it is what
    # the page the customer is reading states. A cross-model target never has a
    # listing row among its candidates, so preferring it cannot pull another
    # listing's evidence into that answer.
    by_meaning: dict[tuple[str, str, str], ProductFact] = {}
    semantic: list[ProductFact] = []
    for fact in deduped:
        identity = (
            canonical_model_identity(fact.model_code)
            or normalize_model(fact.model_code)
            or str(fact.applies_to_product_id or fact.product_id or "")
        )
        key = (identity, str(fact.field_key or ""), _canonical_fact_value(fact))
        first = by_meaning.get(key)
        if first is None:
            by_meaning[key] = fact
            semantic.append(fact)
            continue
        prefers_new = (
            str(fact.scope) == "LISTING" and str(first.scope) != "LISTING")
        loser, winner = (first, fact) if prefers_new else (fact, first)
        if prefers_new:
            by_meaning[key] = fact
            semantic[semantic.index(first)] = fact
        # The dropped copy's origin is not lost: it is recorded against the one
        # that stays, so a reader can still see both sources agreed.
        merged.setdefault(winner.canonical_fact_id, []).append(
            f"{loser.field_key}@{loser.scope}")
        dropped.append({
            "field_key": loser.field_key,
            "model_code": loser.model_code,
            "reason": "SEMANTIC_DUPLICATE_ACROSS_SCOPE",
            "kept_instead": winner.canonical_fact_id,
            "kept_scope": str(winner.scope),
        })
    deduped = semantic

    # --- 2. one fact per model before the budget is spent ------------------
    reserved: list[ProductFact] = []
    for fact in deduped:
        if not any(item.model_code == fact.model_code for item in reserved):
            reserved.append(fact)

    def prompt_fact(fact: "ProductFact") -> dict[str, Any]:
        value = fact.as_prompt_fact()
        also = merged.get(fact.canonical_fact_id)
        if also:
            value["also_reported_as"] = sorted(set(also))
        return value

    def cost(fact: "ProductFact") -> int:
        return (
            len(json.dumps(prompt_fact(fact), ensure_ascii=False, default=str))
            + len(fact.as_prompt_line(merged.get(fact.canonical_fact_id)))
        )

    # --- 3. fill to the budget, most relevant first ------------------------
    kept: list[ProductFact] = []
    spent = _PROMPT_BLOCK_OVERHEAD_CHARS
    for fact in deduped:
        price = cost(fact)
        if spent + price > budget_chars and fact not in reserved:
            dropped.append({
                "field_key": fact.field_key,
                "model_code": fact.model_code,
                "reason": "PRODUCT_KNOWLEDGE_BUDGET",
                "chars": price,
            })
            continue
        kept.append(fact)
        spent += price

    report = {
        "budget_chars": budget_chars,
        "available": len(facts),
        "selected": len(kept),
        "dropped": dropped,
        "duplicate_values_merged": sum(
            1 for item in dropped if item["reason"] == "DUPLICATE_VALUE"
        ),
        "dropped_for_budget": sum(
            1 for item in dropped if item["reason"] == "PRODUCT_KNOWLEDGE_BUDGET"
        ),
        "asked_fields_kept": sorted(
            {item.field_key for item in kept if item.field_key in asked}
        ),
        "asked_fields_dropped": sorted(
            {
                item["field_key"] for item in dropped
                if item["field_key"] in asked
                and item["reason"] == "PRODUCT_KNOWLEDGE_BUDGET"
            }
        ),
        "models_kept": sorted({str(item.model_code or "") for item in kept}),
        # Which fields were folded into which kept fact, so the rendered block
        # can name them and a reader can see nothing was silently discarded.
        "merged_fields": {key: sorted(set(value)) for key, value in merged.items()},
    }
    return kept, [prompt_fact(item) for item in kept], report


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
    # How confidently this listing was tied to a catalogued model, and -- when
    # it was not tied to exactly one -- which models it could have meant.
    # Candidates are carried so a reader downstream can see them as candidates;
    # nothing here elects one, so they never become verified facts.
    identity_status: str = "NOT_FOUND"
    candidate_models: tuple[dict[str, Any], ...] = dataclass_field(default=())
    # Which product this evidence is about, kept apart from which product the
    # customer is writing *from*. A customer reading one listing may ask about
    # another model by name, and answering that question from the listing's
    # record answers a question nobody asked.
    listing_model: str | None = None
    inquiry_target_models: tuple[str, ...] = dataclass_field(default=())
    resolved_pk_targets: tuple[str, ...] = dataclass_field(default=())
    target_resolution_reason: str = LISTING_FALLBACK

    @property
    def has_safe_facts(self) -> bool:
        return bool(self.safe_facts)

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidate_models)

    def safe_field_keys(self) -> frozenset[str]:
        return frozenset(item.field_key for item in self.safe_facts)

    def covers_all(self, fields: Iterable[str]) -> bool:
        wanted = {str(item) for item in fields if str(item).strip()}
        return bool(wanted) and wanted <= set(self.safe_field_keys())

    def facts_in_question_scope(
        self, question: object
    ) -> tuple["ProductFact", ...]:
        """The safe facts this question's own wording puts in play.

        Evidence and contradiction are different jobs and need different sets.
        What the model may *read* is this listing's whole record -- which of
        those rows bears on the question is its judgement. What a code-side
        contradiction check may *speak for* is much narrower: it compares
        stored polarities and quantities with no idea what either sentence is
        about, so it can only be trusted where the customer named the topic.

        Read whole, the record made that difference visible. An approved answer
        saying "티비 자체에 OTT 어플은 설치가 불가능" was reported as
        contradicted by ``set_top_box_ott_supported=YES`` -- the set-top box
        does support OTT, and both statements are true -- and, on the same
        pass, by ``free_delivery=YES``, which is about nothing the sentence
        mentions. Both were then written into the sub-question as CONFLICT,
        which refuses the answer.

        Narrowing here does not narrow what reaches GPT ②, and it restores the
        set the check ran on before the record was offered whole.
        """

        fields, _topics = fields_for_question(question)
        allowed = set(fields)
        for group in required_fact_groups(question):
            allowed.update(group)
        if not allowed:
            return ()
        return tuple(
            item for item in self.safe_facts if item.field_key in allowed
        )

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
            # Not "everything is covered" -- nothing was identified to cover.
            # Returning ``has_safe_facts`` here let the mere existence of a
            # catalogued value vouch for a question that never asked for it,
            # and that answer then cleared the auto-post product-fact hold.
            # A question whose claims this model cannot name is a question
            # the catalog cannot be said to answer.
            return False
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

    def prompt_block(
        self,
        facts: "Sequence[ProductFact] | None" = None,
        *,
        also_reported: "dict[str, Sequence[str]] | None" = None,
    ) -> str:
        chosen = tuple(self.safe_facts if facts is None else facts)
        if not chosen:
            return ""
        names = also_reported or {}
        lines = "\n".join(
            item.as_prompt_line(names.get(item.canonical_fact_id)) for item in chosen
        )
        partial = len(chosen) < len(self.safe_facts)
        return (
            # The header stays as it was. Rewriting it to say the list is not
            # the product's complete attribute set was measured and reverted:
            # on 688536978 it cost the answer that already worked (2/2 runs
            # used LID 117 + 169371 and resolved with the original header, 0/2
            # with the reworded one), and on 688536966 it changed nothing. What
            # an absent field means is stated in the rules below instead.
            "PRODUCT_CATALOG_JSON (exact matched product catalog evidence):\n"
            f"{lines}\n"
            "RULES:\n"
            "- Only the fields listed above may be stated as product fact.\n"
            "- A field that is not listed is UNKNOWN. Never say a feature is "
            "absent, unsupported or missing because it is not listed.\n"
            # Scoped to this block. Read as a global instruction, it forbade
            # GPT ② from applying a Learning answer from another listing even
            # when that answer was the evidence for the question.
            "- Within this PRODUCT_CATALOG_JSON block, never carry a value "
            "from another model, size or package over as this product's fact.\n"
            # What an absent field means, and what it does not.
            #
            # These two lines used to read "say the exact specification needs
            # checking instead of estimating it" and "필요한 항목이 목록에
            # 없으면 추측하지 말고 unresolved 로 남겨라". Measured on the server
            # (688536966 / 688536991, build 323f5c7): this listing's verified
            # record holds 20 fields and none of them is about the remote
            # control, so GPT ② followed this rule and returned unresolved while
            # the same prompt carried approved answers saying the remote is
            # included. A/B replay with only this block differing: 4/4
            # unresolved with it, 4/4 answered from Learning without it.
            "- 이 목록에 없는 항목은 '이 Product Knowledge가 그 사실을 제공하지 "
            "않는다'는 뜻일 뿐이다. 그런 항목은 Learning/Historical 등 이 "
            "프롬프트의 다른 근거를 읽고 적용 가능한지 직접 판단해서 답하라. "
            "모든 근거를 봐도 충분하지 않을 때에만 unresolved 로 남겨라.\n"
            # The list is this product's record, not a shortlist someone
            # prepared for this question. Saying so is what makes the wider
            # list safe: the model has to choose, and choosing badly here
            # means answering with a field nobody asked about.
            + (
                # What this list is has to stay true.  It used to say the list
                # is the product's whole verified record and was not chosen
                # for the question -- which is what makes a wide list safe to
                # read.  Once the prompt budget makes it a selection, the same
                # sentence would be a false claim about the evidence, so the
                # two cases say what is actually in front of the model.
                "- 위 목록은 이 상품의 검증 기록 중 이 질문과 가장 관련이 큰 "
                "항목만 남긴 것이다. 기록에 있으나 여기 없는 항목이 있을 수 "
                "있으므로, 없는 항목을 '이 상품에 없는 사양'으로 단정하지 "
                "마라. 질문에 실제로 필요한 항목만 골라서 사용하고, 나머지는 "
                "답변에 나열하지 마라.\n"
                if partial else
                "- 위 목록은 이 상품의 전체 검증 기록이며 질문에 맞춰 미리 고른 "
                "것이 아니다. 질문에 실제로 필요한 항목만 골라서 사용하고, "
                "나머지는 답변에 나열하지 마라.\n"
            )
            +
            "- kind=DEVICE_SPECIFICATION은 상품 자체의 사양이고, "
            "kind=LISTING_POLICY_SNAPSHOT은 이 판매 페이지의 현재 조건을 "
            "수집한 값이다. 후자는 고객의 주문 상태·배송일·현재 진행 상황의 "
            "근거가 될 수 없으며, 그런 질문은 주문/DPS 조회 근거를 따른다."
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
            "identity_status": self.identity_status,
            "candidate_count": len(self.candidate_models),
            "candidate_models": [dict(item) for item in self.candidate_models],
        }


def _candidate_summary(key: str, record: dict[str, Any]) -> dict[str, Any]:
    """One unelected catalog candidate, reduced to what a reader needs.

    The full record is a page of prose; this keeps the identifying fields and
    the headline specifications so the difference between two candidates is
    visible without the prompt carrying two pages.
    """

    return {
        "model_key": str(key),
        "model": record.get("model"),
        "brand": record.get("brand"),
        "size_inch": record.get("size_inch"),
        "resolution": record.get("resolution"),
        "refresh_rate_hz": record.get("hz"),
        "vesa": record.get("vesa"),
        "color": record.get("color"),
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


_SOURCE_TIME_KEYS = (
    "valid_from", "source_updated_at", "last_verified_at", "collected_at",
    "effective_at", "approved_at",
)


def _parse_source_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _fact_source_time(fact: ProductFact) -> datetime | None:
    times = [
        parsed
        for provenance in fact.provenance
        for key in _SOURCE_TIME_KEYS
        if (parsed := _parse_source_time(provenance.get(key))) is not None
    ]
    return max(times) if times else None


def _fact_source_authority(fact: ProductFact) -> int:
    """Authority already present in source provenance, before recency."""

    sources = {
        str(item.get("source_type") or "").upper()
        for item in fact.provenance
        if isinstance(item, dict)
    }
    if str(fact.source_type or "").strip():
        sources.add(str(fact.source_type).upper())
    if "API" in sources:
        return 3
    if sources & {"MANUAL_VISUAL_TRANSCRIPTION", "IMAGE_VISUAL"}:
        return 2
    if sources & {"IMAGE_TEXT", "OCR", "VISION"}:
        return 1
    return 0


def _normalized_fact_value(fact: ProductFact) -> str:
    value = fact.value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"number:{float(value):g}"
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    compact = re.sub(r"\s+", "", rendered).lower().strip('"')
    # Existing catalog normalization deliberately preserves display wording.
    # "Max 60 Hz" and "Max 60" are the same refresh-rate observation, not a
    # policy conflict.  Collapse a single labelled numeric value only when the
    # whole value is that number plus a harmless max/unit wrapper.
    numeric = re.fullmatch(
        r"(?:max|최대)?(-?\d+(?:\.\d+)?)(?:hz|kg|mm|cm|w|ms|개|채널|%)?",
        compact,
        re.IGNORECASE,
    )
    if numeric:
        return f"number:{float(numeric.group(1)):g}"
    return compact


# What makes two readings of one field the same fact.
#
# The conflict resolver compared ``_normalized_fact_value``, which strips
# whitespace and case and collapses a labelled number. That is the right test for
# a number and the wrong one for everything else: "삼성 서비스센터 : 1588-3366"
# and "삼성전자서비스센터 / 1588-3366" are one telephone number written twice,
# and comparing the strings made them contradict each other, so both were
# withheld. Measured after the review promotions, 75 of 159 rows died that way.
#
# One canonicalizer per field family, each deterministic and each meaning-
# preserving. Nothing here is fuzzy: a different number, a different company, a
# different country or a different word is still a conflict.
_LEGAL_SUFFIXES = ("㈜", "(주)", "주식회사", "(유)", "㈐")
_PHONE_IN_TEXT = re.compile(r"\d{2,4}-\d{3,4}-\d{3,4}|\b\d{4}-\d{4}\b")
_PUNCTUATION_ONLY = re.compile(
    r"[\s·.,()\[\]{}:;/\\|·‧∙・…~〜\-–—!?\"'“”‘’]+"
)
_PARENTHESISED = re.compile(r"[（(]([^）)]*)[）)]")
_DECIMAL = re.compile(r"-?\d+(?:\.\d+)?")
# Condition words that change what a measurement is of. Two values are never
# called equivalent when both name a condition and the conditions differ.
_MEASUREMENT_CONDITIONS = (
    ("WITH_STAND", ("스탠드포함", "스탠드 포함", "with stand", "포함(가로")),
    ("WITHOUT_STAND", ("스탠드미포함", "스탠드 미포함", "스탠드제외", "스탠드 제외",
                       "without stand", "본체만")),
    ("PACKAGE", ("포장", "package", "박스")),
)
_CONTACT_FIELDS = ("as_contact", "as_phone", "as_guide", "contact", "phone",
                   "seller_as_contact_location")
_MANUFACTURER_FIELDS = ("manufacturer", "importer", "brand_company")
_DATE_FIELDS = ("release_ym", "release_date", "release", "manufacture_date")
_ORIGIN_FIELDS = ("origin_country", "country_of_origin", "origin")
_PROSE_FIELDS = ("warranty_policy", "as_guide", "policy", "guide", "notice")
_DIMENSION_FIELDS = ("dimension", "size_mm", "width", "height", "depth")


def _fold_punctuation(text: str) -> str:
    return _PUNCTUATION_ONLY.sub("", unicodedata.normalize("NFKC", text)).lower()


def _measurement_condition(text: str) -> str:
    lowered = text.lower()
    for name, markers in _MEASUREMENT_CONDITIONS:
        if any(marker.lower() in lowered for marker in markers):
            return name
    return ""


def _canonical_contact(text: str) -> str | None:
    """The set of telephone numbers, which is what a contact actually says.

    One number written three ways is one contact. Two numbers -- the maker's and
    a bundled stand vendor's -- is a different contact from one, so the sets are
    compared rather than merged.
    """

    numbers = sorted(set(_PHONE_IN_TEXT.findall(text)))
    return "phones:" + "|".join(numbers) if numbers else None


def _canonical_company(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text)
    for suffix in _LEGAL_SUFFIXES:
        cleaned = cleaned.replace(suffix, "")
    return "company:" + _fold_punctuation(cleaned)


def _canonical_year_month(text: str) -> str | None:
    """YYYY-MM, so "2024-04", "2024년 4월" and "2024.04" are one date."""

    # Unsigned: "2024-04" must read as two numbers, not as 2024 and minus four.
    numbers = [int(item) for item in re.findall(r"\d+", text)]
    years = [item for item in numbers if 1900 <= item <= 2200]
    if not years:
        return None
    year = years[0]
    months = [item for item in numbers if 1 <= item <= 12 and item != year]
    return f"date:{year}-{months[0]:02d}" if months else f"date:{year}"


def _canonical_origin(text: str) -> str:
    """Country of record, plus the set of alternate plants with order removed.

    "한국 (베트남, 중국, 태국)" and "한국 (중국, 태국, 베트남)" name the same
    places. "중국산" and "한국 (베트남, 중국, …)" do not: the country of record
    differs, and that is kept ordered and compared exactly.
    """

    normalized = unicodedata.normalize("NFKC", text)
    match = _PARENTHESISED.search(normalized)
    alternates: tuple[str, ...] = ()
    head = normalized
    if match:
        alternates = tuple(sorted({
            _fold_punctuation(part) for part in match.group(1).split(",")
            if part.strip()
        }))
        head = normalized[: match.start()]
    return "origin:" + _fold_punctuation(head) + "|" + "|".join(alternates)


def _canonical_fact_value(fact: ProductFact) -> str:
    """The field-aware canonical form used to decide whether two rows agree."""

    field = str(fact.field_key or "").lower()
    text = fact.value if isinstance(fact.value, str) else None
    if text is None:
        return _normalized_fact_value(fact)
    if any(name in field for name in _CONTACT_FIELDS):
        contact = _canonical_contact(text)
        if contact:
            return contact
    if any(name in field for name in _ORIGIN_FIELDS):
        return _canonical_origin(text)
    if any(name in field for name in _DATE_FIELDS):
        moment = _canonical_year_month(text)
        if moment:
            return moment
    if any(name in field for name in _MANUFACTURER_FIELDS):
        return _canonical_company(text)
    if any(name in field for name in _PROSE_FIELDS):
        # Spacing and punctuation only. "결함" and "결합" are one character apart
        # and are different words, so folding stops at punctuation.
        return "prose:" + _fold_punctuation(text)
    return _normalized_fact_value(fact)


def _rounding_representative(
    group: Sequence[ProductFact],
) -> ProductFact | None:
    """The most precise reading, when the group differs only in precision.

    Every value must be the same count of numbers, must round to the same
    integers, and no two may name different conditions: a with-stand depth and a
    without-stand depth differ by far more than a rounding step, and refusing an
    explicit contradiction is what keeps this from merging them anyway.
    """

    readings = []
    for fact in group:
        if not isinstance(fact.value, str):
            return None
        numbers = [float(item) for item in _DECIMAL.findall(fact.value)]
        if len(numbers) < 2:
            return None
        readings.append((fact, numbers, _measurement_condition(fact.value)))
    first = readings[0][1]
    if any(len(numbers) != len(first) for _f, numbers, _c in readings):
        return None
    if any(
        round(numbers[index]) != round(first[index])
        for _f, numbers, _c in readings for index in range(len(first))
    ):
        return None
    named = {condition for _f, _n, condition in readings if condition}
    if len(named) > 1:
        return None

    def precision(numbers: Sequence[float]) -> int:
        return sum(
            len(f"{value:g}".partition(".")[2]) for value in numbers
        )

    return max(readings, key=lambda entry: precision(entry[1]))[0]


def _exact_fact_conflict_key(fact: ProductFact) -> tuple[str, ...]:
    """Identity and field first; timestamps never join different subjects."""

    return (
        str(fact.component_scope or ""),
        str(fact.subject or ""),
        str(fact.scope or ""),
        normalize_model(fact.model_code),
        str(fact.applies_to_product_id or ""),
        str(fact.field_key or ""),
    )


def _resolve_exact_fact_conflicts(
    facts: Sequence[ProductFact],
) -> tuple[list[ProductFact], list[ProductFact]]:
    """Resolve only contradictory values for one exact identity and field.

    Authority precedes source time.  A reliable timestamp is used only inside
    the top authority tier.  When equal-authority conflicting values have no
    reliable time relation, none is guessed current: every value in that exact
    group is withheld as CONFLICT/REVIEW evidence.
    """

    grouped: dict[tuple[str, ...], list[ProductFact]] = {}
    for fact in facts:
        grouped.setdefault(_exact_fact_conflict_key(fact), []).append(fact)
    safe: list[ProductFact] = []
    excluded: list[ProductFact] = []
    for group in grouped.values():
        values = {_canonical_fact_value(item) for item in group}
        if len(values) <= 1:
            safe.extend(group)
            continue
        # Same measurement, different precision. The precise reading represents
        # the group and the rounded ones stay as corroboration rather than
        # cancelling it: two spellings of 716.1 x 517.0 x 193.5 mm are not a
        # disagreement about the product's size.
        representative = _rounding_representative(group)
        if representative is not None:
            safe.append(representative)
            for item in group:
                if item.canonical_fact_id == representative.canonical_fact_id:
                    continue
                excluded.append(replace(
                    item, safe_for_answer=False,
                    resolution_status="ROUNDING_VARIANT",
                    exclusion_reason=(
                        "SUPERSEDED_BY_HIGHER_PRECISION_EQUIVALENT_READING"),
                ))
            continue
        top_authority = max(_fact_source_authority(item) for item in group)
        authoritative = [
            item for item in group
            if _fact_source_authority(item) == top_authority
        ]
        authoritative_values = {
            _canonical_fact_value(item) for item in authoritative
        }
        winners: list[ProductFact] = []
        if len(authoritative_values) == 1:
            winners = authoritative
        else:
            timed = [(item, _fact_source_time(item)) for item in authoritative]
            if all(moment is not None for _item, moment in timed):
                latest = max(moment for _item, moment in timed if moment is not None)
                winners = [item for item, moment in timed if moment == latest]
                if len({_canonical_fact_value(item) for item in winners}) > 1:
                    winners = []
        winner_ids = {item.canonical_fact_id for item in winners}
        safe.extend(winners)
        for item in group:
            if item.canonical_fact_id in winner_ids:
                continue
            excluded.append(replace(
                item,
                safe_for_answer=False,
                resolution_status="CONFLICT",
                exclusion_reason=(
                    "SUPERSEDED_BY_NEWER_AUTHORITATIVE_SOURCE"
                    if winners else "EXACT_IDENTITY_FIELD_CONFLICT_NO_RELIABLE_TIME"
                ),
            ))
    return safe, excluded


# Every field the model catalogue can actually produce a value for. Kept beside
# ``_catalog_facts``, which is the only place these keys are filled in.
#
# ``fields_for_question`` narrows a lookup to the topics the customer's wording
# names. That is the right question to ask when deciding what to *show*, and the
# wrong one when the wording is the thing under suspicion: "삼성기사분이 설치하러
# 오시나요" matches no entry in FIELD_TOPICS, so the lookup returned no fields and
# the product's verified specification never reached the prompt at all. With a
# usable GPT ① the catalogue is offered whole instead, and the model decides
# which rows bear on the question.
#
# ``vesa_mm`` and ``weight_catalog`` are in this set even though they appear in
# SUBJECT_SENSITIVE_FIELDS. That list exists because a *package listing* records
# some quantities twice -- the panel's weight and the bundled stand's, the
# display's VESA pattern and the bracket's -- and only the customer's wording
# says which subject was asked about. The model catalogue records no accessory
# at all (see ``_catalog_facts``: every key it fills is the base device), so on
# this path there is no second subject for either field to be confused with, and
# withholding them bought nothing.
#
# What it cost was measurable: "벽에 걸 수 있나요?" names no word the VESA topic
# table recognises, so a listing whose VESA pattern was catalogued reached the
# model without it, while "벽걸이 브라켓 규격은?" -- the same question -- got it.
# Which field answers a question is the judgement that moved to GPT ②; these two
# were still being decided by wording.
CATALOG_BACKED_FIELDS: frozenset[str] = frozenset({
    "screen_size", "resolution", "refresh_rate", "speaker_present",
    "brand", "model_name", "color",
    "hdmi_present", "usb_present", "ethernet_present", "rf_terminal",
    "bluetooth_present", "wifi_present", "stand_spacing",
    "vesa_mm", "weight_catalog",
})

PHYSICAL_DIMENSION_FIELDS: frozenset[str] = frozenset({
    "dimensions_labelled", "dimensions_product", "dimensions_with_stand",
    "dimensions_without_stand", "dimensions_with_stand_mm",
    "dimensions_without_stand_mm", "width", "screen_height_without_stand",
    "depth_screen", "total_height_with_stand", "stand_depth",
})


# The question side and the data side of the same fact, in one place.
#
# ``FIELD_TOPICS`` grew against an ontology the collected records do not use.
# Measured against the merged file: Product Knowledge holds 506 distinct field
# names, FIELD_TOPICS aims at 90, 465 stored names have no keyword route at all,
# and 38 of the names it does aim at exist nowhere in the data. So a question
# could match a topic, produce a field list, and still name nothing the record
# contains -- "as는 몇년인가요" matched no topic at all while ``as_phone`` and
# ``as_guide`` sat on that listing, and "이 제품 베사홀 규격" asked for
# ``vesa_mm`` while the record stored ``vesa``.
#
# Each family names how customers ask and which stored field names answer. The
# fragments are matched against the vocabulary the loaded file actually has, so
# a family can never invent a field, and a stored name that gains or loses a
# suffix keeps working without a code change.
_ATTRIBUTE_FAMILIES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("ORIGIN",
     ("제조국", "원산지", "생산지", "생산 국가", "생산국", "어디서 만들",
      "어디서 생산", "어느 나라", "made in"),
     ("origin_country", "country_of_origin")),
    ("AS_WARRANTY",
     ("as", "a/s", "에이에스", "서비스센터", "서비스 센터", "보증기간",
      "보증 기간", "무상수리", "무상 수리", "고장", "수리"),
     ("as_contact", "as_phone", "as_guide", "warranty")),
    ("MANUFACTURER",
     ("제조사", "제조원", "만든 회사", "수입사", "수입원", "판매원"),
     ("manufacturer", "manufacturer_importer")),
    ("WEIGHT",
     ("무게", "중량", "kg", "킬로"),
     ("weight",)),
    ("DIMENSIONS",
     ("크기", "사이즈", "치수", "가로", "세로", "폭", "높이", "깊이", "두께"),
     ("dimension", "width", "height", "depth")),
    ("VESA",
     ("베사", "vesa", "벽걸이 규격", "브라켓 규격", "홀 간격", "타공"),
     ("vesa",)),
    ("RESOLUTION",
     ("해상도", "resolution", "4k", "uhd", "qhd", "fhd"),
     ("resolution",)),
    ("REFRESH",
     ("주사율", "리프레시", "hz", "헤르츠"),
     ("refresh", "refresh_rate")),
    ("RESPONSE",
     ("응답속도", "응답 속도", "gtg"),
     ("response_time",)),
    ("PANEL",
     ("패널", "ips", "va패널", "va 패널", "tn패널"),
     ("panel",)),
    ("BLUETOOTH",
     ("블루투스", "bluetooth", "무선 이어폰", "무선이어폰", "무선 헤드폰"),
     ("bluetooth",)),
    ("WIRELESS",
     ("와이파이", "wifi", "wi-fi", "무선 연결", "미러링", "캐스팅"),
     ("wifi", "wireless", "mirroring", "cast")),
    ("PORTS",
     ("hdmi", "usb", "단자", "포트", "c타입", "타입c", "랜", "lan", "이더넷"),
     ("hdmi", "usb", "port", "ethernet", "lan_")),
    ("SPEAKER",
     ("스피커", "사운드", "소리", "음량", "speaker"),
     ("speaker", "audio")),
    ("POWER",
     ("소비전력", "대기전력", "전력", "전압", "전기", "에너지", "소비 전력"),
     ("power_consumption", "rated_voltage", "energy", "standby")),
    ("CERTIFICATION",
     ("인증", "kc", "안전인증", "certification"),
     ("certification",)),
    ("DELIVERY_COST",
     ("배송비", "설치비", "추가 비용", "추가비용", "무료배송", "무료 배송",
      "반품비", "교환비"),
     ("additional_cost", "delivery_fee", "return_delivery_fee",
      "exchange_delivery_fee", "delivery_type")),
    ("RELEASE",
     ("출시", "연식", "년형", "제조일", "생산연도", "출시일"),
     ("release", "manufacture_date")),
)


def _family_fragments(text: str) -> tuple[str, ...]:
    """Stored-field fragments the families this text asks about point at."""

    fragments: list[str] = []
    for _family, keywords, targets in _ATTRIBUTE_FAMILIES:
        present = [keyword for keyword in keywords if keyword in text]
        if not present:
            continue
        if all(_is_excluded_mention(text, keyword) for keyword in present):
            continue
        fragments.extend(targets)
    return tuple(dict.fromkeys(fragments))


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
        present = [keyword for keyword in keywords if keyword in text]
        if not present:
            continue
        # A word the customer used only to take something *out* of the
        # question does not put that topic in it. "기존 벽걸이 티비 제거는
        # 무상인가요?" names a wall mount in order to have it hauled away;
        # answering it with this product's VESA holes is a fact about a
        # different object. Same reading ``asks_about_a_bundled_component``
        # already applies to component scope, applied to topic scope.
        if all(_is_excluded_mention(text, keyword) for keyword in present):
            continue
        if (
            "installation_method" in base_fields
            and any(marker in text for marker in INSTALLATION_SCHEDULE_MARKERS)
        ):
            continue
        topics.append(present[0])
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
        for word in (
            "화면 크기", "화면크기", "화면 사이즈", "몇인치", "몇 인치",
            "인치인가", "인치 인가",
        )
    ):
        groups.append(frozenset({"screen_size", "display_size_cm"}))
    if any(
        word in text
        for word in (
            "제품 크기", "제품크기", "가로", "세로", "높이", "깊이", "두께",
            "몇센치", "몇 센치", "센치", "치수", "외형", "dimensions",
        )
    ):
        groups.append(PHYSICAL_DIMENSION_FIELDS)
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
        catalog_repository: ProductCatalogRepository | None = None,
    ) -> None:
        # One Product Knowledge source: ``data/model_data_with_color.json``.
        #
        # ``product_facts.db`` was a second store keyed on the Naver
        # product_id, and for a while it was consulted first. It is retired.
        # Its connection and component attributes came from image OCR and
        # stayed unverified (wifi_present 2 of 19 listings usable,
        # remote_control_included 2 of 21), and being first meant a listing it
        # merely knew about blocked the catalogue even when it had nothing
        # usable to say. Nothing here opens that file and no path falls back
        # to it, so a stray copy on disk cannot revive it.
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
        include_all_catalog_fields: bool = False,
    ) -> ProductKnowledgeResult:
        """Verified facts this inquiry may be answered from.

        ``include_all_catalog_fields`` is set when GPT ① asked for product
        evidence. The catalogue is then offered whole rather than filtered
        through the customer's wording -- see ``CATALOG_BACKED_FIELDS`` for why,
        and for the one class of field that keeps its keyword path.

        Product identity contract, in force here:

        1. A ``product_id`` is the strongest identity and is used first.
        2. With no ``product_id``, the listing name may be matched instead.
        3. An exact ``product_id`` wins over an ambiguous name -- the id names
           the listing the customer is writing from; the name only resembles
           one.
        4. If neither settles which product this is, nothing is reported as a
           VERIFIED fact. Candidates may still travel, labelled as candidates.
        5. A fact the listing store excluded (NEEDS_REVIEW, CONFLICT,
           superseded, foreign model) is never revived from the catalogue.
        6. When the listing store knows the product, its safety verdict stands;
           the catalogue is consulted only when it does not know it.
        """

        key = str(product_id or "").strip()
        texts = [str(item) for item in (questions or ()) if str(item).strip()]
        if str(question or "").strip():
            texts.append(str(question))
        combined = " ".join(texts)
        fields, topics = fields_for_question(combined)
        # The keyword router names fields from its own vocabulary; the record
        # uses another. Expanding against the vocabulary the file actually has
        # is what lets a question reach the row that answers it, and it can
        # only ever add names the data contains.
        fields = self._expand_to_stored_fields(combined, fields)
        # Two different questions, answered from two different places.
        #
        # The model catalogue is a fixed, small ontology, so naming its fields
        # is the only way to ask it anything: ``CATALOG_BACKED_FIELDS`` is the
        # whole of what it can produce.
        #
        # The listing store is not like that. It holds this product's own
        # record -- 216 distinct fields across the store, a median of 83 rows
        # for one product -- and enumerating the subset worth reading was a
        # keyword judgement about the customer's wording. That judgement is
        # what decided, for a listing whose remote-control and installation
        # facts were both stored, that a question about either reached the
        # model without them. ``None`` asks for the record whole, and which
        # rows bear on the question is then read by GPT ② from the rows.
        #
        # Only when GPT ① said this inquiry needs product evidence. Without a
        # usable understanding the keyword topics remain the only thing
        # keeping a specification out of a delivery prompt.
        catalog_fields = fields
        listing_fields: tuple[str, ...] | None = fields
        if include_all_catalog_fields:
            catalog_fields = tuple(dict.fromkeys(
                (*fields, *sorted(CATALOG_BACKED_FIELDS))
            ))
            listing_fields = None

        # Which product the customer is asking about, which is not always the
        # one they are writing from. Resolved from the customer's words only:
        # ``combined`` is the question and its sub-questions, never the listing
        # title, so a title can never nominate itself as an explicit target.
        resolution = self._resolve_inquiry_targets(
            combined, listing_model=model_code, listing_id=key,
        )

        def _catalog(target: InquiryTarget) -> ProductKnowledgeResult:
            return self._catalog_facts_for_inquiry(
                product_id=target.product_id,
                # A listing title identifies its own listing. For any other
                # target it is noise that can only pull the lookup back toward
                # the model the customer did not ask about.
                product_name=product_name if target.is_listing else "",
                option_name=option_name if target.is_listing else "",
                model_code=target.model_code,
                fields=catalog_fields,
                topics=topics,
                combined=combined,
            )

        if not resolution.targets:
            # The customer named a model and it is not one this catalogue
            # holds. The listing's record is not a substitute for it.
            return self._with_targets(
                ProductKnowledgeResult(
                    product_id=key or None, listing_id=None, matched=False,
                    requested_fields=catalog_fields, topics=topics,
                    unavailable_reason="INQUIRY_MODEL_NOT_IN_CATALOG",
                    identity_status="NOT_FOUND",
                ),
                resolution,
            )

        results = [_catalog(target) for target in resolution.targets]
        merged = (
            results[0] if len(results) == 1
            else self._merge_target_results(
                results, product_id=key, fields=catalog_fields, topics=topics,
            )
        )
        return self._with_targets(merged, resolution)

    def _stored_field_names(self) -> frozenset[str]:
        """Every field name the loaded Product Knowledge actually stores."""

        knowledge = self.catalog_repository.product_knowledge()
        names: set[str] = set()
        for section in _PRODUCT_KNOWLEDGE_SECTIONS:
            for row in knowledge.get(section, ()) or ():
                if isinstance(row, dict) and row.get("field"):
                    names.add(str(row["field"]))
        return frozenset(names)

    def _expand_to_stored_fields(
        self, text: str, fields: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Add the stored field names this question's families answer from.

        Additive by design. The router's own names are kept because the model
        catalogue answers to them, and a name that exists in neither place is
        harmless; what changes is that the names the *record* uses are now in
        the request too.
        """

        fragments = _family_fragments(" ".join(str(text or "").lower().split()))
        if not fragments:
            return fields
        stored = self._stored_field_names()
        # A fragment matches a stored name as a prefix or as an embedded part,
        # so "weight" reaches weight, package_weight and weight_with_stand
        # without naming any of them.
        matched = sorted(
            name for name in stored
            if any(fragment in name.lower() for fragment in fragments)
        )
        return tuple(dict.fromkeys((*fields, *matched)))

    def _resolve_inquiry_targets(
        self, text: str, *, listing_model: object, listing_id: str,
    ) -> TargetResolution:
        """Which product(s) this inquiry is about.

        The listing is the subject until the customer names something else.
        When they do, the named model becomes the subject -- all of it, so a
        comparison keeps every model it compares, and none of it when the name
        is not one the catalogue knows.

        Identification is entirely the catalogue's existing one. Nothing here
        compares a model code to a literal or knows what any product line is
        called; a model is whatever ``models_stated_in`` resolves, which is the
        same ``match`` the listing path has always used.
        """

        try:
            aliases = self.catalog_repository.catalog().get("aliases")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            aliases = None
        listing_code = str(listing_model or "").strip()
        listing_norm = normalize_model(listing_code)
        listing_identity = (
            canonical_model_identity(listing_code, aliases=aliases)
            if listing_code else None
        )
        listing_target = InquiryTarget(
            model_code=listing_code, identity=listing_identity,
            product_id=listing_id, is_listing=True,
        )
        stated = self.catalog_repository.models_stated_in(text)

        def _listing_only(reason: str) -> TargetResolution:
            return TargetResolution(
                (listing_target,), reason, listing_code or None,
                listing_identity, stated,
            )

        if not stated.tokens:
            return _listing_only(LISTING_FALLBACK)
        # Quoting the listing's own code is not naming a different product.
        # Customers quote it in full ("LS32DM501EKXKR"), in part ("LS32DM501E")
        # and by pasting the whole title, which drops family fragments like
        # "LH43B" into the text. Each of those is the listing referring to
        # itself, so none of them makes the inquiry unanswerable -- and a
        # listing whose own code the catalogue happens not to hold stays
        # answerable exactly as it was.
        def _quotes_the_listing(token: str) -> bool:
            token_norm = normalize_model(token)
            return bool(
                listing_norm and token_norm
                and (token_norm in listing_norm or listing_norm in token_norm)
            )

        if [
            token for token in stated.unresolved
            if not _quotes_the_listing(token)
        ]:
            return TargetResolution(
                (), UNRESOLVED_EXPLICIT_MODEL, listing_code or None,
                listing_identity, stated,
            )
        if not stated.resolved:
            return _listing_only(LISTING_FALLBACK)
        targets: list[InquiryTarget] = []
        for _token, model_key, identity in stated.resolved:
            is_listing = bool(
                normalize_model(model_key) == listing_norm
                or (listing_identity and identity == listing_identity)
            )
            targets.append(InquiryTarget(
                model_code=model_key, identity=identity,
                product_id=listing_id if is_listing else "",
                is_listing=is_listing,
            ))
        # "이 제품이랑 43BEF 차이가 뭐예요?" -- one side of the comparison is
        # named and the other is pointed at. The listing joins as a target of
        # its own; it does not replace the model that was named.
        if (
            _REFERS_TO_THIS_LISTING.search(text)
            and not any(target.is_listing for target in targets)
            and (listing_code or listing_id)
        ):
            targets.insert(0, listing_target)
        return TargetResolution(
            tuple(targets),
            MULTI_MODEL_COMPARISON if len(targets) > 1 else INQUIRY_EXPLICIT_MODEL,
            listing_code or None, listing_identity, stated,
        )

    @staticmethod
    def _with_targets(
        result: ProductKnowledgeResult, resolution: TargetResolution,
    ) -> ProductKnowledgeResult:
        """Record what the lookup was aimed at, alongside what it found."""

        # The lookup may have settled an identity the resolution could not.
        #
        # For a listing question the resolution's target is read off the title,
        # and a title can carry a fragment that names no catalogued model:
        # "삼성 107.9cm(43인치) 4K UHD LH43B 스마트 비즈니스TV" yields "LH43B",
        # which is not in the catalogue. The lookup then identifies the listing
        # properly from its own API record -- LH43BEDHLGFXKR -- and reporting the
        # title fragment instead would label 475 correct facts as another
        # model's. The lookup wins whenever it settled one and the customer
        # named nothing else.
        settled = tuple(
            item for item in (result.resolved_pk_targets or ()) if str(item).strip()
        )
        stated = tuple(
            item for item in resolution.resolved_pk_targets if str(item).strip()
        )
        targets = (
            settled if (resolution.reason == LISTING_FALLBACK and settled)
            else (stated or settled)
        )
        return replace(
            result,
            listing_model=(
                result.listing_model
                if (resolution.reason == LISTING_FALLBACK and result.listing_model)
                else (
                    resolution.listing_identity
                    or resolution.listing_model
                    or result.listing_model
                )
            ),
            inquiry_target_models=resolution.inquiry_target_models,
            resolved_pk_targets=targets,
            target_resolution_reason=resolution.reason,
        )

    @staticmethod
    def _merge_target_results(
        results: Sequence[ProductKnowledgeResult], *,
        product_id: str, fields: tuple[str, ...], topics: tuple[str, ...],
    ) -> ProductKnowledgeResult:
        """One result covering several targets, each keeping its own facts.

        A comparison is answered from both records or it is not answered. The
        facts stay attributed -- every one carries its own ``model_code`` -- so
        merging them into one list never merges the models they describe.
        """

        def _key(fact: ProductFact) -> tuple[Any, ...]:
            return (fact.model_code, fact.field_key, str(fact.value),
                    fact.subject, fact.canonical_fact_id)

        safe: dict[tuple[Any, ...], ProductFact] = {}
        excluded: dict[tuple[Any, ...], ProductFact] = {}
        for result in results:
            for fact in result.safe_facts:
                safe.setdefault(_key(fact), fact)
            for fact in result.excluded_facts:
                excluded.setdefault(_key(fact), fact)
        matched = [result for result in results if result.matched]
        primary = matched[0] if matched else results[0]
        statuses = {result.identity_status for result in results}
        return ProductKnowledgeResult(
            product_id=product_id or primary.product_id,
            listing_id=next(
                (result.listing_id for result in results if result.listing_id),
                None,
            ),
            matched=bool(matched),
            requested_fields=fields,
            safe_facts=tuple(safe.values()),
            excluded_facts=tuple(excluded.values()),
            unavailable_reason=(
                None if matched else next(
                    (result.unavailable_reason for result in results
                     if result.unavailable_reason), None,
                )
            ),
            topics=topics,
            collection_status=primary.collection_status,
            component_subject=any(result.component_subject for result in results),
            identity_status=(
                statuses.pop() if len(statuses) == 1 else "MULTI_MODEL_TARGET"
            ),
            candidate_models=tuple(
                candidate for result in results
                for candidate in result.candidate_models
            ),
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
            # The strict reading first, then the listing's own API-declared
            # model with its bundle suffix removed. Both describe this listing;
            # neither reaches for another one.
            exact_model_key = (
                self._integrated_exact_model_for_listing(product_id)
                or self._listing_declared_base_model(product_id)
            )
            if exact_model_key:
                integrated, excluded = self._integrated_product_knowledge_facts(
                    product_id=product_id, model_key=exact_model_key,
                    fields=fields,
                    include_all=bool(fields and set(CATALOG_BACKED_FIELDS).issubset(fields)),
                )
                if integrated:
                    return ProductKnowledgeResult(
                        product_id=product_id or None,
                        listing_id=product_id or None,
                        matched=True,
                        requested_fields=fields,
                        topics=topics,
                        safe_facts=tuple(integrated),
                        excluded_facts=tuple(excluded),
                        collection_status="INTEGRATED_PRODUCT_KNOWLEDGE_JSON",
                        identity_status="LISTING_EXACT_API_MODEL",
                        # The listing's identity, now that it has one. Left
                        # unset this reported an empty target and the prompt
                        # announced the listing as a model named "".
                        listing_model=_settled_identity(
                            exact_model_key, self.catalog_repository),
                        resolved_pk_targets=(
                            _settled_identity(
                                exact_model_key, self.catalog_repository),
                        ),
                    )
            integrated, excluded = self._integrated_product_knowledge_facts(
                product_id=product_id, model_key="", fields=fields,
                include_all=bool(fields and set(CATALOG_BACKED_FIELDS).issubset(fields)),
            )
            if integrated:
                return ProductKnowledgeResult(
                    product_id=product_id or None, listing_id=product_id or None,
                    matched=True, requested_fields=fields, topics=topics,
                    safe_facts=tuple(integrated), excluded_facts=tuple(excluded),
                    collection_status="INTEGRATED_PRODUCT_KNOWLEDGE_JSON",
                    identity_status="LISTING_EXACT",
                )
            # AMBIGUOUS carries the models the listing could have meant. They
            # travel as candidates and never as facts: which of two 85-inch
            # panels a title means is not something this lookup can settle, and
            # electing one would put another model's specification into an
            # answer about this one.
            return ProductKnowledgeResult(
                product_id=product_id or None, listing_id=None, matched=False,
                requested_fields=fields, topics=topics,
                unavailable_reason=match.reason,
                identity_status=match.status,
                candidate_models=tuple(
                    _candidate_summary(key, record)
                    for key, record in match.candidates
                ),
            )
        component_subject = asks_about_a_bundled_component(combined)
        facts = [] if component_subject else self._catalog_facts(
            product_id=product_id or match.model_key,
            model_key=match.model_key, record=match.record, fields=fields,
        )
        integrated, excluded = self._integrated_product_knowledge_facts(
            product_id=product_id, model_key=match.model_key, fields=fields,
            include_all=bool(fields and set(CATALOG_BACKED_FIELDS).issubset(fields)),
        )
        facts.extend(integrated)
        return ProductKnowledgeResult(
            product_id=product_id or match.model_key,
            # The inquiry's listing identity remains distinct from the model
            # key; model-level facts still carry their own model_code/scope.
            listing_id=(f"listing_{product_id}" if product_id else match.model_key),
            matched=True, requested_fields=fields, topics=topics,
            safe_facts=tuple(facts), excluded_facts=tuple(excluded),
            collection_status="CATALOG_JSON",
            component_subject=component_subject,
            identity_status=match.status,
            # The identity this lookup settled on, which is not always the one
            # the title suggested: "삼성 UHD BE85D-H …" yields the box name
            # while the catalogue match is LH85BEDH, and reporting the box name
            # labelled 66 of this listing's own facts as another model's.
            listing_model=_settled_identity(
                match.model_key, self.catalog_repository),
            resolved_pk_targets=(
                _settled_identity(match.model_key, self.catalog_repository),
            ),
        )

    def _integrated_exact_model_for_listing(self, product_id: str) -> str | None:
        """Return one exact MAIN_PRODUCT model proven by this listing's API.

        This is deliberately narrower than alias or prefix matching: the
        integrated model-code record must preserve an API ``modelName``
        provenance for this exact product ID, its value must equal its model
        code, and it cannot be a compound/multi-model identifier.
        """
        key = str(product_id or "").strip()
        if not key:
            return None
        knowledge = self.catalog_repository.product_knowledge()
        candidates: set[str] = set()
        for row in knowledge.get("model_facts", ()):
            if not isinstance(row, dict):
                continue
            model = str(row.get("model_code") or "").strip()
            value = str(row.get("value") or "").strip()
            if (
                row.get("subject") != "MAIN_PRODUCT"
                or row.get("field") != "model_code"
                or row.get("scope") != "EXACT_MODEL"
                or row.get("scope_status") != "RESOLVED"
                or not model
                or normalize_model(value) != normalize_model(model)
                or any(marker in model for marker in ("+", ",", "/"))
            ):
                continue
            for provenance in row.get("provenance", ()):
                if not isinstance(provenance, dict):
                    continue
                if (
                    str(provenance.get("source_product_id") or "") == key
                    and provenance.get("source_type") == "API"
                    and str(provenance.get("source_text") or "")
                    == f"modelName: {model}"
                ):
                    candidates.add(model)
                    break
        return next(iter(candidates)) if len(candidates) == 1 else None

    # A bundle listing writes its model as the set it sells: "MODEL+이동식
    # 스탠드". The base segment is the product; the rest is what the bundle adds.
    _BUNDLE_SEPARATORS = ("+", ",", "/")

    def _listing_declared_base_model(self, product_id: str) -> str | None:
        """The model this listing's own API record says it sells.

        ``_integrated_exact_model_for_listing`` refuses a compound identifier,
        and for this store most listings are compounds: the monitor is sold with
        a stand, so the API model name is "LS27FM501EKXKR+이동식 스탠드" and the
        listing resolved to no model at all. Every ``model_facts`` row is keyed
        by model, so those listings reached none of their own specification --
        measured over the inquiries customers sent, 1,076 of 3,419 were on a
        listing with no resolvable model.

        The base segment is taken only from an API row, which is the listing
        declaring its own product rather than a page image that may show
        several. It is accepted only when every such row reduces to the same
        catalogued model: a listing that declares two is ambiguous, and
        choosing one of them is how another product's specification would be
        presented as this one's.
        """

        key = str(product_id or "").strip()
        if not key:
            return None
        knowledge = self.catalog_repository.product_knowledge()
        resolved: set[str] = set()
        for row in knowledge.get("model_facts", ()) or ():
            if not isinstance(row, dict):
                continue
            if (row.get("field") != "model_code"
                    or row.get("subject") != "MAIN_PRODUCT"
                    or row.get("scope") != "EXACT_MODEL"):
                continue
            declares_this_listing = any(
                isinstance(provenance, dict)
                and provenance.get("source_type") == "API"
                and str(provenance.get("source_product_id") or "") == key
                for provenance in row.get("provenance", ()) or ()
            )
            if not declares_this_listing:
                continue
            value = str(row.get("value") or "").strip()
            for separator in self._BUNDLE_SEPARATORS:
                if separator in value:
                    value = value.split(separator)[0].strip()
            if not value:
                continue
            match = self.catalog_repository.match(model_code=value)
            if match.model_key and match.status in {EXACT, UNIQUE_MATCH}:
                resolved.add(match.model_key)
        return next(iter(resolved)) if len(resolved) == 1 else None

    def _integrated_product_knowledge_facts(
        self, *, product_id: str, model_key: str, fields: Sequence[str],
        include_all: bool,
    ) -> tuple[list[ProductFact], list[ProductFact]]:
        """Read candidate evidence from the same catalog JSON.

        This is identity/scope/status filtering only.  It does not score a
        question or choose which fact answers it; the GPT prompt receives the
        retained evidence and makes that judgement.  The fixed cap is solely a
        technical prompt-size bound.
        """
        knowledge = self.catalog_repository.product_knowledge()
        if not knowledge:
            return [], []
        key = str(product_id or "").strip()
        model_norm = normalize_model(model_key)
        aliases = self.catalog_repository.catalog().get("aliases")
        canonical_model = canonical_model_identity(
            model_key,
            aliases=aliases,
        )
        withheld = {
            (normalize_model(item.get("model_code")), str(item.get("field") or ""))
            for item in knowledge.get("v7_final_decisions", ())
            if isinstance(item, dict)
            and item.get("final_decision") == "UNRESOLVED_WITHHELD"
        }
        safe: list[ProductFact] = []
        excluded: list[ProductFact] = []
        requested = {str(item) for item in fields}
        for section in _PRODUCT_KNOWLEDGE_SECTIONS:
            rows = knowledge.get(section, ())
            if not isinstance(rows, list):
                continue
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                subject = str(row.get("subject") or "")
                scope = str(row.get("scope") or "")
                field_key = str(row.get("field") or "")
                row_model = normalize_model(row.get("model_code"))
                row_canonical = canonical_model_identity(
                    row.get("model_code"),
                    aliases=aliases,
                )
                applies = str(
                    row.get("applies_to_product_id") or row.get("product_id") or ""
                )
                model_scoped = section == "model_facts"
                identity_matches = (
                    bool(
                        model_norm
                        and (
                            row_model == model_norm
                            or (
                                canonical_model is not None
                                and row_canonical == canonical_model
                            )
                        )
                    )
                    if model_scoped else bool(
                        key and key in {
                            applies,
                            *(str(item) for item in row.get("source_product_ids", ()) if item),
                        }
                    )
                )
                if not identity_matches:
                    continue
                # MULTI_MODEL evidence is never elected for one model.
                if scope in {"UNKNOWN_SCOPE", "MULTI_MODEL"} or row.get("scope_status") != "RESOLVED":
                    continue
                if (row_model, field_key) in withheld:
                    continue
                value = row.get("runtime_value", row.get("value"))
                status = str(row.get("operational_status") or "")
                value_state = row.get("value_state")
                if (
                    status not in _RUNTIME_PRODUCT_KNOWLEDGE_STATUSES
                    or value_state == "EXPLICIT_NA" or _is_empty(value)
                ):
                    continue
                # Keyword mode retains its legacy bounded behavior.  The GPT
                # evidence route intentionally receives the whole retained
                # product/listing record and decides relevance itself.
                if not include_all and field_key not in requested:
                    continue
                component_scope = (
                    "POLICY" if subject.endswith("_POLICY") else
                    "BUNDLE_ACCESSORY" if subject.startswith("BUNDLED_") or subject == "ACCESSORY" else
                    "LISTING" if subject in {"LISTING", "OPTION"} else BASE_DEVICE_SCOPE
                )
                provenance = tuple(
                    item for item in row.get("provenance", ())
                    if isinstance(item, dict)
                )
                source_type = str(
                    row.get("source_type") or (provenance[0].get("source_type") if provenance else "")
                ) or None
                safe.append(ProductFact(
                    product_id=key or applies or model_key,
                    listing_id=applies or key or model_key,
                    model_code=str(row.get("model_code") or model_key) or None,
                    field_key=field_key, value=value, raw_value=row.get("value"),
                    unit=row.get("unit"), scope=scope,
                    scope_key=str(row.get("model_code") or applies or key or model_key),
                    component_scope=component_scope,
                    volatility=("SEMI_STATIC_POLICY_FACT" if component_scope == "POLICY" else "STATIC_PRODUCT_FACT"),
                    verification_status=status, resolution_status=str(row.get("scope_status") or ""),
                    lifecycle_status=ACTIVE,
                    canonical_fact_id=f"integrated:{section}:{index}", value_id=None,
                    provenance=provenance, safe_for_answer=True,
                    subject=subject, applies_to_product_id=(applies or key or None),
                    source_type=source_type,
                ))
        resolved, conflict_excluded = _resolve_exact_fact_conflicts(safe)
        excluded.extend(conflict_excluded)
        return resolved[:_PRODUCT_KNOWLEDGE_PROMPT_LIMIT], excluded

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
            # A stored ``false`` in this catalogue is an absence of evidence,
            # not evidence of absence: 805 of the 1,586 records carry
            # ``speaker: false`` and not one of their specs says the speaker
            # is missing. Offering it as a fact published "스피커 없음" from a
            # blank field, so a negative is quoted only where the source
            # states one -- which this catalogue never does.
            if value is False:
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
