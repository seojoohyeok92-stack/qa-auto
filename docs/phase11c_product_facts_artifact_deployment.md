# Phase 11-C — 최종 Product Facts Artifact 확정 및 개발 PC 안전 반입

작업일: 2026-08-29 · **DB 교체 없음** · Git commit/push 없음

> **결론을 먼저 적습니다.**
> **FINAL_ARTIFACT_NOT_CONFIRMED**
> §2가 지적한 불일치를 조사한 결과, 승인된 Final Quality Gate가 기술하는 상태
> (`listings 94 / COLLECTION_SUCCESS 93 / 판매종료·수집실패 1`)를 가진 artifact가
> **이 PC 어디에도 존재하지 않습니다.** 후보 4개 전부 `94 / 94 / 0`입니다.
> §11·§35에 따라 `data/product_facts.db`를 교체하지 않고 중단했습니다.

---

## 1. 시작 상태

| 항목 | 값 |
|---|---|
| branch | `main` |
| HEAD | `3950e8b` |
| Phase 11-A/11-B 변경 (미commit) | `M repositories/product_fact_repository.py`, `M scripts/export_inquiry_diagnostics.py`, `M services/product_knowledge_service.py`, `?? docs/phase11a_…md`, `?? docs/phase11b_…md`, `?? tests/test_product_facts_safety_gate_11b.py` |
| 그 외 사용자 변경 | 없음 |

**Rollback 기준 — 개발 PC 현행 DB**

| 항목 | 값 |
|---|---|
| 경로 | `data/product_facts.db` |
| 크기 | 60,170,240 bytes |
| mtime | 2026-08-25 12:30:32 |
| SHA-256 | `cddf3082df82d87065a452ee8140af9f42c4d0b31e91753597717f55cc82ac4c` |

이 파일은 이번 Phase에서 **변경되지 않았습니다**(§16에서 재확인).

---

## 2. Artifact 후보

`<홈>\Desktop` / `Downloads` / `Documents` 전체에서 `*product_facts*` 를 검색했습니다.
Product Facts DB는 아래 5개(개발 PC 사본 포함)가 전부이며, 다른 위치에는 없습니다.

| # | 경로 | 크기 | mtime | SHA-256 (앞 16) |
|---|---|---|---|---|
| A | `상품DB/data/product_facts.db` | 60,170,240 | 2026-08-29 03:32:48 | `8fe643c55807ca86` |
| B | `상품DB/data/archive/product_facts_before_phase10_step2c_20260828T183217Z.db` | 60,170,240 | 2026-08-29 03:32:17 | `cddf3082df82d870` |
| C | `상품DB/data/archive/product_facts_before_final_correction_20260825T113746.db` | 58,957,824 | 2026-08-25 12:19:48 | `9eaef04773e1a641` |
| D | `상품DB/data/archive/product_facts_before_image_completion.db` | 45,633,536 | 2026-08-25 12:19:48 | `6112930e96e2e663` |
| E | `qa-auto/data/product_facts.db` (개발 PC 현행) | 60,170,240 | 2026-08-25 12:30:32 | `cddf3082df82d870` |

`상품DB/data/batch_collector.db`(2,203,648 bytes)는 `listings` 테이블이 없는 수집 작업용 DB라 후보에서 제외했습니다.
**E와 B는 SHA-256이 동일**합니다 — 개발 PC 사본이 Step 2C 직전 스냅샷이라는 Phase 11-A의 판정이 재확인되었습니다.

### 후보별 내부 상태

