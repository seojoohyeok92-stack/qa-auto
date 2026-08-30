# Phase 11-H — Product Facts 미활용 Gap 전수 분석

작업일: 2026-08-30 · production/test 코드 수정 없음 · DB read-only · 서버 PC 미접속

---

## 요약

Product Facts evidence를 쓰지 못하는 문의 **194건을 하나도 빠짐없이 분류**했습니다.

| 주원인 | 건수 | 해결 위치 |
|---|---|---|
| **G1 FACT_MISSING** — 그 상품에 사실 자체가 없음 | **77** | 상품DB PC |
| **G3 FACT_NOT_VERIFIED** — 사실은 있으나 미검증/충돌 | **47** | 상품DB PC |
| **G6 ONTOLOGY_GAP** — 의미를 담을 field 자체가 없음 | **31** | 상품DB PC |
| **G7 NOT_PRODUCT_FACT_DOMAIN** — 애초에 답할 영역 아님 | **23** | 해당 없음(정상) |
| **G2 MAPPING_MISSING** — 사실은 쓸 수 있는데 질문이 닿지 못함 | **9** | 개발 PC |
| **G5 QUESTION_AMBIGUOUS** — 질문 자체가 모호 | **6** | 해당 없음(정상) |
| **G4 SAFETY_SCOPE_BLOCK** — 안전 계약의 올바른 차단 | **1** | 해당 없음(정상) |
| G8 PIPELINE_OR_OTHER | 0 | – |
| **합계** | **194** | |

**핵심 결론: 병목은 개발 PC가 아니라 상품DB입니다.**
개발 PC에서 매핑만 고쳐 즉시 해결되는 것은 **9건(4.6%)**이고,
상품DB의 데이터·검증·ontology 보강이 필요한 것이 **155건(79.9%)**입니다.
나머지 **30건(15.5%)**은 Product Facts가 답하지 않는 것이 정상입니다.

---

## Phase 11-G 기준선

| 항목 | 값 |
|---|---|
| Git HEAD / origin/main | `6611378` (동일, clean) |
| DB SHA-256 | `e0cdd3639cb4f0c5f9bc3f2d1f3c4caf020deca45b9144590614e4bc6f55a078` |
| 안전 지표 | leakage·unsupported·DPS bypass·Missing Item·FP·FN·component 오류 **전부 0** |
| 전체 테스트 | 3,570 passed / 0 failed / 0 skipped |

Phase 11-G 정의로 재현한 수치도 **정확히 일치**했습니다 —
전체 2,772 / 상품사실 649 / exact join 209 / evidence 67.

---

## 분석 Corpus

동일 운영 DB를 read-only로 읽어 Phase 11-G와 같은 2,772건을 사용했습니다.
Phase 11-G의 코드 경로(`fields_for_question` → `facts_for_inquiry`)를 그대로 실행해
evidence 67건까지 재현되므로 **두 Phase의 수치는 비교 가능**합니다.

### 다만 모집단 정의를 넓혔습니다 — 그 이유

Phase 11-G는 "상품사실 문의"를 **`fields_for_question()`이 무언가를 돌려주는 문의**로 정의했습니다.
그 정의를 그대로 쓰면 **매핑이 없어서 인식조차 되지 않은 문의**가 모집단에서 빠지므로
G2(MAPPING_MISSING)와 G6(ONTOLOGY_GAP)이 **원리적으로 0**이 됩니다.
즉 "매핑이 없어서 못 쓴다"는 원인을 찾을 수 없는 정의였습니다.

그래서 분석 전용으로 넓은 모집단을 썼습니다 —
상품 사양 어휘가 **질문 어미와 같은 절 안에서** 등장하면 현재 매핑 여부와 무관하게 후보에 넣고,
DPS·누락 영역은 G7으로 분류해 걸러냈습니다.

| 정의 | 상품사실 문의 | exact join | evidence | Gap |
|---|---|---|---|---|
| Phase 11-G (매핑 기준) | 649 | 209 | 67 | 142 |
| **Phase 11-H (의미 기준)** | **789** | **261** | **67** | **194** |

