from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from answer.text_utils import SELLER_IDENTITY_QUERY, normalize_product_name
from services.product_fact_guard import (
    DIMENSION_TOKEN,
    extract_model_code,
    is_dimension_token,
)


PRODUCT_SCOPES = {
    "MODEL",
    "VARIANT",
    "PRODUCT_FAMILY",
    "CATEGORY",
    "POLICY",
    "GLOBAL",
}

POLICY_TOPICS = {
    "DELIVERY",
    "INSTALLATION",
    "RETURN_CANCEL",
    "AS_SUPPORT",
    "PROMOTION_EVENT",
    "GENERAL_POLICY",
}

STRICT_PRODUCT_TOPICS = {
    "PRODUCT_SPEC",
    "PORT_CONNECTIVITY",
    "ANTENNA_BROADCAST",
    "OTT_SMART_FEATURE",
    "REMOTE_CONTROL",
    "STAND_BRACKET_VESA",
    "DISPLAY_PANEL",
    "AUDIO",
    "ACCESSORY_COMPONENT",
}

GENERIC_TOPICS = {"OTHER", "GENERAL_POLICY", "PRODUCT_SPEC"}

# What a topic/attribute mismatch costs a candidate now that it no longer
# removes one. Large enough that an on-topic candidate outranks an off-topic
# one at equal similarity, small enough that a strong match still survives --
# the ordering is the point, not a second threshold.
TOPIC_MISMATCH_PENALTY = -0.20

# What a *stated* identity contradiction costs a Learning candidate: the model
# code, size, category or brand on the row differs from the current product's.
# A different listing id alone, or no identity to compare, costs nothing -- that
# is not evidence of a different product, and GPT ② reads the identifiers.
#
# Kept for stated contradictions because it was measured: removing it outright
# dropped the content-defined top-3 recall of the retrieval benchmark
# (tests/test_gpt_first_learning_retrieval.py) from 23/25 to 21/25, as 27/32-inch
# M5 rows and stand-accessory listings displaced the same-model answer.
IDENTITY_MISMATCH_PENALTY = -0.20
#
# Identity used to return ``hard_reject=True``, which is the one verdict that
# survives ``hard_conflicts_only`` -- production retrieval's mode -- so a
# candidate whose topic matched the question exactly was removed before GPT ②
# could read it. Measured on the server data: 688218182 asked about 벽걸이
# 설치 and its 추가 비용, and the store holds an approved answer saying the
# bracket ships with the order and the installation fee is not charged
# (LID 117, ``topic_match=MATCH``). It was dropped as
# INSUFFICIENT_PRODUCT_IDENTITY, and GPT ② was handed 해피콜 and 주문취소
# instead. Same shape on 688218219 for 리모컨 (LID 72 / 193649 / 19554).
#
# A Learning row is candidate evidence, not a VERIFIED product fact: whether
# another listing's answer applies here is a judgement about meaning, and it
# belongs to GPT ②, which is told the candidate's origin. Product Fact identity
# safety is unchanged and lives in ``product_knowledge_service``.