| 항목 | A (상품DB 현행) | B / E | C | D |
|---|---|---|---|---|
| listings | 94 | 94 | 94 | 94 |
| **collection_status** | **SUCCESS 94** | **SUCCESS 94** | **SUCCESS 94** | **SUCCESS 94** |
| canonical_facts | 6,331 | 6,331 | 6,306 | 3,984 |
| ACTIVE | 3,894 | 3,894 | 3,869 | 3,976 |
| VERIFIED | 3,547 | 3,546 | 3,511 | 2,460 |
| NEEDS_REVIEW (verification) | 347 | 348 | 358 | 1,516 |
| CONFLICT (resolution) | 29 | 30 | 31 | 0 |
| NEEDS_REVIEW (resolution) | 318 | 318 | 327 | 1,516 |
| **`13074225226`** | **SUCCESS** | **SUCCESS** | **SUCCESS** | **SUCCESS** |

---

## 3. Final Gate 대조

§7이 제시한 승인 상태의 특성을 하나씩 대조했습니다.

| Final Gate 특성 | 확인 결과 | 일치 |
|---|---|---|
| listings 94 | 후보 4개 모두 94 | ○ |
| **COLLECTION_SUCCESS 93** | 후보 4개 모두 **94** | **✕** |
| **판매종료/수집실패 1** | 후보 4개 모두 **0** | **✕** |
| **`13074225226` 판매종료 상태** | 후보 4개 모두 `COLLECTION_SUCCESS` | **✕** |
| Phase 10 Final Quality Gate 통과 | 상품DB에 Final Gate 산출물이 **존재하지 않음** | **✕** |
| Product Facts 최종 테스트 1045/1045 | 상품DB 테스트 수집 결과 **74개** | **✕** |
| Q&A Final Gate 306개 질문 | 존재하는 Q&A 검증 로그는 **200개 질문**(Step 2C) 하나뿐 | **✕** |
| Q&A WRONG 0 / UNSUPPORTED 0 | Step 2C 200문항 기준으로는 충족(0/0) | △ (다른 검증) |
| provenance integrity 이상 0 | Step 2C 적용 후 확인됨 | △ (다른 검증) |

### 판매종료가 다른 방식으로 기록됐을 가능성도 배제했습니다

`listings.collection_status` 외의 경로를 모두 확인했습니다.

- **fact 값**: 최신 후보 A에서 `availability` = `"IN_STOCK"` **94건 전부**, `product_status` = `"SALE"` **94건 전부**.
  `종료` / `품절` / `중지` / `SOLD` 를 포함하는 값은 **한 건도 없습니다.**
- **`13074225226` 상세**: `collection_status=COLLECTION_SUCCESS`,
  `availability=IN_STOCK`(VERIFIED), `product_status=SALE`(VERIFIED),
  `raw_documents` 25건 전부 `COLLECTION_SUCCESS`,
  ACTIVE fact 41건(STATIC 24 / SEMI_STATIC 9 / DYNAMIC 8).
- **로그**: `COLLECTION_FAILED` 기록은 `logs/collection_20260824T0835*.json`과
  `logs/browser_probe_11844406044.json`에 있지만, 이는 2026-08-24의 **일시적 수집 실패**입니다
  (HTTP 429 rate limit, http_status null). 대상은 `11844406044`, `12139453925`, `12601323000` 등이며
  이후 재수집되어 최종 `listings`에는 전부 `COLLECTION_SUCCESS`로 남아 있습니다.
  `13074225226`은 이 실패 목록에도 없습니다.

### 상품DB 프로젝트의 실제 최신 상태

`phase10` / `final_gate` / `quality_gate` 이름을 가진 파일을 전수 검색한 결과,
존재하는 것은 **Phase 10 Step 2C 산출물뿐**입니다.

```
logs/phase10_step2c_qa_validation.json
logs/phase10_step2c_qa_validation_before.json
phase10_step2c_qa_validation.py
phase10_step2c_report.py
reports/phase10_step2c_normalization_quality.xlsx
tests/test_phase10_step2c_normalization.py
data/archive/product_facts_before_phase10_step2c_20260828T183217Z.db
```

즉 이 PC의 상품DB 프로젝트는 **Phase 10 Step 2C까지** 진행된 상태이고,
그 이후의 Final Quality Gate는 **이 PC에서 수행된 적이 없습니다.**

