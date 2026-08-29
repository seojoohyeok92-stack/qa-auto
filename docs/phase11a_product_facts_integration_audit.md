# Phase 11-A — Q&A Auto × Product Facts 연동 구조 사전조사

조사일: 2026-08-29 · 범위: READ-ONLY 조사 · production 코드 변경 없음

> **가장 중요한 결론을 먼저 적습니다.**
> 이번 조사의 전제는 "Product Facts를 어디에 연결할지 설계한다"였지만, 실제 코드를 확인한 결과
> **Product Facts 연동은 이미 구현되어 production 실행 경로에서 동작 중**입니다.
> `ProductFactRepository` → `ProductKnowledgeService` → `AnswerService` → `HybridAnswerService`(prompt/evidence)
> → `AnswerValidator` → `AutoProcessingEligibilityService`(자동등록 차단)까지 전 구간이 연결되어 있고,
> 안전 계약(§19)·Learning 충돌 정책(§18)·자동등록 gate(§26)도 이미 존재합니다.
> 따라서 Phase 11-B의 과제는 "연결"이 아니라 **최신 DB artifact 반입과 남은 3개 gate 보완**입니다.

---

## 1. 실행 환경

| 항목 | 값 |
|---|---|
| 실제 프로젝트 root | `<홈>\Desktop\프로젝트\Q&A 통합\git\qa-auto` |
| 사양서가 예상한 경로 | `<홈>\Desktop\Q&A 통합\Q&A auto` — **존재하지 않음** |
| Python / pytest | pytest 9.1.1 |
| 조사 방식 | 파일 읽기, `mode=ro` + `PRAGMA query_only=ON` DB 조회만 |

**사양서 전제와 다른 점 2가지를 먼저 보고합니다.**

1. 프로젝트 경로가 사양서와 다릅니다. 위 실제 경로를 기준으로 조사했습니다.
2. 사양서는 "이 PC는 상품DB 전용 PC와 다른 PC"라고 했으나, **이 PC에 두 프로젝트가 모두 있습니다.**
   `<홈>\Desktop\상품DB` 가 실재하며 Product Facts 프로젝트 전체가 들어 있습니다.
   이번 조사에서 그 디렉터리는 **읽기 전용 대조 목적으로만** 접근했고(§10, §31), 복사·교체·수정은 하지 않았습니다.

---

## 2. Git 상태

작업 시작 시점에 측정했습니다.

| 항목 | 값 |
|---|---|
| branch | `main` |
| HEAD | `3950e8b` — `26.8.28v2 - 문의 단건 진단 Export 추가` |
| tracking | `origin/main` (`## main...origin/main`, 앞뒤 차이 없음) |
| `git status --porcelain` | **비어 있음 (clean)** |

작업 시작 시점에 사용자가 수정해 둔 파일은 **없었습니다.** 따라서 이번 조사에서 생성한 파일과
기존 변경사항을 구분할 필요가 없습니다. commit / push / reset / checkout / clean / stash 는 하지 않았습니다.

---

## 3. 테스트 Baseline

과거 숫자를 재사용하지 않고 이 PC에서 직접 측정했습니다.

```
3494 passed in 1099.17s (0:18:19)
```

| 항목 | 값 |
|---|---|
| 수집된 테스트 | 3,494개 (`--collect-only` 기준, 테스트 파일 165개) |
| passed | **3,494** |
| failed | **0** |
| skipped | **0** |
| 실행 불가능한 테스트 | 없음 |
| 소요 시간 | 18분 19초 |

**환경 관련 관찰 1건**: 전체 실행 중 CPU 사용률이 낮은 구간이 길었습니다(약 460 CPU초 / 1,099초 실체시간).
실패는 없으므로 결함은 아니지만, 테스트에 대기(sleep/재시도) 구간이 상당히 포함되어 있다는 뜻입니다.
Phase 11-B에서 반복 실행이 잦아질 것이므로, 필요하면 대상 테스트만 선택 실행하는 편이 낫습니다.

`data/product_facts.db`가 존재하므로 `@real_db` 마커가 붙은 실제-DB 테스트도 **모두 실행되어 통과**했습니다.

---

## 4. 현재 Q&A Auto Architecture

git이 추적하는 파일 기준 구성입니다. "존재함"과 "production 실행 경로에서 사용됨"을 구분해 표기했습니다.

| 구성요소 | 파일 수 | 역할 | production 사용 |
|---|---|---|---|
| `services/` | 76 | 파이프라인 본체(분석·생성·검증·자동등록) | ACTIVE |
| `tests/` | 167 | 테스트 | TEST_ONLY |
| `answer/` | 38 | 답변 도메인 모델·validator·provider·prompt | ACTIVE |
| `scripts/` | 30 | 운영 진단·backfill·golden run | 수동 실행 |
| `repositories/` | 29 | DB 접근 계층 | ACTIVE |
| `ui/` | 26 | Streamlit 화면 | ACTIVE |
| `dps/` | 14 | DPS(주문/배송/설치) 조회·Chrome 자동화 | ACTIVE |
| `docs/` | 13 | 문서 | 문서 |
| `api/` | 7 | Naver API 클라이언트(주문/QnA/인증) | ACTIVE |
| `answer_data/` | 7 | 정책·설정 JSON | ACTIVE |
| `core/` | 5 | 분류 엔진·시간·빌드정보 | ACTIVE |
| `naver_workflow/` | 4 | 자동 runner·Kakao 알림 | ACTIVE |
| `uat/` | 4 | UAT | TEST_ONLY |
| `ai/` | 3 | 구형 answer_service / prompt_builder | 확인 필요(§9) |
| `workflow/` | 2 | 워크플로 모델 | ACTIVE |

주요 파일 규모: `services/answer_service.py` 3,561줄, `services/inquiry_analysis_service.py` 1,468줄,
`services/hybrid_answer_service.py` 1,179줄, `services/learning_context_service.py` 814줄,
`services/draft_generation_service.py` 743줄, `services/auto_processing_eligibility_service.py` 505줄.

`app.py`(Streamlit), `main.py`, `config.py`가 최상위에 있습니다.

---

## 5. Inquiry 데이터 구조

`repositories/database.py`가 정의합니다. 기본 `CREATE TABLE inquiries` 이후 `ALTER TABLE`로 컬럼이 추가되어 왔습니다.

**기본 컬럼**: `id`, `store_code`, `source_type`, `source_question_id`, `inquiry_type`, `title`, `content`,
`product_name`, `option_name`, `customer_display`, `order_id`, `product_order_id`, `registered_at`,
`workflow_status`, `answer_status`, `post_status`, `created_at`, `updated_at`, `raw_json`.

**ALTER로 추가된 컬럼(발췌)**: `product_id`, `external_inquiry_id`, `masked_writer_id`, `source_answered`,
`source_status`, `source_created_at`, `source_updated_at`, `last_synced_at`, `order_date`, `order_status`,
`order_lookup_at`, `posted_at`, `post_attempted_at`, `post_error_code`, `post_error_message`,
`post_http_status`, `post_response_id`, `posted_answer_hash`, `posted_draft_id`, `post_actor`.

사양서 §9가 물은 항목의 실제 존재 여부:

| 요구 항목 | 실제 위치 | 존재 |
|---|---|---|
| inquiry id | `inquiries.id`, `source_question_id`, `external_inquiry_id` | ○ |
| platform | `source_type` (`PRODUCT_INQUIRY` / `CUSTOMER_INQUIRY`), `store_code` | ○ |
| product identifier | `inquiries.product_id` | ○ (커버리지 문제 있음 — §6) |
| product name | `inquiries.product_name` | ○ |
| question | `title` + `content` | ○ |
| order number | `order_id`, `product_order_id` | ○ |
| intent / classification | **`inquiries`에는 컬럼 없음.** 실행 시점에 `InquiryAnalysis` / `processing_plan`으로 산출되어 metadata·`activity_logs`에 기록 | 컬럼 ✕ |
| `requires_order_lookup` / `requires_dps_lookup` | **컬럼 없음.** `processing_plan`(runtime) 속성 | 컬럼 ✕ |
| draft answer | `answer_drafts`, `answer_versions` 테이블 | ○ (별도 테이블) |
| final answer | `answer_versions`, `naver_posted_answer` 계열 | ○ |
| approval / review | `approval_history`, `post_reviews`, `post_corrections` | ○ |
| auto-post | `naver_auto_post_settings/state/runs/locks`, `naver_post_attempts` | ○ |
| Naver 등록 상태 | `inquiries.post_status`, `posted_at`, `post_error_code`, `post_response_id` | ○ |

즉 **intent와 lookup 요구 플래그는 DB 컬럼이 아니라 실행 시점 산출물**입니다. Product Facts 연동 설계에서
"DB에서 intent를 읽어 분기한다"는 가정은 성립하지 않습니다.

---

## 6. 상품 식별자 — 이번 조사의 최우선 항목

### 6.1 코드 경로

`services/naver_inquiry_normalizer.py`가 Naver 응답을 `NormalizedInquiry`로 정규화하고
`to_work_item()`으로 넘기면 `services/inquiry_sync_service.py`가 `inquiries`에 저장합니다.

