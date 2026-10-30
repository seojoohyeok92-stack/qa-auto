"""Learning 상태 필터: 파이썬 경로와 SQL 경로가 같은 답을 낸다.

대시보드에는 이 필터가 두 번 적용된다. 목록은 ``dashboard_page`` 이 SQL 로
거르고, KPI 카드가 읽는 ``scoped_items`` 는 ``filter_work_items`` 가 파이썬에서
거른다. 파이썬 쪽이 비교할 값을 얻으려면 행마다 Learning 상태를 붙여야 하고,
그 부착이 운영 DB 전체(11,395 행)에서 115.7 초였다.

두 경로가 같은 답을 낸다는 것이 최적화의 전제다. 실제로 같은데, 우연이 아니다:

  * SQL 은 ``_effective_learning_status_sql`` 의 ``CASE`` 하나를 ``=`` 로
    비교한다 (EXCLUDED > CORRECTED > APPROVED > AUTO > NONE 순).
  * 파이썬은 ``learning_statuses`` 집합의 포함 여부를 보는데, 그 집합을 만드는
    ``resolve_learning_lifecycle`` 이 ``statuses = [primary]`` 로 **한 개**만
    담고, 그 ``primary`` 는 같은 ``CASE`` 가 돌려준 ``effective_status`` 다.

그래서 "집합 포함" 은 사실상 "단일 값 비교" 이고 두 경로가 일치한다. 아래는 그
일치를 다섯 상태와 우선순위 모두에 대해 고정한다 -- 어느 한쪽 정의가 바뀌면
여기서 깨진다.

운영 DB 는 읽지 않는다. 임시 DB 에 각 상태를 직접 구성한다.
"""
from __future__ import annotations

from datetime import date

import pytest

from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository

FILTER_VALUES = ("APPROVED", "AUTO", "EXCLUDED", "CORRECTED", "NONE")
START, END = "2026-07-29", "2026-10-29"


@pytest.fixture
def database(tmp_path) -> Database:
    value = Database(tmp_path / "learning_filter.db")
    value.initialize()
    return value


def _inquiry(database: Database, suffix: str, *, store: str = "OJE_PLUS") -> int:
    return InquiryRepository(database).upsert_work_item({
        "store_code": store,
        "source_type": "PRODUCT_INQUIRY",
        "source_question_id": suffix,
        "external_inquiry_id": suffix,
        "inquiry_type": "PRODUCT_GENERAL",
        "title": "상품문의",
        "content": f"문의 {suffix}",
        "product_name": "삼성 모니터",
        "registered_at": "2026-09-15T10:00:00+09:00",
        "raw_json": {},
    }).inquiry_id


def _learning(
    database: Database,
    inquiry_id: int,
    *,
    key: str,
    human_verified: bool,
    signal: str = "POSITIVE",
    active: bool = True,
) -> None:
    """``learning_examples`` 행 하나. 승인(APPROVED)과 자동(AUTO)을 가른다."""

    from repositories.learning_repository import LearningRepository

    LearningRepository(database).upsert({
        "source_key": key,
        "inquiry_id": inquiry_id,
        "learning_source": "APPROVED_EDITED",
        "question_original_masked": "질문",
        "question_normalized": "질문",
        "product_name": "삼성 모니터",
        "final_answer": "답변입니다.",
        "style_only": False, "rating": 5, "edit_ratio": 0.0,
        "quality_score": 0.9, "version": 1, "style_features_json": {},
        "metadata_json": {
            "facts_authority": "APPROVED_REFERENCE",
            "learning_signal_type": signal,
            "answer_provenance": "STAFF_EDITED",
            "human_verified": human_verified,
        },
        "active": active,
    })