---

## 4. Excel 대조

§8이 지정한 `reports/product_facts_final_catalog.xlsx`는 **존재하지 않습니다.**
`01_상품목록` / `09_상품별Coverage` / `10_README` 시트를 가진 파일도 없습니다.

접근 가능한 최종 성격의 Excel은 `상품DB/exports/final/product_database_94.xlsx`
(3,101,781 bytes, 2026-08-25 12:35:09)이며, 시트 구성이 다릅니다.

| 시트 | 행 수 |
|---|---|
| `products` | 95 (헤더 포함, 데이터 94) |
| `facts` | 4,013 |
| `provenance` | 6,848 |
| `needs_review` | 386 |
| `collection_summary` | 95 |
| `field_coverage` / `product_coverage` / `coverage_audit` / `quality_audit` 등 | – |

이 Excel의 `collection_status` 컬럼은 §7이 말한 어휘를 쓰지 않습니다.

```
products / collection_summary → IMAGE_REVIEW_PENDING 89, COMPLETE 5
```

이는 listing 수집 상태가 아니라 **이미지 검토 완료 상태**를 담는 다른 축입니다.
따라서 이 Excel도 `93 success + 1 판매종료`를 뒷받침하지 않으며,
mtime(2026-08-25)으로 보아 Step 2C 이전 산출물입니다.

**Excel을 DB 대신 사용하지 않았습니다.** 대조 목적으로만 읽었습니다.

---

## 5. `13074225226` 상태

| 후보 | collection_status | availability | product_status |
|---|---|---|---|
| A (상품DB 현행) | `COLLECTION_SUCCESS` | `IN_STOCK` (VERIFIED) | `SALE` (VERIFIED) |
| B / E | `COLLECTION_SUCCESS` | – | – |
| C | `COLLECTION_SUCCESS` | – | – |
| D | `COLLECTION_SUCCESS` | – | – |

상품명: `삼성전자 LS27FM501E-2MO 삼성 무빙스타일 M5 스마트 모니터 IPTV 이동식 거치대 삼탠바이미 M50F 68.6cm(27인치), 웜 화이트`
`collection_run_id` / `run_id` 모두 `20260825T001749Z_batch_capture`.

§9는 "Final Quality Gate에서 승인한 artifact라면 이 상품이 정상수집 상태로만 남아 있어서는 안 된다"고 했습니다.
**네 후보 모두 정상수집 상태로만 남아 있으므로, 넷 중 어느 것도 그 artifact가 아닙니다.**

---

## 6. Final Artifact 판정

# FINAL_ARTIFACT_NOT_CONFIRMED

§10의 확정 규칙(파일명·mtime만으로 고르지 말 것, Final Gate 특성 + Excel + DB 내부 상태가 모두 일치할 것)을 적용한 결과입니다.

후보별 탈락 사유:

| 후보 | 탈락 사유 |
|---|---|
| A (상품DB 현행, `8fe643c5`) | `COLLECTION_SUCCESS 94`, `13074225226` 정상수집. Phase 10 **Step 2C** 결과물이며 Final Gate 산출물이 없음 |
| B (Step 2C 직전, `cddf3082`) | 위와 동일 + Step 2C 이전 상태. 개발 PC 사본과 동일 |
| C (final_correction 직전, `9eaef047`) | `COLLECTION_SUCCESS 94`. CONFLICT 31로 A보다 오래된 상태 |
| D (image_completion 직전, `6112930e`) | `COLLECTION_SUCCESS 94`. NEEDS_REVIEW 1,516으로 품질 정리 이전 상태 |

**"가장 비슷한 DB"를 임의로 선택하지 않았습니다**(§11).
A가 가장 최신이고 품질 지표도 가장 좋지만, Final Gate 특성 3개(93/1, 판매종료 상품, Final Gate 산출물)를
모두 만족하지 못하므로 확정 대상이 아닙니다.