- 상품문의: `questionId`(필수), `productId`, `productName`, `maskedWriterId`, `createDate`
- 고객문의: `inquiryNo`/`inquiryId`(필수), `productId`, `orderId`/`orderNo`, `productOrderId`/`productOrderNo`

두 경로 모두 `_first(payload, "productId")` → `NormalizedInquiry.product_id` → `inquiries.product_id`입니다.

### 6.2 실측 (개발 PC `data/oje_automation.db`, READ-ONLY)

문의 총 2,772건.

| 구분 | 건수 | `product_id` 보유 |
|---|---|---|
| `PRODUCT_INQUIRY` | 1,662 | **717 (43.1%)** |
| `CUSTOMER_INQUIRY` | 1,110 | **0 (0%)** |
| 합계 | 2,772 | **717 (25.9%)** |

`product_name`은 2,772건 전부 존재, `option_name`은 593건 존재.

`raw_json` 형태별로 나누면 소실 지점이 정확히 드러납니다.

| source_type | payload 형태 | 건수 | 컬럼 저장 | raw에 `productId` | raw에 `product_id` |
|---|---|---|---|---|---|
| PRODUCT_INQUIRY | NEW_API | 717 | ○ | ○ | – |
| PRODUCT_INQUIRY | LEGACY_WORKITEM | 945 | **✕** | – | ○ (복구 가능) |
| CUSTOMER_INQUIRY | LEGACY_WORKITEM | 668 | **✕** | – | ○ (복구 가능) |
| CUSTOMER_INQUIRY | NEW_API | 442 | **✕** | **✕** | **✕** |

CUSTOMER_INQUIRY NEW_API의 `source_payload` 키는 `['answered', 'inquiryNo', 'inquiryRegistrationDateTime']`뿐입니다.
**상품 식별자가 애초에 응답에 없습니다.**

### 6.3 사양서 §10의 8개 질문에 대한 답

1. **Naver 문의 API에서 상품번호가 들어오는가?**
   상품문의는 들어옵니다. 고객문의(신규 API 형태)는 **들어오지 않습니다.**
2. **필드명은?** `productId` (camelCase). 저장 컬럼명은 `inquiries.product_id`.
3. **inquiry DB에 그대로 저장되는가?** 값이 오면 문자열 그대로 저장됩니다. 가공·정규화 없음.
4. **저장되지 않는다면 어디에서 사라지는가?** 두 가지 원인이 분리됩니다.
   - **구조적 부재**: 고객문의 신규 API 응답에 상품 식별자가 없음 → 442건. 코드로 해결 불가.
   - **적재 시점 문제**: 구형 work-item 경로로 들어온 1,613건은 `raw_json`에 `product_id`가 남아 있으나
     컬럼이 비어 있음 → **DB에서 복구 가능**(이번 Phase에서는 실행하지 않음).
5. **가장 강한 identifier는?** `inquiries.product_id` (Naver `productId`). 이것 외에 listing을 특정할 수 있는 값은 없습니다.
6. **상품명만 저장되는 경우가 있는가?** 있습니다. 2,055건(74.1%)이 `product_name`만 있습니다.
7. **option 정보가 존재하는가?** `inquiries.option_name`에 593건 존재합니다. 단 Product Facts는 listing 단위이므로 option은 매칭 키가 아닙니다.
8. **동일 모델을 여러 listing에서 팔 때 현재 시스템은 어떻게 구분하는가?**
   `product_id`가 있으면 listing 단위로 정확히 구분됩니다. 없으면 **구분하지 못합니다.**
   현재 `ProductKnowledgeService`는 `product_id`가 없으면 `NO_PRODUCT_ID`로 조회 자체를 포기합니다(안전한 동작).

---

## 7. Product Facts Exact Join 가능성

### 판정: **DIRECT_EXACT_JOIN_POSSIBLE**

근거는 코드와 실측 양쪽입니다.

**의미·출처 동일성**
`repositories/product_fact_repository.py`는 `SELECT ... FROM listings WHERE product_id = ?`,
`canonical_fact_listings cfl ... WHERE cfl.product_id = ?`로 **가공 없는 exact match**를 씁니다.
Product Facts의 `listings.product_url`은 `https://smartstore.naver.com/<store>/products/12601323000` 형태이고
`listings.product_id`는 그 URL의 상품번호와 같습니다. Q&A Auto의 `inquiries.product_id`도 같은 Naver 상품번호입니다.
즉 **동일한 의미의 동일한 출처**입니다.

**실측 (사양서는 값 비교를 요구하지 않았으나, 두 DB가 이 PC에 모두 있어 READ-ONLY로 확인했습니다)**

| 항목 | 값 |
|---|---|
| 문의에 등장한 서로 다른 `product_id` | 79 |
| Product Facts `listings` 수 | 94 |
| **exact match** | **59 (74.7%)** |
| 문의에만 있음(미수집 상품) | 20 |
| Product Facts에만 있음 | 35 |

형식 불일치·정규화 필요·부분 일치는 **한 건도 없었습니다.** 매칭 실패 20건은 형식 문제가 아니라
"그 상품을 아직 수집하지 않았다"입니다. 매핑 테이블은 필요 없습니다.

---

## 8. Product Match 전략

현재 구조에 맞춘 우선순위입니다. 예시를 그대로 쓰지 않고 실제 가용 신호만 사용했습니다.

| 순위 | 키 | 결과 상태 | 채택 |
|---|---|---|---|
| 1 | `inquiries.product_id` = `listings.product_id` (exact) | `EXACT_LISTING_MATCH` | **채택** |
| 2 | `raw_json`에서 복구한 `product_id` (exact) | `EXACT_LISTING_MATCH` | 채택 권장(별도 backfill 필요) |
| 3 | `product_name`에서 추출한 model_code | `EXACT_MODEL_MATCH` / `AMBIGUOUS_MODEL` | **채택하지 않음 — 아래 근거** |
| 4 | 상품 제목 fuzzy 매칭 | – | **금지** |

### model_code 매칭을 채택하지 않는 실측 근거

`services.product_fact_guard.extract_model_code`를 고객문의 상품명 1,110건에 실제로 적용했습니다.

- 모델코드 추출 성공 733건 / 실패 377건, 서로 다른 코드 **57종**
- 추출 결과 상위: `LH50BEFHLGFXKR`(151), **`BE85F`(92)**, `LH43BEFHLGFXKR`(87), **`LH43B`(46)**, **`LH50BED-H`(43)**, **`LH50BEF`(38)**
- 이 57종 중 Product Facts의 `canonical_fact_listings.model_code`에 존재하는 것은 **단 2종**

즉 추출 결과의 대부분이 `LH43B`, `LH50BEF` 같은 **부분 코드**입니다. 이런 값으로 prefix 매칭을 하면
`LH43BEFHLGFXKR`와 `LH43BEHHLGFXKR`(BEF vs BEH, 다른 연식)를 구분하지 못합니다.
또한 Product Facts 쪽 `model_code` 값에도 `삼성 50인치 비즈니스TV + 무빙 이동식 스탠드` 같은
**코드가 아닌 표시명**이 섞여 있습니다(172종 중 일부).

참고로 Product Facts에서 한 model_code가 2개 이상 listing에 대응하는 경우가 **8종** 있습니다
(`LH85BEHHLGFXKR` 3개, `LS32DM501EKXKR`·`LS22D400GAKXKR`·`LS25BG400EKXKR`·`LS24D400GAKXKR` 각 2개).
모델 단위 매칭은 이 경우 원리적으로 listing을 특정할 수 없습니다.

**결론**: `NO_MATCH`가 잘못된 상품 연결보다 안전하다는 원칙 그대로, `product_id`가 없으면 매칭하지 않습니다.
현재 코드가 이미 그렇게 동작합니다.

### 필요한 상태값

| 상태 | 필요성 | 현재 구현 |
|---|---|---|
| `EXACT_LISTING_MATCH` | 필요 | `ProductKnowledgeResult.matched=True`로 존재(이름만 다름) |
| `NO_MATCH` | 필요 | `unavailable_reason` = `PRODUCT_NOT_IN_PRODUCT_DB` / `NO_PRODUCT_ID` |
| `AMBIGUOUS_MODEL` | **현재 불필요** | 모델 매칭을 하지 않으므로 발생하지 않음. 모델 매칭을 도입할 때만 필요 |
| `EXACT_MODEL_MATCH` | **현재 불필요** | 위와 같음 |

`model_code`는 매칭 키가 아니라 **검증 키**로만 쓰입니다 — `_exclusion_reason`의 `MODEL_SCOPE_MISMATCH`가
"찾은 fact가 다른 모델 것이면 버린다"에 사용합니다. 이 용법은 안전하며 유지해야 합니다.

---

## 9. 기존 Product Facts 관련 코드 전수 조사

검색어 `product_facts` / `ProductFacts` / `product_fact` / `product_knowledge` / `specification` / `catalog` 등으로
62개 파일이 매칭되었습니다. 분류 결과입니다.