142와 194의 차이 52건은 **현재 매핑으로는 보이지 않던 문의**입니다.
숫자를 늘리려는 것이 아니라, 그 52건이 바로 G2/G6를 찾기 위한 대상입니다.

---

## Gap 모집단 정의

```
상품 사양을 실제로 묻는 문의
  AND  product_id 로 Product Facts listing 과 exact join 성공
  AND  ProductKnowledgeService 가 최종적으로 safe_facts 를 주지 못함
```

배송상태·주문상태·누락·교환·환불처럼 Product Facts가 답하지 않는 것이 정상인 문의는
모집단에서 배제하지 않고 **G7으로 분류**했습니다. 과대계상하지 않으면서도
"왜 답하지 않는가"를 명시적으로 남기기 위해서입니다.

---

## Gap 전체 수

| 단계 | 건수 |
|---|---|
| 전체 문의 | 2,772 |
| 상품사실 성격 | 789 |
| product_id 없음 또는 미수집 상품 | 528 |
| **exact join 성공** | **261** |
| evidence 사용 | 67 |
| **Gap** | **194** |

**검산**: `67 + 194 = 261 = exact join` ✓ · `G1..G8 합계 = 194 = Gap` ✓ · **미분류 0건** ✓

---

## G1 FACT_MISSING — 77건

질문이 요구하는 사실이 **그 상품에 없습니다.** ontology에는 field가 있으나 값이 없는 경우입니다.

| 개념 | 건수 |
|---|---|
| (개념 미상 — 사양을 묻지만 어느 field인지 특정 못 함) | 22 |
| stand (스탠드 종류·조절·재질) | 19 |
| display | 5 |
| speaker | 4 |
| dimensions | 3 |
| youtube / wifi / netflix / warranty / pivot / bluetooth / ott / installation | 각 2 |

`stand` 19건이 눈에 띕니다 — `stand_type`은 전체 94개 상품 중 **2개**만 usable입니다.

---

## G2 MAPPING_MISSING — 9건

**§17의 엄격 기준**(같은 상품에 ACTIVE·VERIFIED·provenance 정상이며 질문에 그대로 답하는 fact가 실재)
을 통과한 것만 남겼습니다.

| id | 질문 성격 | 답할 수 있는 fact |
|---|---|---|
| 1893 | SmartThings 앱 사용 가능? | `smartthings_hub` |
| 2150 | SmartThings로 TV 제어 가능? | `smartthings_hub`, `multi_device_experience` |
| 2151 | 스마트싱스 사용 안 되나? | `smartthings_hub` |
| 2317 | 자동 피벗 기능? | `package_auto_pivot_supported`, `pivot` |
| 2329 | 직접 설치가 어려운가? | `installation_method` |
| 2633 | 혼자서 설치 가능? | `installation_method` |
| 2637 | 혼자서 설치 가능? | `installation_method` |
| 2644 | 혼자 설치 가능? | `installation_method` |
| 2568 | 보증기간이 어떻게 되나? | `pixel_defect_warranty_period_months` (불량화소 한정 — 부분 답변) |

즉 개발 PC에서 추가할 표현은 실질적으로 **두 가지**입니다 —
`스마트싱스/SmartThings`, 그리고 `혼자 설치 / 직접 설치가 어려운가` 계열.

### 자동 분류가 과대계상했고, 육안 감사로 바로잡았습니다

1차 자동 분류는 G2를 **110건**으로 잡았습니다. 표본을 열어보니 대부분 틀렸습니다 —
긴 문의 본문에 단어가 등장했을 뿐 그것을 묻지 않은 경우였습니다.

| 단계 | G2 | 조치 |
|---|---|---|
| 1차 (단어 포함) | 110 | 「설치」 한 단어가 일정·비용·취소 문의까지 흡수 |
| 2차 (개념 세분화 + DPS 우선) | 61 | 설치를 방법/일정/비용으로 분리 |
| 3차 (질문 어미 근접) | 55 | 단어가 질문 절 안에 있을 때만 인정 |
| **4차 (55건 전수 육안 감사)** | **9** | 실제로 그 fact가 답하는지 사람이 확인 |