---

## 7~15. 반입 관련 절차 — 전부 수행하지 않음

§11·§35에 따라 아래 단계는 **의도적으로 수행하지 않았습니다.**

| 절 | 항목 | 상태 |
|---|---|---|
| §12 | Backup 생성 | **미수행** (확정 실패로 교체 자체가 없음) |
| §13 | Versioned artifact 생성 | **미수행** |
| §14 | 원본/사본 SHA-256 대조 | **미수행** |
| §15 | Versioned artifact READ-ONLY 검증 | **미수행** |
| §16 | 전체 무결성 검사 | **미수행** |
| §17 | Exact Join Coverage 재측정 | **미수행** |
| §18 | `13074225226` R1 실물 검증 | **수행 불가** — 판매종료 상태를 가진 artifact가 없음 |
| §19 | R2 실물 검증 | **미수행** (Phase 11-B에서 이미 실제 DB로 검증 완료) |
| §20 | R4 실물 검증 | **미수행** (Phase 11-B에서 이미 실제 DB로 검증 완료) |
| §21 | Q&A Retrieval Matrix | **미수행** |
| §22~§25 | 운영 경로 교체 | **미수행** |
| §26 | `@real_db` 재실행 | **미수행** (DB 미교체이므로 baseline과 동일) |
| §32 | Rollback 실증 | **불필요** (교체가 없어 rollback 대상이 없음) |

`data/product_facts.db`는 **손대지 않았습니다.** `.env`도 변경하지 않았습니다.

---

## 16. DB 무변경 검증

| 파일 | 시작 SHA-256 | 종료 SHA-256 | 결과 |
|---|---|---|---|
| `qa-auto/data/product_facts.db` | `cddf3082…ac82ac4c` | `cddf3082…ac82ac4c` | **불변** |
| `상품DB/data/product_facts.db` | `8fe643c5…4e563f93` | `8fe643c5…4e563f93` | **불변** |

archive 3개 파일도 읽기 전용으로만 열었습니다.
모든 DB 접근은 `mode=ro` URI + `PRAGMA query_only = ON`을 사용했습니다.

---

## 17. 전체 테스트 — 신규 실패 4건 발견 (원인: 테스트의 날짜 의존성)

| 시점 | passed | failed | 소요 |
|---|---|---|---|
| Phase 11-B 종료 (KST 08-29 05:2x, **UTC 08-28**) | 3,526 | 0 | 17분 48초 |
| **Phase 11-C 시작 (KST 08-29 11:5x, UTC 08-29)** | **3,522** | **4** | 18분 57초 |

합계는 3,526으로 동일하며, 4건이 pass → fail로 바뀌었습니다.

```
FAILED tests/test_atomic_answer_completeness.py::test_case_g_order_number_still_uses_dps
FAILED tests/test_atomic_draft_composition.py::test_a_clean_single_question_still_auto_posts
FAILED tests/test_delivery_pipeline_e2e_dps.py::test_confirmed_date_reaches_the_draft_and_clears_eligibility
FAILED tests/test_golden_auto_post_core_e2e.py::test_gs02_body_order_number_reaches_order_lookup_and_dps
```

증상은 모두 동일합니다: `eligibility`가 `SAFE`여야 하는데 `REVIEW_REQUIRED`가 되고,
자동등록이 `succeeded_count=0 / skipped_count=1`이 됩니다.

### 원인

**Phase 11-A/11-B의 코드 변경과 무관한, 기존 테스트의 wall-clock 의존성입니다.**

네 테스트 모두 DPS 설치/배송 예정일을 `2026-08-28`로 **하드코딩**합니다.