| 항목 | 분류 | 비고 |
|---|---|---|
| `repositories/product_fact_repository.py` | **ACTIVE_PRODUCTION** | `mode=ro` + `query_only` 강제, 쓰기 시 즉시 실패 |
| `services/product_knowledge_service.py` (649줄) | **ACTIVE_PRODUCTION** | 안전 판정 단일 지점 |
| `services/product_fact_guard.py` | **ACTIVE_PRODUCTION** | 질문이 상품스펙 민감인지 분류 |
| `services/answer_service.py` | **ACTIVE_PRODUCTION** | 조회 주체(1236행), guard 판정(1673행), 자동등록 판단(2811~2936행) |
| `services/hybrid_answer_service.py` | **ACTIVE_PRODUCTION** | prompt/evidence 주입, `VERIFIED_PRODUCT_FACT` |
| `services/auto_processing_eligibility_service.py` | **ACTIVE_PRODUCTION** | `PRODUCT_FACT_NOT_VERIFIED` 차단 사유 |
| `services/auto_post_pipeline_service.py` | **ACTIVE_PRODUCTION** | guard 메타데이터 소비 |
| `services/learning_evidence_policy.py` | **ACTIVE_PRODUCTION** | `PRODUCT_FACT_VS_LEARNING_CONFLICT` |
| `services/learning_context_service.py` | **ACTIVE_PRODUCTION** | guard 결과를 context에 기록 |
| `services/learning_compatibility_service.py` | **ACTIVE_PRODUCTION** | `extract_model_code` 재사용 |
| `services/historical_reaudit_service.py` | **ACTIVE_PRODUCTION** | `classify_product_fact` 재사용 |
| `services/draft_generation_service.py` | **ACTIVE_PRODUCTION** | 근거 출처에 `PRODUCT_DB` 포함 |
| `ui/answer_status_presenter.py` | **ACTIVE_PRODUCTION** | 화면에 guard 결과 표시 |
| `scripts/export_inquiry_diagnostics.py` | 운영 도구 | guard 항목 기록 |
| `scripts/e2e_operational_matrix.py`, `golden_matrix.py`, `production_golden_run.py`, `diagnose_auto_post_hold.py`, `export_qa_learning_excel.py`, `backfill_learning_model_code.py` | 운영 도구(수동 실행) | – |
| `tests/test_product_facts_b5.py`, `test_product_facts_e2e_b5.py`, `test_product_fact_guard_excluded_and_shortcuts.py` | **TEST_ONLY** | 실제 DB를 읽는 `@real_db` 테스트 포함 |
| `ai/answer_service.py`, `ai/prompt_builder.py` | **UNKNOWN** | `answer/` 계열과 별개. production 진입점에서의 사용 여부를 Phase 11-B에서 확정 필요 |
| `answer/facts.py`, `answer/fact_selection.py` | ACTIVE_PRODUCTION | **주의**: 여기의 "fact"는 Product Facts가 아니라 inquiry/order/DPS 사실입니다. 이름이 겹치지만 다른 개념입니다 |
| `OJE_PRODUCT_FACTS_DB_PATH` (`.env.example` 6행) | **ACTIVE_PRODUCTION** | DB 경로 전환·비활성화용 |

DEAD_CODE로 분류된 항목은 없습니다.

---

## 10. 개발 PC 기존 `data/product_facts.db`

| 항목 | 값 |
|---|---|
| 경로 | `data/product_facts.db` |
| 크기 | 60,170,240 bytes (약 57.4 MB) |
| 수정 시각 | 2026-08-25 12:30:32 |
| SHA-256 | `cddf3082df82d87065a452ee8140af9f42c4d0b31e91753597717f55cc82ac4c` |
| git 추적 | **추적 중**. `.gitignore`가 `*.db`를 무시하지만 36행 `!data/product_facts.db`로 명시 예외 |

### 이 파일의 정체를 해시로 확정했습니다

| 파일 | SHA-256 |
|---|---|
| 개발 PC `qa-auto/data/product_facts.db` | `cddf3082…ac82ac4c` |
| 상품DB `data/archive/product_facts_before_phase10_step2c_20260828T183217Z.db` | `cddf3082…ac82ac4c` **동일** |
| 상품DB 현행 `data/product_facts.db` | `8fe643c5…4e563f93` **다름** |

**개발 PC의 DB는 상품DB의 Step 2C 적용 직전 스냅샷과 바이트 단위로 동일합니다.**
사양서 §2의 "최신 DB는 아직 복사하지 않았다"가 사실임이 해시로 확인되었습니다.

### 내용

| 테이블 | 행 수 |
|---|---|
| `listings` | 94 (`collection_status` = `COLLECTION_SUCCESS` 94) |
| `canonical_facts` | 6,331 (ACTIVE 3,894 / SUPERSEDED 2,437) |
| `canonical_fact_values` | 6,747 |
| `canonical_fact_provenance` | 11,849 |
| `canonical_fact_listings` | 6,495 |
| `facts` / `provenance` | 6,668 / 6,668 |
| `image_analyses` / `image_assets` | 867 / 471 |

ACTIVE 3,894건의 상태 분포:

- `verification_status`: VERIFIED 3,546 / NEEDS_REVIEW 348
- `resolution_status`: SINGLE_SOURCE 2,734 / MATCH 812 / NEEDS_REVIEW 318 / CONFLICT 30
- `volatility`: STATIC_PRODUCT_FACT 2,261 / SEMI_STATIC_POLICY_FACT 881 / **DYNAMIC_LISTING_FACT 752**

`listings` 스키마: `listing_id`, `product_id`, `product_url`, `input_listing_name`, `pilot_category`,
`run_id`, **`collection_status`**, `collection_run_id`.

**사양서 §2가 기술한 최종 상태(COLLECTION_SUCCESS 93 / 판매종료 1)와 다릅니다**(여기서는 94/0).
이것도 이 파일이 최신이 아니라는 증거입니다.

### production answer path에서 실제로 읽히는가

**읽힙니다.** `ProductFactRepository`의 기본 경로가 `data/product_facts.db`이고,
`AnswerService.__init__`이 `ProductKnowledgeService()`를 기본 생성하므로 답변 생성 시마다 이 파일이 열립니다.
즉 **현재 운영 답변은 Step 2C 이전 스냅샷을 근거로 생성되고 있습니다.**

삭제·수정·교체·migration은 하지 않았습니다.

---

## 11. Template Pipeline

- **저장/검색**: 템플릿 본문은 `answer_data/configs/answer_policy.json`이 아니라 DB(`learning_examples` 계열)와
  `AnswerWrapperTemplate`(`answer/config_loader.py:177`)에 나뉘어 있습니다.
  `answer_policy.json`은 `wrapper`(4개)와 `hard_block_rules`(4개)만 담습니다.
- **적용 조건**: `services/answer_service.py`의 `EXACT_TEMPLATE_MATCH_KINDS`(165행)와
  `_template_may_answer`(180행)가 정확 매칭 여부를 판정합니다.
- **우선순위**: `selected_answer_route`가 `TEMPLATE` / `PRODUCT_DB` / … 중 하나로 확정되며,
  두 경로 모두 `AutoProcessingEligibilityService`(389행)에서 동등하게 자동등록 후보로 취급됩니다.
- **상품 스펙 하드코딩**: 템플릿 본문에 특정 상품의 스펙 수치가 박혀 있는 사례는 이번 검색에서 발견하지 못했습니다.
  다만 `answer_data/configs/model_codes.json`과 `answer_data/learning/model_data_with_color.json`에
  모델별 데이터가 있어, Phase 11-B에서 Product Facts와 중복·모순 여부를 별도로 대조해야 합니다.
- **덮어쓰기 위험**: 현재 구조에서 Product Facts는 **route를 빼앗지 않습니다.** `TEMPLATE`이 정확 매칭되면
  그 경로가 선택되고, Product Facts는 근거(evidence)로만 참여합니다. 정책 템플릿을 상품 사실이 덮어쓰는 구조가 아닙니다.

---

## 12. Learning Pipeline

**DB/테이블** (개발 PC `oje_automation.db` 실측)

| 테이블 | 행 수 |
|---|---|
| `learning_examples` | 901 |
| `answer_learning_provenance` | 799 |
| `historical_cases` / `historical_case_versions` | 257 / 257 |
| `learning_feedback` | 202 |
| `learning_signals` | 18 |
| `project_knowledge` | 8 |
| `learning_candidates` / `learning_signal_confirmations` | 0 / 0 |

**구성요소**: `repositories/learning_repository.py` 외 7개 저장소, `services/learning_context_service.py`(조립),
`services/similar_answer_service.py`(검색·랭킹), `services/learning_evidence_policy.py`(사용 가부 판정),
`services/learning_compatibility_service.py`(상품/주제 적합성), `answer/positive_learning.py`,
`answer/learning_signal.py`, `answer/learning_feedback.py`, `answer/learning_conflict.py`.

**랭킹**: `similar_answer_service`가 relevance band → answer_support band → authority → 원 relevance →
authority → rating → created_at 순으로 정렬합니다. authority는 `learning_evidence_policy.LEARNING_AUTHORITY`
(`APPROVED_EDITED` 8 > `APPROVED_UNEDITED` 6 > `SELLER_ANSWER_VERIFIED` 4).

**성격 판정**: Learning은 **과거 상담/응대 사례 중심**입니다. 승인된 과거 답변, 편집 이력, 피드백 신호,
sub-question 단위 검색 결과를 담습니다. 다만 과거 답변 본문에 스펙 수치가 자연어로 섞여 있으므로
**사실상 상품 사실이 비구조적으로 들어 있는 상태**이며, 이것이 §13의 충돌 원인입니다.
구조화된 상품 사실 저장소 역할은 하지 못합니다(필드·검증상태·provenance 개념이 없음).

