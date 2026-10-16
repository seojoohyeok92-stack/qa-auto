"""One vocabulary for the legacy ``spec`` text, shared by both sides.

``MODEL_CATALOG[*].spec`` is free text, and two different places used to read
it with two different wording lists: ``fields_for_question`` decided which
fields a customer's question asks for, and ``_catalog_facts`` decided which
fields the spec supplies. They drifted. Measured on the catalog of 2026-09-27:
the supply list knew "다리 사이" and the question list did not, so
"다리 사이 간격이 80cm 이하인가요?" never asked for ``stand_spacing`` at all,
while "다리 간격이 얼마인가요?" -- the same question -- did. The field existed,
the value existed, and the wording decided whether they ever met.

So the wording lives here once. :data:`SPEC_ATTRIBUTES` is the only place that
says what an attribute is called, in a spec and in a question, and both sides
read it. A term added for one side is automatically available to the other.

Two kinds of attribute, deliberately not one:

``PRESENCE``
    "does this product have Wi-Fi". The spec mentioning the word is the whole
    of the evidence, so the matched text is what travels.

``MEASUREMENT``
    "how far apart are the legs". A number is being asked for, and the only
    acceptable answer is that label's own value. :func:`facts_from_spec`
    returns nothing when it cannot read one -- it never falls back to the spec
    blob. Before this split, ``stand_spacing``'s value *was* the whole spec
    string, so a question about 80cm was handed 250 characters of unrelated
    specification and could not be answered from it.

Labels this module does not map are not silently dropped: they are reported by
:func:`unmapped_labels`, which is how the next attribute gets added. A label
whose meaning is unclear, or whose text ran together with its neighbour, stays
unmapped on purpose -- an ambiguous value must not become a fact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Iterable

MEASUREMENT = "MEASUREMENT"
PRESENCE = "PRESENCE"


@dataclass(frozen=True)
class SpecAttribute:
    """One attribute, named once for the spec side and the question side."""

    field_key: str
    kind: str
    #: Labels as the spec writes them, longest-first at match time.
    spec_labels: tuple[str, ...] = ()
    #: Wording a customer uses. Matched as substrings of the lowered question.
    question_terms: tuple[str, ...] = ()
    unit: str | None = None
    #: For a label whose value is itself a list of labelled parts, such as
    #: "스탠드 결합 세로길이: 상단결합시: 609.7mm / 하단결합시: 634.7mm / 차이: 25mm".
    nested: tuple[tuple[str, str], ...] = dataclass_field(default_factory=tuple)
    #: True when the question side must not infer this attribute from wording
    #: alone; it is supplied by the spec but asked for by other means.
    supply_only: bool = False
    #: Field names this attribute's wording should also request. The stored
    #: Product Knowledge records one attribute under several names -- a body
    #: colour is ``color``, ``front_color``, ``rear_color`` and
    #: ``color_options`` depending on the row -- and a question about it has to
    #: ask for all of them or the one the record happens to use is missed.
    also_fields: tuple[str, ...] = dataclass_field(default_factory=tuple)
    #: Wording that takes this attribute *out* of the question. "스탠드 색상"
    #: contains "색상", and answering it with the body colour would be the
    #: wrong subject -- the component has its own field.
    not_when: tuple[str, ...] = dataclass_field(default_factory=tuple)
    #: True when the catalogue writes this attribute's value as a list of
    #: items separated by the same comma that separates one label from the
    #: next, so the items after the first are cut off by the split. Measured,
    #: not assumed: "시인성 향상: Audio Description, Zoom Menu and Text,
    #: High Contrast, SeeColors, Color Inversion, Grayscale, Picture Off" is
    #: one list of seven, and the reader kept the first.
    list_value: bool = False
    #: Sub-labels that belong to this attribute instead of naming one of
    #: their own, and so continue its value past a comma. "틸트(앞뒤): 앞:
    #: 3.0° , 뒤: 25.0°" is one range written in two directions, and
    #: "스위블(좌우): 좌,우: 45.0°" was read as the single word "좌".
    continues: tuple[str, ...] = dataclass_field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The registry. Grouped by what the attribute is about, so a reader can see
# which parts of a product are covered and which are not yet.
# ---------------------------------------------------------------------------
SPEC_ATTRIBUTES: tuple[SpecAttribute, ...] = (
    # -- stand geometry ---------------------------------------------------
    SpecAttribute(
        "stand_spacing", MEASUREMENT,
        spec_labels=("스탠드 다리 사이 간격", "스탠드 다리 간격", "다리 사이 간격",
                     "다리 간격", "스탠드 간격", "받침대 사이 간격", "받침대 간격",
                     "stand spacing", "leg spacing", "stand gap"),
        question_terms=("다리 사이 간격", "다리사이 간격", "다리 사이", "다리사이",
                        "스탠드 다리 간격", "스탠드다리 간격", "다리 간격",
                        "다리간격", "스탠드 간격", "스탠드간격",
                        "받침대 사이 간격", "받침대 사이", "받침대 간격",
                        "stand spacing", "leg spacing", "stand gap"),
        unit="mm",
    ),
    SpecAttribute(
        "stand_mount_height", MEASUREMENT,
        spec_labels=("스탠드 결합 세로길이",),
        question_terms=("스탠드 결합", "결합 세로길이", "결합높이", "결합 높이",
                        "상단결합", "하단결합", "상단 결합", "하단 결합"),
        unit="mm",
        nested=(("상단결합시", "stand_mount_height_upper"),
                ("하단결합시", "stand_mount_height_lower"),
                ("차이", "stand_mount_height_difference")),
    ),
    SpecAttribute(
        "dimensions_with_stand", MEASUREMENT,
        spec_labels=("크기(스탠드 포함)",),
        question_terms=("스탠드 포함 크기", "스탠드포함 크기", "스탠드 포함 사이즈"),
        unit="mm",
    ),
    SpecAttribute(
        "weight_with_stand", MEASUREMENT,
        spec_labels=("무게(스탠드 포함)",),
        question_terms=("스탠드 포함 무게", "스탠드포함 무게"),
        unit="kg",
    ),
    SpecAttribute(
        "dimensions_without_stand", MEASUREMENT,
        spec_labels=("스탠드 제외 크기(가로x높이x깊이)", "스탠드 제외 크기"),
        question_terms=("스탠드 제외 크기", "스탠드제외 크기"),
        unit="mm",
    ),
    SpecAttribute(
        "stand_type_spec", MEASUREMENT,
        spec_labels=("스탠드 형태",), supply_only=True,
    ),
    SpecAttribute(
        "stand_color_spec", MEASUREMENT,
        spec_labels=("스탠드 색상",), supply_only=True,
    ),
    # -- stand articulation ----------------------------------------------
    SpecAttribute(
        "tilt_range_spec", MEASUREMENT,
        spec_labels=("틸트(앞뒤)", "틸트(상하)", "틸트"),
        continues=("앞", "뒤", "위", "아래"),
        question_terms=("틸트", "앞뒤 각도", "상하 각도"),
    ),
    SpecAttribute(
        "swivel_range_spec", MEASUREMENT,
        spec_labels=("스위블(좌우)", "스위블"),
        continues=("좌", "우", "좌우"),
        question_terms=("스위블", "좌우 회전"),
    ),
    SpecAttribute(
        "pivot_spec", MEASUREMENT,
        spec_labels=("피벗(가로세로)", "피벗(회전)", "피벗"),
        question_terms=("피벗", "가로세로 회전"),
    ),
    SpecAttribute(
        "elevation_range_spec", MEASUREMENT,
        spec_labels=("엘리베이션(높낮이)", "엘리베이션"),
        question_terms=("엘리베이션", "높낮이", "높이 조절", "높이조절"),
        unit="mm",
    ),
    SpecAttribute(
        "measurement_tolerance", MEASUREMENT,
        spec_labels=("오차범위",),
        question_terms=("오차범위", "오차 범위"), unit="mm",
    ),
    # -- physical ---------------------------------------------------------
    SpecAttribute(
        "dimensions_product_spec", MEASUREMENT,
        spec_labels=("제품크기(가로x높이x깊이)", "크기(가로x세로x깊이)"),
        question_terms=("제품 크기", "제품크기", "본체 크기"), unit="mm",
    ),
    SpecAttribute(
        "dimensions_package_spec", MEASUREMENT,
        spec_labels=("포장크기(가로x높이x깊이)", "포장크기"),
        question_terms=("포장 크기", "포장크기", "박스 크기", "박스크기"),
        unit="mm",
    ),
    SpecAttribute(
        "weight_product_spec", MEASUREMENT,
        spec_labels=("제품 무게",),
        question_terms=("제품 무게", "제품무게", "본체 무게"), unit="kg",
    ),
    SpecAttribute(
        "weight_package_spec", MEASUREMENT,
        spec_labels=("무게 (박스 포함)", "무게(박스 포함)"),
        question_terms=("박스 포함 무게", "포장 무게", "박스포함 무게"),
        unit="kg",
    ),
    SpecAttribute(
        "display_width_mm", MEASUREMENT,
        spec_labels=("디스플레이 가로 길이",),
        question_terms=("디스플레이 가로", "화면 가로 길이"), unit="mm",
    ),
    SpecAttribute(
        "display_height_mm", MEASUREMENT,
        spec_labels=("디스플레이 세로 길이",),
        question_terms=("디스플레이 세로", "화면 세로 길이"), unit="mm",
    ),
    SpecAttribute(
        "vesa_hole_spec", MEASUREMENT,
        spec_labels=("베사홀",), question_terms=("베사홀", "베사 홀"), unit="mm",
    ),
    SpecAttribute(
        "pixel_pitch", MEASUREMENT,
        spec_labels=("픽셀피치",), question_terms=("픽셀피치", "픽셀 피치"),
        unit="mm",
    ),
    SpecAttribute(
        "aspect_ratio", MEASUREMENT,
        spec_labels=("화면비율",), question_terms=("화면비율", "화면 비율"),
    ),
    # -- ports and connectivity ------------------------------------------
    SpecAttribute(
        "hdmi_port_count_spec", MEASUREMENT,
        spec_labels=("HDMI 단자", "[규격] HDMI(전체)", "HDMI"),
        question_terms=("hdmi 개수", "hdmi 몇", "hdmi 단자", "hdmi포트"),
        unit="개",
    ),
    SpecAttribute(
        "usb_port_count_spec", MEASUREMENT,
        spec_labels=("USB Ports", "USB 허브 단자", "USB"),
        question_terms=("usb 개수", "usb 몇", "usb 단자", "usb포트"),
        unit="개",
    ),
    SpecAttribute(
        "display_port_count", MEASUREMENT,
        spec_labels=("Display Port", "DP 단자"),
        question_terms=("display port", "displayport", "dp 단자", "dp 개수"),
        unit="개",
    ),
    SpecAttribute(
        "dsub_port_count", MEASUREMENT,
        spec_labels=("D-SUB 단자",),
        question_terms=("d-sub", "dsub", "디서브"), unit="개",
    ),
    SpecAttribute(
        "ethernet_port_count_spec", MEASUREMENT,
        spec_labels=("Ethernet (LAN 단자)",),
        question_terms=("lan 단자", "랜 단자", "랜포트", "lan 개수"), unit="개",
    ),
    SpecAttribute(
        "rf_port_count", MEASUREMENT,
        spec_labels=("RF단자",), question_terms=("rf 단자", "rf 개수"), unit="개",
    ),
    SpecAttribute(
        "hdmi_arc_spec", MEASUREMENT,
        spec_labels=("HDMI ARC",), question_terms=("hdmi arc", "arc", "earc"),
    ),
    SpecAttribute(
        "power_cable_length", MEASUREMENT,
        spec_labels=("전원 연결선(전원 케이블)", "전원 케이블"),
        question_terms=("전원 케이블", "전원선 길이", "케이블 길이"), unit="m",
    ),
    # -- panel and picture ------------------------------------------------
    SpecAttribute(
        "refresh_rate_spec", MEASUREMENT,
        spec_labels=("주사율",), question_terms=("주사율", "refresh rate"),
        unit="Hz",
    ),
    SpecAttribute(
        "contrast_spec", MEASUREMENT,
        spec_labels=("명암비",), question_terms=("명암비", "contrast"),
    ),
    SpecAttribute(
        "picture_engine_spec", MEASUREMENT,
        spec_labels=("화질엔진",), question_terms=("화질엔진", "화질 엔진"),
    ),
    SpecAttribute(
        "dimming_spec", MEASUREMENT,
        spec_labels=("디밍기술",), question_terms=("디밍", "dimming"),
    ),
    SpecAttribute(
        "motion_spec", MEASUREMENT,
        spec_labels=("모션 성능",), question_terms=("모션 성능", "모션성능"),
    ),
    SpecAttribute(
        "upscaling_spec", MEASUREMENT,
        spec_labels=("AI 화질 업스케일링",),
        question_terms=("업스케일링", "upscaling"),
    ),
    SpecAttribute(
        "hdr_spec", MEASUREMENT,
        spec_labels=("HDR(High Dynamic Range)", "HDR10+", "HDR"),
        # The standard's own names are how customers ask about it, and they
        # are model-code-shaped -- which is why they have to be in the
        # registry rather than in a stopword list somewhere else.
        question_terms=("hdr", "hdr10", "hdr10+", "hdr 10"),
    ),
    SpecAttribute(
        "color_gamut_srgb", MEASUREMENT,
        spec_labels=("[색상영역] sRGB", "sRGB"),
        question_terms=("srgb",), unit="%",
    ),
    SpecAttribute(
        "color_gamut_dci_p3", MEASUREMENT,
        spec_labels=("[색상영역] DCI-P3", "DCI-P3"),
        question_terms=("dci-p3", "dci p3"), unit="%",
    ),
    SpecAttribute(
        "color_gamut_ntsc", MEASUREMENT,
        spec_labels=("[색상영역] NTSC", "NTSC"),
        question_terms=("ntsc",), unit="%",
    ),
    # -- sound ------------------------------------------------------------
    SpecAttribute(
        "speaker_output_spec", MEASUREMENT,
        spec_labels=("사운드 출력 (RMS)", "사운드 출력"),
        question_terms=("사운드 출력", "스피커 출력", "출력 w"), unit="W",
    ),
    SpecAttribute(
        "speaker_type_spec", MEASUREMENT,
        spec_labels=("스피커 종류",), question_terms=("스피커 종류",),
    ),
    SpecAttribute(
        "ots_spec", MEASUREMENT,
        spec_labels=("무빙 사운드 (OTS)", "무빙 사운드"),
        question_terms=("무빙 사운드", "ots"),
    ),
    SpecAttribute(
        "q_symphony_spec", MEASUREMENT,
        spec_labels=("Q-Symphony",), question_terms=("q-symphony", "q심포니"),
    ),
    # -- system and power -------------------------------------------------
    SpecAttribute(
        "os_spec", MEASUREMENT,
        # No bare "os": it appears inside unrelated words. The Korean label
        # and the product name are the reliable wordings.
        spec_labels=("운영체제",), question_terms=("운영체제", "타이젠", "tizen"),
    ),
    SpecAttribute(
        "release_ym_spec", MEASUREMENT,
        spec_labels=("동일 모델의 출시연월", "동일모델의 출시년월", "출시년도"),
        question_terms=("출시연월", "출시년월", "출시 시기", "출시일", "몇년형",
                        "몇 년형"),
    ),
    SpecAttribute(
        "daily_usage_hours_spec", MEASUREMENT,
        spec_labels=("제품 사용 시간",),
        question_terms=("사용 시간", "사용시간", "하루 몇 시간"),
    ),
    SpecAttribute(
        "energy_grade_spec", MEASUREMENT,
        spec_labels=("에너지효율 등급",),
        question_terms=("에너지효율", "에너지 효율", "소비효율 등급"),
    ),
    SpecAttribute(
        "safety_standard_spec", MEASUREMENT,
        list_value=True,
        spec_labels=("안전 규격",), question_terms=("안전 규격", "안전규격"),
    ),
    SpecAttribute(
        "emc_spec", MEASUREMENT,
        spec_labels=("EMC",), question_terms=("emc",),
    ),
    SpecAttribute(
        "broadcast_standard_spec", MEASUREMENT,
        spec_labels=("디지털 방송방식",),
        question_terms=("방송방식", "방송 방식", "atsc"),
    ),
    SpecAttribute(
        "pd_charging_spec", MEASUREMENT,
        spec_labels=("PD충전",), question_terms=("pd충전", "pd 충전"),
    ),
    # -- design and accessibility ----------------------------------------
    SpecAttribute(
        "design_spec", MEASUREMENT,
        spec_labels=("디자인",), question_terms=("디자인",),
    ),
    SpecAttribute(
        "bezel_type_spec", MEASUREMENT,
        spec_labels=("Bezel Type",), question_terms=("베젤", "bezel"),
    ),
    SpecAttribute(
        "slim_type_spec", MEASUREMENT,
        spec_labels=("Slim Type",), question_terms=("slim",),
    ),
    SpecAttribute(
        "front_color_spec", MEASUREMENT,
        spec_labels=("전면 색상",), question_terms=("전면 색상", "전면색상"),
    ),
    SpecAttribute(
        "remote_model", MEASUREMENT,
        spec_labels=("리모컨",), question_terms=("리모컨 모델", "리모컨 모델명"),
    ),
    SpecAttribute(
        "hdmi_cable_included_spec", MEASUREMENT,
        spec_labels=("HDMI 케이블",),
        question_terms=("hdmi 케이블",),
    ),
    SpecAttribute(
        "accessibility_visual", MEASUREMENT,
        list_value=True,
        spec_labels=("시인성 향상",), question_terms=("시인성",),
    ),
    SpecAttribute(
        "accessibility_hearing", MEASUREMENT,
        list_value=True,
        spec_labels=("청각 보조",), question_terms=("청각 보조", "자막", "자캡션"),
    ),
    SpecAttribute(
        "accessibility_voice_guide", MEASUREMENT,
        list_value=True,
        spec_labels=("음성 안내 지원",), question_terms=("음성 안내", "음성안내"),
    ),
    SpecAttribute(
        "remote_repeat_spec", MEASUREMENT,
        list_value=True,
        spec_labels=("리모컨 반응 속도 조정",), question_terms=("리모컨 반응",),
    ),
    SpecAttribute(
        "mobile_connect_spec", MEASUREMENT,
        list_value=True,
        spec_labels=("모바일 무선 연결",),
        question_terms=("모바일 연결", "미러링"),
    ),
    # -- monitor features -------------------------------------------------
    SpecAttribute(
        "kvm_switch", MEASUREMENT,
        spec_labels=("KVM 스위치",), question_terms=("kvm",),
    ),
    SpecAttribute(
        "daisy_chain", MEASUREMENT,
        spec_labels=("데이지 체인",), question_terms=("데이지 체인", "데이지체인"),
    ),
    SpecAttribute(
        "usb_pd_support", MEASUREMENT,
        spec_labels=("USB-PD",), question_terms=("usb-pd", "usb pd"),
    ),
    SpecAttribute(
        "docking_station", MEASUREMENT,
        spec_labels=("도킹스테이션",), question_terms=("도킹", "docking"),
    ),
    SpecAttribute(
        "thunderbolt4", MEASUREMENT,
        spec_labels=("썬더볼트4",), question_terms=("썬더볼트", "thunderbolt"),
    ),
    SpecAttribute(
        "sw_screen_split", MEASUREMENT,
        spec_labels=("SW 화면분할",), question_terms=("화면분할", "화면 분할"),
    ),
    # -- brightness, kept apart by condition ------------------------------
    # The stored Product Knowledge names the typical reading
    # ``brightness_typical`` and the peak ``brightness_peak``; the question
    # router's own name for the typical is ``brightness_typical_cd_m2``. All
    # of them are asked for, because which one the record happens to use is
    # not something the question can know.
    #
    # Measured on the catalog of 2026-09-27: of the 14 models that carry both
    # an API-sourced ``brightness_typical`` and a bare "<N>cd" token in their
    # spec, 13 agree -- the token *is* the typical. One does not: S49CG954's
    # spec says 1,000cd while the API says 450, and the same spec carries
    # "VESA HDR 1000". That one value is the HDR peak, and
    # ``_peak_brightness_relabel`` moves it rather than letting it answer
    # "밝기 몇 니트인가요?" as the typical.
    SpecAttribute(
        "brightness_typical_cd_m2", MEASUREMENT,
        spec_labels=("밝기",),
        question_terms=("밝기", "휘도", "니트", "nit"),
        # The peak travels with the typical: a brightness question has to see
        # both conditions, or the model answers "450" for a panel the
        # catalogue also records at 1,000cd under HDR. They stay separate
        # fields -- this only requests them together.
        also_fields=("brightness_typical", "brightness", "brightness_min",
                     "brightness_peak"),
        unit="cd/m2",
    ),
    SpecAttribute(
        "brightness_peak", MEASUREMENT,
        spec_labels=(),
        question_terms=("피크 밝기", "최대 밝기", "최대밝기", "hdr 최대",
                        "최대 휘도", "peak brightness"),
        also_fields=("brightness_peak_cd_m2", "hdr_certification"),
        unit="cd/m2",
    ),
    SpecAttribute(
        "hdr_certification", MEASUREMENT,
        spec_labels=(),
        question_terms=("displayhdr", "vesa hdr", "hdr 인증", "hdr 등급"),
    ),
    # -- colour, kept apart by subject -----------------------------------
    # A general colour question is about the product's own body. The record
    # writes that under four names, so all four are asked for. The stand has
    # its own colour and its own field: answering "스탠드 색상" with the body
    # colour would be the wrong subject, so the component wordings take this
    # attribute out of the question rather than competing with it. The colour
    # *gamut* fields (sRGB/DCI-P3/NTSC coverage) are a different attribute
    # again and keep their own terms.
    SpecAttribute(
        "color", MEASUREMENT,
        spec_labels=("색상", "제품 색상"),
        question_terms=("색상", "색깔", "컬러", "무슨 색", "어떤 색",
                        "색이 뭐", "색상이 뭐", "색깔이 뭐", "몇 가지 색"),
        also_fields=("front_color", "rear_color", "color_options",
                     "front_color_spec"),
        not_when=("스탠드 색", "스탠드색", "받침대 색", "받침대색",
                  "거치대 색", "거치대색", "색영역", "색상영역", "색재현",
                  "gamut", "srgb", "dci", "ntsc", "adobe"),
    ),
    SpecAttribute(
        "stand_color", MEASUREMENT,
        spec_labels=(),
        question_terms=("스탠드 색", "스탠드색", "받침대 색", "받침대색",
                        "거치대 색", "거치대색", "다리 색", "다리색"),
        also_fields=("stand_color_spec", "accessory_color"),
    ),
    SpecAttribute(
        "rear_color", MEASUREMENT,
        spec_labels=("후면 색상", "후면"),
        question_terms=("후면 색", "후면색", "뒷면 색", "뒷면색", "뒤쪽 색"),
    ),
    # The positional dialect writes the contrast as a ratio ("3,000:1") while
    # the labelled one writes a marketing name ("Mega Contrast"). Both answer
    # "명암비가 어떻게 되나요?", and ``contrast_ratio`` is the name the stored
    # Product Knowledge already uses for the ratio, so it is reused rather
    # than renamed. No spec label of its own: a positional token has none.
    SpecAttribute(
        "contrast_ratio", MEASUREMENT,
        spec_labels=(), question_terms=("명암비", "명암 비", "contrast"),
    ),
    # -- 2-b: attributes a bracketed section states ----------------------
    # These are asked on their own and no existing field holds them. Each one
    # is supplied by :func:`facts_from_section_spec`, where the section
    # heading is part of the evidence: "92%" names nothing, and
    # "[색상영역] DCI-P3: 92%" names a gamut.
    SpecAttribute(
        "color_gamut_adobe_rgb", MEASUREMENT, spec_labels=("Adobe RGB",),
        question_terms=("adobe rgb", "adobergb", "어도비 rgb", "어도비rgb"),
        unit="%",
    ),
    SpecAttribute(
        "color_bit_depth", MEASUREMENT, spec_labels=(),
        question_terms=("10비트", "8비트", "10bit", "8bit", "비트 심도",
                        "색 심도", "컬러 심도", "bit depth", "색상 심도"),
    ),
    # Three certifications, not one feature. A panel can carry FreeSync
    # Premium Pro and G-Sync compatible at once, and answering "지싱크
    # 되나요?" from a FreeSync token would be a different product's claim.
    SpecAttribute(
        "freesync_spec", MEASUREMENT, spec_labels=(),
        question_terms=("프리싱크", "프리 싱크", "freesync", "free sync"),
    ),
    SpecAttribute(
        "gsync_spec", MEASUREMENT, spec_labels=(),
        question_terms=("지싱크", "지 싱크", "gsync", "g-sync", "g sync"),
    ),
    SpecAttribute(
        "adaptive_sync_spec", MEASUREMENT, spec_labels=(),
        question_terms=("어댑티브 싱크", "어댑티브싱크", "adaptive sync"),
    ),
    SpecAttribute(
        "vrr_spec", MEASUREMENT, spec_labels=(),
        question_terms=("vrr", "가변 주사율", "가변주사율", "allm"),
        also_fields=("freesync_spec", "gsync_spec", "adaptive_sync_spec"),
    ),
    SpecAttribute(
        "crosshair_spec", MEASUREMENT, spec_labels=(),
        question_terms=("조준선", "크로스헤어", "crosshair"),
    ),
    SpecAttribute(
        "black_equalizer_spec", MEASUREMENT, spec_labels=(),
        question_terms=("블랙 이퀄라이저", "블랙이퀄라이저", "블랙 안정화",
                        "black equalizer"),
    ),
    SpecAttribute(
        "input_lag_control_spec", MEASUREMENT, spec_labels=(),
        question_terms=("인풋랙", "인풋 랙", "입력 지연", "input lag"),
    ),
    # A port's presence, its revision and how many there are are three
    # questions. The count fields already exist; these do not.
    SpecAttribute(
        "hdmi_version_spec", MEASUREMENT, spec_labels=(),
        question_terms=("hdmi 버전", "hdmi 규격", "hdmi 2.0", "hdmi 2.1",
                        "hdmi2.0", "hdmi2.1"),
    ),
    SpecAttribute(
        "hdmi_variant_spec", MEASUREMENT, spec_labels=(),
        question_terms=("미니 hdmi", "mini hdmi", "마이크로 hdmi",
                        "micro hdmi"),
    ),
    SpecAttribute(
        "displayport_present", MEASUREMENT, spec_labels=(),
        question_terms=("디스플레이포트", "display port", "displayport",
                        "dp 포트", "dp 있"),
    ),
    SpecAttribute(
        "displayport_version_spec", MEASUREMENT, spec_labels=(),
        question_terms=("dp 버전", "dp 1.4", "dp 2.1", "displayport 버전"),
    ),
    SpecAttribute(
        "displayport_variant_spec", MEASUREMENT, spec_labels=(),
        question_terms=("미니 dp", "mini dp", "미니 디스플레이포트"),
    ),
    SpecAttribute(
        "dsub_present", MEASUREMENT, spec_labels=(),
        question_terms=("d-sub", "dsub", "디서브", "d서브", "아날로그 단자"),
    ),
    SpecAttribute(
        "dvi_present", MEASUREMENT, spec_labels=(),
        question_terms=("dvi", "디비아이"),
    ),
    SpecAttribute(
        "usb_c_present", MEASUREMENT, spec_labels=(),
        question_terms=("usb c", "usb-c", "c타입", "타입c", "type-c",
                        "type c", "씨타입"),
    ),
    SpecAttribute(
        "thunderbolt_spec", MEASUREMENT, spec_labels=(),
        question_terms=("썬더볼트", "thunderbolt"),
    ),
    SpecAttribute(
        "mhl_present", MEASUREMENT, spec_labels=(),
        question_terms=("mhl",),
    ),
    SpecAttribute(
        "component_present", MEASUREMENT, spec_labels=(),
        question_terms=("컴포넌트", "component 단자"),
    ),
    SpecAttribute(
        "composite_present", MEASUREMENT, spec_labels=(),
        question_terms=("컴포지트", "av 단자", "composite"),
    ),
    SpecAttribute(
        "remote_present", MEASUREMENT, spec_labels=(),
        question_terms=("리모컨", "리모콘"),
    ),
    SpecAttribute(
        "wireless_display_spec", MEASUREMENT, spec_labels=(),
        question_terms=("무선 디스플레이", "무선디스플레이", "wireless display",
                        "무선 display"),
    ),
    SpecAttribute(
        "smart_tv_spec", MEASUREMENT, spec_labels=(),
        question_terms=("스마트tv", "스마트 tv", "스마트티비", "스마트 티비"),
    ),
    SpecAttribute(
        "airplay_spec", MEASUREMENT, spec_labels=(),
        question_terms=("에어플레이", "airplay", "아이폰 연동", "아이폰연동"),
    ),
    # Whether the set runs OTT at all, which apps it names, and the Samsung
    # channel service are three statements; 11 models say 삼성TV+ 사용 불가
    # *and* 자체 OTT 지원 불가, which is why they are not one field.
    SpecAttribute(
        "ott_support_spec", MEASUREMENT, spec_labels=(),
        question_terms=("ott", "오티티"),
        also_fields=("ott_apps_spec", "samsung_tv_plus_spec"),
    ),
    SpecAttribute(
        "ott_apps_spec", MEASUREMENT, spec_labels=(),
        question_terms=("넷플릭스", "netflix", "유튜브", "youtube", "디즈니",
                        "웨이브", "티빙", "쿠팡플레이", "왓챠", "라프텔"),
        also_fields=("ott_support_spec",),
    ),
    SpecAttribute(
        "samsung_tv_plus_spec", MEASUREMENT, spec_labels=(),
        question_terms=("삼성tv플러스", "삼성 tv 플러스", "삼성tv+", "tv플러스",
                        "lg채널"),
    ),
    SpecAttribute(
        "smart_feature_spec", MEASUREMENT, spec_labels=(),
        question_terms=("실시간 번역", "실시간번역", "음성인식", "음성 인식"),
    ),
    SpecAttribute(
        "smart_home_spec", MEASUREMENT, spec_labels=(),
        question_terms=("스마트홈", "스마트 홈", "지그비", "zigbee", "매터",
                        "matter"),
    ),
    # Two refresh rates under two resolutions. Promoting either as the plain
    # refresh rate would answer "몇 Hz인가요?" with a mode the customer did
    # not ask about, so the condition travels inside the value.
    SpecAttribute(
        "dual_mode_spec", MEASUREMENT, spec_labels=(),
        question_terms=("듀얼 해상도", "듀얼해상도", "해상도 전환", "dual mode"),
    ),
    SpecAttribute(
        "battery_capacity_spec", MEASUREMENT, spec_labels=("배터리 용량",),
        question_terms=("배터리", "mah"),
    ),
    SpecAttribute(
        "touch_support_spec", MEASUREMENT, spec_labels=(),
        question_terms=("터치", "touch"),
    ),
    SpecAttribute(
        "flicker_free_spec", MEASUREMENT, spec_labels=(),
        question_terms=("플리커", "flicker", "깜빡임"),
    ),
    SpecAttribute(
        "hdr_tuning_spec", MEASUREMENT, spec_labels=(),
        question_terms=("톤매핑", "톤 매핑", "hgig", "hdr 자동"),
    ),
    # How the product stands. Separate from the wall-mount *policy*, which is
    # a seller rule and not a catalogue measurement.
    SpecAttribute(
        "form_factor_spec", MEASUREMENT, spec_labels=(),
        question_terms=("설치 형태", "거치 형태", "스탠드형", "포터블",
                        "이동형", "벽걸이형"),
    ),
    SpecAttribute(
        "stand_width_spec", MEASUREMENT, spec_labels=("스탠드너비",),
        question_terms=("스탠드 너비", "스탠드너비", "받침대 너비"), unit="mm",
    ),
    # -- 2-c: what an unlabelled enumeration token states -----------------
    # The catalogue writes part of each spec as a bare list of words --
    # "틸트(상하) / 평면 / IPS / 헤드폰 아웃 / LED 조명" -- with no label and
    # no shape to recognise. Measured over the whole catalogue: 11,578 such
    # tokens on 1,569 models, which 2-a and 2-b both passed over.
    #
    # A capability stated without a range is a capability, not a range. The
    # range fields above stay what they are: 1,356 models say only that the
    # stand tilts, and reporting that as a tilt *angle* would invent a
    # measurement the spec does not make.
    SpecAttribute(
        "tilt", MEASUREMENT, spec_labels=(),
        question_terms=("틸트", "상하 각도", "각도 조절", "각도조절",
                        "기울기 조절", "기울일 수"),
        also_fields=("tilt_range_spec",),
    ),
    SpecAttribute(
        "swivel", MEASUREMENT, spec_labels=(),
        question_terms=("스위블", "좌우 회전", "좌우로 돌"),
        also_fields=("swivel_range_spec",),
    ),
    SpecAttribute(
        "pivot", MEASUREMENT, spec_labels=(),
        question_terms=("피벗", "피봇", "세로 회전", "세로로 돌",
                        "가로세로 회전"),
        also_fields=("pivot_spec",),
    ),
    SpecAttribute(
        "height_adjust", MEASUREMENT, spec_labels=(),
        question_terms=("높이 조절", "높이조절", "높낮이", "엘리베이션"),
        also_fields=("elevation_range_spec",),
    ),
    SpecAttribute(
        "hinge_type", MEASUREMENT, spec_labels=(),
        question_terms=("힌지", "듀얼힌지", "관절"),
    ),
    SpecAttribute(
        "auto_pivot", MEASUREMENT, spec_labels=(),
        question_terms=("오토피벗", "오토 피벗", "자동 회전", "자동 전환"),
    ),
    # -- the screen itself ------------------------------------------------
    SpecAttribute(
        "panel_type", MEASUREMENT, spec_labels=(),
        question_terms=("패널", "panel", "ips", "va 패널", "tn 패널",
                        "oled", "패널 종류", "패널종류"),
    ),
    SpecAttribute(
        "backlight_type", MEASUREMENT, spec_labels=(),
        question_terms=("백라이트", "back light", "backlight",
                        "led 백라이트"),
    ),
    SpecAttribute(
        "panel_surface", MEASUREMENT, spec_labels=(),
        question_terms=("강화유리", "표면 처리", "화면 표면", "유리 패널"),
    ),
    SpecAttribute(
        "flat_curved", MEASUREMENT, spec_labels=(),
        question_terms=("평면", "커브드", "곡면", "커브", "휘어",
                        "플랫", "curved"),
        also_fields=("curvature",),
    ),
    SpecAttribute(
        "curvature", MEASUREMENT, spec_labels=(),
        question_terms=("곡률", "곡률반경", "곡률 반경", "r값"),
    ),
    SpecAttribute(
        "screen_width_class", MEASUREMENT, spec_labels=(),
        question_terms=("와이드", "울트라와이드", "울트라 와이드",
                        "화면 비율 종류"),
        also_fields=("aspect_ratio",),
    ),
    SpecAttribute(
        "response_time_basis", MEASUREMENT, spec_labels=(),
        question_terms=("응답속도 기준", "응답속도기준", "gtg", "mprt"),
        also_fields=("response_time_ms",),
    ),
    # -- light, eye care, picture modes -----------------------------------
    SpecAttribute(
        "core_lighting", MEASUREMENT, spec_labels=(),
        question_terms=("led 조명", "조명", "라이팅", "앰비언트"),
    ),
    SpecAttribute(
        "eye_saver", MEASUREMENT, spec_labels=(),
        question_terms=("블루라이트", "블루 라이트", "눈 보호", "눈보호",
                        "저청색광"),
    ),
    SpecAttribute(
        "pip", MEASUREMENT, spec_labels=(),
        question_terms=("pip", "화면 안에 화면", "화면 속 화면"),
        also_fields=("pbp",),
    ),
    SpecAttribute(
        "pbp", MEASUREMENT, spec_labels=(),
        question_terms=("pbp", "화면 두 개", "두 화면", "화면 나눠",
                        "동시 입력"),
        also_fields=("pip",),
    ),
    SpecAttribute(
        "game_mode", MEASUREMENT, spec_labels=(),
        question_terms=("게임모드", "게임 모드", "game mode"),
    ),
    # -- sound and ports --------------------------------------------------
    SpecAttribute(
        "headphone_out", MEASUREMENT, spec_labels=(),
        question_terms=("헤드폰", "이어폰", "3.5mm", "오디오 아웃",
                        "오디오 출력 단자", "이어폰 단자"),
    ),
    SpecAttribute(
        "hdcp", MEASUREMENT, spec_labels=(),
        question_terms=("hdcp",),
    ),
    SpecAttribute(
        "usb_hub", MEASUREMENT, spec_labels=(),
        question_terms=("usb 허브", "허브 기능", "usb hub"),
    ),
    SpecAttribute(
        "vesa_screw", MEASUREMENT, spec_labels=(),
        question_terms=("나사 규격", "나사규격", "볼트 규격", "나사 크기"),
    ),
    SpecAttribute(
        "dp_cable_included", MEASUREMENT, spec_labels=(),
        question_terms=("dp 케이블", "디스플레이포트 케이블"),
    ),
    # -- power ------------------------------------------------------------
    SpecAttribute(
        "rated_voltage", MEASUREMENT, spec_labels=(),
        question_terms=("입력 전압", "전압", "전원 규격", "프리볼트"),
    ),
    SpecAttribute(
        "power_consumption_labelled", MEASUREMENT, spec_labels=(),
        question_terms=("소비전력", "소비 전력", "전력 소모", "전기 사용"),
        unit="W",
        also_fields=("power_typical", "power_max"),
        # The value this field holds is the one the source stated *without* a
        # condition: 7 models write "25W" and nothing else. It answers "소비
        # 전력이 얼마인가요" and it is not evidence for any condition, because
        # the source never said which draw it measured. "일반 소비전력" and
        # "최대 소비전력" both contain "소비전력", so without this a question
        # about one condition was handed the conditionless figure and the
        # answer could state it as that condition: measured on S27BG400,
        # "일반 소비전력은 48W입니다" from a bare 48W passed and auto-posted.
        # The condition-specific fields below are the only authority for a
        # condition, and a question that names one asks for them alone.
        not_when=("일반 소비전력", "일반소비전력", "표준 소비전력",
                  "표준소비전력", "typical", "최대 소비전력", "최대소비전력",
                  "max 소비전력", "대기 소비전력", "대기소비전력",
                  "대기 전력", "대기전력", "standby"),
    ),
    SpecAttribute(
        "power_typical", MEASUREMENT, spec_labels=(),
        question_terms=("일반 소비전력", "일반소비전력", "표준 소비전력",
                        "표준소비전력", "typical 소비전력"),
        unit="W",
    ),
    SpecAttribute(
        "power_max", MEASUREMENT, spec_labels=(),
        question_terms=("최대 소비전력", "최대소비전력", "max 소비전력"),
        unit="W",
    ),
    # The stored record has stated standby draw for 40 models all along, and
    # no wording reached it, so a standby question fell through to the
    # conditionless figure. Named here so the condition has its own
    # authority; nothing new is read from the catalogue for it.
    SpecAttribute(
        "power_standby", MEASUREMENT, spec_labels=(),
        question_terms=("대기 소비전력", "대기소비전력", "대기 전력",
                        "대기전력", "standby"),
        unit="W",
    ),
    # -- smart ------------------------------------------------------------
    SpecAttribute(
        "google_play", MEASUREMENT, spec_labels=(),
        question_terms=("구글플레이", "구글 플레이", "google play",
                        "플레이스토어"),
    ),
    SpecAttribute(
        "battery_included", MEASUREMENT, spec_labels=(),
        question_terms=("내장 배터리", "내장배터리", "배터리 내장",
                        "충전해서"),
        also_fields=("battery_capacity_spec",),
    ),
    # -- what the product is ----------------------------------------------
    SpecAttribute(
        "series_name", MEASUREMENT, spec_labels=(),
        question_terms=("시리즈", "series", "몇 시리즈"),
    ),
    SpecAttribute(
        "product_category", MEASUREMENT, spec_labels=(),
        question_terms=("품목", "제품 종류", "제품종류", "어떤 제품",
                        "모니터인가요", "tv인가요"),
    ),
    SpecAttribute(
        "usage_environment", MEASUREMENT, spec_labels=(),
        question_terms=("실내", "실외", "야외", "설치 환경"),
    ),
    SpecAttribute(
        "country_of_origin", MEASUREMENT, spec_labels=(),
        question_terms=("제조국", "원산지", "어디서 만들", "어디서 생산",
                        "생산지"),
    ),
    # -- presence: the spec naming it is the whole of the evidence --------
    SpecAttribute(
        "hdmi_present", PRESENCE, spec_labels=("HDMI",),
        question_terms=(), supply_only=True,
    ),
    SpecAttribute(
        "usb_present", PRESENCE, spec_labels=("USB",),
        question_terms=(), supply_only=True,
    ),
    SpecAttribute(
        "ethernet_present", PRESENCE,
        spec_labels=("LAN", "랜", "ETHERNET"),
        question_terms=("lan", "ethernet", "유선랜"),
    ),
    SpecAttribute(
        "rf_terminal", PRESENCE,
        spec_labels=("RF", "동축", "안테나"),
        question_terms=("rf", "coax", "antenna", "동축", "안테나"),
    ),
    SpecAttribute(
        "bluetooth_present", PRESENCE,
        spec_labels=("BLUETOOTH", "블루투스"),
        question_terms=(), supply_only=True,
    ),
    SpecAttribute(
        "wifi_present", PRESENCE,
        spec_labels=("WI-FI", "WIFI", "와이파이", "무선랜"),
        question_terms=(), supply_only=True,
    ),
)

#: The topic label reported for a question term match, per field.
_TOPIC_OF = {
    "rf_terminal": "rf",
    "ethernet_present": "ethernet",
}

PRESENCE_TOKENS: dict[str, tuple[str, ...]] = {
    item.field_key: item.spec_labels
    for item in SPEC_ATTRIBUTES if item.kind == PRESENCE
}
MEASUREMENT_ATTRIBUTES: tuple[SpecAttribute, ...] = tuple(
    item for item in SPEC_ATTRIBUTES if item.kind == MEASUREMENT
)
SPEC_FIELD_KEYS: frozenset[str] = frozenset(
    item.field_key for item in SPEC_ATTRIBUTES)
MEASUREMENT_FIELD_KEYS: frozenset[str] = frozenset(
    item.field_key for item in MEASUREMENT_ATTRIBUTES)
#: Field keys produced only by a nested label, which has no label of its own.
NESTED_FIELD_KEYS: frozenset[str] = frozenset(
    key for item in SPEC_ATTRIBUTES for _label, key in item.nested)
SPEC_UNITS: dict[str, str] = {
    **{item.field_key: item.unit for item in SPEC_ATTRIBUTES if item.unit},
    **{key: item.unit for item in SPEC_ATTRIBUTES if item.unit
       for _label, key in item.nested},
}


def fields_for_spec_question(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(fields, topics) this wording asks for, from the shared vocabulary.

    The question side of the registry. ``supply_only`` attributes are skipped:
    they exist because the spec states them, not because wording selects them.
    """

    lowered = " ".join(str(text or "").lower().split())
    if not lowered:
        return (), ()
    fields: list[str] = []
    topics: list[str] = []
    for item in SPEC_ATTRIBUTES:
        if item.supply_only or not item.question_terms:
            continue
        if not any(term in lowered for term in item.question_terms):
            continue
        if any(term in lowered for term in item.not_when):
            continue
        fields.append(item.field_key)
        fields.extend(key for _label, key in item.nested)
        fields.extend(item.also_fields)
        topics.append(_TOPIC_OF.get(item.field_key, item.field_key))
    return tuple(dict.fromkeys(fields)), tuple(dict.fromkeys(topics))


