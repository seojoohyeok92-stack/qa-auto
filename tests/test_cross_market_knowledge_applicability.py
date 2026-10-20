"""Which Naver knowledge a Coupang question may be answered from.

``_SHARED_FORWARD`` opened the direction, and it opened all of it: a Learning
row's origin was the only thing consulted, and a row that says nothing about
its applicability is read as Naver's -- which every legacy row does.  On the
26.10.12 server snapshot that was the whole cross-market surface: of the 1,831
rows in the Coupang candidate pool, 1,066 were Naver rows and not one of them
carried applicability metadata.

Origin is the right question about a corpus and the wrong one about a
sentence.  Among those 1,066, once the company template frame was removed,
141 claims still routed the customer through a Naver-only surface -- 네이버폼,
톡톡, 네이버페이, 거래명세표, 스마트스토어, and the terse ones whose whole
answer is "네이버로 기재해주셔야합니다", which is the most directly wrong
thing that can cross: a Coupang buyer must write 쿠팡.  Asked "거래명세서 발급
받을 수 있나요?", all eight of the top-eight rows offered to a Coupang
inquiry were Naver document-procedure answers.

Two boundaries are pinned here as much as the block itself.

**The frame is not the claim.**  Every answer this system writes closes with a
footer naming 네이버 톡톡.  Judging whole answers condemned 307 rows, 211 of
them for that boilerplate alone -- including a model's Bluetooth
specification.  Product knowledge whose only Naver mention is the template
still crosses.

**Naming a market is not routing to it.**  The manufacturer's own procedure
tells the customer to enter 구매처 as "구매해주신 마켓(네이버,옥션,G마켓,쿠팡)";
that answer is one a Coupang buyer needs, and it names Coupang itself.
"""

from __future__ import annotations

import json

import pytest

from answer.cross_market_knowledge import (
    claim_text, crosses_markets_safely, foreign_market_channel,
)
from repositories.database import Database
from repositories.learning_repository import (
    LearningRepository, is_market_applicable, is_market_knowledge_shareable,
)

NAVER_STORE = "OJE_PLUS"
COUPANG_STORE = "COUPANG_OJE_NS"

# The real wrapper closing, which is what made whole-answer scanning useless.
FOOTER = (
    "\n\n안내드린 내용이 문의하신 내용과 다른 경우, "
    "네이버 톡톡으로 문의 남겨주시면 담당자가 확인 후 안내드리겠습니다."
)


def naver_row(answer: str, *, applicability: object = None) -> dict:
    """A legacy Naver Learning row: a store, and no applicability at all."""

    metadata: dict = {"facts_authority": "HUMAN_VERIFIED_NAVER_POSTED"}
    if applicability is not None:
        metadata["market_applicability"] = applicability
    return {
        "store_code": NAVER_STORE,
        "final_answer": answer,
        "metadata_json": metadata,
    }


def coupang_row(answer: str) -> dict:
    """A Coupang approval, as Thread C pinned it: no store, COUPANG_ONLY."""

    return {
        "store_code": None,
        "final_answer": answer,
        "metadata_json": {
            "origin_market": "COUPANG",
            "market_applicability": "COUPANG_ONLY",
            "facts_authority": "HUMAN_VERIFIED_MARKETPLACE_ANSWER",
        },
    }


# --- 1-2. product and model knowledge crosses -----------------------------

@pytest.mark.parametrize("answer", [
    pytest.param("이 제품의 소비전력은 1000W입니다.", id="spec"),
    pytest.param(
        "LS27D400 모델은 스피커가 내장되어 있지 않습니다.", id="model-spec"),
    pytest.param(
        "해당 모니터는 블루투스를 지원하므로 에어팟을 페어링하여 사용하실 수 "
        "있습니다.", id="model-feature"),
    pytest.param(
        "해당 모델은 VESA 100x100 규격이라 시중 벽걸이 브라켓과 호환됩니다.",
        id="compatibility"),
    pytest.param(
        "구성품은 본체, 스탠드, 전원 어댑터, 리모컨입니다.", id="contents"),
    pytest.param(
        "전원 버튼을 5초간 길게 누르면 초기화됩니다.", id="usage"),
    pytest.param(
        "제품 불량이 의심되는 경우 삼성전자 고객센터 1588-3366으로 A/S "
        "접수해 주시면 됩니다.", id="manufacturer-service"),
])
def test_product_knowledge_answers_a_coupang_question(answer) -> None:
    """Spec, feature, compatibility, contents, usage, maker's own service."""

    row = naver_row(answer + FOOTER)
    assert is_market_knowledge_shareable(row, "COUPANG") is True


def test_the_template_footer_alone_never_withholds_a_claim() -> None:
    """211 of the 307 whole-answer matches were only ever the boilerplate."""

    answer = "이 제품의 소비전력은 1000W입니다." + FOOTER
    assert "네이버 톡톡" in answer
    assert "네이버" not in claim_text(answer)
    assert is_market_knowledge_shareable(naver_row(answer), "COUPANG") is True


# --- 3-6. another market's channel and procedures do not cross ------------