---

## 13. Product Facts vs Learning — 역할 분리와 충돌

**역할 분리는 가능하며, 이미 코드로 강제되어 있습니다.**

`services/learning_evidence_policy.py`의 `fact_conflicts()`가 승인된 Learning 답변과 안전한 Product Fact를
대조합니다. 두 가지 방식으로 모순을 잡습니다.

1. **극성 충돌(`opposed_polarity`)** — 답변이 "있다"인데 fact가 "없다"인 경우 등
2. **수량 충돌(`opposed_quantity`)** — `quantities_conflict()`가 "HDMI 2개"와 fact `hdmi_port_count=3`처럼
   극성이 없는 수치 모순을 잡습니다

충돌이 하나라도 발견되면 `evaluate()`가 `LearningEvidenceDecision(False, "PRODUCT_FACT_VS_LEARNING_CONFLICT")`를
반환하여 **Learning이 근거로 쓰이지 못하게 합니다.**

즉 사양서 §18의 예시(Learning "HDMI 2개" vs Product Facts `hdmi_port_count=3` VERIFIED)는
**이미 Product Facts 우위로 처리되고 있습니다.**

**권장 정책(변경 제안 아님, 현행 유지 확인)**: 객관적 상품 스펙에서는 Product Facts가 authority,
응대 방식·정책·예외처리에서는 Learning이 authority. 현행 구현이 이미 이 형태입니다.

---

## 14. DPS Pipeline

**흐름**: `InquiryProcessingPlanService`가 `plan.requires_order_lookup` / `plan.requires_dps_lookup`을 결정 →
`AnswerService`가 해당 워크플로 단계를 reopen/skip → `dps/service.py`·`dps/provider.py`가 조회 →
`repositories/dps_repository.py`에 결과 저장.

**Product Facts가 DPS를 우회할 수 있는가 — 코드 순서로 확인했습니다.**

`services/answer_service.py`에서
`plan = self.plans.create(...)` (1215행) → `product_knowledge = self.product_knowledge.facts_for_inquiry(...)` (1236행)
→ `if plan.requires_dps_lookup:` (1327행)

**`plan`이 Product Facts 조회보다 먼저 확정되고, Product Facts 결과가 `plan`으로 되돌아가는 경로가 없습니다.**
따라서 Product Facts가 `requires_dps_lookup`을 끄거나 DPS 라우팅을 대체하는 것은 **구조적으로 불가능**합니다.

추가로 `UNUSABLE_VOLATILITY = {"DYNAMIC_LISTING_FACT"}`가 가격·재고·배송비 같은 변동 사실을 근거에서 제외하므로,
Product Facts가 "현재 배송상태/설치예정일/주문상태"의 근거로 쓰일 여지도 없습니다.
Phase 11-B에서 이 두 성질(순서, 변동성 제외)을 깨지 않는 것이 필수 조건입니다.

---

## 15. Intent / Semantic Analyzer

- `services/inquiry_analysis_service.py`(1,468줄)가 규칙 기반 분류, `services/semantic_analysis.py` +
  `services/gpt_semantic_analyzer_service.py`가 의미 분석(기본 OFF — `OJE_SEMANTIC_ANALYZER_ENABLED=0`).
- 상품스펙 질문은 **intent 분류와 별개로** `services/product_fact_guard.py`의 `classify_product_fact()`가
  `PRODUCT_FACT_TERMS`(인치·해상도·hdmi·usb·vesa·스탠드·무게·구성품 등)와
  `FACT_QUERY_MARKERS`(몇·얼마·있나요·되나요·포함·지원 등)의 동시 등장으로 판정합니다.
- `COMMON_POLICY_TERMS`(거래명세서·영수증·반품 절차 등)가 **우선**하여, 정책 질문이 상품스펙 질문으로
  오분류되지 않도록 막고 있습니다.
- 사양서가 예시로 든 질문들("HDMI 몇 개예요?", "블루투스 돼요?", "무게가 얼마예요?", "베사 규격이 어떻게 돼요?",
  "스탠드 포함인가요?")은 모두 `FIELD_TOPICS` 매핑에 실제 항목이 있습니다.
  다만 **"리모컨 포함인가요?"는 `FIELD_TOPICS`에 대응 항목이 없습니다** — 현재는 어떤 필드도 요청하지 않아
  `NO_PRODUCT_FACT_TOPIC`으로 조회를 건너뜁니다. 안전하지만 답변 불가입니다.

외부 GPT/API는 호출하지 않았습니다.

---

## 16. Answer Context

`AnswerFacts`(`answer/facts.py`)가 `inquiry` / `product` / `order` / `delivery` / `installation` / `dps` /
`rule` / `activity` / `policy` / `warnings`로 구성됩니다. 여기의 `product`는 문의에 딸린 상품 표시정보이지
Product Facts가 아닙니다.

Product Facts는 **`AnswerFacts`에 들어가지 않고** `request.metadata["product_knowledge"]`에
`ProductKnowledgeResult` 객체로 실려 다닙니다. `HybridAnswerService._product_facts_context()`가 이를 읽어
prompt context로 변환합니다.

**변경 범위가 가장 작은 위치**는 지금 그대로입니다. `metadata`에 싣는 방식은 `AnswerFacts` 데이터클래스와
`to_prompt_dict()`의 개인정보 제거 로직, validator의 fact-path 해석(`answer/fact_selection.py`)을
전혀 건드리지 않습니다. Phase 11-B에서 `AnswerFacts`에 필드를 새로 추가하는 것은 **권장하지 않습니다.**

---

## 17. Prompt Builder

- `answer/prompt_builder.py`(314줄)가 실사용 빌더입니다. `build()`(147행)가 `system_policy`(200행),
  `allowed_fact_paths`(162행)를 포함한 payload를 만들고 `safe_payload()`가 정제합니다.
- `ai/prompt_builder.py`(267줄)는 별개 파일로, production 사용 여부가 불확실합니다(§9 UNKNOWN).
- Product Facts는 `HybridAnswerService._product_facts_context()`가
  `{"product_facts": {"instructions": <prompt_block()>, "facts": [...], "product_id": ...}}` 형태로 넣습니다.

**자연어 참고문장 vs structured evidence block — 현행은 둘의 조합이며 이 방식이 옳습니다.**
`prompt_block()`이 "모르는 값은 채우지 말라"는 규칙 문장을 포함하고(`test_prompt_block_states_the_unknown_rule`),
`facts` 배열이 구조화된 값·필드키·provenance를 함께 전달합니다. validator가 같은 구조를 근거로 재검증할 수 있으므로
**structured evidence block을 유지하고, 자연어 단독 주입으로 바꾸지 않는 것**을 권장합니다.
이번 Phase에서 prompt는 수정하지 않았습니다.

---

## 18. 현재 Evidence 구조

한 답변에 참여할 수 있는 근거는 다음과 같습니다.

| 근거 | 출처 | 표기 |
|---|---|---|
| 정책·고정 문구 | `answer_policy.json`, wrapper 템플릿 | `FIXED_TEMPLATE`, `POLICY` |
| 현재 주문/배송/설치 | DPS 조회 결과 | `dps.*` fact path |
| 상품 객관 스펙 | Product Facts | `VERIFIED_PRODUCT_FACT`, `PRODUCT_DB` |
| 과거 승인 답변 | Learning | `ACTIVE_POSITIVE_LEARNING` |
| 현재 문의 본문 | inquiry | `CURRENT_INQUIRY` |

`services/learning_context_service.py:789`에 이미
`"facts_authority": "PRODUCT_DB_POLICY_VALIDATOR_FIRST"`라는 문자열이 존재합니다.

---

## 19. 권장 Evidence Authority Matrix

도메인별로 나눈 권장안입니다. 현행 구현과 어긋나는 부분이 없어 **변경 없이 문서화만 필요**합니다.

| 질문 도메인 | 1순위 | 2순위 | 사용 금지 |
|---|---|---|---|
| 회사 고정정책(반품·교환·영수증) | Template / 정책 JSON | Learning | Product Facts |
| 현재 주문·배송·설치 일정 | DPS | Template(실패 시 안내문) | **Product Facts (절대)** |
| 상품 고유 객관 스펙 | **Product Facts (VERIFIED)** | Learning(모순 없을 때만) | 추측 |
| 과거 응대 방식·어투·예외처리 | Learning | Template | Product Facts |
| 최종 자연어 작성 | GPT | – | GPT가 사실 생성 |

핵심 원칙: **GPT는 근거를 만들지 않고 문장만 만든다.** 현행 validator가 이를 강제합니다.

---

## 20. Product Facts 안전 계약

사양서 §24가 요구한 계약은 **이미 `services/product_knowledge_service.py`의 `_exclusion_reason()` 한 곳에서
전부 강제**되고 있습니다. 조건을 하나라도 통과하지 못하면 근거에서 제외됩니다.