# ---------------------------------------------------------------------------
# Spec text parsing
# ---------------------------------------------------------------------------
_SEGMENT = re.compile(r"[,，]\s*")
# A thousands separator is not a segment boundary. "명암비: 1,000:1" was being
# cut into "명암비: 1" and "000:1", which both truncated the value and invented
# a label. Protected before splitting and restored afterwards, so the rule
# stays about the one character it is about.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")
_PROTECTED_COMMA = "\x00"
_LABELLED = re.compile(r"^([^:]{1,40}?)\s*:\s*(.+)$", re.S)
_NUMERIC_LABEL = re.compile(r"^[\d.,\s/x×()\-]+$")
_NESTED_SPLIT = re.compile(r"\s*/\s*")
_MEASURE = re.compile(
    r"^\s*(?P<number>-?\d+(?:\.\d+)?(?:\s*[xX×]\s*-?\d+(?:\.\d+)?)*)"
    r"\s*(?P<unit>mm|cm|m|kg|g|w|hz|개|%|도|°|인치)?\s*$",
    re.IGNORECASE,
)


def labelled_segments(spec: object) -> list[tuple[str, str]]:
    """Every ``label: value`` the spec states, in order.

    A colon inside a value -- "1,000:1", "16:9" -- is not a boundary, because
    a label is never purely numeric. Segments that do not parse are left for
    :func:`unmapped_labels` rather than guessed at.

    A value the comma cut in two is put back together, and only for an
    attribute that says its value is written that way. Measured over the
    catalogue, the split was not merely losing the rest of such a value, it
    was reporting a wrong one: ``swivel_range_spec`` on 10 models read
    "스위블(좌우): 좌,우: 45.0°" as the single word "좌", and
    ``accessibility_visual`` on 11 models read one of seven features. An
    attribute that does not declare it is untouched, because a continuation
    rule applied to all of them would have swallowed real assertions --
    "픽셀피치: 0.24795mm, HDMI 케이블" is a pixel pitch followed by a cable,
    not a pixel pitch of two parts.
    """

    text = _THOUSANDS.sub(_PROTECTED_COMMA, str(spec or ""))
    pairs: list[tuple[str, str]] = []
    #: The attribute whose value may still continue, and nothing once a
    #: segment that is not its continuation arrives.
    open_item: SpecAttribute | None = None
    for part in _SEGMENT.split(text):
        part = part.strip().strip("/ ").strip()
        if not part:
            continue
        restored = part.replace(_PROTECTED_COMMA, ",")
        found = _LABELLED.match(part)
        label = (found.group(1).strip().replace(_PROTECTED_COMMA, ",")
                 if found else "")
        value = (found.group(2).strip().replace(_PROTECTED_COMMA, ",")
                 if found else "")
        readable = bool(
            found and label and value and not _NUMERIC_LABEL.match(label))
        attribute = _attribute_for_label(label) if readable else None
        if attribute is not None:
            pairs.append((label, value))
            open_item = (attribute if attribute.list_value
                         or attribute.continues else None)
            continue
        if open_item is not None and pairs:
            # Its own sub-label -- "뒤: 25.0°" after "틸트(앞뒤): 앞: 3.0°" --
            # or a bare item of a list. Anything else closes the value,
            # including a token another reader recognises by its own shape.
            continuing = (
                (readable and label in open_item.continues)
                or (not readable and open_item.list_value
                    and not _names_a_shape(restored))
            )
            if continuing:
                head_label, head_value = pairs[-1]
                pairs[-1] = (head_label, f"{head_value}, {restored}")
                continue
        if readable:
            pairs.append((label, value))
        open_item = None
    return pairs


