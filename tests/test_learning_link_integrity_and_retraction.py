"""Learning 의 문의-답변 연결 무결성과 정정 답변의 승계.

운영 DB 스냅숏(2026-10-22) 전수 감사에서 나온 두 결과를 고정한다.

연결. 활성 2,074행 중 다른 문의의 답변이나 질문이 붙은 행은 0건이었다. 질문은
언제나 ``_build`` 가 그 문의 행에서 만들고, 답변 후보는 모두 ``inquiry_id`` 로만
스코프된다. 다만 draft 와 historical case 는 자기 id 로 읽히기만 하고 그 문의의
것인지 확인되지 않았다 -- 지금 0건인 것은 보장이 아니라 상태다. 여기서는 그
확인을 고정한다.

중복. 같은 원본 답변이 8쌍 중복 저장돼 있었다. ``source_key`` 는
``sha256(learning_source|inquiry_id|masked_question|masked_answer)`` 이므로
마스킹 규칙이 바뀌거나(5쌍, 자사 공식번호가 더 이상 마스킹되지 않게 된 뒤)
다른 경로가 같은 답변을 다시 수집하면(3쌍) upsert 가 빗나가 두 번째 행이 생긴다.

정정. 네이버 한 문의에 판매자 댓글이 여러 개 달리면 각각이 독립된 활성
Learning 이 되고, 뒤 답변이 앞 답변을 틀렸다고 말해도 앞 답변의 자격은 그대로
남는다. 문의 2411 에서 철회된 주장이 GPT ② 프롬프트에 8회 들어갔다.

반대 방향도 함께 고정한다. 문의 2317 의 고객은 "자동 피벗"을 물었고 챗봇은
"피벗"을 답했다 -- PK 가 이 모델의 pivot 을 -92°~+92° 로 기록하므로 그 답변은
틀리지 않았다. 뒤이은 "추가답변 드립니다. 자동피벗기능은 탑재되어있지 않습니다"
는 다른 기능에 대한 보충이고 정정이 아니다. 뒤에 왔다는 사실만으로 승계시키면
맞는 답변을 버린다.
"""
from __future__ import annotations

import itertools
import json

import pytest

from repositories.database import Database
from repositories.learning_repository import (
    LearningRepository,
    origin_answer_key,
)
from services.learning_service import LearningLinkError, LearningService
from services.similar_answer_service import _drop_retracted_answers


_key = itertools.count()

# 운영 DB 의 실제 본문. 문의 2411 의 철회된 챗봇 답변과 그 정정.
RETRACTED_2411 = (
    "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
    "삼성 삼탠바이미 스마트 M5 모니터는 이어폰 또는 유선 헤드셋을 연결할 수 "
    "있는 3.5mm 오디오 잭을 제공하여 유선 헤드셋 연결이 가능합니다."
)
CORRECTION_2411 = (
    "안녕하세요, 고객님. 챗봇이 잘못된 답변을 드려 정정드립니다. "
    "문의주신 상품은 3.5mm 오디오 잭이 제공되지 않아 유선 헤드셋 연결은 "
    "어려운 점 양해 부탁드립니다."
)
# 문의 2317. 정정이 아니라 다른 하위 질문에 대한 추가 답변.
PIVOT_2317 = (
    "♣♧안녕하세요♧♣\n오제 챗봇(Chat Bot)이 답변드립니다.\n\n"
    "문의하신 S27D400 모델은 피벗 기능을 지원하여 화면을 세로 방향으로 돌려 "
    "사용할 수 있습니다."
)
SUPPLEMENT_2317 = "안녕하세요. 추가답변 드립니다.\n\n자동피벗기능은 탑재되어있지 않습니다."
# 문의 1903. 철회를 "혼동드린" 쪽 표현으로 말한 경우.
CORRECTION_1903 = (
    "안녕하세요 고객님\n챗봇 답변으로 혼동드린 점 죄송하며, 이벤트 "
    "유의사항에 나와있듯이 리뷰이벤트의 경우 계정당 1회 참여입니다."
)


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "link-integrity.db")
    value.initialize()
    return value


