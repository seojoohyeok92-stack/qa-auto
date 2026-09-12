"""검색 방향은 GPT ① 이 정하고, CODE 는 그 검색을 실행한다.

이 파일이 고정하는 것은 하나다. 고객이 저장된 답변과 다른 낱말로 물었을 때
그 답변이 GPT ② 앞까지 오는가.

v5 까지의 계약은 "찾아온 후보를 CODE 가 의미로 지우지 않는다" 였고 그것은
달성됐다(soft 제거 0건). 남은 문제는 그 반대편이었다: 지우는 것이 아니라
애초에 가져오지 못하는 것. lexical 점수는 질의 문장을 저장된 질문과 비교하는데,
"쓰던 티비 가져가 주시나요?" 와 "기존 폐가전도 무료로 수거해 주시나요?" 는 같은
것을 묻고 낱말은 거의 겹치지 않는다.

그래서 GPT ① 이 atom 마다 "어떤 내용의 답변을 찾아야 하는가" 를 함께 낸다.
CODE 는 그 문장들을 기존 검색엔진에 추가 질의로 넣을 뿐이고, 어느 분기도 그것을
분류하지 않는다. GPT 호출 횟수는 그대로다.

읽는 사람이 알아야 할 규칙:

* 정답을 Learning ID 로 적지 않는다. 정답은 "그 답변이 담고 있어야 하는 사실"의
  규칙이고, 대상 집합은 실행 시점에 운영 corpus 에서 계산된다. corpus 에는 같은
  사실을 담은 행이 여럿 있고 GPT ② 는 그 중 하나만 보면 되므로, 이것이 운영상
  옳은 정의다. ID 를 적으면 label 이 불완전해져 recall 이 실제보다 낮게 나오고,
  특정 ID 를 겨냥해 점수를 올릴 여지도 생긴다.
* fixture 의 retrieval query 는 고객 문장만 보고 쓴 것이며, 정답 원문을 베끼지
  않았다는 것을 테스트가 직접 검사한다
  (test_fixture_queries_do_not_copy_the_stored_questions).
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest

from repositories.database import Database
from repositories.learning_repository import LearningRepository
from services.learning_semantic_index import LearningSemanticIndex
from services.semantic_analysis import (
    MAX_RETRIEVAL_QUERIES,
    MAX_RETRIEVAL_QUERY_LENGTH,
    parse,
)
from services.similar_answer_service import (
    SimilarAnswerService,
    normalize_learning_question,
)
from tests.test_v4_evidence_generalization import (
    IDENTIFIED_PRODUCT,
    SOURCE_DB,
    STORE,
    UNIDENTIFIED_PRODUCT,
    _EvidenceReadingStub,
    _run,
    _spec_atom,
)

INDEX_PATH = Path("data/learning_semantic_index.json")


# ===========================================================================
# 1. GPT ① contract -- schema 와 parser
# ===========================================================================

def _understanding(**overrides):
    payload = {
        "primary_action": "INSTALLATION_METHOD",
        "confidence": 0.95,
        "atomic_questions": [{
            "text": "설치는 누가 하나요?",
            "action": "INSTALLATION_METHOD",
            "requested_information": "설치 주체",
            "requested_attribute": "ACTOR",
        }],
    }
    payload["atomic_questions"][0].update(overrides)
    return payload


def test_gpt1_returns_retrieval_queries_per_atomic_question():
    """계약의 본체: atom 마다 검색 방향이 붙어서 온다."""

    result = parse(_understanding(retrieval_queries=[
        "기사 방문 설치로 진행되는 상품인지에 대한 안내",
        "고객이 직접 설치해야 하는지에 대한 안내",
    ]))
    atom = result.atomic_questions[0]
    assert atom.retrieval_queries == (
        "기사 방문 설치로 진행되는 상품인지에 대한 안내",
        "고객이 직접 설치해야 하는지에 대한 안내",
    )
    assert atom.to_dict()["retrieval_queries"] == list(atom.retrieval_queries)


def test_the_existing_understanding_contract_is_not_broken():
    """기존 필드는 그대로다. 새 필드는 덧붙는 것이지 바꾸는 것이 아니다."""

    result = parse(_understanding(retrieval_queries=["설치 주체에 대한 안내"]))
    assert result.usable
    assert result.primary_action == "INSTALLATION_METHOD"
    assert result.purchase_state == "UNKNOWN"
    atom = result.atomic_questions[0]
    assert atom.text == "설치는 누가 하나요?"
    assert atom.action == "INSTALLATION_METHOD"
    assert atom.requested_information == "설치 주체"
    assert atom.requested_attribute == "ACTOR"


@pytest.mark.parametrize("value", [
    None, "", "문자열 하나", 0, {"a": 1}, ["", "  ", "짧"],
])
def test_a_missing_or_malformed_retrieval_query_is_not_an_error(value):
    """검색 방향이 없거나 깨져도 이해 자체는 살아 있어야 한다.

    action 이 깨지면 고객이 무엇을 원했는지 알 수 없으니 거절하는 것이 맞다.
    검색 방향이 깨진 것은 다르다 -- 검색이 고객 문장 하나로 돌아갈 뿐이고,
    그것은 이 필드가 생기기 전 모든 검색이 하던 일이다.
    """

    result = parse(_understanding(retrieval_queries=value))
    assert result.usable
    assert result.atomic_questions[0].retrieval_queries == ()


def test_retrieval_queries_are_bounded_in_count_and_length():
    result = parse(_understanding(retrieval_queries=[
        "첫 번째 검색 관점에 대한 안내",
        "첫 번째 검색 관점에 대한 안내",
        "두 번째 검색 관점에 대한 안내",
        "세 번째 검색 관점에 대한 안내",
        "네 번째 검색 관점에 대한 안내",
        "가" * 400,
    ]))
    queries = result.atomic_questions[0].retrieval_queries
    assert len(queries) == MAX_RETRIEVAL_QUERIES
    assert len(set(queries)) == len(queries)
    assert all(len(q) <= MAX_RETRIEVAL_QUERY_LENGTH for q in queries)


def test_the_gpt1_prompt_asks_for_retrieval_queries_without_a_taxonomy():
    """자유로운 자연어를 요구해야 한다. 고를 목록을 주면 목록이 규칙이 된다."""

    from services.gpt_semantic_analyzer_service import (
        PROMPT_BUDGET,
        GptSemanticAnalyzerService,
    )

    service = GptSemanticAnalyzerService.__new__(GptSemanticAnalyzerService)
    prompt = service.build_prompt("설치는 기사님이 해주시나요?")
    assert "retrieval_queries" in prompt
    assert len(prompt) < PROMPT_BUDGET
    # 새 closed vocabulary 를 만들지 않았다. 아래는 그런 taxonomy 를 만들었다면
    # 반드시 프롬프트에 나타났을 이름들이다.
    for forbidden in ("INSTALLATION_TOPIC", "VESA", "RF", "OTT", "WALL_MOUNT"):
        assert forbidden not in prompt, forbidden


# ===========================================================================
# 2. corpus -- 실제 운영 Learning 으로만 측정한다
# ===========================================================================

def _copy_all_active_learning(target: Database) -> int:
    source = sqlite3.connect(f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute(
            "SELECT * FROM learning_examples WHERE active=1"
        ).fetchall()
        if not rows:
            return 0
        inquiry_ids = sorted({
            int(r["inquiry_id"]) for r in rows if r["inquiry_id"] is not None
        })
        inquiries = []
        for start in range(0, len(inquiry_ids), 500):
            chunk = inquiry_ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            inquiries.extend(source.execute(
                f"SELECT * FROM inquiries WHERE id IN ({marks})", chunk
            ).fetchall())
        with target.transaction() as connection:
            for table, table_rows in (
                ("inquiries", inquiries), ("learning_examples", rows),
            ):
                if not table_rows:
                    continue
                cols = {
                    c[1] for c in connection.execute(f"PRAGMA table_info({table})")
                }
                names = [n for n in table_rows[0].keys() if n in cols]
                sql = (
                    f"INSERT OR IGNORE INTO {table} ({','.join(names)})"
                    f" VALUES ({','.join('?' for _ in names)})"
                )
                # approval_history / answer_drafts 는 ON DELETE SET NULL 인
                # 선택적 링크이고 이 fixture 는 복사하지 않으므로 그 두 칸만 비운다.
                optional = {"approval_history_id", "answer_draft_id"}
                connection.executemany(sql, [
                    tuple(None if n in optional else r[n] for n in names)
                    for r in table_rows
                ])
        return len(rows)
    finally:
        source.close()


# 25개 benchmark. 각 항목은
# (분류, 고객 질문, GPT ① 이 낼 법한 검색 방향, (반드시 포함, 하나는 포함))
BENCHMARK = [
    ("폐가전수거", "쓰던 티비 가져가 주시나요?",
     ["기존에 쓰던 폐가전을 무상으로 수거해 주는지에 대한 안내",
      "설치 방문 시 헌 가전을 회수해 가는 절차 안내"],
     ([r"폐가전|기존\s*티비|기존티비|헌\s*가전"], [r"수거|회수|가져가"])),
    ("폐가전수거", "예전 텔레비전 처리도 같이 되나요?",
     ["폐가전 수거가 함께 진행되는지에 대한 안내",
      "제품 설치와 동시에 기존 가전을 회수하는지에 대한 안내"],
     ([r"폐가전|기존\s*티비|기존티비|헌\s*가전"], [r"수거|회수|가져가"])),
    ("자가설치", "제가 직접 조립해야 하나요?",
     ["고객이 직접 설치해야 하는 제품인지 자가설치 가능 여부 안내",
      "전문 기사 방문 설치로 진행되는 상품인지에 대한 안내"],
     ([r"자가\s*설치|직접\s*설치"], [r"가능|어렵|아니|기사"])),
    ("설치주체", "누가 와서 달아주시는 건가요?",
     ["설치를 수행하는 주체가 누구인지에 대한 안내",
      "삼성 기사가 방문하여 설치하는 상품인지에 대한 안내"],
     ([r"기사님|기사분|설치\s*기사"],
      [r"방문\s*설치|방문하여\s*설치|설치해\s*드리|설치\s*진행|설치를\s*진행"])),
    ("자가설치", "다른 거치대를 따로 사면 달아주시나요?",
     ["별도로 구매한 거치대 스탠드에 대한 설치 지원 여부 안내",
      "옵션 외 스탠드를 사용할 때 설치 지원 범위 안내"],
     ([r"스탠드|거치대"], [r"직접\s*설치|설치는\s*지원되지|자가설치|별도설치"])),
    ("A/S", "고장나면 어디로 연락하나요?",
     ["제품 고장 시 A/S 접수 연락처 안내",
      "제조사 서비스센터를 통한 수리 접수 방법 안내"],
     ([r"1588-3366|삼성전자서비스|서비스\s*센터|서비스센터"],
      [r"A/?S|에이에스|수리|고장|불량|점검"])),
    ("보증", "무상 보증 기간이 얼마나 되나요?",
     ["무상 A/S 보증 기간 안내", "패널 보증 기간이 본체와 다른지에 대한 안내"],
     ([r"무상|보증"], [r"1년|2년|패널"])),
    ("벽걸이", "벽에 붙여서 쓸 수 있나요?",
     ["벽걸이 설치가 가능한 제품인지에 대한 안내",
      "벽걸이 설치 옵션을 추가 구매하는 방법 안내"],
     ([r"벽걸이"], [r"설치|브라켓|추가"])),
    ("벽걸이", "브라켓은 같이 오나요?",
     ["벽걸이 브라켓이 제품 구성에 포함되는지 안내",
      "벽걸이 브라켓을 별도로 추가 구매해야 하는지 안내"],
     ([r"브라켓"], [r"포함|추가\s*구매|함께\s*출고|별도|같이"])),
    ("호환", "쓰던 거치대 나사 간격이 맞을까요?",
     ["기존 벽걸이 브라켓과의 규격 호환 여부 안내",
      "VESA 나사 규격이 맞지 않을 수 있다는 안내"],
     ([r"브라켓|벽걸이|거치대"], [r"호환|규격|VESA|베사|나사"])),
    ("호환", "지금 쓰는 벽 거치대에 새 티비를 달 수 있을까요?",
     ["기존에 설치된 벽걸이 거치대를 새 제품에 재사용할 수 있는지 안내",
      "설치 기사가 현장에서 호환 여부를 확인한다는 안내"],
     ([r"브라켓|벽걸이|거치대"], [r"호환|규격|VESA|베사|나사"])),
    ("천정설치", "천장에 매달아 쓰고 싶은데 되나요?",
     ["천정형 설치 지원 여부 안내",
      "기사 방문 설치 범위에서 제외되는 설치 방식 안내"],
     ([r"천정|천장"], [r"설치"])),
    ("구성품", "조작기는 같이 들어있나요?",
     ["리모컨이 제품 구성품에 포함되는지 안내", "기본 동봉 구성품 목록 안내"],
     ([r"리모컨|리모콘"], [r"포함|동봉|같이|들어"])),
    ("구성품", "연결선도 같이 주시나요?",
     ["HDMI 케이블이 동봉되는지에 대한 안내",
      "연결 케이블을 별도로 구매해야 하는지 안내"],
     ([r"케이블"], [r"동봉|포함|별도\s*구매"])),
    ("단자", "안테나 선 꽂으면 방송 나오나요?",
     ["안테나를 직접 연결해 지상파 방송을 수신할 수 있는지 안내",
      "RF 단자 유무와 공중파 시청 가능 여부 안내"],
     ([r"RF\s*단자|안테나|공중파|지상파"], [r"없|불가|수신|셋톱|셋탑"])),
    ("단자", "노트북이랑 선으로 이어서 쓸 수 있어요?",
     ["노트북을 HDMI 로 연결해 사용할 수 있는지 안내",
      "PC 연결용 영상 입력 포트 지원 여부 안내"],
     ([r"HDMI"], [r"노트북|PC|연결"])),
    ("단자", "쓰던 셋톱을 그대로 연결해도 되나요?",
     ["기존 셋톱박스를 연결해 사용할 수 있는지 안내",
      "셋톱박스 연결 방식과 필요한 단자 안내"],
     ([r"셋톱|셋탑"], [r"연결|HDMI|사용"])),
    ("OTT", "넷플릭스 같은 건 바로 되나요?",
     ["제품 자체에서 넷플릭스 등 OTT 앱을 지원하는지 안내",
      "OTT 시청을 위해 별도 셋톱박스가 필요한지 안내"],
     ([r"넷플릭스|OTT"], [r"지원|가능|셋톱|셋탑|시청"])),
    ("OTT", "티빙 시청 가능한가요?",
     ["티빙 등 OTT 서비스 시청 가능 여부 안내",
      "인터넷 연결 시 이용 가능한 스트리밍 앱 안내"],
     ([r"넷플릭스|OTT|티빙"], [r"지원|가능|셋톱|셋탑|시청"])),
    ("이벤트", "온누리 받으려면 서류를 어떻게 내나요?",
     ["온누리상품권 환급 신청 방법 안내", "행사 혜택 신청 절차와 기한 안내"],
     ([r"온누리"], [r"신청|환급|접수"])),
    ("이벤트", "리뷰 쓰면 포인트는 언제 들어오나요?",
     ["리뷰 이벤트 참여 후 포인트가 지급되는 시점 안내",
      "네이버폼 작성 기준 혜택 발송 일정 안내"],
     ([r"리뷰\s*이벤트|네이버폼|포토리뷰"], [r"지급|발송|다음달|영업일"])),
    ("이벤트", "거래 내역서는 어디서 발급받나요?",
     ["구매내역서 발급 방법 안내",
      "결제 내역에서 주문 상세 정보를 확인하는 방법 안내"],
     ([r"구매내역서|거래명세|영수증|결제내역"], [r"발급|확인"])),
    ("배송일정", "설치 날짜는 어떻게 알 수 있나요?",
     ["설치 예정일을 확인하는 방법 안내",
      "설치 전날 알림톡으로 일정이 안내되는지에 대한 안내"],
     ([r"알림톡"], [r"설치\s*예정일|배송예정일|확인"])),
    ("정품보증", "새 제품 맞나요, 리퍼는 아니죠?",
     ["정품 미개봉 새 제품인지에 대한 안내",
      "반품 리퍼 제품을 취급하는지에 대한 안내"],
     ([r"리퍼|미개봉"], [r"새\s*제품|새상품|취급|아닙|않"])),
    ("스마트기능", "셋톱 없이 일반 방송 보는 것과 뭐가 다른가요?",
     ["셋톱박스 없이 방송 시청이 가능한지에 대한 안내",
      "일반 TV 와 스마트 모니터의 방송 수신 방식 차이 안내"],
     ([r"셋톱|셋탑"], [r"방송|지상파|공중파|RF"])),
]


def _targets(rule, rows) -> set[int]:
    must_all, must_any = rule
    found = set()
    for row in rows:
        answer = str(row.get("final_answer") or "")
        if not answer:
            continue
        if all(re.search(p, answer, re.I) for p in must_all) and any(
            re.search(p, answer, re.I) for p in must_any
        ):
            found.add(int(row["id"]))
    return found


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    database = Database(tmp_path_factory.mktemp("gpt-first") / "corpus.db")
    database.initialize()
    copied = _copy_all_active_learning(database)
    assert copied > 500, f"운영 corpus 복사 실패: {copied}"
    service = SimilarAnswerService(LearningRepository(database))
    pool = service.repository.candidates(store_code=STORE, limit=2000)
    diagnostics = service.repository.candidate_diagnostics(store_code=STORE)
    assert len(pool) > 500
    return {
        "service": service, "pool": pool, "diagnostics": diagnostics,
        "by_id": {int(r["id"]): r for r in pool},
    }


def _search(corpus, question, queries):
    """운영과 같은 경로. queries 가 비면 v5 까지의 동작 그대로다."""

    goal = {
        "customer_goal": None,
        "requested_information": question,
        "atomic_question": question,
        "retrieval_queries": list(queries),
        "order_evidence_required": None,
        "schedule_scoped": None,
    }
    corpus["service"].search(
        question, store_code=STORE, product_name=IDENTIFIED_PRODUCT,
        candidate_pool=corpus["pool"],
        candidate_diagnostics=corpus["diagnostics"],
        semantic_goal=goal, limit=3, hard_conflicts_only=True,
    )
    return corpus["service"].last_trace


def _rank(trace, targets):
    for position, identifier in enumerate(trace["candidate_ids"], start=1):
        if identifier in targets:
            return position
    return None


def _reachable(corpus, question, queries, targets) -> bool:
    """정답이 Hard Safety 를 통과해 이 상품에서 쓸 수 있는가.

    다른 모델 전용 답변은 strict identity 로 걸러지는 것이 맞다. 그런 질의는
    "검색이 못 찾은" 것이 아니라 "이 상품에는 답이 없는" 것이므로, recall 을
    말할 때 둘을 섞으면 안 된다.
    """

    subset = [r for r in corpus["pool"] if int(r["id"]) in targets]
    if not subset:
        return False
    goal = {"requested_information": question, "atomic_question": question,
            "retrieval_queries": list(queries)}
    corpus["service"].search(
        question, store_code=STORE, product_name=IDENTIFIED_PRODUCT,
        candidate_pool=subset, candidate_diagnostics=corpus["diagnostics"],
        semantic_goal=goal, limit=3, hard_conflicts_only=True,
    )
    return corpus["service"].last_trace["above_threshold_count"] > 0


@pytest.fixture(scope="module")
def benchmark(corpus):
    """BEFORE/AFTER 를 같은 corpus, 같은 정답 정의로 한 번만 돌린다."""

    results = []
    for category, question, queries, rule in BENCHMARK:
        targets = _targets(rule, corpus["pool"])
        before = _search(corpus, question, ())
        before_rank = _rank(before, targets)
        after = _search(corpus, question, queries)
        results.append({
            "category": category, "question": question, "queries": queries,
            "targets": targets,
            "before": before_rank,
            "after": _rank(after, targets),
            "after_top6": list(after["candidate_ids"][:6]),
            "reachable": _reachable(corpus, question, queries, targets),
        })
    return results


def _recall(results, key, k, *, answerable_only):
    rows = [r for r in results if r["reachable"]] if answerable_only else results
    hit = sum(1 for r in rows if r[key] and r[key] <= k)
    return 100.0 * hit / max(len(rows), 1)


# ===========================================================================
# 3. benchmark -- BEFORE / AFTER
# ===========================================================================

def test_the_benchmark_is_the_hard_kind(benchmark):
    """질의가 저장된 질문의 복사본이면 아무것도 증명하지 못한다."""

    assert len(BENCHMARK) == 25
    assert len({c for c, *_rest in BENCHMARK}) >= 12
    assert all(r["targets"] for r in benchmark), "정답이 없는 질의가 있다"
    assert sum(1 for r in benchmark if r["reachable"]) >= 20


def test_fixture_queries_do_not_copy_the_stored_questions(corpus):
    """GPT ① 은 corpus 를 보지 않는다. fixture 도 그래야 한다.

    검색 방향이 정답의 원문을 베낀 것이면 recall 이 오르는 것은 당연하고 아무
    의미도 없다. 각 검색 방향을 corpus 전체와 비교해, 어느 저장된 질문과도
    사실상 같은 문장이 아님을 확인한다.
    """

    stored = [
        (int(r["id"]),
         normalize_learning_question(r["question_original_masked"]))
        for r in corpus["pool"]
    ]
    for _category, _question, queries, _rule in BENCHMARK:
        for query in queries:
            normalised = normalize_learning_question(query)
            worst = max(
                (SimilarAnswerService._similarity(normalised, text), identifier)
                for identifier, text in stored
            )
            assert worst[0] < 0.75, (query, worst)


def test_multi_query_retrieval_beats_the_single_query_baseline(benchmark):
    """이 작업의 존재 이유. 같은 corpus, 같은 정답, 질의만 다르다."""

    before6 = _recall(benchmark, "before", 6, answerable_only=True)
    after6 = _recall(benchmark, "after", 6, answerable_only=True)
    before1 = _recall(benchmark, "before", 1, answerable_only=True)
    after1 = _recall(benchmark, "after", 1, answerable_only=True)
    assert after6 > before6, (before6, after6)
    assert after1 > before1, (before1, after1)
    # 측정된 값은 68.2% -> 90.9%. 문턱은 회귀 감지용이며 측정치보다 낮게 둔다.
    assert after6 >= 85.0, after6


def test_the_delivered_top_three_carry_the_answer(benchmark):
    """실제로 GPT ② 에게 가는 것은 상위 3건이다. 거기 들어와야 의미가 있다."""

    after3 = _recall(benchmark, "after", 3, answerable_only=True)
    before3 = _recall(benchmark, "before", 3, answerable_only=True)
    assert after3 > before3, (before3, after3)
    assert after3 >= 85.0, after3


def test_no_query_regresses_against_the_baseline(benchmark):
    """전체는 좋아졌는데 어떤 질의는 나빠졌다면 그건 맞바꾼 것이다."""

    regressed = [
        (r["question"], r["before"], r["after"])
        for r in benchmark
        if r["before"] is not None
        and (r["after"] is None or r["after"] > r["before"] + 3)
    ]
    assert not regressed, regressed


def test_extra_queries_do_not_flood_the_candidates_with_noise(benchmark):
    """recall 만 올리고 무관 후보를 쏟아부으면 좋은 구조가 아니다."""

    for row in benchmark:
        assert len(row["after_top6"]) <= 6
        assert len(set(row["after_top6"])) == len(row["after_top6"]), "중복 후보"
    precision = [
        sum(1 for i in row["after_top6"] if i in row["targets"])
        / max(len(row["after_top6"]), 1)
        for row in benchmark
    ]
    # 실측 0.35. baseline 0.22 보다 높다 -- 넓힌 검색이 더 정확해졌다.
    assert sum(precision) / len(precision) > 0.25


# ===========================================================================
# 4. merge / deduplicate / fallback
# ===========================================================================

def test_one_learning_found_by_two_queries_appears_once(corpus):
    """같은 행이 여러 질의에서 걸려도 GPT ② 에는 한 번만 간다."""

    _category, question, queries, _rule = BENCHMARK[0]
    trace = _search(corpus, question, queries)
    identifiers = trace["candidate_ids"]
    assert len(identifiers) == len(set(identifiers))
    assert len(trace["selected_learning_ids"]) == len(
        set(trace["selected_learning_ids"])
    )


def test_retrieval_without_queries_is_exactly_the_previous_behaviour(corpus):
    """§31 fallback. GPT ① 이 검색 방향을 못 내도 검색은 원문으로 돈다."""

    _category, question, _queries, _rule = BENCHMARK[3]
    absent = dict(_search(corpus, question, ()))
    empty = dict(_search(corpus, question, []))
    malformed = dict(_search(corpus, question, ["", "   "]))
    assert absent["candidate_ids"] == empty["candidate_ids"]
    assert absent["candidate_ids"] == malformed["candidate_ids"]
    assert absent["query_variants"] == [normalize_learning_question(question)]


def test_the_queries_are_recorded_so_a_reviewer_can_see_them(corpus):
    """무엇으로 찾았는지가 남아야 사람이 검증할 수 있다."""

    _category, question, queries, _rule = BENCHMARK[0]
    trace = _search(corpus, question, queries)
    assert trace["semantic_goal"]["retrieval_queries"] == list(queries)
    assert len(trace["query_variants"]) > 1


def test_code_never_reclassifies_the_queries(corpus):
    """CODE 는 검색 방향의 의미를 다시 판단하지 않는다.

    질의는 다른 어떤 질의와도 같은 방식으로 처리된다: 정규화해서 같은 점수
    함수에 넣는 것뿐이고, 내용을 보고 갈라지는 분기가 없다. 뜻이 통하지 않는
    문장을 넣어도 예외 없이 같은 경로로 처리된다는 것으로 확인한다.
    """

    _category, question, _queries, _rule = BENCHMARK[0]
    nonsense = _search(corpus, question, ["ZZZQQQ 존재하지 않는 검색 방향 XYZ"])
    assert nonsense["candidate_ids"], "의미 없는 질의가 검색을 죽이면 안 된다"
    assert nonsense["rejection_counts"]["SEMANTIC_GOAL_MISMATCH"] == 0


@pytest.mark.parametrize(
    ("category", "question", "queries"),
    [(c, q, r) for c, q, r, _rule in BENCHMARK],
    ids=[f"{i}-{c}" for i, (c, *_rest) in enumerate(BENCHMARK)],
)
def test_no_candidate_is_removed_for_a_soft_semantic_reason(
    corpus, category, question, queries,
):
    """v5 의 계약은 그대로다. 질의가 늘어도 의미로 지우지 않는다."""

    soft = {
        "TOPIC_MISMATCH", "TOPIC_PARTIAL_COVERAGE", "SEMANTIC_GOAL_MISMATCH",
        "BELOW_SIMILARITY_THRESHOLD", "CONTEXT_POLICY_REJECTED",
    }
    trace = _search(corpus, question, queries)
    counts = trace.get("rejection_counts") or {}
    offending = {k: v for k, v in counts.items() if k in soft and v}
    assert not offending, (question, offending)


# ===========================================================================
# 5. per-atom provenance / compound
# ===========================================================================

def test_each_atom_carries_its_own_retrieval_queries(tmp_path, monkeypatch):
    """한 atom 의 검색 방향이 다른 atom 의 근거를 오염시키지 않는다."""

    run = _run(
        tmp_path, monkeypatch, name="per-atom",
        question="설치는 누가 하나요? 그리고 해상도는 어떻게 되나요?",
        product_name=IDENTIFIED_PRODUCT,
        atoms=[
            {"text": "설치는 누가 하나요?", "action": "INSTALLATION_METHOD",
             "requested_information": "설치 주체",
             "requested_attribute": "ACTOR",
             "retrieval_queries": [
                 "기사 방문 설치로 진행되는 상품인지에 대한 안내"]},
            {"text": "해상도는 어떻게 되나요?", "action": "PRODUCT_SPEC",
             "requested_information": "해상도",
             "requested_attribute": "SPEC_VALUE",
             "retrieval_queries": ["제품의 해상도 사양에 대한 안내"]},
        ],
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT,),
    )
    assert run.prompt is not None, run.error
    learning = (run.prompt["input"] or {}).get("similar_approved_answers") or []
    for item in learning:
        assert item.get("matched_subquestion") in {
            "설치는 누가 하나요?", "해상도는 어떻게 되나요?",
        }, item.get("matched_subquestion")


def test_the_searched_directions_are_observable_but_not_in_the_prompt(
    tmp_path, monkeypatch,
):
    """무엇으로 찾았는지는 사람이 볼 수 있어야 하고, GPT ② 는 볼 필요가 없다.

    검색 방향은 운영 추적용이지 근거가 아니다. 프롬프트에 넣으면 모델이 그것을
    사실처럼 읽을 수 있고, 예전에 retrieval trace 가 프롬프트의 94.7% 를 차지한
    적도 있다. 새 logging framework 없이 기존 자리에 얹는다.
    """

    queries = ["기존 폐가전을 무상으로 수거해 주는지에 대한 안내"]
    run = _run(
        tmp_path, monkeypatch, name="observable",
        question="폐가전도 가져가시나요?", product_name=IDENTIFIED_PRODUCT,
        atoms=[{
            "text": "폐가전도 가져가시나요?", "action": "COLLECTION",
            "requested_information": "폐가전 수거",
            "requested_attribute": "EXISTENCE_OR_CAPABILITY",
            "retrieval_queries": queries,
        }],
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT,),
    )
    assert run.prompt is not None, run.error
    context = run.context or {}
    traces = ((context.get("learning_retrieval") or {}).get("subquestions") or [])
    searched = [
        (trace.get("semantic_goal") or {}).get("retrieval_queries") or []
        for trace in traces
    ]
    assert queries in searched, searched
    assert "learning_retrieval" not in (run.prompt.get("input") or {})
    assert queries[0] not in run.raw_prompt


def test_a_compound_inquiry_keeps_product_evidence_beside_learning(
    tmp_path, monkeypatch,
):
    """검색 방향이 붙어도 Product 경로는 그대로다 (§33)."""

    run = _run(
        tmp_path, monkeypatch, name="compound-queries",
        question="해상도가 어떻게 되나요? 그리고 폐가전도 가져가시나요?",
        product_name=IDENTIFIED_PRODUCT,
        atoms=[
            _spec_atom("해상도가 어떻게 되나요?", "해상도"),
            {"text": "폐가전도 가져가시나요?", "action": "COLLECTION",
             "requested_information": "폐가전 수거",
             "requested_attribute": "EXISTENCE_OR_CAPABILITY",
             "retrieval_queries": [
                 "기존 폐가전을 무상으로 수거해 주는지에 대한 안내"]},
        ],
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT,),
    )
    assert run.prompt is not None, run.error
    assert (run.prompt["input"] or {}).get("product_catalog"), (
        "Learning 검색을 바꾸면서 Product 근거가 사라졌다"
    )


# ===========================================================================
# 6. Hard Safety -- wrong evidence / no evidence 는 그대로여야 한다
# ===========================================================================

def test_extra_queries_do_not_let_another_model_fact_through(
    tmp_path, monkeypatch,
):
    """다른 상품의 사양이 오더라도 출처가 표시된다 (§40, P0-2 갱신).

    이전 계약은 "다른 상품 Learning 은 프롬프트에 오지 않는다" 였다. 그 계약은
    CODE 가 의미 판단으로 후보를 삭제해야만 지킬 수 있었고, 실제 서버 문의
    688218182 / 688218219 에서 질문에 정확히 답하는 Learning(LID 117 / 72)까지
    같은 규칙으로 지워졌다.

    현재 계약은 "다른 상품 Learning 은 출처가 표시된 채 전달되고, 적용 여부는
    GPT ② 가 판단한다" 이다. 따라서 여기서 확인할 것은 부재가 아니라 라벨이다.
    다른 모델의 사양이 근거 없이 고객에게 단정되는 것은 evidence_origin 라벨과
    프롬프트 지시, 그리고 validator 의 ungrounded-claim 검사가 막는다.
    """

    run = _run(
        tmp_path, monkeypatch, name="wrong-evidence-mq",
        question="이 제품 해상도가 어떻게 되나요?",
        product_name=UNIDENTIFIED_PRODUCT,
        atoms=[{
            "text": "이 제품 해상도가 어떻게 되나요?", "action": "PRODUCT_SPEC",
            "requested_information": "해상도",
            "requested_attribute": "SPEC_VALUE",
            "retrieval_queries": [
                "제품의 해상도 사양에 대한 안내",
                "4K UHD 해상도를 지원하는지에 대한 안내",
            ],
        }],
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT, UNIDENTIFIED_PRODUCT),
    )
    assert run.prompt is not None, run.error
    for item in (
        (run.prompt["input"] or {}).get("similar_approved_answers") or []
    ):
        origin = item.get("evidence_origin") or {}
        assert origin, ("Learning 후보에 출처 라벨이 없다", item.get("learning_example_id"))
        if str(item.get("source_product_name") or "") == UNIDENTIFIED_PRODUCT:
            continue
        # 다른 상품에서 온 후보는 반드시 그렇게 표시되어야 하고, 현재 상품에서
        # 온 자료로 위장되어서는 안 된다.
        assert origin.get("identity") != "SAME_PRODUCT", (
            "다른 모델 Learning 이 현재 상품 자료로 표시됐다",
            item.get("source_product_name"),
        )
        # 라벨은 출처 정보일 뿐 적용 금지 지시가 붙지 않는다.
        assert "note" not in origin, (
            "Learning 출처 라벨에 사용 지시가 붙었다",
            item.get("source_product_name"),
        )


def test_a_question_with_no_stored_answer_still_goes_unresolved(
    tmp_path, monkeypatch,
):
    """검색을 잘한다고 없는 근거를 만들어 답하면 안 된다 (§41)."""

    question = "이 제품 소비전력이 몇 와트인가요?"
    stub = _EvidenceReadingStub(
        picker=lambda ctx: {"unresolved": [question], "can_auto_post": False}
    )
    run = _run(
        tmp_path, monkeypatch, name="no-evidence-mq", question=question,
        product_name=IDENTIFIED_PRODUCT,
        atoms=[{
            "text": question, "action": "PRODUCT_SPEC",
            "requested_information": "소비전력",
            "requested_attribute": "SPEC_VALUE",
            "retrieval_queries": ["제품의 소비전력 수치에 대한 안내"],
        }],
        stub=stub, learning_products=(IDENTIFIED_PRODUCT,),
    )
    assert run.error is None, run.error
    result = run.outcome.result
    draft = (result.metadata.get("hybrid") or {}).get("draft") or {}
    assert draft["unresolved"] == [question]
    assert draft["can_auto_post"] is False
    assert result.needs_review is True
    assert result.metadata["pipeline_trace"]["answer"]["unresolved"] == 1


# ===========================================================================
# 7. 비용 -- GPT 호출 횟수, prompt 크기, 시간
# ===========================================================================

def test_the_number_of_gpt_calls_is_unchanged(tmp_path, monkeypatch):
    """§5 의 핵심 제약. 검색 방향은 기존 GPT ① 응답에 얹혀서 온다."""

    stub = _EvidenceReadingStub()
    run = _run(
        tmp_path, monkeypatch, name="call-count",
        question="설치는 누가 하나요?", product_name=IDENTIFIED_PRODUCT,
        atoms=[{
            "text": "설치는 누가 하나요?", "action": "INSTALLATION_METHOD",
            "requested_information": "설치 주체", "requested_attribute": "ACTOR",
            "retrieval_queries": [
                "기사 방문 설치로 진행되는 상품인지에 대한 안내",
                "고객이 직접 설치해야 하는지에 대한 안내",
            ],
        }],
        stub=stub, learning_products=(IDENTIFIED_PRODUCT,),
    )
    assert run.prompt is not None, run.error
    tasks = [str(c["task"]).upper() for c in stub.captured]
    # 이해는 별도 stub 이 받으므로 여기 잡히는 것은 DRAFT 뿐이어야 한다.
    assert tasks.count("DRAFT") == 1, tasks
    assert not [t for t in tasks if t != "DRAFT"], tasks


def test_the_draft_prompt_still_fits_its_budget(tmp_path, monkeypatch):
    from services.learning_context_service import DRAFT_PROMPT_BUDGET_CHARS

    run = _run(
        tmp_path, monkeypatch, name="budget-mq",
        question="폐가전 수거되나요? 설치는 누가 하나요? 해상도는요?",
        product_name=IDENTIFIED_PRODUCT,
        atoms=[
            {"text": "폐가전 수거되나요?", "action": "COLLECTION",
             "requested_information": "폐가전 수거",
             "requested_attribute": "EXISTENCE_OR_CAPABILITY",
             "retrieval_queries": ["기존 폐가전 무상 수거 여부에 대한 안내",
                                   "설치 방문 시 헌 가전 회수 절차 안내"]},
            {"text": "설치는 누가 하나요?", "action": "INSTALLATION_METHOD",
             "requested_information": "설치 주체",
             "requested_attribute": "ACTOR",
             "retrieval_queries": ["기사 방문 설치 진행 여부에 대한 안내",
                                   "자가설치 가능 여부에 대한 안내"]},
            _spec_atom("해상도는요?", "해상도"),
        ],
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT,),
    )
    assert run.prompt is not None, run.error
    assert len(run.raw_prompt) < DRAFT_PROMPT_BUDGET_CHARS, len(run.raw_prompt)


def test_multi_query_search_stays_within_a_reasonable_time(corpus):
    """CODE 구간만 잰다. GPT latency 는 여기서 측정하지 않는다."""

    _category, question, queries, _rule = BENCHMARK[0]
    _search(corpus, question, queries)
    started = time.perf_counter()
    for _ in range(3):
        _search(corpus, question, queries)
    per_search = (time.perf_counter() - started) / 3
    # 후보 900건에 대한 순수 계산. 회귀 감지용 상한이며 실측(약 0.44s)의 4배.
    assert per_search < 2.0, per_search


# ===========================================================================
# 8. semantic index lifecycle
# ===========================================================================

def test_the_index_coverage_can_be_reported_without_a_network_call():
    """누락을 사람이 볼 수 있어야 한다. 이것이 20건이 조용히 빠진 이유다."""

    from scripts.rebuild_learning_semantic_index import coverage

    report = coverage(SOURCE_DB, INDEX_PATH)
    assert report["eligible"] > 0
    assert report["indexed"] <= report["eligible"]
    assert report["missing"] == report["eligible"] - report["indexed"]
    assert 0.0 <= report["coverage_pct"] <= 100.0


def test_index_eligibility_is_the_retrieval_one_and_nothing_more():
    """index 가 품질/상품 판단을 미리 하면 그것은 filter 지 cache 가 아니다."""

    from scripts.rebuild_learning_semantic_index import eligible_rows

    rows = eligible_rows(SOURCE_DB)
    assert rows
    connection = sqlite3.connect(
        f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True
    )
    try:
        active = connection.execute(
            "SELECT COUNT(*) FROM learning_examples WHERE active=1"
        ).fetchone()[0]
    finally:
        connection.close()
    # 텍스트가 없는 행만 빠진다. 그 외의 이유로 줄어들면 안 된다.
    assert len(rows) <= active
    assert len(rows) >= active - 10


def test_a_learning_without_a_vector_is_still_retrievable(corpus):
    """index 는 파생 데이터다. 없거나 낡아도 검색이 죽으면 안 된다.

    누락 20건이 있는 지금도 lexical 경로는 그대로 돌아야 하고, 그래서 이번
    개선이 index 재생성을 기다리지 않고 효과를 낸다.
    """

    index = LearningSemanticIndex.load(INDEX_PATH)
    indexed = set(index.vectors)
    unindexed = [
        int(r["id"]) for r in corpus["pool"] if int(r["id"]) not in indexed
    ]
    if not unindexed:
        pytest.skip("index 가 100% 커버리지라 이 대조군은 성립하지 않는다")
    row = corpus["by_id"][unindexed[0]]
    trace = _search(corpus, str(row["question_original_masked"] or "")[:120], ())
    assert trace["candidate_ids"], "벡터가 없는 행의 질문으로도 검색은 돌아야 한다"