버려진 46건의 실제 정체: 외부 셋톱박스 호환 17, 기존 벽걸이 재사용 10,
배송·주문·AS 문의 13, 철거 서비스 3, 기타 3.
**자동 규칙만 믿었다면 "개발 PC에서 110건을 해결할 수 있다"는 잘못된 결론에 도달했을 것입니다.**

---

## G3 FACT_NOT_VERIFIED — 47건

사실은 있으나 `NEEDS_REVIEW` / `CONFLICT`라 안전 계약이 정당하게 막습니다.

| field | 건수 |
|---|---|
| `dimensions_without_stand_mm` | 7 |
| `ott_supported` | 6 |
| `set_top_box_ott_supported` | 6 |
| `screen_mirroring` | 6 |
| `dimensions_with_stand_mm` | 6 |
| **`package_contains_set_top_box`** | **5** |
| `remote_control_included` | 4 |
| `smartthings_hub` / `bluetooth_present` / `pixel_defect_warranty_*` | 각 3 |
| `hdmi_present` / `hdmi_port_count` / `tv_plus` | 각 1~2 |

**여기서 Phase 11-G 보고를 정정합니다.**
Phase 11-G는 set-top box 보류 사유로 "포함 여부를 표현할 field가 없다"고 적었습니다.
**그것은 틀렸습니다.** `package_contains_set_top_box` field는 **존재하며 ACTIVE 50건**입니다.
다만 **usable은 0건** — 전부 미검증이거나 충돌 상태입니다.
따라서 set-top box 포함 여부는 **ontology 문제가 아니라 데이터 검증 문제(G3)** 입니다.

---

## G4 SAFETY_SCOPE_BLOCK — 1건

`PRODUCT_LINE_NOT_IN_VALUE` — 그 상품이 실제로 그 제품라인이 아니어서 차단된 정상 동작입니다.
안전 계약이 과도하게 막고 있는 사례는 **없습니다**.

---

## G5 QUESTION_AMBIGUOUS — 6건

`연결되나요?`, `사용 가능한가요?`처럼 대상을 지정하지 않은 표현,
그리고 "거치대 빼고 동일한 모델 맞나요?" 같은 비교 질문입니다.
evidence 하나를 골라 답하면 오히려 위험하므로 UNKNOWN이 정답입니다.

---

## G6 ONTOLOGY_GAP — 31건

**고객이 묻는 의미를 담을 field가 ontology에 없습니다.** 세 덩어리로 나뉩니다.

| 의미 | 건수 | 현재 가장 가까운 field | 왜 그걸로 답하면 위험한가 |
|---|---|---|---|
| **외부 셋톱박스 호환·연결** | **17** | `set_top_box_wifi_present` 등 | 이 field들은 **판매 패키지에 포함된 셋톱박스의 사양**이지 "고객이 이미 쓰는 KT·SK·딜라이브 셋톱박스와 연결되는가"가 아님 |
| **기존 벽걸이·브라켓 재사용** | **10** | `vesa_mm` | VESA 규격이 같아도 무타공 브라켓·기시공 위치·타공 구멍 일치는 별개 문제 |
| 철거·이설 서비스 범위 | 2 | `installation_fee_applies` | "설치비 부과 안 함"이 "기존 TV 철거를 해준다"를 뜻하지 않음 |
| 기타 | 2 | – | – |

필요할 수 있는 semantic(제안일 뿐, 이번 Phase에서 ontology를 수정하지 않았습니다):

