# Phase 11-C 재개 — `product_facts_final.db` Q&A Auto 호환성 검증

작업일: 2026-08-29 · **DB 교체 없음** · production 코드 변경 없음 · Git commit/push 없음

> **결론을 먼저 적습니다.**
> **FINAL_DB_COMPATIBLE**
> 사용자가 반입한 `data/product_facts_final.db`는 기존 DB와 schema가 **완전히 동일**하고,
> `ProductFactRepository` / `ProductKnowledgeService`가 수정 없이 그대로 읽습니다.
> 상품 94개 × 질문 34개 = **3,196건 판정에서 WRONG 0 / UNSUPPORTED_BUT_ANSWERED 0**,
> 답변 가능한 질문은 1,258건 → **1,535건(+277)**으로 늘었습니다.
> Phase 11-B의 R1·R2·R4 gate가 모두 정상 동작하며, 특히 **R1이 실제 판매종료 상품에서 처음으로 발화**했습니다.

---

## 1. 시작 상태

| 항목 | 값 |
|---|---|
| branch | `main` |
| HEAD | `3950e8b` |
| Phase 11-A/B/C 기존 변경 | `M repositories/product_fact_repository.py`, `M scripts/export_inquiry_diagnostics.py`, `M services/product_knowledge_service.py`, `?? docs/phase11a…`, `?? docs/phase11b…`, `?? docs/phase11c_product_facts_artifact_deployment.md`, `?? tests/test_product_facts_safety_gate_11b.py` |

Phase 11-B Safety Gate를 포함해 기존 변경을 **되돌리거나 삭제하지 않았습니다.**

---

## 2. 두 DB Identity

| 항목 | 기존 `product_facts.db` | 신규 `product_facts_final.db` |
|---|---|---|
| 크기 | 60,170,240 bytes | **142,131,200 bytes** |
| mtime | 2026-08-25 12:30:32 | 2026-08-29 02:20:20 |
| SHA-256 | `cddf3082df82d87065a452ee8140af9f42c4d0b31e91753597717f55cc82ac4c` | **`e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078`** |

두 파일은 명백히 다른 artifact입니다. 신규 DB는 크기가 2.36배이며,
Phase 11-C 앞선 조사에서 확인한 상품DB PC의 후보 4개(모두 60 MB 이하) 중 **어느 것과도 다릅니다.**
사용자가 다른 경로에서 직접 가져온 새 artifact가 맞습니다.

**이번 Master 후보 기준 fingerprint: `e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078`**

WAL/journal 파일은 두 DB 모두 존재하지 않습니다.

---

## 3. SQLite 무결성

| 검사 | 기존 | 신규 |
|---|---|---|
| `PRAGMA integrity_check` | **ok** | **ok** |
| `PRAGMA foreign_key_check` | 0건 | 0건 |
| page_size / page_count | 4096 / 14,690 | 4096 / 34,700 |

두 DB 모두 `mode=ro` URI + `PRAGMA query_only = ON`으로만 열었습니다.
신규 DB에 `UPDATE` / `INSERT` / `DELETE`를 한 번도 실행하지 않았습니다.

---

## 4. Schema 비교

**완전히 동일합니다. migration이 필요 없습니다.**

| 항목 | 결과 |
|---|---|
| 테이블 집합 | 22개 동일 (추가·삭제 0) |
| 컬럼 | 차이 있는 테이블 **0개** (이름·타입·NOT NULL·기본값 모두 일치) |
| 인덱스 | 8개 동일 (추가·삭제 0) |
| foreign key 정의 | 동일 |
| `schema_migrations` | 3건 동일 (`001_canonical_facts`, `002_multi_product_metadata`, `003_canonical_lifecycle_repair`) |

코드가 실제로 사용하는 테이블명을 확인했습니다 —
`listings`, `canonical_facts`, `canonical_fact_values`, `canonical_fact_provenance`, `canonical_fact_listings`.
(사양서 §8이 언급한 `listing_canonical_facts`라는 테이블은 존재하지 않습니다.)

---

## 5. 데이터 규모 비교

| 지표 | 기존 | 신규 | 변화 |
|---|---|---|---|
| listings | 94 | 94 | – |
| **collection_status** | SUCCESS 94 | **SUCCESS 93 / FAILED 1** | **판매종료 1건 등장** |
| canonical_facts | 6,331 | 7,841 | +1,510 |
| ACTIVE | 3,894 | 5,136 | +1,242 |
| SUPERSEDED | 2,437 | 2,705 | +268 |
| VERIFIED (ACTIVE) | 3,546 | 4,283 | +737 |
| NEEDS_REVIEW (verification) | 348 | 853 | +505 |
| resolution MATCH | 812 | **3,461** | +2,649 |
| resolution SINGLE_SOURCE | 2,734 | 822 | −1,912 |
| resolution CONFLICT | 30 | 136 | +106 |
| resolution NEEDS_REVIEW | 318 | 717 | +399 |
| provenance (전체) | 11,849 | 25,003 | +13,154 |
| provenance (ACTIVE) | 6,309 | 19,267 | +12,958 |
| ACTIVE field 종류 | 200 | 216 | +16 |

**무엇이 달라졌는지**: 출처가 대폭 늘었습니다(provenance ACTIVE 3배). 그 결과
`SINGLE_SOURCE`였던 사실들이 복수 출처로 교차확인되어 `MATCH`로 이동했고(+2,649),
동시에 출처가 늘면서 서로 어긋나는 경우도 함께 늘었습니다(CONFLICT +106, NEEDS_REVIEW +399).
이는 품질 저하가 아니라 **검증 밀도가 높아진 결과**이며, 늘어난 CONFLICT/NEEDS_REVIEW는
기존 안전계약에 따라 자동으로 답변 근거에서 제외됩니다.

---

## 6. 상품 비교