| 조건 | 미충족 시 사유 코드 |
|---|---|
| `lifecycle_status = ACTIVE` | `SUPERSEDED_BY_LATER_RUN` |
| `verification_status = VERIFIED` | `VERIFICATION_<상태>` |
| `resolution_status ∉ {CONFLICT, NEEDS_REVIEW}` | `RESOLUTION_CONFLICT` / `RESOLUTION_NEEDS_REVIEW` |
| `volatility ≠ DYNAMIC_LISTING_FACT` | `VOLATILE_LISTING_FACT` |
| 선택된 값 존재 | `NO_SELECTED_VALUE` |
| 값이 비어있지 않음 | `VALUE_EMPTY_OR_UNKNOWN` |
| ACTIVE provenance 존재 | `NO_ACTIVE_PROVENANCE` |
| provenance 중 VERIFIED 존재 | `PROVENANCE_NOT_VERIFIED` |
| 기대 모델과 일치 | `MODEL_SCOPE_MISMATCH` |

**"없는 fact는 부정 사실이 아니다"**가 명시적으로 구현되어 있습니다
(`VALUE_EMPTY_OR_UNKNOWN` 주석, `test_D_missing_field_never_becomes_a_negative_claim`,
`test_real_db_absent_field_stays_absent`가 `"없"`이라는 글자가 근거문에 등장하지 않음을 검증).

**계약 위반 시 강제 지점**: `AutoProcessingEligibilityService`(345~352행)가
`product_fact_guard.sensitive == True` 이면서 `current_fact_verified == False`이면
`PRODUCT_FACT_NOT_VERIFIED`를 차단 사유로 추가합니다 — 자동등록이 막힙니다.

---

## 21. Brand / Manufacturer

`FIELD_TOPICS`에 `("제조사", "브랜드", "made in", "원산지", "제조국") → ("manufacturer", "brand", "country_of_origin")`
매핑이 있습니다. 세 필드를 **함께** 요청하는 점은 옳습니다.

**그러나 사양서 §25가 지적한 계약은 아직 없습니다.**

- `brand`(Naver listing 등록 브랜드 속성)와 `manufacturer`(제조 법인)의 **의미 차이를 코드가 구분하지 않습니다.**
  "삼성 제품인가요?"에 `brand` 값 하나로 답할 위험이 남아 있습니다.
- `오디세이` / `스마트모니터` / `무빙스타일` 같은 **product-line 질문을 인식하는 항목이 없습니다.**
  이 단어들은 `PRODUCT_FACT_TERMS`에도 `FIELD_TOPICS`에도 없어, 현재는 `NO_PRODUCT_FACT_TOPIC`으로
  조회를 건너뜁니다(오답은 아니지만 미답변).

**권장 처리 위치**: `services/product_knowledge_service.py`의 `fields_for_question()` 단계.
brand/manufacturer/country_of_origin 세 값이 모두 VERIFIED일 때만 제조사 질문에 근거를 제공하고,
하나라도 빠지면 근거를 주지 않는 규칙을 이 함수에 두는 것이 가장 안전합니다.
Phase 11-B 대상이며 이번에 구현하지 않았습니다.

---

## 22. Package / Subject 안전성

**부분적으로 구현되어 있고, 중요한 구멍이 하나 있습니다.**

구현된 부분 — `ACCESSORY_FIELD_PREFIX = "accessory_"`와 `BASE_DEVICE_SCOPE` / `ACCESSORY_SCOPE` 분리.
`FIELD_TOPICS`가 질문 유형별로 본체 필드와 액세서리 필드를 **따로** 나열합니다.

| 질문 | 본체 필드 | 액세서리 필드 |
|---|---|---|
| 베사/벽걸이 | `vesa_mm` | `accessory_vesa_mm` |
| 무게 | `weight_with_stand_kg`, `weight_without_stand_kg` | `accessory_package_weight_kg`, `accessory_max_load_kg` |
| 스탠드/거치대 | `stand_type`, `stand_detachable` | `accessory_materials`, `accessory_max_load_kg`, … |
| 높낮이/피벗 | (없음) | `accessory_height_adjustment_mm`, `accessory_pivot_degrees` |

`test_G_accessory_and_base_scope_are_labelled_separately`가 이 분리를 검증합니다.

**구멍**: `brand` / `manufacturer` / `country_of_origin`에는 **액세서리 대응 필드가 없습니다.**
따라서 "셋톱박스도 삼성 제품인가요?", "같이 오는 스탠드도 삼성인가요?" 같은 질문에서
**패키지 listing의 브랜드가 내부 구성품의 브랜드로 재사용될 수 있습니다.**

또한 질문의 subject(본체냐 구성품이냐)를 판별하는 로직은 `_explicit_accessory_vesa_scope()` 하나뿐이며
VESA 질문에만 적용됩니다. 일반화된 subject 판별기는 없습니다.

→ **Integration Risk에 HIGH로 기록합니다.**

---

## 23. 누락 / 미배송 문의 안전정책

**이미 구현되어 있고 Product Facts보다 상위에서 동작합니다.**

`answer/inquiry_analysis.py`(138~143행)가 `MISSING_ITEM_REPORT`를 `EMPTY_QUESTION`, `HIGH_RISK_OR_DISPUTE`와
같은 목록에 두어 `can_generate_answer = False`로 만듭니다. 결과적으로 `AutoAnswerProhibitedError`가 발생하고
`AutomaticDraftService`가 `POLICY_BLOCKED`를 반환합니다 — **초안 생성 자체가 일어나지 않습니다.**

판별은 `answer/text_utils.py:694`의 `is_missing_item_report()`,
호출은 `services/inquiry_analysis_service.py`(948행, 1146~1148행).
의미 분석 계층에도 `MISSING_ITEM_REPORT`와 `NOT_RECEIVED`가 정의되어 있습니다(`services/semantic_analysis.py`).

**Product Facts 연동 후에도 유지됩니다.** 차단이 Product Facts 조회보다 앞선 분석 단계에서 일어나므로,
`remote_control_included=true` 같은 fact가 있더라도 "리모컨이 안 왔어요"를 자동 처리할 수 없습니다.
Phase 11-B에서 이 순서를 바꾸지 않는 것이 필수 조건입니다.

---

## 24. Delisted / Dynamic Fact

**DYNAMIC은 막혀 있고, DELISTED는 막혀 있지 않습니다.**

- **DYNAMIC**: `UNUSABLE_VOLATILITY = {"DYNAMIC_LISTING_FACT"}`가 752건(ACTIVE의 19.3%)을 근거에서 제외합니다.
  `test_volatile_listing_fact_is_never_evidence`가 검증합니다. **문제 없음.**
- **DELISTED**: `listings.collection_status` 컬럼은 존재하고 `listing_for_product()`의 SELECT 목록에도
  포함되어 있으나, **코드 전체에서 그 값을 읽어 판단하는 곳이 한 군데도 없습니다.**
  (`grep -rn "collection_status"` 결과가 `product_fact_repository.py:93` SELECT 문 한 줄뿐)

현재 DB는 94건 전부 `COLLECTION_SUCCESS`라 문제가 드러나지 않지만,
사양서 §2가 밝힌 최신 DB는 **93 성공 / 1 판매종료**입니다.
최신 DB를 반입하는 순간 판매종료 listing의 과거 사실이 아무 제한 없이 근거로 쓰일 수 있습니다.

→ **Integration Risk에 BLOCKER로 기록합니다.** 필요한 gate:
`collection_status != COLLECTION_SUCCESS`이면 최소한 `DYNAMIC_LISTING_FACT`와
`SEMI_STATIC_POLICY_FACT`를 차단하고, `STATIC_PRODUCT_FACT`만 허용하거나 전체를 REVIEW로 보냅니다.

---

## 25. Retrieval Interface

**새로 만들 필요가 없습니다.** 현행 인터페이스가 사양서 §29의 요구를 대부분 충족합니다.

```python
ProductKnowledgeService.facts_for_inquiry(
    *, product_id, questions=None, question="", model_code=None
) -> ProductKnowledgeResult
```

`ProductKnowledgeResult`가 제공하는 것:

| 사양서 요구 개념 | 현행 대응 |
|---|---|
| `match_status` | `matched: bool` + `unavailable_reason` (`NO_PRODUCT_ID` / `NO_PRODUCT_FACT_TOPIC` / `PRODUCT_FACTS_DB_UNAVAILABLE` / `PRODUCT_NOT_IN_PRODUCT_DB` / `LOOKUP_FAILED:<타입>`) |
| `matched_listing_id` | `listing_id` |
| `verified_facts` | `safe_facts`, `safe_field_keys()`, `covers_all()` |
| `needs_review_fields` / `conflict_fields` | `excluded_facts`(각 fact가 `exclusion_reason` 보유) |
| `unknown_fields` | `requested_fields` − `safe_field_keys()` |
| `provenance_summary` | 각 `ProductFact.provenance` |
| prompt 주입 | `prompt_block()`, `evidence_text()`, `as_prompt_line()` |

**부족한 것 1가지**: `collection_status`가 결과에 실리지 않습니다. §24의 gate를 위해
`ProductKnowledgeResult`에 `collection_status` 필드를 추가하는 것이 Phase 11-B의 최소 변경입니다.
`listing_for_product()`가 이미 SELECT하고 있으므로 쿼리 변경은 필요 없습니다.

---

## 26. Retrieval Safety Gate

