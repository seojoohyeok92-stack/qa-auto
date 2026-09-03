"""운영자가 Negative Learning에 남긴 메모를 교정 지식으로 읽는다.

Negative Learning은 오랫동안 "이 과거 답변을 재사용하지 마라"는 배제 신호로만
쓰였다.  그런데 운영자는 배제하면서 *왜* 틀렸는지와 *어떻게* 답해야 하는지를
같이 적어 두었다.  서버 스냅샷 기준으로 그런 메모가 30건 있고, 그 30건은
``learning_feedback.correction_note`` 에만 남아 있어 런타임에서는 한 번도 읽힌
적이 없다 (``learning_signals`` 로 승격된 것은 그보다 나중에 저장된 16건뿐이다).

이 모듈은 그 메모를 구조화한다.  구조화라고 해도 새 문장을 만들지 않는다.
운영자가 쓴 절(clause)을 그대로 잘라서 어느 역할인지만 붙인다.

    BAD_PATTERN   반복하면 안 되는 내용        ("고객센터 별도 신청 안내는 잘못됨")
    CORRECTION    앞으로 이렇게 답하라는 지시   ("설치기사 방문 시 수거 요청하도록 안내")
    GOOD_PATTERN  맞았다고 확인해 준 내용      ("무료수거 안내는 맞음")
    REASON        저장 당시 선택한 분류 코드

메모가 없는 Negative는 여기 들어오지 않는다.  "아마 이런 이유였겠지"를 만들어
내는 것이 이 파이프라인에서 가장 위험한 실패이므로, 근거가 되는 문장이 없으면
교정 지식도 없다 -- 기존 배제 신호만 그대로 남는다.

원본 메모는 언제나 ``source_memo`` 에 그대로 보존된다.  파싱은 해석일 뿐이고,
source-of-truth 는 운영자가 쓴 원문이다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# 저장 UI가 쓰는 분류 코드.  런타임에서는 사람이 읽을 수 있는 한 줄로만 쓰이며,
# 여기 없는 코드는 코드 문자열 그대로 전달된다 (알 수 없는 코드를 버리지 않는다).
REASON_LABELS: dict[str, str] = {
    "INTENT_NOT_REFLECTED": "고객이 실제로 물어본 내용이 답변에 반영되지 않음",
    "DELIVERY_INSTALLATION_ERROR": "배송/설치 안내가 잘못됨",
    "ROUTING_ERROR": "문의 의도를 다른 유형으로 잘못 분류함",
    "PRODUCT_INFO_ERROR": "제품 정보가 사실과 다름",
    "FACT_ERROR": "사실관계가 틀림",
    "TONE_EXPRESSION": "표현/말투 문제",
    "CUSTOMER_SPECIFIC": "이 고객에게만 해당하는 내용이라 재사용 불가",
    "ONE_TIME_EXCEPTION": "일회성 예외 처리",
    "NOT_REUSABLE": "재사용 불가",
    "TEST_OR_MEANINGLESS": "테스트/무의미",
    "OTHER": "기타",
}


# 절을 나누는 경계.  줄바꿈, "1." 같은 번호 매김, 문장부호만 쓴다.  운영자가
# 실제로 쓴 구분자이고, 여기서 더 잘게 쪼개면 한 문장이 반으로 갈린다.
_CLAUSE_SPLIT = re.compile(r"(?:\r?\n)+|(?<=[.!?])\s+|\s*(?=\d+\.\s)")
_LEADING_MARKER = re.compile(r"^\s*(?:\d+\s*[.)]|[-*·])\s*")

# 운영자가 "앞으로 이렇게 하라"고 쓸 때의 어미.  한국어 업무 메모는 명사형
# 종결(-함/-됨)로 끝나는 경우가 대부분이라 동사 활용형만으로는 잡히지 않는다.
_CORRECTION_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"해야\s*(?:함|합니다|하[며고]|되|됨)"),
    re.compile(r"(?:돼|되어|되)야\s*(?:함|합니다|한다)"),
    re.compile(r"(?:됐|했)어야\s*(?:함|합니다|한다)"),
    re.compile(r"필요\s*(?:함|합니다|하다)"),
    re.compile(r"들어가야\s*함"),
    re.compile(r"(?:하|되)면\s*됨|하면\s*된다|남기면\s*됨"),
    re.compile(r"하도록\s*(?:안내|요청|유도)"),
    re.compile(r"(?:안내|답변|요청|확인)\s*(?:해야|하는\s*게|하는\s*것이)"),
    re.compile(r"(?:하는\s*게|하는\s*것이)\s*(?:맞|좋)"),
    re.compile(r"(?:안내|답변)\s*[.]?\s*$"),
    re.compile(r"라고\s*(?:안내|답변|남기)"),
)

# 운영자가 "이건 잘못됐다"고 쓸 때의 표현.
_BAD_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"잘못\s*(?:됨|됐|되었|된|함)?"),
    re.compile(r"틀[렸림]"),
    re.compile(r"없었음|없음|누락|빠[졌짐]"),
    re.compile(r"않았음|안\s*됐|안됐|못했|못\s*했"),
    re.compile(r"오인|잘못\s*판단|잘못\s*분류"),
    # "하지 말 것" 같은 금지문만.  "주문하지 않은 고객" 처럼 상황을
    # 서술하는 부정은 잘못된 답변을 가리키는 말이 아니다.
    re.compile(r"(?:하|주|쓰|넣)지\s*(?:마|말것|말아|말아야|않아야|않도록)"),
    re.compile(r"불필요|과도"),
)

# 운영자가 "이 부분은 맞다"고 확인해 준 표현.
_GOOD_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"잘\s*했|잘함|적절(?:한|했)"),
    re.compile(r"(?:은|는|이|가)\s*맞(?:음|다|고|으며)"),
    re.compile(r"지금[의\s]*답변처럼|간결한\s*답변"),
)


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _clauses(memo: str) -> list[str]:
    parts: list[str] = []
    for raw in _CLAUSE_SPLIT.split(str(memo or "")):
        clause = _LEADING_MARKER.sub("", str(raw or "")).strip()
        clause = clause.strip(" \t·-")
        if clause:
            parts.append(clause)
    return parts


@dataclass(frozen=True)
class NegativeCorrection:
    """하나의 Negative 메모를, 원문을 보존한 채 역할별로 나눈 것."""

    feedback_id: int
    source_memo: str
    reason_code: str = ""
    bad_patterns: tuple[str, ...] = ()
    corrections: tuple[str, ...] = ()
    good_patterns: tuple[str, ...] = ()

    @property
    def reason_label(self) -> str:
        code = str(self.reason_code or "").upper()
        return REASON_LABELS.get(code, code)

    @property
    def has_memo(self) -> bool:
        return bool(str(self.source_memo or "").strip())

    @property
    def structured(self) -> bool:
        """운영자 메모에서 역할이 실제로 구분되었는지."""

        return bool(self.bad_patterns or self.good_patterns) and bool(
            self.corrections
        )

    @property
    def guidance_text(self) -> str:
        """근거로 인용될 수 있는 부분만.  BAD_PATTERN은 절대 포함하지 않는다."""

        return " ".join((*self.good_patterns, *self.corrections)).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_id": int(self.feedback_id),
            "reason_code": self.reason_code or None,
            "reason": self.reason_label or None,
            "bad_patterns": list(self.bad_patterns),
            "corrections": list(self.corrections),
            "good_patterns": list(self.good_patterns),
            "structured": self.structured,
        }


def parse_operator_memo(
    memo: object,
    *,
    reason_code: object = "",
    feedback_id: object = 0,
) -> NegativeCorrection | None:
    """운영자 메모를 역할별로 나눈다.  메모가 없으면 ``None``.

    분류되지 않은 절은 버리지 않고 ``corrections`` 로 남긴다.  운영자가 쓴
    지시문 전체가 교정 지식이고, 어미를 인식하지 못했다는 이유로 그 지시를
    없애는 것이 새 문장을 만드는 것보다 낫지 않기 때문이다.  반대로 없는
    말을 만들어내는 일은 어느 경로에서도 일어나지 않는다 -- 여기서 나오는
    모든 문자열은 원문에서 잘라낸 것이다.
    """

    text = str(memo or "").strip()
    if not text:
        return None
    bad: list[str] = []
    correction: list[str] = []
    good: list[str] = []
    for clause in _clauses(text):
        is_correction = _matches(_CORRECTION_MARKERS, clause)
        is_bad = _matches(_BAD_MARKERS, clause)
        is_good = _matches(_GOOD_MARKERS, clause)
        if is_correction:
            # 처방이 붙은 절은 교정 지시다.  "…답변이 잘못됨. …돼야함" 처럼
            # 진단과 처방이 한 절에 같이 있으면 처방 쪽으로 읽는다.
            correction.append(clause)
        elif is_bad:
            bad.append(clause)
        elif is_good:
            good.append(clause)
        else:
            correction.append(clause)
    return NegativeCorrection(
        feedback_id=int(feedback_id or 0),
        source_memo=text,
        reason_code=str(reason_code or "").upper(),
        bad_patterns=tuple(dict.fromkeys(bad)),
        corrections=tuple(dict.fromkeys(correction)),
        good_patterns=tuple(dict.fromkeys(good)),
    )