| 항목 | 결과 |
|---|---|
| 공통 product_id | **94 (전부 일치)** |
| 신규에만 존재 | 없음 |
| 기존에만 존재 | 없음 |

### `13074225226` — 신규 DB의 실제 값 (과거 보고서와 대조하지 않고 그대로 기록)

| 항목 | 값 |
|---|---|
| `listing_id` | `listing_13074225226` |
| **`collection_status`** | **`COLLECTION_FAILED`** |
| `input_listing_name` | 삼성전자 LS27FM501E-2MO 삼성 무빙스타일 M5 스마트 모니터 IPTV 이동식 거치대 삼탠바이미 M50F 68.6cm(27인치), 웜 화이트 |
| `run_id` / `collection_run_id` | `20260825T001749Z_batch_capture` |
| ACTIVE fact | DYNAMIC 8(VERIFIED) / SEMI_STATIC 9(VERIFIED) / STATIC 18(VERIFIED) + 6(NEEDS_REVIEW) |
| `raw_documents` | 25건 전부 `COLLECTION_SUCCESS` (과거 수집분) |
| `availability` / `product_status` | `IN_STOCK` / `SALE` — **둘 다 VERIFIED로 남아 있음** |

마지막 줄이 중요합니다. 판매종료 listing인데 "재고 있음 / 판매중"이 VERIFIED 상태로 남아 있습니다.
이것이 Phase 11-B의 R1 gate가 겨냥한 바로 그 위험이며, 아래 §10에서 실제로 차단되는 것을 확인했습니다.

---

## 7. Repository 호환성

`ProductFactRepository`에 신규 DB 경로를 인자로 넘겨 검증했습니다. 기본 경로는 바꾸지 않았습니다.

| 항목 | 결과 |
|---|---|
| `available()` | True |
| `identity()` | size 142,131,200 / mtime `2026-08-28T17:20:20+00:00` |
| `PRAGMA query_only` 강제 | 1 (읽기 전용 확인) |
| `listing_for_product('13074225226')` | 정상 — `collection_status='COLLECTION_FAILED'` 포함 반환 |
| `facts_for_product(...)` | 정상 (value decoding, scope, volatility, verification/resolution 모두 반환) |
| `provenance_for_values(...)` | 정상 |
| **오류** | **0** |

---

## 8. Service 호환성

`ProductKnowledgeService`에 신규 DB를 경로 주입해 **상품 94개 × 질문 6종 = 564회** 조회했습니다.

| 항목 | 결과 |
|---|---|
| 예외 발생 | **0건** |
| safe fact 합계 | 488 |
| excluded fact 합계 | 763 |

기존 `data/product_facts.db`는 교체하지 않았습니다.

---

## 9. Exact Join

| 항목 | 값 |
|---|---|
| 문의 총수 | 2,772 |
| `product_id` 보유 | 717 (`PRODUCT_INQUIRY` 717/1,662, `CUSTOMER_INQUIRY` **0**/1,110) |
| unique product_id | 79 |
| **exact matched (신규 DB)** | **59** |
| unmatched | 20 |

기존 DB와 **동일**합니다(두 DB의 listing 집합이 같은 94개이므로). Phase 11-A 측정치(79/59)도 그대로입니다.
fuzzy matching은 도입하지 않았습니다.

---

## 10. R1 실검증 — 판매종료 상품에서 최초 발화

`13074225226`(COLLECTION_FAILED)의 ACTIVE fact 전체를 중앙 판정 로직에 직접 통과시켰습니다.

| volatility | 판정 결과 |
|---|---|
| `DYNAMIC_LISTING_FACT` 8건 | 전부 `VOLATILE_LISTING_FACT` (기존 규칙, 상태와 무관) |
| `SEMI_STATIC_POLICY_FACT` 9건 | 전부 **`COLLECTION_STATUS_NOT_CURRENT`** ← Phase 11-B가 추가한 gate |
| `STATIC_PRODUCT_FACT` | **USABLE 18** / SUPERSEDED 36 / VERIFICATION_NEEDS_REVIEW 6 |

차단된 SEMI_STATIC 필드 예: `arrival_guarantee`, `delivery_company`, `delivery_fee` —
판매가 끝난 listing의 배송 조건이며, 고객에게 말하면 지킬 수 없는 약속이 됩니다.

**대조군**: 같은 fact를 `COLLECTION_SUCCESS`로 가정하면 SEMI_STATIC 9건이 전부 `USABLE`이 됩니다.
즉 gate가 정확히 `collection_status` 때문에 차단한 것이며, 다른 조건이 우연히 걸린 것이 아닙니다.

**정적 사양은 그대로 살아남았습니다**(18건 usable) — §8이 요구한
"판매종료라는 이유만으로 화면크기·HDMI 개수를 UNKNOWN으로 만들지 말 것"이 지켜졌습니다.

**Fail-safe**: `FIELD_TOPICS`가 요청 가능한 63개 필드는 신규 DB에서도 **전부 `STATIC_PRODUCT_FACT`**입니다.
따라서 고객 질문 경로에서는 이 gate가 아직 발화하지 않으며, Phase 11-B에서 보고한 대로
**방어층(defence in depth)** 지위가 유지됩니다. 저장소 수준 조회에서는 위와 같이 실제로 작동합니다.

---

## 11. R2 실검증 — 패키지/구성품

신규 DB의 실제 상품으로 검증했습니다.

| 상품 | 유형 | 질문 | 결과 |
|---|---|---|---|
| `11848813000` | TV+셋톱박스 패키지 | 셋톱박스도 삼성 제품인가요? | **차단** `COMPONENT_SUBJECT_UNRESOLVED` |
| `11848813000` | TV+셋톱박스 패키지 | 스탠드 제조사가 삼성인가요? | **차단** 동일 |
| `9866761076` | 무빙스탠드 패키지 | 같이 오는 스탠드도 삼성 제품인가요? | **차단** 동일 |
| `9866761076` | 무빙스탠드 패키지 | 이 상품 브랜드가 뭐예요? | 허용 — `brand=삼성` |
| `11779070305` | 셋톱박스 단독 | 이 셋톱박스 제조사는 어디예요? | 허용 — **`manufacturer=이노피아테크`** |
| `11779070305` | 셋톱박스 단독 | 이 셋톱박스 브랜드가 어디예요? | 허용 — **`brand=SHAKS`** |

