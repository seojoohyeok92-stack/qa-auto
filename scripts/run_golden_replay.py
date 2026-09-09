r"""Golden Production Replay -- build the set, or measure a baseline against it.

    python .\scripts\run_golden_replay.py population
    python .\scripts\run_golden_replay.py select --tier anchor
    python .\scripts\run_golden_replay.py run --tier anchor --name CURRENT_BASELINE
    python .\scripts\run_golden_replay.py compare --before A.json --after B.json

Kept out of pytest on purpose. The suite checks that the code behaves as
specified; this measures whether the specification is producing good answers,
and mixing the two would let a green suite stand in for a quality result.

Nothing here can reach the outside world: the production database is copied
read-only, order lookup / DPS / the Naver client are recorders that raise on
use, and ``--mode fast`` makes no model call at all.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from golden import metrics as metrics_module  # noqa: E402
from golden import population as population_module  # noqa: E402
from golden import selection as selection_module  # noqa: E402
from golden.runner import GoldenReplayWorkspace, run_case  # noqa: E402

DEFAULT_DB = PROJECT_ROOT / "data" / "서버pc_data" / "data" / "oje_automation.db"
CASES_DIR = PROJECT_ROOT / "golden" / "cases"
BASELINE_DIR = PROJECT_ROOT / "golden" / "baselines"


def _catalog_status(rows: list[population_module.PopulationRow]) -> dict[int, str]:
    """Resolve product identity once for the whole corpus."""

    from repositories.product_catalog_repository import ProductCatalogRepository

    repository = ProductCatalogRepository()
    cache: dict[str, str] = {}
    out: dict[int, str] = {}
    for row in rows:
        name = row.product_name
        if name not in cache:
            try:
                cache[name] = str(repository.match(product_name=name).status)
            except Exception:  # noqa: BLE001 - identity is data, never fatal
                cache[name] = "LOOKUP_FAILED"
        out[row.inquiry_id] = cache[name]
    return out


def _git_state() -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                text=True, timeout=30,
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": run("status", "--porcelain"),
    }


def _fingerprint(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path), "exists": True,
        "size_bytes": stat.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stat.st_mtime)),
    }


def cmd_population(args: argparse.Namespace) -> int:
    rows = population_module.load(Path(args.database))
    status = _catalog_status(rows)
    strata = {
        row.inquiry_id: population_module.stratum_of(
            row, catalog_status=status.get(row.inquiry_id, "UNKNOWN")
        )
        for row in rows
    }
    summary = population_module.summarise(rows, strata)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\n[written] {args.out}")
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    rows = population_module.load(Path(args.database))
    status = _catalog_status(rows)
    cases, report = selection_module.select(
        rows, status,
        target_ratio=args.ratio,
        require_replayable=not args.include_unreplayable,
    )
    if args.tier == "anchor":
        cases = [case for case in cases if case.tier == "ANCHOR"]
    labelled = selection_module.apply_labels(
        cases, PROJECT_ROOT / "golden" / "labels" / "anchors.json"
    )
    path = CASES_DIR / f"{args.tier}.jsonl"
    written = selection_module.write_cases(cases, path)
    print(json.dumps({**report, "written": written, "labelled": labelled,
                      "path": str(path)},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    path = CASES_DIR / f"{args.tier}.jsonl"
    cases = selection_module.read_cases(path)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print(f"no cases in {path}")
        return 1

    # The workspace holds a full copy of the production database (~985 MB).
    # Left behind, a few dozen runs fill the disk -- which is exactly what
    # happened. A caller who passes --workspace owns it; one we created is
    # ours to remove.
    temporary = not args.workspace
    workspace_dir = Path(args.workspace or tempfile.mkdtemp(prefix="qa-golden-"))
    print(f"source database : {args.database}  [read-only]")
    workspace = GoldenReplayWorkspace(Path(args.database), workspace_dir)
    print(f"replay database : {workspace.db_path}")
    print(f"semantic index  : {'server copy' if workspace.install_semantic_index() else 'NOT FOUND'}")
    print(f"mode            : {args.mode}\n")

    factory = None
    if args.mode == "full":
        from answer.providers.provider_factory import create_gpt_provider
        factory = lambda: create_gpt_provider("openai")  # noqa: E731

    started = time.monotonic()
    results = []
    for index, case in enumerate(cases, start=1):
        result = run_case(
            case, workspace, mode=args.mode, live_provider_factory=factory
        )
        results.append(result)
        seen = result.observed
        print(
            f"[{index:>3}/{len(cases)}] {case.case_id:<12} {case.tier:<7}"
            f" ran={seen.ran} elig={seen.eligibility_decision}"
            f" unresolved={len(seen.unresolved)}"
            f" L={seen.used_learning_ids} fid={seen.fidelity}"
            + (f" ERROR={seen.error}" if seen.error else "")
        )
    elapsed = round(time.monotonic() - started, 1)

    report = metrics_module.score(results, mode=args.mode)
    report["baseline_name"] = args.name
    report["elapsed_seconds"] = elapsed
    report["tier"] = args.tier
    report["git"] = _git_state()
    report["data_fingerprint"] = {
        "database": _fingerprint(Path(args.database)),
        "semantic_index": _fingerprint(workspace.index_path),
        "cases": _fingerprint(path),
    }
    report["cases_detail"] = [result.to_dict() for result in results]

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    out = BASELINE_DIR / f"{args.name}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"GOLDEN {args.tier.upper()} / mode={args.mode} / {elapsed}s")
    print("=" * 78)
    for name in metrics_module.METRIC_NAMES:
        print(f"  {name:<36} {report['metrics'][name]['render']}")
    print(f"\n  severity: {report['severity']}")
    print(f"  side effects: {report['side_effects']}")
    print(f"  replay fidelity: {report['fidelity']}")
    print(f"\n[written] {out}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """List the cases a person still has to judge, and what to decide."""

    cases = selection_module.read_cases(CASES_DIR / f"{args.tier}.jsonl")
    lines = [
        "# Golden human-review queue",
        "",
        "각 항목은 '프로그램이 무엇을 했는가'가 아니라 '무엇이 옳은가'를 묻는다.",
        "판단이 서지 않으면 비워 두는 것이 정답이다 -- 빈 칸은 NOT_YET_LABELED로",
        "보고되고, 추측으로 채운 값은 그대로 정답 취급되어 다음 비교를 오염시킨다.",
        "",
    ]
    pending = 0
    for case in cases:
        missing = [
            name for name in (
                "expected_answerability", "expected_review_required",
                "expected_order_lookup", "expected_dps_lookup",
                "expected_answer_quality",
            )
            if getattr(case.label, name) is None
        ]
        if not missing and case.label.required_subquestions:
            continue
        pending += 1
        lines += [
            f"## {case.case_id}  ({case.tier})",
            f"- product: {case.product_name}",
            f"- question: {' '.join(case.question.split())[:200]}",
            f"- stratum: action={case.stratum.get('primary_action')}"
            f" identity={case.stratum.get('product_identity')}"
            f" atoms={case.stratum.get('atom_bucket')}"
            f" need_order={case.stratum.get('need_order')}"
            f" need_dps={case.stratum.get('need_dps')}",
            f"- unlabelled: {', '.join(missing) or '(subquestions only)'}",
        ]
        if case.label.production_answer:
            answer = " ".join(case.label.production_answer.split())
            lines.append(f"- 실제 판매자 답변: {answer[:300]}")
        else:
            lines.append("- 실제 판매자 답변: (없음 — 정답 근거를 사람이 정해야 함)")
        lines.append("")
    lines.insert(4, f"미판정 case: {pending} / {len(cases)}")
    out = CASES_DIR / f"human_review_queue_{args.tier}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(chr(10).join(lines), encoding="utf-8")
    print(f"pending={pending} of {len(cases)}")
    print(f"[written] {out}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    print(f"{'metric':<36} {'before':>26} {'after':>26}")
    print("-" * 92)
    for name in metrics_module.METRIC_NAMES:
        b = before["metrics"].get(name, {}).get("render", "-")
        a = after["metrics"].get(name, {}).get("render", "-")
        flag = "" if b == a else "   <-- changed"
        print(f"{name:<36} {b:>26} {a:>26}{flag}")
    print("\nseverity  before:", before.get("severity"), " after:", after.get("severity"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("population", help="measure the evaluable corpus")
    p.add_argument("--out")
    p.set_defaults(func=cmd_population)

    p = sub.add_parser("select", help="build a Golden case file")
    p.add_argument("--tier", default="core", choices=("anchor", "core"))
    p.add_argument("--ratio", type=float, default=0.25)
    p.add_argument("--include-unreplayable", action="store_true")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("run", help="replay a tier and record a baseline")
    p.add_argument("--tier", default="anchor")
    p.add_argument("--mode", default="fast", choices=("fast", "full"))
    p.add_argument("--name", default="UNNAMED_BASELINE")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workspace")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("queue", help="emit the human-review queue")
    p.add_argument("--tier", default="core")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("compare", help="diff two baselines")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