def _names_a_shape(token: str) -> bool:
    """Whether another reader claims this token by its own shape.

    A continuation is text that only the label before it explains. A token
    that explains itself is a separate assertion, so it ends the value rather
    than extending it.
    """

    stripped = token.strip()
    if any(pattern.match(stripped) for _f, pattern, _u in _POSITIONAL_PATTERNS):
        return True
    compact = stripped.upper().replace(" ", "")
    return any(
        compact in {str(name).upper().replace(" ", "") for name in names}
        for names in PRESENCE_TOKENS.values()
    )


def _attribute_for_label(label: str) -> SpecAttribute | None:
    """The attribute this spec label names, preferring the longest match."""

    lowered = label.strip().lower()
    best: SpecAttribute | None = None
    best_length = 0
    for item in SPEC_ATTRIBUTES:
        if item.kind != MEASUREMENT:
            continue
        for candidate in item.spec_labels:
            name = candidate.strip().lower()
            if lowered == name and len(name) > best_length:
                best, best_length = item, len(name)
    return best


def _normalise_measure(value: str, attribute: SpecAttribute) -> str | None:
    """The value, kept whole, once it is readable as this attribute's value.

    Returns ``None`` when the text is not a value for this attribute -- which
    is the whole point of the measurement/presence split. The unit stays *in*
    the returned string: the prompt renders ``value`` and not ``unit``, so a
    bare "674.9" would reach the model with nothing to say whether it is
    millimetres or centimetres.
    """

    text = " ".join(str(value or "").split())
    if not text:
        return None
    # A run-on segment -- a label whose value swallowed what came after it --
    # is not a value. The catalog has several shapes of this, and the guard
    # has to catch all of them rather than the first one found:
    #
    #   "색상: 블랙 / LG용 등급 : 보급형출시년도 : 2023년형종류 : ..."
    #   "스탠드 형태: 플랫형 / 밝기 300 / 패널:VA패널 / 소비전력 225 / 214.7 cm"
    #
    # The second was promoted as ``stand_type_spec`` by an earlier version of
    # this guard -- one colon, 41 characters -- and was caught by auditing the
    # 12 models that assert the label against what the service returns. So a
    # slash-joined value is refused once it carries another label, and refused
    # outright beyond two parts. Two clean parts stay: "eARC/ARC" and the
    # usage hours "8/7" are single values written with a slash.
    if text.count(":") >= 2 and not attribute.continues:
        return None
    if len(text) > 80 and not (attribute.continues or attribute.list_value):
        return None
    if len(text) > 200:
        return None
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if len(parts) >= 3 and not (attribute.continues or attribute.list_value):
        return None
    if (len(parts) == 2 and any(":" in part for part in parts)
            and not attribute.continues):
        return None
    return text