# Topics say what an answer is *about*; these say what is being asked *of* it.
#
# A customer asked which seller name to enter on the 온누리 rebate form after
# being told their entry was wrong. The store holds an approved answer about
# the rebate's application period, and both texts contain "온누리", so
# PROMOTION_EVENT matched on both sides and the period answer was selected as
# grounds for a question about the seller name. It answered a question nobody
# asked, with a URL and a date range attached.
#
# The subject was never the problem -- both really are about 온누리. What was
# missing is the attribute: 행사+기간 and 행사+판매처 are different questions,
# as are 배송+기간 and 배송+변경, 설치+방법 and 설치+비용, 브라켓+호환 and
# 브라켓+구성품. One dimension, not one rule per promotion.
ATTRIBUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PERIOD",
        re.compile(
            r"기간|기한|마감|종료일|시작일|며칠|언제까지|언제부터|유효",
            re.IGNORECASE,
        ),
    ),
    # Shared with the rule engine's event branch, so the two layers cannot
    # disagree about what counts as asking which seller to name.
    ("SELLER_IDENTITY", SELLER_IDENTITY_QUERY),
    (
        # "얼마나 걸리나요" is a duration, not a price, so 얼마 may not match
        # loosely -- it would put COST on half the delivery questions in the
        # store and start rejecting their own answers.
        "COST",
        re.compile(
            r"비용|가격|요금|무료|유료|추가금|금액|얼마(?:인|예요|에요|일까|입니까|죠)",
            re.IGNORECASE,
        ),
    ),
    (
        "METHOD",
        re.compile(
            r"방법|어떻게\s*(?:하|되|신청|진행)|절차|신청\s*방법|입력\s*(?:하|해)",
            re.IGNORECASE,
        ),
    ),
    (
        # Not a bare "포함되": "설치비도 포함되어 있나요" is about what the price
        # covers, not about what is in the box, and reading it as CONTENTS made
        # it clash with the identically-meant "설치가 포함된 가격인가요" -- the
        # gate rejecting a question against its own answer.
        "CONTENTS",
        re.compile(
            r"구성품|동봉|같이\s*오|함께\s*오|별도\s*구매|따로\s*구매|포함품",
            re.IGNORECASE,
        ),
    ),
    (
        "COMPATIBILITY",
        re.compile(r"호환|맞나요|맞는지|규격이\s*맞|사용\s*가능한", re.IGNORECASE),
    ),
    (
        "CHANGE_REQUEST",
        re.compile(
            r"변경|바꿔|바꾸|수정하라|수정해|옮겨|미뤄|당겨|취소해", re.IGNORECASE
        ),
    ),
)


TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "RETURN_CANCEL",
        re.compile(r"반품|교환|환불|취소|철회|회수|수거", re.IGNORECASE),
    ),
    (
        "PROMOTION_EVENT",
        re.compile(
            r"프로모션|이벤트|행사|쿠폰|포인트|적립|사은품|감사제|페스티벌|라이브\s*방송|온누리",
            re.IGNORECASE,
        ),
    ),
    (
        # Both boundaries are Latin-only lookarounds rather than \W, because
        # Korean agglutinates straight onto the term with no space on either
        # side: "삼성센터AS무상기간", "A/S는", "A/S가". Hangul is a word
        # character, so a \W boundary never matched those forms while the
        # answer still matched via "서비스센터" -- the question then looked
        # like it had no A/S topic, and answering it registered as an
        # unrequested topic, holding otherwise safe compound answers for
        # review. That is exactly what happened to inquiry 686125753, whose
        # unspaced "삼성센터AS무상기간알려주세요" classified as OTHER.
        # The lookarounds still reject Latin neighbours such as ASUS and GAS.
        "AS_SUPPORT",
        re.compile(
            r"(?<![A-Za-z0-9])A\s*/?\s*S(?![A-Za-z0-9])|애프터\s*서비스"
            r"|서비스\s*센터|무상\s*수리|보증\s*기간|수리\s*접수",
            re.IGNORECASE,
        ),
    ),
    (
        "ANTENNA_BROADCAST",
        re.compile(
            r"동축|안테나|RF\s*단자|지상파|방송\s*수신|채널\s*(?:검색|미지원|수신)|ATSC|튜너",
            re.IGNORECASE,
        ),
    ),
    (
        "PORT_CONNECTIVITY",
        re.compile(
            r"HDMI|USB|DP\s*(?:포트|단자)|DisplayPort|포트\s*(?:수|개수)|입력\s*단자|출력\s*단자|단자\s*(?:수|개수)",
            re.IGNORECASE,
        ),
    ),
    (
        "REMOTE_CONTROL",
        re.compile(r"리모컨|원격\s*제어|리모트", re.IGNORECASE),
    ),
    (
        "OTT_SMART_FEATURE",
        re.compile(
            r"OTT|넷플릭스|유튜브|디즈니\s*플러스|티빙|웨이브|SmartThings|스마트\s*앱|미러링|AirPlay",
            re.IGNORECASE,
        ),
    ),
    (
        "STAND_BRACKET_VESA",
        re.compile(
            r"VESA|베사|브라켓|벽걸이|스탠드|받침대|"
            r"다리\s*(?:탈부착|탈착|분리|장착)|거치대|마운트",
            re.IGNORECASE,
        ),
    ),
    (
        "DISPLAY_PANEL",
        re.compile(
            r"패널|화면\s*(?:꺼짐|불량|파손|검은|밝기|크기)|해상도|UHD|FHD|4K|8K|QLED|OLED|화질|불량\s*화소",
            re.IGNORECASE,
        ),
    ),
    (
        "AUDIO",
        re.compile(r"스피커|음질|음량|오디오|사운드|이어폰|블루투스\s*음향", re.IGNORECASE),
    ),
    (
        "ACCESSORY_COMPONENT",
        re.compile(r"구성품|동봉|포함품|케이블\s*포함|어댑터|전원선|액세서리", re.IGNORECASE),
    ),
    (
        "DELIVERY",
        re.compile(r"배송|출고|발송|도착|택배|배송비|운송", re.IGNORECASE),
    ),
    (
        "INSTALLATION",
        re.compile(
            r"설치|자가\s*설치|방문\s*설치|설치기사",
            re.IGNORECASE,
        ),
    ),
    (
        "GENERAL_POLICY",
        re.compile(r"정책|절차|접수\s*방법|상담\s*절차|고객센터\s*안내", re.IGNORECASE),
    ),
    (
        "PRODUCT_SPEC",
        re.compile(
            r"사양|스펙|규격|크기|치수|무게|기능\s*(?:지원|여부)|지원\s*여부|호환\s*여부",
            re.IGNORECASE,
        ),
    ),
)