| 검사 | 현행 | 위치 |
|---|---|---|
| 상품 exact match인가? | ○ | `facts_for_inquiry` |
| ambiguous match인가? | 해당 없음(모델 매칭 미사용) | – |
| `collection_status`는? | **✕ 없음** | 신설 필요 |
| VERIFIED / NEEDS_REVIEW / CONFLICT | ○ | `_exclusion_reason` |
| subject 안전한가? | △ 필드 접두사 분리만, brand 미포함 | `FIELD_TOPICS` |
| STATIC/DYNAMIC 구분 | ○ | `UNUSABLE_VOLATILITY` |
| provenance 존재 | ○ | `_exclusion_reason` |

안전하지 않을 때의 귀결도 이미 존재합니다: 근거에서 제외 → `product_fact_guard.sensitive`가 참인데
검증된 사실이 없음 → `PRODUCT_FACT_NOT_VERIFIED` → 자동등록 차단 → 직원 검토.

---

## 27. Auto-post 영향

기존 enum·사유 코드를 우선 사용한 권장안입니다.

| 상황 | 권장 | 기존 코드 | 현행 동작 |
|---|---|---|---|
| A. exact match + VERIFIED + subject 명확 | **AUTO 가능** | `auto_post_allowed=True` | 이미 동작 |
| B. `NO_MATCH` | 상품스펙 질문이면 **REVIEW** | `PRODUCT_FACT_NOT_VERIFIED` | 이미 동작 |
| C. `AMBIGUOUS_MODEL` | **REVIEW** | 신설 필요 | 현재 발생하지 않음 |
| D. `NEEDS_REVIEW` | **REVIEW** | `RESOLUTION_NEEDS_REVIEW` → `PRODUCT_FACT_NOT_VERIFIED` | 이미 동작 |
| E. `CONFLICT` | **REVIEW** | `RESOLUTION_CONFLICT` → 동일 | 이미 동작 |
| F. subject ambiguous | **REVIEW** | **없음** | **미구현 — 위험** |
| G. delisted dynamic fact | **ANSWER BLOCK** | **없음** | **미구현 — 위험** |

A~E는 추가 작업이 필요 없습니다. **F와 G만 신설 대상**입니다.

---

## 28. Shadow Mode

**재사용 가능한 구조가 이미 있습니다.**

- `services/gpt_governance_service.py`: `GptMode.SHADOW`, `canary_selected(inquiry_id, percentage)`,
  `shadow_comparison` 기록 필드를 갖추고 있습니다.
- `services/gpt_semantic_analyzer_service.py`: shadow 실행 + `OJE_SEMANTIC_ANALYZER_ENABLED` 플래그(기본 0).

Product Facts는 이미 production에서 동작 중이므로 "최초 연동 shadow"는 해당되지 않습니다.
대신 **DB 교체 shadow**가 필요합니다: 최신 DB를 별도 경로에 두고 두 DB의 조회 결과를 비교하는 방식이며,
`ProductFactRepository(path)`가 경로를 인자로 받으므로 **production 코드 변경 없이 스크립트만으로 가능**합니다.
이번 조사에서 그 방식으로 실제 비교를 수행했습니다(§31 참조).

---

## 29. Diagnostic Export

`scripts/export_inquiry_diagnostics.py`의 `routing_section()`이 이미 기록합니다:
`selected_answer_route`, `answer_source`, `generation_mode`, `template_*`, `reason_code`,
`question_category`, `gpt_called`, `requires_manual_review`, 그리고
`product_fact_guard` 하위의 `classification`, `sensitive`, `current_fact_verified`,
`current_fact_source`, `auto_post_allowed`.

**추가로 기록해야 할 항목** (Phase 11-B, 이번에 수정하지 않음):

| 항목 | 현재 |
|---|---|
| product identifier / match method / matched listing | ✕ |
| `collection_status` | ✕ |
| requested Product Facts fields | ✕ |
| returned VERIFIED facts | ✕ |
| blocked NEEDS_REVIEW / CONFLICT (사유별) | ✕ |
| subject decision | ✕ |
| Product Facts used/not used, 최종 답변에 사용된 fact | 부분 (`current_fact_source`) |

`ProductKnowledgeResult.to_dict()`가 이미 존재하므로 **직렬화 코드를 새로 만들 필요는 없습니다.**

---

## 30. 성능

개발 PC에서 실제 측정했습니다(`ProductKnowledgeService.facts_for_inquiry`, 상품 2개 × 20회).

| 상품 | 평균 | 최대 |
|---|---|---|
| `12601323000` | 2.9 ms | 3.7 ms |
| `10198648691` | 3.1 ms | 3.9 ms |

| 항목 | 상태 |
|---|---|
| connection 방식 | `mode=ro` URI + `PRAGMA query_only=ON` + `busy_timeout=5000ms` |
| connection lifecycle | **쿼리마다 새 연결을 열고 닫음**. `facts_for_inquiry` 1회당 3회(listing / facts / provenance) |
| 인덱스 | `canonical_facts` 2개, `canonical_fact_listings` 1개, `canonical_fact_provenance` 2개 존재. `listings`는 autoindex만(94행이라 무관) |
| per-request 쿼리 | 답변 1건당 1회 조회. `answer_service.py:2822`는 metadata 재사용이라 중복 조회 아님 |
| caching 필요 | **불필요.** GPT 호출이 수 초 단위인 데 비해 3ms는 무시 가능 |

연결 재사용 최적화도 **권장하지 않습니다.** 매번 닫는 구조가 파일 교체(§32)를 안전하게 만들어 주는 이점이 더 큽니다.

단, 이 수치는 개발 PC의 구버전 DB 기준입니다. 최신 DB에서 재측정이 필요합니다.

---

## 31. Source of Truth

| 후보 | 안전성 | 업데이트 | 롤백 | 성능 | 복잡도 | DB 충돌 |
|---|---|---|---|---|---|---|
| 1. DB 파일 수동 복사 | 낮음(버전 추적 불가) | 쉬움 | 어려움 | 동일 | 낮음 | 있음 |
| 2. **versioned DB artifact** | **높음** | 보통 | **쉬움** | 동일 | 낮음 | 없음 |
| 3. canonical subset export | 높음 | 어려움 | 보통 | 더 빠름 | 높음 | 없음 |
| 4. Q&A Auto DB로 import | 낮음(쓰기 발생) | 어려움 | 어려움 | 동일 | 높음 | **큼** |
| 5. 별도 Product Facts service | 높음 | 보통 | 쉬움 | 느림(네트워크) | 매우 높음 | 없음 |

### 권장: **2. versioned DB artifact**

이유는 현행 코드가 이미 그 방식을 전제하고 있기 때문입니다.

- `OJE_PRODUCT_FACTS_DB_PATH` 환경변수로 경로를 바꿀 수 있습니다.
- `ProductFactRepository(path)`가 경로를 인자로 받습니다.
- 조회 때마다 연결을 새로 열므로 재시작 없이 파일을 교체할 수 있습니다.
- Q&A Auto DB(`oje_automation.db`)와 물리적으로 분리되어 있어 충돌이 없습니다.

구체안: 상품DB PC에서 `product_facts_<YYYYMMDD>_<sha256 앞 12자리>.db` 형태로 산출 →
개발/서버 PC의 `data/` 아래 배치 → `OJE_PRODUCT_FACTS_DB_PATH`로 지정.
해시를 파일명에 넣으면 §10에서 했던 것과 같은 방식으로 어느 버전이 도는지 언제든 확인할 수 있습니다.

---

## 32. 기존 `data/product_facts.db` 처리 방안

현재 파일은 **git이 추적하는 57 MB 바이너리**입니다(`.gitignore` 36행이 명시 예외).
이 상태로 버전마다 교체하면 저장소가 계속 커집니다.

권장 순서(이번 Phase에서는 실행하지 않음):

1. 현재 파일을 `data/archive/product_facts_20260825_cddf3082df82.db`로 **보존**(legacy 증거).
2. 최신 DB를 `data/product_facts_<날짜>_<해시12>.db`로 배치.
3. `.env`의 `OJE_PRODUCT_FACTS_DB_PATH`를 새 파일로 지정.
4. `data/product_facts.db` 자체는 **git 추적에서 제외**하고 `.gitignore` 36행의 예외를 제거.
   단, `@real_db` 테스트가 이 경로를 기본값으로 쓰므로(테스트는 파일이 없으면 skip) CI 정책을 함께 결정해야 합니다.
5. 교체 후 `@real_db` 테스트 재실행.

**교체 안전성 사전 검증을 이미 수행했습니다.** 상품DB의 현행 파일(Step 2C 적용본, `8fe643c5…`)을
READ-ONLY로 열어 `test_real_db_*`가 검사하는 조건을 동일하게 실행했습니다.

| 검사 | 개발 PC DB (구) | 상품DB 현행 (신) |
|---|---|---|
| `10198648691` matched / `hdmi_port_count` / `vesa_mm` | True / True / True | True / True / True |
| `10194603339`에 `hdmi_port_count` 부재 | 부재 | 부재 |
| 샘플 5개 상품 safe_facts 합계 | 38 | 38 |
| 불안전 fact 유출 | 0 | 0 |

