from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from answer.exceptions import AnswerConfigError


DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "answer_data"


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
def _load_cached(root_text: str) -> AnswerConfig:
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

    active_schedules = tuple(
        row
        for row in schedules
        if str(row.get("사용여부") or "Y").strip().upper()
        in {"Y", "YES", "TRUE", "1"}
    )
    return AnswerConfig(
        answer_policy=answer_policy,
        shipping=shipping,
        events=events,
        models=models,
        model_catalog=model_data["MODEL_CATALOG"],
        install_schedule_rules=active_schedules,
    )


def load_answer_config(
    data_root: str | Path | None = None,
) -> AnswerConfig:
    root = Path(data_root or DEFAULT_DATA_ROOT).resolve()
    return _load_cached(str(root))


def clear_config_cache() -> None:
    _load_cached.cache_clear()