SIZE_PATTERN = re.compile(r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*(?:인치|inch|\")", re.IGNORECASE)
MODEL_PATTERN = re.compile(
    r"\b(?=[A-Z0-9-]{5,}\b)(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9-]+\b",
    re.IGNORECASE,
)
DISTINCTIVE_PRODUCT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]{2,}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]+",
    re.IGNORECASE,
)
BRAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SAMSUNG", re.compile(r"삼성|SAMSUNG", re.IGNORECASE)),
    ("LG", re.compile(r"(?:^|\W)LG(?:\W|$)|엘지", re.IGNORECASE)),
    ("HANSUNG", re.compile(r"한성|HANSUNG", re.IGNORECASE)),
)
MODEL_STOPWORDS = {
    "SMART", "UHD4K", "QLED4K", "OLED4K", "HDMI2", "HDMI20", "HDMI21",
}
# A measurement is not a model code. MODEL_PATTERN accepts any 5+ character
# run mixing letters and digits, so "214cm(85인치)" in a product title yields
# the model code "214CM" -- and two different listings of the same size then
# match as EXPLICIT_MODEL_CODE_MATCH, which is the strongest identity verdict
# the gate can issue. Real operational rows show this: "214CM" is stored as
# the model code for both the 벽걸이 and the 스탠드 listing, so a stand or VESA
# fact from one would have settled the other. Dimensions are excluded here
# rather than repaired in the data, because the fallback -- product_id and
# distinctive-name matching -- is already correct and already in place.
# Moved to ``product_fact_guard`` so the rule sits beside the pattern it
# corrects and one module owns it. Re-exported here for existing readers.
_is_dimension_token = is_dimension_token


@dataclass(frozen=True)
class ProductIdentity:
    product_id: str | None
    product_name: str | None
    normalized_name: str | None
    model_code: str | None
    size_inches: float | None
    category: str | None
    family: str | None
    option: str | None
    brand: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KnowledgeProfile:
    scope: str
    topics: tuple[str, ...]
    strict_product_fact: bool
    variant_sensitive: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompatibilityDecision:
    eligible: bool
    hard_reject: bool
    reject_reason: str | None
    score_adjustment: float
    product_scope: str
    product_match: str
    product_match_reason: str
    query_topics: tuple[str, ...]
    candidate_topics: tuple[str, ...]
    topic_match: str
    topic_match_reason: str
    current_product: ProductIdentity
    candidate_product: ProductIdentity

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["current_product"] = self.current_product.to_dict()
        result["candidate_product"] = self.candidate_product.to_dict()
        return result


