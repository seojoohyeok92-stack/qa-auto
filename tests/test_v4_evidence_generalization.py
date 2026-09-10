"""Evidence Pipeline 이 특정 문의가 아니라 일반적으로 작동하는지 검증한다.

v4 replay 는 서버에서 실패한 여섯 건을 재현한다. 이 파일은 그 여섯 건이 우연히
고쳐진 것이 아님을 보이는 쪽이다: 실제 운영 Catalog 와 Learning 을 source 로
삼아, fact 종류가 다르고 질문 표현이 다르고 evidence source 조합이 달라도 같은
파이프라인이 같은 방식으로 동작하는지를 본다.

읽는 사람이 알아야 할 두 가지 규칙:

* 정답 값을 이 파일에 적어두지 않는다. Product fact 의 기대값은 실행 시점에
  ``data/model_data_with_color.json`` 에서 읽고, Learning 의 기대값은 운영 DB 의
  행에서 읽는다. 그래야 카탈로그가 바뀌면 테스트가 같이 따라간다.
* 질문 문장은 테스트가 만들지만, 그 표현이 production code 의 어떤 표에도
  등장하지 않아야 한다는 것이 요점이다. "벽에 붙여서 쓸 수 있나요?" 가
  통과하는 이유는 그 문장을 아는 코드가 있어서가 아니라, 상품이 식별되면
  카탈로그 행 전체가 GPT ② 에게 가기 때문이다.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from answer.governance_models import GptProviderSettings
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.learning_repository import LearningRepository
from repositories.product_catalog_repository import ProductCatalogRepository
from services.answer_service import AnswerService
from services.gpt_governance_service import GovernedHybridAnswerService
from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService
from services.similar_answer_service import SimilarAnswerService

SOURCE_DB = Path("data/oje_automation.db")
CATALOG = Path("data/model_data_with_color.json")
STORE = "OJE_PLUS"

# 카탈로그가 식별할 수 있는 실제 상품(대조군의 기준점).
IDENTIFIED_PRODUCT = (
    "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
)
# 이 파일은 카탈로그(model_data_with_color.json) 경로를 검증한다. 상품 식별
# 계약상 product_id 가 있으면 listing store(product_facts.db)가 먼저 답하므로,
# 여기서는 product_id 를 비워 이름 기반 카탈로그 매칭(계약 2번)을 태운다.
#
# 이전에는 harness 가 모든 케이스에 "11815213767" 을 박아 넣었는데, 그것은
# 삼탠바이미 listing 의 id 이고 IDENTIFIED_PRODUCT 는 LH50BEFHLGFXKR 이다.
# id 가 무시되던 동안에는 무해했지만, 이제 id 가 가장 강한 식별자이므로 그
# 조합은 "A 의 id 와 B 의 이름" 이라는 존재하지 않는 상품을 시험하게 된다.
CATALOGUE_ONLY_PRODUCT_ID = ""

# 상품명에 model code 가 없어 카탈로그가 식별하지 못하는 실제 상품.
UNIDENTIFIED_PRODUCT = (
    "삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 거치대 화이트"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _catalog_record(product_name: str):
    match = ProductCatalogRepository().match(product_name=product_name)
    assert match.record is not None, f"카탈로그가 식별해야 하는 상품: {product_name}"
    return match


# ---------------------------------------------------------------------------
# 공용 harness
# ---------------------------------------------------------------------------

def _semantic_payload(atoms, *, purchase_state="UNKNOWN"):
    actions = [item["action"] for item in atoms]
    return {
        "primary_action": actions[0],
        "secondary_actions": list(dict.fromkeys(actions[1:])),
        "request_type": "QUESTION",
        "objects": [],
        "atomic_questions": [dict(item) for item in atoms],
        "deadline": None,
        "constraints": [],
        "negation": False,
        "conditional": False,
        "requires_order_context": False,
        "requires_delivery_schedule": False,
        "asks_delivery_schedule": False,
        "asks_delivery_outcome": False,
        "purchase_state": purchase_state,
        "confidence": 0.95,
    }


class _SemanticStub:
    name = "semantic_stub"

    def __init__(self, payload):
        self.payload = payload

    def generate_json(self, *, task, prompt, context):
        return dict(self.payload)


class _EvidenceReadingStub:
    """GPT ②. 주어진 evidence 중 무엇을 고를지만 deterministic 하게 대체한다.

    ``picker`` 는 production 이 만든 context 를 받아 사용할 evidence 를 고른다.
    prompt 를 우회하지 않고 실제로 읽는다는 점이 중요하다 -- 이 stub 이 아무것도
    고를 수 없다면 그것은 evidence 가 도달하지 않았다는 뜻이다.
    """

    name = "draft_stub"
    call_records: list = []

    def __init__(self, picker=None):
        self.captured: list[dict] = []
        self.picker = picker or (lambda ctx: {})

    def generate_json(self, *, task, prompt, context):
        self.captured.append({"task": task, "prompt": prompt, "context": context})
        chosen = self.picker(context) or {}
        unresolved = list(chosen.get("unresolved") or [])
        return {
            "answer": chosen.get("answer") or "확인 후 안내드리겠습니다.",
            "confidence": 0.9,
            "used_facts": [],
            "missing_information": [],
            "requires_review": bool(unresolved),
            "warnings": [],
            "learning_usage": [],
            "historical_usage": [],
            "subquestion_results": [],
            "evidence_decisions": list(chosen.get("evidence_decisions") or []),
            "used_template_ids": list(chosen.get("used_template_ids") or []),
            "used_product_facts": list(chosen.get("used_product_facts") or []),
            "used_learning_ids": list(chosen.get("used_learning_ids") or []),
            "used_historical_ids": list(chosen.get("used_historical_ids") or []),
            "ignored_evidence": list(chosen.get("ignored_evidence") or []),
            "unresolved": unresolved,
            "can_auto_post": bool(chosen.get("can_auto_post")),
            "reason": "generalization",
        }


def _copy_learning(target: Database, product_names, limit=60) -> int:
    """운영 DB 의 ACTIVE Learning 을 tmp DB 로 복사한다 (원본 READ ONLY)."""

    source = sqlite3.connect(f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in product_names)
        rows = source.execute(
            f"SELECT * FROM learning_examples WHERE active=1"
            f" AND product_name IN ({marks}) LIMIT {int(limit)}",
            tuple(product_names),
        ).fetchall()
        if not rows:
            return 0
        inquiry_ids = sorted({
            int(r["inquiry_id"]) for r in rows if r["inquiry_id"] is not None
        })
        inquiries = []
        if inquiry_ids:
            im = ",".join("?" for _ in inquiry_ids)
            inquiries = source.execute(
                f"SELECT * FROM inquiries WHERE id IN ({im})", inquiry_ids
            ).fetchall()
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
                # ``learning_examples`` 는 approval_history 와 answer_drafts 도
                # 참조한다. 둘 다 ON DELETE SET NULL 인 선택적 링크이고 이
                # fixture 는 복사하지 않으므로, 그 두 칸만 비운다. 출처인
                # inquiry_id 는 위에서 함께 복사했으므로 그대로 둔다.
                optional = {"approval_history_id", "answer_draft_id"}
                connection.executemany(sql, [
                    tuple(None if n in optional else r[n] for n in names)
                    for r in table_rows
                ])
        return len(rows)
    finally:
        source.close()


def _run(tmp_path, monkeypatch, *, name, question, product_name, atoms,
         stub, learning_products=(), option_name=None,
         product_id=CATALOGUE_ONLY_PRODUCT_ID):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    if learning_products:
        _copy_learning(database, tuple(learning_products))

    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": STORE,
        "source_type": "TEST",
        "source_question_id": f"gen-{name}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": question,
        "product_id": product_id,
        "product_name": product_name,
        "option_name": option_name,
        "raw_json": {},
    }).inquiry_id

    governed = GovernedHybridAnswerService(
        database, provider=stub,
        settings=GptProviderSettings(provider_name="fake"),
    )
    import services.answer_service as answer_module

    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(answer_module, "notify_qna_safely", lambda **_k: False)

    outcome, error = None, None
    try:
        outcome = AnswerService(
            database, hybrid_service=governed,
            semantic_analyzer=GptSemanticAnalyzerService(
                _SemanticStub(_semantic_payload(atoms))
            ),
        ).generate_for_inquiry(inquiry_id)
    except Exception as exc:  # noqa: BLE001
        error = exc

    drafts = [c for c in stub.captured if str(c["task"]).upper() == "DRAFT"]
    return SimpleNamespace(
        database=database, outcome=outcome, error=error,
        prompt=json.loads(drafts[-1]["prompt"]) if drafts else None,
        raw_prompt=drafts[-1]["prompt"] if drafts else "",
        context=drafts[-1]["context"] if drafts else None,
    )


def _spec_atom(text, info="제품 사양"):
    return {
        "text": text, "action": "PRODUCT_SPEC",
        "requested_information": info, "requested_attribute": "SPEC_VALUE",
    }


def _catalog_values(prompt) -> dict[str, str]:
    block = (prompt.get("input") or {}).get("product_catalog") or {}
    return {
        str(item.get("field_key")): str(item.get("value"))
        for item in (block.get("facts") or [])
    }


# ===========================================================================
# 1. Product fact 일반화 -- 서로 다른 fact 종류가 같은 경로로 도달하는가
# ===========================================================================

# (카탈로그 field, ProductKnowledge field_key, 질문) -- 값은 실행 시 카탈로그에서 읽는다
PRODUCT_FACT_CASES = [
    ("resolution", "resolution", "이 제품 해상도가 어떻게 되나요?"),
    ("size_inch", "screen_size", "화면이 몇 인치인가요?"),
    ("hz", "refresh_rate", "주사율은 얼마인가요?"),
    ("vesa", "vesa_mm", "벽에 붙여서 쓸 수 있나요?"),
    ("weight", "weight_catalog", "무겁나요?"),
    ("brand", "brand", "어느 회사 제품인가요?"),
    ("model", "model_name", "정확한 모델명이 뭔가요?"),
    ("speaker", "speaker_present", "소리는 나오나요?"),
]


@pytest.mark.parametrize(
    ("catalog_field", "field_key", "question"),
    PRODUCT_FACT_CASES,
    ids=[c[1] for c in PRODUCT_FACT_CASES],
)
def test_every_catalogued_fact_kind_reaches_gpt2(
    tmp_path, monkeypatch, catalog_field, field_key, question,
):
    """상품이 식별되면 fact 종류와 무관하게 검증 사양이 GPT ② 까지 간다.

    기대값은 카탈로그에서 읽는다. 이 테스트가 지키는 것은 어떤 값이 아니라
    "fact 종류가 달라도 같은 경로를 쓴다" 는 성질이다.
    """

    match = _catalog_record(IDENTIFIED_PRODUCT)
    expected = match.record.get(catalog_field)
    if expected in (None, "", [], {}):
        pytest.skip(f"이 모델은 {catalog_field} 를 카탈로그에 갖고 있지 않다")

    run = _run(
        tmp_path, monkeypatch, name=f"pf-{field_key}", question=question,
        product_name=IDENTIFIED_PRODUCT, atoms=[_spec_atom(question)],
        stub=_EvidenceReadingStub(),
    )
    assert run.prompt is not None, run.error

    catalog = (run.prompt["input"] or {}).get("product_catalog")
    assert catalog, "식별된 상품인데 VERIFIED PRODUCT CATALOG FACTS 가 없다"
    assert catalog["identity_status"] in {"EXACT", "UNIQUE_MATCH"}
    values = _catalog_values(run.prompt)
    assert field_key in values, (
        f"{field_key} 가 GPT ② context 에서 사라졌다", sorted(values)
    )


@pytest.mark.parametrize("question", [
    "이 제품 해상도가 어떻게 되나요?",
    "화질 사양이 어떻게 되나요?",
    "UHD 제품인가요?",
    "화면 선명도가 어느 정도인가요?",
])
def test_the_same_fact_survives_different_phrasings(
    tmp_path, monkeypatch, question,
):
    """같은 fact 를 다른 표현으로 물어도 같은 카탈로그 값이 도달한다.

    이 표현들 중 어느 것도 production code 의 표에 없다. 통과하는 이유는
    상품이 식별되면 카탈로그 행 전체가 전달되기 때문이다.
    """

    match = _catalog_record(IDENTIFIED_PRODUCT)
    expected = str(match.record["resolution"])

    run = _run(
        tmp_path, monkeypatch, name=f"ph-{abs(hash(question)) % 10000}",
        question=question, product_name=IDENTIFIED_PRODUCT,
        atoms=[_spec_atom(question)], stub=_EvidenceReadingStub(),
    )
    assert run.prompt is not None, run.error
    assert _catalog_values(run.prompt).get("resolution") == expected
    assert expected in run.raw_prompt


def test_gpt2_can_answer_from_the_catalogue_value_it_was_given(
    tmp_path, monkeypatch,
):
    """전달 → 선택 → 최종 답변까지가 실제 카탈로그 값으로 이어진다."""

    match = _catalog_record(IDENTIFIED_PRODUCT)
    expected = str(match.record["resolution"])

    def picker(context):
        values = {
            str(f.get("field_key")): str(f.get("value"))
            for f in ((context.get("product_catalog") or {}).get("facts") or [])
        }
        if "resolution" not in values:
            return {}
        return {
            "answer": f"해당 제품의 해상도는 {values['resolution']} 입니다.",
            "used_product_facts": ["resolution"],
            "can_auto_post": True,
        }

    run = _run(
        tmp_path, monkeypatch, name="pf-answer",
        question="이 제품 해상도가 어떻게 되나요?",
        product_name=IDENTIFIED_PRODUCT,
        atoms=[_spec_atom("이 제품 해상도가 어떻게 되나요?")],
        stub=_EvidenceReadingStub(picker),
    )
    assert run.error is None, run.error
    result = run.outcome.result
    assert expected in result.answer
    draft = (result.metadata.get("hybrid") or {}).get("draft") or {}
    assert draft["used_product_facts"] == ["resolution"]


# ===========================================================================
# 2. Product identity 안전성
# ===========================================================================

def test_an_ambiguous_listing_never_yields_verified_facts(tmp_path, monkeypatch):
    """후보가 둘이면 코드가 하나를 고르지 않는다.

    운영 카탈로그에서 실제로 모호한 상품명을 찾아 쓴다. 두 후보는 weight 나
    resolution 같은 실제 field 에서 값이 다르므로, 하나를 골랐다면 다른 모델의
    사양을 이 상품의 사실로 말하게 된다.
    """

    repo = ProductCatalogRepository()
    source = sqlite3.connect(f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True)
    try:
        names = [
            r[0] for r in source.execute(
                "SELECT DISTINCT product_name FROM inquiries"
                " WHERE product_name IS NOT NULL AND product_name<>''"
            )
        ]
    finally:
        source.close()
    ambiguous = next(
        (n for n in names if repo.match(product_name=n).status == "AMBIGUOUS"),
        None,
    )
    assert ambiguous, "운영 데이터에 AMBIGUOUS 상품이 하나는 있어야 한다"
    match = repo.match(product_name=ambiguous)
    assert match.model_key is None and match.record is None
    assert len(match.candidates) >= 2

    run = _run(
        tmp_path, monkeypatch, name="ambiguous",
        question="이 제품 해상도가 어떻게 되나요?", product_name=ambiguous,
        atoms=[_spec_atom("이 제품 해상도가 어떻게 되나요?")],
        stub=_EvidenceReadingStub(),
    )
    assert run.prompt is not None, run.error
    inp = run.prompt["input"]
    assert inp.get("product_catalog") is None, "AMBIGUOUS 인데 검증 사양이 생겼다"
    candidates = inp.get("product_candidates")
    assert candidates and candidates["identity_status"] == "AMBIGUOUS"
    assert len(candidates["models"]) == len(match.candidates)
    assert inp["product_identity"]["matched"] is False
    # 후보라는 사실과 승격 금지가 프롬프트에 명시되어야 한다.
    assert "product_candidates" in run.prompt["product_information_tiers"]


def test_an_unidentified_listing_still_carries_its_own_metadata(
    tmp_path, monkeypatch,
):
    """식별 실패가 상품 자체를 감추는 이유가 되지 않는다."""

    run = _run(
        tmp_path, monkeypatch, name="notfound",
        question="이 제품 해상도가 어떻게 되나요?",
        product_name=UNIDENTIFIED_PRODUCT,
        atoms=[_spec_atom("이 제품 해상도가 어떻게 되나요?")],
        stub=_EvidenceReadingStub(),
    )
    assert run.prompt is not None, run.error
    facts = run.prompt["allowed_facts"]
    assert facts.get("product.name") == UNIDENTIFIED_PRODUCT
    assert run.prompt["input"]["product_identity"]["status"] == "NOT_FOUND"
    assert run.prompt["input"].get("product_catalog") is None


def test_identity_states_are_exhaustive_and_only_two_carry_a_record():
    """식별 결과는 네 상태뿐이고, record 를 갖는 것은 둘뿐이다."""

    from repositories.product_catalog_repository import (
        AMBIGUOUS, EXACT, NOT_FOUND, UNIQUE_MATCH,
    )

    repo = ProductCatalogRepository()
    source = sqlite3.connect(f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True)
    try:
        names = [
            r[0] for r in source.execute(
                "SELECT DISTINCT product_name FROM inquiries"
                " WHERE product_name IS NOT NULL AND product_name<>'' LIMIT 400"
            )
        ]
    finally:
        source.close()
    seen = set()
    for name in names:
        match = repo.match(product_name=name)
        seen.add(match.status)
        assert match.status in {EXACT, UNIQUE_MATCH, AMBIGUOUS, NOT_FOUND}
        if match.status in {EXACT, UNIQUE_MATCH}:
            assert match.record is not None and match.model_key
        else:
            assert match.record is None and match.model_key is None
    assert {EXACT, NOT_FOUND} <= seen


# ===========================================================================
# 3. Learning -- 검색은 코드, 관련성 판단은 GPT ②
# ===========================================================================

def _learning_rows(product_names, limit=60):
    source = sqlite3.connect(f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in product_names)
        return [
            dict(r) for r in source.execute(
                f"SELECT id, product_name, question_original_masked q,"
                f" final_answer a FROM learning_examples WHERE active=1"
                f" AND product_name IN ({marks}) LIMIT {int(limit)}",
                tuple(product_names),
            )
        ]
    finally:
        source.close()


# 실제 corpus 에서 확인한 서로 다른 의미 유형. 표현은 원문과 겹치지 않는다.
LEARNING_QUERIES = [
    ("폐가전수거", "쓰던 티비 가져가 주시나요?"),
    ("폐가전수거", "예전 텔레비전 처리도 같이 되나요?"),
    ("설치주체", "제가 직접 조립해야 하나요?"),
    ("설치주체", "누가 와서 달아주시는 건가요?"),
    ("자가설치", "다른 거치대를 따로 사면 달아주시나요?"),
    ("A/S", "고장나면 어디로 연락하나요?"),
    ("A/S", "받침대 분리는 어디에 물어보면 되나요?"),
    ("벽걸이", "벽에 붙여서 쓸 수 있나요?"),
    ("벽걸이", "쓰던 거치대 나사 간격이 맞을까요?"),
    ("구성품", "소리는 따로 스피커 없어도 나오나요?"),
    ("구성품", "조작기는 같이 들어있나요?"),
    ("연결/단자", "안테나 선 꽂으면 방송 나오나요?"),
    ("연결/단자", "노트북이랑 선으로 이어서 쓸 수 있어요?"),
    ("OTT", "넷플릭스 같은 건 바로 되나요?"),
    ("OTT", "티빙 시청 가능한가요?"),
    ("이벤트/혜택", "온누리 받으려면 서류를 어떻게 내나요?"),
    ("이벤트/혜택", "거래 내역서는 어디서 발급받나요?"),
    ("정품/보증", "구매하면 예전 제품 무료로 치워주시나요?"),
    ("배송절차", "연식이 다르면 받는 날짜가 달라지나요?"),
    ("호환성", "지금 쓰는 벽 거치대에 새 티비를 달 수 있을까요?"),
    ("제조/연식", "언제 만들어진 물건인지 알 수 있나요?"),
    ("스마트기능", "셋톱 없이 보는 것과 뭐가 다른가요?"),
]


def test_the_learning_query_set_covers_many_distinct_meanings():
    """검증 세트가 한 주제에 몰려 있지 않다는 것 자체를 고정한다."""

    categories = {c for c, _ in LEARNING_QUERIES}
    assert len(categories) >= 8, categories
    assert len(LEARNING_QUERIES) >= 20


@pytest.fixture(scope="module")
def learning_search(tmp_path_factory):
    database = Database(tmp_path_factory.mktemp("gen-learning") / "learning.db")
    database.initialize()
    copied = _copy_learning(
        database, (IDENTIFIED_PRODUCT, UNIDENTIFIED_PRODUCT), limit=200
    )
    assert copied, "운영 DB 에서 Learning 을 하나도 복사하지 못했다"
    service = SimilarAnswerService(LearningRepository(database))
    pool = service.repository.candidates(store_code=STORE, limit=2000)
    diagnostics = service.repository.candidate_diagnostics(store_code=STORE)
    return SimpleNamespace(
        service=service, pool=pool, diagnostics=diagnostics, copied=copied,
    )


@pytest.mark.parametrize(
    ("category", "query"), LEARNING_QUERIES,
    ids=[f"{i}-{c}" for i, (c, _) in enumerate(LEARNING_QUERIES)],
)
def test_no_candidate_is_removed_for_a_soft_semantic_reason(
    learning_search, category, query,
):
    """검색 단계에서 의미 불일치를 이유로 후보를 지우지 않는다.

    이것이 이번 architecture 의 핵심 계약이다. 제거는 identity/validity 처럼
    기계적으로 확정 가능한 사유로만 일어나야 하고, "관련 없어 보인다" 는 판단은
    GPT ② 의 몫이다. topic·attribute·similarity 임계값은 순위에만 쓰인다.
    """

    soft = {
        "TOPIC_MISMATCH", "TOPIC_PARTIAL_COVERAGE", "SEMANTIC_GOAL_MISMATCH",
        "BELOW_SIMILARITY_THRESHOLD", "CONTEXT_POLICY_REJECTED",
    }
    learning_search.service.search(
        query, store_code=STORE, product_name=IDENTIFIED_PRODUCT,
        candidate_pool=learning_search.pool,
        candidate_diagnostics=learning_search.diagnostics,
        limit=3, hard_conflicts_only=True,
    )
    counts = (learning_search.service.last_trace or {}).get("rejection_counts") or {}
    offending = {k: v for k, v in counts.items() if k in soft and v}
    assert not offending, (query, offending)


@pytest.mark.parametrize(
    ("category", "query"), LEARNING_QUERIES[:12],
    ids=[f"{i}-{c}" for i, (c, _) in enumerate(LEARNING_QUERIES[:12])],
)
def test_retrieval_returns_candidates_for_paraphrased_questions(
    learning_search, category, query,
):
    """표현이 원문과 달라도 검색은 후보를 돌려준다(빈손이 아니다)."""

    results = learning_search.service.search(
        query, store_code=STORE, product_name=IDENTIFIED_PRODUCT,
        candidate_pool=learning_search.pool,
        candidate_diagnostics=learning_search.diagnostics,
        limit=3, hard_conflicts_only=True,
    )
    assert results, f"{query!r} 에 대해 후보가 하나도 없다"
    for item in results:
        assert item.get("final_answer")
        # GPT ② 가 재사용 가능성을 판단할 수 있도록 출처가 붙어 있어야 한다.
        assert "relevance" in item and "answer_support" in item


def assert_cross_model_learning_is_labelled(prompt_input, current_product):
    """다른 모델 Learning 은 삭제 대신 출처가 표시된 채 전달된다 (P0-2).

    이전 계약은 "사양 질문에서는 다른 모델의 Learning 이 GPT ② 에 도달하지
    않는다" 였다. 그 계약을 지키려면 CODE 가 의미 판단으로 후보를 지워야 했고,
    서버 실문의 688218182 / 688218219 에서 질문에 정확히 답하는 Learning
    (LID 117 "설치비는 청구되지 않습니다", LID 72 "리모컨이 포함되어 있습니다")
    까지 같은 규칙으로 사라졌다. GPT ② 에는 해피콜·온누리상품권만 남았다.

    현재 계약: 후보는 전달하되 어느 상품에서 왔는지 표시하고, 적용 여부는
    GPT ② 가 판단한다. 다른 모델의 사양이 근거 없이 단정되는 것은
    evidence_origin 라벨, 프롬프트 지시, validator 의 ungrounded-claim 검사가
    막는다 -- 후보의 부재가 아니라.
    """

    for item in (prompt_input.get("similar_approved_answers") or []):
        origin = item.get("evidence_origin") or {}
        assert origin, ("Learning 후보에 출처 라벨이 없다", item)
        source = str(item.get("source_product_name") or "")
        if source == current_product:
            continue
        assert origin.get("identity") != "SAME_PRODUCT", (
            "다른 모델 Learning 이 현재 상품 자료로 표시됐다", source,
        )
        if origin.get("knowledge") == "PRODUCT_SPECIFIC":
            assert origin.get("note"), (
                "다른 모델의 사양 Learning 에 자동 적용 금지 안내가 없다", source,
            )


def test_a_cross_model_learning_is_labelled_for_a_specification_question(
    tmp_path, monkeypatch,
):
    """사양 질문에서 다른 모델의 Learning 은 출처가 표시되어 전달된다."""

    run = _run(
        tmp_path, monkeypatch, name="crossmodel",
        question="이 제품 해상도가 어떻게 되나요?",
        product_name=UNIDENTIFIED_PRODUCT,
        atoms=[_spec_atom("이 제품 해상도가 어떻게 되나요?")],
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT, UNIDENTIFIED_PRODUCT),
    )
    assert run.prompt is not None, run.error
    assert_cross_model_learning_is_labelled(
        run.prompt["input"], UNIDENTIFIED_PRODUCT,
    )


# ===========================================================================
# 4. Template / Historical / Negative
# ===========================================================================

def test_template_candidates_are_offered_and_chosen_by_gpt2(
    tmp_path, monkeypatch,
):
    """Template 은 후보로 제공되고 선택은 GPT ② 가 한다."""

    seen = {}

    def picker(context):
        candidates = context.get("template_candidates") or []
        seen["count"] = len(candidates)
        if not candidates:
            return {}
        chosen = candidates[0]
        return {
            "answer": chosen["answer"],
            "used_template_ids": [chosen["template_id"]],
            "ignored_evidence": [
                {"kind": "TEMPLATE", "id": c["template_id"], "reason": "미선택"}
                for c in candidates[1:]
            ],
            "can_auto_post": True,
        }

    run = _run(
        tmp_path, monkeypatch, name="template",
        question="설치는 누가 해주시나요?",
        product_name=UNIDENTIFIED_PRODUCT,
        atoms=[{
            "text": "설치는 누가 해주시나요?", "action": "INSTALLATION_METHOD",
            "requested_information": "설치 주체", "requested_attribute": "ACTOR",
        }],
        stub=_EvidenceReadingStub(picker),
    )
    assert run.error is None, run.error
    assert seen["count"] >= 2, "서로 다른 Template 후보가 여럿 제공되어야 한다"
    draft = (run.outcome.result.metadata.get("hybrid") or {}).get("draft") or {}
    assert draft["used_template_ids"], "GPT ② 가 Template 을 고르지 못했다"
    assert draft["ignored_evidence"], "고르지 않은 후보도 보고되어야 한다"


def test_historical_candidates_survive_concept_mismatch(tmp_path, monkeypatch):
    """Historical 도 concept 불일치로 GPT ② 전에 삭제되지 않는다."""

    from datetime import UTC, datetime

    from services.historical_case_service import HistoricalCaseService

    database = Database(tmp_path / "historical.db")
    database.initialize()
    service = HistoricalCaseService(database)
    case = service.prepare_case({
        "store_code": STORE, "source_type": "PRODUCT_INQUIRY",
        "external_inquiry_id": "gen-hist", "title": "상품 문의",
        "content": "설치는 기사님이 오시나요?",
        "product_name": UNIDENTIFIED_PRODUCT,
        "seller_answer": "해당 상품은 삼성 기사님이 방문하여 설치하는 상품입니다.",
        "answered": True, "source_created_at": datetime.now(UTC).isoformat(),
    }, source_reference="GEN:hist")
    row, _ = service.repository.upsert(case)

    # 개념이 겹치지 않는 질문으로도 후보 자체는 살아 있어야 한다.
    detailed = service.search_detailed(
        "온누리 상품권은 어떻게 신청하나요?", store_code=STORE,
        product_name=UNIDENTIFIED_PRODUCT, limit=3, hard_conflicts_only=True,
    )
    rejects = detailed["rejection_counts"]
    assert not rejects.get("LOW_RELEVANCE"), (
        "concept 불일치가 GPT ② 전에 후보를 지웠다", rejects
    )
    assert int(row["id"]) > 0

    # legacy 경로(hard_conflicts_only=False)는 예전대로 제거한다.
    legacy = service.search_detailed(
        "온누리 상품권은 어떻게 신청하나요?", store_code=STORE,
        product_name=UNIDENTIFIED_PRODUCT, limit=3, hard_conflicts_only=False,
    )
    assert legacy["selected_count"] <= detailed["selected_count"]


def test_negative_corrections_reach_gpt2_as_constraints(tmp_path, monkeypatch):
    """Negative 는 후보가 아니라 제약으로 전달되고, 적용 여부는 GPT ② 가 본다."""

    run = _run(
        tmp_path, monkeypatch, name="negative",
        question="폐가전 수거도 해주시나요?",
        product_name=UNIDENTIFIED_PRODUCT,
        atoms=[{
            "text": "폐가전 수거도 해주시나요?", "action": "COLLECTION",
            "requested_information": "폐가전 수거 가능 여부",
            "requested_attribute": "EXISTENCE_OR_CAPABILITY",
        }],
        stub=_EvidenceReadingStub(),
        learning_products=(UNIDENTIFIED_PRODUCT,),
    )
    assert run.prompt is not None, run.error
    policy = run.prompt["learning_usage_policy"]
    # Negative 가 없으면 지시도 붙지 않는다(빈 규칙을 만들지 않는다).
    corrections = run.prompt["input"].get("negative_corrections")
    if corrections:
        assert "negative_correction_instructions" in run.prompt
        rules = run.prompt["negative_correction_instructions"]
        assert any("claim" in r or "취소" in r for r in rules)
    assert policy["retrieval_candidates_are_not_approved_evidence"] is True


# ===========================================================================
# 5. 복합문의 -- source 조합별 독립성
# ===========================================================================

COMPOUND_CASES = [
    ("product+learning", [
        _spec_atom("이 제품 해상도가 어떻게 되나요?"),
        {"text": "폐가전 수거되나요?", "action": "COLLECTION",
         "requested_information": "폐가전 수거", "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
    ]),
    ("product+template", [
        _spec_atom("화면이 몇 인치인가요?"),
        {"text": "설치는 누가 하나요?", "action": "INSTALLATION_METHOD",
         "requested_information": "설치 주체", "requested_attribute": "ACTOR"},
    ]),
    ("learning+template", [
        {"text": "설치는 누가 하나요?", "action": "INSTALLATION_METHOD",
         "requested_information": "설치 주체", "requested_attribute": "ACTOR"},
        {"text": "A/S는 어디서 받나요?", "action": "REPAIR",
         "requested_information": "A/S 접수처", "requested_attribute": "METHOD_OR_PROCEDURE"},
    ]),
    ("product+learning+template", [
        _spec_atom("주사율은 얼마인가요?"),
        {"text": "설치는 누가 하나요?", "action": "INSTALLATION_METHOD",
         "requested_information": "설치 주체", "requested_attribute": "ACTOR"},
        {"text": "폐가전 수거되나요?", "action": "COLLECTION",
         "requested_information": "폐가전 수거", "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
    ]),
    ("learning+historical", [
        {"text": "셋톱박스가 필요한가요?", "action": "PRODUCT_CONCEPT",
         "requested_information": "셋톱박스 필요 여부", "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
        {"text": "가정에서 써도 되나요?", "action": "OTHER",
         "requested_information": "가정용 사용 가능", "requested_attribute": "PERMISSION_OR_OPTION"},
    ]),
    ("product+negative", [
        _spec_atom("어느 회사 제품인가요?"),
        {"text": "폐가전 수거 신청은 어디에 하나요?", "action": "COLLECTION",
         "requested_information": "폐가전 수거 신청 방법",
         "requested_attribute": "METHOD_OR_PROCEDURE"},
    ]),
]


@pytest.mark.parametrize(("name", "atoms"), COMPOUND_CASES, ids=[c[0] for c in COMPOUND_CASES])
def test_each_atom_keeps_its_own_evidence_in_a_compound_inquiry(
    tmp_path, monkeypatch, name, atoms,
):
    """한 atom 의 source 때문에 다른 atom 의 evidence 가 사라지지 않는다."""

    question = " ".join(a["text"] for a in atoms)
    run = _run(
        tmp_path, monkeypatch, name=f"cmp-{name}", question=question,
        product_name=IDENTIFIED_PRODUCT, atoms=atoms,
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT, UNIDENTIFIED_PRODUCT),
    )
    assert run.prompt is not None, run.error
    evidence = run.prompt["input"]["subquestion_evidence"]
    assert len(evidence) == len(atoms), evidence

    # 사양 atom 이 있으면 검증 카탈로그가 함께 있어야 한다.
    if any(a["action"] == "PRODUCT_SPEC" for a in atoms):
        assert run.prompt["input"].get("product_catalog"), (
            "사양 atom 이 있는데 카탈로그가 없다", name
        )
    # 설치 atom 이 있으면 Template 후보가 함께 있어야 한다.
    if any(a["action"] == "INSTALLATION_METHOD" for a in atoms):
        assert run.prompt["input"].get("template_candidates"), (
            "설치 atom 이 있는데 Template 후보가 없다", name
        )
    # atom 마다 자기 근거를 갖고, 다른 atom 의 근거를 빌려 쓰지 않는다.
    for item in evidence:
        assert item["subquestion"] in {a["text"] for a in atoms}


# ===========================================================================
# 6. No-Evidence / Wrong-Evidence control
# ===========================================================================

NO_EVIDENCE_QUESTIONS = [
    "이 제품 소비전력이 정확히 몇 와트인가요?",
    "이 모델 패널 제조사가 어디인가요?",
    "구매하면 언제까지 무상 부품 교체가 되나요?",
    "이 제품 색온도 캘리브레이션 수치가 어떻게 되나요?",
    "설치 기사님 성함을 미리 알 수 있나요?",
]


@pytest.mark.parametrize("question", NO_EVIDENCE_QUESTIONS)
def test_without_evidence_nothing_is_invented(tmp_path, monkeypatch, question):
    """근거가 없으면 GPT ② 가 unresolved 로 남기고 자동등록되지 않는다."""

    def picker(context):
        return {
            "answer": "확인 후 안내드리겠습니다.",
            "unresolved": [question],
            "can_auto_post": False,
        }

    run = _run(
        tmp_path, monkeypatch, name=f"noev-{abs(hash(question)) % 10000}",
        question=question, product_name=IDENTIFIED_PRODUCT,
        atoms=[_spec_atom(question)], stub=_EvidenceReadingStub(picker),
    )
    assert run.error is None, run.error
    result = run.outcome.result
    draft = (result.metadata.get("hybrid") or {}).get("draft") or {}
    assert draft["unresolved"] == [question]
    assert draft["can_auto_post"] is False
    assert result.needs_review is True
    trace = result.metadata["pipeline_trace"]
    assert trace["answer"]["unresolved"] == 1


WRONG_EVIDENCE_CASES = [
    ("다른 모델 사양", "이 제품 해상도가 어떻게 되나요?", "PRODUCT_SPEC"),
    ("다른 사이즈 사양", "화면이 몇 인치인가요?", "PRODUCT_SPEC"),
    ("다른 모델 주사율", "주사율은 얼마인가요?", "PRODUCT_SPEC"),
    ("다른 모델 무게", "무겁나요?", "PRODUCT_SPEC"),
    ("다른 모델 브라켓", "벽에 붙여서 쓸 수 있나요?", "PRODUCT_SPEC"),
]


@pytest.mark.parametrize(
    ("label", "question", "action"), WRONG_EVIDENCE_CASES,
    ids=[c[0] for c in WRONG_EVIDENCE_CASES],
)
def test_wrong_model_evidence_is_labelled_for_a_specification_question(
    tmp_path, monkeypatch, label, question, action,
):
    """비슷하지만 다른 모델의 근거는 출처가 표시된 채 전달된다 (P0-2).

    identity 비교 자체는 그대로다 -- model/size/category 를 비교하고 MISMATCH
    로 판정한다. 바뀐 것은 그 판정의 결과가 '삭제'에서 '표시 + 감점'이 된
    것뿐이다. 적용 여부는 GPT ② 가 판단한다.

    Product Fact 계약은 변하지 않았다: 식별되지 않은 상품에는 여전히 검증된
    카탈로그 사양이 붙지 않는다. 아래 마지막 단언이 그것을 지킨다.
    """

    run = _run(
        tmp_path, monkeypatch, name=f"wrong-{abs(hash(question)) % 10000}",
        question=question, product_name=UNIDENTIFIED_PRODUCT,
        atoms=[_spec_atom(question)], stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT, UNIDENTIFIED_PRODUCT),
    )
    assert run.prompt is not None, run.error
    assert_cross_model_learning_is_labelled(
        run.prompt["input"], UNIDENTIFIED_PRODUCT,
    )
    # 카탈로그 쪽은 그대로: 식별되지 않았으면 검증 사양이 없다.
    assert run.prompt["input"].get("product_catalog") is None


# ===========================================================================
# 7. Prompt 구성과 예산
# ===========================================================================

def test_the_prompt_separates_every_evidence_kind(tmp_path, monkeypatch):
    """GPT ② 가 근거의 종류를 구분할 수 있어야 한다."""

    atoms = [
        _spec_atom("이 제품 해상도가 어떻게 되나요?"),
        {"text": "설치는 누가 하나요?", "action": "INSTALLATION_METHOD",
         "requested_information": "설치 주체", "requested_attribute": "ACTOR"},
    ]
    run = _run(
        tmp_path, monkeypatch, name="promptshape",
        question=" ".join(a["text"] for a in atoms),
        product_name=IDENTIFIED_PRODUCT, atoms=atoms,
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT,),
    )
    assert run.prompt is not None, run.error
    payload, inp = run.prompt, run.prompt["input"]
    assert payload["customer_inquiry"]
    assert payload["allowed_facts"].get("product.name")
    assert payload["product_information_tiers"]
    assert payload["evidence_judgement_rules"]
    assert inp["product_identity"]["status"]
    assert inp.get("product_catalog")
    assert inp.get("template_candidates")
    assert inp.get("subquestion_evidence")
    for item in (inp.get("similar_approved_answers") or []):
        for field in ("learning_example_id", "relevance", "answer_support",
                      "source_question", "source_product_name", "authority"):
            assert field in item, (field, sorted(item))


def test_relevant_evidence_is_never_lost_to_prompt_trimming(
    tmp_path, monkeypatch,
):
    """예산 때문에 근거가 잘려나가지 않는다(여유가 실제로 있는지 측정)."""

    from services.learning_context_service import DRAFT_PROMPT_BUDGET_CHARS

    atoms = [
        _spec_atom("이 제품 해상도가 어떻게 되나요?"),
        {"text": "설치는 누가 하나요?", "action": "INSTALLATION_METHOD",
         "requested_information": "설치 주체", "requested_attribute": "ACTOR"},
        {"text": "폐가전 수거되나요?", "action": "COLLECTION",
         "requested_information": "폐가전 수거", "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
    ]
    run = _run(
        tmp_path, monkeypatch, name="budget",
        question=" ".join(a["text"] for a in atoms),
        product_name=IDENTIFIED_PRODUCT, atoms=atoms,
        stub=_EvidenceReadingStub(),
        learning_products=(IDENTIFIED_PRODUCT, UNIDENTIFIED_PRODUCT),
    )
    assert run.prompt is not None, run.error
    assert len(run.raw_prompt) < DRAFT_PROMPT_BUDGET_CHARS
    hybrid = (run.outcome.result.metadata.get("hybrid") or {})
    budget = (hybrid.get("provider_telemetry") or {}).get("prompt_budget") or {}
    if budget:
        assert not budget.get("dropped"), budget
        assert budget.get("within_budget") is not False


def test_the_source_data_is_never_written(tmp_path, monkeypatch):
    """이 파일의 어떤 테스트도 운영 DB/카탈로그를 수정하지 않는다."""

    before = (_sha256(SOURCE_DB), _sha256(CATALOG))
    _run(
        tmp_path, monkeypatch, name="readonly",
        question="이 제품 해상도가 어떻게 되나요?",
        product_name=IDENTIFIED_PRODUCT,
        atoms=[_spec_atom("이 제품 해상도가 어떻게 되나요?")],
        stub=_EvidenceReadingStub(), learning_products=(IDENTIFIED_PRODUCT,),
    )
    assert (_sha256(SOURCE_DB), _sha256(CATALOG)) == before