| 후보 field | scope | volatility | subject |
|---|---|---|---|
| 외부 셋톱박스 연결 지원 (RF/HDMI 입력 기준) | PRODUCT_SPECIFIC | STATIC | 본체 |
| 안테나 직결(셋톱박스 없이) 시청 가능 | PRODUCT_SPECIFIC | STATIC | 본체 |
| 기존 브라켓 재사용 가능 조건 | LISTING_SPECIFIC | SEMI_STATIC | 설치 서비스 |
| 기존 TV 철거·이설 서비스 포함 여부 | LISTING_SPECIFIC | SEMI_STATIC | 설치 서비스 |

---

## G7 NOT_PRODUCT_FACT_DOMAIN — 23건

배송·설치 일정(DPS), 주문 변경, 폐가전 수거 옵션, 상품권 신청, AS 접수,
도서지역 배송 가능 여부 등입니다. Product Facts가 답하지 않는 것이 **정상**이며
개선 대상이 아닙니다.

---

## G8 PIPELINE_OR_OTHER — 0건

앞의 7개로 설명되지 않는 pipeline 문제는 **없었습니다.**

---

## Topic별 Gap

| topic | 계 | G1 | G2 | G3 | G4 | G5 | G6 | G7 |
|---|---|---|---|---|---|---|---|---|
| (개념 미상) | 30 | 22 | 0 | 6 | 0 | 2 | 0 | 0 |
| stand | 26 | 19 | 0 | 1 | 0 | 2 | 2 | 2 |
| dimensions | 13 | 3 | 0 | 7 | 0 | 0 | 1 | 2 |
| installation_possible | 13 | 2 | 3 | 0 | 0 | 0 | 6 | 2 |
| ott | 11 | 2 | 0 | 5 | 0 | 0 | 4 | 0 |
| set_top_box | 10 | 0 | 0 | 4 | 0 | 0 | 6 | 0 |
| display | 9 | 5 | 0 | 2 | 1 | 0 | 1 | 0 |
| installation_schedule | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 |
| installation_method | 7 | 1 | 1 | 0 | 0 | 1 | 0 | 4 |
| wifi | 7 | 2 | 0 | 5 | 0 | 0 | 0 | 0 |
| netflix | 7 | 2 | 0 | 0 | 0 | 0 | 4 | 1 |
| mobile_connectivity | 6 | 0 | 3 | 3 | 0 | 0 | 0 | 0 |
| warranty | 6 | 2 | 1 | 3 | 0 | 0 | 0 | 0 |
| youtube | 5 | 2 | 0 | 1 | 0 | 0 | 2 | 0 |
| speaker | 5 | 4 | 0 | 0 | 0 | 1 | 0 | 0 |

---

## 상품별 Gap Top 10

| product_id | Gap |
|---|---|
| 12021985151 | 24 |
| 9866761076 | 22 |
| 10914735269 | 19 |
| 9775146473 | 17 |
| 12139453925 | 16 |
| 12143215609 | 12 |
| 9645702227 | 8 |
| 10914781557 | 8 |
| 9645661432 | 7 |
| 13239109816 | 7 |

상위 6개 상품이 Gap의 **약 57%(110건)** 를 차지합니다.
상품DB 보강을 전면 재수집이 아니라 **이 소수 상품에 집중**하면 효율이 높습니다.

---

## Field별 Gap

G3(미검증)에서 검증만 끝내면 열리는 field가 가장 실질적입니다 —
`dimensions_*` 13, `ott_supported`+`set_top_box_ott_supported` 12,
`screen_mirroring` 6, `package_contains_set_top_box` 5, `remote_control_included` 4.

---

## Set-top Box

의미별 전수 분리 결과입니다.

| 의미 | 문의 | 관련 field | 상태 | 답변 가능? |
|---|---|---|---|---|
| A. 포함 여부 | G3 5건 | `package_contains_set_top_box` | ACTIVE 50 / **usable 0** | ✕ 검증 필요 |
| B. 별도구매 여부 | 위와 동일 | 동일 | 동일 | ✕ |
| **C. 외부 셋톱박스 연결/호환** | **G6 17건** | **없음** | – | ✕ **ontology 부재** |
| D. Wi-Fi | 0 | `set_top_box_wifi_present` (32 usable) | 양호 | ○ (묻는 사람이 없음) |
| E. Bluetooth | 0 | `set_top_box_bluetooth_*` (4) | 양호 | ○ |
| F. OTT | G3 6건 | `set_top_box_ott_supported` (16 usable) | 일부 미검증 | △ |
| G. 제조사/브랜드 | 0 | listing brand/manufacturer | component gate 차단 | 의도된 차단 |
| H. 자체 사양 | 0 | `set_top_box_*` | 양호 | ○ |