listing-level brand/manufacturer가 구성품으로 상속되는 사례는 **0건**입니다.

신규 DB에서 이 위험이 실재함을 보여주는 추가 근거:

| product_id | 상품 | brand | manufacturer |
|---|---|---|---|
| `12101311850` | 삼탠바이미 이동식 거치대(스탠드 중심 listing) | **오베닉** | **(주)오제플러스** |
| `13239109816` | 삼성 43인치 TV 무빙스타일 패키지 | 무빙스타일 | 삼성전자 |
| `11779070305` | 샥스 G1 셋탑박스 | SHAKS | 이노피아테크 |

패키지에 들어가는 스탠드·셋톱박스가 삼성이 아닌 사례가 실제로 존재합니다.

---

## 12. R4 실검증 — brand / manufacturer

신규 DB의 실제 값 분포:

| field | 값 (usable 기준) |
|---|---|
| `brand` (83) | 삼성 63, **오디세이 8, 스마트모니터 6, 무빙스타일 4**, 오베닉 1, SHAKS 1 |
| `manufacturer` (86) | 삼성전자 84, (주)오제플러스 1, 이노피아테크 1 |
| `country_of_origin` (83) | 국산 11, 한국(베트남,중국…) 계열 다수, 베트남 7 … |

검증 결과:

| 상품 | 질문 | 요청 필드 | 답변 |
|---|---|---|---|
| `12601323000` | 브랜드가 뭐예요? | `brand` | 삼성 |
| `12601323000` | 제조사가 어디예요? | `manufacturer` | 삼성전자 |
| `12601323000` | 원산지가 어디예요? | `country_of_origin` | 한국,베트남,중국… |
| `12601323000` | **삼성 제품인가요?** | **`manufacturer`만** | 삼성전자 |
| **`12101311850`** | **삼성 제품인가요?** | **`manufacturer`만** | **(주)오제플러스** |
| `12101311850` | 브랜드가 뭐예요? | `brand` | 오베닉 |
| `12601323000` | 오디세이 제품인가요? | `brand`,`model_name` | `model_name=삼성전자 오디세이 G5 G50F LS32FG500` (brand는 `PRODUCT_LINE_NOT_IN_VALUE`) |
| `13239109816` | 무빙스타일 제품인가요? | `brand`,`model_name` | 둘 다 사용 (`brand=무빙스타일`) |
| `11844406044` | 오디세이 제품인가요? | `brand`,`model_name` | **둘 다 차단 → UNKNOWN** |
| `10245133834` | 스마트모니터인가요? | `brand`,`model_name` | **둘 다 차단 → UNKNOWN** |

`12101311850` 사례가 R4의 존재 이유를 그대로 보여줍니다 — brand(`오베닉`)로 답했다면
"삼성 제품인가요?"에 잘못 답할 뻔했고, manufacturer로 답해 `(주)오제플러스`가 나왔습니다.

**Negative inference 없음**: 근거가 없는 두 경우 모두 `evidence_text()`와 `prompt_block()`이
**빈 문자열**입니다 — 모델에게 "오디세이"라는 단어조차 전달되지 않으므로 "아니다"라고 답할 근거가 없습니다.

---

## 13. 전체 Field 현황

신규 DB의 ACTIVE field는 **216종**입니다. Q&A 사용 가능성으로 분류하면:

| 분류 | 종수 |
|---|---|
| **A. 이미 Q&A에서 사용 가능** | **63** |
| **B. DB에 VERIFIED가 있으나 Q&A topic mapping 없음** | **140** |
| C. 안전상 사용 금지 (`DYNAMIC_LISTING_FACT`) | 8 |
| E. 매핑도 없고 usable도 0 | 5 |
| D. 매핑은 있으나 usable 0 | 0 |

A 분류 상위: `manufacture_date` 94, `model_name` 93, `manufacturer` 86, `brand` 83,
`certification_number` 83, `country_of_origin` 83, `hdmi_present` 68, `usb_present` 66,
`screen_size` 60, `resolution` 49, `hdmi_port_count` 41.

---

## 14. Q&A Mapping 공백 (B 분류 상위)

| field | usable 상품 수 | volatility |
|---|---|---|
| `category_id` / `channel_id` / `product_id` | 94 | STATIC (식별자 — 답변 대상 아님) |
| `delivery_fee` / `delivery_method` / `free_delivery` / `free_return_insurance` / `installation_fee_applies` / `seller_name` | 94 | **SEMI_STATIC** (정책 — Template/DPS 영역) |
| `delivery_company` | 93 | SEMI_STATIC |
| `seo_page_title` / `feature_description` / `product_name` | 88~92 | STATIC (마케팅 문구) |
| `arrival_guarantee` / `option_usable` | 87 | SEMI_STATIC |
| `category` | 85 | STATIC |
| `representative_image_*` | 81~84 | STATIC (이미지 메타) |
| `installation_method` | 35 | SEMI_STATIC |
| `power_cable_included` | 38 | STATIC |
| `set_top_box_wifi_present` / `set_top_box_wifi_standard` | 32 | STATIC |
| `panel_button_lock` / `usb_port_lock` | 23~25 | STATIC |
| `tv_plus` / `universal_guide` / `hdmi_cec` | 21 | STATIC |

B 분류 140종의 대부분은 **식별자·이미지 메타·마케팅 문구·배송정책**으로,
Q&A에서 Product Facts가 답할 대상이 아니거나(§18 DPS/Template 영역) 답변 가치가 없는 항목입니다.