def add_inquiry(
    database: Database,
    *,
    store_code: str = "OJE_PLUS",
    source_type: str = "PRODUCT_INQUIRY",
    title: str = "상품 문의",
    content: str = "질문 내용",
    product_name: str = "삼성 에센셜 모니터 68.6cm(27인치) LS27D400",
    product_id: str = "10281062902",
    source_question_id: str | None = None,
) -> int:
    external = source_question_id or "Q%d" % next(_key)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO inquiries(
                store_code, source_type, source_question_id,
                external_inquiry_id, inquiry_type, title, content,
                product_name, product_id, raw_json, source_answered
            ) VALUES (?,?,?,?,'PRODUCT_INQUIRY',?,?,?,?,'{}',1)
            """,
            (store_code, source_type, external, external, title, content,
             product_name, product_id),
        )
        return int(
            connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def add_draft(
    database: Database, *, inquiry_id: int, final_answer: str,
    edited_answer: str | None = None, review_status: str = "APPROVED",
) -> int:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO answer_drafts(
                inquiry_id, original_answer, edited_answer, final_answer,
                review_status, validation_status, source, metadata_json
            ) VALUES (?,?,?,?,?,'PASSED','GPT','{}')
            """,
            (inquiry_id, "초안", edited_answer, final_answer, review_status),
        )
        return int(
            connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def metadata_of(row) -> dict:
    """저장된 행의 metadata. 경로에 따라 dict 또는 JSON 문자열로 돌아온다."""

    value = row["metadata_json"]
    return value if isinstance(value, dict) else json.loads(value or "{}")


def candidate(
    learning_id: int, *, inquiry_id: int | None, answer: str,
    created_at: str,
) -> tuple[float, dict[str, object]]:
    """검색 pool 의 한 후보. ``candidates()`` 가 넘기는 행 모양 그대로."""

    return (1.0, {
        "id": learning_id,
        "inquiry_id": inquiry_id,
        "final_answer": answer,
        "created_at": created_at,
    })


# ---------------------------------------------------------------- 정정 승계

def test_01_a_retraction_withdraws_the_answer_it_names() -> None:
    ranked = [
        candidate(152700, inquiry_id=2411, answer=RETRACTED_2411,
                  created_at="2026-08-17T22:38:10Z"),
        candidate(154436, inquiry_id=2411, answer=CORRECTION_2411,
                  created_at="2026-08-18T00:39:37Z"),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert [int(item["id"]) for _score, item in kept] == [154436]
    assert notes == [{
        "inquiry_id": 2411,
        "retracted_learning_id": 152700,
        "retracted_by_learning_id": 154436,
        "retracted_at": "2026-08-18T00:39:37Z",
    }]


def test_02_the_correction_itself_is_never_withdrawn() -> None:
    ranked = [
        candidate(152700, inquiry_id=2411, answer=RETRACTED_2411,
                  created_at="2026-08-17T22:38:10Z"),
        candidate(154436, inquiry_id=2411, answer=CORRECTION_2411,
                  created_at="2026-08-18T00:39:37Z"),
    ]
    kept, _notes = _drop_retracted_answers(ranked)

    assert any(item["final_answer"] == CORRECTION_2411
               for _score, item in kept)


def test_03_a_supplement_about_another_feature_does_not_retract() -> None:
    """문의 2317. 뒤에 왔지만 앞 답변을 틀렸다고 말하지 않는다."""

    ranked = [
        candidate(60957, inquiry_id=2317, answer=PIVOT_2317,
                  created_at="2026-08-14T01:06:52Z"),
        candidate(62358, inquiry_id=2317, answer=SUPPLEMENT_2317,
                  created_at="2026-08-14T02:26:52Z"),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert notes == []
    assert [int(item["id"]) for _score, item in kept] == [60957, 62358]


def test_04_the_hondong_wording_also_retracts() -> None:
    ranked = [
        candidate(105, inquiry_id=1903, answer="챗봇 답변입니다. 품목별로 지급됩니다.",
                  created_at="2026-08-06T00:21:26Z"),
        candidate(1215, inquiry_id=1903, answer=CORRECTION_1903,
                  created_at="2026-08-06T01:07:09Z"),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert [int(item["id"]) for _score, item in kept] == [1215]
    assert notes[0]["retracted_learning_id"] == 105


def test_05_a_retraction_reaches_only_its_own_inquiry() -> None:
    """다른 문의의 답변은 같은 상품이라도 철회되지 않는다."""

    ranked = [
        candidate(152700, inquiry_id=2411, answer=RETRACTED_2411,
                  created_at="2026-08-17T22:38:10Z"),
        candidate(900001, inquiry_id=9999, answer=RETRACTED_2411,
                  created_at="2026-08-17T22:38:10Z"),
        candidate(154436, inquiry_id=2411, answer=CORRECTION_2411,
                  created_at="2026-08-18T00:39:37Z"),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert [int(item["id"]) for _score, item in kept] == [900001, 154436]
    assert [note["inquiry_id"] for note in notes] == [2411]


def test_06_an_earlier_retraction_cannot_withdraw_a_later_answer() -> None:
    ranked = [
        candidate(154436, inquiry_id=2411, answer=CORRECTION_2411,
                  created_at="2026-08-18T00:39:37Z"),
        candidate(152700, inquiry_id=2411, answer=RETRACTED_2411,
                  created_at="2026-08-19T00:00:00Z"),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert notes == []
    assert len(kept) == 2


def test_07_a_lone_retraction_removes_nothing() -> None:
    """정정본만 pool 에 있으면 지울 대상이 없다. 규칙은 형제행으로만 발동한다."""

    ranked = [
        candidate(154436, inquiry_id=2411, answer=CORRECTION_2411,
                  created_at="2026-08-18T00:39:37Z"),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert notes == []
    assert len(kept) == 1


def test_08_rows_without_an_inquiry_are_left_alone() -> None:
    ranked = [
        candidate(2, inquiry_id=None, answer=RETRACTED_2411,
                  created_at="2026-08-01T00:00:00Z"),
        candidate(3, inquiry_id=None, answer=CORRECTION_2411,
                  created_at="2026-08-02T00:00:00Z"),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert notes == []
    assert len(kept) == 2


def test_09_an_undated_retraction_withdraws_nothing() -> None:
    """시각을 알 수 없으면 어느 쪽이 앞인지 모른다. 추정하지 않는다."""

    ranked = [
        candidate(152700, inquiry_id=2411, answer=RETRACTED_2411,
                  created_at="2026-08-17T22:38:10Z"),
        candidate(154436, inquiry_id=2411, answer=CORRECTION_2411,
                  created_at=""),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert notes == []
    assert len(kept) == 2


def test_10_an_address_correction_is_not_an_answer_retraction() -> None:
    ranked = [
        candidate(1, inquiry_id=4242, answer="배송은 2주 소요됩니다.",
                  created_at="2026-08-01T00:00:00Z"),
        candidate(2, inquiry_id=4242,
                  answer="주소를 정정해 반영 도와드렸습니다.",
                  created_at="2026-08-02T00:00:00Z"),
    ]
    kept, notes = _drop_retracted_answers(ranked)

    assert notes == []
    assert len(kept) == 2


# ------------------------------------------------- 저장 연결: draft ↔ 문의

def test_11_a_draft_from_another_inquiry_is_refused(database: Database) -> None:
    mine = add_inquiry(database, content="이 문의의 질문")
    theirs = add_inquiry(database, content="다른 고객의 질문")
    foreign_draft = add_draft(
        database, inquiry_id=theirs, final_answer="다른 고객에게 보낸 답변")
    service = LearningService(database)
    inquiry = service.inquiries.get(mine)
    draft = service.answers.get(foreign_draft)

    with pytest.raises(LearningLinkError) as error:
        service._build(
            inquiry=inquiry, draft=draft,
            learning_source="APPROVED_EDITED", answer="답변",
        )

    assert str(theirs) in str(error.value)
    assert str(mine) in str(error.value)


def test_12_the_refusal_stores_nothing(database: Database) -> None:
    mine = add_inquiry(database)
    theirs = add_inquiry(database)
    foreign_draft = add_draft(
        database, inquiry_id=theirs, final_answer="다른 고객에게 보낸 답변")
    service = LearningService(database)

    with pytest.raises(LearningLinkError):
        service._build(
            inquiry=service.inquiries.get(mine),
            draft=service.answers.get(foreign_draft),
            learning_source="APPROVED_EDITED", answer="답변",
        )

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 0


def test_13_the_same_inquiry_s_draft_is_accepted(database: Database) -> None:
    inquiry_id = add_inquiry(database)
    draft_id = add_draft(
        database, inquiry_id=inquiry_id, final_answer="정상 답변입니다.",
        edited_answer="정상 답변입니다.")
    service = LearningService(database)

    example = service._build(
        inquiry=service.inquiries.get(inquiry_id),
        draft=service.answers.get(draft_id),
        learning_source="APPROVED_EDITED", answer="정상 답변입니다.",
    )

    assert example is not None
    assert int(example["inquiry_id"]) == inquiry_id
    assert int(example["answer_draft_id"]) == draft_id


def test_14_a_marketplace_answer_without_a_draft_still_builds(
    database: Database,
) -> None:
    """draft 가 없는 경로(판매자 답변 수집)는 검증 대상이 아니다."""

    inquiry_id = add_inquiry(database)
    service = LearningService(database)

    example = service._build(
        inquiry=service.inquiries.get(inquiry_id), draft=None,
        learning_source="SELLER_ANSWER", answer="판매자가 등록한 답변입니다.",
        seller_answer="판매자가 등록한 답변입니다.",
    )

    assert example is not None
    assert example["learning_source"] == "SELLER_ANSWER"


def test_15_the_same_product_s_other_inquiry_is_still_a_mismatch(
    database: Database,
) -> None:
    """상품 ID 가 같다는 사실은 연결을 증명하지 않는다."""

    mine = add_inquiry(database, product_id="10281062902")
    theirs = add_inquiry(database, product_id="10281062902")
    foreign_draft = add_draft(
        database, inquiry_id=theirs, final_answer="다른 문의의 답변")
    service = LearningService(database)

    with pytest.raises(LearningLinkError):
        service._build(
            inquiry=service.inquiries.get(mine),
            draft=service.answers.get(foreign_draft),
            learning_source="APPROVED_UNEDITED", answer="답변",
        )


def test_16_a_coupang_draft_cannot_serve_a_naver_inquiry(
    database: Database,
) -> None:
    naver = add_inquiry(database, store_code="OJE_PLUS",
                        source_type="PRODUCT_INQUIRY")
    coupang = add_inquiry(database, store_code="COUPANG_OJE_NS",
                          source_type="COUPANG_ONLINE_INQUIRY")
    coupang_draft = add_draft(
        database, inquiry_id=coupang, final_answer="쿠팡 문의에 보낸 답변")
    service = LearningService(database)

    with pytest.raises(LearningLinkError):
        service._build(
            inquiry=service.inquiries.get(naver),
            draft=service.answers.get(coupang_draft),
            learning_source="APPROVED_EDITED", answer="답변",
        )


def test_17_markets_sharing_a_source_question_id_do_not_cross(
    database: Database,
) -> None:
    """마켓이 달라도 원본 질문 ID 가 같을 수 있다. 연결은 문의 행 id 로 한다."""

    naver = add_inquiry(database, store_code="OJE_PLUS",
                        source_question_id="777777")
    coupang = add_inquiry(database, store_code="COUPANG_OJE_NS",
                          source_type="COUPANG_ONLINE_INQUIRY",
                          source_question_id="777777")
    assert naver != coupang
    service = LearningService(database)
    coupang_draft = add_draft(
        database, inquiry_id=coupang, final_answer="쿠팡 답변")

    with pytest.raises(LearningLinkError):
        service._build(
            inquiry=service.inquiries.get(naver),
            draft=service.answers.get(coupang_draft),
            learning_source="APPROVED_EDITED", answer="답변",
        )


def test_18_a_staff_edit_keeps_its_edited_text_and_source(
    database: Database,
) -> None:
    inquiry_id = add_inquiry(database)
    draft_id = add_draft(
        database, inquiry_id=inquiry_id,
        final_answer="직원이 고친 최종 답변입니다.",
        edited_answer="직원이 고친 최종 답변입니다.")
    service = LearningService(database)

    example = service._build(
        inquiry=service.inquiries.get(inquiry_id),
        draft=service.answers.get(draft_id),
        learning_source="APPROVED_EDITED",
        answer="직원이 고친 최종 답변입니다.",
    )

    assert example["learning_source"] == "APPROVED_EDITED"
    assert example["edited_answer"] == "직원이 고친 최종 답변입니다."
    assert example["final_answer"] == "직원이 고친 최종 답변입니다."
    assert int(example["answer_draft_id"]) == draft_id


# ----------------------------------------------- 저장 연결: Historical 승격

def _case(database: Database, *, store_code: str, inquiry_id: int | None,
          question: str, answer: str) -> dict[str, object]:
    return {
        "id": 4242,
        "store_code": store_code,
        "inquiry_id": inquiry_id,
        "question": question,
        "question_normalized": question,
        "seller_answer": answer,
        "product_name": "삼성 모니터",
        "product_id": "10281062902",
        "fingerprint": "fp-%d" % next(_key),
        "quality_score": 0.8,
        "classification": "PRODUCT_INQUIRY",
        "inquiry_type": "PRODUCT_INQUIRY",
        "policy_risk": "NONE",
        "answer_updated_at": "2026-08-20T00:00:00Z",
        "metadata_json": {"market": "NAVER"},
    }


def test_19_historical_promotion_refuses_another_market_s_inquiry(
    database: Database,
) -> None:
    coupang = add_inquiry(database, store_code="COUPANG_OJE_NS",
                          source_type="COUPANG_ONLINE_INQUIRY")
    service = LearningService(database)
    case = _case(database, store_code="OJE_PLUS", inquiry_id=coupang,
                 question="질문", answer="답변")

    with pytest.raises(LearningLinkError) as error:
        service.capture_historical_promotion(case=case, actor="관리자")

    assert "COUPANG_OJE_NS" in str(error.value)
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 0


def test_20_historical_promotion_accepts_its_own_market(
    database: Database,
) -> None:
    inquiry_id = add_inquiry(database, store_code="OJE_PLUS")
    service = LearningService(database)
    case = _case(database, store_code="OJE_PLUS", inquiry_id=inquiry_id,
                 question="스탠드 높이 조절이 되나요?",
                 answer="높이 조절은 105mm 범위에서 가능합니다.")

    saved = service.capture_historical_promotion(case=case, actor="관리자")

    assert int(saved["inquiry_id"]) == inquiry_id
    assert metadata_of(saved)["source_origin"] == "HISTORICAL_PROMOTED"


def test_21_a_case_pointing_at_no_inquiry_row_is_refused_by_the_key(
    database: Database,
) -> None:
    """존재하지 않는 문의 ID 는 스키마의 외래키가 이미 막는다.

    ``learning_examples.inquiry_id`` 는 ``inquiries.id`` 를 참조하고 운영 DB 에
    고아 행은 0건이다. 이미 보장된 것을 다시 구현하지 않는다는 뜻이고, 이 테스트는
    새 검증이 그 앞을 가로채 다른 오류로 바꾸지 않는지를 고정한다 -- 마켓 불일치가
    아니므로 ``LearningLinkError`` 가 아니어야 한다.
    """

    import sqlite3

    service = LearningService(database)
    case = _case(database, store_code="OJE_PLUS", inquiry_id=999999,
                 question="예전 질문", answer="예전 판매자 답변입니다.")

    with pytest.raises(sqlite3.IntegrityError):
        service.capture_historical_promotion(case=case, actor="관리자")


def test_22_a_case_without_an_inquiry_link_still_promotes(
    database: Database,
) -> None:
    service = LearningService(database)
    case = _case(database, store_code="OJE_PLUS", inquiry_id=None,
                 question="연결 없는 질문", answer="연결 없는 답변입니다.")

    saved = service.capture_historical_promotion(case=case, actor="관리자")

    assert saved["inquiry_id"] is None


# ------------------------------------------------------- 재수집 중복 방지

def test_23_the_contact_number_masking_change_no_longer_duplicates(
    database: Database,
) -> None:
    """운영에서 5쌍을 만든 경로. 두 번째 수집이 같은 행을 갱신한다."""

    inquiry_id = add_inquiry(database)
    repository = LearningRepository(database)
    masked = "문의는 판매자고객센터)<masked-phone>로 연락 주세요."
    readable = "문의는 판매자고객센터)02-706-2678로 연락 주세요."
    first = repository.upsert({
        "source_key": "key-masked",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": masked,
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
    })
    second = repository.upsert({
        "source_key": "key-readable",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": readable,
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
    })

    assert int(second["id"]) == int(first["id"])
    assert second["final_answer"] == readable
    assert second["source_key"] == "key-masked"
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 1


def test_24_a_second_capture_path_no_longer_duplicates(
    database: Database,
) -> None:
    """운영에서 3쌍을 만든 경로. learning_source 는 digest 의 첫 성분이다."""

    inquiry_id = add_inquiry(database)
    repository = LearningRepository(database)
    answer = "스피커가 내장되어 있지 않습니다."
    first = repository.upsert({
        "source_key": "key-seller",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": answer,
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
    })
    second = repository.upsert({
        "source_key": "key-auto-post",
        "inquiry_id": inquiry_id,
        "learning_source": "AUTO_POST_REVIEWED_NO_CHANGE",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": answer,
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
    })

    assert int(second["id"]) == int(first["id"])
    assert second["learning_source"] == "AUTO_POST_REVIEWED_NO_CHANGE"
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 1


def test_25_a_corrected_answer_is_not_treated_as_a_duplicate(
    database: Database,
) -> None:
    """숫자만 다른 답변은 다른 답변이다. 정정본이 원본을 덮어쓰면 안 된다."""

    inquiry_id = add_inquiry(database)
    repository = LearningRepository(database)
    repository.upsert({
        "source_key": "key-first",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": "배송은 약 2주 소요됩니다.",
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
    })
    repository.upsert({
        "source_key": "key-second",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": "배송은 약 3주 소요됩니다.",
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
    })

    with database.connection() as connection:
        answers = {
            row[0] for row in connection.execute(
                "SELECT final_answer FROM learning_examples")}
    assert answers == {"배송은 약 2주 소요됩니다.", "배송은 약 3주 소요됩니다."}


def test_26_another_inquiry_s_identical_answer_stays_its_own_row(
    database: Database,
) -> None:
    """같은 정형 답변이 여러 문의에 쓰인다. 문의별로 각자의 행이어야 한다."""

    first_inquiry = add_inquiry(database)
    second_inquiry = add_inquiry(database)
    repository = LearningRepository(database)
    answer = "삼성전자 서비스센터로 연락 주시기 바랍니다."
    for index, inquiry_id in enumerate((first_inquiry, second_inquiry)):
        repository.upsert({
            "source_key": "key-%d" % index,
            "inquiry_id": inquiry_id,
            "learning_source": "SELLER_ANSWER",
            "question_original_masked": "질문",
            "question_normalized": "질문",
            "final_answer": answer,
            "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
        })

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 2


def test_27_a_different_question_keeps_its_own_row(
    database: Database,
) -> None:
    """한 문의에 여러 답변이 있는 경우는 그대로 유지된다."""

    inquiry_id = add_inquiry(database)
    repository = LearningRepository(database)
    for index, (question, answer) in enumerate((
        ("설치비 포함인가요", "무료로 설치됩니다."),
        ("배송 기간은요", "약 2~3주 소요됩니다."),
    )):
        repository.upsert({
            "source_key": "key-%d" % index,
            "inquiry_id": inquiry_id,
            "learning_source": "SELLER_ANSWER",
            "question_original_masked": question,
            "question_normalized": question,
            "final_answer": answer,
            "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
        })

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 2


def test_28_a_re_collection_does_not_overwrite_a_human_verified_row(
    database: Database,
) -> None:
    inquiry_id = add_inquiry(database)
    repository = LearningRepository(database)
    verified = repository.upsert({
        "source_key": "key-verified",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": "사람이 확인한 답변입니다. 문의는 02-706-2678.",
        "metadata_json": {"human_verified": True},
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
    })
    again = repository.upsert({
        "source_key": "key-recollected",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": "사람이 확인한 답변입니다. 문의는 <masked-phone>.",
        "rating": 1,
        "active": True,
    })

    assert int(again["id"]) == int(verified["id"])
    assert int(again["rating"]) == 5
    assert metadata_of(again)["human_verified"] is True
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 1


def test_29_a_row_without_an_inquiry_keeps_the_source_key_path(
    database: Database,
) -> None:
    repository = LearningRepository(database)
    for index in range(2):
        repository.upsert({
            "source_key": "key-%d" % index,
            "inquiry_id": None,
            "learning_source": "APPROVED_EDITED",
            "question_original_masked": "질문",
            "question_normalized": "질문",
            "final_answer": "동일한 답변입니다.",
            "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "active": True,
        })

    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 2


def test_30_the_origin_key_neutralises_only_contacts_and_masks() -> None:
    assert origin_answer_key("문의는 <masked-phone>로") == (
        origin_answer_key("문의는 02-706-2678로"))
    assert origin_answer_key("센터 1588-3366") == (
        origin_answer_key("센터 <masked-phone>"))
    assert origin_answer_key("약 2주") != origin_answer_key("약 3주")
    assert origin_answer_key("1110.8 x 643.8") != (
        origin_answer_key("1920 x 1080"))
    assert origin_answer_key("  여러   공백  ") == origin_answer_key("여러 공백")


def test_31_approving_an_already_stored_answer_upgrades_that_row(
    database: Database,
) -> None:
    """운영 DB 의 8번째 중복쌍(문의 2123)이 생긴 경로.

    판매자 답변이 STYLE_ONLY 로 먼저 수집되고, 같은 답변을 사람이 승인하면
    승인 경로는 ``allow_human_authority_update=True`` 로 들어온다. 이제 같은
    원본을 알아보고 그 행을 승격한다 -- 두 번째 행을 만들지 않는다.
    """

    inquiry_id = add_inquiry(database)
    repository = LearningRepository(database)
    answer = "문의하신 모델은 스피커가 내장되어 있지 않습니다."
    collected = repository.upsert({
        "source_key": "key-collected",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "스피커 있나요",
        "question_normalized": "스피커 있나요",
        "final_answer": answer,
        "style_only": True,
        "rating": 3,
        "edit_ratio": 0.0,
        "quality_score": 0.6,
        "version": 1,
        "metadata_json": {"facts_authority": "STYLE_ONLY"},
        "active": True,
    })

    approved = repository.upsert_human_verified_atomic(
        {
            "source_key": "key-approved",
            "inquiry_id": inquiry_id,
            "learning_source": "SELLER_ANSWER",
            "question_original_masked": "스피커 있나요",
            "question_normalized": "스피커 있나요",
            "final_answer": answer,
            "style_only": False,
            "rating": 5,
            "edit_ratio": 0.0,
            "quality_score": 1.0,
            "version": 1,
            "metadata_json": {
                "human_verified": True,
                "answer_reference_id": 1,
                "verified_by": "local-admin",
                "answer_provenance": "NAVER_POSTED",
                "facts_authority": "HUMAN_VERIFIED_NAVER_POSTED",
            },
            "active": True,
        },
        feedback_answer_sources=("NAVER_POSTED",),
    )

    assert int(approved["id"]) == int(collected["id"])
    assert metadata_of(approved)["human_verified"] is True
    assert metadata_of(approved)["verified_by"] == "local-admin"
    assert int(approved["rating"]) == 5
    assert not approved["style_only"]
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 1


def test_32_approving_a_different_answer_adds_its_own_row(
    database: Database,
) -> None:
    """승인 기능이 좁아지지 않는다: 다른 답변은 새 행으로 들어온다."""

    inquiry_id = add_inquiry(database)
    repository = LearningRepository(database)
    repository.upsert({
        "source_key": "key-collected",
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "스피커 있나요",
        "question_normalized": "스피커 있나요",
        "final_answer": "스피커가 내장되어 있지 않습니다.",
        "rating": 3,
        "edit_ratio": 0.0,
        "quality_score": 0.6,
        "version": 1,
        "active": True,
    })

    approved = repository.upsert_human_verified_atomic(
        {
            "source_key": "key-approved",
            "inquiry_id": inquiry_id,
            "learning_source": "SELLER_ANSWER",
            "question_original_masked": "스피커 있나요",
            "question_normalized": "스피커 있나요",
            "final_answer": "스피커는 10W 출력으로 내장되어 있습니다.",
            "rating": 5,
            "edit_ratio": 0.0,
            "quality_score": 1.0,
            "version": 1,
            "metadata_json": {
                "human_verified": True, "answer_reference_id": 2},
            "active": True,
        },
        feedback_answer_sources=("NAVER_POSTED",),
    )

    assert approved["source_key"] == "key-approved"
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 2


def add_approval(database: Database, *, inquiry_id: int, draft_id: int) -> int:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO approval_history(
                inquiry_id, answer_draft_id, action, actor,
                previous_status, new_status
            ) VALUES (?,?,'APPROVED','local-admin','REVIEW','APPROVED')
            """,
            (inquiry_id, draft_id),
        )
        return int(
            connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_33_a_re_collection_keeps_the_approval_trail(
    database: Database,
) -> None:
    """재수집이 승인·수정 이력을 지우지 않는다.

    사람이 확인한 행은 draft 와 approval_history 로 "누가 무엇을 승인했는지"를
    가리킨다. 같은 답변이 다시 수집될 때 그 연결이 끊기면 승인 기록을 추적할 수
    없게 된다.
    """

    inquiry_id = add_inquiry(database)
    draft_id = add_draft(
        database, inquiry_id=inquiry_id,
        final_answer="직원이 고친 답변입니다. 문의는 02-706-2678.",
        edited_answer="직원이 고친 답변입니다. 문의는 02-706-2678.")
    approval_id = add_approval(
        database, inquiry_id=inquiry_id, draft_id=draft_id)
    repository = LearningRepository(database)
    approved = repository.upsert({
        "source_key": "key-approved",
        "inquiry_id": inquiry_id,
        "answer_draft_id": draft_id,
        "approval_history_id": approval_id,
        "learning_source": "APPROVED_EDITED",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "edited_answer": "직원이 고친 답변입니다. 문의는 02-706-2678.",
        "final_answer": "직원이 고친 답변입니다. 문의는 02-706-2678.",
        "metadata_json": {
            "human_verified": True, "verified_by": "local-admin"},
        "rating": 5,
        "edit_ratio": 0.4,
        "quality_score": 1.0,
        "version": 1,
        "active": True,
    })

    again = repository.upsert({
        "source_key": "key-recollected",
        "inquiry_id": inquiry_id,
        "answer_draft_id": None,
        "approval_history_id": None,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "final_answer": "직원이 고친 답변입니다. 문의는 <masked-phone>.",
        "rating": 3,
        "edit_ratio": 0.0,
        "quality_score": 0.6,
        "version": 1,
        "active": True,
    })

    assert int(again["id"]) == int(approved["id"])
    assert int(again["answer_draft_id"]) == draft_id
    assert int(again["approval_history_id"]) == approval_id
    assert again["learning_source"] == "APPROVED_EDITED"
    assert again["edited_answer"] == (
        "직원이 고친 답변입니다. 문의는 02-706-2678.")
    assert metadata_of(again)["verified_by"] == "local-admin"
    with database.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM learning_examples").fetchone()[0] == 1


# ------------------------------- 정정본이 pool 에 없을 때도 철회가 적용된다

def test_34_a_stored_retraction_withdraws_even_when_it_missed_the_pool(
) -> None:
    """운영 코드로 재현된 경계.

    두 형제는 같은 질문으로 점수가 매겨지지만 ``relevance`` 는 floor 적용 전에
    ``0.6 * answer_support`` 를 더하고 그 항은 각 행의 *답변* 에서 계산된다.
    문의 2536 에서 질의 "주문정보 자세히 보기에서 배송예정일 확인하나요" 는
    철회된 답변을 0.651 로, 세 줄짜리 정정본을 0.24 미만으로 매겨 pool 에
    철회된 주장만 남았다.
    """

    ranked = [
        candidate(152700, inquiry_id=2411, answer=RETRACTED_2411,
                  created_at="2026-08-17T22:38:10Z"),
    ]
    stored = [{
        "id": 154436, "inquiry_id": 2411,
        "final_answer": CORRECTION_2411,
        "created_at": "2026-08-18T00:39:37Z",
    }]

    kept, notes = _drop_retracted_answers(ranked, stored_siblings=stored)

    assert kept == []
    assert notes[0]["retracted_learning_id"] == 152700
    assert notes[0]["retracted_by_learning_id"] == 154436


def test_35_a_stored_sibling_is_never_itself_removed() -> None:
    """저장소에서 읽어온 행은 후보가 아니다. 제거 대상이 되지 않는다."""

    ranked = [
        candidate(154436, inquiry_id=2411, answer=CORRECTION_2411,
                  created_at="2026-08-18T00:39:37Z"),
    ]
    stored = [{
        "id": 152700, "inquiry_id": 2411,
        "final_answer": RETRACTED_2411,
        "created_at": "2026-08-17T22:38:10Z",
    }]

    kept, notes = _drop_retracted_answers(ranked, stored_siblings=stored)

    assert [int(item["id"]) for _score, item in kept] == [154436]
    assert notes == []


def test_36_stored_siblings_of_other_inquiries_are_ignored() -> None:
    ranked = [
        candidate(900001, inquiry_id=9999, answer=RETRACTED_2411,
                  created_at="2026-08-17T22:38:10Z"),
    ]
    stored = [{
        "id": 154436, "inquiry_id": 2411,
        "final_answer": CORRECTION_2411,
        "created_at": "2026-08-18T00:39:37Z",
    }]

    kept, notes = _drop_retracted_answers(ranked, stored_siblings=stored)

    assert len(kept) == 1
    assert notes == []


def test_37_a_stored_supplement_still_does_not_retract() -> None:
    ranked = [
        candidate(60957, inquiry_id=2317, answer=PIVOT_2317,
                  created_at="2026-08-14T01:06:52Z"),
    ]
    stored = [{
        "id": 62358, "inquiry_id": 2317,
        "final_answer": SUPPLEMENT_2317,
        "created_at": "2026-08-14T02:26:52Z",
    }]

    kept, notes = _drop_retracted_answers(ranked, stored_siblings=stored)

    assert [int(item["id"]) for _score, item in kept] == [60957]
    assert notes == []


def test_38_a_retraction_below_the_floor_reaches_the_real_search(
    database: Database,
) -> None:
    """정정본이 relevance floor 를 넘지 못해도 철회가 적용된다.

    ``search`` 는 이미 읽어둔 전체 usable 코퍼스를 넘기므로, floor 에서
    걸러진 정정본도 형제로 보인다.
    """

    from services.similar_answer_service import SimilarAnswerService

    inquiry_id = add_inquiry(
        database, content="제주도도 배송설치 가능할까요?",
        product_name="삼성 삼탠바이미 스마트 M5 모니터")
    repository = LearningRepository(database)
    common = {
        "inquiry_id": inquiry_id,
        "learning_source": "SELLER_ANSWER",
        "question_original_masked": (
            "상품 문의\n제주도도 배송설치 가능할까요?"),
        "question_normalized": "상품 문의 제주도도 배송설치 가능할까요",
        "product_name": "삼성 삼탠바이미 스마트 M5 모니터",
        "rating": 5,
        "edit_ratio": 0.0,
        "quality_score": 0.8,
        "version": 1,
        "metadata_json": {"answer_provenance": "NAVER_POSTED"},
        "active": True,
    }
    withdrawn = repository.upsert({
        **common,
        "source_key": "key-withdrawn",
        "final_answer": (
            "설치 예정일은 수취인 카카오톡으로 발송되는 삼성전자로지텍 "
            "알림톡을 통해 안내되며, 알림톡에서 '주문정보 자세히 보기'를 "
            "클릭한 후 배송예정일을 확인할 수 있습니다."),
    })
    repository.upsert({
        **common,
        "source_key": "key-correction",
        "final_answer": (
            "안녕하세요. 챗봇이 잘못된 답변을 드려 정정드립니다. "
            "제주도도 배송/설치 가능합니다."),
    })

    service = SimilarAnswerService(repository)
    found = service.search(
        "주문정보 자세히 보기에서 배송예정일 확인하나요",
        store_code="OJE_PLUS",
        product_name="삼성 삼탠바이미 스마트 M5 모니터",
        inquiry_type="PRODUCT_INQUIRY", limit=8,
    )

    assert int(withdrawn["id"]) not in {int(item["id"]) for item in found}
    assert [note["retracted_learning_id"]
            for note in service.last_trace["retracted_learnings"]] == [
        int(withdrawn["id"])]
