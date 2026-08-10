from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values
from answer.governance_models import GptMode
from uat.models import (
    EnvironmentRequirement,
    EnvironmentValidationResult,
    EnvironmentVariableCheck,
    UatStatus,
)


@dataclass(frozen=True)
class VariableSpec:
    name: str
    requirement: EnvironmentRequirement
    scope: str
    description: str
    resolution: str
    secret: bool = False


KNOWN_VARIABLES: tuple[VariableSpec, ...] = (
    VariableSpec(
        "OJE_DB_PATH", EnvironmentRequirement.OPTIONAL, "Database",
        "SQLite 경로 재정의", "미설정 시 data/oje_automation.db를 사용합니다.",
    ),
    VariableSpec(
        "DEFAULT_STORE_CODE", EnvironmentRequirement.REQUIRED, "Naver Store",
        "기본 네이버 스토어 코드", "활성 스토어 코드로 설정하세요.",
    ),
    VariableSpec(
        "OJE_PLUS_ENABLED", EnvironmentRequirement.OPTIONAL, "Naver Store",
        "오제플러스 조회 활성화", "true 또는 false를 사용하세요.",
    ),
    VariableSpec(
        "OJE_PLUS_CLIENT_ID", EnvironmentRequirement.CONDITIONAL, "Naver Store",
        "오제플러스 API Client ID", "스토어 활성 시 등록하세요.", True,
    ),
    VariableSpec(
        "OJE_PLUS_CLIENT_SECRET", EnvironmentRequirement.CONDITIONAL, "Naver Store",
        "오제플러스 API Client Secret", "스토어 활성 시 등록하세요.", True,
    ),
    VariableSpec(
        "SMART_STORE_ENABLED", EnvironmentRequirement.OPTIONAL, "Naver Store",
        "스마트스토어 조회 활성화", "true 또는 false를 사용하세요.",
    ),
    VariableSpec(
        "SMART_STORE_CLIENT_ID", EnvironmentRequirement.CONDITIONAL, "Naver Store",
        "스마트스토어 API Client ID", "스토어 활성 시 등록하세요.", True,
    ),
    VariableSpec(
        "SMART_STORE_CLIENT_SECRET", EnvironmentRequirement.CONDITIONAL, "Naver Store",
        "스마트스토어 API Client Secret", "스토어 활성 시 등록하세요.", True,
    ),
    VariableSpec(
        "DPS_AGENT_HOST", EnvironmentRequirement.OPTIONAL, "DPS Agent",
        "DPS Agent 호스트", "개발 PC 기본값은 127.0.0.1입니다.",
    ),
    VariableSpec(
        "DPS_AGENT_PORT", EnvironmentRequirement.OPTIONAL, "DPS Agent",
        "DPS Agent 포트", "1~65535 숫자를 사용하세요.",
    ),
    VariableSpec(
        "QNA_GPT_MODE", EnvironmentRequirement.OPTIONAL, "GPT Governance",
        "GPT 실행 모드", "FAKE/SHADOW/CANARY/ACTIVE/DISABLED 중 선택하세요.",
    ),
    VariableSpec(
        "QNA_GPT_PROVIDER", EnvironmentRequirement.CONDITIONAL, "GPT Governance",
        "GPT Provider", "실제 모드에서 승인된 provider를 설정하세요.",
    ),
    VariableSpec(
        "QNA_GPT_MODEL", EnvironmentRequirement.CONDITIONAL, "GPT Governance",
        "GPT 모델", "실제 모드에서 허용된 모델을 설정하세요.",
    ),
    VariableSpec(
        "QNA_GPT_API_KEY", EnvironmentRequirement.CONDITIONAL, "GPT Governance",
        "GPT API key", "실제 모드에서 secret store로 공급하세요.", True,
    ),
    VariableSpec(
        "QNA_GPT_COMPANY_APPROVED", EnvironmentRequirement.CONDITIONAL,
        "GPT Governance", "회사 승인 Gate", "승인 후에만 true로 설정하세요.",
    ),
    VariableSpec(
        "QNA_GPT_PRIVACY_ENABLED", EnvironmentRequirement.REQUIRED, "Privacy",
        "Prompt 개인정보 보호", "true로 유지하세요.",
    ),
    VariableSpec(
        "QNA_UAT_MODE", EnvironmentRequirement.OPTIONAL, "UAT Mode",
        "개발 PC UAT 화면", "true이면 UAT 점검 화면을 표시합니다.",
    ),
    VariableSpec(
        "QNA_LOCAL_AUTH_ENABLED", EnvironmentRequirement.OPTIONAL, "Application",
        "로컬 역할 인증", "로컬 사용자 로그인 사용 시 true로 설정하세요.",
    ),
    VariableSpec(
        "NAVER_CLIENT_ID", EnvironmentRequirement.DEPRECATED, "Naver Store",
        "이전 단일 스토어 Client ID", "스토어별 변수로 이전하세요.", True,
    ),
    VariableSpec(
        "NAVER_CLIENT_SECRET", EnvironmentRequirement.DEPRECATED, "Naver Store",
        "이전 단일 스토어 Client Secret", "스토어별 변수로 이전하세요.", True,
    ),
) + tuple(
    VariableSpec(
        name,
        EnvironmentRequirement.OPTIONAL,
        scope,
        "운영 정책 세부 설정",
        "Phase 6A 운영 정책 문서의 허용 범위를 확인하세요.",
    )
    for name, scope in (
        ("DPS_CONNECT_TIMEOUT_SECONDS", "DPS Agent"),
        ("OJE_PLUS_STORE_NAME", "Naver Store"),
        ("SMART_STORE_STORE_NAME", "Naver Store"),
        ("DPS_READ_TIMEOUT_SECONDS", "DPS Agent"),
        ("DPS_TOTAL_TIMEOUT_SECONDS", "DPS Agent"),
        ("DPS_SUCCESS_CACHE_TTL_SECONDS", "DPS Agent"),
        ("DPS_NOT_FOUND_CACHE_TTL_SECONDS", "DPS Agent"),
        ("QNA_GPT_ENABLED", "GPT Governance"),
        ("QNA_GPT_ALLOWED_PROVIDERS", "GPT Governance"),
        ("QNA_GPT_ALLOWED_MODELS", "GPT Governance"),
        ("QNA_GPT_CONNECT_TIMEOUT_SECONDS", "GPT Governance"),
        ("QNA_GPT_READ_TIMEOUT_SECONDS", "GPT Governance"),
        ("QNA_GPT_TOTAL_TIMEOUT_SECONDS", "GPT Governance"),
        ("QNA_GPT_MAX_RETRIES", "GPT Governance"),
        ("QNA_GPT_RETRY_BACKOFF_SECONDS", "GPT Governance"),
        ("QNA_GPT_REQUESTS_PER_MINUTE", "GPT Governance"),
        ("QNA_GPT_DAILY_REQUEST_LIMIT", "GPT Governance"),
        ("QNA_GPT_PER_INQUIRY_LIMIT", "GPT Governance"),
        ("QNA_GPT_REGENERATION_COOLDOWN_SECONDS", "GPT Governance"),
        ("QNA_GPT_DAILY_COST_LIMIT_KRW", "GPT Governance"),
        ("QNA_GPT_USD_KRW_RATE", "GPT Governance"),
        ("QNA_GPT_CANARY_PERCENTAGE", "GPT Governance"),
        ("QNA_GPT_SHADOW_ENABLED", "GPT Governance"),
        ("QNA_GPT_PROMPT_VERSION", "GPT Governance"),
        ("QNA_GPT_PRIVACY_POLICY_VERSION", "Privacy"),
        ("QNA_GPT_VALIDATOR_POLICY_VERSION", "GPT Governance"),
        ("QNA_GPT_COMPANY_TONE_VERSION", "GPT Governance"),
        ("QNA_GPT_POLICY_VERSION", "GPT Governance"),
        ("QNA_GPT_PROMPT_CAPTURE_COMPANY_APPROVED", "Privacy"),
        ("QNA_GPT_PROMPT_CAPTURE_SECURITY_APPROVED", "Privacy"),
    )
)

