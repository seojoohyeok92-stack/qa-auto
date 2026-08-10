from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = PROJECT_ROOT / "answer" / "engine.py"
DELIVERY_PATH = PROJECT_ROOT / "services" / "phase9_answer_policy.py"
CONFIG_DIR = PROJECT_ROOT / "answer_data" / "configs"
TEST_DIR = PROJECT_ROOT / "tests"
PLACEHOLDER_PATTERN = re.compile(
    r"\{\{[^{}]+\}\}|\$\{[^{}]+\}|(?<!\{)\{[A-Za-z_][^{}]*\}(?!\})"
)


def _literal_strings(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    return list(
        dict.fromkeys(
            value.value
            for value in ast.walk(node)
            if isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value.strip()
        )
    )


def _value(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ast.unparse(node) if node is not None else ""


def _tested(template: dict[str, Any], test_text: str) -> bool:
    candidates = [
        str(template.get("template_name") or ""),
        str(template.get("category") or ""),
        *[str(value) for value in template.get("positive_keywords") or []],
    ]
    return any(value and value in test_text for value in candidates)


class RuleVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.conditions: list[ast.AST] = []
        self.rows: list[dict[str, Any]] = []

    def visit_If(self, node: ast.If) -> None:
        self.conditions.append(node.test)
        for child in node.body:
            self.visit(child)
        self.conditions.pop()
        for child in node.orelse:
            self.visit(child)

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name not in {"yes", "need_info"} or len(node.args) < 2:
            self.generic_visit(node)
            return
        category = _value(node.args[0])
        answer = _value(node.args[1])
        positive: list[str] = []
        for condition in self.conditions:
            positive.extend(_literal_strings(condition))
        positive = list(dict.fromkeys(positive))
        product_db = category.startswith("모델스펙/") and "item" in answer
        self.rows.append(
            {
                "template_id": f"rule:{category}:{node.lineno}",
                "template_name": category,
                "category": category,
                "source_file": f"answer/engine.py:{node.lineno}",
                "active": True,
                "stores": ["ALL"],
                "inquiry_types": ["PRODUCT_INQUIRY", "CUSTOMER_INQUIRY"],
                "positive_keywords": positive,
                "negative_keywords": [],
                "priority": node.lineno,
                "answer_text": answer,
                "placeholders": PLACEHOLDER_PATTERN.findall(answer),
                "placeholders_required": bool(
                    PLACEHOLDER_PATTERN.search(answer)
                ),
                "validator": "PRODUCT_DB" if product_db else "TEMPLATE",
                "service_path": "AnswerService -> AnswerEngine",
                "connected": True,
                "source_kind": "PRODUCT_DB" if product_db else "PYTHON_RULE",
            }
        )
        self.generic_visit(node)


def _python_rules() -> list[dict[str, Any]]:
    visitor = RuleVisitor()
    visitor.visit(ast.parse(ENGINE_PATH.read_text(encoding="utf-8")))
    return visitor.rows


def _config_templates() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(CONFIG_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))

        def walk(
            value: Any,
            keys: list[str],
            parent: dict[str, Any] | None = None,
        ) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    walk(child, [*keys, str(key)], value)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, [*keys, str(index)], parent)
            elif (
                isinstance(value, str)
                and keys
                and (
                    keys[-1].endswith("answer")
                    or keys[-1] in {"신규주문안내", "기존주문안내"}
                )
            ):
                keywords = (
                    parent.get("keywords", [])
                    if isinstance(parent, dict)
                    else []
                )
                if isinstance(parent, dict) and not keywords:
                    keywords = [
                        *str(parent.get("상품키워드") or "").split(","),
                        *str(parent.get("모델키워드") or "").split(","),
                    ]
                    keywords = [value.strip() for value in keywords if value.strip()]
                identifier = ".".join(keys)
                rows.append(
                    {
                        "template_id": f"config:{path.name}:{identifier}",
                        "template_name": identifier,
                        "category": keys[0],
                        "source_file": f"answer_data/configs/{path.name}",
                        "active": True,
                        "stores": ["ALL"],
                        "inquiry_types": [
                            "PRODUCT_INQUIRY",
                            "CUSTOMER_INQUIRY",
                        ],
                        "positive_keywords": list(keywords),
                        "negative_keywords": [],
                        "priority": 100,
                        "answer_text": value,
                        "placeholders": PLACEHOLDER_PATTERN.findall(value),
                        "placeholders_required": bool(
                            PLACEHOLDER_PATTERN.search(value)
                        ),
                        "validator": "TEMPLATE",
                        "service_path": "AnswerConfig -> AnswerEngine",
                        "connected": True,
                        "source_kind": "JSON_CONFIG",
                    }
                )

        walk(data, [])
    return rows


def _delivery_templates() -> list[dict[str, Any]]:
    tree = ast.parse(DELIVERY_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        target = targets[0] if targets else None
        name = target.id if isinstance(target, ast.Name) else ""
        value = node.value
        if not name.endswith("_ANSWER"):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            answer = value.value
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "format_final_answer"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            answer = value.args[0].value
        else:
            continue
        rows.append(
            {
                "template_id": f"delivery:{name}",
                "template_name": name,
                "category": "DELIVERY_INSTALLATION",
                "source_file": f"services/phase9_answer_policy.py:{node.lineno}",
                "active": True,
                "stores": ["ALL"],
                "inquiry_types": ["PRODUCT_INQUIRY", "CUSTOMER_INQUIRY"],
                "positive_keywords": ["배송", "설치", "도착", "기사님"],
                "negative_keywords": [],
                "priority": 2,
                "answer_text": answer,
                "placeholders": PLACEHOLDER_PATTERN.findall(answer),
                "placeholders_required": bool(
                    PLACEHOLDER_PATTERN.search(answer)
                ),
                "validator": name,
                "service_path": "AnswerService -> phase9_answer_policy",
                "connected": True,
                "source_kind": "DELIVERY_SAFE_TEMPLATE",
            }
        )
    return rows


def catalog() -> list[dict[str, Any]]:
    test_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in TEST_DIR.glob("test_*.py")
    )
    rows = [*_delivery_templates(), *_config_templates(), *_python_rules()]
    for row in rows:
        row["tested"] = _tested(row, test_text)
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("priority") or 9999),
            str(row.get("template_id") or ""),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List connected fixed templates and answer rules."
    )
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--keyword", default="")
    args = parser.parse_args()
    rows = catalog()
    if args.active_only:
        rows = [row for row in rows if row["active"]]
    keyword = str(args.keyword or "").casefold().strip()
    if keyword:
        rows = [
            row
            for row in rows
            if keyword
            in json.dumps(row, ensure_ascii=False).casefold()
        ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