---

## 15. 핵심 정보 Coverage

| 항목 | field 수 | VERIFIED 보유 상품(최대) | Q&A 매핑 | 상태 |
|---|---|---|---|---|
| 모델명 | 1 | 93 | 1 | 사용 가능 |
| 모델코드 | 2 | 30 | 2 | 사용 가능 |
| 브랜드 | 1 | 87 | 1 | 사용 가능 |
| 제조사 | 1 | 92 | 1 | 사용 가능 |
| 화면크기 | 2 | 62 | 2 | 사용 가능 |
| 해상도 | 2 | 49 | 2 | 사용 가능 |
| 주사율 | 1 | 39 | 1 | 사용 가능 |
| HDMI | 3 | 74 | 3 | 사용 가능 |
| USB | 3 | 72 | 3 | 사용 가능 |
| DisplayPort | 3 | 38 | 2 | 사용 가능 |
| Bluetooth | 1 | 3 | 1 | 사용 가능(커버리지 낮음) |
| Wi-Fi | 1 | 3 | 1 | 사용 가능(커버리지 낮음) |
| 크기 | 2 | 39 | 2 | 사용 가능 |
| 무게 | 2 | 40 | 2 | 사용 가능 |
| VESA | 2 | 34 | 2 | 사용 가능 |
| 구성품 | 14 | 38 | 1 | 일부만 매핑 |
| 스탠드 | 19 | 2 | 13 | 사용 가능(커버리지 낮음) |
| **리모컨** | 1 | 2 | **0** | **매핑 없음** |
| **설치방법** | 4 | 94 | **0** | **매핑 없음** (SEMI_STATIC 포함) |
| **OTT** | 7 | 94 | **0** | **매핑 없음** |
| **셋톱박스** | 6 | 32 | **0** | **매핑 없음** |
| **보증/AS** | 12 | 8 | **0** | **매핑 없음** |
| **비밀번호/PIN** | 8 | 25 | **0** | **매핑 없음** |

---

## 16. Retrieval Matrix

상품 **94개** × 질문 **34종** = **3,196건** 판정. (§21 최소 요건 20상품·300건을 크게 상회)

질문군에는 모델명·모델코드·브랜드·제조사·원산지·삼성여부·제품라인 2종·화면크기·해상도·주사율·패널·
HDMI·USB·DisplayPort·블루투스·와이파이·스피커·무게·크기·VESA·스탠드·시야각·소비전력·에너지등급·
HDR·OS·미러링·인증번호·출시, 그리고 subject 검증용 4종(STB/스탠드 주어, 자기지시)이 포함됩니다.

| 판정 | 기존 DB | **신규 DB** |
|---|---|---|
| CORRECT | 1,258 | **1,535** |
| SAFE_UNKNOWN | 1,938 | 1,661 |
| **WRONG** | 0 | **0** |
| **UNSUPPORTED_BUT_ANSWERED** | 0 | **0** |

신규 DB에서 답변 가능한 질문이 **277건 증가**했고, 오답과 근거 없는 답변은 양쪽 모두 0입니다.

### 판정 방식의 강도를 밝힙니다

답변된 fact 1,944건이 어떤 근거로 CORRECT 판정을 받았는지 분리해 집계했습니다.

| 지지 수준 | 건수 | 비율 | 의미 |
|---|---|---|---|
| `SOURCE_TEXT` | 1,703 | 87.6% | 캡처된 페이지·이미지 원문이 값을 **그대로** 담고 있음 (가장 강함) |
| `DERIVED_AFFIRMATIVE` | 220 | 11.3% | `hdmi_present="YES"`처럼 파생된 긍정 판정. 근거가 존재하고, 그 근거가 부재를 진술하지 **않음**을 확인 |
| `CANONICAL_FORM` | 14 | 0.7% | 정규화·번역된 값(`화이트`→`WHITE`, `2024년 4월`→`2024-04`). **원본 캡처 문자열이 근거 원문에 실제로 있는지** 확인 |
| `RAW_CAPTURE` | 4 | 0.2% | 원문은 일부만 보존, 값 행의 원본 캡처가 값을 담고 있음 |
| `DERIVED_NEGATIVE` | 3 | 0.2% | 파생된 부정 판정. 근거가 실제로 부재를 진술할 때만 인정 |

약한 판정 수준으로 도망가지 않도록 세 단계 모두 **실제 근거를 요구**합니다.
특히 `DERIVED_NEGATIVE`는 근거에 부재 진술이 없으면 `UNSUPPORTED_BUT_ANSWERED`로 실패시킵니다.

### 부정 사실의 근거를 개별 확인했습니다

Q&A가 사용할 수 있는 부정값 fact를 전수 확인한 결과, 모두 페이지에 **명시적으로 기재**돼 있었습니다.

| field | 값 | 근거 원문 |
|---|---|---|
| `ott_supported` | NO | "본 제품은 넷플릭스 등 OTT 기능을 지원하지 않는 모델입니다." |
| `package_auto_pivot_supported` | NO | "해당 모델은 오토피벗을 지원하지 않는 모델입니다" |
| `accessory_shelf_included` | NO | "선반 설치 가능(별매)" |
| `free_delivery` | NO | `window.__PRELOADED_STATE__…freeDelivery = false` |

`hdmi_present` / `usb_present` / `displayport_present`에는 **NO 값이 하나도 없습니다** —
"HDMI 없습니다" 같은 답변이 만들어질 여지가 없습니다.
"없는 fact는 부정 사실이 아니다" 계약은 그대로 유지됩니다.

---

## 17. SAFE_UNKNOWN 분석

SAFE_UNKNOWN 1,661건의 사유입니다(한 질문에 복수 사유 가능, 중복 포함 집계).

