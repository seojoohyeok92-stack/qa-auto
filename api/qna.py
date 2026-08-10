from datetime import datetime, timedelta, timezone

from config import QNA_URL
from api.naver_read_client import request_json


def format_api_datetime(value: datetime) -> str:
    """
    네이버 문서 예시와 동일한 UTC 형식으로 변환합니다.

    예:
    2026-07-24T04:00:00.000Z
    """

    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def get_qna_list(
    access_token: str,
    days: int = 30,
    page: int = 1,
    size: int = 100,
    answered: bool | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    timeout: tuple[float, float] = (5.0, 20.0),
    max_retries: int = 2,
    backoff_seconds: float = 0.5,
    session=None,
    sleeper=None,
) -> dict:
    """지정한 기간의 상품 문의 목록을 조회합니다."""

    if days < 1:
        raise ValueError("days는 1 이상이어야 합니다.")

    if page < 1:
        raise ValueError("page는 1 이상이어야 합니다.")

    if not 1 <= size <= 100:
        raise ValueError("size는 1부터 100 사이여야 합니다.")

    to_date = to_date or datetime.now(timezone.utc)
    from_date = from_date or to_date - timedelta(days=days)
    if from_date.tzinfo is None or to_date.tzinfo is None:
        raise ValueError("from_date와 to_date는 timezone-aware datetime이어야 합니다.")
    if from_date >= to_date:
        raise ValueError("from_date는 to_date보다 이전이어야 합니다.")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    params = {
        "fromDate": format_api_datetime(from_date),
        "toDate": format_api_datetime(to_date),
        "page": page,
        "size": size,
    }

    # answered는 선택값이므로 전달받은 경우에만 추가합니다.
    if answered is not None:
        params["answered"] = str(answered).lower()

    request_kwargs = {}
    if session is not None:
        request_kwargs["session"] = session
    if sleeper is not None:
        request_kwargs["sleeper"] = sleeper
    result = request_json(
        "GET",
        QNA_URL,
        headers=headers,
        params=params,
        timeout=timeout,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        **request_kwargs,
    )

    # 프로그램에서 조회 조건을 확인할 수 있도록 추가합니다.
    result["_request"] = {
        "fromDate": params["fromDate"],
        "toDate": params["toDate"],
        "page": page,
        "size": size,
        "answered": answered,
    }

    return result