def facts_from_spec(spec: object) -> dict[str, tuple[str, str | None]]:
    """``{field_key: (value, unit)}`` for every attribute this spec states.

    Measurement attributes only. A label whose value cannot be read is absent
    from the result; nothing here ever returns the spec blob.
    """

    out: dict[str, tuple[str, str | None]] = {}
    for label, value in labelled_segments(spec):
        attribute = _attribute_for_label(label)
        if attribute is None:
            continue
        if attribute.nested:
            for part in _NESTED_SPLIT.split(value):
                inner = _LABELLED.match(part.strip())
                if not inner:
                    continue
                name = inner.group(1).strip()
                for nested_label, field_key in attribute.nested:
                    if name == nested_label:
                        settled = _normalise_measure(
                            inner.group(2), attribute)
                        if settled and field_key not in out:
                            out[field_key] = (settled, attribute.unit)
            continue
        settled = _normalise_measure(value, attribute)
        if settled is None:
            continue
        # First statement wins: the spec lists a label once, and a second
        # occurrence with a different value is a conflict this module does not
        # resolve.
        if attribute.field_key in out and out[attribute.field_key][0] != settled:
            continue
        out.setdefault(attribute.field_key, (settled, attribute.unit))
    return out


def unmapped_labels(spec: object) -> tuple[str, ...]:
    """Labels the spec states that no attribute claims, for diagnostics.

    This is the extension point: a label appearing here is information the
    catalog holds and the registry does not model yet. It is reported rather
    than promoted, because a label nobody has read is not a verified fact.
    """

    return tuple(dict.fromkeys(
        label for label, _value in labelled_segments(spec)
        if _attribute_for_label(label) is None
    ))


def units_for(field_keys: Iterable[str]) -> dict[str, str]:
    return {key: SPEC_UNITS[key] for key in field_keys if key in SPEC_UNITS}


# ---------------------------------------------------------------------------
# Specification words that are shaped like model codes
# ---------------------------------------------------------------------------
# "DCI-P3" and "HDR10" are letters and digits with a hyphen, which is also what
# a model code looks like. The catalogue's model-token scan therefore reads
# them as a named model, finds no such model, and the inquiry fails closed --
# measured: "dci-p3 색영역" and "hdr10" returned no product facts at all, while
# "dci p3" and "hdr 10" returned them, because only the hyphenated spellings
# satisfy the shape.
#
# This is not a stopword list. It is the registry read from the other
# direction: a word is a specification word because some attribute here is
# named by it. Adding a term for the question side therefore protects it on
# the identity side too, with no second list to keep in step.
_SHAPE = re.compile(r"^(?=.*[A-Z])(?=.*\d)[A-Z\d+]+$")


def _identity_shape(text: object) -> str:
    """The comparison form: upper case, no separators."""

    return re.sub(r"[^0-9A-Za-z+]", "", str(text or "")).upper()


def _spec_vocabulary() -> frozenset[str]:
    words: set[str] = set()
    for item in SPEC_ATTRIBUTES:
        for term in (*item.question_terms, *item.spec_labels):
            shaped = _identity_shape(term)
            # Only the ones that could be mistaken for a model code. A purely
            # Korean or purely alphabetic term is never read as one, so adding
            # it here would widen this for no reason.
            if len(shaped) >= 3 and _SHAPE.match(shaped):
                words.add(shaped)
    return frozenset(words)


SPEC_VOCABULARY: frozenset[str] = _spec_vocabulary()


def names_a_specification(token: object) -> bool:
    """Whether this token is a specification word rather than a model code.

    Callers must ask the catalogue first: a token the catalogue resolves to a
    model *is* a model, whatever it also happens to describe. This answers only
    the question that is left -- "the catalogue does not know this code; is it
    a specification word?"
    """

    shaped = _identity_shape(token)
    return bool(shaped) and shaped in SPEC_VOCABULARY


# ---------------------------------------------------------------------------
# The positional dialect: slash-delimited, unlabelled
# ---------------------------------------------------------------------------
# "80cm(32인치) / 3840 x 2160(4K UHD) / 60Hz / VA / 와이드(16:9) / 평면 /
#  4ms(GTG) / 400cd / 3,000:1 / HDR10+ / 스피커 / 베사홀: 100 x 100mm"
#
# Most of that is an enumeration whose meaning comes from where it sits, and
# none of it is claimed here. What is claimed is the subset where the token's
# own unit or shape settles the attribute with nothing else to read: "60Hz" is
# a refresh rate wherever it appears, and "VA" is a panel type only because of
# the company it keeps.
#
# Every pattern is anchored end to end, so a bare number is never a value:
# "400" is not a brightness, "2023년형" is not a refresh rate, and
# "SU10349-23002" is not anything. The shapes deliberately left out -- panel
# type, surface, aspect ratio, sync technology, port names, and the bracketed
# "[색상영역] / [게임특화] / [단자정보]" sections -- are the next two steps.
_POSITIONAL_PATTERNS: tuple[tuple[str, "re.Pattern[str]", str | None], ...] = (
    # cd and cd/㎡ are the same measurement written two ways.
    # A thousands separator is part of the number. "1,000cd" was refused by
    # a digits-only pattern, and the whole-corpus audit found 27 such tokens
    # (1,000cd x21, 1,300cd x4, 1,400cd, 1,600cd) going nowhere.
    ("brightness_typical_cd_m2",
     re.compile(r"^(\d{1,3}(?:,\d{3})+|\d{2,4})\s*(?:cd|nit|니트)"
                r"(?:\s*/\s*(?:㎡|m2|m²))?$", re.I),
     "cd/m2"),
    # The mode in brackets ("GTG", "MPRT") is part of the measurement and is
    # kept: 1ms(MPRT) and 1ms(GTG) are not the same claim.
    ("response_time_ms",
     re.compile(r"^(\d+(?:\.\d+)?)\s*ms(?:\s*\([^)]{1,12}\))?$", re.I), "ms"),
    # A contrast ratio's second term is 1. "16:9" is an aspect ratio, which
    # this refuses, and which stays unmapped until 2-c.
    ("contrast_ratio",
     re.compile(r"^(?:\d{1,3}(?:,\d{3})*|\d+)\s*:\s*1$"), None),
    # Labelled even inside the positional dialect, so the label decides.
    ("vesa_mm",
     re.compile(r"^베사홀\s*:\s*\d{2,4}\s*[xX×]\s*\d{2,4}\s*mm$"), "mm"),
    ("refresh_rate",
     re.compile(r"^(\d{1,3}(?:,\d{3})+|\d{2,4})\s*Hz$", re.I), "Hz"),
    ("weight_catalog", re.compile(r"^(\d+(?:\.\d+)?)\s*kg$", re.I), "kg"),
    # One token stating one measurement twice. Kept whole rather than split
    # into a cm fact and an inch fact, which would be two facts for one thing.
    ("screen_size",
     re.compile(r"^\d+(?:\.\d+)?\s*cm\s*\(\s*\d+(?:\.\d+)?\s*인치\s*\)$"), None),
    # Both terms are pixel counts: no unit, and three digits or more. The unit
    # is what separates this from "베사홀: 100 x 100mm", and the separator is
    # what separates it from "16:9".
    ("resolution",
     re.compile(r"^\d{3,5}\s*[xX×]\s*\d{3,5}(?:\s*\([^)]{1,14}\))?$"), None),
)

