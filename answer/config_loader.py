from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from core.time_utils import KST
from answer.exceptions import AnswerConfigError


DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "answer_data"

# Files whose content decides an answer. Their modification time is part of the
# cache key so an operator editing a schedule or policy on the server does not
# have to restart the process before the change reaches customers.
_CACHED_CONFIG_FILES = (
    ("configs", "answer_policy.json"),
    ("configs", "shipping_config.json"),
    ("configs", "event_config.json"),
    ("configs", "model_codes.json"),
    ("configs", "install_schedule_rules.json"),
    ("learning", "model_data_with_color.json"),
)

# Normalised validity metadata is attached under this key. The operator's own
# row is never rewritten -- the original record is preserved verbatim so an
# expired schedule stays auditable in the file it was written in.
SCHEDULE_VALIDITY_KEY = "_validity"

VALIDITY_ACTIVE = "ACTIVE"
VALIDITY_SCHEDULED = "SCHEDULED"
VALIDITY_EXPIRED = "EXPIRED"
VALIDITY_DISABLED = "DISABLED"
VALIDITY_INVALID = "INVALID"

_TRUTHY = {"Y", "YES", "TRUE", "1"}
_VALIDITY_TYPES = ("PERMANENT", "TEMPORARY")

# Korean keys match the rest of this operator-maintained workbook export; the
# English aliases mirror ``learning_examples`` so both spellings are accepted.
_VALIDITY_TYPE_KEYS = ("유효유형", "validity_type")
_VALID_FROM_KEYS = ("유효시작", "valid_from")
_VALID_UNTIL_KEYS = ("유효종료", "valid_until")
_EVENT_NAME_KEYS = ("이벤트명", "event_name")