| 파일 | 위치 |
|---|---|
| `test_atomic_answer_completeness.py` | 279–280행 `installation_date` |
| `test_atomic_draft_composition.py` | 427–428행 `installation_date` |
| `test_delivery_pipeline_e2e_dps.py` | 184·222·372·394행 |
| `test_golden_auto_post_core_e2e.py` | 106행 `RecordingDps(date="2026-08-28")` |

`dps/dates.py`의 `is_schedule_stale()`은 예정일을 문의의 `created_at`과 비교합니다.
테스트가 삽입하는 문의의 `created_at`은 DB 기본값 `strftime('%Y-%m-%dT%H:%M:%fZ','now')` — **UTC 기준 현재시각**입니다.

실측:

```
현재 UTC 2026-08-29T03:33:19  (KST 12:33)
is_schedule_stale("2026-08-28", created_at="2026-08-27T20:00:00Z") -> False
is_schedule_stale("2026-08-28", created_at="2026-08-28T20:00:00Z") -> False
is_schedule_stale("2026-08-28", created_at="2026-08-29T03:00:00Z") -> True
```

즉 **UTC 날짜가 2026-08-29로 넘어가는 순간(KST 08-29 09:00)** 하드코딩된 예정일이 과거가 되어
`STALE_DPS_SCHEDULE` 판정을 받고, 확정 답변을 만들 수 없어 `REVIEW_REQUIRED`가 됩니다.

Phase 11-B의 전체 테스트는 KST 05:2x(**UTC 2026-08-28**)에 끝나 통과했고,
Phase 11-C의 실행은 KST 11:5x(**UTC 2026-08-29**)에 시작해 실패했습니다.
경계를 넘은 것 외에 달라진 것이 없습니다.

### 제 변경이 원인이 아님을 실증했습니다

`OJE_PRODUCT_FACTS_DB_PATH`를 존재하지 않는 경로로 지정해
**Product Facts를 완전히 비활성화한 상태**에서 같은 4건을 실행했습니다
(`.env`는 수정하지 않고 해당 프로세스 환경변수로만).

```
4 failed in 7.96s  — 동일한 4건이 동일하게 실패
```

Product Facts가 아예 조회되지 않는 상태에서도 같은 실패가 재현되므로,
Phase 11-A/11-B가 추가한 R1·R2·R4 gate는 이 실패의 원인이 아닙니다.

### 성격과 권고

이것은 **일회성 flaky가 아니라 오늘부터 매일 실패하는 회귀**입니다.
`2026-08-28`을 쓰는 테스트 파일은 6개이며, 그중 4개가 "예정일이 아직 지나지 않았다"를
전제로 한 결과를 단언하고 있습니다.

| 파일 | 현재 |
|---|---|
| `test_atomic_answer_completeness.py` | **실패** |
| `test_atomic_draft_composition.py` | **실패** |
| `test_delivery_pipeline_e2e_dps.py` | **실패** |
| `test_golden_auto_post_core_e2e.py` | **실패** |
| `test_dps_multi_item_max_date_acceptance.py` | 통과 (해당 단언 없음) |
| `test_semantic_coverage_soft_gate.py` | 통과 (해당 단언 없음) |

**이번 Phase에서는 고치지 않았습니다.** 이 Phase의 범위는 artifact 확정이고,
§35에 따라 이미 중단 상태이며, 무엇보다 기대값을 임의로 바꾸면
"예정일이 지나면 확정 답변을 막는다"는 **올바른 안전 동작**을 가릴 수 있기 때문입니다.

권고하는 수정 방향(별도 승인 필요):
날짜를 고정 문자열로 두지 말고 문의 등록일 기준 상대값(예: 오늘 또는 오늘+N일)으로 만들어,
"예정일이 아직 오지 않았다"는 조건 자체를 테스트가 보장하도록 바꾸는 것입니다.
기대값(`SAFE`)은 그대로 두고 입력만 시간 독립적으로 만드는 방식입니다.

---

## 18. 참고 — 두 60MB DB의 실제 차이 (의사결정 자료)