#: Fields the positional reader can supply. Every one already exists: the
#: question router and the stored Product Knowledge name these attributes
#: already, so nothing new is introduced for 2-a.
POSITIONAL_FIELD_KEYS: frozenset[str] = frozenset(
    field for field, _pattern, _unit in _POSITIONAL_PATTERNS)
_POSITIONAL_UNITS: dict[str, str] = {
    field: unit for field, _pattern, unit in _POSITIONAL_PATTERNS if unit
}


# Both delimiters, because the catalogue uses both for the same kind of
# enumeration. Measured on the catalog of 2026-09-27: 47 models write their
# positional spec with commas instead of slashes -- "블랙, 2023년 11월,
# 123.8cm(49인치), 5120x1440(DQHD), 240Hz, VA, 화면비율: 32:9, 1ms(GTG),
# 450cd, 2,500:1" -- and their 272 self-describing tokens were read by nothing,
# because the slash split produced one segment and the labelled parser only
# reads the segments that carry a label.
#
# Splitting on both is safe precisely because every pattern is anchored end to
# end: a segment that carries a label ("제품크기(가로x높이x깊이): 967.5 x 561.4
# x 59.7 mm") cannot match any of them, so a labelled spec loses nothing by
# being tokenised this way as well.
_TOKEN_SPLIT = re.compile(r"\s*[/,，]\s*")


def positional_tokens(spec: object) -> list[str]:
    """Each slash- or comma-delimited assertion, in order.

    A thousands separator is protected first: "2,500:1" is one contrast ratio,
    not a "2" followed by a "500:1".
    """

    text = _THOUSANDS.sub(_PROTECTED_COMMA, str(spec or ""))
    out: list[str] = []
    for part in _TOKEN_SPLIT.split(text):
        part = part.strip().strip(",").strip().replace(_PROTECTED_COMMA, ",")
        if part:
            out.append(part)
    return out


def classify_positional(
    spec: object,
) -> tuple[dict[str, tuple[str, str | None, str]], tuple[str, ...],
           tuple[str, ...]]:
    """``(safe, ambiguous, unmapped)`` for one positional spec.

    ``safe`` maps a field to ``(value, unit, source_token)``; the token is
    carried so provenance survives into the fact. A token that more than one
    pattern claims is ambiguous and is promoted by neither -- the shape has to
    settle the attribute by itself or it is not 2-a. A field asserted twice
    with different values is dropped from ``safe`` for the same reason.
    """

    safe: dict[str, tuple[str, str | None, str]] = {}
    conflicting: set[str] = set()
    ambiguous: list[str] = []
    unmapped: list[str] = []
    for token in positional_tokens(spec):
        matched = [field for field, pattern, _unit in _POSITIONAL_PATTERNS
                   if pattern.match(token)]
        if len(matched) > 1:
            ambiguous.append(token)
            continue
        if not matched:
            unmapped.append(token)
            continue
        field = matched[0]
        if field in safe and safe[field][0] != token:
            # Two different values for one attribute. Choosing between them is
            # a review decision, so neither is offered.
            conflicting.add(field)
            continue
        safe.setdefault(field, (token, _POSITIONAL_UNITS.get(field), token))
    for field in conflicting:
        safe.pop(field, None)
    return safe, tuple(ambiguous), tuple(unmapped)


def facts_from_positional_spec(
    spec: object, *, already_supplied: Iterable[str] = (),
) -> dict[str, tuple[str, str | None, str]]:
    """Positional facts for the fields nothing better has supplied.

    ``already_supplied`` is how precedence is kept: a dedicated catalogue
    column, or an explicit ``label: value`` in the spec, is better evidence
    than a token recognised by its shape, so this never overwrites one.
    Measured on the catalog of 2026-09-27, the columns already populate
    ``screen_size`` for 1,484 of 1,487 positional models and ``resolution``
    for 1,486 -- reading those from the spec as well would have produced a
    second fact for the same thing on almost every model.
    """

    supplied = {str(name) for name in already_supplied}
    safe, _ambiguous, _unmapped = classify_positional(spec)
    return {field: value for field, value in safe.items()
            if field not in supplied}


# ---------------------------------------------------------------------------
# Section context (2-b)
# ---------------------------------------------------------------------------
# The catalogue writes part of each positional spec under a bracketed heading:
# "[색상영역] DCI-P3: 92% / sRGB: 99% / [게임특화] FreeSync / 조준선 표시 /
# [단자정보] HDMI 2.1 / DP 1.4 / PD충전: 최대65W". The heading is what makes
# the token readable -- a bare "92%" names nothing, and the same percentage
# under [색상영역] with the label DCI-P3 names one gamut and not another -- so
# a rule here may fire only inside the sections it is listed under.
#
# Measured over the whole catalogue (1,575 models production resolves): 10,362
# tokens sit under a heading, in 7 sections. Nothing recognised the heading
# before this, so the labelled parser (no top-level "label: value") and the
# positional one (no self-describing shape) both passed over them.
_SECTION_HEADING = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$")

#: One value, and a second different value means neither is offered.
SINGLE = "SINGLE"
#: A tier ladder: the most specific tier stated is the product's tier.
LADDER = "LADDER"
#: Several values that are all true at once -- apps, features, port revisions.
LIST = "LIST"
#: A yes or a no. The token's whole content is that the product has the
#: thing, so the value reported is "지원" or "미지원" rather than the word
#: the spec happened to use for it.
POLARITY = "POLARITY"

#: Tier ladders, most general first. A set of values inside one ladder is not
#: a conflict: "FreeSync / FreeSync Premium / FreeSync Premium Pro" is one
#: certification stated with its predecessors, and 211 models write it that
#: way. A value outside the ladder (G-Sync compatible next to G-Sync
#: Ultimate -- 1 model) is left unresolved rather than ranked.
_LADDERS: dict[str, tuple[str, ...]] = {
    "freesync_spec": ("FreeSync", "FreeSync Premium", "FreeSync Premium Pro"),
    "gsync_spec": ("G-Sync", "G-Sync Ultimate"),
}


@dataclass(frozen=True)
class SectionRule:
    """How one token is read, inside the sections that allow it."""

    name: str
    pattern: "re.Pattern[str]"
    #: The canonical field, or None when the token is deliberately withheld.
    field: str | None
    shape: str = SINGLE
    #: Why a withheld token is withheld. Reported, never guessed around.
    reason: str = ""


def _s(name, regex, field, shape=SINGLE, reason="") -> SectionRule:
    return SectionRule(name, re.compile(regex, re.I), field, shape, reason)


_SECTION_GAMUT = (
    _s("srgb", r"^sRGB\s*[:：]?\s*(\d+(?:\.\d+)?%?)~?$",
       "color_gamut_srgb"),
    _s("dci_p3", r"^DCI[\s-]?P3\s*[:：]?\s*(\d+(?:\.\d+)?%?)~?$",
       "color_gamut_dci_p3"),
    _s("ntsc", r"^NTSC\s*[:：]?\s*(\d+(?:\.\d+)?%?)~?$",
       "color_gamut_ntsc"),
    _s("adobe_rgb", r"^Adobe\s*RGB\s*[:：]?\s*(\d+(?:\.\d+)?%?)~?$",
       "color_gamut_adobe_rgb"),
    _s("bit_depth", r"^(\d+\s?bit(?:\s*\([^)]*\)|\s+True)?)$",
       "color_bit_depth"),
)

_SECTION_GAMING = (
    _s("freesync", r"^(FreeSync(?:\s+Premium(?:\s+Pro)?)?)$", "freesync_spec",
       LADDER),
    _s("gsync", r"^(G[\s-]?Sync(?:\s+compatible|\s+Ultimate)?)$",
       "gsync_spec", LADDER),
    _s("adaptive_sync", r"^(Adaptive\s*Sync)$", "adaptive_sync_spec"),
    _s("crosshair", r"^(조준선\s*표시)$", "crosshair_spec"),
    _s("black_equalizer", r"^(블랙\s*이퀄라이저)$", "black_equalizer_spec"),
    _s("input_lag", r"^(인풋랙\s*제어)$", "input_lag_control_spec"),
)

_SECTION_PORTS = (
    # Count before version before presence: the most committed reading first.
    _s("hdmi_count",
       r"^HDMI(?:\s*\d\.\d)?(?:\(전체\))?\s*[:：]\s*(\d+)\s*개?$",
       "hdmi_port_count_spec"),
    _s("hdmi_count_trailing", r"^HDMI\s*\d\.\d\s+(\d+)\s*개$",
       "hdmi_port_count_spec"),
    _s("hdmi_version", r"^(HDMI\s*\d\.\d)$", "hdmi_version_spec", LIST),
    _s("hdmi_variant", r"^((?:mini|micro)\s*HDMI)$", "hdmi_variant_spec",
       LIST),
    # Plain "HDMI" adds nothing: ``hdmi_present`` already fires on the spec
    # mentioning it, and 645 such tokens would otherwise have become a second
    # fact for a statement already made.
    _s("hdmi_plain", r"^(HDMI)$", "hdmi_present"),
    _s("dp_count", r"^(?:DP|Display\s*Port)\s*[:：]\s*(\d+)\s*개?$",
       "display_port_count"),
    _s("dp_version", r"^(DP\s*\d\.\d)$", "displayport_version_spec", LIST),
    _s("dp_variant", r"^(mini\s*DP)$", "displayport_variant_spec", LIST),
    _s("dp_plain", r"^(DP|Display\s*Port)$", "displayport_present"),
    _s("dsub", r"^(D-?SUB)$", "dsub_present"),
    _s("dvi", r"^(DVI)$", "dvi_present"),
    _s("usb_c_count", r"^USB\s*C타입\s+(\d+)\s*개$", "usb_port_count_spec"),
    _s("usb_count", r"^USB(?:\s*포트)?\s*[:：]\s*(\d+)\s*개?$",
       "usb_port_count_spec"),
    _s("usb_c", r"^(USB[\s-]?C\s*타입|USB-?C)$", "usb_c_present"),
    _s("usb_in", r"^(USB입력)$", "usb_present"),
    _s("thunderbolt", r"^(썬더볼트\s*[34])$", "thunderbolt_spec", LIST),
    _s("mhl", r"^(MHL)$", "mhl_present"),
    _s("component", r"^(컴포넌트)$", "component_present"),
    _s("composite", r"^(컴포지트)$", "composite_present"),
    _s("rf_count", r"^RF단자\s*[:：]\s*(\d+)\s*개?$", "rf_port_count"),
    _s("rf", r"^(RF안테나|RF단자)$", "rf_terminal"),
    # A truncated negation glued to the value, so neither reading is safe.
    _s("pd_corrupt", r"^PD충전.*(?:없음|음)$", None, SINGLE,
       "the wattage is followed by a truncated 없음, so the value cannot be "
       "trusted either way"),
    # The charging condition stays inside the value: "최대65W" is a ceiling,
    # not a continuous rating.
    _s("pd", r"^PD충전\s*[:：]?\s*(최대\s*\d+(?:\.\d+)?W|\d+(?:\.\d+)?W)$",
       "pd_charging_spec"),
    _s("pd_trailing", r"^(최대\s*\d+W)충전$", "pd_charging_spec"),
    _s("port_corrupt", r"^(?:DP|HDMI)[^:：]*(?:없음|음)$", None, SINGLE,
       "a truncated negation glued to the port name"),
    _s("cable",
       r"^(?:전원\s*연결선\(전원\s*케이블\)|전원케이블길기)\s*[:：]\s*"
       r"([\d.]+\s*m)$", "power_cable_length"),
    _s("remote", r"^리모컨\s*(있음|없음)$", "remote_present"),
    _s("wireless_display", r"^무선\s*Display\s*연결\s*(있음|없음)$",
       "wireless_display_spec"),
    _s("price", r"^출시가\s*[:：]\s*([\d,]+원)$", None, SINGLE,
       "a launch price is listing/promotion data: it changes without the "
       "product changing, which is the hazard DYNAMIC_LISTING_FACT exists "
       "for"),
    _s("tilt_range", r"^틸트\s*[:：]\s*(.+)$", "tilt_range_spec"),
    _s("pivot_range", r"^피봇\s*[:：]\s*(.+)$", "pivot_spec"),
    _s("motion_negated", r"^(?:틸트|피봇)\s*x$", None, SINGLE,
       "a trailing x is an undocumented negation; reading it as support "
       "would invert the statement"),
    _s("motion_bare", r"^(틸트|피봇)$", None, SINGLE,
       "the token names the movement without a range, and the field holds a "
       "range"),
    _s("smart_tv", r"^(스마트TV)$", "smart_tv_spec"),
    _s("airplay", r"^(에어플레이(?:\([^)]*\))?)$", "airplay_spec"),
    _s("ott_list", r"^OTT\s*\((.+)\)$", "ott_apps_spec", LIST),
    _s("ott_support", r"^(OTT지원)$", "ott_support_spec", LIST),
    _s("dual_mode", r"^(듀얼\s*해상도\s*.+)$", "dual_mode_spec"),
)

