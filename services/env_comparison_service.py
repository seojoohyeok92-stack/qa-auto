from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from repositories.database import Database
from repositories.log_repository import LogRepository
from services.environment_validation_service import KNOWN_VARIABLES
from uat.models import EnvironmentRequirement


KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_MARKERS = (
    "SECRET", "TOKEN", "API_KEY", "PASSWORD", "OTP", "COOKIE", "SESSION",
)


class EnvParseError(ValueError):
    pass


@dataclass(frozen=True)
class EnvEntry:
    name: str
    value: str

    @property
    def state(self) -> str:
        return "PRESENT" if self.value else "EMPTY"


@dataclass(frozen=True)
class EnvComparisonItem:
    name: str
    current_state: str
    compared_state: str
    comparison: str
    requirement: str
    secret: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EnvComparisonReport:
    items: tuple[EnvComparisonItem, ...]
    current_file_name: str
    compared_file_name: str

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [item.to_dict() for item in self.items],
            "current_file_name": self.current_file_name,
            "compared_file_name": self.compared_file_name,
        }


@dataclass(frozen=True)
class EnvMergeResult:
    backup_path: Path
    changed_names: tuple[str, ...]


def is_secret_name(name: str) -> bool:
    upper = str(name).upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise EnvParseError(f"{path.name} 파일을 텍스트로 해석할 수 없습니다.")


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, EnvEntry]:
    target = Path(path)
    if not target.is_file():
        raise EnvParseError(f"{target.name} 파일을 찾을 수 없습니다.")
    parsed: dict[str, EnvEntry] = {}
    for line_number, raw_line in enumerate(_read_text(target).splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise EnvParseError(
                f"{target.name}의 {line_number}행 형식이 올바르지 않습니다."
            )
        name, value = line.split("=", 1)
        name = name.strip()
        if not KEY_PATTERN.fullmatch(name):
            raise EnvParseError(
                f"{target.name}의 {line_number}행 변수명이 올바르지 않습니다."
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        parsed[name] = EnvEntry(name, value)
    return parsed


class EnvComparisonService:
    def __init__(
        self, database: Database | None = None, *, actor: str = "local-admin"
    ) -> None:
        self.specs = {item.name: item for item in KNOWN_VARIABLES}
        self.logs = LogRepository(database) if database else None
        self.actor = str(actor).strip() or "local-admin"

    def compare(
        self,
        current_path: str | os.PathLike[str],
        compared_path: str | os.PathLike[str],
    ) -> EnvComparisonReport:
        current = parse_env_file(current_path)
        compared = parse_env_file(compared_path)
        items: list[EnvComparisonItem] = []
        for name in sorted(set(current) | set(compared) | set(self.specs)):
            left = current.get(name)
            right = compared.get(name)
            if left is None:
                comparison = "COMPARED_ONLY"
            elif right is None:
                comparison = "CURRENT_ONLY"
            elif left.value == right.value:
                comparison = "SAME"
            else:
                comparison = "DIFFERENT"
            spec = self.specs.get(name)
            requirement = (
                spec.requirement.value
                if spec is not None
                else EnvironmentRequirement.UNKNOWN.value
            )
            items.append(
                EnvComparisonItem(
                    name=name,
                    current_state=left.state if left else "MISSING",
                    compared_state=right.state if right else "MISSING",
                    comparison=comparison,
                    requirement=requirement,
                    secret=is_secret_name(name),
                )
            )
        return EnvComparisonReport(
            tuple(items), Path(current_path).name, Path(compared_path).name
        )

    def merge_selected(
        self,
        current_path: str | os.PathLike[str],
        compared_path: str | os.PathLike[str],
        *,
        selected_names: Iterable[str],
        overwrite_existing: bool = False,
    ) -> EnvMergeResult:
        current_file = Path(current_path)
        compared = parse_env_file(compared_path)
        current = parse_env_file(current_file)
        selected = tuple(dict.fromkeys(str(name) for name in selected_names))
        changes: dict[str, str] = {}
        for name in selected:
            if name not in compared:
                continue
            if name in current and not overwrite_existing:
                continue
            changes[name] = compared[name].value
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = current_file.with_name(f"{current_file.name}.backup_{timestamp}")
        shutil.copy2(current_file, backup)
        if changes:
            lines = _read_text(current_file).splitlines()
            positions: dict[str, int] = {}
            for index, raw_line in enumerate(lines):
                candidate = raw_line.strip()
                if "=" in candidate and not candidate.startswith("#"):
                    key = candidate.split("=", 1)[0].strip()
                    if KEY_PATTERN.fullmatch(key):
                        positions[key] = index
            for name, value in changes.items():
                replacement = f"{name}={value}"
                if name in positions:
                    lines[positions[name]] = replacement
                else:
                    lines.append(replacement)
            current_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = EnvMergeResult(backup, tuple(changes))
        if self.logs:
            self.logs.record_system(
                "ENV_MERGE_COMPLETED",
                "선택한 환경변수 병합을 완료했습니다.",
                details={
                    "actor": self.actor,
                    "changed_names": list(result.changed_names),
                    "backup_file_name": result.backup_path.name,
                    "secret_values_recorded": False,
                },
            )
        return result

    def rollback(
        self,
        current_path: str | os.PathLike[str],
        backup_path: str | os.PathLike[str],
    ) -> None:
        current = Path(current_path).resolve()
        backup = Path(backup_path).resolve()
        if not backup.is_file() or backup.parent != current.parent:
            raise ValueError("같은 폴더의 유효한 .env 백업만 복원할 수 있습니다.")
        shutil.copy2(backup, current)
        if self.logs:
            self.logs.record_system(
                "ENV_MERGE_ROLLED_BACK",
                "환경변수 파일을 선택한 백업으로 복원했습니다.",
                level="WARNING",
                details={
                    "actor": self.actor,
                    "backup_file_name": backup.name,
                    "secret_values_recorded": False,
                },
            )
