from __future__ import annotations

from pathlib import Path

import pytest

from services.env_comparison_service import (
    EnvComparisonService,
    EnvParseError,
    is_secret_name,
    parse_env_file,
)
from services.environment_validation_service import EnvironmentValidationService
from uat.models import EnvironmentRequirement, UatStatus


def checks(environ: dict[str, str]):
    return {
        item.name: item for item in EnvironmentValidationService(environ).validate().checks
    }


def minimum_env(**overrides: str) -> dict[str, str]:
    value = {
        "DEFAULT_STORE_CODE": "OJE_PLUS",
        "OJE_PLUS_ENABLED": "false",
        "SMART_STORE_ENABLED": "false",
        "QNA_GPT_MODE": "FAKE",
        "QNA_GPT_PRIVACY_ENABLED": "true",
    }
    value.update(overrides)
    return value


def test_required_variables_are_valid() -> None:
    result = EnvironmentValidationService(minimum_env()).validate()
    assert result.status is UatStatus.NORMAL


def test_required_variable_missing_is_reported_without_value() -> None:
    items = checks({})
    assert items["DEFAULT_STORE_CODE"].status is UatStatus.NOT_CONFIGURED
    assert "value" not in items["DEFAULT_STORE_CODE"].to_dict()


def test_empty_required_variable_is_missing() -> None:
    items = checks(minimum_env(DEFAULT_STORE_CODE="  "))
    assert items["DEFAULT_STORE_CODE"].present is False


@pytest.mark.parametrize("mode", ["SHADOW", "CANARY", "ACTIVE"])
def test_real_modes_make_provider_fields_conditional(mode: str) -> None:
    items = checks(minimum_env(QNA_GPT_MODE=mode))
    assert items["QNA_GPT_API_KEY"].status is UatStatus.NOT_CONFIGURED
    assert items["QNA_GPT_PROVIDER"].status is UatStatus.NOT_CONFIGURED


@pytest.mark.parametrize("mode", ["FAKE", "DISABLED"])
def test_non_network_modes_do_not_require_api_key(mode: str) -> None:
    items = checks(minimum_env(QNA_GPT_MODE=mode))
    assert items["QNA_GPT_API_KEY"].valid


def test_enabled_store_requires_credentials() -> None:
    items = checks(minimum_env(OJE_PLUS_ENABLED="true"))
    assert items["OJE_PLUS_CLIENT_ID"].status is UatStatus.NOT_CONFIGURED
    assert items["OJE_PLUS_CLIENT_SECRET"].status is UatStatus.NOT_CONFIGURED


def test_disabled_store_does_not_require_credentials() -> None:
    items = checks(minimum_env(OJE_PLUS_ENABLED="false"))
    assert items["OJE_PLUS_CLIENT_SECRET"].valid


def test_invalid_boolean_is_rejected() -> None:
    items = checks(minimum_env(QNA_GPT_PRIVACY_ENABLED="perhaps"))
    assert items["QNA_GPT_PRIVACY_ENABLED"].status is UatStatus.FAILED


def test_invalid_port_is_rejected() -> None:
    items = checks(minimum_env(DPS_AGENT_PORT="99999"))
    assert items["DPS_AGENT_PORT"].status is UatStatus.FAILED


def test_deprecated_variable_is_warning() -> None:
    items = checks(minimum_env(NAVER_CLIENT_ID="configured"))
    assert items["NAVER_CLIENT_ID"].requirement is EnvironmentRequirement.DEPRECATED
    assert items["NAVER_CLIENT_ID"].status is UatStatus.WARNING


def test_unknown_project_variable_is_reported() -> None:
    items = checks(minimum_env(QNA_MISSPELLED_SETTING="x"))
    assert items["QNA_MISSPELLED_SETTING"].requirement is EnvironmentRequirement.UNKNOWN


def test_secret_value_is_not_serialized() -> None:
    secret = "sensitive-test-value"
    result = EnvironmentValidationService(
        minimum_env(
            QNA_GPT_MODE="ACTIVE",
            QNA_GPT_API_KEY=secret,
            QNA_GPT_PROVIDER="openai",
            QNA_GPT_MODEL="model",
            QNA_GPT_COMPANY_APPROVED="true",
        )
    ).validate()
    assert secret not in str(result.to_dict())