**Step 2C 적용본으로 교체해도 기존 테스트는 깨지지 않습니다.**
다만 사양서 §2가 말한 *최종* DB(94 listings / 93 성공 / 1 판매종료)는 이 파일과도 다른 또 하나의 버전이므로,
그 파일이 반입되면 같은 검증을 다시 수행해야 합니다.

---

## 33. Rollback

**기존 수단으로 충분하며 새로 만들 것이 없습니다.**

| 수단 | 동작 | 효과 |
|---|---|---|
| `OJE_PRODUCT_FACTS_DB_PATH`를 이전 버전 파일로 변경 | 즉시 이전 DB 사용 | **1순위 롤백** |
| `OJE_PRODUCT_FACTS_DB_PATH`를 없는 경로로 지정 | `available()` False → `PRODUCT_FACTS_DB_UNAVAILABLE` | **Product Facts 완전 비활성화** |
| `data/product_facts.db` 파일 제거 | 위와 동일 | 동일 |

세 번째가 특히 중요합니다. `facts_for_inquiry`가 예외를 잡아
`unavailable_reason`만 남기고 정상 반환하므로(`test_missing_database_degrades_quietly`),
**DB가 사라져도 답변 파이프라인은 멈추지 않고 상품 근거만 없는 상태로 계속 동작**합니다.
연결을 매 쿼리마다 새로 열기 때문에 프로세스 재시작도 필요 없습니다.

`gpt_governance_service`의 SHADOW/canary는 GPT 전용이라 Product Facts 롤백에는 쓰지 않습니다.

---

## 34. 테스트 계획

### 재사용 가능한 기존 테스트

| 파일 | 커버 항목 |
|---|---|
| `tests/test_product_facts_b5.py` | VERIFIED / NEEDS_REVIEW / CONFLICT / SUPERSEDED / provenance 부재 / 다른 모델 provenance / accessory-base scope 분리 / DYNAMIC 차단 / 결측이 부정 사실이 되지 않음 / prompt 규칙 문장 / 무관 질문 / 미지 상품 / product_id 없음 / DB 부재 / 복합 질문 / 저장소 쓰기 거부 / 경로 설정 / 실제 DB 4종 |
| `tests/test_product_facts_e2e_b5.py` | 실제 DB 기반 end-to-end 14종 |
| `tests/test_product_fact_guard_excluded_and_shortcuts.py` | guard 분류·제외 |
| `tests/test_learning_authority_and_model_identity.py` | Learning authority, 모델 동일성 |
| `tests/test_auto_post_gate_server_cases.py`, `test_auto_post_policy_v7.py` | 자동등록 gate |
| `tests/test_missing_information_review_scope.py`, `test_pre_generation_gate.py` | 사전 차단 |
| `tests/test_export_inquiry_diagnostics.py` | 진단 export |

사양서 §38이 요구한 19개 항목 중 **Exact listing match, No match, VERIFIED, NEEDS_REVIEW, CONFLICT, UNKNOWN,
Package subject(필드 접두사 한정), Learning conflict, Template coexistence, DPS coexistence,
Missing delivery, GPT hallucination, Auto-post gate, Diagnostic export는 이미 커버**되어 있습니다.

### 신규로 필요한 테스트

| 대상 | 내용 |
|---|---|
| Delisted dynamic fact | `collection_status != COLLECTION_SUCCESS` listing의 DYNAMIC/SEMI_STATIC 사실이 근거로 나오지 않을 것 |
| STB / Stand subject | "셋톱박스도 삼성인가요?"에 패키지 본체 `brand`가 근거로 나오지 않을 것 |
| Brand vs Manufacturer | `brand`만 VERIFIED이고 `manufacturer`가 없으면 제조사 질문에 근거를 주지 않을 것 |
| Product-line 질문 | "오디세이 맞나요?"가 오답을 내지 않을 것(현재는 미답변 — 유지 확인) |
| Ambiguous model | 모델 매칭을 도입할 경우에만 필요 |
| DB 버전 교체 | 신규 artifact로 `@real_db` 전체 재실행 |
| Shadow 비교 | 두 DB 조회 결과 차이를 스크립트로 산출 |

---

## 35. CURRENT Architecture Map

실제 코드 기준입니다.

```
Naver API (api/qna.py, api/customer_inquiry.py, api/naver_read_client.py)
  │  상품문의: questionId, productId, productName
  │  고객문의: inquiryNo, orderId, productOrderId   ← productId 없음
  ▼
services/naver_inquiry_normalizer.py  NormalizedInquiry.to_work_item()
  ▼
services/inquiry_sync_service.py  →  inquiries 테이블
  │                                   (product_id 보유율 25.9%)
  ▼
services/auto_post_pipeline_service.py  run_pending()
  ├─ services/automatic_draft_service.py
  │    └─ services/answer_service.py
  │         ├─ 1215행  InquiryProcessingPlanService.create()
  │         │            → requires_order_lookup / requires_dps_lookup 확정
  │         ├─ 1236행  ProductKnowledgeService.facts_for_inquiry()   ★ plan 이후
  │         │            → repositories/product_fact_repository.py (mode=ro)
  │         │            → data/product_facts.db
  │         │            → request.metadata["product_knowledge"]
  │         ├─ 1314행  주문 조회 / 1327행 DPS 조회 (plan에 따라)
  │         ├─ 1673행  classify_product_fact()  → product_fact_guard
  │         ├─ 라우팅  TEMPLATE / PRODUCT_DB / 학습 / GPT
  │         │    └─ services/hybrid_answer_service.py
  │         │         ├─ _product_facts_context()  → prompt의 product_facts 블록
  │         │         ├─ learning_context + VERIFIED_PRODUCT_FACT
  │         │         └─ services/learning_evidence_policy.py
  │         │              → PRODUCT_FACT_VS_LEARNING_CONFLICT 시 Learning 배제
  │         ├─ answer/answer_validator.py
  │         └─ 2811~2936행  product_fact_guard 최종 판정
  │                          → metadata["product_fact_guard"]
  ├─ services/auto_processing_eligibility_service.py
  │    └─ 345~352행  sensitive && !current_fact_verified
  │                   → PRODUCT_FACT_NOT_VERIFIED (자동등록 차단)
  └─ services/naver_post_service.py  → Naver 등록 / 또는 직원 검토 보류
                                        ui/answer_status_presenter.py
```

사전 차단(위 흐름에 진입하지 못함): `MISSING_ITEM_REPORT`, `NOT_RECEIVED`, `EMPTY_QUESTION`,
`HIGH_RISK_OR_DISPUTE` → `AutoAnswerProhibitedError` → `POLICY_BLOCKED`.

---

## 36. PROPOSED Architecture Map

**구조 변경은 제안하지 않습니다.** 현행 배치가 이미 옳습니다. 추가되는 것은 gate 3개와 진단 항목뿐입니다.
아래에서 `★`가 신규입니다.

```
inquiries.product_id
  │   (없으면) ★ raw_json에서 product_id 복구 — 별도 backfill, 매칭 규칙은 그대로 exact only
  ▼
ProductKnowledgeService.facts_for_inquiry(product_id, questions, model_code)
  │
  ├─ exact match 실패 → NO_PRODUCT_ID / PRODUCT_NOT_IN_PRODUCT_DB → 근거 없음 (기존)
  │
  ├─ ★ Gate 1: collection_status
  │      COLLECTION_SUCCESS 아니면 DYNAMIC_LISTING_FACT·SEMI_STATIC_POLICY_FACT 차단
  │      → 사유 코드 DELISTED_LISTING_FACT
  │
  ├─ Gate 2: _exclusion_reason (기존 9개 조건 — 변경 없음)
  │
  ├─ ★ Gate 3: subject
  │      질문이 구성품/부속품을 가리키면 본체 brand·manufacturer·country_of_origin 차단
  │      → 사유 코드 SUBJECT_SCOPE_UNRESOLVED
  │
  └─ ★ Gate 4: brand ≠ manufacturer
         제조사 질문은 세 필드가 모두 VERIFIED일 때만 근거 제공

  ▼
ProductKnowledgeResult (★ collection_status 필드 추가)
  ▼
HybridAnswerService._product_facts_context()   (변경 없음)
  ▼
prompt structured evidence block               (변경 없음)
  ▼
AnswerValidator                                (변경 없음)
  ▼
AutoProcessingEligibilityService               (변경 없음 — 기존 PRODUCT_FACT_NOT_VERIFIED가 그대로 동작)
  ▼
★ export_inquiry_diagnostics: match method / collection_status / requested fields /
   returned VERIFIED / blocked 사유별 / subject decision 기록
```

---

## 37. 파일 단위 Integration Plan

