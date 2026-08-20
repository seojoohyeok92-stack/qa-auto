from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


CHECKPOINT = re.compile(r"DPS_LOOKUP_CHECKPOINT\s+(\{.*\})\s*$")


def parse_runs(log_path: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    with log_path.open(encoding="utf-8", errors="replace") as source:
        for line_number, line in enumerate(source, 1):
            match = CHECKPOINT.search(line)
            if not match:
                continue
            checkpoint = json.loads(match.group(1))
            stage = str(checkpoint.get("checkpoint") or "")
            if stage == "LOOKUP_REQUEST_RECEIVED":
                if current is not None:
                    runs.append(current)
                current = {
                    "started_line": line_number,
                    "timestamp": line[:23],
                    "checkpoints": [],
                }
            if current is None:
                continue
            current["checkpoints"].append(checkpoint)
            if stage == "LOOKUP_RESPONSE_SENT":
                current["completed"] = True
                current["total_ms"] = int(checkpoint.get("total_elapsed_ms") or 0)
                runs.append(current)
                current = None
    if current is not None:
        runs.append(current)
    return runs


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Parse DPS checkpoint wall-clock timings")
    parser.add_argument("log", nargs="?", type=Path, default=Path("logs/dps_agent.log"))
    parser.add_argument("--minimum-ms", type=int, default=1000)
    args = parser.parse_args()
    runs = parse_runs(args.log)
    completed = [run for run in runs if run.get("completed")]
    slow = [run for run in completed if int(run.get("total_ms") or 0) >= args.minimum_ms]
    print(json.dumps({
        "log": str(args.log),
        "run_count": len(runs),
        "completed_count": len(completed),
        "slow_runs": slow,
        "latest_complete": completed[-1] if completed else None,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
