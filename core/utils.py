import re
from typing import Any


# 키워드마다 중요도가 다르므로 점수를 다르게 설정합니다.
DELIVERY_KEYWORD_SCORES = {
    # 배송 관련 핵심 단어
    "배송": 35,
    "배송일": 45,
    "배송일정": 50,
    "배송예정": 50,
    "배송예정일": 55,
    "도착": 30,
    "도착예정": 45,
    "출고": 30,
    "출고일": 40,
    "택배": 30,
    "송장": 35,

    # 설치 관련 핵심 단어
    "설치": 40,
    "설치일": 50,
    "설치일정": 55,
    "설치예정": 55,
    "설치예정일": 60,
    "벽걸이": 20,
    "스탠드": 15,

    # 방문 및 기사 관련 단어
    "기사": 25,
    "기사님": 30,
    "방문": 25,
    "방문일": 40,
    "연락": 15,

    # 일정 질문에 자주 쓰이는 단어
    "언제": 20,
    "일정": 20,
    "예정": 15,
    "날짜": 15,
}


# 이 점수 이상이면 배송·설치 관련 문의로 분류합니다.
DELIVERY_SCORE_THRESHOLD = 35
DELIVERY_SCHEDULE_PATTERNS = (
    "몇시에 도착",
    "몇 시에 도착",
    "언제 와",
    "언제와",
    "언제 오",
    "언제오",
    "오늘 오",
    "내일 오",
    "도착 시간",
    "도착시간",
    "설치 날짜",
    "설치날짜",
    "설치 예정일",
    "설치예정일",
    "방문 시간",
    "방문시간",
    "기사님 언제",
    "기사님 몇 시",
    "기사님 몇시",
)


def normalize_text(content: str | None) -> str:
    """
    키워드 비교를 위해 문의 내용을 정리합니다.

    공백과 줄바꿈을 제거하고 소문자로 변환합니다.
    """

    if not content:
        return ""

    return re.sub(
        r"\s+",
        "",
        str(content),
    ).lower()


def calculate_delivery_score(
    content: str | None,
) -> tuple[int, list[str]]:
    """
    문의 내용에 포함된 배송·설치 키워드 점수를 계산합니다.

    반환값:
    - 총점
    - 발견된 키워드 목록
    """

    normalized_content = normalize_text(content)

    if not normalized_content:
        return 0, []

    for pattern in DELIVERY_SCHEDULE_PATTERNS:
        if normalize_text(pattern) in normalized_content:
            return DELIVERY_SCORE_THRESHOLD, [pattern]

    total_score = 0
    matched_keywords: list[str] = []

    # 긴 단어를 먼저 검사합니다.
    # 예: '설치예정일'이 있으면 '설치', '예정', '일정'을
    # 모두 중복 계산하는 일을 줄이기 위함입니다.
    sorted_keywords = sorted(
        DELIVERY_KEYWORD_SCORES,
        key=len,
        reverse=True,
    )

    remaining_content = normalized_content

    for keyword in sorted_keywords:
        normalized_keyword = normalize_text(keyword)

        if normalized_keyword in remaining_content:
            total_score += DELIVERY_KEYWORD_SCORES[keyword]
            matched_keywords.append(keyword)

            # 이미 점수로 계산한 긴 단어를 지워서
            # 포함 관계에 있는 짧은 단어의 중복 점수를 줄입니다.
            remaining_content = remaining_content.replace(
                normalized_keyword,
                "",
            )

    return total_score, matched_keywords


def is_delivery_inquiry(content: str | None) -> bool:
    """
    문의가 배송 또는 설치 관련 문의인지 판별합니다.
    """

    score, _ = calculate_delivery_score(content)

    return score >= DELIVERY_SCORE_THRESHOLD


def extract_number_candidates(
    content: str | None,
) -> list[str]:
    """
    문의 내용에서 주문번호 또는 상품주문번호 후보를 찾습니다.

    숫자 사이의 공백이나 하이픈은 제거합니다.

    예:
    2026-0706-7402-2711
    2026 0706 7402 2711

    위와 같은 입력도 다음 번호 후보로 추출합니다.

    2026070674022711
    """

    if not content:
        return []

    text = str(content)

    # 숫자와 숫자 사이에 있는 공백이나 하이픈만 제거합니다.
    compact_content = re.sub(
        r"(?<=\d)[\s-]+(?=\d)",
        "",
        text,
    )

    # 네이버 주문번호와 상품주문번호는 일반적으로 긴 숫자이므로
    # 12자리부터 20자리까지의 연속 숫자를 후보로 추출합니다.
    candidates = re.findall(
        r"(?<!\d)\d{12,20}(?!\d)",
        compact_content,
    )

    # 동일한 번호가 여러 번 있어도 한 번만 반환합니다.
    return list(dict.fromkeys(candidates))


def classify_inquiry(
    content: str | None,
) -> dict[str, Any]:
    """
    문의 내용을 분석해 작업 큐 분류에 필요한 정보를 반환합니다.

    반환 예:

    {
        "is_delivery": True,
        "score": 75,
        "matched_keywords": ["설치일정", "언제"],
        "number_candidates": ["2026070674022711"],
        "queue_status": "ORDER_LOOKUP_READY"
    }
    """

    score, matched_keywords = calculate_delivery_score(content)
    number_candidates = extract_number_candidates(content)

    is_delivery = score >= DELIVERY_SCORE_THRESHOLD

    if not is_delivery:
        queue_status = "GENERAL_INQUIRY"

    elif number_candidates:
        queue_status = "ORDER_LOOKUP_READY"

    else:
        queue_status = "CUSTOMER_CONFIRMATION_REQUIRED"

    return {
        "is_delivery": is_delivery,
        "score": score,
        "matched_keywords": matched_keywords,
        "number_candidates": number_candidates,
        "queue_status": queue_status,
    }


def get_queue_label(queue_status: str) -> str:
    """
    내부 작업 큐 상태를 화면 표시용 한글로 변환합니다.
    """

    labels = {
        "AUTO_PROCESSABLE": "🟢 자동 처리 가능",
        "ORDER_LOOKUP_READY": "🔵 주문 조회 대기",
        "CUSTOMER_CONFIRMATION_REQUIRED": "🟡 고객 확인 필요",
        "ORDER_LOOKUP_FAILED": "🟡 주문 확인 필요",
        "GENERAL_INQUIRY": "⚪ 일반 문의",
    }

    return labels.get(
        queue_status,
        f"알 수 없는 상태: {queue_status}",
    )


def create_order_request_message() -> str:
    """
    배송·설치 문의에 주문번호가 없을 때 사용할 안내문을 만듭니다.
    """

    return (
        "안녕하세요.\n\n"
        "정확한 배송 및 설치 일정 확인을 위해 "
        "네이버 주문내역에서 해당 주문을 선택한 뒤 "
        "'문의하기'를 통해 다시 문의해 주세요.\n\n"
        "상품 문의로 남기시는 경우에는 개인정보 보호를 위해 "
        "반드시 비밀글로 작성하시고, 주문번호 또는 "
        "상품주문번호를 함께 남겨 주세요.\n\n"
        "확인 후 빠르게 안내드리겠습니다."
    )
