from ui.dps_presenter import build_dps_display


def test_dps_presenter_exposes_integrated_status_fields() -> None:
    display = build_dps_display(
        lookup_required=True,
        order_id="ORDER-1",
        latest_row={
            "lookup_status": "SUCCESS",
            "normalized_result_json": {
                "lookup_status": "SUCCESS",
                "cache_used": True,
                "queried_at": "2026-07-29T14:30:00+09:00",
                "elapsed_seconds": 53.2,
                "delivery_status": "배송 준비 중",
                "installation_status": "설치 예정",
                "installation_date": "2026-08-03",
                "sales_number": "SALE-1",
            },
        },
    )
    assert display["status_label"] == "조회 성공"
    assert display["cache_used"] is True
    assert display["installation_date"] == "2026-08-03"
    assert display["sales_number"] == "SALE-1"
