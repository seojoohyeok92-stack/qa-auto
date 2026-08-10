from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from answer.config_loader import (
    DEFAULT_DATA_ROOT,
    clear_config_cache,
    load_answer_config,
)
from answer.exceptions import AnswerConfigError
from answer.models import AnswerRequest, AnswerResult, AnswerStatus


def test_answer_request_normal_creation() -> None:
    request = AnswerRequest(
        inquiry_id=10,
        question_id="Q-10",
        store_code="OJE_PLUS",
        question="배송 문의",
        product_name="TV",
        metadata={"source": "test"},
    )
    assert request.inquiry_id == 10
    assert request.question_id == "Q-10"
    assert request.metadata == {"source": "test"}


def test_answer_request_handles_missing_fields() -> None:
    request = AnswerRequest(question=None, product_name=None, metadata=None)
    assert request.question == ""
    assert request.product_name == ""
    assert request.metadata == {}


def test_answer_result_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="Invalid AnswerStatus"):
        AnswerResult(
            status="TYPO",
            category="test",
            reason="test",
            answer="test",
            provider="rules",
            auto_answerable=True,
            needs_review=False,
        )


def test_answer_result_serialization() -> None:
    result = AnswerResult(
        status=AnswerStatus.GENERATED,
        category="배송",
        reason="규칙 일치",
        answer="답변",
        provider="rules",
        auto_answerable=True,
        needs_review=False,
        warnings=("검토",),
    )
    serialized = result.to_dict()
    assert serialized["status"] == "GENERATED"
    assert serialized["warnings"] == ["검토"]


def test_config_loader_loads_expected_data() -> None:
    clear_config_cache()
    config = load_answer_config()
    assert config.answer_policy["hard_block_rules"]
    assert config.shipping["parcel_default_answer"]
    assert len(config.model_catalog) == 1586
    assert len(config.install_schedule_rules) == 9


def test_config_loader_is_independent_of_working_directory(
    tmp_path,
    monkeypatch,
) -> None:
    clear_config_cache()
    monkeypatch.chdir(tmp_path)
    config = load_answer_config()
    assert len(config.model_catalog) == 1586


def test_config_loader_reports_missing_file(tmp_path) -> None:
    data_root = tmp_path / "answer_data"
    (data_root / "configs").mkdir(parents=True)
    (data_root / "learning").mkdir(parents=True)
    with pytest.raises(AnswerConfigError, match="필수 답변 설정파일"):
        load_answer_config(data_root)


def test_config_loader_rejects_invalid_json(tmp_path) -> None:
    data_root = tmp_path / "answer_data"
    shutil.copytree(DEFAULT_DATA_ROOT, data_root)
    (data_root / "configs" / "answer_policy.json").write_text(
        "{invalid",
        encoding="utf-8",
    )
    clear_config_cache()
    with pytest.raises(AnswerConfigError, match="JSON 형식"):
        load_answer_config(data_root)


def test_config_cache_can_be_cleared(tmp_path) -> None:
    data_root = tmp_path / "answer_data"
    shutil.copytree(DEFAULT_DATA_ROOT, data_root)
    clear_config_cache()
    first = load_answer_config(data_root)
    policy_path = data_root / "configs" / "answer_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["cache_probe"] = "changed"
    policy_path.write_text(
        json.dumps(policy, ensure_ascii=False),
        encoding="utf-8",
    )
    cached = load_answer_config(data_root)
    assert "cache_probe" not in cached.answer_policy
    clear_config_cache()
    reloaded = load_answer_config(data_root)
    assert reloaded.answer_policy["cache_probe"] == "changed"
