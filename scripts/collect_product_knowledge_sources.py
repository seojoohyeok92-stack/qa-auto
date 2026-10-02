"""Read-only source collector for the production Product Knowledge catalog.

The collector deliberately has no authentication flow and no write API
client.  Every network request is an HTTP GET.  Commerce API mode therefore
requires an already-issued access token file; this script never exchanges a
client secret for a token.

Artifacts are immutable evidence, not runtime facts.  A later verifier must
still establish product/model/subject/scope before anything is merged into
``data/model_data_with_color.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from PIL import Image


COMMERCE_PRODUCT_URL = (
    "https://api.commerce.naver.com/external/v2/products/channel-products/{product_id}"
)
PUBLIC_PRODUCT_URL = "https://smartstore.naver.com/samsung_monitor/products/{product_id}"
IMAGE_ATTRIBUTES = (
    "src", "currentsrc", "srcset", "data-src", "data-original", "data-lazy-src",
)
IMAGE_KEY_MARKERS = ("image", "img", "thumbnail", "representative")
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
CSS_URL_RE = re.compile(r"url\((?:['\"])?([^)'\"]+)", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def catalog_product_ids(payload: Mapping[str, Any]) -> list[str]:
    """Return current listing IDs represented by integrated Product Knowledge."""

    knowledge = payload.get("PRODUCT_KNOWLEDGE") or {}
    found: set[str] = set()
    for section in (
        "model_facts", "listing_facts", "bundle_accessory_facts", "policy_facts",
        "multi_model_facts", "excluded_records",
    ):
        rows = knowledge.get(section) or []
        if isinstance(rows, Mapping):
            rows = rows.values()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for key in ("product_id", "applies_to_product_id", "source_product_id"):
                value = str(row.get(key) or "").strip()
                if value.isdigit():
                    found.add(value)
            for key in ("source_product_ids", "applies_to_product_ids"):
                for item in row.get(key) or ():
                    value = str(item or "").strip()
                    if value.isdigit():
                        found.add(value)
    return sorted(found, key=lambda value: (len(value), value))


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() in {"img", "source"}:
            for key in IMAGE_ATTRIBUTES:
                self.urls.update(_attribute_urls(values.get(key, "")))
        elif tag.lower() == "iframe":
            self.urls.update(_attribute_urls(values.get("src", "")))
        style = values.get("style", "")
        self.urls.update(match.group(1) for match in CSS_URL_RE.finditer(style))

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.text.append(value)


def _attribute_urls(value: str) -> set[str]:
    result: set[str] = set()
    for part in str(value or "").split(","):
        candidate = part.strip().split(" ", 1)[0]
        if candidate:
            result.add(html.unescape(candidate))
    return result


def html_evidence(value: str, *, base_url: str) -> tuple[str, set[str]]:
    parser = _PageParser()
    parser.feed(value or "")
    urls = {
        urllib.parse.urljoin(base_url, candidate)
        for candidate in parser.urls
        if candidate and not candidate.startswith(("data:", "javascript:"))
    }
    return "\n".join(parser.text), urls


def _json_image_urls(node: Any, *, key_hint: str = "") -> set[str]:
    result: set[str] = set()
    if isinstance(node, Mapping):
        for key, value in node.items():
            child_key = str(key).lower()
            child_hint = (
                key_hint if child_key in {"url", "source", "src"} and key_hint
                else child_key
            )
            result.update(_json_image_urls(value, key_hint=child_hint))
    elif isinstance(node, list):
        for value in node:
            result.update(_json_image_urls(value, key_hint=key_hint))
    elif isinstance(node, str):
        if "<" in node and ">" in node:
            _, urls = html_evidence(node, base_url="")
            result.update(urls)
        if any(marker in key_hint for marker in IMAGE_KEY_MARKERS):
            result.update(URL_RE.findall(node))
    return result


def _detail_html_fragments(node: Any) -> list[str]:
    result: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            if str(key).lower() == "detailcontent" and isinstance(value, str):
                result.append(value)
            elif isinstance(value, (Mapping, list)):
                result.extend(_detail_html_fragments(value))
    elif isinstance(node, list):
        for value in node:
            result.extend(_detail_html_fragments(value))
    return result


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


Get = Callable[[str, Mapping[str, str]], Response]


def http_get(url: str, headers: Mapping[str, str]) -> Response:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return Response(
                status=int(response.status), headers=dict(response.headers.items()),
                body=response.read(), url=response.geturl(),
            )
    except urllib.error.HTTPError as exc:
        return Response(
            status=int(exc.code), headers=dict(exc.headers.items()), body=exc.read(),
            url=exc.geturl(),
        )


def _safe_asset_name(url: str, digest: str, content_type: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if not suffix or len(suffix) > 8:
        suffix = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(content_type.split(";", 1)[0].lower(), ".bin")
    return f"{digest}{suffix}"


def _image_dimensions(path: Path) -> tuple[int | None, int | None, str | None]:
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height), image.format
    except (OSError, ValueError):
        return None, None, None


def _write_bytes_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(payload)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def collect_product(
    product_id: str, *, output: Path, mode: str, access_token: str | None,
    get: Get = http_get,
) -> dict[str, Any]:
    collected_at = utc_now()
    product_dir = output / "products" / product_id
    if mode == "commerce":
        if not access_token:
            raise ValueError("COMMERCE_ACCESS_TOKEN_REQUIRED")
        source_url = COMMERCE_PRODUCT_URL.format(product_id=product_id)
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    else:
        source_url = PUBLIC_PRODUCT_URL.format(product_id=product_id)
        headers = {"Accept": "text/html,application/xhtml+xml", "User-Agent": "Mozilla/5.0"}

    response = get(source_url, headers)
    _write_bytes_once(product_dir / "source_response.bin", response.body)
    manifest: dict[str, Any] = {
        "product_id": product_id, "source_url": source_url,
        "final_url": response.url, "collected_at": collected_at,
        "http_method": "GET", "http_status": response.status,
        "content_type": response.headers.get("Content-Type", ""),
        "source_sha256": sha256_bytes(response.body), "mode": mode,
        "assets": [],
    }
    urls: set[str] = set()
    if 200 <= response.status < 300:
        decoded = response.body.decode("utf-8", errors="replace")
        if mode == "commerce":
            payload = json.loads(decoded)
            _write_text(product_dir / "source.json", json.dumps(payload, ensure_ascii=False, indent=2))
            fragments = _detail_html_fragments(payload)
            urls.update(_json_image_urls(payload))
            text_parts: list[str] = []
            for index, fragment in enumerate(fragments):
                _write_text(product_dir / f"detail_{index:03d}.html", fragment)
                text, fragment_urls = html_evidence(fragment, base_url=source_url)
                text_parts.append(text)
                urls.update(fragment_urls)
            _write_text(product_dir / "page_text.txt", "\n".join(text_parts))
        else:
            _write_text(product_dir / "page.html", decoded)
            text, urls = html_evidence(decoded, base_url=response.url)
            _write_text(product_dir / "page_text.txt", text)

    assets_dir = output / "assets"
    for asset_url in sorted(url for url in urls if url.startswith(("http://", "https://"))):
        item: dict[str, Any] = {"url": asset_url, "http_method": "GET"}
        asset_response = get(asset_url, {"Referer": source_url, "User-Agent": "Mozilla/5.0"})
        item["http_status"] = asset_response.status
        item["content_type"] = asset_response.headers.get("Content-Type", "")
        if 200 <= asset_response.status < 300 and asset_response.body:
            digest = sha256_bytes(asset_response.body)
            target = assets_dir / _safe_asset_name(
                asset_url, digest, item["content_type"],
            )
            _write_bytes_once(target, asset_response.body)
            width, height, image_format = _image_dimensions(target)
            item.update({
                "sha256": digest, "file": target.relative_to(output).as_posix(),
                "file_size": len(asset_response.body), "width": width, "height": height,
                "image_format": image_format,
            })
        manifest["assets"].append(item)
    _write_text(product_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def collect(
    *, catalog_path: Path, output: Path, mode: str,
    access_token_file: Path | None = None, get: Get = http_get,
    product_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    ids = list(product_ids or catalog_product_ids(catalog))
    token = None
    if access_token_file:
        token = access_token_file.read_text(encoding="utf-8").strip()
    output.mkdir(parents=True, exist_ok=True)
    run = {
        "started_at": utc_now(), "mode": mode, "catalog": str(catalog_path),
        "http_policy": "GET_ONLY", "product_count": len(ids), "products": [],
    }
    for product_id in ids:
        try:
            manifest = collect_product(
                product_id, output=output, mode=mode, access_token=token, get=get,
            )
            run["products"].append({
                "product_id": product_id, "http_status": manifest["http_status"],
                "asset_count": len(manifest["assets"]), "error": None,
            })
        except Exception as exc:  # one bad page must not hide the other 102 outcomes
            run["products"].append({
                "product_id": product_id, "http_status": None,
                "asset_count": 0, "error": f"{type(exc).__name__}: {exc}",
            })
    run["finished_at"] = utc_now()
    _write_text(output / "run_manifest.json", json.dumps(run, ensure_ascii=False, indent=2))
    return run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("data/model_data_with_color.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("public", "commerce"), default="public")
    parser.add_argument(
        "--access-token-file", type=Path,
        help="Already-issued token file. The collector never performs OAuth POST.",
    )
    parser.add_argument("--product-id", action="append", dest="product_ids")
    args = parser.parse_args()
    result = collect(
        catalog_path=args.catalog, output=args.output, mode=args.mode,
        access_token_file=args.access_token_file, product_ids=args.product_ids,
    )
    print(json.dumps({
        "product_count": result["product_count"],
        "success": sum(1 for item in result["products"] if item["http_status"] == 200),
        "failed": sum(1 for item in result["products"] if item["http_status"] != 200),
        "http_policy": result["http_policy"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