_BOOLEAN_NAMES = {
    "OJE_PLUS_ENABLED", "SMART_STORE_ENABLED", "QNA_GPT_COMPANY_APPROVED",
    "QNA_GPT_PRIVACY_ENABLED", "QNA_UAT_MODE", "QNA_LOCAL_AUTH_ENABLED",
    "QNA_GPT_ENABLED", "QNA_GPT_SHADOW_ENABLED",
    "QNA_GPT_PROMPT_CAPTURE_COMPANY_APPROVED",
    "QNA_GPT_PROMPT_CAPTURE_SECURITY_APPROVED",
}
_BOOLEAN_VALUES = {"1", "0", "true", "false", "yes", "no", "on", "off"}
_NONNEGATIVE_NUMBER_NAMES = {
    "DPS_CONNECT_TIMEOUT_SECONDS", "DPS_READ_TIMEOUT_SECONDS",
    "DPS_TOTAL_TIMEOUT_SECONDS", "DPS_SUCCESS_CACHE_TTL_SECONDS",
    "DPS_NOT_FOUND_CACHE_TTL_SECONDS", "QNA_GPT_CONNECT_TIMEOUT_SECONDS",
    "QNA_GPT_READ_TIMEOUT_SECONDS", "QNA_GPT_TOTAL_TIMEOUT_SECONDS",
    "QNA_GPT_MAX_RETRIES", "QNA_GPT_RETRY_BACKOFF_SECONDS",
    "QNA_GPT_REQUESTS_PER_MINUTE", "QNA_GPT_DAILY_REQUEST_LIMIT",
    "QNA_GPT_PER_INQUIRY_LIMIT", "QNA_GPT_REGENERATION_COOLDOWN_SECONDS",
    "QNA_GPT_DAILY_COST_LIMIT_KRW", "QNA_GPT_CANARY_PERCENTAGE",
    "QNA_GPT_USD_KRW_RATE",
}


