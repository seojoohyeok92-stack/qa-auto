from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_MARKERS = (
    "SECRET",
    "API_KEY",
    "PASSWORD",
    "TOKEN",
    "PRIVATE_KEY",
    "ACCESS_KEY",
)


@dataclass(frozen=True, slots=True)
class EnvLine:
    key: str
    value: str


def is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_MARKERS)


def parse_env(path: Path) -> dict[str, EnvLine]:
    if not path.is_file():
        raise FileNotFoundError(path)
    entries: dict[str, EnvLine] = {}
    text = path.read_text(encoding="utf-8-sig")
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"Invalid env line {line_number} in {path.name}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ValueError(f"Invalid env key at line {line_number} in {path.name}")
        entries[key] = EnvLine(key, value.strip())
    return entries


def missing_keys(example_path: Path, env_path: Path) -> tuple[str, ...]:
    example = parse_env(example_path)
    current = parse_env(env_path)
    return tuple(key for key in example if key not in current)


def add_safe_defaults(example_path: Path, env_path: Path) -> tuple[str, ...]:
    example = parse_env(example_path)
    current = parse_env(env_path)
    additions = tuple(
        entry
        for key, entry in example.items()
        if key not in current and entry.value and not is_secret_key(key)
    )
    if not additions:
        return ()

    original = env_path.read_text(encoding="utf-8-sig")
    separator = "" if not original or original.endswith(("\n", "\r")) else "\n"
    block = ["", "# Safe defaults added from .env.example by scripts/check_env.py"]
    block.extend(f"{entry.key}={entry.value}" for entry in additions)
    with env_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(separator + "\n".join(block) + "\n")
    return tuple(entry.key for entry in additions)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare .env keys without displaying any values."
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--example-file", type=Path, default=Path(".env.example")
    )
    parser.add_argument(
        "--add-safe-defaults",
        action="store_true",
        help="Append only missing, non-secret, non-empty example defaults.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        missing = missing_keys(args.example_file, args.env_file)
    except FileNotFoundError as error:
        missing_path = error.filename or (error.args[0] if error.args else ".env")
        print(f"Environment file not found: {Path(missing_path).name}")
        return 2
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Environment check failed: {error}")
        return 2

    if missing:
        print("Missing keys (values are never shown):")
        for key in missing:
            print(f"- {key}")
    else:
        print("No keys are missing.")

    if args.add_safe_defaults:
        added = add_safe_defaults(args.example_file, args.env_file)
        if added:
            print("Added safe defaults (existing values were not changed):")
            for key in added:
                print(f"- {key}")
        else:
            print("No safe defaults were added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