def _feedback(database: Database, inquiry_id: int, *, signal: str) -> None:
    """``learning_feedback`` 행 하나. 제외(EXCLUDED)와 교정(CORRECTED)을 만든다."""

    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO learning_feedback(
                source_key, feedback_type, correction_reason,
                learning_signal_type, source, inquiry_id, active,
                metadata_json, created_at, updated_at
            ) VALUES (?, 'STAFF_CORRECTION', 'perf-test', ?, 'STAFF', ?, 1, '{}',
                      '2026-09-15T10:00:00Z', '2026-09-15T10:00:00Z')
            """,
            (f"fb-{inquiry_id}-{signal}", signal, inquiry_id),
        )


def _seed_every_status(database: Database) -> dict[str, int]:
    """다섯 상태를 각각 하나씩, 그리고 우선순위가 걸리는 조합까지."""

    ids: dict[str, int] = {}
    ids["NONE"] = _inquiry(database, "none")

    ids["APPROVED"] = _inquiry(database, "approved")
    _learning(database, ids["APPROVED"], key="k-approved", human_verified=True)

    ids["AUTO"] = _inquiry(database, "auto")
    _learning(database, ids["AUTO"], key="k-auto", human_verified=False)

    ids["EXCLUDED"] = _inquiry(database, "excluded")
    _feedback(database, ids["EXCLUDED"], signal="NEGATIVE")

    ids["CORRECTED"] = _inquiry(database, "corrected")
    _feedback(database, ids["CORRECTED"], signal="INTENT_CORRECTION")

    # 우선순위 조합: 승인이 있어도 제외가 있으면 EXCLUDED 다.
    ids["EXCLUDED_OVER_APPROVED"] = _inquiry(database, "excluded-over-approved")
    _learning(
        database, ids["EXCLUDED_OVER_APPROVED"], key="k-eoa",
        human_verified=True,
    )
    _feedback(database, ids["EXCLUDED_OVER_APPROVED"], signal="EXCLUDED")

    # 교정이 승인보다 앞선다.
    ids["CORRECTED_OVER_APPROVED"] = _inquiry(
        database, "corrected-over-approved")
    _learning(
        database, ids["CORRECTED_OVER_APPROVED"], key="k-coa",
        human_verified=True,
    )
    _feedback(
        database, ids["CORRECTED_OVER_APPROVED"], signal="INTENT_CORRECTION")

    # 승인이 자동보다 앞선다.
    ids["APPROVED_OVER_AUTO"] = _inquiry(database, "approved-over-auto")
    _learning(
        database, ids["APPROVED_OVER_AUTO"], key="k-aoa-1",
        human_verified=True,
    )
    _learning(
        database, ids["APPROVED_OVER_AUTO"], key="k-aoa-2",
        human_verified=False,
    )
    return ids


def _python_ids(database: Database, value: str, **overrides) -> list[str]:
    """기존 경로: 전체 행에 상태를 붙이고 파이썬에서 거른다."""

    import app as appmod

    repository = InquiryRepository(database)
    rows = repository.list(
        limit=max(1, repository.count(
            registered_from=START, registered_to=END)),
        registered_from=START, registered_to=END,
    )
    items = appmod._attach_dashboard_learning(
        database,
        appmod._attach_dashboard_routes(
            database, appmod._dashboard_work_items_from_rows(rows)
        ),
    )
    kwargs = dict(
        store_codes=[], source="ALL", queues=[], priorities=[],
        answer_status="ALL", delivery_only=False, search_query="",
        start_date=date.fromisoformat(START), end_date=date.fromisoformat(END),
        route="ALL",
    )
    kwargs.update(overrides)
    return [
        str(item.get("inquiry_id"))
        for item in appmod.filter_work_items(
            items, learning_status=value, **kwargs
        )
    ]


def _sql_ids(database: Database, value: str, **overrides) -> tuple[list[str], int]:
    """신규 경로: 모든 페이지를 SQL 로 조회해 모은다."""

    repository = InquiryRepository(database)
    collected: list[str] = []
    page, total = 1, 0
    while True:
        kwargs = dict(
            store_codes=[], source="ALL", queues=[], priorities=[],
            answer_status="ALL", delivery_only=False, search_query="",
            start_date=START, end_date=END, kpi_filter=None,
        )
        kwargs.update(overrides)
        rows, total, total_pages = repository.dashboard_page(
            page=page, page_size=10, learning_status=value, **kwargs
        )
        collected.extend(str(row["source_question_id"]) for row in rows)
        if page >= total_pages:
            break
        page += 1
    return collected, total


# ===========================================================================
# 1. 다섯 상태 전부에서 두 경로가 같은 ID·건수·순서를 낸다
# ===========================================================================

@pytest.mark.parametrize("value", FILTER_VALUES)
def test_both_paths_return_the_same_inquiries(database, value) -> None:
    _seed_every_status(database)

    python_ids = _python_ids(database, value)
    sql_ids, total = _sql_ids(database, value)

    assert python_ids == sql_ids, (value, python_ids, sql_ids)
    assert total == len(python_ids)


def test_all_means_no_learning_condition(database) -> None:
    ids = _seed_every_status(database)

    python_ids = _python_ids(database, "ALL")
    sql_ids, total = _sql_ids(database, "ALL")

    assert python_ids == sql_ids
    assert total == len(ids)


def test_the_five_filters_partition_the_inquiries(database) -> None:
    """한 문의는 정확히 한 필터에만 걸린다 -- 단일 effective status 이므로."""

    ids = _seed_every_status(database)
    seen: list[str] = []
    for value in FILTER_VALUES:
        seen.extend(_sql_ids(database, value)[0])

    assert sorted(seen) == sorted(seen[:])          # 중복 없음을 아래에서 확인
    assert len(seen) == len(set(seen)) == len(ids)


# ===========================================================================
# 2. 우선순위가 두 경로에서 같다
# ===========================================================================

@pytest.mark.parametrize("suffix, expected", [
    ("excluded-over-approved", "EXCLUDED"),
    ("corrected-over-approved", "CORRECTED"),
    ("approved-over-auto", "APPROVED"),
])
def test_the_precedence_is_the_same_on_both_sides(
    database, suffix, expected,
) -> None:
    _seed_every_status(database)

    for value in FILTER_VALUES:
        in_python = suffix in _python_ids(database, value)
        in_sql = suffix in _sql_ids(database, value)[0]
        assert in_python == in_sql, (value, suffix)
        assert in_python == (value == expected), (value, suffix)


# ===========================================================================
# 3. 다른 필터와의 결합
# ===========================================================================

def test_the_learning_filter_combines_with_a_search_query(database) -> None:
    ids = _seed_every_status(database)
    extra = _inquiry(database, "approved-searchable")
    _learning(database, extra, key="k-search", human_verified=True)

    python_ids = _python_ids(
        database, "APPROVED", search_query="approved-searchable")
    sql_ids, total = _sql_ids(
        database, "APPROVED", search_query="approved-searchable")

    assert python_ids == sql_ids == ["approved-searchable"]
    assert total == 1
    assert ids["APPROVED"]


def test_the_learning_filter_combines_with_a_store_filter(database) -> None:
    _seed_every_status(database)
    coupang = _inquiry(database, "coupang-approved", store="COUPANG_OJE_NS")
    _learning(database, coupang, key="k-coupang", human_verified=True)

    naver_python = _python_ids(
        database, "APPROVED", store_codes=["OJE_PLUS"])
    naver_sql, naver_total = _sql_ids(
        database, "APPROVED", store_codes=["OJE_PLUS"])
    coupang_python = _python_ids(
        database, "APPROVED", store_codes=["COUPANG_OJE_NS"])
    coupang_sql, coupang_total = _sql_ids(
        database, "APPROVED", store_codes=["COUPANG_OJE_NS"])
    both_python = _python_ids(
        database, "APPROVED", store_codes=["OJE_PLUS", "COUPANG_OJE_NS"])
    both_sql, both_total = _sql_ids(
        database, "APPROVED", store_codes=["OJE_PLUS", "COUPANG_OJE_NS"])

    assert naver_python == naver_sql and "coupang-approved" not in naver_sql
    assert coupang_python == coupang_sql == ["coupang-approved"]
    assert both_python == both_sql
    assert naver_total + coupang_total == both_total
    assert set(both_sql) == set(naver_sql) | set(coupang_sql)


def test_a_wider_date_range_brings_back_older_inquiries(database) -> None:
    """1차에서 넣은 기본 3개월을 넓히면 과거 문의도 필터에 걸린다."""

    old = _inquiry(database, "old-approved")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE inquiries SET registered_at=? WHERE id=?",
            ("2024-09-01T10:00:00+09:00", old),
        )
    _learning(database, old, key="k-old", human_verified=True)

    inside, _ = _sql_ids(database, "APPROVED")
    repository = InquiryRepository(database)
    widened, widened_total = [], 0
    rows, widened_total, pages = repository.dashboard_page(
        store_codes=[], source="ALL", queues=[], priorities=[],
        answer_status="ALL", delivery_only=False, search_query="",
        start_date="2024-01-01", end_date=END, kpi_filter=None,
        page=1, page_size=30, learning_status="APPROVED",
    )
    widened = [str(row["source_question_id"]) for row in rows]

    assert "old-approved" not in inside
    assert "old-approved" in widened
    assert widened_total == 1


# ===========================================================================
# 4. 승인·수정 이후 상태가 반영된다
# ===========================================================================

def test_approving_a_learning_moves_the_inquiry_between_filters(
    database,
) -> None:
    inquiry_id = _inquiry(database, "becomes-approved")

    assert _sql_ids(database, "NONE")[0] == ["becomes-approved"]

    _learning(database, inquiry_id, key="k-becomes", human_verified=True)

    assert _sql_ids(database, "NONE")[0] == []
    assert _sql_ids(database, "APPROVED")[0] == ["becomes-approved"]
    assert _python_ids(database, "APPROVED") == ["becomes-approved"]


def test_a_common_learning_approval_is_reflected(database) -> None:
    """공통 Learning 직접 승인도 같은 문의의 상태를 APPROVED 로 만든다."""

    from repositories.learning_repository import LearningRepository

    inquiry_id = _inquiry(database, "common-approved")
    LearningRepository(database).add_common_learning_for_inquiry(
        inquiry_id, question="질문", answer="공통 답변입니다.",
        product_scope="POLICY", market_applicability="COMMON", actor="op",
    )

    assert _sql_ids(database, "APPROVED")[0] == ["common-approved"]
    assert _python_ids(database, "APPROVED") == ["common-approved"]


def test_deactivating_the_learning_returns_the_inquiry_to_none(
    database,
) -> None:
    inquiry_id = _inquiry(database, "deactivated")
    _learning(database, inquiry_id, key="k-deact", human_verified=True)
    assert _sql_ids(database, "APPROVED")[0] == ["deactivated"]

    with database.transaction() as connection:
        connection.execute(
            "UPDATE learning_examples SET active=0 WHERE inquiry_id=?",
            (inquiry_id,),
        )

    assert _sql_ids(database, "APPROVED")[0] == []
    assert _sql_ids(database, "NONE")[0] == ["deactivated"]
    assert _python_ids(database, "NONE") == ["deactivated"]


# ===========================================================================
# 5. 느렸던 것은 구조가 아니라 상태를 내는 식이었다
# ===========================================================================

def test_the_whole_table_badge_pass_is_not_per_row_anymore(database) -> None:
    """``learning_states`` 가 문의 수에 비례해 폭발하지 않는다.

    115.7 초의 원인은 ``_effective_learning_status_sql`` 이 **상관** 서브쿼리라
    문의 한 행마다 네 번씩 다시 평가된 것이었다. 비상관으로 바꾼 뒤에는 각
    집합이 한 번만 계산된다 -- 운영 DB 사본에서 11,395 행이 35ms.

    여기서는 그 성질을 식의 모양으로 고정한다. 시간을 재는 테스트는 기계
    속도에 묶이지만, "바깥 행을 참조하지 않는다" 는 성질은 그렇지 않다.
    """

    from repositories.inquiry_repository import InquiryRepository

    sql = InquiryRepository._effective_learning_status_sql("i")

    # 상관 서브쿼리의 표식: 서브쿼리 안에서 바깥 별칭을 비교하던 형태.
    assert "ap.inquiry_id=i.id" not in sql.replace(" ", "")
    assert "au.inquiry_id=i.id" not in sql.replace(" ", "")
    assert "ex.inquiry_id=i.id" not in sql.replace(" ", "")
    assert "co.inquiry_id=i.id" not in sql.replace(" ", "")
    # 비상관 멤버십으로 바뀌었다.
    assert sql.count("i.id IN (") >= 4


def test_the_badge_pass_still_returns_the_same_states(database) -> None:
    """식을 바꿔도 각 문의의 상태는 그대로다."""

    ids = _seed_every_status(database)
    states = InquiryRepository(database).learning_states(list(ids.values()))

    assert states[ids["NONE"]]["learning_status"] == "NONE"
    assert states[ids["APPROVED"]]["learning_status"] == "APPROVED"
    assert states[ids["AUTO"]]["learning_status"] == "AUTO"
    assert states[ids["EXCLUDED"]]["learning_status"] == "EXCLUDED"
    assert states[ids["CORRECTED"]]["learning_status"] == "CORRECTED"
    assert states[ids["EXCLUDED_OVER_APPROVED"]]["learning_status"] == "EXCLUDED"
    assert states[ids["CORRECTED_OVER_APPROVED"]]["learning_status"] == "CORRECTED"
    assert states[ids["APPROVED_OVER_AUTO"]]["learning_status"] == "APPROVED"


def test_a_market_selection_still_partitions_under_the_filter(
    database,
) -> None:
    """네이버 단독·쿠팡 단독·복수 선택이 합쳐서 같다."""

    _seed_every_status(database)
    coupang = _inquiry(database, "coupang-approved", store="COUPANG_OJE_NS")
    _learning(database, coupang, key="k-cp", human_verified=True)

    naver, naver_total = _sql_ids(
        database, "APPROVED", store_codes=["OJE_PLUS"])
    coupang_only, coupang_total = _sql_ids(
        database, "APPROVED", store_codes=["COUPANG_OJE_NS"])
    both, both_total = _sql_ids(
        database, "APPROVED", store_codes=["OJE_PLUS", "COUPANG_OJE_NS"])

    assert coupang_only == ["coupang-approved"]
    assert "coupang-approved" not in naver
    assert set(both) == set(naver) | set(coupang_only)
    assert naver_total + coupang_total == both_total
    # 파이썬 경로도 같은 분할을 본다.
    assert sorted(_python_ids(
        database, "APPROVED", store_codes=["OJE_PLUS"])) == sorted(naver)
