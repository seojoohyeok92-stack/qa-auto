from __future__ import annotations

import re
from typing import Iterable

from answer.text_utils import mask_personal_information


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:01[016789]|0\d{1,2})[- .]?\d{3,4}[- .]?\d{4}(?!\d)")
ORDER_ID = re.compile(r"(?<!\d)\d{16}(?!\d)")
PRODUCT_ORDER_ID = re.compile(r"(?<!\d)\d{8,15}(?!\d)")
SECRET = re.compile(
    r"(?i)\b(?:"
    r"authorization\s*[:=]\s*(?:bearer\s+)?\S+|"
    r"(?:token|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
    r"api[_ -]?key|password)\s*[:=]\s*\S+"
    r")"
)
ADDRESS = re.compile(
    r"(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|"
    r"전북|전남|경북|경남|제주)[^\n,]{0,50}(?:로|길|동|읍|면)\s*\d+(?:-\d+)?"
)


class LearningPrivacyService:
    """Learning 저장과 검색 프롬프트의 개인정보를 이중으로 제거한다."""

    def mask(self, value: object, *, customer_names: Iterable[str] = ()) -> str:
        text = str(value or "")
        for name in customer_names:
            clean = str(name or "").strip()
            if len(clean) >= 2:
                text = text.replace(clean, "<masked-name>")
        text = mask_personal_information(text)
        text = EMAIL.sub("<masked-email>", text)
        text = PHONE.sub("<masked-phone>", text)
        text = ORDER_ID.sub("<masked-order-id>", text)
        text = PRODUCT_ORDER_ID.sub("<masked-product-order-id>", text)
        text = ADDRESS.sub("<masked-address>", text)
        return SECRET.sub("<masked-secret>", text).strip()