**실제 고객이 묻는 것은 C(외부 셋톱박스 호환) 17건인데, 정작 DB가 잘 갖춘 것은 D·E·H입니다.**
데이터가 있는 곳과 질문이 오는 곳이 어긋나 있습니다.

---

## 구성품

| 유형 | 문의 | 현재 |
|---|---|---|
| 전체 목록 | 소수 | `*_included` 부분 값만 존재 — 완전성을 표현할 방법이 없어 매핑하지 않음(정상) |
| 특정 구성품 포함 | G3 4건(`remote_control_included`) | 검증 필요 |
| 특정 구성품 누락 | G7 | Missing Item 경로 |
| 특정 구성품 모델/종류 | G1 | 해당 field 없음 |

---

## 리모컨

| 유형 | 현재 |
|---|---|
| 포함 | `remote_control_included` — usable 2 / 미검증 4 → **G3** |
| 종류·모델 | field 없음 → G1/G6 |
| 호환 | field 없음 |
| 누락 | Missing Item(정상 차단) |

---

## OTT / Streaming

| 서비스 | 명시적 evidence | 상태 |
|---|---|---|
| OTT 일반 | `ott_supported` | usable 2, 미검증 6 |
| YouTube | `youtube_supported` | usable 1 |
| Netflix | `ott_supported_services` | **usable 0** |
| Netflix·Disney+ (셋톱박스) | `set_top_box_ott_supported_services` = `["Netflix","Disney+"]` | **usable 4 — 실재** |
| Samsung TV Plus | `tv_plus` | usable 21 |
| Disney+ / Wavve / Tving (본체) | 없음 | – |

`set_top_box_ott_supported_services`에 Netflix·Disney+가 실제로 적혀 있는 상품이 4개 있습니다.
다만 그 값은 **셋톱박스**의 지원 서비스이므로, 본체 질문에 그대로 쓰면 subject 오류가 됩니다.

---

## 설치

| 유형 | 분류 | 건수 |
|---|---|---|
| 설치 방법/주체 | G2 4 / G7 4 | 8 |
| 설치 가능 여부(기존 브라켓 등) | G6 6 / G1 2 / G2 3 / G7 2 | 13 |
| 설치 일정 | **G7 8** | 8 |
| 설치비·철거 | G6 2 | 2 |

설치 일정 8건은 전부 DPS 영역이며 Gap이 아닙니다.

---

## 케이블

| 유형 | field | 상태 |
|---|---|---|
| HDMI 케이블 포함 | `hdmi_cable_included` | usable 1 |
| 전원 케이블 포함 | `power_cable_included` | usable 38 |
| DP 케이블 포함 | **없음** | G6 |
| USB 케이블 포함 | 없음 | – |

`hdmi_present`(포트 존재)와 `hdmi_cable_included`(케이블 동봉)는 별개 field로 유지되고 있습니다.

---

## 무선 연결

| 표현 | 분류 |
|---|---|
| SmartThings / 스마트싱스 | **G2 3건** — `smartthings_hub`(14 usable)가 답할 수 있음 |
| 미러링 | G3 6건 — `screen_mirroring` 미검증 |
| 블루투스 | G1 2 / G3 1 |
| 와이파이 | G1 2 / G3 5 |
| 「무선으로 연결되나요?」 | **G5** — 대상 미지정, UNKNOWN이 정답 |

---

## RAW Coverage

```
67 / 261 = 25.7%
```

Phase 11-G가 보고한 32.1%(67/209)와 다른 이유는 **분모 정의가 넓어졌기** 때문입니다.
같은 분모(209)로 보면 32.1%로 동일하며, 두 수치를 섞어 쓰지 않았습니다.