| 파일 | 현재 역할 | 예상 변경 | 변경 이유 | 위험도 |
|---|---|---|---|---|
| `services/product_knowledge_service.py` | 안전 판정 단일 지점 | `collection_status` gate, subject gate, brand/manufacturer 규칙, `ProductKnowledgeResult`에 필드 추가 | §24 BLOCKER, §22 HIGH, §21 MEDIUM 해소 | **MEDIUM** |
| `repositories/product_fact_repository.py` | 읽기 전용 접근 | `listing_for_product` 반환에 `collection_status` 노출(이미 SELECT 중) | gate 입력 제공 | **LOW** |
| `scripts/export_inquiry_diagnostics.py` | 단건 진단 | Product Facts 상세 항목 추가 | §29 | **LOW** |
| `tests/test_product_facts_b5.py` | 안전계약 테스트 | 신규 케이스 추가(기존 기대값 변경 없음) | §34 | **LOW** |
| `tests/test_product_facts_e2e_b5.py` | e2e | 신규 artifact 기준 재검증 | §32 | **LOW** |
| `.env` / `.env.example` | 설정 | `OJE_PRODUCT_FACTS_DB_PATH` 실제 지정 | §31 | **LOW** |
| `data/` 및 `.gitignore` | DB 배치 | versioned artifact 배치, git 추적 정책 결정 | §32 | **MEDIUM** |
| `services/answer_service.py` | 파이프라인 본체 | **변경 불필요** | 조회 지점·순서가 이미 올바름 | – |
| `services/hybrid_answer_service.py` | prompt/evidence | **변경 불필요** | – | – |
| `services/auto_processing_eligibility_service.py` | 자동등록 gate | **변경 불필요** | 기존 사유 코드로 충분 | – |
| `services/learning_evidence_policy.py` | Learning 충돌 | **변경 불필요** | 이미 Product Facts 우위 | – |
| `answer/prompt_builder.py` | prompt | **변경 불필요** | – | – |

HIGH 위험도로 분류한 파일은 없습니다. 변경이 `product_knowledge_service.py` 한 곳에 모이기 때문입니다.

---

## 38. Integration Risk

### BLOCKER

**R1. `collection_status` gate 부재 (§24)**
`listings.collection_status`를 읽어 판단하는 코드가 없습니다. 현재 DB는 94건 전부 성공이라 드러나지 않지만,
사양서가 밝힌 최신 DB에는 판매종료 1건이 포함됩니다. 최신 DB를 반입하면
판매종료 상품의 과거 사실이 무제한으로 근거가 됩니다.
**최신 DB 반입 전에 반드시 해결해야 합니다.**

### HIGH

**R2. 패키지 subject 미구분 (§22)**
`brand` / `manufacturer` / `country_of_origin`에 액세서리 대응 필드가 없어,
"셋톱박스도 삼성인가요?"에 패키지 본체 브랜드가 답변 근거가 될 수 있습니다.
일반화된 subject 판별기가 없고 `_explicit_accessory_vesa_scope()`는 VESA 질문에만 적용됩니다.

**R3. 운영 답변이 구버전 DB를 근거로 생성 중 (§10)**
현재 `data/product_facts.db`는 Step 2C 이전 스냅샷임이 해시로 확인되었습니다.
답변 품질 문제라기보다 "어느 버전이 도는지 관리되지 않는다"는 운영 문제입니다.
버전 표기가 파일명·로그 어디에도 없습니다.

### MEDIUM

**R4. brand ≠ manufacturer 계약 부재 (§21)**
두 값의 의미 차이를 코드가 구분하지 않습니다.

**R5. `product_id` 커버리지 25.9% (§6)**
고객문의 신규 API 442건은 상품 식별자가 원천적으로 없어 해결 불가.
구형 경로 1,613건은 `raw_json`에서 복구 가능하나 아직 하지 않았습니다.
현재는 안전하게 실패하므로(근거 없음) 오답 위험은 아니고 **커버리지 손실**입니다.

**R6. 답변 커버리지 30.5% (실측)**
Product Facts에 존재하는 59개 상품 × 8개 대표 질문 = 472건 중 **144건(30.5%)**만 안전한 사실을 제공했습니다.
질문군별: 크기 35 / 인치 35 / 해상도 24 / HDMI 21 / USB 18 / 무게 5 / VESA 3 / 스탠드 3.
HDMI·USB는 사실이 있어도 대부분 제외됩니다(각각 36건·37건이 "제외만"). 이는 DB의 `NEEDS_REVIEW` 다수와 일치합니다.
최신 DB에서 개선되었을 수 있으므로 반입 후 재측정이 필요합니다.

**R7. `data/product_facts.db`가 git 추적 대상 (§32)**
57 MB 바이너리를 버전마다 커밋하면 저장소가 급격히 커집니다.

### LOW

**R8. "리모컨 포함인가요?" 미대응 (§15)** — `FIELD_TOPICS`에 항목이 없어 미답변. 오답은 아님.
**R9. product-line 질문 미대응 (§21)** — 오디세이/스마트모니터/무빙스타일 인식 항목 없음. 미답변.
**R10. `ai/answer_service.py`·`ai/prompt_builder.py` 용도 불명 (§9)** — production 사용 여부 미확정.

### FUTURE

**R11. 모델 단위 매칭** — 현재 불필요하나 도입 시 `AMBIGUOUS_MODEL` 상태와 8종 다중 listing 처리가 필요.
**R12. Product Facts 서비스 분리** — 상품 수가 크게 늘 때만 검토.

---

## 39. Phase 11-B 권장 범위

전체 답변 엔진에 새로 연결할 것이 없으므로(이미 연결됨), **가장 작은 안전 단위**는 다음과 같습니다.

### 포함

1. **`collection_status` gate 구현** (R1 BLOCKER) — `product_knowledge_service.py` 한 곳,
   `ProductKnowledgeResult`에 `collection_status` 노출, 신규 사유 코드 `DELISTED_LISTING_FACT`.
2. **subject gate + brand/manufacturer 계약** (R2 HIGH, R4 MEDIUM) — 같은 파일.
3. 위 3개에 대한 **단위 테스트 신규 추가** (기존 기대값 변경 없음).
4. **최신 Product Facts artifact 반입** — versioned 파일명 + 해시, `OJE_PRODUCT_FACTS_DB_PATH` 지정,
   기존 파일은 `data/archive/`로 보존.
5. **반입 전후 shadow 비교 스크립트** — 두 DB 조회 결과 차이를 산출(production 코드 변경 없음).
6. 반입 후 **커버리지 재측정**(R6) 및 `@real_db` 테스트 재실행.

### 제외 (Phase 11-B에서 하지 않음)

- GPT prompt 수정
- auto-post 로직 수정 (기존 `PRODUCT_FACT_NOT_VERIFIED`로 충분)
- DPS 관련 일체
- `answer_service.py` / `hybrid_answer_service.py` 수정
- 모델 단위 매칭 도입
- `product_id` backfill (R5) — 별도 Phase로 분리. 오답 위험이 없고 범위가 크게 다름
- `.gitignore` / git 추적 정책 변경 (R7) — 사용자 결정 필요

이 범위는 **고객 답변 경로의 동작을 바꾸지 않으면서**(gate는 근거를 줄이는 방향으로만 작동)
BLOCKER를 제거하고 최신 데이터를 반입합니다.

---

## 40. 이번 작업에서 변경된 파일

작업 종료 시점 `git status --porcelain` 결과:

```
?? docs/phase11a_product_facts_integration_audit.md
```

| 항목 | 건수 |
|---|---|
| 신규 생성 | **1** (이 보고서) |
| production 코드 변경 | **0** |
| 테스트 파일 변경 | **0** |
| DB 변경 (INSERT/UPDATE/DELETE/schema/migration) | **0** |
| `.env` 변경 | **0** |
| Product Facts DB 복사·교체 | **0** |
| git commit / push / reset / checkout / clean / stash | **0** |
| Naver 등록 / DPS 조회 / GPT 호출 / Chrome 자동화 / Kakao 발송 | **0** |

작업 시작 시점에 사용자가 수정해 둔 파일은 없었으므로(§2), 위 1건이 이번 조사가 만든 전부입니다.

모든 DB 접근은 `mode=ro` URI + `PRAGMA query_only=ON`으로 수행했고,
상품DB 프로젝트 파일도 읽기 전용으로만 열었습니다.

---

## 41. 최종 판정

# PHASE 11-A READY — Product Facts 연동 구조 설계 완료

현재 Q&A Auto 구조를 충분히 파악했고 다음 구현 단계를 안전하게 설계할 수 있습니다.

판정 근거:

- 실제 문의 처리 흐름을 코드 순서까지 확정했습니다(§35).
- 상품 식별자의 출처·저장·소실 지점을 실측으로 확정했습니다(§6).
- Exact join 가능성을 코드와 값 양쪽으로 확인했습니다 — **DIRECT_EXACT_JOIN_POSSIBLE**, 실측 59/79 일치, 형식 불일치 0건(§7).
- 개발 PC DB의 정체를 해시로 확정했습니다(§10).
- 안전 계약·Learning 충돌·자동등록 gate가 이미 구현되어 있음을 확인했습니다(§20, §13, §27).
- 남은 위험을 BLOCKER 1건 / HIGH 2건 / MEDIUM 4건 / LOW 3건으로 특정했고, 모든 BLOCKER·HIGH의
  해결 위치가 `services/product_knowledge_service.py` 한 파일로 모입니다(§37, §38).
- 테스트 baseline 3,494 passed / 실패 0을 이 PC에서 직접 측정했습니다(§3).

단, **BLOCKER R1(`collection_status` gate)을 해결하기 전에 최신 Product Facts DB를 반입해서는 안 됩니다.**
판매종료 listing이 포함된 순간 그 상품의 과거 사실이 제한 없이 답변 근거가 되기 때문입니다.