@dataclass(frozen=True)
class AnswerRelevanceDecision:
    status: str
    reason: str
    question_topics: tuple[str, ...]
    answer_topics: tuple[str, ...]
    uncovered_question_topics: tuple[str, ...]
    unrelated_answer_topics: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(value: object) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text or None


def _model_code(*values: object) -> str | None:
    # Only the first two inputs are explicit model-code fields.  Product names
    # and options must be pattern-scanned; treating the whole display name as a
    # model code makes unrelated legacy products appear to be exact models.
    for value in values[:2]:
        explicit = _clean(value)
        if explicit:
            code = explicit.upper()
            if code not in MODEL_STOPWORDS and not _is_dimension_token(code):
                return code
    for value in values[2:]:
        text = str(value or "").upper()
        candidates = [
            match for match in MODEL_PATTERN.findall(text)
            if match.upper() not in MODEL_STOPWORDS
            and not _is_dimension_token(match)
            and extract_model_code(match) is not None
        ]
        if candidates:
            return max(candidates, key=len).upper()
    return None


def _size(*values: object) -> float | None:
    for value in values:
        match = SIZE_PATTERN.search(str(value or ""))
        if match:
            size = float(match.group(1))
            if 10 <= size <= 200:
                return size
    return None


def _category(*values: object) -> str | None:
    text = " ".join(str(value or "") for value in values).lower()
    if re.search(r"모니터|monitor", text, re.IGNORECASE):
        return "MONITOR"
    if re.search(r"텔레비전|티비|(?:^|\W)tv(?:\W|$)|uhd|qled|oled", text, re.IGNORECASE):
        return "TV"
    if re.search(r"냉장고|refrigerator", text, re.IGNORECASE):
        return "REFRIGERATOR"
    if re.search(r"세탁기|washer", text, re.IGNORECASE):
        return "WASHER"
    if re.search(r"에어컨|air\s*conditioner", text, re.IGNORECASE):
        return "AIR_CONDITIONER"
    return None


def _brand(*values: object) -> str | None:
    text = " ".join(str(value or "") for value in values)
    return next((name for name, pattern in BRAND_PATTERNS if pattern.search(text)), None)


def classify_topics(value: object) -> tuple[str, ...]:
    text = str(value or "")
    topics = [topic for topic, pattern in TOPIC_PATTERNS if pattern.search(text)]
    specialized = [topic for topic in topics if topic not in GENERIC_TOPICS]
    if specialized:
        topics = [topic for topic in topics if topic not in {"GENERAL_POLICY", "PRODUCT_SPEC"}]
    return tuple(dict.fromkeys(topics or ["OTHER"]))