| 사유 | 건수 | 성격 |
|---|---|---|
| `NO_FACT_FOR_TOPIC` | 1,057 | DB에 해당 fact 자체가 없음 — 수집 범위 문제 |
| `SUPERSEDED_BY_LATER_RUN` | 434 | 재수집으로 대체된 과거 값 — 정상 |
| `VERIFICATION_NEEDS_REVIEW` | 263 | 출처 미확정 — 품질 게이트 |
| `COMPONENT_SUBJECT_UNRESOLVED` | 181 | **R2 subject 안전장치** |
| `PRODUCT_LINE_NOT_IN_VALUE` | 167 | **R4 제품라인 근거 요건** |

기존 DB 대비 변화: `NO_FACT_FOR_TOPIC` 1,336 → 1,057(−279, 사실이 늘어 공백이 줄어듦),
`VERIFICATION_NEEDS_REVIEW` 37 → 263(+226, 출처가 늘며 미확정도 함께 증가).
R2·R4로 인한 차단(181/167)은 두 DB에서 거의 동일합니다.

**주제별 CORRECT 상위**: 출시 94, 모델명 93, 모델코드 93, 제조사 92, 삼성여부 92, STB자기지시 92, 인증번호 89.
**하위**: 에너지등급 2, 스탠드주어 3, OS 3, 스탠드 3, 스피커 3, 블루투스 3, 와이파이 5, 제품라인2 5, 시야각 11.
`STB주어`("셋톱박스도 삼성 제품인가요?")는 **CORRECT 0** — R2가 의도대로 전부 보류시킨 결과입니다.

---

## 18. 매핑 확장 후보 (§23 — 구현하지 않음)

| field | usable | volatility | 고객 질문 예시 | subject 위험 | 추천 |
|---|---|---|---|---|---|
| `power_cable_included` | 38 | STATIC | "전원 케이블 들어있나요?" | 낮음 | **권장** |
| `set_top_box_wifi_present` | 32 | STATIC | "셋톱박스 와이파이 되나요?" | **높음** (구성품 scope 설계 선행 필요) | 보류 |
| `panel_button_lock` / `usb_port_lock` | 23~25 | STATIC | "버튼/USB 잠금 되나요?" | 낮음 | 검토 |
| `tv_plus` / `universal_guide` / `hdmi_cec` | 21 | STATIC | "TV플러스 되나요?" | 낮음 | 검토 |
| `set_top_box_ott_supported` | 16 | STATIC | "셋톱박스로 OTT 되나요?" | **높음** | 보류 |
| `business_tv_app` | 14 | STATIC | "비즈니스TV 앱 되나요?" | 낮음 | 검토 |
| `password_lock_supported` | 12 | STATIC | "비밀번호 잠금 되나요?" | 낮음 | 검토 |
| `pixel_defect_warranty_period_months` | 7 | STATIC | "불량화소 보증 기간?" | 중간 (정책 성격) | 보류 |
| `installation_method` | 35 | **SEMI_STATIC** | "설치는 어떻게 하나요?" | **높음** (DPS/Template 영역 침범) | **금지** |
| `remote_control_included` | 2 | STATIC | "리모컨 포함인가요?" | **높음** (누락 문의와 혼동) | **금지** |
| `ott_supported` | 2 | STATIC | "넷플릭스 되나요?" | 낮음 | 커버리지 부족 |

**이번 Phase에서 매핑을 확장하지 않았습니다.** 이유는 셋입니다.

1. §29 — 신규 DB를 안전하게 읽는 데 **코드 수정이 전혀 필요 없었습니다.**
2. 매핑 추가는 신규 DB뿐 아니라 **현재 기본 경로의 기존 DB 답변까지 바꿉니다.** 전환 전에 할 일이 아닙니다.
3. `set_top_box_*`는 구성품 고유 사실인데, 현재 `component_scope`는 `accessory_` 접두사만 보고 판정하므로
   그대로 매핑하면 R2 gate가 이들을 본체 사실로 오인합니다. **설계 선행이 필요한 항목**입니다(§25 잔여 위험).

`remote_control_included`는 §25(누락 문의) 정책과 충돌 위험이 커 커버리지(2건)에 비해 이득이 없습니다.

---

## 19. DPS 불변

`services/answer_service.py`는 이번에도 **변경하지 않았습니다.** Phase 11-A에서 확인한 순서가 그대로입니다.

```
1215행  plan = self.plans.create(...)   ← requires_dps_lookup / requires_order_lookup 확정
1236행  product_knowledge = ...facts_for_inquiry(...)
1327행  if plan.requires_dps_lookup: ...
```

Product Facts 결과가 `plan`으로 되돌아가는 경로가 없으므로, 신규 DB의 어떤 정보도
`requires_dps_lookup` / `requires_order_lookup` 결정을 바꿀 수 없습니다.

또한 현재 배송일·설치예정일·주문상태에 해당하는 fact는 전부 `DYNAMIC_LISTING_FACT`이거나
`SEMI_STATIC_POLICY_FACT`이며, 전자는 항상 차단되고 후자는 `FIELD_TOPICS`에 매핑돼 있지 않아
질문 경로로 도달하지 않습니다. DPS 실제 실행은 하지 않았습니다.

## 20. Missing Item 불변

`answer/inquiry_analysis.py`의 `MISSING_ITEM_REPORT` 차단은 Product Facts 조회보다
**앞선 분석 단계**에서 `can_generate_answer=False`로 일어납니다. 해당 코드는 변경하지 않았습니다.

신규 DB에 `remote_control_included="YES"`(2건)가 있어도 이 순서 때문에 영향을 줄 수 없으며,
해당 field는 `FIELD_TOPICS`에 매핑돼 있지도 않습니다(§18에서 매핑하지 않기로 한 이유 중 하나).

## 21. Learning 불변