@pytest.mark.parametrize("answer,why", [
    pytest.param(
        "거래명세서는 네이버쇼핑 주문내역을 캡쳐해서 제출하시면 됩니다.",
        "document", id="document-procedure"),
    pytest.param(
        "별도의 거래명세표 배너나 링크는 운영하지 않습니다.",
        "document", id="document-statement"),
    pytest.param(
        "자세한 내용은 네이버 톡톡으로 문의해주세요.", "channel", id="talktalk"),
    pytest.param(
        "해당 상품은 스마트스토어에서 구매하실 수 있습니다.",
        "channel", id="smartstore"),
    pytest.param(
        "구매내역서는 네이버페이 > 결제내역 > 주문 상세정보 캡쳐로 확인 "
        "가능합니다.", "channel", id="naverpay"),
    pytest.param(
        "리뷰 작성 후 네이버폼까지 접수해주셔야 정상 참여됩니다.",
        "channel", id="naverform"),
    pytest.param(
        "주문번호와 상품주문번호는 서로 다르게 부여됩니다.",
        "order-process", id="product-order-number"),
    pytest.param(
        "반품은 네이버 결제내역에서 반품 신청해 주세요.",
        "order-process", id="return-procedure"),
    pytest.param(
        "교환은 네이버 주문내역에서 교환 신청 후 회수 기사님께 전달해 주세요.",
        "order-process", id="exchange-procedure"),
    pytest.param(
        "https://smartstore.naver.com/samsungpartner/products/123 "
        "구매 링크 남겨드립니다.", "channel", id="storefront-link"),
    pytest.param("구매처 항목은 '네이버'로 선택해 주세요.", "value", id="as-value"),
    pytest.param("네이버로 기재해주셔야합니다.", "value", id="as-value-terse"),
    pytest.param("네이버입니다.", "value", id="bare-market-name"),
])
def test_another_markets_channel_does_not_answer_coupang(answer, why) -> None:
    row = naver_row(answer + FOOTER)
    assert is_market_knowledge_shareable(row, "COUPANG") is False, why
    assert foreign_market_channel(claim_text(answer), "COUPANG") == "NAVER"


def test_a_market_neutral_policy_still_crosses() -> None:
    """Consumer law is not one marketplace's procedure.

    The forbidden thing is a channel, not the word 반품: blocking the topic
    would withhold what every marketplace's customer is owed.
    """

    row = naver_row("반품은 상품 수령 후 7일 이내에 가능합니다." + FOOTER)
    assert is_market_knowledge_shareable(row, "COUPANG") is True


def test_a_procedure_that_names_coupang_too_is_not_foreign() -> None:
    """The manufacturer's own form, which lists every marketplace."""

    answer = (
        "삼성닷컴 환급 신청 페이지에서 구매처를 실제 구매하신 "
        "마켓(네이버·옥션·G마켓·쿠팡 등)으로 선택해 주세요." + FOOTER
    )
    assert is_market_knowledge_shareable(naver_row(answer), "COUPANG") is True
    # Read from Naver it is equally not foreign, for the same reason.
    assert foreign_market_channel(claim_text(answer), "NAVER") == ""


# --- 7. the closed direction stays closed --------------------------------

def test_coupang_learning_never_answers_a_naver_question() -> None:
    row = coupang_row("쿠팡 구매내역에서 영수증을 출력하실 수 있습니다.")
    assert is_market_knowledge_shareable(row, "NAVER") is False
    assert is_market_knowledge_shareable(row, "COUPANG") is True


def test_a_coupang_row_is_not_gated_by_its_own_channel_words() -> None:
    """Same-market reads are never content-gated.

    A Coupang answer naming 쿠팡 윙 is Coupang's own knowledge; the gate asks
    about crossing, and nothing is being crossed.
    """

    row = coupang_row("쿠팡 윙에서 확인하실 수 있습니다.")
    assert is_market_knowledge_shareable(row, "COUPANG") is True
    assert crosses_markets_safely(
        claim="쿠팡 윙에서 확인하실 수 있습니다.",
        origin_market="COUPANG", market="COUPANG") is True


def test_a_naver_question_reads_its_own_corpus_unchanged() -> None:
    """The Naver pool must not shrink: this gate is about crossing only."""

    for answer in (
        "자세한 내용은 네이버 톡톡으로 문의해주세요.",
        "거래명세서는 네이버쇼핑 주문내역을 캡쳐해서 제출하시면 됩니다.",
        "구매처 항목은 '네이버'로 선택해 주세요.",
        "이 제품의 소비전력은 1000W입니다.",
    ):
        row = naver_row(answer + FOOTER)
        assert is_market_knowledge_shareable(row, "NAVER") is True, answer


# --- 8. legacy metadata absence is not a licence --------------------------

def test_legacy_rows_are_not_treated_as_common() -> None:
    """No applicability means the claim is read, not that anything goes.

    All 1,066 Naver rows in the measured Coupang pool are this case, so this
    is the whole policy rather than an edge of it.
    """

    channel = naver_row("네이버 톡톡으로 문의해주세요." + FOOTER)
    assert channel["metadata_json"].get("market_applicability") is None
    # The origin rule alone still admits it -- that is the gap being closed.
    assert is_market_applicable(channel["metadata_json"], "COUPANG") is True
    assert is_market_knowledge_shareable(channel, "COUPANG") is False


