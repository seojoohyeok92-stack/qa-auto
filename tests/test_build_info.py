from __future__ import annotations

import re
import subprocess
from pathlib import Path

from streamlit.testing.v1 import AppTest

from core import build_info


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_git_build_info_matches_current_repository_head() -> None:
    expected_hash = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    ).stdout.strip()
    info = build_info.read_git_build_info(PROJECT_ROOT)

    assert info.short_hash == expected_hash
    assert re.fullmatch(r"[0-9a-f]+", info.short_hash or "")
    assert expected_hash in info.display


def test_git_unavailable_uses_unknown_fallback(monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(build_info.subprocess, "run", unavailable)
    info = build_info.read_git_build_info(PROJECT_ROOT)

    assert info.short_hash is None
    assert info.branch is None
    assert info.display == "Build: unknown"


def test_git_build_info_is_cached_across_reruns(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def successful(command, **kwargs):
        calls.append(tuple(command))
        output = "2b42cad\n" if "--short" in command else "main\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    build_info.get_git_build_info.cache_clear()
    monkeypatch.setattr(build_info.subprocess, "run", successful)
    try:
        assert build_info.get_git_build_info().display == "Build · main · 2b42cad"
        assert build_info.get_git_build_info().display == "Build · main · 2b42cad"
        assert len(calls) == 2
    finally:
        build_info.get_git_build_info.cache_clear()


def test_build_footer_apptest_renders_compact_identity_and_fallback() -> None:
    app = AppTest.from_string(
        '''
from unittest.mock import patch
from core.build_info import GitBuildInfo
from ui.build_info import render_build_footer
with patch("ui.build_info.get_git_build_info", return_value=GitBuildInfo("abc1234", "main")):
    render_build_footer()
'''
    ).run(timeout=30)
    assert not app.exception
    rendered = "\n".join(item.value for item in app.markdown)
    assert "dashboard-build-footer" in rendered
    assert "Build · main · abc1234" in rendered
    assert str(PROJECT_ROOT) not in rendered

    fallback = AppTest.from_string(
        '''
from unittest.mock import patch
from core.build_info import GitBuildInfo
from ui.build_info import render_build_footer
with patch("ui.build_info.get_git_build_info", return_value=GitBuildInfo(None, None)):
    render_build_footer()
'''
    ).run(timeout=30)
    assert not fallback.exception
    assert any("Build: unknown" in item.value for item in fallback.markdown)
