from __future__ import annotations

from enum import Enum
from typing import TypeVar


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class InquiryStatus(StringEnum):
    NEW = "NEW"
    ANALYZING = "ANALYZING"
    ORDER_PENDING = "ORDER_PENDING"
    DPS_PENDING = "DPS_PENDING"
    ANSWER_PENDING = "ANSWER_PENDING"
    REVIEW_PENDING = "REVIEW_PENDING"
    READY_TO_POST = "READY_TO_POST"
    POSTED = "POSTED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    FAILED = "FAILED"


class StepCode(StringEnum):
    INQUIRY_COLLECTED = "INQUIRY_COLLECTED"
    QUESTION_ANALYZED = "QUESTION_ANALYZED"
    ORDER_IDENTIFIED = "ORDER_IDENTIFIED"
    NAVER_ORDER_LOOKUP = "NAVER_ORDER_LOOKUP"
    DPS_LOOKUP = "DPS_LOOKUP"
    ANSWER_GENERATED = "ANSWER_GENERATED"
    STAFF_REVIEW = "STAFF_REVIEW"
    NAVER_POST = "NAVER_POST"
    LEARNING_SAVED = "LEARNING_SAVED"


class StepStatus(StringEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


DEFAULT_STEP_ORDER: tuple[StepCode, ...] = (
    StepCode.INQUIRY_COLLECTED,
    StepCode.QUESTION_ANALYZED,
    StepCode.ORDER_IDENTIFIED,
    StepCode.NAVER_ORDER_LOOKUP,
    StepCode.DPS_LOOKUP,
    StepCode.ANSWER_GENERATED,
    StepCode.STAFF_REVIEW,
    StepCode.NAVER_POST,
    StepCode.LEARNING_SAVED,
)

STEP_ORDER_INDEX = {
    step_code.value: index
    for index, step_code in enumerate(DEFAULT_STEP_ORDER)
}

ALLOWED_STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset(
        {
            StepStatus.RUNNING,
            StepStatus.FAILED,
            StepStatus.NEEDS_REVIEW,
            StepStatus.SKIPPED,
        }
    ),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.NEEDS_REVIEW,
            StepStatus.SKIPPED,
        }
    ),
    StepStatus.FAILED: frozenset({StepStatus.RUNNING, StepStatus.SKIPPED}),
    StepStatus.NEEDS_REVIEW: frozenset(
        {StepStatus.RUNNING, StepStatus.COMPLETED, StepStatus.SKIPPED}
    ),
    StepStatus.COMPLETED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
}

EnumType = TypeVar("EnumType", bound=StringEnum)


def _validate_enum(value: str | EnumType, enum_type: type[EnumType]) -> EnumType:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(
            f"Invalid {enum_type.__name__}: {value!r}. Allowed: {allowed}"
        ) from error


def validate_inquiry_status(value: str | InquiryStatus) -> InquiryStatus:
    return _validate_enum(value, InquiryStatus)


def validate_step_code(value: str | StepCode) -> StepCode:
    return _validate_enum(value, StepCode)


def validate_step_status(value: str | StepStatus) -> StepStatus:
    return _validate_enum(value, StepStatus)


def validate_step_transition(
    current: str | StepStatus,
    target: str | StepStatus,
) -> tuple[StepStatus, StepStatus]:
    current_status = validate_step_status(current)
    target_status = validate_step_status(target)
    if target_status not in ALLOWED_STEP_TRANSITIONS[current_status]:
        raise ValueError(
            f"Invalid workflow step transition: "
            f"{current_status.value} -> {target_status.value}"
        )
    return current_status, target_status
