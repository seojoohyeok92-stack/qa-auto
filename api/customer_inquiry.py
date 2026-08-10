from datetime import date, datetime, timedelta

from config import CUSTOMER_INQUIRY_URL
from api.naver_read_client import request_json


def format_search_date(value: date) -> str:
    """
    고객 문의 API에서 사용하는 날짜 형식으로 변환합니다.

    예:
    2026-07-24
    """

    return value.strftime("%Y-%m-%d")


def get_customer_inquiries(
    access_token: str,
    days: int = 30,
    page: int = 1,
    size: int = 100,
    answered: bool | None = None,
    from_datetime: datetime | None = None,
    to_datetime: datetime | None = None,
    timeout: tuple[float, float] = (5.0, 20.0),
    max_retries: int = 2,
    backoff_seconds: float = 0.5,
    session=None,
    sleeper=None,
) -> dict:
    """
    고객 문의 목록을 조회합니다.

    answered:
    - None: 전체 문의
    - False: 답변대기 문의
    - True: 답변완료 문의
    """

    if days < 1:
        raise ValueError("days는 1 이상이어야 합니다.")

    if days > 364:
        raise ValueError(
            "스마트스토어 고객 문의는 최대 365일 범위까지만 조회할 수 있습니다."
        )

    if page < 1:
        raise ValueError("page는 1 이상이어야 합니다.")

    if not 10 <= size <= 200:
        raise ValueError("size는 10부터 200 사이여야 합니다.")

    if (from_datetime and from_datetime.tzinfo is None) or (
        to_datetime and to_datetime.tzinfo is None
    ):
        raise ValueError(
            "from_datetime과 to_datetime은 timezone-aware datetime이어야 합니다."
        )
    if from_datetime and to_datetime and from_datetime >= to_datetime:
        raise ValueError("from_datetime은 to_datetime보다 이전이어야 합니다.")
    end_search_date = (
        to_datetime.astimezone().date() if to_datetime else date.today()
    )
    start_search_date = (
        from_datetime.astimezone().date()
        if from_datetime
        else end_search_date - timedelta(days=days)
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    params = {
        "page": page,
        "size": size,
        "startSearchDate": format_search_date(start_search_date),
        "endSearchDate": format_search_date(end_search_date),
    }

    if answered is not None:
        params["answered"] = str(answered).lower()

    request_kwargs = {}
    if session is not None:
        request_kwargs["session"] = session
    if sleeper is not None:
        request_kwargs["sleeper"] = sleeper
    result = request_json(
        "GET",
        CUSTOMER_INQUIRY_URL,
        headers=headers,
        params=params,
        timeout=timeout,
        max_retries=max_retries,
        backoff_seconds=backoff_seconds,
        **request_kwargs,
    )

    result["_request"] = {
        "startSearchDate": params["startSearchDate"],
        "endSearchDate": params["endSearchDate"],
        "page": page,
        "size": size,
        "answered": answered,
    }

    return result
