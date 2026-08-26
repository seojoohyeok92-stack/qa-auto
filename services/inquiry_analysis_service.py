from __future__ import annotations

import dataclasses
import re

from answer.inquiry_analysis import (
    AnswerStrategy,
    InquiryAnalysis,
    InquiryType,
    OrderIdStatus,
)
from answer.models import AnswerRequest
from answer.text_utils import (
    CURRENT_DELIVERY_SCHEDULE_QUERY,
    CURRENT_INSTALLATION_SCHEDULE_QUERY,
    is_general_delivery_policy_question,
    is_operational_schedule_request,
    is_package_contents_question,
    is_product_concept_question,
    is_weekend_delivery_policy_question,
    split_subquestions,
)


GENERAL_ORDER_ID_PATTERN = re.compile(r"(?<!\d)\d{16}(?!\d)")
ANY_LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)\d{10,24}(?!\d)")
VALID_GENERAL_ORDER_ID_PATTERN = re.compile(r"\d{16}")

DELIVERY_SCHEDULE_WORDS = (
    "도착예정",
    "도착 예정",
    "도착예정일",
    "도착 예정일",
    "배송일",
    "배송예정일",
    "배송 일정",
    "배송일정",
    "배송 언제",
    "배송 언제 오",
    "배송 날짜",
    "배송날짜",
    "배송조회",
    "배송 조회",
    "지정일 배송",
    "이번 주에 받",
    "언제 받을",
    "언제쯤 받을",
    "언제쯤받을",
    "받을 수 있을",
    "받을수있을",
    "언제 도착",
    "주문했는데 언제",
    "언제 오",
    "설치일",
    "설치 일정",
    "설치 언제",
    "방문 일정",
    "기사 방문",
    "도착 시간",
    "도착시간",
    "방문 시간",
    "방문시간",
    "몇 시에 도착",
    "몇시에 도착",
    "몇 시쯤 방문",
    "몇시쯤 방문",
    "기사님 언제",
    "기사님 몇 시",
    "기사님 몇시",
    "설치 날짜",
    "설치날짜",
    "설치 예정일",
    "설치예정일",
    "설치 언제",
    "언제 설치",
    "오늘 오",
    "내일 오",
    "일정 확인",
    # Complaints about the schedule slipping. The customer is asking what the
    # schedule now is, not asking us to change it -- so this is a lookup and
    # must keep its order/DPS requirement. An explicit request ("미뤄주세요")
    # is matched earlier by _is_schedule_change_request and still wins.
    "미뤄지는데",
    "미뤄졌는데",
    "지연되는데",
    "지연됐는데",
    "늦어지는데",
    "늦어졌는데",
)
NOTIFICATION_POLICY_WORDS = (
    "알림톡",
    "안내 문자",
    "문자 오",
    "전날 연락",
    "연락이 오",
    "연락 오",
)
ORDER_STATUS_WORDS = (
    "주문 상태",
    "주문 확인",
    "주문됐",
    "결제 확인",
    "출고 상태",
)
INSTALLATION_GENERAL_WORDS = (
    "설치는",
    "설치 방법",
    "설치방법",
    "설치가 어떻게",
    "어떻게 설치",
    "설치해 주",
    "설치해주",
    "벽걸이",
    "스탠드",
    "타공",
    "설치비",
    "설치 조건",
    "설치 가능",
    "브라켓",
    "자가설치",
    "자가 설치",
    "기사님이 설치",
    "기사 설치",
    "방문설치",
    "방문 설치",
)
PRODUCT_GENERAL_WORDS = (
    "서비스센터",
    "서비스 센터",
    "AS는",
    "AS를",
    "AS가",
    "AS 는",
    "수리",
    "보증기간",
    "보증 기간",
    "무상수리",
    "무상 수리",
    "튼튼",
    "내구",
    "사용할 수",
    "사용 가능",
    "쓸 수",
    "사양",
    "기능",
    "구성품",
    "호환",
    "크기",
    "무게",
    "색상",
    "모델",
    "지원하나요",
    "가능한가요",
    "HDMI",
    "hdmi",
    "패널",
    "LED",
    "QLED",
    "OLED",
    "인치",
    "A/S",
    "a/s",
    "에이에스",
    "네이버포인트",
    "네이버 포인트",
    "무빙스타일",
)
# Courtesy wording that carries no question. Listed as *complete* expressions
# rather than stems, because a stem swallows real words -- bare "감사" would
# match 삼성 감사제 and bare "네" would match 네이버. Matched against the
# whitespace-stripped text, so spaced variants are covered without listing
# them. This is the only literal list here; what decides the verdict is
# whether anything is *left* after removing them, not the list itself.
COURTESY_ONLY_EXPRESSIONS = (
    # greeting
    "안녕하세요", "안녕하십니까", "안녕하세여", "안녕히계세요", "안녕히가세요",
    "반갑습니다", "반가워요", "안녕", "하이", "헬로",
    # thanks
    "감사합니다", "감사드립니다", "감사해요", "감사요", "고맙습니다", "고마워요",
    "땡큐", "thankyou", "thanks", "hello", "hi",
    # closing / acknowledgement
    "수고하세요", "수고하십시오", "수고많으세요", "수고하셨습니다",
    "좋은하루되세요", "좋은하루보내세요", "잘부탁드립니다", "잘부탁합니다",
    "알겠습니다", "확인했습니다", "넵", "네네",
)
# Longest first so "안녕하세요" is consumed before the shorter "안녕".
_COURTESY_RE = re.compile(
    "|".join(
        re.escape(item)
        for item in sorted(COURTESY_ONLY_EXPRESSIONS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
# A syllable, digit or letter is the smallest unit that can carry meaning.
# Korean jamo alone (ㅁㄴㅇㄹ, ㅋㅋㅋ) and punctuation are not.
_MEANINGFUL_CHAR_RE = re.compile(r"[가-힣0-9A-Za-z]")


def _has_no_substantive_question(question: str) -> bool:
    """True only when the message asks nothing at all.

    Two structural findings, both of which have to be proven -- an inquiry is
    treated as substantive unless shown otherwise, so an unusual but real
    question is never mistaken for chatter:

    1. nothing that can carry meaning is present (loose jamo, emoticons,
       punctuation), or
    2. removing courtesy wording leaves nothing that can carry meaning.

    Length is deliberately not consulted: "설치되나요?" is short and real.
    """

    text = re.sub(r"\s+", "", str(question or ""))
    if not text:
        # An empty inquiry is a separate finding (EMPTY_QUESTION) and keeps
        # its own handling below.
        return False
    if not _MEANINGFUL_CHAR_RE.search(text):
        return True
    return not _MEANINGFUL_CHAR_RE.search(_COURTESY_RE.sub("", text))


CANCEL_WORDS = ("취소", "반품", "교환", "환불")
SCHEDULE_CHANGE_WORDS = (
    "설치일 변경",
    "설치 일정 변경",
    "배송일 변경",
    "배송 일정 변경",
    "변경해 주세요",
    "변경해주세요",
    "빨리 받을 수 있나요",
    "더 빨리 받을 수 있나요",
    "일찍 받을 수 있나요",
)
# A schedule *change* is a request to perform work on the order, not a
# question about it. Matched as target + action together so that asking
# "설치 예정일이 언제인가요?" stays an ordinary schedule lookup, and an
# unrelated "색상 변경" or "주소 변경" is not pulled in either.
SCHEDULE_CHANGE_TARGET_WORDS = (
    "설치일",
    "설치 일정",
    "설치일정",
    "설치 날짜",
    "설치날짜",
    "설치 예정일",
    "설치예정일",
    "설치 요청일",
    "설치요청일",
    "배송일",
    "배송 일정",
    "배송일정",
    "배송 날짜",
    "배송날짜",
    "배송 예정일",
    "배송예정일",
    "배송 요청일",
    "배송요청일",
    "방문일",
    "방문 시간",
    "방문시간",
    "방문 일정",
    "기사 일정",
    "기사님 일정",
    "기사 방문",
    "수령일",
)
SCHEDULE_CHANGE_ACTION_WORDS = (
    "변경",
    "바꾸",
    "바꿔",
    "당겨",
    "당길",
    "미루",
    "미뤄",
    "연기",
    "앞당",
    "늦춰",
    "늦추",
    "옮겨",
    "옮기",
    "조정",
)


# Card/payment benefits change with each promotion period, and nothing in
# this system holds a verified current answer for them: there is no card or
# promotion catalog, and a past Learning answer only proves what was true
# then. A draft is still generated so staff have something to work from, but
# it must not be published without a person confirming today's terms.
# Deliberately keyed on the *benefit*, not on "카드" -- "카드 결제 가능한가요?"
# is an ordinary payment-method question and stays auto-answerable.
PAYMENT_BENEFIT_WORDS = (
    "카드혜택",
    "카드 혜택",
    "카드할인",
    "카드 할인",
    "청구할인",
    "제휴할인",
    "제휴 할인",
    "할인혜택",
    "할인 혜택",
    "무이자",
    "캐시백",
    "적립",
    "포인트",
    "프로모션",
)


# Whether a specific accessory/bracket/model actually fits is a fact the
# customer buys on, and this system only holds verified compatibility for the
# catalog's own accessories (see AnswerEngine._stand_or_battery, backed by
# stand_rules/battery_rules). A question about the customer's *own* hardware
# has no such source, so the answer may be drafted but not published without
# a person. Requires an object *and* a fit relation together, so that plain
# feature questions ("인터넷 연결해서 쓸 수 있나요?", "HDMI 연결 가능한가요?")
# are not swept in.
COMPATIBILITY_OBJECT_WORDS = (
    "브라켓",
    "브래킷",
    "거치대",
    "스탠드",
    "월마운트",
    "벽걸이",
    "배터리",
    "액세서리",
    "악세서리",
    "받침대",
)
COMPATIBILITY_RELATION_WORDS = (
    "호환",
    "맞나요",
    "맞을까",
    "맞는지",
    "장착",
    "부착",
    "달 수",
    "달수",
    "쓸 수",
    "사용할 수",
    "같이 사용",
    "함께 사용",
    "이용 가능",
)


def _is_compatibility_question(question: str) -> bool:
    # Removing and reattaching the product's own basic stand is an assembly
    # fact, not a claim that a customer's separate accessory will fit.  It may
    # be answered by an exact verified Product Fact or exact-model Approved
    # Learning; actual bracket/third-party fit questions remain compatibility.
    basic_stand_assembly = any(
        word in question for word in ("스탠드", "받침대", "다리")
    ) and any(
        word in question
        for word in ("탈부착", "탈착", "분리", "떼었다", "떼고", "다시 장착")
    )
    if basic_stand_assembly:
        return False
    return any(
        word in question for word in COMPATIBILITY_OBJECT_WORDS
    ) and any(word in question for word in COMPATIBILITY_RELATION_WORDS)


def _is_unverifiable_payment_benefit(question: str) -> bool:
    return any(word in question for word in PAYMENT_BENEFIT_WORDS)


def _is_schedule_status_lookup(question: str) -> bool:
    """"It was postponed; when is it now?" -- a report plus a lookup.

    This reports a change that already happened and asks what the schedule is
    now.  It is a lookup, not a request that Q&A Auto perform another change.
    Passive/result wording *and* an explicit lookup marker are both required,
    so "please postpone it" still takes the staff path.
    """

    passive_delay = any(
        word in question
        for word in (
            "미뤄지", "미뤄졌", "늦춰지", "늦춰졌", "연기되", "변경되",
            "변경됐", "바뀌었", "바뀌었는데",
        )
    )
    lookup_request = any(
        word in question
        for word in ("언제", "지금", "현재", "되나요", "오나요", "오시나요")
    )
    return passive_delay and lookup_request


def _subquestion_records(
    pairs: list[tuple[str, InquiryAnalysis]],
) -> tuple[dict[str, object], ...]:
    """One record per atomic question, holding that question's own verdict.

    Deliberately the same fields the aggregate carries, so "why is the whole
    inquiry held?" and "which part holds it?" are answered from the same
    vocabulary. Nothing here decides anything: the aggregate flags above are
    what the safety gates read, and this only records how they were reached.
    """

    records: list[dict[str, object]] = []
    for text, item in pairs:
        records.append({
            "question": text,
            "inquiry_subtype": item.inquiry_subtype,
            "detected_intent": item.detected_intent,
            "answer_strategy": item.answer_strategy.value,
            "requires_order_lookup": item.requires_order_lookup,
            "requires_dps_lookup": item.requires_dps_lookup,
            "requires_order_id": item.requires_order_id,
            "manual_review_required": item.manual_review_required,
            "can_generate_answer": item.can_generate_answer,
            "confidence": item.confidence,
        })
    return tuple(records)


def _is_schedule_change_request(question: str) -> bool:
    if _is_schedule_status_lookup(question):
        return False
    if any(word in question for word in SCHEDULE_CHANGE_WORDS):
        return True
    # "이번 주말에 설치해주세요." asks staff to schedule the visit, but it
    # moves no existing date, so it carries neither a change verb nor a
    # schedule noun and was classified GENERAL_INSTALLATION_GUIDANCE -- an
    # ordinary information question, auto-answerable. Naming a time *and*
    # instructing us is the same operational request as moving a date, and
    # belongs on the same staff path. "주말 설치 가능한가요?" names a time but
    # asks whether we do it at all, and stays an ordinary policy question.
    if is_operational_schedule_request(question):
        return True
    action = any(word in question for word in SCHEDULE_CHANGE_ACTION_WORDS)
    if not action:
        return False
    if any(
        word in question for word in SCHEDULE_CHANGE_TARGET_WORDS
    ):
        return True
    # Customers often omit the word "date" while still making an operational
    # request ("installation earlier", "adjust the driver's schedule").
    # Action wording remains mandatory so ordinary schedule lookups are not
    # captured.  A temporal destination covers the terse follow-up
    # "move it to this week" after Naver has supplied the inquiry context.
    return any(
        word in question
        for word in ("배송", "설치", "기사", "방문", "일정")
    ) or any(
        word in question
        for word in ("이번 주", "다음 주", "이번주", "다음주", "내일", "하루")
    )
# Expressions that require a person to look at the actual case before the
# customer gets any reply. Matched as substrings against the whitespace
# normalized question, so each entry is chosen to be unambiguous on its own:
# bare "보상" is deliberately absent because "보상판매" (trade-in) is an
# ordinary sales question, and no return/refund word is listed here because
# asking how returns work is answerable from fixed policy -- only an
# individual refund decision is escalated, which CANCEL_WORDS already covers.
HIGH_RISK_WORDS = (
    # Legal / injury exposure
    "피해보상",
    "법적",
    "소송",
    "분쟁",
    "화재",
    "감전",
    "부상",
    # Physical damage: the real condition of the item has to be verified
    # before promising anything, so it can never be answered automatically.
    "깨져",
    "깨진",
    "깨졌",
    "파손",
    "찌그러",
    "훼손",
    "부서",
    # Service complaints: staff have to handle dissatisfaction directly.
    "불친절",
    "불만",
    "엉망",
    "항의",
    # Compensation / liability: the company's responsibility is never a
    # decision an automatic answer may make.
    "보상해",
    "보상 해",
    "배상",
    "책임",
    "과실",
)
# Strong markers: the customer names the purchase itself, so the question is
# about what would happen if they bought -- an explicit "when" does not change
# that ("오늘 주문하면 언제 받을 수 있나요?").
PRE_PURCHASE_DELIVERY_WORDS = (
    "오늘주문하면",
    "지금주문하면",
    "주문하면",
    # Customers write the same pre-purchase condition several ways, and only
    # "…하면" was listed, so "오늘 주문시 언제 배송되나요?" was read as an
    # existing order and answered by asking for an order number the customer
    # does not have yet.
    "주문시",
    "주문하시면",
    "구매시",
    "구매하시면",
    "주문할예정",
    "구매하려",
    "구매예정",
    "구매하면",
    "주문할예정",
    "구매하려",
    "구매예정",
)
# Weaker markers: they describe *feasibility* ("can I have it by Saturday?",
# "how long does delivery take?") rather than naming a purchase. On their own
# they cannot outrank an explicit "when", because "언제 받을 수 있나요?" from
# someone who has already ordered is a schedule lookup, not a policy question
# -- that reading is what stopped it requiring an order number.
PRE_PURCHASE_FEASIBILITY_WORDS = (
    "배송얼마나걸",
    "배송기간",
    "받을수있",
    "도착가능",
    "배송가능",
    "설치가능",
)
EXPLICIT_WHEN = re.compile(r"언제|며칠|몇일")
POST_PURCHASE_DELIVERY_WORDS = (
    "주문했",
    "주문완료",
    "주문한지",
    "구매했",
    "구매완료",
    "결제했",
    "결제완료",
    "배송조회",
    "배송상태",
    "발송대기",
    "배송준비",
    "출고대기",
    "송장",
    "운송장",
)


class InquiryAnalysisService:
    """Deterministic first-pass analysis used before any provider call."""

    @staticmethod
    def _intent(question: str) -> str | None:
        compact = re.sub(r"[\s\W_]+", "", question).lower()
        # Existing-order delivery coordination is an evidence-retrieval intent,
        # not an information-insufficient terminal state.  Keep method/policy
        # questions out by requiring a contact/scheduling signal as well as a
        # current-progress signal.
        agent_coordination = "기사" in compact and any(
            token in compact
            for token in (
                "연락", "통화", "약속", "방문일정", "방문시간",
                "방문날짜", "날짜시간", "일정안", "일정이안",
            )
        )
        current_progress = any(
            token in compact
            for token in (
                "배송중", "배송상태", "진행중", "연락이없",
                "연락없", "못정", "안잡", "기다려", "기다리",
                "언제연락", "언제오",
            )
        )
        if agent_coordination and current_progress and any(
            token in compact for token in ("배송", "설치", "방문", "기사")
        ):
            return (
                "INSTALLATION_DATE"
                if "설치" in compact and "배송" not in compact
                else "DELIVERY_STATUS"
            )
        # Customer-facing schedule language is a deterministic business rule,
        # not a score.  Keep this block ahead of legacy keyword scoring and
        # provider inference so newly-synced inquiries cannot inherit a stale
        # GENERAL classification.
        # "When does the *notice* arrive?" -- about the message, not the
        # shipment. Checked before the schedule shapes below so the two are
        # never merged: both contain 언제, and one contains 발송
        # ("알림톡은 언제 발송되나요?").
        #
        # "안내문자" is listed beside "문자안내" because customers write it
        # both ways and only one spelling was here, so "배송 안내 문자는 언제
        # 오나요?" was read as a shipment question.
        notification_policy = any(
            token in compact
            for token in (
                "배송알림톡",
                "알림톡은언제",
                "알림톡언제",
                "문자안내",
                "안내문자",
                "안내문자는언제",
                "일반배송정책",
                "이벤트배송정책",
            )
        )
        if notification_policy:
            return "NOTIFICATION_POLICY"
        if any(
            token in compact
            for token in (
                "설치기사님몇시",
                "기사님몇시",
                "기사방문시간",
                "방문시간",
                "몇시설치",
            )
        ):
            return "INSTALLATION_TIME"
        if any(
            token in compact
            for token in (
                "몇시에도착",
                "몇시도착",
                "몇시에와",
                "배송시간",
            )
        ):
            return "DELIVERY_TIME"
        if any(
            token in compact
            for token in (
                "설치예정",
                "설치일",
                "설치날짜",
                "설치언제",
                "설치기사님언제",
                "설치기사님은언제",
                "기사님언제",
                "기사님은언제",
            )
        ) or CURRENT_INSTALLATION_SCHEDULE_QUERY.search(compact):
            return "INSTALLATION_DATE"
        if any(
            token in compact
            for token in (
                "배송되나요",
                "배송날짜",
                "배송언제",
                "언제배송",
                "도착",
                "언제와요",
                "언제오나요",
                "언제쯤",
                "받을수",
                "언제받을",
                "주문한지",
                "한달",
                "한달이네요",
                "출고",
                "배송지연",
            )
        ) or CURRENT_DELIVERY_SCHEDULE_QUERY.search(compact):
            return "DELIVERY_DATE"
        if any(
            token in compact
            for token in (
                "자가설치",
                "혼자설치",
                "직접설치",
                "기사님이설치",
                "기사님설치",
                "기사설치",
                "설치해주시",
                "설치해주나",
                "방문설치",
            )
        ) or (
            "기사" in compact
            and "설치" in compact
            and any(token in compact for token in ("해주시", "해주나", "가능", "벽걸이"))
            and not current_progress
        ):
            return "INSTALLATION_METHOD"
        notification = any(
            re.sub(r"\s+", "", word) in compact
            for word in NOTIFICATION_POLICY_WORDS
        )
        explicit_schedule = any(
            token in compact
            for token in (
                "배송일", "배송예정일", "배송일정", "설치일",
                "배송날짜",
                "설치날짜", "설치예정일", "설치일정", "도착예정",
                "도착날짜", "도착시간", "방문시간", "몇시에도착",
                "몇시에오", "몇시쯤방문", "언제도착", "언제받",
                "언제쯤받", "받을수있", "언제올까",
                "언제설치", "배송언제", "설치언제", "오늘오",
                "내일오", "기사님언제", "기사님몇시", "일정확인",
            )
        )
        if notification and not explicit_schedule:
            return "NOTIFICATION_POLICY"
        if any(
            token in compact
            for token in (
                "설치기사님몇시", "설치시간", "방문시간",
                "몇시쯤방문", "기사님몇시",
            )
        ):
            return "INSTALLATION_TIME"
        if any(
            token in compact
            for token in (
                "몇시에도착", "몇시에오", "도착시간", "배송시간",
            )
        ):
            return "DELIVERY_TIME"
        if any(
            token in compact
            for token in (
                "설치일", "설치날짜", "설치예정일", "설치일정",
                "언제설치",
            )
        ):
            return "INSTALLATION_DATE"
        if any(
            token in compact
            for token in (
                "배송일", "배송예정일", "배송일정", "도착예정",
                "배송날짜",
                "도착날짜", "언제도착", "언제받", "배송언제",
                "언제쯤받", "받을수있", "언제올까",
                "오늘오", "내일오", "언제와", "언제오", "일정확인",
            )
        ):
            return "DELIVERY_DATE"
        if any(token in compact for token in ("배송조회", "배송상태", "출고상태")):
            return "DELIVERY_STATUS"
        if "예정일" in compact and (
            re.search(r"\d{1,2}(?:월|[./-])\d{1,2}", compact)
            or any(
                token in compact
                for token in ("말일까지", "말일", "언제", "가능할까요", "기다리")
            )
        ):
            # Existing-order questions often omit the nouns 배송/설치 after
            # mentioning a promised date.  The date/deadline combination is
            # still an authoritative schedule lookup intent.
            return "DELIVERY_DATE"
        return None

    @staticmethod
    def _is_pre_purchase_delivery(
        question: str,
        *,
        has_order_evidence: bool,
    ) -> bool:
        """Separate policy/availability questions from an existing order lookup."""

        if has_order_evidence:
            return False
        compact = re.sub(r"[\s\W_]+", "", str(question or "")).lower()
        if any(word in compact for word in POST_PURCHASE_DELIVERY_WORDS):
            return False
        if re.search(r"\d{1,2}(?:월|[./-])\d{1,2}(?:일)?주문", compact):
            return False
        # A general delivery-policy question is one concept, and it had three
        # separate definitions: this word list, the rule engine's shipping
        # keywords, and the predicates in text_utils. They disagreed, and the
        # gap was where inquiries fell through. "혹시 토요일에도 배달
        # 가능하나요? / 주문시 며칠 소요되나요" is two ordinary policy
        # questions, but this list has no 배달 and no "며칠 소요", so neither
        # part reached the pre-purchase path: both classified UNCLASSIFIED,
        # which set manual_review_required and dragged the whole inquiry to a
        # person at confidence 0.45.
        #
        # The text_utils predicates are the definition the rule engine already
        # routes on, so consulting them here is what keeps the two layers from
        # drifting apart again.
        if is_general_delivery_policy_question(
            question
        ) or is_weekend_delivery_policy_question(question):
            return True
        # "받을" only matched one inflection; "받아볼수", "받아보" and
        # "받는" are the same word doing the same job in a pre-purchase
        # question, and missing them sent "오늘 주문하면 언제쯤 받아볼수
        # 있을까요?" down the existing-order path.
        delivery_context = any(
            word in compact
            for word in (
                "배송", "배달", "발송", "도착", "받을", "받아", "받는",
                "수령", "설치",
            )
        )
        if not delivery_context:
            return False
        if any(word in compact for word in PRE_PURCHASE_DELIVERY_WORDS):
            return True
        # Feasibility wording alone settles it only when the customer did not
        # ask *when*.
        #
        # "can I have it by Saturday?" is named above as the example of this
        # tier, but no wording for it was ever listed, so "토요일에도
        # 배송되나요?" fell through to the existing-order path: it was read as
        # DELIVERY_DATE and demanded an order number and a DPS lookup for a
        # customer asking about the weekend policy. It sits in the weak tier
        # deliberately -- "제 주문 토요일에 언제 도착하나요?" carries an
        # explicit "when" and stays a schedule lookup.
        feasibility = any(
            word in compact for word in PRE_PURCHASE_FEASIBILITY_WORDS
        ) or is_weekend_delivery_policy_question(question)
        return feasibility and not EXPLICIT_WHEN.search(compact)

    # A connector joins two clauses, but it does not on its own mean there
    # are two questions: "50인치, 60인치 중 어떤 게 좋나요" is one comparison.
    # These only propose candidate boundaries; _connector_segments decides
    # whether a boundary actually separates two intents.
    LIST_CONNECTOR = re.compile(r"\s*,\s*|\s*그리고\s*")
    CLAUSE_CONNECTOR = re.compile(r"(?<=[가-힣])고(?=\s)")
    # Shorter than this is an enumerated noun ("스탠드", "배송"), not a
    # question of its own.
    MIN_SEGMENT_LENGTH = 4

    def analyze(self, request: AnswerRequest) -> InquiryAnalysis:
        subquestions = split_subquestions(request.question)
        if len(subquestions) == 1 and _is_schedule_change_request(
            request.question
        ):
            # The splitter can retain two sentence fragments as one candidate
            # and the connector refinement can then separate the schedule
            # target from its action.  Judge this indivisible operational
            # request on the full original text before attempting that split.
            return self._analyze_single(request)
        if len(subquestions) == 1:
            # The deterministic splitter keys on sentence enders and list
            # punctuation, so a connector-joined compound arrives as one
            # question. Left that way, a single HARD word ("파손") classifies
            # the whole text HIGH_RISK and suppresses drafting, losing the
            # answerable part with it. Refine only this case, so the existing
            # decomposition path is untouched.
            refined = self._connector_segments(request, subquestions[0])
            if refined:
                subquestions = refined
        if len(subquestions) > 1:
            return self._analyze_compound(request, subquestions)
        return self._analyze_single(request)

    def _subtype_of(
        self, request: AnswerRequest, text: str
    ) -> tuple[str, str]:
        """Classify one candidate segment as (subtype, detected_intent)."""

        judged = self._analyze_single(
            dataclasses.replace(request, question=text)
        )
        return judged.inquiry_subtype, judged.detected_intent

    def _connector_segments(
        self, request: AnswerRequest, text: str
    ) -> tuple[str, ...]:
        """Split a connector-joined question only when the sides differ.

        The connector is never the evidence. Each candidate segment is run
        through the same single-question classifier, and the split is kept
        only when the segments genuinely carry different intents. That is
        what separates "설치방법, 파손 보상 알려주세요" (installation guidance
        plus a dispute) from "50인치, 60인치 중 어떤 게 좋나요" (one comparison,
        both sides PRODUCT_SPEC_OR_FEATURE) without listing either sentence.
        """

        for segments in self._candidate_segmentations(text):
            if len(segments) < 2:
                continue
            if any(
                len(segment) < self.MIN_SEGMENT_LENGTH for segment in segments
            ):
                continue
            judged = [self._subtype_of(request, segment) for segment in segments]
            # An UNCLASSIFIED segment is far more often a fragment left by
            # cutting mid-clause ("AS도 되고") than a question of its own, and
            # an unclassified part drags the whole inquiry into review. Only
            # split where every side stands on its own as a recognised
            # question.
            if any(subtype == "UNCLASSIFIED" for subtype, _ in judged):
                continue
            # Compare subtype *and* intent: "설치방법" and "기존 브라켓과
            # 호환되나요" share the installation subtype but differ in intent,
            # while the two halves of "50인치, 60인치 중 어떤 게 좋나요" match on
            # both and stay one question.
            if len(set(judged)) < 2:
                continue
            return segments
        return ()

    def _candidate_segmentations(self, text: str) -> list[tuple[str, ...]]:
        """Candidate splits, most explicit connector first."""

        candidates: list[tuple[str, ...]] = []
        listed = tuple(
            part.strip()
            for part in self.LIST_CONNECTOR.split(text)
            if part and part.strip()
        )
        if len(listed) > 1:
            candidates.append(listed)
        # A verbal "-고" chains clauses and can repeat inside one clause
        # ("알고 싶고"); the final one is the real clause boundary, so only
        # that split is offered.
        boundaries = list(self.CLAUSE_CONNECTOR.finditer(text))
        if boundaries:
            end = boundaries[-1].end()
            left, right = text[:end].strip(), text[end:].strip()
            if left and right:
                candidates.append((left, right))
        return candidates

    def _analyze_compound(
        self, request: AnswerRequest, subquestions: tuple[str, ...]
    ) -> InquiryAnalysis:
        """Judge each sub-question on its own, then combine the verdicts.

        A single representative intent cannot describe a compound inquiry: the
        first matching branch used to win outright, so one high-risk phrase
        both suppressed draft generation for the whole message and erased the
        DPS requirement that another sub-question still needed. Requirements
        and review flags are therefore OR-ed across sub-questions, while the
        ability to answer survives if *any* part can be answered.
        """

        # Text and verdict are carried together from here on. The filtering
        # below drops fragments, and the whole-message rescues append one, so
        # a parallel list would silently fall out of step -- and the per-question
        # record built at the end has to name the question it judged.
        pairs: list[tuple[str, InquiryAnalysis]] = [
            (
                subquestion,
                self._analyze_single(
                    dataclasses.replace(request, question=subquestion)
                ),
            )
            for subquestion in subquestions
        ]
        parts = [analysis for _, analysis in pairs]
        # The whole-message rescue below has a mirror image. Splitting can also
        # separate the *report* of a change from the question about it:
        # "설치가 미뤄졌다고 들었는데 / 언제 오나요?" leaves one fragment holding
        # "미뤄" with no lookup marker, which reads as "please postpone it", and
        # the other holding "언제" with nothing to postpone. Read apart, a
        # customer asking what their new date is became a rescheduling request
        # and was sent to staff -- the same evidence, split, reversing the
        # verdict. The undivided message is the one that says what was meant.
        if _is_schedule_status_lookup(request.question) and any(
            item.inquiry_subtype == "SCHEDULE_CHANGE_REQUEST" for item in parts
        ):
            return self._analyze_single(request)
        # Sentence splitting can separate the schedule target ("installation
        # date") from the requested action ("move it earlier").  Each fragment
        # is harmless alone, but the full customer request requires an actual
        # staff action.  Preserve both the useful per-part analysis and the
        # whole-message safety judgement in the compound aggregation.
        if _is_schedule_change_request(request.question) and not any(
            item.inquiry_subtype == "SCHEDULE_CHANGE_REQUEST" for item in parts
        ):
            whole = self._analyze_single(request)
            # Target and action split across sentences are one operational
            # request, not two independently useful questions.  Keep compound
            # drafting only when another genuinely different question (A/S,
            # a product feature, etc.) is also present.
            other_intent = any(
                item.inquiry_subtype != "UNCLASSIFIED"
                and not item.delivery_related
                for item in parts
            )
            if not other_intent:
                return whole
            pairs.append((request.question, whole))
            parts.append(whole)
        # A fragment that classifies as nothing in particular, sitting beside
        # real questions, is a greeting or a closing remark ("확인 부탁드립니다.")
        # rather than an unanswerable question, and must not drag the whole
        # inquiry into review. When *every* part is unclassified there is no
        # compound to speak of, so the original single-question judgement of
        # the full text stands.
        classified = [
            item for item in parts if item.inquiry_subtype != "UNCLASSIFIED"
        ]
        if not classified:
            return self._analyze_single(request)
        # An unclassified fragment that still asks for review or for the order
        # is a real question the classifier could not label -- a payment
        # benefit question, say -- not a greeting. Dropping it discarded its
        # review requirement along with it, so a compound inquiry could lose
        # the very flag that was meant to hold it back. Keep such a part in the
        # aggregation (and in the sub-question count) while the representative
        # intent below still comes from a classified part.
        pairs = [
            (text, item)
            for text, item in pairs
            if item.inquiry_subtype != "UNCLASSIFIED"
            or item.manual_review_required
            or item.requires_order_id
            or item.requires_order_lookup
            or item.requires_dps_lookup
        ]
        parts = [item for _, item in pairs]
        # Sub-questions that all mean the same thing are not a compound
        # inquiry either; relabelling them would change nothing except to
        # lose the specific intent the pipeline downstream relies on.
        subtypes = {item.inquiry_subtype for item in parts}
        if len(subtypes) == 1:
            # One intent, but still two questions. "혹시 토요일에도 배달
            # 가능하나요? / 주문시 며칠 소요되나요" are both delivery-policy
            # questions and rightly share a subtype -- yet the weekend half has
            # no confirmed rule behind it and the duration half does, so a
            # draft has to tell them apart. Returning the single-question
            # analysis unchanged threw the breakdown away and left the model
            # with one blob again.
            return dataclasses.replace(
                self._analyze_single(request),
                subquestion_analyses=_subquestion_records(pairs),
            )

        # The representative carries the inquiry type, order status, strategy
        # and intent, so it must be a part the classifier actually recognised.
        representative = next(
            (
                item
                for item in classified
                if item.manual_review_required
            ),
            classified[0],
        )
        answerable = [item for item in parts if item.can_generate_answer]
        # Keep a subtype that permits drafting whenever some part is
        # answerable; the review flag below is what withholds publishing.
        subtype = (
            "COMPOUND_MULTI_INTENT"
            if answerable
            else representative.inquiry_subtype
        )
        compatibility = any(
            item.detected_intent == "PRODUCT_COMPATIBILITY" for item in parts
        )
        manual = any(item.manual_review_required for item in parts)
        reasons: list[str] = [
            f"복합문의로 {len(parts)}개 질문을 각각 판단했습니다."
        ]
        for item in parts:
            reasons.extend(item.reasons)
        return InquiryAnalysis(
            inquiry_type=representative.inquiry_type,
            inquiry_subtype=subtype,
            # A sub-question that needs the order or DPS keeps that
            # requirement alive for the whole inquiry.
            requires_order_lookup=any(
                item.requires_order_lookup for item in parts
            ),
            requires_dps_lookup=any(
                item.requires_dps_lookup for item in parts
            ),
            requires_order_id=any(item.requires_order_id for item in parts),
            order_id_present=representative.order_id_present,
            order_id_validated=representative.order_id_validated,
            order_id_status=representative.order_id_status,
            answer_strategy=representative.answer_strategy,
            selected_fact_keys=tuple(
                dict.fromkeys(
                    key for item in parts for key in item.selected_fact_keys
                )
            ),
            confidence=min(item.confidence for item in parts),
            reasons=tuple(dict.fromkeys(reasons)),
            manual_review_required=manual,
            auto_answerable=not manual,
            subquestion_analyses=_subquestion_records(pairs),
            # Preserved so the auto-post gate still raises
            # PRODUCT_COMPATIBILITY_NOT_VERIFIED for the compound inquiry.
            detected_intent=(
                "PRODUCT_COMPATIBILITY"
                if compatibility
                else representative.detected_intent
            ),
            # Union of every contributing sub-question's cause. ``manual`` is
            # OR-ed above, which loses *why*: one risky sub-question and one
            # unclassified fragment produce the same True. Keeping both causes
            # means a caller can require that all of them are classifier gaps
            # before treating the hold as one.
            manual_review_sources=tuple(dict.fromkeys(
                source
                for item in parts
                for source in item.manual_review_sources
            )),
        )

    def _analyze_single(self, request: AnswerRequest) -> InquiryAnalysis:
        question = re.sub(r"\s+", " ", str(request.question or "")).strip()
        legacy_type = str(request.inquiry_type or "").strip()
        order_text = str(request.order_id or "").strip()
        order_present = bool(order_text)
        has_order = bool(VALID_GENERAL_ORDER_ID_PATTERN.fullmatch(order_text))
        has_product_order = bool(
            str(request.product_order_id or "").strip()
        )
        detected_intent = self._intent(question)
        candidates = GENERAL_ORDER_ID_PATTERN.findall(question)
        long_numbers = ANY_LONG_NUMBER_PATTERN.findall(question)
        pre_purchase_delivery = self._is_pre_purchase_delivery(
            question,
            has_order_evidence=bool(
                has_order or has_product_order or candidates or long_numbers
            ),
        )

        if has_order:
            order_status = OrderIdStatus.VALIDATED
        elif order_present:
            order_status = OrderIdStatus.INVALID
        elif has_product_order:
            order_status = OrderIdStatus.AMBIGUOUS
        elif len(candidates) == 1:
            order_status = OrderIdStatus.CANDIDATE_FOUND
        elif len(candidates) > 1:
            order_status = OrderIdStatus.AMBIGUOUS
        else:
            order_status = OrderIdStatus.MISSING

        reasons: list[str] = []
        if any(word in question for word in HIGH_RISK_WORDS):
            kind = InquiryType.MANUAL_REVIEW_REQUIRED
            subtype = "HIGH_RISK_OR_DISPUTE"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.MANUAL_REVIEW
            confidence = 0.99
            manual = True
            reasons.append("위험·분쟁 관련 표현이 있어 직원 판단이 필요합니다.")
        elif any(word in question for word in CANCEL_WORDS):
            kind = InquiryType.CANCEL_RETURN_EXCHANGE
            subtype = "CANCEL_RETURN_EXCHANGE"
            requires_order = True
            requires_dps = False
            strategy = (
                AnswerStrategy.MANUAL_REVIEW
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.97
            manual = has_order
            reasons.append("취소·반품·교환 문의로 분류했습니다.")
        elif _is_schedule_change_request(question):
            kind = InquiryType.DELIVERY_INSTALLATION_STATUS
            subtype = "SCHEDULE_CHANGE_REQUEST"
            requires_order = True
            requires_dps = True
            # Q&A Auto cannot reschedule an order, so it must never reply on
            # its own -- not even to ask for the order number, and never with
            # the *current* date, which would read as ignoring the request.
            # Staff review is required whether or not an order id is present.
            strategy = AnswerStrategy.MANUAL_REVIEW
            confidence = 0.99
            manual = True
            detected_intent = "SCHEDULE_CHANGE"
            reasons.append("일정 변경 요청은 직원 검토가 필요합니다.")
        elif detected_intent == "NOTIFICATION_POLICY":
            kind = InquiryType.INSTALLATION_GENERAL
            subtype = "DELIVERY_NOTIFICATION_POLICY"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.97
            manual = False
            reasons.append("배송·설치 알림 방식에 대한 일반 정책 문의입니다.")
        elif pre_purchase_delivery:
            kind = InquiryType.INSTALLATION_GENERAL
            subtype = "PRE_PURCHASE_DELIVERY_GUIDANCE"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.99
            manual = False
            detected_intent = "PRE_PURCHASE_DELIVERY"
            reasons.append(
                "주문 전 배송·설치 가능 여부를 묻는 일반 정책 문의입니다."
            )
        elif detected_intent in {
            "DELIVERY_DATE",
            "DELIVERY_TIME",
            "INSTALLATION_DATE",
            "INSTALLATION_TIME",
            "DELIVERY_STATUS",
        } or any(word in question for word in DELIVERY_SCHEDULE_WORDS):
            kind = InquiryType.DELIVERY_INSTALLATION_STATUS
            subtype = "DELIVERY_OR_INSTALLATION_SCHEDULE"
            requires_order = True
            requires_dps = True
            strategy = (
                AnswerStrategy.DIRECT_FACT_ANSWER
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.98
            manual = False
            detected_intent = detected_intent or "DELIVERY_DATE"
            reasons.append("배송 또는 설치 일정 확인 표현을 찾았습니다.")
        elif any(word in question for word in ORDER_STATUS_WORDS):
            kind = InquiryType.ORDER_STATUS
            subtype = "ORDER_LOOKUP"
            requires_order = True
            requires_dps = False
            strategy = (
                AnswerStrategy.DIRECT_FACT_ANSWER
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.96
            manual = False
            reasons.append("주문 상태 확인 표현을 찾았습니다.")
        elif detected_intent == "INSTALLATION_METHOD" or (
            any(word in question for word in INSTALLATION_GENERAL_WORDS)
            # "배송 올 때 스탠드도 같이 오나요?" contains 스탠드, which opens
            # the installation branch, but the customer is asking what is in
            # the box -- a fact about the product's contents, not about how it
            # gets installed. Asking how to install a stand is unaffected: the
            # contents predicate needs the item *and* an inclusion or
            # "must I supply it" attribute.
            and not is_package_contents_question(question)
        ):
            kind = InquiryType.INSTALLATION_GENERAL
            subtype = (
                "INSTALLATION_METHOD"
                if detected_intent == "INSTALLATION_METHOD"
                else "GENERAL_INSTALLATION_GUIDANCE"
            )
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.94
            manual = False
            reasons.append(
                "설치 주체·자가설치 여부를 묻는 설치방법 문의입니다."
                if detected_intent == "INSTALLATION_METHOD"
                else "특정 주문과 무관한 설치 일반 문의입니다."
            )
        elif (
            any(word in question for word in PRODUCT_GENERAL_WORDS)
            or is_package_contents_question(question)
            # A question about what the product *is*, rather than about a
            # property it has. The word list above enumerates attributes and
            # therefore never recognised "스마트티비는 처음인데 인터넷티비랑
            # 다른건가요" -- which fell to UNCLASSIFIED and, through the
            # compound OR, held a four-question inquiry for a person.
            or is_product_concept_question(question)
        ):
            kind = InquiryType.PRODUCT_GENERAL
            subtype = "PRODUCT_SPEC_OR_FEATURE"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.92
            manual = False
            # "무엇이 함께 오는가"는 그 상품의 구성 사실이므로, 배송 정책이
            # 아니라 Product Fact·동일상품 Learning이 답해야 하는 질문이다.
            # 이 분기에 오면 product_fact_guard가 구성품 문의로 인식해 검증된
            # 근거를 요구하고, 근거가 없을 때만 직원 검토로 간다.
            reasons.append("제품 사양·기능 관련 일반 문의입니다.")
        elif _has_no_substantive_question(question):
            # Nothing was asked, so there is nothing an automatic answer could
            # be right about. Placed after every keyword branch so that a real
            # finding still wins ("안녕하세요, 제품이 파손돼서 왔어요" stays
            # HIGH_RISK_OR_DISPUTE), and before the category fallbacks so that
            # the channel's category cannot turn a greeting into an answerable
            # product question. A draft is still generated for staff; only
            # publishing it automatically is withheld -- the subtype is not
            # UNCLASSIFIED, so the classifier-gap relaxation cannot reach it.
            kind = InquiryType.INFORMATION_INSUFFICIENT
            subtype = "NO_SUBSTANTIVE_QUESTION"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.REQUEST_ADDITIONAL_INFORMATION
            confidence = 0.99
            manual = True
            reasons.append("인사·감사 표현만 있고 실제 질문이 없습니다.")
        elif "배송" in legacy_type:
            kind = InquiryType.DELIVERY_INSTALLATION_STATUS
            subtype = "LEGACY_DELIVERY_CATEGORY"
            requires_order = True
            requires_dps = True
            strategy = (
                AnswerStrategy.DIRECT_FACT_ANSWER
                if has_order
                else AnswerStrategy.REQUEST_ORDER_ID
            )
            confidence = 0.78
            manual = False
            detected_intent = detected_intent or "DELIVERY_STATUS"
            reasons.append(
                "본문 키워드 대신 기존 배송 문의 유형을 보조 근거로 사용했습니다."
            )
        elif "상품" in legacy_type:
            kind = InquiryType.PRODUCT_GENERAL
            subtype = "LEGACY_PRODUCT_CATEGORY"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.GENERAL_GUIDANCE
            confidence = 0.72
            manual = False
            reasons.append(
                "본문 키워드 대신 기존 상품 문의 유형을 보조 근거로 사용했습니다."
            )
        elif not question:
            kind = InquiryType.INFORMATION_INSUFFICIENT
            subtype = "EMPTY_QUESTION"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.REQUEST_ADDITIONAL_INFORMATION
            confidence = 1.0
            manual = True
            reasons.append("문의 내용이 비어 있습니다.")
        else:
            kind = InquiryType.INFORMATION_INSUFFICIENT
            subtype = "UNCLASSIFIED"
            requires_order = False
            requires_dps = False
            strategy = AnswerStrategy.MANUAL_REVIEW
            confidence = 0.45
            manual = True
            detected_intent = detected_intent or "GENERAL"
            reasons.append("결정적인 문의 유형 규칙과 일치하지 않습니다.")

        # Tag compatibility questions so the auto-post gate can require an
        # authoritative fact. Only a tag: the type, strategy and draft route
        # are untouched, and an exact accessory template can still answer.
        if _is_compatibility_question(question):
            detected_intent = "PRODUCT_COMPATIBILITY"
            reasons.append(
                "액세서리·설치 호환 여부는 확정 근거가 있어야 안내할 수 "
                "있습니다."
            )

        # Raise the review flag without changing the classified type or the
        # answer strategy, so the normal Learning/GPT draft is still produced
        # for staff to edit -- only publishing it automatically is withheld.
        if not manual and _is_unverifiable_payment_benefit(question):
            manual = True
            reasons.append(
                "카드·결제 혜택은 시점에 따라 달라져 현재 적용 여부를 "
                "직원이 확인해야 합니다."
            )

        requires_order_id = requires_order
        validated = has_order
        effective_kind = kind
        if requires_order_id and not validated:
            effective_kind = InquiryType.ORDER_INFO_REQUIRED
            # Asking the customer for their order number is a safe automatic
            # reply only when the intent itself did not already require a
            # person. A schedule change still needs staff even before the
            # order number arrives, so the review requirement is preserved
            # here instead of being cleared by the order-id shortcut.
            if not manual:
                strategy = AnswerStrategy.REQUEST_ORDER_ID
            if has_product_order:
                reasons.append(
                    "상품주문번호만 있으며 DPS에 사용할 일반 주문번호가 없습니다."
                )
            elif candidates:
                reasons.append(
                    "문의 본문의 숫자는 후보일 뿐 API 검증된 주문번호가 아닙니다."
                )
            else:
                reasons.append(
                    "일반 주문번호가 없거나 형식이 유효하지 않습니다."
                    if order_present
                    else "확인된 네이버 일반 주문번호가 없습니다."
                )
        elif not requires_order_id and not validated:
            order_status = OrderIdStatus.NOT_REQUIRED

        selected = self._selected_keys(effective_kind, strategy)
        return InquiryAnalysis(
            inquiry_type=effective_kind,
            inquiry_subtype=subtype,
            requires_order_lookup=requires_order,
            # Business requirement and call eligibility are deliberately
            # separate. A missing order id still means DPS is required for a
            # delivery inquiry, but can_execute_dps_lookup remains false.
            requires_dps_lookup=requires_dps,
            requires_order_id=requires_order_id,
            order_id_present=has_order,
            order_id_validated=validated,
            order_id_status=order_status,
            answer_strategy=strategy,
            selected_fact_keys=selected,
            confidence=confidence,
            reasons=tuple(reasons),
            manual_review_required=manual,
            auto_answerable=not manual,
            detected_intent=detected_intent or "GENERAL",
            # The subtype *is* the cause: every branch above that sets
            # ``manual`` names its own finding there (HIGH_RISK_OR_DISPUTE,
            # CANCEL_RETURN_EXCHANGE, SCHEDULE_CHANGE_REQUEST, EMPTY_QUESTION,
            # UNCLASSIFIED, ...). Recording it lets a caller tell a real
            # finding apart from a classifier gap without re-deriving either.
            manual_review_sources=(subtype,) if manual else (),
        )

    @staticmethod
    def _selected_keys(
        inquiry_type: InquiryType,
        strategy: AnswerStrategy,
    ) -> tuple[str, ...]:
        if strategy is AnswerStrategy.REQUEST_ORDER_ID:
            return (
                "analysis.requires_order_id",
                "analysis.order_id_status",
                "analysis.private_post_required",
            )
        if strategy is AnswerStrategy.MANUAL_REVIEW:
            return ("rule.answer",)
        if inquiry_type is InquiryType.DELIVERY_INSTALLATION_STATUS:
            return (
                "installation.date",
                "installation.source",
                "installation.installation_date_confirmed",
                "policy.installation_notification_policy",
                "policy.date_may_change",
            )
        if inquiry_type is InquiryType.ORDER_STATUS:
            return ("delivery.status", "order.order_status")
        if inquiry_type in {
            InquiryType.PRODUCT_GENERAL,
            InquiryType.INSTALLATION_GENERAL,
        }:
            return ("product.name", "product.option_name", "rule.answer")
        return ("rule.answer",)