`services/learning_evidence_policy.py`는 변경하지 않았습니다.
`PRODUCT_FACT_VS_LEARNING_CONFLICT` 정책과 Learning ranking/retrieval 모두 그대로입니다.

## 22. Auto-post 안전성

`services/auto_processing_eligibility_service.py`는 변경하지 않았습니다.
신규 DB에서 늘어난 `NEEDS_REVIEW`(853) / `CONFLICT`(136)는 중앙 판정에서 `excluded_facts`로 분류되어
`safe_facts`에 들어가지 않습니다. 따라서

- `has_safe_facts` → False
- `supports_question(...)` → False
- `prompt_block()` / `evidence_text()` → 빈 문자열

이 되어 `current_fact_verified`가 서지 않고, 기존 `PRODUCT_FACT_NOT_VERIFIED` 차단이 그대로 걸립니다.
실제 Naver 등록은 하지 않았습니다.

---

## 23. Latency

| DB | 조회 60회 | 평균 | 중앙 | 최대 |
|---|---|---|---|---|
| 기존 (60 MB) | 60 | 2.66 ms | 3.22 ms | 4.29 ms |
| **신규 (142 MB)** | 60 | 3.15 ms | **3.61 ms** | 4.59 ms |

Phase 11-B에서 측정한 약 3 ms 수준과 같습니다. 파일이 2.36배 커졌지만
인덱스가 동일하고 조회가 exact key 기반이라 **회귀라 할 만한 변화가 없습니다**(중앙값 +0.39 ms).

`identity(digest=True)`로 142 MB 전체 SHA-256을 계산하는 데 **136 ms**입니다.
답변 경로에서는 호출되지 않으며 명시적 진단에서만 계산합니다.

---

## 24. DB Identity 관측성

```
identity()            → {"path": "data/product_facts_final.db", "available": true,
                         "size_bytes": 142131200,
                         "modified_at": "2026-08-28T17:20:20+00:00", "sha256": null}
identity(digest=True) → sha256 = e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078
```

진단 Export는 디렉터리를 제외하고 **파일명만** 내보내므로
기존 계약(`test_the_export_names_no_machine_or_path`)이 유지됩니다.

---

## 25. 코드 변경

**production 코드를 한 줄도 변경하지 않았습니다.**

신규 DB는 수정 없이 그대로 읽히므로 §29의 "이미 호환된다면 수정하지 마세요"에 해당합니다.

이번 Phase에서 만든 것:

| 파일 | 위치 | 성격 |
|---|---|---|
| `docs/phase11c_final_db_compatibility.md` | 저장소 | 이 보고서 (유일한 저장소 신규 파일) |
| `retrieval_matrix.py` + `matrix_final.json` / `matrix_old.json` | scratchpad | 검증 도구·결과 (저장소 밖) |
| `final_db_tests/` (기존 테스트 3개 복사본, 경로만 치환) | scratchpad | @real_db 사전 검증용 (저장소 밖) |

기존 `docs/phase11c_product_facts_artifact_deployment.md`는 **덮어쓰지 않았습니다.**

---

## 26. 테스트

### 신규 DB로 `@real_db` 테스트 사전 검증

`@real_db` 테스트는 `REAL_DB = Path("data") / "product_facts.db"`로 경로가 **하드코딩**되어 있어
환경변수 override를 따라가지 않습니다. 그래서 세 파일을 scratchpad로 복사해
경로만 신규 DB로 치환한 뒤 실행했습니다(저장소 파일 미변경).

```
105 passed, 2 failed
```

**실패 2건 모두 결함이 아닙니다.**

**(1) `test_real_db_shipped_listings_are_all_currently_collected` — 설계된 tripwire의 정상 발화**

Phase 11-B에서 이 테스트를 만들 때 docstring에 이렇게 적었습니다:
"when a newer artifact arrives with a delisted listing, this will fail and say so out loud."
신규 DB에 `COLLECTION_FAILED` listing이 실제로 들어왔으므로 예정대로 발화한 것입니다.

**(2) `test_real_db_absent_field_stays_absent` — 데이터가 개선되어 전제가 낡음**

이 테스트는 `10194603339`에 "verified port count가 없다"를 전제로 커버리지 공백을 문서화했습니다.
신규 DB는 그 공백을 메웠습니다.

| DB | 결과 |
|---|---|
| 기존 | `hdmi_present=YES`, `hdmi_version=2.0` (port count 없음) |
| 신규 | **`hdmi_port_count=1`** 추가 — 근거 `IMAGE_OCR: "HDMI 1개"` |

근거가 확실한 개선이며 안전 규칙 위반이 아닙니다.

**두 테스트 모두 이번에 수정하지 않았습니다.** §33에 따라 DB를 아직 전환하지 않았고,
기본 경로가 기존 DB인 동안에는 둘 다 정상 통과하기 때문입니다.
전환 시점의 최소 수정안은 §29에 적었습니다.

### 신규 테스트 추가 여부

추가하지 않았습니다. `data/product_facts_final.db`라는 파일명은 전환 시 사라지거나 바뀔 이름이라,
그 경로에 의존하는 테스트를 저장소에 남기면 전환 직후 깨집니다.
Phase 11-B의 gate 테스트 32건이 이미 fixture 기반으로 R1·R2·R4를 검증하고 있고,
신규 DB에 대한 검증은 위 복사본 실행과 §16 matrix로 수행했습니다.

### 전체 테스트 — 두 조건으로 실행

**A. 기본 경로(기존 DB) — 현재 상태 확인**

```
4 failed, 3522 passed in 1135.00s (0:18:54)

FAILED tests/test_atomic_answer_completeness.py::test_case_g_order_number_still_uses_dps
FAILED tests/test_atomic_draft_composition.py::test_a_clean_single_question_still_auto_posts
FAILED tests/test_delivery_pipeline_e2e_dps.py::test_confirmed_date_reaches_the_draft_and_clears_eligibility
FAILED tests/test_golden_auto_post_core_e2e.py::test_gs02_body_order_number_reaches_order_lookup_and_dps
```