def extract_product_identity(
    *,
    product_id: object = None,
    product_name: object = None,
    model_code: object = None,
    option: object = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProductIdentity:
    metadata = metadata or {}
    stored_identity = metadata.get("product_identity")
    stored_identity = (
        stored_identity if isinstance(stored_identity, Mapping) else {}
    )
    name = _clean(
        product_name or metadata.get("product_name")
        or stored_identity.get("product_name")
    )
    option_text = _clean(
        option or metadata.get("option_name") or metadata.get("option")
        or stored_identity.get("option")
    )
    explicit_category = _clean(
        metadata.get("product_category") or metadata.get("category")
        or stored_identity.get("category")
    )
    return ProductIdentity(
        product_id=_clean(
            product_id or metadata.get("product_id")
            or stored_identity.get("product_id")
        ),
        product_name=name,
        normalized_name=(normalize_product_name(name) or None) if name else None,
        model_code=_model_code(
            model_code,
            metadata.get("model_code") or stored_identity.get("model_code"),
            name,
            option_text,
        ),
        size_inches=_size(
            metadata.get("size_variant") or stored_identity.get("size_inches"),
            option_text,
            name,
        ),
        category=(explicit_category.upper() if explicit_category else _category(name, option_text)),
        family=_clean(
            metadata.get("product_family") or stored_identity.get("family")
        ),
        option=option_text,
        brand=_brand(name, option_text),
    )


def profile_knowledge(
    *,
    question: object,
    answer: object,
    identity: ProductIdentity,
    metadata: Mapping[str, Any] | None = None,
) -> KnowledgeProfile:
    metadata = metadata or {}
    question_topics = classify_topics(question)
    answer_topics = classify_topics(answer)
    topics = tuple(dict.fromkeys((*question_topics, *answer_topics)))
    strict = bool(set(topics) & STRICT_PRODUCT_TOPICS)
    variant_sensitive = bool(
        set(topics) & {"STAND_BRACKET_VESA", "DISPLAY_PANEL", "ACCESSORY_COMPONENT"}
        or identity.size_inches is not None
        or identity.option is not None
    )
    explicit_scope = str(metadata.get("product_scope") or "").upper()
    if explicit_scope in PRODUCT_SCOPES:
        scope = explicit_scope
    elif strict and identity.model_code:
        scope = "MODEL"
    elif strict and (identity.product_id or variant_sensitive):
        scope = "VARIANT"
    elif set(topics) & POLICY_TOPICS and not strict:
        scope = "POLICY"
    elif strict:
        scope = "CATEGORY"
    elif identity.category:
        scope = "PRODUCT_FAMILY"
    else:
        scope = "GLOBAL"
    return KnowledgeProfile(
        scope=scope,
        topics=topics,
        strict_product_fact=strict,
        variant_sensitive=variant_sensitive,
    )


def attributes_of(text: object) -> frozenset[str]:
    """Which properties of the subject this text is about."""

    body = str(text or "")
    return frozenset(
        name for name, pattern in ATTRIBUTE_PATTERNS if pattern.search(body)
    )


def _attribute_conflict(
    query_text: str, candidate_text: str
) -> tuple[str, str] | None:
    """Whether the two are about different properties of the same subject.

    Both sides must be specific and they must share *nothing*. Sharing one
    attribute is enough to keep them together: the customer who asked which
    seller name to enter also reported being told to correct it, so their
    question carries CHANGE_REQUEST as well as SELLER_IDENTITY, and the stored
    seller-name answer carries METHOD as well. Requiring full coverage in both
    directions rejected the one answer that actually answered them.

    A side with no attribute at all is generic guidance and is left alone --
    rejecting it would throw away most of the reusable corpus, which is the
    false rejection this work exists to avoid rather than cause.
    """

    query = attributes_of(query_text)
    candidate = attributes_of(candidate_text)
    if not query or not candidate or query & candidate:
        return None
    return sorted(query)[0], sorted(candidate)[0]


def _topic_compatibility(
    query_topics: tuple[str, ...], candidate_topics: tuple[str, ...]
) -> tuple[bool, str, str, float]:
    query = set(query_topics) - {"OTHER"}
    candidate = set(candidate_topics) - {"OTHER"}
    query_specific = query - GENERIC_TOPICS
    candidate_specific = candidate - GENERIC_TOPICS
    if query_specific:
        if not query_specific & candidate_specific:
            return False, "MISMATCH", "TOPIC_MISMATCH", 0.0
        if len(query_specific) > 1 and not query_specific.issubset(candidate_specific):
            return False, "PARTIAL", "TOPIC_PARTIAL_COVERAGE", 0.0
        return True, "MATCH", "SPECIALIZED_TOPIC_MATCH", 0.0
    if "PRODUCT_SPEC" in query and candidate_specific & POLICY_TOPICS:
        return False, "MISMATCH", "TOPIC_MISMATCH", 0.0
    if "GENERAL_POLICY" in query and candidate_specific & STRICT_PRODUCT_TOPICS:
        return False, "MISMATCH", "TOPIC_MISMATCH", 0.0
    if query and candidate and not query & candidate:
        return True, "UNCERTAIN", "NO_SHARED_EXPLICIT_TOPIC", -0.10
    return True, "MATCH" if query & candidate else "UNCERTAIN", (
        "TOPIC_MATCH" if query & candidate else "TOPIC_UNCLEAR"
    ), 0.0 if query & candidate else -0.08


class LearningCompatibilityService:
    """Shared precision-first product and topic gate for runtime Learning."""

    def evaluate(
        self,
        *,
        current_question: object,
        current_product: ProductIdentity,
        candidate_question: object,
        candidate_answer: object,
        candidate_product: ProductIdentity,
        candidate_metadata: Mapping[str, Any] | None = None,
        authority: str = "AUTO",
        query_is_product_fact: bool = False,
    ) -> CompatibilityDecision:
        """Whether this candidate may stand beside this inquiry, and how well.

        ``query_is_product_fact`` says the *customer* asked for a specification.
        Strictness was read only off the candidate's own text before, so a
        candidate that reads like general guidance was judged leniently even
        when the question was "이 제품 해상도가 4K UHD 맞나요?" -- and a
        43-inch panel's answer could sit beside a 50-inch listing. The topic
        gate happened to remove most of those, which is why it went unnoticed;
        with that gate demoted to a ranking signal, identity has to be the
        thing that holds, and it needs to know what was asked.

        The caller already computes this (``classify_product_fact`` in
        ``learning_context_service``) and has been passing it into ``search``
        as ``product_fact_sensitive`` where nothing read it. This connects the
        existing signal rather than deriving a new one.
        """

        metadata = candidate_metadata or {}
        query_topics = classify_topics(current_question)
        profile = profile_knowledge(
            question=candidate_question,
            answer=candidate_answer,
            identity=candidate_product,
            metadata=metadata,
        )
        topic_ok, topic_match, topic_reason, topic_adjustment = _topic_compatibility(
            query_topics, profile.topics
        )
        # Same subject, different property. Checked after the topic gate and
        # reported through it, because "about 온누리 but about its period, not
        # its seller" is a topic mismatch in every sense that matters here --
        # it just needed a second dimension to be visible at all.
        attribute_conflict = _attribute_conflict(
            str(current_question or ""),
            " ".join(
                str(value or "")
                for value in (candidate_question, candidate_answer)
            ),
        )
        if topic_ok and attribute_conflict is not None:
            asked, offered = attribute_conflict
            topic_ok = False
            topic_match = "MISMATCH"
            topic_reason = f"ATTRIBUTE_MISMATCH_{asked}_VS_{offered}"
        # A topic or attribute mismatch no longer returns here.
        #
        # Both are read off anchor tables over the customer's wording, so they
        # answer "do these two texts look like they are about the same thing" --
        # a judgement about meaning, made lexically. Returning
        # ``hard_reject=True`` made them survive the ``hard_conflicts_only``
        # filter in ``SimilarAnswerService``, and they then removed candidates
        # before GPT ② could read them: 499 of 561 safe candidates on one
        # measured inquiry, for a question whose answer was in the store.
        #
        # Returning early was also what made the ordering wrong. The identity
        # checks below are the ones that must never be skipped -- a different
        # model's specification is wrong however relevant it reads -- and they
        # sat *after* the topic gate, so softening the gate in place would have
        # let a cross-model candidate through on a topic it happened to miss.
        # Identity is settled first now, and the topic finding is carried into
        # whatever that decides: a hard reject keeps its reason, and an eligible
        # candidate keeps the mismatch as a ranking penalty and a recorded
        # label for GPT ② to read.
        # A product-spec question may legitimately need a company policy or
        # procedure candidate (for example collection or service handling).
        # The candidate's persisted scope, not a topic keyword inferred from
        # the new question, determines whether its contents are a model fact.
        # MODEL/VARIANT facts stay strict; POLICY/GLOBAL evidence reaches GPT②
        # for semantic relevance and sufficiency judgement.
        explicit_scope = str(metadata.get("product_scope") or "").upper()
        # A missing scope is not evidence that a row is a general policy.  In
        # particular, many pre-scope Learning rows carry an explicit source
        # model code while their wording is too sparse for the old lexical
        # profiler to recognise a specification.  If GPT① says the customer
        # needs a product fact, that explicit model provenance makes a
        # different model mechanically incompatible.  A source deliberately
        # persisted as POLICY/GLOBAL remains recallable for GPT②; provenance
        # alone never suppresses it.
        strict = profile.strict_product_fact or bool(
            query_is_product_fact
            and (
                profile.scope not in {"POLICY", "GLOBAL"}
                or (
                    bool(candidate_product.model_code)
                    and explicit_scope not in {"POLICY", "GLOBAL"}
                )
            )
        )
        variant = profile.variant_sensitive or profile.scope in {"MODEL", "VARIANT"}
        current = current_product
        candidate = candidate_product

        def reject(reason: str, product_reason: str) -> CompatibilityDecision:
            """Identity is not established. Report what differs; decide nothing.

            ``eligible`` stays False, so the feedback-signal path -- where a
            VERIFIED fact would otherwise be applied by CODE -- behaves exactly
            as before. For Learning, production retrieval keeps the row
            (``hard_reject`` is soft) and the identity facts travel to GPT ②
            as provenance.

            The label says only what is known: a different listing id is not
            a different model, and no identity at all is not a different
            product -- neither costs rank. Only a stated contradiction
            (``MISMATCH``) keeps ``IDENTITY_MISMATCH_PENALTY``.
            """

            product_match = {
                "SOURCE_PRODUCT_ID_MISMATCH": "DIFFERENT_LISTING",
                "STRICT_FACT_REQUIRES_EXACT_PRODUCT_MODEL_OR_VARIANT": (
                    "IDENTITY_UNKNOWN"
                ),
            }.get(product_reason, "MISMATCH")
            identity_penalty = (
                IDENTITY_MISMATCH_PENALTY if product_match == "MISMATCH" else 0.0
            )
            return CompatibilityDecision(
                False, False, reason,
                identity_penalty + (0.0 if topic_ok else TOPIC_MISMATCH_PENALTY),
                profile.scope, product_match,
                product_reason, query_topics, profile.topics, topic_match,
                topic_reason, current, candidate,
            )

        def accept(
            product_match: str,
            product_reason: str,
            adjustment: float,
        ) -> CompatibilityDecision:
            """Identity cleared. Report the topic finding without enforcing it.

            A topic or attribute mismatch still makes the candidate ineligible,
            so the legacy path -- which removes anything ineligible -- behaves
            exactly as it did. What changes is ``hard_reject``: the finding is
            soft, so ``hard_conflicts_only`` (the production retrieval mode)
            keeps the candidate and lets GPT ② read it.
            """

            if topic_ok:
                return CompatibilityDecision(
                    True, False, None, adjustment, profile.scope,
                    product_match, product_reason, query_topics,
                    profile.topics, topic_match, topic_reason,
                    current, candidate,
                )
            return CompatibilityDecision(
                False, False, topic_reason, adjustment + TOPIC_MISMATCH_PENALTY,
                profile.scope, product_match, product_reason, query_topics,
                profile.topics, topic_match, topic_reason, current, candidate,
            )

        if strict and current.brand and candidate.brand and current.brand != candidate.brand:
            return reject("PRODUCT_CATEGORY_MISMATCH", "BRAND_MISMATCH")
        if strict and current.category and candidate.category and current.category != candidate.category:
            return reject("PRODUCT_CATEGORY_MISMATCH", "CATEGORY_MISMATCH")
        if (
            strict and variant and current.size_inches is not None
            and candidate.size_inches is not None
            and current.size_inches != candidate.size_inches
        ):
            return reject("PRODUCT_VARIANT_MISMATCH", "EXPLICIT_SIZE_MISMATCH")
        if strict and current.model_code and candidate.model_code:
            if current.model_code != candidate.model_code:
                return reject("MODEL_MISMATCH", "EXPLICIT_MODEL_CODE_MISMATCH")
            if (
                variant and current.size_inches is not None
                and candidate.size_inches is not None
                and current.size_inches != candidate.size_inches
            ):
                return reject("PRODUCT_VARIANT_MISMATCH", "EXPLICIT_SIZE_MISMATCH")
            return accept(
                "EXACT_MODEL", "EXPLICIT_MODEL_CODE_MATCH", topic_adjustment,
            )
        if current.product_id and candidate.product_id:
            if current.product_id == candidate.product_id:
                if (
                    variant and current.size_inches is not None
                    and candidate.size_inches is not None
                    and current.size_inches != candidate.size_inches
                ):
                    return reject("PRODUCT_VARIANT_MISMATCH", "EXPLICIT_SIZE_MISMATCH")
                return accept(
                    "EXACT_PRODUCT", "SOURCE_PRODUCT_ID_MATCH", topic_adjustment,
                )
            if strict:
                return reject("MODEL_MISMATCH", "SOURCE_PRODUCT_ID_MISMATCH")
        exact_distinctive_name = bool(
            strict
            and current.normalized_name
            and current.normalized_name == candidate.normalized_name
            and DISTINCTIVE_PRODUCT_TOKEN.search(current.product_name or "")
        )
        if exact_distinctive_name:
            return accept(
                "EXACT_NAME", "DISTINCTIVE_NORMALIZED_PRODUCT_NAME_MATCH",
                topic_adjustment - 0.02,
            )
        if strict:
            return reject(
                "INSUFFICIENT_PRODUCT_IDENTITY",
                "STRICT_FACT_REQUIRES_EXACT_PRODUCT_MODEL_OR_VARIANT",
            )

        adjustment = topic_adjustment
        product_match = "POLICY_COMPATIBLE"
        product_reason = "PRODUCT_INDEPENDENT_POLICY_OR_GENERAL_KNOWLEDGE"
        if current.category and candidate.category and current.category != candidate.category:
            adjustment -= 0.05
            product_match = "CATEGORY_UNCERTAIN"
            product_reason = "POLICY_CATEGORY_DIFFERS_SOFT_PENALTY"
        elif current.product_id and candidate.product_id and current.product_id == candidate.product_id:
            product_match = "EXACT_PRODUCT"
            product_reason = "SOURCE_PRODUCT_ID_MATCH"
        elif current.normalized_name and current.normalized_name == candidate.normalized_name:
            product_match = "EXACT_NAME"
            product_reason = "NORMALIZED_PRODUCT_NAME_MATCH"
        return accept(product_match, product_reason, adjustment)

    def answer_relevance(
        self, *, questions: Iterable[object], answer: object
    ) -> AnswerRelevanceDecision:
        question_topics = tuple(dict.fromkeys(
            topic
            for question in questions
            for topic in classify_topics(question)
            if topic != "OTHER"
        ))
        answer_topics = tuple(
            topic for topic in classify_topics(answer) if topic != "OTHER"
        )
        required = set(question_topics) - GENERIC_TOPICS
        supplied = set(answer_topics) - GENERIC_TOPICS
        uncovered = tuple(sorted(required - supplied))
        unrelated = tuple(sorted(supplied - required)) if required else ()
        if required and supplied and not required & supplied:
            status = "BLOCK"
            reason = "ANSWER_TOPIC_MISMATCH"
        elif len(required) > 1 and uncovered:
            status = "REVIEW_REQUIRED"
            reason = "COMPOUND_QUESTION_PARTIAL_COVERAGE"
        elif required and not supplied:
            status = "REVIEW_REQUIRED"
            reason = "ANSWER_TOPIC_NOT_DEMONSTRATED"
        elif required and unrelated:
            # Reached only once every topic the customer asked about is
            # covered -- the earlier branches take the mismatch, the partial
            # compound and the nothing-supplied cases. What is left is an
            # answer that answered everything and also mentioned adjacent
            # context: "패널은 2년" alongside the A/S warranty, "설치예정일"
            # alongside the delivery question. That is ordinary helpful
            # answering, not a safety finding, and any real risk inside the
            # extra content is still caught by fact grounding, date
            # grounding, privacy and prohibited-content rules. Recorded for
            # staff, but it no longer holds the answer back on its own.
            status = "WARN"
            reason = "ANSWER_CONTAINS_UNREQUESTED_TOPIC"
        else:
            status = "PASS"
            reason = "ANSWER_TOPIC_COMPATIBLE"
        return AnswerRelevanceDecision(
            status=status,
            reason=reason,
            question_topics=question_topics,
            answer_topics=answer_topics,
            uncovered_question_topics=uncovered,
            unrelated_answer_topics=unrelated,
        )
