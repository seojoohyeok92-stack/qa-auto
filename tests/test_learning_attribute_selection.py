"""A stored answer about a different property is not evidence for this one.

Identity settles *which product* a row is about. It says nothing about *which
property*, and on 690027174 -- where is this 27-inch monitor made -- the rows
that survived identity were about review points, an SK Broadband set-top, the
온누리 gift certificate and a DP cable. Every one of them passed because it named
the right product; not one of them states an origin.

So the question's attribute and the row's attribute are named in one vocabulary
-- the families Product Knowledge already uses to route a question to a stored
field -- and a row is removed only when both sides are known and they disagree.
Both halves matter. "Undetermined" is not "unrelated": a row whose subject
cannot be named is left to the ranking exactly as before, recorded as
ATTRIBUTE_UNRESOLVED, because blocking on silence would delete the operational
answers that carry no family keyword at all.

The gate runs only where Gate 1 runs -- on an atom GPT ① labelled a product-fact
action -- so installation, delivery and policy answers never meet it. 688218182
is the case that keeps it honest: the store's own answer about who installs a
wall mount and what it costs must still arrive.
"""

from __future__ import annotations

import pytest

from services.learning_context_service import PRODUCT_FACT_ACTIONS
from services.product_knowledge_service import attribute_families

ORIGIN_QUERY = (
    "27인치 모니터의 생산 국가 27인치 모니터의 생산지가 어디인가요?"
    " 27인치 모니터의 제조국 또는 생산지에 대한 안내"
)


def _blocked(query_text: str, candidate_text: str, *, action: str) -> bool:
    """The rule as ``SimilarAnswerService`` applies it."""

    if action not in PRODUCT_FACT_ACTIONS:
        return False
    query = attribute_families(query_text)
    candidate = attribute_families(candidate_text)
    return bool(query and candidate and not (query & candidate))


# --- the vocabulary is Product Knowledge's, not a second one ---------------

def test_families_are_read_from_the_product_knowledge_table():
    """The field router's own families, reused by name."""

    from services.product_knowledge_service import _ATTRIBUTE_FAMILIES

    names = {family for family, _keywords, _targets in _ATTRIBUTE_FAMILIES}
    assert {"ORIGIN", "VESA", "SPEAKER", "DIMENSIONS"} <= names
    assert attribute_families("생산지가 어디인가요") == frozenset({"ORIGIN"})
    assert attribute_families("베사홀 규격") == frozenset({"VESA"})


def test_an_unnameable_subject_is_empty_not_wrong():
    assert attribute_families("") == frozenset()
    assert attribute_families("안녕하세요 고객님") == frozenset()


def test_an_exclusion_is_not_a_mention():
    """``_is_excluded_mention`` still applies, as in the field router.

    The marker has to fall within six characters of the term, which is the
    window the field router uses; further away it is a separate clause and the
    mention stands.
    """

    assert "VESA" not in attribute_families("베사 빼고 알려주세요")
    assert "VESA" in attribute_families("베사 규격은 빼고 다른 것도 알려주세요")


# --- 690027174: the four rows that survived identity -----------------------

@pytest.mark.parametrize("label,answer", [
    ("review points",
     "리뷰 작성 후 네이버페이 포인트 적립은 폼 입력 확인 후 지급됩니다."),
    ("SK Broadband",
     "SK브로드밴드 셋톱박스는 해당 방송사 기사님이 방문하여 설치해 드립니다."),
    ("온누리",
     "온누리상품권 지급은 구매 확정 후 순차적으로 진행됩니다."),
    ("DP cable",
     "DP케이블 불량은 교환 접수 후 회수하여 처리해 드립니다."),
])
def test_an_origin_question_removes_a_row_about_something_else(label, answer):
    assert _blocked(ORIGIN_QUERY, answer, action="PRODUCT_SPEC"), label


def test_an_origin_question_keeps_a_row_about_origin():
    answer = "해당 제품의 제조국은 한국이며 일부 물량은 베트남에서 생산됩니다."

    assert not _blocked(ORIGIN_QUERY, answer, action="PRODUCT_SPEC")


def test_a_vesa_question_removes_an_audio_answer():
    assert _blocked("이 제품 베사홀 규격", "스피커 출력은 20W입니다.",
                    action="PRODUCT_SPEC")


# --- undetermined never blocks ---------------------------------------------

def test_an_unresolved_candidate_is_kept():
    """Fail-open, on purpose: silence is not a statement about the subject."""

    assert not _blocked(ORIGIN_QUERY, "네, 가능합니다. 감사합니다.",
                        action="PRODUCT_SPEC")


def test_an_unresolved_question_blocks_nothing():
    assert not _blocked("이거 어떤가요?", "스피커 출력은 20W입니다.",
                        action="PRODUCT_SPEC")


# --- operational and policy answers never meet the gate --------------------

@pytest.mark.parametrize("action", [
    "INSTALLATION_METHOD", "DELIVERY_POLICY", "DELIVERY_STATUS",
    "CANCEL_RETURN", "FORM_FIELD_GUIDANCE", "DELIVERY_DEADLINE_CONFIRMATION",
])
def test_a_non_product_fact_question_is_never_attribute_gated(action):
    """688218182's shape: the attributes differ and the row still stays."""

    assert action not in PRODUCT_FACT_ACTIONS
    assert not _blocked(
        "벽걸이 설치에 추가 비용이 있나요?",
        "해당 제품의 제조국은 한국입니다.", action=action)


def test_the_installation_answer_survives_an_installation_question():
    """L117: the store's own answer to exactly this question."""

    answer = (
        "벽걸이 추가하시게 될 경우 벽걸이용 브라켓이 함께 출고되며, "
        "설치비는 청구되지 않습니다. 삼성전자 기사님께서 설치까지 해드립니다.")
    query = "벽걸이 설치 추가 비용 벽걸이 설치에 추가 비용이 있나요?"

    assert attribute_families(query) & attribute_families(answer)
    assert not _blocked(query, answer, action="INSTALLATION_METHOD")


# --- what this gate deliberately does not decide ---------------------------

def test_the_same_attribute_from_another_listing_is_not_this_gates_business():
    """688218219 / the 32-inch M5 rows.

    They answer exactly the attribute that was asked -- package contents --
    for a different listing. That is a question about scope, which identity
    and ``PRODUCT_CATEGORY_MISMATCH`` govern, and this gate must not pretend
    to settle it by pattern-matching the subject.
    """

    query = "리모컨의 기본 구성품 포함 여부"
    answer = "해당 상품은 리모컨이 기본 구성품으로 함께 동봉되어 출고됩니다."

    assert attribute_families(query) & attribute_families(answer)
    assert not _blocked(query, answer, action="PACKAGE_CONTENTS")