**실패 4건은 §27에 적은 DPS 날짜 하드코딩 문제이며, Phase 11-C 앞선 조사에서 확인한 것과 정확히 동일합니다.**
이번 Phase에서 코드를 전혀 바꾸지 않았으므로 새로 생긴 실패가 아닙니다.
Phase 11-B 종료 시점의 3,526 passed와 비교하면 합계는 같고(3,522+4=3,526)
UTC 날짜가 `2026-08-28`을 넘어가면서 이 4건만 pass→fail로 이동한 상태가 유지되고 있습니다.

**B. `OJE_PRODUCT_FACTS_DB_PATH=data/product_facts_final.db` — 전환 리허설**

파이프라인 전체가 신규 DB를 읽도록 환경변수만 바꿔 실행했습니다(`.env` 미수정, 해당 프로세스 한정).

```
5 failed, 3521 passed in 1120.20s (0:18:40)

FAILED tests/test_atomic_answer_completeness.py::test_case_g_order_number_still_uses_dps
FAILED tests/test_atomic_draft_composition.py::test_a_clean_single_question_still_auto_posts
FAILED tests/test_delivery_pipeline_e2e_dps.py::test_confirmed_date_reaches_the_draft_and_clears_eligibility
FAILED tests/test_golden_auto_post_core_e2e.py::test_gs02_body_order_number_reaches_order_lookup_and_dps
FAILED tests/test_product_facts_b5.py::test_default_path_is_configurable
```

| 실패 | 성격 |
|---|---|
| DPS 날짜 4건 | A와 동일한 기존 문제. **Product Facts와 무관** |
| `test_default_path_is_configurable` | **리허설 방식의 부작용** |

마지막 1건의 원인을 확인했습니다. 이 테스트의 **첫 줄**이
`get_product_facts_path() == Path("data")/"product_facts.db"` —
즉 "환경변수가 설정되지 않았을 때의 기본값"을 단언합니다.
리허설을 위해 전체 실행에 환경변수를 걸어 두었으므로 그 전제가 깨진 것입니다.

```
AssertionError: assert WindowsPath('data/product_facts_final.db')
                    == WindowsPath('data')/'product_facts.db'
```

이는 결함이 아니라 **환경변수 override가 정상 작동한다는 증거**이며,
권장 전환 방식(A안 — 파일 교체, 환경변수 미사용)에서는 발생하지 않습니다.

**결론: 신규 DB로 파이프라인 전체를 돌려도 Product Facts에서 기인한 실패가 0건입니다.**
Template · Learning · DPS routing · GPT context 준비 · validation · approval · auto-post eligibility가
모두 기존대로 동작했습니다. 실제 GPT 호출, Naver 등록, DPS 실행은 하지 않았습니다.

---

## 27. DB 무변경 확인

| 파일 | 항목 | 작업 전 | 작업 후 | 결과 |
|---|---|---|---|---|
| `data/product_facts.db` | SHA-256 | `cddf3082…ac82ac4c` | `cddf3082…ac82ac4c` | **불변** |
| | size / mtime | 60,170,240 / 2026-08-25 12:30:32 | 동일 | **불변** |
| `data/product_facts_final.db` | SHA-256 | `e0cdd363…6f55a078` | `e0cdd363…6f55a078` | **불변** |
| | size / mtime | 142,131,200 / 2026-08-29 02:20:20 | 동일 | **불변** |

`-wal` / `-shm` / `-journal` 잔여 파일도 **없습니다**.
모든 접근이 `mode=ro` URI + `PRAGMA query_only = ON`이었고,
`identity(digest=True)`의 해시 계산도 파일을 읽기만 합니다.

`data/oje_automation.db`(문의 DB)도 읽기 전용으로만 조회했습니다.

---

## 28. Git 상태

```
 M repositories/product_fact_repository.py     ← Phase 11-B
 M scripts/export_inquiry_diagnostics.py       ← Phase 11-B
 M services/product_knowledge_service.py       ← Phase 11-B
?? docs/phase11a_product_facts_integration_audit.md
?? docs/phase11b_product_facts_safety_gate.md
?? docs/phase11c_product_facts_artifact_deployment.md
?? docs/phase11c_final_db_compatibility.md     ← 이번 Phase 신규 (유일)
?? tests/test_product_facts_safety_gate_11b.py ← Phase 11-B
```

HEAD는 `3950e8b` 그대로이며 commit / push 하지 않았습니다.
**이번 Phase에서 추가된 것은 이 보고서 1개뿐**이고, 기존 변경은 하나도 되돌리지 않았습니다.

`data/product_facts.db`는 `.gitignore` 36행의 `!data/product_facts.db` 예외로 git 추적 대상입니다.
**`data/product_facts_final.db`는 `*.db` 규칙에 걸려 추적되지 않습니다**(untracked 목록에도 나타나지 않음).
git 추적 정책은 임의로 변경하지 않았습니다 — 전환 시 파일명이 `product_facts.db`가 되면
자동으로 추적 대상이 되므로, 57 MB → 135 MB 바이너리를 저장소에 담을지 별도 결정이 필요합니다.

---

## 29. 전환 가능성 및 다음 단계

### 전환 시 반드시 함께 처리해야 할 것

**(1) `test_real_db_shipped_listings_are_all_currently_collected` 최소 수정**

현재 단언 `statuses == {"COLLECTION_SUCCESS"}`는 "운영 DB는 항상 94/94여야 한다"는 가정을 담고 있고,
이는 새 안전계약("non-success listing의 STATIC은 허용, SEMI_STATIC/DYNAMIC은 차단")과 맞지 않습니다.
권장 수정: 상태 분포를 단언하는 대신,
**non-success listing이 있으면 그 listing의 SEMI_STATIC/DYNAMIC fact가 실제로 차단되는지**를 단언하도록 바꿉니다.
기대값을 통과시키려 낮추는 것이 아니라, 검사 대상을 계약으로 옮기는 변경입니다.