def test_an_explicit_applicability_keeps_its_authority() -> None:
    """COMMON is an operator's decision and outranks the content read.

    Pinned because it is the existing policy, not a new one: a row that
    states its own scope is believed in both directions, and this gate was
    added underneath that rule rather than in front of it.
    """

    stated = naver_row(
        "네이버 톡톡으로 문의해주세요." + FOOTER, applicability="COMMON")
    assert is_market_knowledge_shareable(stated, "COUPANG") is True

    withheld = naver_row(
        "이 제품의 소비전력은 1000W입니다." + FOOTER,
        applicability="NAVER_ONLY")
    assert is_market_knowledge_shareable(withheld, "COUPANG") is False


# --- 9. origin is recorded, not rewritten --------------------------------

def test_the_gate_never_rewrites_a_rows_origin() -> None:
    """Applicability is a second question, not an edit to the first.

    Thread C pinned that a Coupang approval is stored with
    ``origin_market`` COUPANG; nothing here may relabel a row COMMON to let
    it travel.
    """

    row = coupang_row("해당 제품 소비전력은 1800W입니다.")
    before = dict(row["metadata_json"])
    is_market_knowledge_shareable(row, "NAVER")
    is_market_knowledge_shareable(row, "COUPANG")
    assert row["metadata_json"] == before
    assert row["metadata_json"]["origin_market"] == "COUPANG"


# --- 10. the whole pool, through the production query --------------------

@pytest.fixture
def database(tmp_path) -> Database:
    built = Database(tmp_path / "cross-market.db")
    built.initialize()
    return built


def _seed(database: Database, key: str, *, store_code, answer: str,
          question: str, metadata: dict) -> str:
    """One Learning row, seeded the way the existing policy test seeds them."""

    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO learning_examples(
                source_key, learning_source, question_original_masked,
                question_normalized, store_code, inquiry_type, product_name,
                model_code, final_answer, seller_answer, posted, rating,
                edit_ratio, quality_score, style_only, version, metadata_json,
                active, usage_count, validity_type, validity_active,
                created_at, updated_at
            ) VALUES (?, 'APPROVED_UNEDITED', ?, ?, ?, 'PRODUCT_INQUIRY',
                      '삼성 32인치 모니터', 'S32DM501', ?, ?,
                      1, 5, 0.0, 1.0, 0, 1, ?, 1, 0, 'PERMANENT', 1,
                      '2026-10-10T00:00:00.000Z', '2026-10-10T00:00:00.000Z')
            """,
            (key, question, question, store_code, answer, answer,
             json.dumps({"learning_signal_type": "POSITIVE",
                         "human_verified": True, **metadata},
                        ensure_ascii=False)),
        )
    return key


def test_the_coupang_candidate_pool_carries_no_naver_channel(
    database,
) -> None:
    """End to end: what the pool admits is what reaches ranking and GPT②.

    ``candidates`` is the one place the market decision is made -- for a
    Coupang caller the store predicate is dropped entirely -- so a row kept
    out here is kept out of every later stage.
    """

    verified = {"facts_authority": "HUMAN_VERIFIED_NAVER_POSTED"}
    _seed(database, "spec", store_code=NAVER_STORE,
          question="소비전력이 얼마인가요?",
          answer="이 제품의 소비전력은 1000W입니다." + FOOTER, metadata=verified)
    _seed(database, "talktalk", store_code=NAVER_STORE,
          question="문의는 어디로 하나요?",
          answer="자세한 내용은 네이버 톡톡으로 문의해주세요." + FOOTER,
          metadata=verified)
    _seed(database, "invoice", store_code=NAVER_STORE,
          question="거래명세서 주세요",
          answer="거래명세서는 네이버쇼핑 주문내역을 캡쳐해 제출해 주세요."
          + FOOTER, metadata=verified)
    _seed(database, "coupang-own", store_code=None,
          question="소비전력이 얼마인가요?",
          answer="해당 제품 소비전력은 1800W입니다.",
          metadata={"origin_market": "COUPANG",
                    "market_applicability": "COUPANG_ONLY",
                    "facts_authority": "HUMAN_VERIFIED_MARKETPLACE_ANSWER"})

    repository = LearningRepository(database)

    def pool(store_code: str, market: str) -> set[str]:
        return {
            str(item["source_key"]) for item in repository.candidates(
                store_code=store_code, market=market, limit=2000)
        }

    coupang = pool(COUPANG_STORE, "COUPANG")
    assert "spec" in coupang, "product knowledge must still cross"
    assert "coupang-own" in coupang
    assert "talktalk" not in coupang
    assert "invoice" not in coupang

    # The Naver pool is untouched, including the rows withheld above.
    naver = pool(NAVER_STORE, "NAVER")
    assert {"spec", "talktalk", "invoice"} <= naver
    assert "coupang-own" not in naver