**이것은 artifact 선택 근거가 아닙니다.** 다음 단계를 판단하는 데 필요한 사실만 적습니다.

개발 PC 현행(`cddf3082`)과 상품DB 현행(`8fe643c5`)을 상품 94개 × 질문 15개 = 1,410건으로 비교했습니다.

| 항목 | 값 |
|---|---|
| 결과 동일 | **1,409** |
| 달라진 질문 | **1** |
| safe fact 총량 | 724 → 725 |

유일한 차이는 `12143215609 / "모델명이 뭔가요?"`가 `model_name`을 답할 수 있게 되는 것입니다
(Step 2C의 공백 정규화로 `MODEL:LH85BEFHLGFXKR`의 CONFLICT가 해소된 결과).

즉 상품DB 현행 DB를 반입하더라도 **고객 답변이 달라지는 경우는 94개 상품 전체에서 1건**입니다.
반입을 서두를 실익이 없으며, Final Gate artifact를 제대로 확정한 뒤 진행해도 손실이 거의 없습니다.

Phase 11-B에서 이미 확인한 사항도 함께 적어 둡니다.

- 상품DB 현행 DB의 schema는 `ProductFactRepository` / `ProductKnowledgeService`가 그대로 읽습니다.
- `@real_db` 테스트가 검사하는 조건이 두 DB에서 동일한 결과를 냅니다.

---

## 19. Source of Truth 기록 (§33)

| 역할 | 대상 | 현재 상태 |
|---|---|---|
| **Master** | Product Facts 전용 PC의 Final Quality Gate 통과 DB | **소재 불명.** 이 PC에 없음 |
| **Development** | Master의 byte-identical versioned copy | **미확보.** 현재 `data/product_facts.db`는 Step 2C 직전 스냅샷(`cddf3082`) |
| **Production** | 서버 PC 배포용 동일 artifact | 미배포 |

향후 관리 순서는 다음과 같이 유지합니다.

```
상품DB PC → Quality Gate → SHA-256 확정 → versioned artifact
         → 개발 PC 검증 → 서버 배포
```

개발 PC에서 Product Facts DB를 직접 편집하지 않습니다. 이번 Phase에서도 편집하지 않았습니다.

**추가로 기록해야 할 사실**: 이 PC에는 Q&A Auto와 상품DB 두 프로젝트가 모두 있습니다
(`<홈>\Desktop\상품DB`). Phase 11-A부터 계속 보고한 내용이며,
"Product Facts 전용 PC"와 "개발 PC"가 물리적으로 분리되어 있다는 전제는 이 환경에서 성립하지 않습니다.
Master DB가 별도 PC에 있다면, 그 PC는 여기가 아닙니다.

---

## 20. 변경 파일

이번 Phase에서 생성한 것은 이 보고서 1개뿐입니다.

```
?? docs/phase11c_product_facts_artifact_deployment.md
```

Phase 11-A/11-B의 변경사항은 그대로 유지했습니다(삭제·commit 없음).

| 항목 | 건수 |
|---|---|
| production 코드 변경 | **0** |
| 테스트 변경 | **0** |
| DB 교체·수정 | **0** |
| `.env` 변경 | **0** |
| git commit / push | **0** |
| Naver 등록 / DPS 실행 / Chrome / Kakao / GPT 호출 | **0** |

`data/product_facts.db`는 `.gitignore` 36행의 `!data/product_facts.db` 예외로 **git 추적 대상**입니다.
Phase 11-A에서 보고한 대로이며, 이번 Phase에서 git 정책을 변경하지 않았습니다.

---

## 21. 잔여 위험

**BLOCKER — Master artifact 소재 불명**
승인된 Final Quality Gate DB가 이 PC에 없습니다. 그 DB가 실제로 존재하는지,
다른 저장매체·다른 PC에 있는지, 아니면 Final Gate 보고 자체가 다른 상태를 기술한 것인지
확인되기 전에는 반입을 진행할 수 없습니다.