_SECTION_STANDARD = (
    _s("vesa_hole", r"^베사홀\s*[:：]?\s*(\d+\s*x\s*\d+\s*mm)$",
       "vesa_hole_spec"),
    _s("dimensions", r"^크기\(가로x세로x깊이\)\s*[:：]?\s*(.+)$",
       "dimensions_product_spec"),
    _s("weight_two", r"^무게\s*[:：]?\s*[\d.]+\([\d.]+\)\s*kg$", None,
       SINGLE,
       "two weights in one token with nothing saying which is with the "
       "stand; with/without stand stay separate fields"),
    _s("weight_plain", r"^무게\s*[:：]?\s*([\d.]+\s*kg)$",
       "weight_product_spec"),
    # KQ42SF90AEXKR states its weight under [규격] with no label at all. The
    # positional reader already claims that token as ``weight_catalog``, so
    # naming it here maps it to the same field, where precedence drops it as
    # a duplicate instead of letting it fall through unread.
    _s("weight_bare", r"^([\d.]+\s*kg)$", "weight_catalog"),
    _s("stand_width", r"^스탠드너비\s*[:：]\s*(\d+\s*mm)$",
       "stand_width_spec"),
    _s("form_factor", r"^(스탠드형|포터블형|이동형|벽걸이형)$",
       "form_factor_spec"),
    _s("samsung_tv_plus", r"^(삼성TV\+?\s*사용\s*불가)$",
       "samsung_tv_plus_spec", LIST),
    _s("ott_blocked", r"^(자체\s*OTT\s*지원\s*불가[^/]*|OTT\s*사용\s*불가)$",
       "ott_support_spec", LIST),
    _s("business_notice", r"^※.*사업자전용.*$", None, SINGLE,
       "a listing notice about proof of business registration, not a fact "
       "about the product"),
    _s("speaker", r"^(스피커)$", "speaker_present"),
    _s("mirroring", r"^(미러링)$", "mobile_connect_spec"),
)

_SECTION_PICTURE = (
    _s("hdr", r"^(HDR10\+?|HDR\s*10\+?)$", "hdr_spec", LIST),
    _s("hdr_tuning", r"^(HDR자동조절|톤매핑|HGIG)$", "hdr_tuning_spec", LIST),
    _s("flicker_free", r"^(플리커프리)$", "flicker_free_spec"),
    _s("vrr", r"^(VRR\(?\d*Hz?\)?|ALLM)$", "vrr_spec", LIST),
    _s("game_mode", r"^(게임모드|AI오토게임모드|사커모드|휴싱크|OTT)$", None,
       SINGLE, "a picture preset name with no measurable value"),
)

_SECTION_SMART = (
    _s("apps",
       r"^(넷플릭스|유튜브|디즈니\+|웨이브|티빙|쿠팡플레이|아마존|"
       r"왓챠(?:\s*등등\)?)?)$", "ott_apps_spec", LIST),
    _s("channels", r"^(LG채널|삼성TV플러스)$", "samsung_tv_plus_spec", LIST),
    _s("smart_feature", r"^(미러링|실시간번역|음성인식)$",
       "smart_feature_spec", LIST),
    _s("smart_home", r"^(AI스마트홈|지그비|매터|룸투룸셰어\([^)]*\))$",
       "smart_home_spec", LIST),
)

_SECTION_MISC = (
    _s("dual_mode", r"^(듀얼\s*해상도\s*.+)$", "dual_mode_spec"),
    _s("battery", r"^배터리\s*용량\s*[:：]\s*([\d,]+\s*mAh)$",
       "battery_capacity_spec"),
    _s("touch", r"^(터치\s*미지원|터치기능\s*USB\s*연결\s*필요)$",
       "touch_support_spec", LIST),
    _s("bundle",
       r"^(.*포함|.*별도\s*구매|.*캘리브레이터.*|스탠드\s*커버|"
       r"알루미늄\s*스탠드.*|플렉시블\s*거치대.*)$", None, SINGLE,
       "a bundled or separately sold accessory: listing/bundle content, "
       "which never becomes one of the model's own facts"),
    _s("colour_word",
       r"^(?:색상\s*[:：]\s*)?(블랙|화이트|그레이|오렌지)$", None, SINGLE,
       "the section does not say whose colour this is -- body, stand or "
       "accessory -- and those stay separate subjects"),
    _s("no_field",
       r"^(PVC\s*프리|절전기능\([^)]*\)|Magic Upscale 지원|무선충전|"
       r"모바일 무선 충전|곡률 조절 가능|Dolby|DTS 지원\([^)]*\)|"
       r"그램 \+view 2세대|1\.07B 컬러 지원|16\.7M 컬러지원|섀도우부스트|"
       r"Light FX|Quick FIT|Vivid Pixel)$", None, SINGLE,
       "no canonical field expresses it, and inventing one for a handful of "
       "tokens would add wording nothing asks for"),
)

#: Which rules a section allows. A heading not listed here supplies nothing,
#: so it appears in :func:`unmapped_section_tokens` instead of being guessed
#: at.
SECTION_RULES: dict[str, tuple[SectionRule, ...]] = {
    "색상영역": _SECTION_GAMUT + _SECTION_GAMING + _SECTION_PORTS,
    "게임특화": _SECTION_GAMING + _SECTION_PORTS + _SECTION_GAMUT
             + _SECTION_MISC,
    "단자정보": _SECTION_PORTS + _SECTION_GAMING + _SECTION_SMART
             + _SECTION_PICTURE + _SECTION_MISC,
    "규격": _SECTION_STANDARD + _SECTION_PORTS + _SECTION_PICTURE
          + _SECTION_MISC + _SECTION_SMART,
    "화질": _SECTION_PICTURE + _SECTION_PORTS + _SECTION_GAMING
          + _SECTION_STANDARD + _SECTION_MISC,
    "스마트": _SECTION_SMART + _SECTION_PICTURE + _SECTION_MISC,
    "기타": _SECTION_MISC + _SECTION_GAMING + _SECTION_PICTURE
          + _SECTION_STANDARD + _SECTION_PORTS,
}

#: Every field a section rule can supply.
SECTION_FIELD_KEYS: frozenset[str] = frozenset(
    rule.field for rules in SECTION_RULES.values() for rule in rules
    if rule.field
)


def sectioned_tokens(spec: object) -> list[tuple[str, str]]:
    """``(section, token)`` for a spec; section "" before the first heading.

    Two things the plain token split gets wrong are repaired here, both
    measured on the catalogue:

    * A token can carry more than one heading. C22F392 writes
      "[게임특화] [단자정보] HDMI": an empty 게임특화 section, then 단자정보
      whose first value is HDMI. Stripping one heading filed HDMI -- and
      every token after it -- under the wrong section.
    * The delimiter cuts inside parentheses, so
      "OTT(넷플릭스, 디즈니+, ... 왓챠 등등)" arrives as eight fragments. A
      fragment such as "웨이브" would assert on its own an app list the spec
      never made, so an unbalanced "(" absorbs what follows until it closes.
    """

    joined: list[str] = []
    pending: str | None = None
    for token in positional_tokens(spec):
        if pending is not None:
            pending = f"{pending}, {token}"
            if pending.count("(") <= pending.count(")"):
                joined.append(pending)
                pending = None
            continue
        if token.count("(") > token.count(")"):
            pending = token
            continue
        joined.append(token)
    if pending is not None:
        joined.append(pending)

    out: list[tuple[str, str]] = []
    section = ""
    for token in joined:
        rest = token
        while (found := _SECTION_HEADING.match(rest)):
            section = found.group(1).strip()
            rest = found.group(2).strip()
        if rest:
            out.append((section, rest))
    return out


def _section_rule(section: str, token: str) -> SectionRule | None:
    for rule in SECTION_RULES.get(section, ()):  # listed order is precedence
        if rule.pattern.match(token):
            return rule
    return None


def _ladder_value(field: str, values: list[str]) -> str | None:
    """The tier to report, or None when the stated tiers cannot be ranked.

    One value needs no ranking and is reported as the spec states it: 524
    models say only "G-Sync compatible", which is not a step on the G-Sync
    ladder and is still exactly what they support. Ranking is needed only
    when several tiers are stated at once, and then every one of them has to
    sit on the same ladder -- "G-Sync Ultimate / G-Sync compatible" (1 model)
    does not, so neither is chosen.
    """

    if len(values) == 1:
        return values[0]
    ladder = [tier.lower() for tier in _LADDERS.get(field, ())]
    ranks = [ladder.index(value.lower()) for value in values
             if value.lower() in ladder]
    if len(ranks) != len(values) or not ranks:
        return None
    return _LADDERS[field][max(ranks)]


def classify_section_spec(
    spec: object,
) -> tuple[dict[str, tuple[str, str | None, str]],
           tuple[tuple[str, str, str], ...], tuple[tuple[str, str], ...]]:
    """``(safe, withheld, unmapped)`` for one spec's section tokens.

    ``safe`` maps a field to ``(value, unit, source)``, where the source keeps
    the heading -- "[색상영역] DCI-P3: 92%" -- so provenance survives into the
    fact. ``withheld`` carries ``(source, field_or_empty, reason)`` and
    ``unmapped`` carries ``(section, token)``, which is how the next rule gets
    written rather than guessed.
    """

    gathered: dict[str, list[tuple[str, str]]] = {}
    shapes: dict[str, str] = {}
    withheld: list[tuple[str, str, str]] = []
    unmapped: list[tuple[str, str]] = []

    for section, token in sectioned_tokens(spec):
        if not section:
            continue
        rule = _section_rule(section, token)
        if rule is None:
            unmapped.append((section, token))
            continue
        if rule.field is None:
            withheld.append((f"[{section}] {token}", "", rule.reason))
            continue
        found = rule.pattern.match(token)
        value = (found.group(1) if found and found.lastindex
                 else token).strip()
        if not value:
            continue
        gathered.setdefault(rule.field, []).append(
            (value, f"[{section}] {token}"))
        shapes[rule.field] = rule.shape

    safe: dict[str, tuple[str, str | None, str]] = {}
    for field, pairs in gathered.items():
        values = list(dict.fromkeys(value for value, _source in pairs))
        sources = " / ".join(dict.fromkeys(source for _value, source in pairs))
        shape = shapes[field]
        if shape == LIST:
            settled: str | None = ", ".join(values)
        elif shape == LADDER:
            settled = _ladder_value(field, values)
            if settled is None:
                withheld.append((
                    sources, field,
                    "the tiers stated are not one ladder, so ranking them "
                    "would invent a certification the spec does not state"))
                continue
        elif len(values) == 1:
            settled = values[0]
        else:
            withheld.append((
                sources, field,
                "the section states two different values for one attribute; "
                "choosing between them is a review decision"))
            continue
        safe[field] = (settled, SPEC_UNITS.get(field), sources)
    return safe, tuple(withheld), tuple(unmapped)


def facts_from_section_spec(
    spec: object, *, already_supplied: Iterable[str] = (),
) -> dict[str, tuple[str, str | None, str]]:
    """Section-context facts for the fields nothing better has supplied.

    Precedence is unchanged: a dedicated catalogue column, an explicit
    top-level ``label: value``, and a self-describing positional token are all
    read first, and this never overwrites one. What it adds is the reading
    only the heading makes possible.
    """

    supplied = {str(name) for name in already_supplied}
    safe, _withheld, _unmapped = classify_section_spec(spec)
    return {field: value for field, value in safe.items()
            if field not in supplied}


def withheld_section_tokens(spec: object) -> tuple[tuple[str, str, str], ...]:
    """``(source, field, reason)`` for every section token not promoted."""

    _safe, withheld, _unmapped = classify_section_spec(spec)
    return withheld


def unmapped_section_tokens(spec: object) -> tuple[tuple[str, str], ...]:
    """``(section, token)`` for every section token no rule reads."""

    _safe, _withheld, unmapped = classify_section_spec(spec)
    return unmapped


# ---------------------------------------------------------------------------
# Unlabelled enumeration (2-c)
# ---------------------------------------------------------------------------
# What is left once the three readers above have had the spec. The catalogue
# writes a run of bare words with no label and no shape to anchor on:
# "블랙 / 평면 / 화면비율 16:9 / 패널 타입 IPS / 눈 보호 모드 / 플리커프리 /
# 스마트X / HDMI 2개 / 틸트 -2.0~+21.0 / 스위블x / 피봇x". Measured over the
# whole catalogue: 11,578 such tokens on 1,569 of the 1,575 models production
# resolves, which 2-a (no label, no self-describing shape) and 2-b (no
# heading) both passed over.
#
# A token here is read by what it *says*, which is why a rule may name more
# than one field: "와이드(16:9)" states a width class and an aspect ratio, and
# reading only one of them would drop what the spec wrote. And a rule may
# name no field at all, and then it carries the reason -- a bare "60g" does
# not say whether it weighs the product, the package or the stand, and
# guessing would be the one thing this round must not do.
@dataclass(frozen=True)
class EnumRule:
    """How one unlabelled token is read."""

    name: str
    pattern: "re.Pattern[str]"
    #: ``(field, capture group)``; group 0 is the whole token. Empty means
    #: the token is deliberately withheld.
    targets: tuple[tuple[str, int], ...] = ()
    shape: str = SINGLE
    #: Why a withheld token is withheld. Reported, never guessed around.
    reason: str = ""
    #: Fires only on the spec's first token, which is the one place the
    #: catalogue writes the series name -- and "M6" elsewhere is a screw.
    head_only: bool = False
    #: The value to report for a token whose whole content is "it has this".
    value: str = ""


def _e(name, regex, targets=(), shape=SINGLE, reason="", head_only=False,
       value="") -> EnumRule:
    return EnumRule(name, re.compile(regex, re.I), tuple(targets), shape,
                    reason, head_only, value)


