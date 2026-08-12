from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class GitBuildInfo:
    short_hash: str | None
    branch: str | None

    @property
    def display(self) -> str:
        if not self.short_hash:
            return "Build: unknown"
        if self.branch:
            return f"Build · {self.branch} · {self.short_hash}"
        return f"Build · {self.short_hash}"


def _git_output(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return completed.stdout.strip()


def read_git_build_info(
    repository_root: Path = PROJECT_ROOT,
) -> GitBuildInfo:
    """Read Git identity without leaking command failures to the Dashboard."""
    try:
        short_hash = _git_output(repository_root, "rev-parse", "--short", "HEAD")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return GitBuildInfo(short_hash=None, branch=None)
    if not short_hash:
        return GitBuildInfo(short_hash=None, branch=None)
    try:
        branch = _git_output(
            repository_root, "rev-parse", "--abbrev-ref", "HEAD"
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        branch = ""
    return GitBuildInfo(
        short_hash=short_hash,
        branch=branch if branch and branch != "HEAD" else None,
    )


@lru_cache(maxsize=1)
def get_git_build_info() -> GitBuildInfo:
    """Return one process-cached build identity for all UI entry points."""
    return read_git_build_info()
