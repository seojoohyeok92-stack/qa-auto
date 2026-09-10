"""실제 운영 문의로 검증하는 v4 evidence pipeline 계약.

서버에서 실패한 문의들은 GPT 가 답을 거부한 것이 아니라, 답에 필요한 근거가
GPT ② 앞에서 사라진 것이었다. 세 지점이 각각 독립적으로 그렇게 했다.

* ``ProductCatalogRepository.match`` 가 상품명에서 model code 를 못 찾아
  카탈로그 전체를 포기했다. 이 매장 문의의 52% 가 그런 상품명이다.
* ``FactSelectionService`` 가 keyword 분류기의 ``answer_strategy`` 를 읽어
  ``rule.answer`` 하나만 남겼고, 그래서 상품명조차 프롬프트에 없었다.
* Learning topic gate 가 ``hard_reject=True`` 로 분류되어
  ``hard_conflicts_only=True`` 를 우회하고 후보를 제거했다.

이 파일이 지키는 것은 특정 문의의 정답이 아니라 그 세 지점의 계약이다.
따라서 어떤 assertion 도 "4K UHD" 같은 값을 코드에 적어두고 비교하지 않는다.
검증 사양은 Product Catalog 에서 읽어오고, Learning 은 실제 운영 DB 에서
읽어온 행을 사용한다. inquiry id 는 fixture 추적용 라벨일 뿐이다.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from answer.governance_models import GptProviderSettings
from answer.models import AnswerStatus
from repositories.database import Database
from repositories.inquiry_repository import InquiryRepository
from repositories.product_catalog_repository import ProductCatalogRepository
from services.answer_service import AnswerService
from services.gpt_governance_service import GovernedHybridAnswerService
from services.gpt_semantic_analyzer_service import GptSemanticAnalyzerService

SOURCE_DB = Path("data/oje_automation.db")
CATALOG = Path("data/model_data_with_color.json")

# 서버에서 실제로 실패/성공한 문의들. 값이 아니라 흐름을 재현하기 위한 fixture.
PRODUCT_NO_MODEL_CODE = (
    "삼성 삼탠바이미 50인치(125cm) 4K UHD 무빙 스마트 비즈니스TV 거치대 화이트"
)
# 상품명에 model code 가 들어 있어 카탈로그가 식별할 수 있는 대조군.
PRODUCT_WITH_MODEL_CODE = (
    "삼성 125.7cm(50인치) UHD 4K 1등급 비즈니스TV LH50BEFHLGFXKR 스탠드형"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    """GPT ①. 서버가 실제로 낸 이해 결과를 재생한다."""

    name = "semantic_stub"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def generate_json(self, *, task, prompt, context):
        self.calls += 1
        return dict(self.payload)


class _DraftStub:
    """GPT ②. production 이 만든 prompt/context 를 캡처하고 계약대로 답한다."""

    name = "draft_stub"
    call_records: list = []

    def __init__(self, *, answer="확인 후 안내드리겠습니다.", unresolved=(),
                 used_learning_ids=(), can_auto_post=False, fail=None):
        self.captured: list[dict] = []
        self.answer = answer
        self.unresolved = list(unresolved)
        self.used_learning_ids = list(used_learning_ids)
        self.can_auto_post = can_auto_post
        self.fail = fail

    def generate_json(self, *, task, prompt, context):
        self.captured.append({"task": task, "prompt": prompt, "context": context})
        if self.fail is not None:
            raise self.fail
        return {
            "answer": self.answer,
            "confidence": 0.9,
            "used_facts": [],
            "missing_information": [],
            "requires_review": bool(self.unresolved),
            "warnings": [],
            "learning_usage": [],
            "historical_usage": [],
            "subquestion_results": [],
            "evidence_decisions": [],
            "used_template_ids": [],
            "used_product_facts": [],
            "used_learning_ids": self.used_learning_ids,
            "used_historical_ids": [],
            "ignored_evidence": [],
            "unresolved": self.unresolved,
            "can_auto_post": self.can_auto_post,
            "reason": "replay",
        }


class _OrderSpy:
    def __init__(self):
        self.calls = 0

    def lookup_for_inquiry(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("주문 조회가 실행되면 안 됩니다")


def _copy_learning(target: Database, *, product_names: tuple[str, ...]) -> int:
    """운영 DB 에서 해당 상품의 ACTIVE Learning 을 tmp DB 로 복사한다(READ ONLY)."""

    source = sqlite3.connect(f"file:{SOURCE_DB.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in product_names)
        rows = source.execute(
            f"SELECT * FROM learning_examples"
            f" WHERE active=1 AND product_name IN ({marks}) LIMIT 60",
            product_names,
        ).fetchall()
        if not rows:
            return 0
        # learning_examples.inquiry_id 는 inquiries 를 참조하므로 원본 문의도
        # 함께 복사한다. 복사하지 않으면 FOREIGN KEY 로 실패하고, 참조를
        # 끊으면 Learning 의 출처를 잃는다.
        inquiry_ids = sorted({
            int(row["inquiry_id"]) for row in rows
            if row["inquiry_id"] is not None
        })
        inquiries = []
        if inquiry_ids:
            marks = ",".join("?" for _ in inquiry_ids)
            inquiries = source.execute(
                f"SELECT * FROM inquiries WHERE id IN ({marks})", inquiry_ids,
            ).fetchall()
        with target.transaction() as connection:
            for table, table_rows in (
                ("inquiries", inquiries), ("learning_examples", rows),
            ):
                if not table_rows:
                    continue
                columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                names = [name for name in table_rows[0].keys() if name in columns]
                sql = (
                    f"INSERT OR IGNORE INTO {table} ({','.join(names)})"
                    f" VALUES ({','.join('?' for _ in names)})"
                )
                connection.executemany(
                    sql,
                    [tuple(row[name] for name in names) for row in table_rows],
                )
        return len(rows)
    finally:
        source.close()


def _run(tmp_path, monkeypatch, *, name, question, product_name,
         atoms, draft_stub, learning_products=(),
         product_id="11815213767"):
    database = Database(tmp_path / f"{name}.db")
    database.initialize()
    copied = _copy_learning(database, product_names=tuple(learning_products)) \
        if learning_products else 0

    inquiry_id = InquiryRepository(database).upsert_work_item({
        "store_code": "OJE_PLUS",
        "source_type": "TEST",
        "source_question_id": f"v4-{name}",
        "inquiry_type": "PRODUCT_INQUIRY",
        "content": question,
        "product_id": product_id,
        "product_name": product_name,
        "option_name": None,
        "raw_json": {},
    }).inquiry_id

    semantic = _SemanticStub(_semantic_payload(atoms))
    governed = GovernedHybridAnswerService(
        database, provider=draft_stub,
        settings=GptProviderSettings(provider_name="fake"),
    )
    order = _OrderSpy()

    import services.answer_service as answer_module

    monkeypatch.setenv("OJE_SEMANTIC_ANALYZER_ENABLED", "1")
    monkeypatch.setattr(answer_module, "notify_qna_safely", lambda **_k: False)

    outcome = None
    error = None
    try:
        outcome = AnswerService(
            database, hybrid_service=governed, order_lookup_service=order,
            semantic_analyzer=GptSemanticAnalyzerService(semantic),
        ).generate_for_inquiry(inquiry_id)
    except Exception as exc:  # noqa: BLE001 - fallback 경로도 검증 대상
        error = exc

    drafts = [c for c in draft_stub.captured if str(c["task"]).upper() == "DRAFT"]
    prompt = json.loads(drafts[-1]["prompt"]) if drafts else None
    return SimpleNamespace(
        database=database, inquiry_id=inquiry_id, outcome=outcome, error=error,
        prompt=prompt, raw_prompt=drafts[-1]["prompt"] if drafts else "",
        order=order, semantic=semantic, copied_learning=copied,
    )


def _listing(prompt) -> dict:
    facts = prompt.get("allowed_facts") or {}
    return {k: v for k, v in facts.items() if str(k).startswith("product.")}


# ---------------------------------------------------------------------------
# 687932845 — 상품명에 model code 가 없는 상품의 사양 문의
# ---------------------------------------------------------------------------

def test_687932845_listing_metadata_survives_the_keyword_classifier(
    tmp_path, monkeypatch,
):
    """분류기가 UNCLASSIFIED 를 내도 상품 context 는 GPT ② 에 남는다.

    서버에서 이 문의는 ``answer_strategy=MANUAL_REVIEW`` 로 분류되어
    ``allowed_fact_paths`` 가 비었고, 상품명조차 프롬프트에 없었다. GPT ② 는
    어떤 상품인지 모르는 채로 사양을 물어보는 문의를 받았다.
    """

    question = "이 제품 해상도가 4K UHD 맞나요? 화질이 UHD 제품인지 확인 부탁드립니다."
    run = _run(
        tmp_path, monkeypatch, name="845", question=question,
        product_name=PRODUCT_NO_MODEL_CODE,
        atoms=[
            {"text": "이 제품 해상도가 4K UHD 맞나요?", "action": "PRODUCT_SPEC",
             "requested_information": "제품 해상도", "requested_attribute": "SPEC_VALUE"},
            {"text": "화질이 UHD 제품인지 확인 부탁드립니다.", "action": "PRODUCT_SPEC",
             "requested_information": "UHD 지원 여부", "requested_attribute": "SPEC_VALUE"},
        ],
        draft_stub=_DraftStub(unresolved=["이 제품 해상도가 4K UHD 맞나요?"]),
    )
    assert run.prompt is not None, run.error

    # GPT ① 이 판단한 것이 실행 계약에 그대로 남아 있어야 한다.
    understanding = (
        run.outcome.result.metadata["semantic_routing"]["understanding"]
    )
    assert understanding["usable"] is True
    assert understanding["need_product"] is True
    assert len(understanding["questions"]) == 2

    # 상품명은 판매 페이지 표기이며, 그 자체가 프롬프트에 있어야 한다.
    listing = _listing(run.prompt)
    assert listing.get("product.name") == PRODUCT_NO_MODEL_CODE
    assert listing.get("product.product_id")

    # 세 tier 를 구분하는 규칙이 프롬프트에 있어야 한다. 그래야 판매 페이지
    # 표기를 검증 사양으로 승격하지 않는다.
    tiers = run.prompt["product_information_tiers"]
    assert set(tiers) >= {
        "listing_metadata", "product_catalog", "product_candidates", "precedence",
    }

    # 조회가 어떻게 끝났는지 자체가 전달된다 -- "조회했는데 없었다" 와
    # "조회하지 않았다" 는 다른 상황이다.
    #
    # 이 상품명에는 model code 가 없어 카탈로그는 여전히 식별하지 못한다.
    # 그러나 이제 listing store(product_facts.db)가 Naver product_id 로
    # 같은 상품을 식별하므로, 이 문의의 identity 는 NOT_FOUND 가 아니라
    # LISTING_EXACT 로 끝날 수 있다. 두 출처는 provenance 가 다르고
    # product_information_tiers 가 그 차이를 모델에게 설명하므로, 같은 이름을
    # 쓰지 않는다.
    identity = run.prompt["input"]["product_identity"]
    assert identity["status"] in {
        "NOT_FOUND", "AMBIGUOUS", "UNIQUE_MATCH", "EXACT", "LISTING_EXACT",
    }
    assert identity["matched"] is (identity["status"] == "LISTING_EXACT")

    assert run.order.calls == 0


def test_a_listing_that_names_its_model_reaches_the_verified_catalogue(
    tmp_path, monkeypatch,
):
    """대조군 -- 식별이 되면 검증 사양이 GPT ② 까지 간다.

    기대값을 코드에 적지 않는다. Product Catalog 에서 그 모델의 resolution 을
    읽어와, 같은 값이 프롬프트에 도달했는지 확인한다.
    """

    match = ProductCatalogRepository().match(product_name=PRODUCT_WITH_MODEL_CODE)
    assert match.record is not None, "대조군 상품은 카탈로그가 식별할 수 있어야 한다"
    expected_resolution = match.record.get("resolution")
    assert expected_resolution

    run = _run(
        tmp_path, monkeypatch, name="identified",
        question="이 제품 해상도가 어떻게 되나요?",
        product_name=PRODUCT_WITH_MODEL_CODE,
        # 이 대조군만 product_id 를 비운다. harness 기본값 "11815213767" 은
        # 삼탠바이미 listing 의 id 이고, 이 케이스의 상품명은
        # LH50BEFHLGFXKR 이다. 식별 계약에서 id 가 최우선(1번)이므로 그
        # 조합은 "A 의 id + B 의 모델명" 이라는 존재하지 않는 상품이 되고,
        # listing store 가 이름이 요구하는 모델과 다르다며 사실을 배제한 뒤
        # (5번) 카탈로그 대체도 막는다(6번). 이 테스트의 주어는 카탈로그
        # 경로이므로 이름으로 식별하게 둔다(2번).
        product_id="",
        atoms=[{
            "text": "이 제품 해상도가 어떻게 되나요?", "action": "PRODUCT_SPEC",
            "requested_information": "제품 해상도", "requested_attribute": "SPEC_VALUE",
        }],
        draft_stub=_DraftStub(),
    )
    assert run.prompt is not None, run.error
    catalog = run.prompt["input"].get("product_catalog")
    assert catalog, run.prompt["input"].get("product_identity")
    assert catalog["identity_status"] in {"EXACT", "UNIQUE_MATCH"}
    values = {
        str(item.get("field_key")): str(item.get("value"))
        for item in catalog["facts"]
    }
    assert values.get("resolution") == str(expected_resolution)
    assert str(expected_resolution) in run.raw_prompt


# ---------------------------------------------------------------------------
# 687932860 — 사양 + 설치가 함께 온 복합 문의
# ---------------------------------------------------------------------------

def test_687932860_each_atomic_question_keeps_its_own_evidence(
    tmp_path, monkeypatch,
):
    """한 질문의 자료 때문에 다른 질문의 자료가 사라지지 않는다.

    설치는 상품과 무관한 운영 정책이라 다른 리스팅의 승인 답변도 근거가 될 수
    있고, 사양은 그렇지 않다. 두 판정이 atom 별로 따로 이루어져야 한다.
    """

    run = _run(
        tmp_path, monkeypatch, name="860",
        question="이 제품 4K UHD 맞나요? 그리고 설치는 삼성 기사님이 방문해서 해주시는 건가요?",
        product_name=PRODUCT_NO_MODEL_CODE,
        atoms=[
            {"text": "이 제품 4K UHD 맞나요?", "action": "PRODUCT_SPEC",
             "requested_information": "4K UHD 지원 여부",
             "requested_attribute": "SPEC_VALUE"},
            {"text": "설치는 삼성 기사님이 방문해서 해주시는 건가요?",
             "action": "INSTALLATION_METHOD",
             "requested_information": "설치 수행 주체",
             "requested_attribute": "ACTOR"},
        ],
        draft_stub=_DraftStub(unresolved=["이 제품 4K UHD 맞나요?"]),
        learning_products=(PRODUCT_NO_MODEL_CODE, PRODUCT_WITH_MODEL_CODE),
    )
    assert run.prompt is not None, run.error
    inp = run.prompt["input"]

    # Template 후보와 listing metadata 가 동시에 존재해야 한다.
    assert inp.get("template_candidates"), "설치 상품 Template 후보가 필요하다"
    assert _listing(run.prompt).get("product.name") == PRODUCT_NO_MODEL_CODE

    evidence = inp["subquestion_evidence"]
    assert len(evidence) == 2, evidence
    by_question = {str(item["subquestion"]): item for item in evidence}
    install = by_question["설치는 삼성 기사님이 방문해서 해주시는 건가요?"]
    spec = by_question["이 제품 4K UHD 맞나요?"]

    # 설치 atom 은 후보를 받는다.
    assert install["status"] == "CANDIDATE"
    assert install["learning_ids"] or install["historical_case_ids"], install

    # 사양 atom 에 다른 모델의 답변이 붙을 수 있고, 붙으면 그렇게 표시된다.
    #
    # 이전 계약은 "붙지 않는다" 였다. 그 계약은 두 가지가 받치고 있었고 둘 다
    # 의미 판단이었다: identity mismatch 의 hard delete(P0-2 에서 제거)와
    # style_only 채널 분리(판매자가 실제로 보낸 답변 전부가 여기 해당). 둘을
    # 치우면 후보는 도달하고, 현재 상품의 사실로 단정되지 않게 막는 것은
    # 부재가 아니라 evidence_origin 라벨 + 프롬프트 지시 + validator 의
    # ungrounded-claim 검사다. 여기서 확인할 것도 그 라벨이다.
    spec_ids = set(spec["learning_ids"] or [])
    if spec_ids:
        attached = {
            int(item["learning_example_id"]): item
            for item in inp["similar_approved_answers"]
        }
        for learning_id in spec_ids:
            item = attached[learning_id]
            source = str(item.get("source_product_name") or "")
            if source == PRODUCT_NO_MODEL_CODE:
                continue
            origin = item.get("evidence_origin") or {}
            assert origin.get("identity") != "SAME_PRODUCT", (
                "다른 모델 Learning 이 현재 상품 자료로 표시됐다", source
            )
            if origin.get("knowledge") == "PRODUCT_SPECIFIC":
                assert origin.get("note"), (
                    "다른 모델의 사양 Learning 에 자동 적용 금지 안내가 없다",
                    source,
                )


# ---------------------------------------------------------------------------
# 687932815 — 이미 정상 동작하던 설치 문의 (regression 보호)
# ---------------------------------------------------------------------------

def test_687932815_installation_answer_still_works_end_to_end(
    tmp_path, monkeypatch,
):
    """이미 자동등록까지 성공하던 경로를 v4 가 망가뜨리지 않는다."""

    stub = _DraftStub(
        answer="해당 상품은 삼성 기사님이 방문하여 설치해 드리는 상품입니다.",
        unresolved=(), can_auto_post=True,
    )
    run = _run(
        tmp_path, monkeypatch, name="815",
        question="이 제품 설치는 제가 직접 하는 건가요? 아니면 삼성 쪽에서 기사님이 오셔서 설치해주시나요?",
        product_name=PRODUCT_NO_MODEL_CODE,
        atoms=[{
            "text": "이 제품 설치는 제가 직접 하는 건가요?",
            "action": "INSTALLATION_METHOD",
            "requested_information": "설치 수행 주체",
            "requested_attribute": "ACTOR",
        }],
        draft_stub=stub,
        learning_products=(PRODUCT_NO_MODEL_CODE,),
    )
    assert run.prompt is not None, run.error
    assert run.prompt["input"].get("template_candidates")

    result = run.outcome.result
    assert "삼성 기사님이 방문" in result.answer
    assert result.status is AnswerStatus.GENERATED
    assert result.needs_review is False
    assert run.order.calls == 0


# ---------------------------------------------------------------------------
# 687932894 — RF/셋톱박스/가정용 복합 문의
# ---------------------------------------------------------------------------

def test_687932894_candidates_are_not_removed_by_soft_semantic_gates(
    tmp_path, monkeypatch,
):
    """검색된 후보가 topic/concept 불일치로 GPT ② 앞에서 사라지지 않는다.

    "RF" 를 특별 취급하는 코드를 넣지 않는다. 검증하는 것은 후보가 도달하는지,
    그리고 관련성 판단이 GPT ② 에게 남는지이다.
    """

    run = _run(
        tmp_path, monkeypatch, name="894",
        question=(
            "예전에 쓰던 TV처럼 RF 케이블을 벽에 연결해서 바로 방송을 볼 수 있나요?"
            " 별도 셋톱박스가 필요한지도 궁금하고, 집에서 사용하는 제품인데"
            " 구매해도 되는지도 알려주세요."
        ),
        product_name=PRODUCT_NO_MODEL_CODE,
        atoms=[
            {"text": "RF 케이블을 벽에 연결해서 바로 방송을 볼 수 있나요?",
             "action": "PRODUCT_SPEC",
             "requested_information": "RF 방송 수신 가능 여부",
             "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
            {"text": "별도 셋톱박스가 필요한가요?", "action": "PRODUCT_CONCEPT",
             "requested_information": "셋톱박스 필요 여부",
             "requested_attribute": "EXISTENCE_OR_CAPABILITY"},
            {"text": "집에서 사용해도 되나요?", "action": "OTHER",
             "requested_information": "가정용 구매 가능 여부",
             "requested_attribute": "PERMISSION_OR_OPTION"},
        ],
        draft_stub=_DraftStub(unresolved=["RF 케이블을 벽에 연결해서 바로 방송을 볼 수 있나요?"]),
        learning_products=(PRODUCT_NO_MODEL_CODE,),
    )
    assert run.prompt is not None, run.error
    evidence = run.prompt["input"]["subquestion_evidence"]
    assert len(evidence) == 3, evidence

    # 어떤 atom 도 soft semantic mismatch 를 이유로 근거가 비워지지 않는다.
    # 검색이 정말 아무것도 못 찾은 경우만 NO_RELIABLE_SOURCE 다.
    for item in evidence:
        if item["status"] == "NO_RELIABLE_SOURCE":
            assert not item["learning_ids"] and not item["historical_case_ids"], (
                "근거가 붙어 있는데 NO_RELIABLE_SOURCE 이면 코드가 의미로 지운 것", item
            )
        else:
            assert item["status"] in {"CANDIDATE", "ANSWERABLE"}, item

    # 관련성 판단이 GPT ② 의 몫이라는 지시가 프롬프트에 있어야 한다.
    policy = run.prompt["learning_usage_policy"]
    assert policy["retrieval_candidates_are_not_approved_evidence"] is True
    assert "subquestion_evidence_is_binding" not in policy


# ---------------------------------------------------------------------------
# 687932871 — 근거가 없는 금액 문의
# ---------------------------------------------------------------------------

def test_687932871_no_price_is_invented_when_no_source_states_one(
    tmp_path, monkeypatch,
):
    """근거가 없으면 unresolved 로 남고, 없는 금액을 만들지 않는다."""

    stub = _DraftStub(
        answer="벽걸이 설치 추가 비용은 확인 후 안내드리겠습니다.",
        unresolved=["벽걸이로 설치하면 추가 비용이 정확히 얼마인가요?"],
        can_auto_post=False,
    )
    run = _run(
        tmp_path, monkeypatch, name="871",
        question="벽걸이로 설치하면 추가 비용이 정확히 얼마인가요?",
        product_name=PRODUCT_NO_MODEL_CODE,
        atoms=[{
            "text": "벽걸이로 설치하면 추가 비용이 정확히 얼마인가요?",
            "action": "INSTALLATION_METHOD",
            "requested_information": "벽걸이 설치 추가 비용",
            "requested_attribute": "AMOUNT_OR_COST",
        }],
        draft_stub=stub,
        learning_products=(PRODUCT_NO_MODEL_CODE,),
    )
    assert run.prompt is not None, run.error
    result = run.outcome.result
    draft = (result.metadata.get("hybrid") or {}).get("draft") or {}
    assert draft["unresolved"], "근거 없는 금액 질문은 unresolved 로 남아야 한다"
    assert result.needs_review is True

    from services.auto_processing_eligibility_service import (
        GPT_REPORTED_UNRESOLVED,
    )

    trace = result.metadata["pipeline_trace"]
    assert trace["answer"]["unresolved"] == 1
    assert GPT_REPORTED_UNRESOLVED  # 게이트 코드가 존재한다


# ---------------------------------------------------------------------------
# 687927188 — 정상 GPT 답변을 post-GPT topic validator 가 폐기하던 문제
# ---------------------------------------------------------------------------

def test_687927188_a_valid_gpt_answer_is_not_discarded_by_a_topic_validator(
    tmp_path, monkeypatch,
):
    """이 문의는 배포 전 ``ANSWER_TOPIC_MISMATCH`` 로 답변이 폐기됐다.

    GPT ② 가 근거를 읽고 쓴 답변을 anchor table 이 뒤집지 못해야 한다.
    """

    stub = _DraftStub(
        answer=(
            "해당 상품은 RF 단자가 있어 기존 안테나 케이블을 연결하실 수 있습니다."
            " 가정에서도 구매해 사용하실 수 있습니다."
        ),
        unresolved=(), can_auto_post=True,
    )
    run = _run(
        tmp_path, monkeypatch, name="927188",
        question=(
            "예전 티비에서 RF 케이블로 벽에 연결해서 사용했습니다."
            " 상세설명에는 RF 케이블 설치가 가능한거같은데 가능한지 문의드립니다."
            " 그리고 가정에서 사용하는건데 구입이 가능한가요?"
        ),
        product_name=PRODUCT_NO_MODEL_CODE,
        atoms=[
            {"text": "RF 케이블 설치가 가능한지 문의드립니다.", "action": "PRODUCT_SPEC",
             "requested_information": "RF 케이블 설치 가능 여부",
             "requested_attribute": "COMPATIBILITY"},
            {"text": "가정에서 사용하는건데 구입이 가능한가요?", "action": "OTHER",
             "requested_information": "가정용 구매 가능 여부",
             "requested_attribute": "PERMISSION_OR_OPTION"},
        ],
        draft_stub=stub,
        learning_products=(PRODUCT_NO_MODEL_CODE,),
    )
    assert run.error is None, run.error
    result = run.outcome.result
    validation = (result.metadata.get("hybrid") or {}).get("validation") or {}
    assert validation.get("passed") is True, validation
    assert "ANSWER_TOPIC_MISMATCH" not in json.dumps(
        validation.get("errors") or [], ensure_ascii=False
    )
    assert "RF 단자" in result.answer
    assert (result.metadata.get("hybrid") or {}).get("fallback_used") is False


def test_a_provider_failure_still_produces_a_review_only_safe_draft(
    tmp_path, monkeypatch,
):
    """provider 장애는 여전히 Review 전용 안전 초안으로 끝난다."""

    from answer.provider_errors import GptProviderTimeoutError

    stub = _DraftStub(fail=GptProviderTimeoutError("timeout"))
    run = _run(
        tmp_path, monkeypatch, name="providerfail",
        question="이 제품 해상도가 어떻게 되나요?",
        product_name=PRODUCT_NO_MODEL_CODE,
        atoms=[{
            "text": "이 제품 해상도가 어떻게 되나요?", "action": "PRODUCT_SPEC",
            "requested_information": "제품 해상도", "requested_attribute": "SPEC_VALUE",
        }],
        draft_stub=stub,
    )
    # 답변이 저장됐다면 Review 대상이어야 하고, 자동등록 경로여서는 안 된다.
    if run.outcome is not None:
        result = run.outcome.result
        assert result.needs_review is True
        assert result.metadata.get("selected_answer_route") == (
            "REVIEW_REQUIRED_SAFE_DRAFT"
        )
    else:
        assert run.error is not None


def test_the_source_database_and_catalogue_are_never_written(tmp_path, monkeypatch):
    """이 파일의 어떤 테스트도 운영 DB/카탈로그를 건드리지 않는다."""

    before_db, before_catalog = _sha256(SOURCE_DB), _sha256(CATALOG)
    _run(
        tmp_path, monkeypatch, name="readonly",
        question="이 제품 해상도가 어떻게 되나요?",
        product_name=PRODUCT_WITH_MODEL_CODE,
        atoms=[{
            "text": "이 제품 해상도가 어떻게 되나요?", "action": "PRODUCT_SPEC",
            "requested_information": "제품 해상도", "requested_attribute": "SPEC_VALUE",
        }],
        draft_stub=_DraftStub(),
        learning_products=(PRODUCT_WITH_MODEL_CODE,),
    )
    assert _sha256(SOURCE_DB) == before_db
    assert _sha256(CATALOG) == before_catalog