_SUPPORTED = "지원"
_UNSUPPORTED = "미지원"
#: The same yes and no, in the notation the stored rows use for the fields
#: that already have one.
_PRESENT = "있음"
_ABSENT_VALUE = "없음"

ENUM_RULES: tuple[EnumRule, ...] = (
    # -- the source says it holds nothing, or says it unreadably ----------
    _e("no information", r"^정보\s*없음$", reason=
       "the catalogue states that it holds no specification for this model"),
    _e("size with a truncated 정보없음 glued to it",
       r"^정(?:보(?:없)?)?\d+(?:\.\d+)?\s*cm\s*\(\s*\d+(?:\.\d+)?\s*인치\s*\)$",
       reason="the source runs '정보없음' into the screen size, so neither "
              "the size nor the absence can be read as stated"),
    _e("several labels run together with no delimiter",
       r"^(?:LG용 등급|Optical에너지효율|품목)\s*[:：]?\s*.{40,}$", reason=
       "the source writes several labelled values with no delimiter between "
       "them, so no one value can be cut out of it"),
    _e("a service URL cut up by the delimiter",
       r"^(?:AS\s*[:：]\s*https?\s*[:：]?|[a-z0-9.\-]+\.(?:com|kr|net)"
       r"|as|kr 링크를 통해 접수 가능)$", reason=
       "a support URL the delimiter cut into fragments, not a product "
       "attribute"),
    _e("certification number", r"^R-R-[A-Z]{3}-[A-Z0-9\-]+$", reason=
       "the certificate's identifier rather than an attribute of the "
       "product, and shaped like a model code, so it is not offered as a "
       "fact"),

    # -- the stand's movement, stated as a capability ---------------------
    # 1,356 models say only that the stand tilts. That is a capability, and
    # reporting it as a tilt *angle* would invent a measurement, so the range
    # fields keep their own meaning and these are separate attributes.
    _e("tilt capability", r"^(?:스탠드\s*)?틸트(?:\(상하\)|\s*상하)?$",
       (("tilt", 0),), POLARITY, value=_SUPPORTED),
    _e("swivel capability", r"^스위블(?:\(좌우\))?$",
       (("swivel", 0),), POLARITY, value=_SUPPORTED),
    _e("swivel denied", r"^스위블\s*[xX]$",
       (("swivel", 0),), POLARITY, value=_UNSUPPORTED),
    _e("pivot capability", r"^피[벗봇](?:\(회전\))?$",
       (("pivot", 0),), POLARITY, value=_SUPPORTED),
    _e("pivot denied", r"^피[벗봇]\s*[xX]$",
       (("pivot", 0),), POLARITY, value=_UNSUPPORTED),
    _e("height adjustment", r"^엘리베이션(?:\(높낮이\))?$",
       (("height_adjust", 0),), POLARITY, value=_SUPPORTED),
    _e("auto pivot", r"^가로 및 세로 자동 전환\(오토피벗\)(?:\(자동 회전\))?$",
       (("auto_pivot", 0),), POLARITY, value=_SUPPORTED),
    _e("auto pivot denied", r"^오토피벗 지원\s*[xX]$",
       (("auto_pivot", 0),), POLARITY, value=_UNSUPPORTED),

    # -- the stand's movement, stated as a range --------------------------
    _e("tilt stated as a pair of directions",
       r"^틸트\(앞뒤\)\s*[:：]\s*((?:앞|뒤|위|아래)\s*[:：].+)$",
       (("tilt_range_spec", 1),)),
    _e("tilt range",
       r"^(?:스탠드\s*)?틸[트드]\s*(-?\d+(?:\.\d+)?\s*도?\s*~\s*"
       r"\+?\d+(?:\.\d+)?\s*도?)$", (("tilt_range_spec", 1),)),
    _e("swivel range",
       r"^스위블\s*(-?\d+(?:\.\d+)?\s*~\s*\+?\d+(?:\.\d+)?)$",
       (("swivel_range_spec", 1),)),
    _e("pivot range",
       r"^피[벗봇]\s*(-?\d+(?:\.\d+)?\s*~\s*\+?\d+(?:\.\d+)?)$",
       (("pivot_spec", 1),)),
    _e("stand hinge", r"^(듀얼힌지)$", (("hinge_type", 1),)),

    # -- the shape of the screen ------------------------------------------
    _e("flat or curved", r"^(?:화면\s*)?(평면|커브드|곡면|Flat)$",
       (("flat_curved", 1),)),
    _e("flat or curved, labelled", r"^곡면형\s*[:：]\s*(평면|커브드|곡면)$",
       (("flat_curved", 1),)),
    _e("curvature radius", r"^(\d{3,4}R)~?$", (("curvature", 1),)),
    _e("width class with its ratio",
       r"^(와이드|울트라와이드|일반)\((\d+:\d+)\)$",
       (("screen_width_class", 1), ("aspect_ratio", 2))),
    _e("aspect ratio, labelled", r"^화면비율\s+(\d+:\d+)$",
       (("aspect_ratio", 1),)),

    # -- the panel --------------------------------------------------------
    _e("panel, labelled", r"^(?:패널 타입|패널|페널)\s*[:：]?\s*"
                          r"([A-Za-z가-힣\-\+ ]{2,20}(?:패널)?)$",
       (("panel_type", 1),)),
    _e("panel technology",
       r"^((?:(?:QD|Nano|Fast|Rapid|SS|A|AH)[- ])*"
       r"(?:IPS|VA|TN|PLS|OLED|ADS|A?MVA|PVA|HVA|AHVA)\+?"
       r"(?: Black)?(?:패널)?(?: \(Neo Blade [^)]{1,20}\))?)$",
       (("panel_type", 1),)),
    _e("backlight", r"^(?:화면종류\s*[:：]\s*)?(LED)$",
       (("backlight_type", 1),)),
    _e("panel surface", r"^(강화유리)$", (("panel_surface", 1),)),

    # -- the picture ------------------------------------------------------
    _e("HDR format", r"^(HDR\s?\d+\+?)$", (("hdr_spec", 1),)),
    _e("HDR certification",
       r"^((?:VESA\s+)?Display\s?HDR \d+|VESA HDR \d+"
       r"|VESA TRUE BLACK \d+)$",
       (("hdr_certification", 1),)),
    _e("HDR tone mapping", r"^(HGIG)$", (("hdr_tuning_spec", 1),)),
    _e("contrast, approximate", r"^~([\d,.]+\s*:\s*1)$",
       (("contrast_spec", 1),)),
    _e("contrast ratio", r"^([\d,.]+\s*:\s*1)$", (("contrast_spec", 1),)),
    _e("contrast, labelled", r"^명암비\s+([\d,]+\s*:\s*1)$",
       (("contrast_spec", 1),)),
    _e("brightness, labelled", r"^밝기\s+(\d{2,4})$",
       (("brightness_typical_cd_m2", 1),)),
    _e("peak brightness, labelled", r"^최대밝기\s*[:：]\s*(\d{2,4})\s*nits?$",
       (("brightness_peak", 1),)),
    _e("response time, labelled",
       r"^응답속도\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:ms)?(?:\s*\(GTG\))?$",
       (("response_time_ms", 1),)),
    _e("response time basis", r"^응답속도기준\s*[:：]\s*(GTG|MPRT)$",
       (("response_time_basis", 1),)),
    _e("gamut NTSC", r"^(?:색표현\s*[:：]\s*)?NTSC\s*\((\d{1,3}%)\)$",
       (("color_gamut_ntsc", 1),)),
    _e("gamut sRGB", r"^sRGB\s*\((\d{1,3}%)\)$", (("color_gamut_srgb", 1),)),
    _e("gamut DCI-P3", r"^DCI[\s-]?P3[\s(]+(\d{1,3}%)\)?$",
       (("color_gamut_dci_p3", 1),)),
    _e("flicker free", r"^(?:시력보호\s*[:：]\s*)?(?:플리커\s?프리|폴리커프리)$",
       (("flicker_free_spec", 0),), POLARITY, value=_PRESENT),
    _e("low blue light",
       r"^(?:로우\s?블루라이트|블루라이트 차단|눈\s?보호\s?모드)$",
       (("eye_saver", 0),), POLARITY, value=_PRESENT),
    _e("ambient lighting", r"^(LED 조명|엣지 라이팅)$",
       (("core_lighting", 1),)),
    _e("picture by picture", r"^PBP$",
       (("pbp", 0),), POLARITY, value=_PRESENT),
    _e("picture in picture", r"^PIP$",
       (("pip", 0),), POLARITY, value=_PRESENT),

    # -- gaming -----------------------------------------------------------
    _e("FreeSync", r"^(FreeSync(?: Premium(?: Pro)?)?)(?: 있음)?$",
       (("freesync_spec", 1),), LADDER),
    _e("G-Sync", r"^(G-Sync)(?:\s*compatible|\(호환\))?$",
       (("gsync_spec", 0),), LADDER),
    _e("Adaptive Sync", r"^(Adaptive Sync)$", (("adaptive_sync_spec", 1),)),
    _e("crosshair", r"^조준선\s*(?:표시|모드)$",
       (("crosshair_spec", 0),), POLARITY, value=_SUPPORTED),
    _e("black equalizer", r"^블랙\s?이퀄라이[저져]$",
       (("black_equalizer_spec", 0),), POLARITY, value=_SUPPORTED),
    _e("input lag", r"^인풋렉감소$",
       (("input_lag_control_spec", 0),), POLARITY, value=_SUPPORTED),
    _e("game mode", r"^게임\s?모드$",
       (("game_mode", 0),), POLARITY, value=_PRESENT),

    # -- sound ------------------------------------------------------------
    _e("headphone out", r"^(?:헤드폰 아웃|헤드폰|이어폰 단자)$",
       (("headphone_out", 0),), POLARITY, value=_PRESENT),
    _e("speaker output", r"^스피커\s*[:：]\s*(\d+W\s*\+\s*\d+W)$",
       (("speaker_output_spec", 1),)),
    _e("speaker built in", r"^(?:스피커|부가기능\s*[:：]\s*스피커 내장)$",
       (("speaker_present", 0),), POLARITY, value=_SUPPORTED),
    _e("HDMI ARC", r"^(ARC|eARC)$", (("hdmi_arc_spec", 1),)),

    # -- ports and what comes in the box ----------------------------------
    _e("USB PD", r"^USB PD 지원$",
       (("usb_pd_support", 0),), POLARITY, value=_SUPPORTED),
    _e("HDCP", r"^(HDCP)$", (("hdcp", 0),), POLARITY, value=_SUPPORTED),
    _e("HDMI count", r"^HDMI\s*(?:포트|IN|OUT)?\s*[:：]?\s*(\d+)\s*개?$",
       (("hdmi_port_count_spec", 1),)),
    _e("DP count", r"^DP\s*(?:포트)?\s*[:：]?\s*(\d+)\s*개?$",
       (("display_port_count", 1),)),
    _e("USB count", r"^USB\s*포트\s*[:：]\s*(\d+)\s*개?$",
       (("usb_port_count_spec", 1),)),
    _e("USB hub", r"^USB 허브$",
       (("usb_hub", 0),), POLARITY, value=_PRESENT),
    _e("component", r"^컴포넌트$",
       (("component_present", 0),), POLARITY, value=_SUPPORTED),
    _e("composite", r"^컴포지트$",
       (("composite_present", 0),), POLARITY, value=_SUPPORTED),
    _e("already stated as a presence flag",
       r"^(?:블루투스\s*[\d.]*|Bluetooth|Wi-?Fi(?:\(무선\)|\s*[\d.]*)"
       r"|와이파이|LAN(?:\(유선\)| 포트)?|USB|HDMI|RF)$",
       reason="the spec names the port and nothing else, and the presence "
              "flag read from the same words already states it"),
    _e("presence restated as a label",
       r"^(?:Wi-?Fi|블루투스|미러링|오디오|파일연동)\s*[:：]\s*"
       r"(?:지원|Wi-?Fi)$",
       reason="the label restates a presence the flag already carries, and "
              "'지원' adds no value to read"),
    _e("VESA hole", r"^(?:베사홀|VESA규격)\s*[:：]?\s*"
                    r"(\d{2,4}\s*[xX×]\s*\d{2,4}(?:\s*mm)?)$",
       (("vesa_hole_spec", 1),)),
    _e("VESA screw", r"^(M\d)\s*나사$", (("vesa_screw", 1),)),
    _e("power cable length", r"^전원 케이블 길이\s*(\d+(?:\.\d+)?m)$",
       (("power_cable_length", 1),)),
    _e("HDMI cable included", r"^HDMI\s?케이블(?: 있음| 포함)?$",
       (("hdmi_cable_included_spec", 0),), POLARITY, value=_PRESENT),
    _e("DP cable included", r"^DP 케이블(?: 있음| 포함)?$",
       (("dp_cable_included", 0),), POLARITY, value=_PRESENT),
    _e("remote included", r"^리모컨(?:\s?포함(?:\(음성인식 가능\))?)?$",
       (("remote_present", 0),), POLARITY, value=_PRESENT),
    _e("remote absent", r"^리모컨 없음$",
       (("remote_present", 0),), POLARITY, value=_ABSENT_VALUE),

    # -- smart ------------------------------------------------------------
    _e("OTT app list", r"^(?:OTT|스마트기능)\((.+)\)$",
       (("ott_apps_spec", 1),), LIST),
    _e("OTT denied", r"^(?:자체 )?OTT (?:지원 불가\(.+\)|사용 불가)$",
       (("ott_support_spec", 0),), POLARITY, value=_UNSUPPORTED),
    _e("Samsung TV Plus denied", r"^삼성TV\+? ?사용 불가$",
       (("samsung_tv_plus_spec", 0),), POLARITY, value=_UNSUPPORTED),
    _e("Samsung TV Plus", r"^(?:삼성TV ?플러스|TV ?plus)$",
       (("samsung_tv_plus_spec", 0),), POLARITY, value=_SUPPORTED),
    _e("smart denied", r"^스마트\s*[xX]$",
       (("smart_tv_spec", 0),), POLARITY, value=_UNSUPPORTED),
    _e("smart", r"^(?:스마트|스마트TV)$",
       (("smart_tv_spec", 0),), POLARITY, value=_SUPPORTED),
    _e("app store", r"^삼성앱스토어$",
       (("smart_feature_spec", 0),), POLARITY, value=_SUPPORTED),
    _e("Google Play denied", r"^스마트기능\s*[:：]\s*구글플레이불가$",
       (("google_play", 0),), POLARITY, value=_UNSUPPORTED),
    _e("smart home", r"^스마트기능\s*[:：]\s*(SmartThings)$",
       (("smart_home_spec", 1),)),
    _e("operating system", r"^(?:플랫폼|os)\s*[:：]\s*(.+)$",
       (("os_spec", 1),)),
    _e("AirPlay", r"^에어플레이\(아이폰연동\)$",
       (("airplay_spec", 0),), POLARITY, value=_SUPPORTED),
    _e("mirroring with one platform denied",
       r"^미러링\(아이폰 미러링 지원 안됨\)$", reason=
       "the token asserts mirroring and denies it for one platform at once, "
       "so neither reading is the value of the field"),
    _e("mirroring", r"^(?:미러링|스마트뷰|모바일 화[면명]공유\(.+\))$",
       (("mobile_connect_spec", 0),), POLARITY, value=_SUPPORTED),
    _e("screen split", r"^멀티스크린$",
       (("sw_screen_split", 0),), POLARITY, value=_SUPPORTED),
    _e("voice guide denied", r"^음성 미지원$",
       (("accessibility_voice_guide", 0),), POLARITY, value=_UNSUPPORTED),

    # -- touch ------------------------------------------------------------
    _e("touch points", r"^멀티터치\s*[:：]\s*(\d+포인트)$",
       (("touch_support_spec", 1),)),
    _e("touch", r"^(터치스크린|멀티터치)$",
       (("touch_support_spec", 1),), LIST),
    _e("touch denied", r"^화면터치\s*[xX]$",
       (("touch_support_spec", 0),), POLARITY, value=_UNSUPPORTED),

    # -- power ------------------------------------------------------------
    _e("power input", r"^(?:전원\s*[:：]?\s*)?(AC\s?\d+\s*~\s*\d+\s*[Vv]"
                      r"|\d+\s*~\s*\d+\s*[Vv])$", (("rated_voltage", 1),)),
    _e("power typical", r"^(\d+(?:\.\d+)?\s*W)\s*\(Typical\)$",
       (("power_typical", 1),)),
    _e("power max", r"^(\d+(?:\.\d+)?\s*W)\s*\(Max\)$",
       (("power_max", 1),)),
    _e("power standby", r"^소비전력\(대기\)\s*[:：].+$", reason=
       "the label names standby and the value states two figures without "
       "saying which is which"),
    _e("power consumption with no condition stated",
       r"^(\d+(?:\.\d+)?\s*W)$", (("power_consumption_labelled", 1),)),
    _e("power consumption, labelled",
       r"^소비전력\s*[:：]?\s*(\d+(?:\.\d+)?\s*W?)$",
       (("power_consumption_labelled", 1),)),
    _e("energy grade", r"^에너지(?:소비)?효율\s*(\d+)\s*등급$",
       (("energy_grade_spec", 1),)),

    # -- size, resolution, dimensions, weight -----------------------------
    _e("size inch first",
       r"^(\d+(?:\.\d+)?\s*인치\s*\(\s*\d+(?:\.\d+)?\s*cm\s*\))$",
       (("screen_size", 1),)),
    _e("size metres and inch",
       r"^(\d+(?:\.\d+)?m\s*\(\s*\d+(?:\.\d+)?\s*인치\s*\))$",
       (("screen_size", 1),)),
    _e("size range, labelled",
       r"^화면크기\s*[:：]\s*(\d+~\d+\s*cm\s*\(\s*\d+\s*인치\s*\))$",
       (("screen_size", 1),)),
    _e("size centimetres only", r"^(\d+(?:\.\d+)?\s*cm)$",
       (("screen_size", 1),)),
    # The unit stays in the value: the prompt renders the value and not the
    # unit, so a bare "27" would reach the model with nothing to say whether
    # it is inches or centimetres.
    _e("size inches only", r"^\d+(?:\.\d+)?\s*(?:인치|형)$",
       (("screen_size", 0),)),
    _e("resolution name", r"^(FHD|HD|QHD|UHD|\d+K(?: UHD)?)$",
       (("resolution", 1),)),
    _e("resolution, labelled", r"^(?:화면\s*)?해상도\s*[:：]\s*(.+)$",
       (("resolution", 1),)),
    _e("resolution lines", r"^(\d{3,4}p)$", (("resolution", 1),)),
    _e("screen type", r"^화면종류\s*[:：]\s*(.+)$",
       (("backlight_type", 1),)),
    _e("dimensions with stand",
       r"^(?:크기\(스탠드\s?포함\)\s*[:：]?\s*|스탠드\s?포함\s*)"
       r"(\d[\d.\sxX×]*\d\s*(?:mm|cm)?)$", (("dimensions_with_stand", 1),)),
    _e("dimensions without stand",
       r"^스탠드\s?제외\s?(?:크기)?\(?\s*(\d[\d.\sxX×]*\d)\s*\)?$",
       (("dimensions_without_stand", 1),)),
    _e("dimensions, labelled",
       r"^크기\(?(?:가로\s*[xX×]\s*세로)?\)?\s*[:：]?\s*"
       r"\(?(\d[\d.\sxX×]*\d\s*(?:mm|cm)?)\)?$",
       (("dimensions_product_spec", 1),)),
    _e("weight, labelled", r"^무게\s*[:：]\s*(\d+(?:\.\d+)?\s*kg)$",
       (("weight_product_spec", 1),)),
    _e("bare mass or dimension with no subject",
       r"^\(?\d[\d.,\s~xX×]*\d?\s*(?:g|mm|cm|kg 이상|kg)?\)?$", reason=
       "a figure with no subject: the spec states a mass or a pair of "
       "lengths without saying whether it is the product, the package or "
       "the stand"),
    _e("mass range with no subject", r"^\d+(?:\.\d+)?\s*~\s*\d+(?:\.\d+)?kg$",
       reason="a mass range with no subject, so what it measures is not "
              "stated"),

    # -- what the product is ----------------------------------------------
    _e("series name", r"^([MG]\d)$", (("series_name", 1),), head_only=True),
    _e("category, labelled", r"^품목\s*[:：]\s*(.{2,20})$",
       (("product_category", 1),), LIST),
    _e("category",
       r"^(상업용디스플레이|디스플레이모듈|전자칠판|LED TV|OLED TV|LED 모니터)$",
       (("product_category", 1),), LIST),
    _e("usage environment", r"^(실내용|실외용)$",
       (("usage_environment", 1),)),
    _e("form factor", r"^(휴대용|접이식)$", (("form_factor_spec", 1),)),
    _e("built-in battery", r"^(?:내장배터리|배터리 내장)$",
       (("battery_included", 0),), POLARITY, value=_PRESENT),
    _e("country of origin", r"^제조국\s*[:：]\s*(.{2,10})$",
       (("country_of_origin", 1),)),
    _e("maker", r"^(?:삼성전자(?:\(주\))?|에이서|LG|델|HP|벤큐)$", reason=
       "the maker, which the catalogue's own brand column states, so the "
       "spec's restatement adds nothing"),
    _e("colour", r"^(블랙|화이트|웜 화이트|머큐리실버|실버|그레이)$",
       (("color", 1),)),
    _e("front colour", r"^전면\s*[:：]\s*(.{2,10})$",
       (("front_color_spec", 1),)),
    _e("release year", r"^(\d{2,4}\s*년(?:\s*\d{1,2}\s*월|형))$",
       (("release_ym_spec", 1),)),

    # -- withheld ---------------------------------------------------------
    _e("one half of an electrical rating", r"^\d+(?:\.\d+)?\s*[VA]$",
       reason="one half of a rating the spec writes as a pair (5V/1A), so "
              "neither the voltage nor the current stands on its own"),
    _e("bare ratio", r"^[\d,]+\s*:\s*\d+(?: 이상)?$", reason=
       "a bare ratio: an aspect ratio and a contrast ratio are written the "
       "same way, and the token does not say which it is"),
    _e("screen orientation", r"^화면전환\s*[:：]\s*(?:가로|세로)$", reason=
       "the token names one orientation without saying whether it is the "
       "default or the only one supported"),
    _e("package contents", r"^구성\s*[:：]\s*.+$", reason=
       "what this listing ships, which is a listing fact and is never "
       "pooled across the models that share a base identity"),
    _e("eye tracking", r"^아이트래킹$", reason=
       "no second model states it, so there is nothing to check the reading "
       "against"),
    _e("language support", r"^한국어지원$", reason=
       "the token does not say what is in Korean -- the menu, the voice "
       "guide or the manual"),
    _e("processor", r"^(?:CPU|GPU)\s*[:：].+$", reason=
       "a datasheet line stated by one model, in a vendor's own notation, "
       "with no second source to check the reading against"),
    _e("content support note", r"^\d+FPS 및 .+$", reason=
       "a conditional note about content support rather than an attribute "
       "of the product"),
    _e("stand mount maximum", r"^최대\s*[:：]\s*\d+(?:\.\d+)?\s*mm$", reason=
       "a maximum with no subject: the attribute it bounds is in the token "
       "before it, which the delimiter cut away"),
)

