from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.collect_product_knowledge_sources import (
    Response,
    catalog_product_ids,
    collect_product,
    html_evidence,
)


def test_catalog_product_ids_collects_all_integrated_listing_references():
    payload = {
        "PRODUCT_KNOWLEDGE": {
            "model_facts": [
                {"source_product_ids": ["20", "10"], "product_id": "30"},
            ],
            "listing_facts": [{"applies_to_product_id": "40"}],
            "excluded_records": [{"source_product_id": "not-an-id"}],
        }
    }
    assert catalog_product_ids(payload) == ["10", "20", "30", "40"]


def test_html_discovery_reads_lazy_srcset_iframe_and_text():
    page = """
    <div>제품 사양</div>
    <img src="/a.jpg" data-src="https://cdn.example/b.png">
    <source srcset="/c.webp 1x, /c2.webp 2x">
    <iframe src="/details.html"></iframe>
    """
    text, urls = html_evidence(page, base_url="https://shop.example/p/1")
    assert "제품 사양" in text
    assert urls == {
        "https://shop.example/a.jpg", "https://cdn.example/b.png",
        "https://shop.example/c.webp", "https://shop.example/c2.webp",
        "https://shop.example/details.html",
    }


def test_public_collection_uses_get_only_and_preserves_dedup_mapping(tmp_path: Path):
    calls: list[tuple[str, dict[str, str]]] = []
    image = b"not-a-real-image-but-stable-evidence"

    def get(url: str, headers: dict[str, str]) -> Response:
        calls.append((url, dict(headers)))
        if "/products/" in url:
            body = b'<html><body>fact<img src="https://cdn.example/a.jpg"><img data-original="https://cdn.example/a.jpg"></body></html>'
            return Response(200, {"Content-Type": "text/html"}, body, url)
        return Response(200, {"Content-Type": "image/jpeg"}, image, url)

    manifest = collect_product(
        "123", output=tmp_path, mode="public", access_token=None, get=get,
    )
    assert len(manifest["assets"]) == 1
    assert all(item[0].startswith("https://") for item in calls)
    assert manifest["http_method"] == "GET"
    assert manifest["assets"][0]["http_method"] == "GET"
    assert (tmp_path / "products" / "123" / "page.html").exists()
    assert (tmp_path / manifest["assets"][0]["file"]).read_bytes() == image


def test_commerce_collection_requires_preissued_token_and_never_authenticates(tmp_path: Path):
    with pytest.raises(ValueError, match="COMMERCE_ACCESS_TOKEN_REQUIRED"):
        collect_product(
            "123", output=tmp_path, mode="commerce", access_token=None,
            get=lambda *_: pytest.fail("network must not be called without token"),
        )


def test_commerce_response_preserves_json_detail_html_text_and_assets(tmp_path: Path):
    calls: list[str] = []
    payload = {
        "originProduct": {
            "detailContent": '<p>VESA 200 x 200</p><img data-lazy-src="https://cdn.example/spec.png">',
            "images": {"representativeImage": {"url": "https://cdn.example/hero.png"}},
        }
    }

    def get(url: str, headers: dict[str, str]) -> Response:
        calls.append(url)
        if "channel-products" in url:
            assert headers["Authorization"] == "Bearer issued-token"
            return Response(
                200, {"Content-Type": "application/json"},
                json.dumps(payload).encode(), url,
            )
        return Response(200, {"Content-Type": "image/png"}, b"asset", url)

    manifest = collect_product(
        "123", output=tmp_path, mode="commerce", access_token="issued-token", get=get,
    )
    assert len(manifest["assets"]) == 2
    assert "VESA 200 x 200" in (tmp_path / "products" / "123" / "page_text.txt").read_text(encoding="utf-8")
    assert (tmp_path / "products" / "123" / "source.json").exists()
    assert not any("oauth" in url.lower() for url in calls)