def test_parse_env_comments_whitespace_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# comment\n A = 1 \nB='two'\n\n", encoding="utf-8")
    parsed = parse_env_file(path)
    assert parsed["A"].value == "1"
    assert parsed["B"].value == "two"


def test_parse_env_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes("\ufeffNAME=값\n".encode("utf-8"))
    assert parse_env_file(path)["NAME"].value == "값"


def test_parse_env_cp949(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes("NAME=한글\n".encode("cp949"))
    assert parse_env_file(path)["NAME"].value == "한글"


def test_parse_env_rejects_binary(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"\xff\xfe\x00\x81")
    with pytest.raises(EnvParseError):
        parse_env_file(path)


def test_parse_env_rejects_missing_equals(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("INVALID\n", encoding="utf-8")
    with pytest.raises(EnvParseError, match="형식"):
        parse_env_file(path)


def compare_files(tmp_path: Path, left: str, right: str):
    current = tmp_path / "current.env"
    compared = tmp_path / "compared.env"
    current.write_text(left, encoding="utf-8")
    compared.write_text(right, encoding="utf-8")
    return EnvComparisonService().compare(current, compared)


def test_env_compare_same(tmp_path: Path) -> None:
    report = compare_files(tmp_path, "A=1\n", "A=1\n")
    item = next(item for item in report.items if item.name == "A")
    assert item.comparison == "SAME"


def test_env_compare_different_does_not_expose_values(tmp_path: Path) -> None:
    report = compare_files(tmp_path, "QNA_GPT_API_KEY=left\n", "QNA_GPT_API_KEY=right\n")
    text = str(report.to_dict())
    assert "left" not in text and "right" not in text
    item = next(item for item in report.items if item.name == "QNA_GPT_API_KEY")
    assert item.comparison == "DIFFERENT" and item.secret


def test_env_compare_one_side_only(tmp_path: Path) -> None:
    report = compare_files(tmp_path, "ONLY_CURRENT=1\n", "ONLY_OTHER=2\n")
    states = {item.name: item.comparison for item in report.items}
    assert states["ONLY_CURRENT"] == "CURRENT_ONLY"
    assert states["ONLY_OTHER"] == "COMPARED_ONLY"


def test_env_compare_empty_value(tmp_path: Path) -> None:
    report = compare_files(tmp_path, "A=\n", "A=value\n")
    item = next(item for item in report.items if item.name == "A")
    assert item.current_state == "EMPTY"
    assert item.compared_state == "PRESENT"


@pytest.mark.parametrize(
    "name", ["API_KEY", "CLIENT_SECRET", "ACCESS_TOKEN", "PASSWORD", "OTP"]
)
def test_secret_name_detection(name: str) -> None:
    assert is_secret_name(name)


def test_merge_defaults_to_no_overwrite_and_creates_backup(tmp_path: Path) -> None:
    current = tmp_path / ".env"
    compared = tmp_path / "old.env"
    current.write_text("A=current\n", encoding="utf-8")
    compared.write_text("A=old\nB=added\n", encoding="utf-8")
    result = EnvComparisonService().merge_selected(
        current, compared, selected_names=["A", "B"]
    )
    assert result.backup_path.is_file()
    assert result.changed_names == ("B",)
    assert "A=current" in current.read_text(encoding="utf-8")


def test_merge_selected_overwrite_and_rollback(tmp_path: Path) -> None:
    current = tmp_path / ".env"
    compared = tmp_path / "old.env"
    current.write_text("A=current\n", encoding="utf-8")
    compared.write_text("A=old\n", encoding="utf-8")
    service = EnvComparisonService()
    result = service.merge_selected(
        current, compared, selected_names=["A"], overwrite_existing=True
    )
    assert "A=old" in current.read_text(encoding="utf-8")
    service.rollback(current, result.backup_path)
    assert "A=current" in current.read_text(encoding="utf-8")


def test_rollback_rejects_other_directory(tmp_path: Path) -> None:
    current = tmp_path / "a" / ".env"
    backup = tmp_path / "b" / ".env.backup"
    current.parent.mkdir()
    backup.parent.mkdir()
    current.write_text("A=1\n", encoding="utf-8")
    backup.write_text("A=2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        EnvComparisonService().rollback(current, backup)