## ELIGIBLE Coverage

Product Facts가 **원칙적으로 답할 수 있어야 하는** 문의만 분모로 삼습니다.

제외한 것은 두 가지뿐입니다.

- **G7 23건** — 배송·주문·AS·상품권 등 Product Facts 영역이 아님
- **G5 6건** — 질문이 대상을 지정하지 않아 어떤 evidence도 안전하게 고를 수 없음

```
ELIGIBLE = 67 / 232 = 28.9%
```

**fact가 DB에 없다는 이유로는 분모에서 빼지 않았습니다** — 그것이 바로 Product DB coverage 부족이기 때문입니다.
G4(안전 차단 1건)도 남겨 두었습니다. 숫자를 좋게 만들려는 제외는 하지 않았습니다.

---

## IMMEDIATE_DEV_OPPORTUNITY

**9건.** 현재 DB와 현재 안전 계약만으로, 개발 PC에서 매핑만 고치면 즉시 답변 가능해집니다.

| 표현 계열 | 건수 | 근거 fact (usable 상품 수) |
|---|---|---|
| 스마트싱스 / SmartThings | 3 | `smartthings_hub` (14) |
| 혼자·직접 설치 가능 여부 | 4 | `installation_method` (35) |
| 자동 피벗 | 1 | `package_auto_pivot_supported`, `pivot` |
| 보증기간(불량화소 한정) | 1 | `pixel_defect_warranty_period_months` (7) |

이론상 최대 +9건, evidence 67 → 76 (**+13.4%**).

---

## PRODUCT_DB_OPPORTUNITY

**155건.** Q&A 코드는 문제가 없고, Product Facts 데이터·검증·ontology가 보강되면 열립니다.

| 구분 | 건수 | 필요한 작업 |
|---|---|---|
| G3 미검증 | **47** | 기존 수집 데이터의 **검증만** 하면 됨 — 가장 저렴 |
| G6 ontology | **31** | 새 semantic field 설계 + 수집 |
| G1 미수집 | **77** | 해당 상품 사실 수집/추출 |

이론상 최대 +155건, evidence 67 → 222.
다만 G1 77건 중 22건은 "어느 field인지 특정되지 않는" 문의라 실제 달성치는 더 낮습니다.

---

## AMBIGUOUS_OR_INTENTIONAL

**30건** (G7 23 + G5 6 + G4 1).
coverage 실패가 아니라 **의도된 동작**입니다. 개선 대상이 아닙니다.

---

## 예상 ROI

| 방안 | 예상 해결 | 개발 복잡도 | 안전 위험 | 필요 작업 |
|---|---|---|---|---|
| **A. 개발 PC 매핑 추가** | **+9** (최대) | 낮음 | 낮음 (Phase 11-G와 동일 패턴) | 표현 2계열 추가 + 테스트 |
| **B. 상품DB 보강** | **+155** (최대) | 중~높음 | 중간 (ontology 신설은 subject 설계 필요) | 검증 47 → ontology 31 → 수집 77 |
| **C. 현상 유지** | 0 | – | – | – |

B 안에서도 **G3 검증 47건이 압도적으로 저렴**합니다 —
새 데이터를 모으는 것이 아니라 이미 수집된 것을 확정하는 작업이기 때문입니다.
상위 6개 상품이 Gap의 57%를 차지하므로 대상도 좁힐 수 있습니다.

---

## 우선순위 Top 10