**HIGH — 운영 답변이 계속 구버전 DB를 근거로 생성됨**
`data/product_facts.db`는 Step 2C 이전 스냅샷입니다. 다만 §18에서 측정한 대로
실질 영향은 94개 상품 중 1건이므로 긴급도는 낮습니다.

**MEDIUM — Final Gate 지표와 이 PC의 상품DB 상태가 크게 다름**
테스트 수(1045 vs 74), Q&A 검증 문항 수(306 vs 200), collection_status 분포(93/1 vs 94/0)가
모두 다릅니다. 두 가지 가능성이 있습니다.
(a) Final Gate가 다른 환경에서 수행되었고 그 산출물이 이 PC에 없다.
(b) Final Gate 보고에 인용된 수치가 실제 artifact와 일치하지 않는다.
어느 쪽인지는 이 PC의 자료만으로는 판정할 수 없습니다.

**HIGH — DPS 날짜 하드코딩 테스트 4건이 오늘부터 매일 실패**
§17에 상세를 적었습니다. Product Facts와 무관한 기존 결함이며, 방치하면
"전체 테스트 실패 0"이라는 회귀 기준선 자체를 쓸 수 없게 됩니다.
다음 작업 전에 별도로 처리하는 것을 권합니다.

**LOW — `product_facts_final_catalog.xlsx` 부재**
§8이 지정한 파일이 없어 Excel 교차검증을 완전한 형태로 수행하지 못했습니다.

---

## 22. 다음 Phase 권장

**Phase 11-C를 재실행하기 전에 사용자가 확인해야 할 것** — 아래 셋 중 하나입니다.

1. **Master DB의 실제 위치를 지정**해 주십시오. 외장 매체·다른 PC·백업본 등.
   확보되면 SHA-256만 대조해 즉시 이 Phase를 이어서 진행할 수 있습니다.
2. **Final Quality Gate를 이 PC의 상품DB 프로젝트에서 실제로 수행**하는 방안.
   현재 상태는 Phase 10 Step 2C까지이며, `13074225226`을 포함해 어떤 상품도 판매종료로 기록돼 있지 않습니다.
   판매종료 반영이 필요하다면 그 상품의 재수집이 선행되어야 합니다(이번 Phase 금지 항목).
3. **Final Gate 보고 수치가 실제와 달랐음을 인정**하고, 상품DB 현행 DB(`8fe643c5`, Step 2C 결과물)를
   새 기준으로 삼는 방안. 이 경우 §18에서 측정한 대로 답변 변화는 1건이며,
   Phase 11-B의 R1 gate는 판매종료 listing이 없는 동안 계속 방어층으로만 존재합니다.

**어느 쪽이든 제가 임의로 선택하지 않았습니다.** 사용자 결정 사항입니다.

세 방안 모두에서 Phase 11-B가 만든 안전장치는 그대로 유효합니다.
특히 `test_real_db_shipped_listings_are_all_currently_collected`가
판매종료 listing이 포함된 artifact가 반입되는 순간 실패로 알려 줍니다.

---

## 23. 최종 판정

# PHASE 11-C NOT READY — 최종 Product Facts Artifact를 안전하게 확정/반입할 수 없음

이유: 승인된 Final Quality Gate의 데이터 특성(`COLLECTION_SUCCESS 93`, 판매종료 1건,
`13074225226` 판매종료 상태)을 만족하는 artifact가 이 PC에 존재하지 않습니다.
후보 4개 전부 `94 / 94 / 0`이며, 판매종료 상태는 `listings`·fact 값·`raw_documents`
어느 경로에도 기록돼 있지 않습니다.

§11·§35에 따라 `data/product_facts.db`를 교체하지 않고 중단했습니다.
개발 PC와 상품DB의 DB 파일은 작업 전후 SHA-256이 동일합니다.