#: Every field an enumeration rule can supply.
ENUM_FIELD_KEYS: frozenset[str] = frozenset(
    field for rule in ENUM_RULES for field, _group in rule.targets
)

#: Fields whose value is a list of items that are all true at once -- apps,
#: port revisions, named features. Read by the answer-grounding check, which
#: has to ask about one member rather than about the whole string.
LIST_FIELDS: frozenset[str] = frozenset(
    rule.field for rules in SECTION_RULES.values() for rule in rules
    if rule.field and rule.shape == LIST
) | frozenset(
    field for rule in ENUM_RULES if rule.shape == LIST
    for field, _group in rule.targets
)

#: Fields whose value is one tier on a ladder. The tier is the claim, so an
#: answer that states a lower one is not stating this fact.
LADDER_FIELDS: frozenset[str] = frozenset(_LADDERS)


def _enum_rule(token: str, *, head: bool) -> EnumRule | None:
    for rule in ENUM_RULES:  # listed order is precedence
        if rule.head_only and not head:
            continue
        if rule.pattern.match(token):
            return rule
    return None


def enumeration_tokens(spec: object) -> list[str]:
    """The tokens no heading governs, in order.

    Only these are offered to the rules above. A token under a heading is
    2-b's, and its heading is what makes it readable -- a bare "92%" means
    one gamut there and nothing here.
    """

    return [token for section, token in sectioned_tokens(spec)
            if not section]


def classify_enumeration_spec(
    spec: object,
) -> tuple[dict[str, tuple[str, str | None, str]],
           tuple[tuple[str, str, str], ...], tuple[str, ...]]:
    """``(safe, withheld, unmapped)`` for one spec's unlabelled tokens.

    ``safe`` maps a field to ``(value, unit, source)``, where the source is
    the token as the spec writes it so provenance survives into the fact.
    ``withheld`` carries ``(source, field_or_empty, reason)`` and ``unmapped``
    the tokens no rule reads, which is how the next rule gets written rather
    than guessed.
    """

    gathered: dict[str, list[tuple[str, str]]] = {}
    shapes: dict[str, str] = {}
    withheld: list[tuple[str, str, str]] = []
    unmapped: list[str] = []

    tokens = enumeration_tokens(spec)
    first = tokens[0] if tokens else None
    for index, token in enumerate(tokens):
        rule = _enum_rule(token, head=index == 0 and token == first)
        if rule is None:
            unmapped.append(token)
            continue
        if not rule.targets:
            withheld.append((token, "", rule.reason))
            continue
        found = rule.pattern.match(token)
        for field, group in rule.targets:
            if rule.value:
                value = rule.value
            else:
                value = ((found.group(group) if found and group else token)
                         or "").strip()
            if not value:
                continue
            gathered.setdefault(field, []).append((value, token))
            shapes[field] = rule.shape

    safe: dict[str, tuple[str, str | None, str]] = {}
    for field, pairs in gathered.items():
        values = list(dict.fromkeys(value for value, _source in pairs))
        sources = " / ".join(dict.fromkeys(source for _value, source in pairs))
        shape = shapes[field]
        if shape == LIST:
            settled: str | None = ", ".join(values)
        elif shape == LADDER:
            settled = _ladder_value(field, values)
            if settled is None:
                withheld.append((
                    sources, field,
                    "the tiers stated are not one ladder, so ranking them "
                    "would invent a certification the spec does not state"))
                continue
        elif len(values) == 1:
            settled = values[0]
        else:
            withheld.append((
                sources, field,
                "the spec states two different values for one attribute; "
                "choosing between them is a review decision"))
            continue
        unit = SPEC_UNITS.get(field) or _POSITIONAL_UNITS.get(field)
        safe[field] = (settled, unit, sources)
    return safe, tuple(withheld), tuple(unmapped)


def facts_from_enumeration_spec(
    spec: object, *, already_supplied: Iterable[str] = (),
) -> dict[str, tuple[str, str | None, str]]:
    """Enumeration facts for the fields nothing better has supplied.

    Last in the same precedence the other three readers keep: a dedicated
    catalogue column, an explicit ``label: value``, a self-describing
    positional token and a section-context token are all better evidence
    than a word recognised by the vocabulary alone, and none of them is
    overwritten here.
    """

    supplied = {str(name) for name in already_supplied}
    safe, _withheld, _unmapped = classify_enumeration_spec(spec)
    return {field: value for field, value in safe.items()
            if field not in supplied}


def withheld_enumeration_tokens(
    spec: object,
) -> tuple[tuple[str, str, str], ...]:
    """``(source, field, reason)`` for every token not promoted."""

    _safe, withheld, _unmapped = classify_enumeration_spec(spec)
    return withheld


def unmapped_enumeration_tokens(spec: object) -> tuple[str, ...]:
    """Every unlabelled token no rule reads."""

    _safe, _withheld, unmapped = classify_enumeration_spec(spec)
    return unmapped