class EnvironmentValidationService:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        if environ is None:
            env_file = Path(__file__).resolve().parents[1] / ".env"
            file_values = {
                key: str(value or "")
                for key, value in dotenv_values(env_file).items()
            }
            file_values.update(os.environ)
            self.environ = file_values
        else:
            self.environ = dict(environ)
        self.specs = {item.name: item for item in KNOWN_VARIABLES}

    def _conditional_required(self, name: str) -> bool:
        mode = str(self.environ.get("QNA_GPT_MODE", "FAKE")).upper()
        if name.startswith("QNA_GPT_"):
            return mode in {"SHADOW", "CANARY", "ACTIVE"}
        if name.startswith("OJE_PLUS_") and name.endswith(
            ("CLIENT_ID", "CLIENT_SECRET")
        ):
            return str(self.environ.get("OJE_PLUS_ENABLED", "true")).lower() not in {
                "0", "false", "no", "off",
            }
        if name.startswith("SMART_STORE_") and name.endswith(
            ("CLIENT_ID", "CLIENT_SECRET")
        ):
            return str(self.environ.get("SMART_STORE_ENABLED", "true")).lower() not in {
                "0", "false", "no", "off",
            }
        return False

    def _valid_format(self, name: str, value: str) -> bool:
        if name in _BOOLEAN_NAMES:
            return value.strip().lower() in _BOOLEAN_VALUES
        if name == "DPS_AGENT_PORT":
            try:
                return 1 <= int(value) <= 65_535
            except ValueError:
                return False
        if name in _NONNEGATIVE_NUMBER_NAMES:
            try:
                number = float(value)
                return number >= 0 and (
                    name != "QNA_GPT_CANARY_PERCENTAGE" or number <= 100
                )
            except ValueError:
                return False
        if name == "QNA_GPT_MODE":
            return value.strip().upper() in {item.value for item in GptMode}
        return bool(value.strip())

    def validate(self) -> EnvironmentValidationResult:
        checks: list[EnvironmentVariableCheck] = []
        for spec in KNOWN_VARIABLES:
            value = self.environ.get(spec.name)
            present = value is not None and bool(str(value).strip())
            required = (
                spec.requirement is EnvironmentRequirement.REQUIRED
                or (
                    spec.requirement is EnvironmentRequirement.CONDITIONAL
                    and self._conditional_required(spec.name)
                )
            )
            valid = present and self._valid_format(spec.name, str(value))
            if spec.requirement is EnvironmentRequirement.DEPRECATED and present:
                status = UatStatus.WARNING
            elif required and not present:
                status = UatStatus.NOT_CONFIGURED
            elif present and not valid:
                status = UatStatus.FAILED
            else:
                status = UatStatus.NORMAL
            checks.append(
                EnvironmentVariableCheck(
                    name=spec.name,
                    requirement=spec.requirement,
                    scope=spec.scope,
                    present=present,
                    valid=(valid if present else not required),
                    description=spec.description,
                    resolution=spec.resolution,
                    status=status,
                )
            )
        for name in sorted(set(self.environ).difference(self.specs)):
            if name.startswith(("QNA_", "NAVER_", "DPS_", "OJE_", "SMART_STORE_")):
                checks.append(
                    EnvironmentVariableCheck(
                        name=name,
                        requirement=EnvironmentRequirement.UNKNOWN,
                        scope="Unknown",
                        present=True,
                        valid=False,
                        description="정의되지 않은 프로젝트 환경변수입니다.",
                        resolution="오타 또는 폐기된 설정인지 확인하세요.",
                        status=UatStatus.WARNING,
                    )
                )
        statuses = {item.status for item in checks}
        overall = (
            UatStatus.FAILED if UatStatus.FAILED in statuses
            else UatStatus.WARNING
            if statuses.intersection({UatStatus.WARNING, UatStatus.NOT_CONFIGURED})
            else UatStatus.NORMAL
        )
        return EnvironmentValidationResult(
            tuple(checks), overall, datetime.now(UTC).isoformat()
        )
