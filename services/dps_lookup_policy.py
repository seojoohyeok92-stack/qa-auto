from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum

from answer.models import AnswerRequest


class DpsLookupStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    WAITING_FOR_ORDER_ID = "WAITING_FOR_ORDER_ID"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    AGENT_OFFLINE = "AGENT_OFFLINE"
    AUTOMATION_ERROR = "AUTOMATION_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    STALE_CACHE = "STALE_CACHE"
    CANCELLED = "CANCELLED"

    def __str__(self) -> str:
        return self.value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class DpsSettings:
    connect_timeout_seconds: float = 7.0
    read_timeout_seconds: float = 100.0
    total_timeout_seconds: float = 120.0
    refresh_interval_minutes: int = 30
    success_ttl_seconds: int = 1_800
    not_found_ttl_seconds: int = 300

    @classmethod
    def from_environment(cls) -> "DpsSettings":
        refresh_minutes = int(
            _positive_float("DPS_REFRESH_INTERVAL_MINUTES", 30)
        )
        return cls(
            connect_timeout_seconds=_positive_float(
                "DPS_CONNECT_TIMEOUT_SECONDS", 7.0
            ),
            read_timeout_seconds=_positive_float(
                "DPS_READ_TIMEOUT_SECONDS", 100.0
            ),
            total_timeout_seconds=_positive_float(
                "DPS_TOTAL_TIMEOUT_SECONDS", 120.0
            ),
            refresh_interval_minutes=refresh_minutes,
            # DPS is demand-driven rather than blindly polled. A successful
            # order lookup is refreshed after this cache freshness interval.
            success_ttl_seconds=int(
                _positive_float(
                    "DPS_SUCCESS_CACHE_TTL_SECONDS", refresh_minutes * 60
                )
            ),
            not_found_ttl_seconds=int(
                _positive_float("DPS_NOT_FOUND_CACHE_TTL_SECONDS", 300)
            ),
        )


CHANGE_PATTERNS = (
    "설치일 변경",
    "설치 날짜 변경",
    "배송일 변경",
    "배송 날짜 변경",
    "주소 변경",
    "변경해주세요",
    "변경해 주세요",
    "바꿔주세요",
    "바꿔 주세요",
    "연기해주세요",
    "연기해 주세요",
    "오늘 꼭",
    "기사님 연락처",
)
LOOKUP_PATTERNS = (
    "배송은 언제",
    "배송 언제",
    "배송 예정",
    "배송 상태",
    "배송이 늦",
    "배송 지연",
    "출고 상태",
    "출고는 언제",
    "출고 언제",
    "설치는 언제",
    "설치 언제",
    "설치일",
    "설치 예정일",
    "방문일",
    "방문 예정",
    "기사님 방문",
    "몇 시에 도착",
    "몇시에 도착",
    "언제 도착",
    "언제 받을",
    "언제쯤 받을",
    "언제쯤받을",
    "받을 수 있을",
    "받을수있을",
    "언제 와",
    "언제와",
    "오늘 오",
    "내일 오",
    "도착 시간",
    "도착시간",
    "설치 날짜",
    "설치날짜",
    "방문 시간",
    "방문시간",
    "기사님 언제",
    "기사님 몇 시",
    "기사님 몇시",
    # Promised-date/deadline wording seen in real Naver inquiries. These do
    # not necessarily repeat the words 배송/설치 in every sentence.
    "예정일",
    "말일까지",
    "기다리다 지쳐",
    "기다리다",
)


def split_question_segments(question: str) -> tuple[str, ...]:
    text = str(question or "").strip()
    if not text:
        return ()
    parts = re.split(r"(?<=[?.!。！？])\s*|[\r\n]+", text)
    return tuple(part.strip() for part in parts if part.strip())


def is_change_request(question: str) -> bool:
    compact = re.sub(r"\s+", " ", str(question or "")).strip()
    return any(pattern in compact for pattern in CHANGE_PATTERNS)


def requires_dps_lookup(question: str) -> bool:
    compact = re.sub(r"\s+", " ", str(question or "")).strip()
    if is_change_request(compact):
        return True
    no_space = re.sub(r"\s+", "", compact)
    return any(
        pattern in compact or re.sub(r"\s+", "", pattern) in no_space
        for pattern in LOOKUP_PATTERNS
    )


@dataclass(frozen=True)
class DpsLookupDecision:
    lookup_required: bool
    status: DpsLookupStatus
    change_request: bool
    order_id: str | None
    general_segments: tuple[str, ...]
    dps_segments: tuple[str, ...]
    reason: str


class DpsLookupPolicy:
    def decide(self, request: AnswerRequest) -> DpsLookupDecision:
        segments = split_question_segments(request.question)
        dps_segments = tuple(
            segment for segment in segments if requires_dps_lookup(segment)
        )
        general_segments = tuple(
            segment for segment in segments if segment not in dps_segments
        )
        processing_plan = request.metadata.get("processing_plan")
        processing_plan = (
            processing_plan if isinstance(processing_plan, dict) else {}
        )
        plan_requires_dps = bool(
            processing_plan.get("requires_dps_lookup")
            and processing_plan.get("is_delivery")
        )
        required = bool(
            dps_segments
            or requires_dps_lookup(request.question)
            or plan_requires_dps
        )
        if plan_requires_dps and required and not dps_segments:
            # The canonical phase-9 analysis has already identified a
            # schedule inquiry. The lower UI policy must not silently turn it
            # back into NOT_REQUIRED merely because its legacy phrase list is
            # narrower. Preserve partial-answer behavior whenever a matching
            # segment exists; only use the full question as a last resort.
            dps_segments = segments or (
                (request.question,) if request.question else ()
            )
            general_segments = ()
        change = is_change_request(request.question)
        order_id = str(request.order_id or "").strip() or None
        if not required:
            status = DpsLookupStatus.NOT_REQUIRED
            reason = "DPS 조회가 필요하지 않은 일반 문의입니다."
        elif not order_id:
            status = DpsLookupStatus.WAITING_FOR_ORDER_ID
            reason = "DPS 조회에 필요한 네이버 일반 주문번호가 없습니다."
        else:
            status = DpsLookupStatus.PENDING
            reason = "배송·설치 현황 확인을 위해 DPS 조회가 필요합니다."
        return DpsLookupDecision(
            lookup_required=required,
            status=status,
            change_request=change,
            order_id=order_id,
            general_segments=general_segments,
            dps_segments=dps_segments,
            reason=reason,
        )