def _first_key(row: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def _parse_boundary(value: Any) -> tuple[date | None, bool]:
    """Return (date, ok). ``ok`` is False only for an unparseable value."""

    if value in (None, ""):
        return None, True
    if isinstance(value, datetime):
        return value.date(), True
    if isinstance(value, date):
        return value, True
    text = str(value).strip()
    if not text:
        return None, True
    try:
        return date.fromisoformat(text[:10]), True
    except ValueError:
        return None, False


def parse_schedule_validity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise one install-schedule row's validity window.

    Mirrors ``learning_examples``: PERMANENT rows always apply, TEMPORARY rows
    apply only inside their window. An incomplete or unparseable TEMPORARY
    window is rejected rather than guessed -- a schedule promise the operator
    could not date is exactly the kind of claim that must not reach a customer.
    """

    raw_type = str(_first_key(row, _VALIDITY_TYPE_KEYS) or "PERMANENT").strip().upper()
    validity_type = raw_type if raw_type in _VALIDITY_TYPES else "PERMANENT"
    enabled = str(row.get("사용여부") or "Y").strip().upper() in _TRUTHY

    valid_from, from_ok = _parse_boundary(_first_key(row, _VALID_FROM_KEYS))
    valid_until, until_ok = _parse_boundary(_first_key(row, _VALID_UNTIL_KEYS))
    error: str | None = None
    if not from_ok:
        error = "유효시작 날짜 형식이 올바르지 않습니다(YYYY-MM-DD)."
    elif not until_ok:
        error = "유효종료 날짜 형식이 올바르지 않습니다(YYYY-MM-DD)."
    elif raw_type and raw_type not in _VALIDITY_TYPES:
        error = f"알 수 없는 유효유형입니다: {raw_type}"
    elif validity_type == "TEMPORARY" and valid_until is None:
        error = "TEMPORARY 정책에는 유효종료가 필요합니다."
    elif (
        valid_from is not None
        and valid_until is not None
        and valid_until < valid_from
    ):
        error = "유효종료가 유효시작보다 빠릅니다."

    return {
        "type": validity_type,
        "enabled": enabled,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "event_name": str(_first_key(row, _EVENT_NAME_KEYS) or "").strip(),
        "error": error,
    }


def install_schedule_status(
    rule: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """Whether one install-schedule rule may be used right now (KST)."""

    validity = rule.get(SCHEDULE_VALIDITY_KEY)
    if not isinstance(validity, Mapping):
        validity = parse_schedule_validity(rule)
    if validity.get("error"):
        return VALIDITY_INVALID
    if not validity.get("enabled", True):
        return VALIDITY_DISABLED
    if validity.get("type") != "TEMPORARY":
        return VALIDITY_ACTIVE

    current = now or datetime.now(KST)
    current = (
        current.replace(tzinfo=KST) if current.tzinfo is None
        else current.astimezone(KST)
    )
    valid_from = validity.get("valid_from")
    valid_until = validity.get("valid_until")
    if valid_from is not None and current < datetime.combine(
        valid_from, time.min, tzinfo=KST
    ):
        return VALIDITY_SCHEDULED
    if valid_until is not None and current > datetime.combine(
        valid_until, time.max, tzinfo=KST
    ):
        return VALIDITY_EXPIRED
    return VALIDITY_ACTIVE


def active_install_schedule_rules(
    rules: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The subset of loaded rules usable at ``now``; the rest stay on record."""

    moment = now or datetime.now(KST)
    return [
        rule for rule in rules
        if install_schedule_status(rule, now=moment) == VALIDITY_ACTIVE
    ]


@dataclass(frozen=True)
class AnswerConfig:
    answer_policy: dict[str, Any]
    shipping: dict[str, Any]
    events: dict[str, Any]
    models: dict[str, Any]
    model_catalog: dict[str, Any]
    install_schedule_rules: tuple[dict[str, Any], ...]
    learned_rules: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AnswerWrapperTemplate:
    header: str
    footer: str
    legacy_headers: tuple[str, ...] = ()
    legacy_footers: tuple[str, ...] = ()


def _read_json(path: Path, expected_type: type) -> Any:
    if not path.is_file():
        raise AnswerConfigError(f"필수 답변 설정파일이 없습니다: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise AnswerConfigError(
            f"답변 설정 JSON 형식이 올바르지 않습니다: {path.name}"
        ) from error
    except OSError as error:
        raise AnswerConfigError(
            f"답변 설정파일을 읽을 수 없습니다: {path}"
        ) from error
    if not isinstance(value, expected_type):
        raise AnswerConfigError(
            f"답변 설정 형식이 올바르지 않습니다: "
            f"{path.name}에는 {expected_type.__name__} 값이 필요합니다."
        )
    return value


def _require_keys(
    value: dict[str, Any],
    keys: tuple[str, ...],
    file_name: str,
) -> None:
    missing = [key for key in keys if key not in value]
    if missing:
        raise AnswerConfigError(
            f"답변 설정에 필수 항목이 없습니다: "
            f"{file_name} ({', '.join(missing)})"
        )


def load_answer_wrapper(
    data_root: str | Path | None = None,
) -> AnswerWrapperTemplate:
    """Load the shared answer wrapper verbatim from the Template repository."""

    root = Path(data_root or DEFAULT_DATA_ROOT).resolve()
    policy = _read_json(root / "configs" / "answer_policy.json", dict)
    value = policy.get("wrapper")
    if not isinstance(value, dict):
        raise AnswerConfigError(
            "답변 설정에 공통 Wrapper Template가 없습니다: answer_policy.json"
        )
    header = value.get("header")
    footer = value.get("footer")
    if not isinstance(header, str) or not header:
        raise AnswerConfigError("공통 Wrapper Header가 올바르지 않습니다.")
    if not isinstance(footer, str) or not footer:
        raise AnswerConfigError("공통 Wrapper Footer가 올바르지 않습니다.")
    legacy_headers = value.get("legacy_headers", ())
    legacy_footers = value.get("legacy_footers", ())
    if not isinstance(legacy_headers, list) or not all(
        isinstance(item, str) for item in legacy_headers
    ):
        raise AnswerConfigError("공통 Wrapper legacy_headers가 올바르지 않습니다.")
    if not isinstance(legacy_footers, list) or not all(
        isinstance(item, str) for item in legacy_footers
    ):
        raise AnswerConfigError("공통 Wrapper legacy_footers가 올바르지 않습니다.")
    return AnswerWrapperTemplate(
        header=header,
        footer=footer,
        legacy_headers=tuple(legacy_headers),
        legacy_footers=tuple(legacy_footers),
    )


@lru_cache(maxsize=8)
def _load_cached(
    root_text: str,
    signature: tuple[tuple[str, int, int], ...] = (),
) -> AnswerConfig:
    del signature  # part of the cache key only
    root = Path(root_text)
    config_dir = root / "configs"
    learning_dir = root / "learning"

    answer_policy = _read_json(config_dir / "answer_policy.json", dict)
    shipping = _read_json(config_dir / "shipping_config.json", dict)
    events = _read_json(config_dir / "event_config.json", dict)
    models = _read_json(config_dir / "model_codes.json", dict)
    schedules = _read_json(
        config_dir / "install_schedule_rules.json",
        list,
    )
    model_data = _read_json(
        learning_dir / "model_data_with_color.json",
        dict,
    )

    _require_keys(
        answer_policy,
        ("hard_block_rules", "wrapper"),
        "answer_policy.json",
    )
    _require_keys(
        shipping,
        ("shipping_keywords", "parcel_default_answer"),
        "shipping_config.json",
    )
    _require_keys(events, ("review_event", "onnuri"), "event_config.json")
    _require_keys(
        models,
        ("stand_rules", "battery_rules"),
        "model_codes.json",
    )
    _require_keys(
        model_data,
        ("MODEL_CATALOG",),
        "model_data_with_color.json",
    )
    if not all(isinstance(row, dict) for row in schedules):
        raise AnswerConfigError(
            "install_schedule_rules.json의 모든 항목은 object여야 합니다."
        )

    # Validity is parsed here but *applied* at selection time (see
    # ``active_install_schedule_rules``). Filtering expired rows here would
    # freeze the verdict into the cached config: a process that started while
    # an event schedule was still valid would keep answering with it for days
    # after it expired.
    enabled_schedules = tuple(
        {**row, SCHEDULE_VALIDITY_KEY: parse_schedule_validity(row)}
        for row in schedules
        if str(row.get("사용여부") or "Y").strip().upper() in _TRUTHY
    )
    return AnswerConfig(
        answer_policy=answer_policy,
        shipping=shipping,
        events=events,
        models=models,
        model_catalog=model_data["MODEL_CATALOG"],
        install_schedule_rules=enabled_schedules,
    )


def _config_signature(root: Path) -> tuple[tuple[str, int, int], ...]:
    """Identity of the config files on disk, used as part of the cache key."""

    signature: list[tuple[str, int, int]] = []
    for parts in _CACHED_CONFIG_FILES:
        path = root.joinpath(*parts)
        try:
            stat = path.stat()
            signature.append((path.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((path.name, -1, -1))
    return tuple(signature)


def load_answer_config(
    data_root: str | Path | None = None,
) -> AnswerConfig:
    root = Path(data_root or DEFAULT_DATA_ROOT).resolve()
    # The signature is part of the key rather than a reason to clear the cache:
    # an edited file simply misses and reloads, while unchanged files keep
    # hitting the same entry. Nothing has to remember to call
    # ``clear_config_cache`` on the server.
    return _load_cached(str(root), _config_signature(root))


def clear_config_cache() -> None:
    _load_cached.cache_clear()