| 순위 | topic | 범주 | 문의 | 상품 | 해결 위치 | 예상 증가 | 안전 위험 | 난이도 | 권고 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 외부 셋톱박스 호환 | G6 | 17 | 다수 | 상품DB | +17 | 중 | 중 | **권고** |
| 2 | 치수(with/without stand) 검증 | G3 | 13 | 다수 | 상품DB | +13 | 낮음 | **낮음** | **강력 권고** |
| 3 | OTT 지원 검증 | G3 | 12 | 다수 | 상품DB | +12 | 낮음 | 낮음 | **강력 권고** |
| 4 | 기존 벽걸이·브라켓 재사용 | G6 | 10 | 다수 | 상품DB | +10 | 중 | 중 | 권고 |
| 5 | 스탠드 사실 수집 | G1 | 19 | 소수 | 상품DB | +19 | 낮음 | 중 | 권고 |
| 6 | 미러링 검증 | G3 | 6 | 다수 | 상품DB | +6 | 낮음 | 낮음 | 권고 |
| 7 | 셋톱박스 포함 여부 검증 | G3 | 5 | 다수 | 상품DB | +5 | 낮음 | 낮음 | **강력 권고** |
| 8 | 혼자·직접 설치 표현 | G2 | 4 | 35 | **개발 PC** | +4 | 낮음 | **낮음** | **강력 권고** |
| 9 | SmartThings 표현 | G2 | 3 | 14 | **개발 PC** | +3 | 낮음 | **낮음** | **강력 권고** |
| 10 | 리모컨 포함 검증 | G3 | 4 | 다수 | 상품DB | +4 | 낮음 | 낮음 | 권고 |

---

## 다음 작업 위치

**C. 개발 PC + 상품DB PC 둘 다** — 다만 **비중이 크게 다릅니다.**

- 개발 PC: **9건**, 표현 2계열 추가로 반나절 규모. 즉시 착수 가능
- 상품DB PC: **155건**, 그중 **검증만으로 47건**이 열림. 여기가 본 병목

개발 PC 작업만으로는 coverage가 25.7% → 29.1%에 그칩니다.
반면 상품DB의 **G3 검증 47건만** 처리해도 25.7% → 43.7%가 됩니다.

---

## Product Facts 종료 여부

**OPTION C를 권고합니다 — 상품DB PC에서 Fact/Ontology/검증을 보강한 뒤 개발 PC로 복귀.**

근거:

| 항목 | 수치 |
|---|---|
| 개발 PC에서 해결 가능 | 9 / 194 (**4.6%**) |
| 상품DB에서 해결 가능 | 155 / 194 (**79.9%**) |
| 개선 대상 아님 | 30 / 194 (15.5%) |

개발 PC에서 매핑을 더 파고들어도 얻을 수 있는 최대치가 9건입니다.
Phase 11-G에서 이미 안전한 매핑은 대부분 소진했고, 남은 것은 데이터 쪽입니다.

**다만 개발 PC 9건은 비용이 매우 낮으므로**, 상품DB 작업을 시작하기 전에
짧은 마무리 Phase로 처리하는 것이 합리적입니다(Top 10의 8·9번).

---

## DB 무변경

| 항목 | 값 |
|---|---|
| SHA-256 (작업 전/후) | `e0cdd363…6f55a078` **동일** |
| size / mtime | 142,131,200 / 2026-08-29 23:42:01 **동일** |
| `integrity_check` | ok |
| WAL / SHM / journal | **없음** |

---

## Git 상태

production 코드 **변경 0** · test 코드 **변경 0**.
분석 스크립트는 전부 저장소 밖 TEMP에 두었고, 저장소에는 이 보고서만 추가했습니다.

---

## 최종 판정

# PHASE 11-H READY — Product Facts 미활용 Gap 전수 분석 완료

- Gap **194건 전수 분류, 미분류 0**
- 검산 일치: `G1..G8 합계 = 194 = Gap`, `evidence 67 + Gap 194 = exact join 261`
- 다음 작업 위치를 수치로 결정 가능: 개발 PC 9건(4.6%) vs 상품DB 155건(79.9%)

분석 과정에서 **자체 분류기의 과대계상을 발견해 4단계에 걸쳐 바로잡았고**
(G2 110 → 61 → 55 → 9), **Phase 11-G의 set-top box 보류 사유가 사실과 달랐음을 정정**했습니다
(`package_contains_set_top_box`는 존재하며, 문제는 ontology가 아니라 검증).