**(2) `test_real_db_absent_field_stays_absent` 최소 수정**

`10194603339`/`hdmi_port_count`가 더 이상 공백이 아니므로, 이 테스트가 지키려는 규칙
("커버리지 공백은 usable fact가 되지 않는다")을 여전히 만족하는 다른 (상품, field) 쌍으로 교체합니다.
규칙 자체는 그대로 두어야 합니다.

### 전환 방식 권장

`.env` 수정이 금지된 현재 조건에서는 **A안(파일 교체)**이 적절합니다.

1. `data/product_facts.db`를 `data/archive/product_facts_before_final_<timestamp>.db`로 백업 (SHA-256 대조)
2. `product_facts_final.db`의 **byte-identical 복사본**으로 교체 (SQL 변환 금지)
3. 교체 직후 SHA-256이 `e0cdd363…`와 일치하는지 확인
4. 위 두 테스트 최소 수정 후 전체 테스트 재실행
5. 롤백 경로: 백업 파일로 되돌리거나 `OJE_PRODUCT_FACTS_DB_PATH`로 이전 파일 지정

### 그 다음 Phase 권장

- §18의 매핑 확장 후보 중 낮은 위험 항목(`power_cable_included`, `tv_plus`, `panel_button_lock` 등) 검토
- `set_top_box_*` 매핑을 위한 **구성품 scope 설계** — 현재 `component_scope`가 `accessory_` 접두사만 인식
- DPS 날짜 하드코딩 테스트 4건 처리(§27 참조)

---

## 30. 잔여 위험

**MEDIUM — `set_top_box_*` 필드와 R2 gate의 개념 불일치**
신규 DB에는 셋톱박스 고유 사실이 32개 상품에 있습니다. 이들은 구성품 사실인데
`component_scope` 판정이 `accessory_` 접두사만 보므로 본체 사실로 분류됩니다.
현재는 매핑돼 있지 않아 무해하지만, 매핑하기 전에 반드시 설계해야 합니다.

**MEDIUM — CONFLICT / NEEDS_REVIEW 대폭 증가**
CONFLICT 30 → 136, NEEDS_REVIEW(resolution) 318 → 717. 안전계약이 자동 차단하므로 오답 위험은 없지만,
그만큼 답변 못 하는 항목이 생깁니다. 상품DB 쪽 품질 정리 대상입니다.

**MEDIUM — DPS 날짜 하드코딩 테스트 4건**
Phase 11-C 앞선 조사에서 확인한 기존 문제로, Product Facts와 무관합니다.
`2026-08-28`을 고정한 4개 테스트가 UTC 날짜 경계를 넘으며 매일 실패합니다.

**LOW — `CUSTOMER_INQUIRY` 1,110건의 product_id 부재**
Naver 고객문의 API 응답에 상품 식별자가 없어 Product Facts를 조회할 수 없습니다. 구조적 한계입니다.

**LOW — 파생 판정 11.3%**
`DERIVED_AFFIRMATIVE` 220건은 "YES" 같은 파생 값이라 문자열 대조가 불가능하고 극성만 검증했습니다.
근거가 부재를 진술하지 않음은 확인했으나, 원문 그대로의 대조보다는 약한 검증입니다.

---

## 31. 최종 판정

# PHASE 11-C FINAL DB READY — 새 Product Facts DB Q&A 호환성 검증 완료

§32의 판정 기준을 하나씩 대조합니다.

| 기준 | 결과 |
|---|---|
| SQLite integrity 정상 | ○ `integrity_check = ok`, FK 위반 0 |
| schema 호환 | ○ **완전 동일** — 테이블·컬럼·인덱스·FK·migration 모두 일치, migration 불필요 |
| repository / service 호환 | ○ 564회 조회 예외 0, production 코드 수정 0 |
| **WRONG = 0** | ○ 3,196건 중 **0** |
| **UNSUPPORTED_BUT_ANSWERED = 0** | ○ 3,196건 중 **0** |
| R1 정상 | ○ 판매종료 `13074225226`에서 SEMI_STATIC 9건 차단 / STATIC 18건 유지 |
| R2 정상 | ○ 실제 패키지·구성품 6개 사례 전부 기대대로, 상속 0건 |
| R4 정상 | ○ brand/manufacturer 분리, 제품라인 근거 요건, 부정 추론 없음 |
| DPS routing 불변 | ○ `answer_service.py` 미변경, plan이 조회보다 선행 |
| Missing Item 불변 | ○ 차단이 Product Facts 조회보다 앞선 단계 |
| Learning safety 불변 | ○ `learning_evidence_policy.py` 미변경 |
| auto-post safety 정상 | ○ NEEDS_REVIEW/CONFLICT는 `excluded_facts`로 분류되어 gate 통과 불가 |

**FINAL_DB_COMPATIBLE**

덧붙여, 이 DB는 답변 품질을 실제로 개선합니다 — 동일한 3,196건 질문에서
답변 가능 건수가 **1,258 → 1,535 (+277)**로 늘고, 오답은 0을 유지합니다.

### 전환 전 반드시 처리할 것

1. `test_real_db_shipped_listings_are_all_currently_collected` 최소 수정 (§29-(1))
2. `test_real_db_absent_field_stays_absent` 최소 수정 (§29-(2))
3. `data/product_facts.db` 백업 및 SHA-256 대조
4. git 추적 정책 결정 — 전환 후 135 MB 바이너리가 추적 대상이 됩니다

§33에 따라 **기존 `data/product_facts.db`를 삭제하지도 교체하지도 않았고**,
`product_facts_final.db`도 그대로 두었습니다. 실제 전환은 사용자 확인 후 수행합니다.
