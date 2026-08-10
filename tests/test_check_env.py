from __future__ import annotations

from pathlib import Path

from scripts.check_env import add_safe_defaults, main, missing_keys


def test_env_checker_never_prints_secret_values(tmp_path: Path, capsys) -> None:
    example = tmp_path / ".env.example"
    current = tmp_path / ".env"
    example.write_text(
        "SAFE_FLAG=true\nCLIENT_SECRET=example-secret\nAPI_KEY=example-key\n",
        encoding="utf-8",
    )
    current.write_text("CLIENT_SECRET=production-secret\n", encoding="utf-8")
    assert main([
        "--example-file", str(example), "--env-file", str(current)
    ]) == 0
    output = capsys.readouterr().out
    assert "example-secret" not in output
    assert "production-secret" not in output
    assert "example-key" not in output
    assert "API_KEY" in output


def test_add_safe_defaults_never_overwrites_or_adds_secrets(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    current = tmp_path / ".env"
    example.write_text(
        "EXISTING=new-default\nSAFE_FLAG=true\nCLIENT_SECRET=unsafe\nEMPTY=\n",
        encoding="utf-8",
    )
    current.write_text("EXISTING=production-value\n", encoding="utf-8")
    added = add_safe_defaults(example, current)
    result = current.read_text(encoding="utf-8")
    assert added == ("SAFE_FLAG",)
    assert "EXISTING=production-value" in result
    assert "EXISTING=new-default" not in result
    assert "CLIENT_SECRET" not in result
    assert "SAFE_FLAG=true" in result


def test_missing_keys_reports_names_only(tmp_path: Path) -> None:
    example = tmp_path / ".env.example"
    current = tmp_path / ".env"
    example.write_text("ONE=secret-one\nTWO=secret-two\n", encoding="utf-8")
    current.write_text("ONE=current-secret\n", encoding="utf-8")
    assert missing_keys(example, current) == ("TWO",)
