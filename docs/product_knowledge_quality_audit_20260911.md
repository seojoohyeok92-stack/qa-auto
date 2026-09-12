# Product Knowledge 품질 조사 (2026-09-11) — 조사만, 수정 없음

코드/데이터 수정 없음. 서버 스냅숏 `data/서버pc_data/`(09-11) read-only 사용.
`product_facts.db`, `model_data_with_color.json`은 로컬 `data/`와 SHA-256 동일.
집계는 정규식 추출 + 표본 육안 확인 기반이며 규모 판단용이다. 조사 스크립트는 세션 임시 폴더에 있었으므로 보존되지 않았다.

## 1. 저장 구조

| | `data/product_facts.db` | `data/model_data_with_color.json` |
|---|---|---|
| 키 | Naver product_id (listing) | 모델 키 |
| 규모 | 94 listing | MODEL_CATALOG 1,586, alias 72 |
| 구조 | raw_documents 7,136 → facts 16,846(field 266종) → canonical_facts 7,841(field 216종) + values 8,404 + provenance 25,701 + canonical_fact_listings 7,881 | 한 레코드에 raw `spec` + structured(`vesa, weight, speaker(bool), hz, size_inch, resolution, color, brand`) |
| 없는 필드 | – | Wi-Fi/LAN/BT/HDMI/USB/리모컨. 런타임은 spec 토큰 존재로 판정하고 spec 전체를 값으로 넘김. 리모컨은 미노출 |

조회 순서(`services/product_knowledge_service.py` facts_for_inquiry): product_id가 product_facts에 있으면 그 결과가 최종(전부 제외돼도 JSON으로 안 넘어감), 없을 때만 JSON 매칭.
`docs/product_catalog_runtime_finalization.md`의 "runtime은 product_facts.db를 안 읽는다"는 현재 코드와 다름.

## 2. raw → structured 경로

두 저장소 모두 생성 코드가 이 리포에 없다. product_facts는 외부 수집·canonicalizer 산출물(DOM/IMAGE → facts → canonical 정규화), 이 리포는 `mode=ro`로 읽고 `_exclusion_reason`으로 판정만 한다. JSON은 생성 스크립트 없음(git: 최초 커밋 + `93e59d3`), structured 값은 spec에서 파생되지 않았다.

## 3. 규모

- product_facts: 94 상품, ACTIVE canonical 5,136(VERIFIED 4,284 / NEEDS_REVIEW 716 / CONFLICT 136), 고유 raw 값 5,842
- JSON: 1,586건(spec null 5, "정보 없음" 5)
- 서버 문의 3,200: product_facts 경로 937(68 상품), product_id 있으나 PF 없음 103, product_id 없음(JSON 이름 매칭) 2,160

## 4. raw 명시값 vs structured

### JSON

| field | 일치 | raw 있음·struct 없음 | 불일치 | struct만(raw 근거 없음) | 둘 다 없음 |
|---|---:|---:|---:|---:|---:|
| 화면크기 | 1,524 | 0 | 0 | 16 | 46 |
| 해상도 | 1,479 | 0 | 0 | 23 | 83 |
| 주사율 | 1,477 | 1 (43LM561C0NA) | 0 | 41 | 67 |
| VESA | 1,401 | 1 (S27D400) | 0 | 45 | 139 |
| 무게 | 1,342 | 0 | 63 | 28 | 153 |
| 스피커 | 750 | 0 | 0 | true 25 / false 805 | 6 |
| 색상 | 40 | 13(후보) | 2 | 29 | 1,502 |
| Wi-Fi/LAN/BT/HDMI/USB/리모컨/RF | 필드 없음 | raw 명시 140/73/141/1,522/394/37/22 | – | – | – |

- 무게 불일치 63: struct < spec kg이 거의 전부(예 S43DM701 8.7kg vs 11kg), spec kg 대부분 scope 표기 없음.
- speaker=false 805: spec에 부정 표기 없음. 런타임이 `speaker_present: NO`를 CATALOG_JSON safe fact로 노출(`_is_empty(False)`는 False).

### product_facts (raw fact → canonical lineage, 5,842)

USABLE 4,427 / NEEDS_REVIEW·CONFLICT 996(대부분 IMAGE_OCR) / superseded-only 92 / lineage 없음 7 / raw empty 320.
재실행으로 VERIFIED 값이 사라진 경우 0.

| field | usable 상품 | 검토대기 상품 |
|---|---:|---:|
| vesa_mm | 33 | 17 |
| weight_with_stand_kg | 28 | 22 |
| refresh_rate | 39 | 18 |
| wifi_present | 2 | 17 |
| bluetooth_present | 4 | 30 |
| remote_control_included | 2 | 20 |
| power_cable_included | 38 | 35 |
| display_size_cm | 8 | 15 |

`sections` 페이지 텍스트는 교차판매 상품명·리뷰가 섞여 raw 근거로 쓸 수 없음.

## 5. 대표 사례

- S27D400(JSON): spec `무게(스탠드 포함): 4kg,  100 x 100, HDMI 단자` — 같은 위치 `100 x 100mm` 30건은 채워짐, mm 없는 이 1건만 null. 같은 상품 listing 11844406044는 product_facts에서 vesa_mm 100x100 VERIFIED 정상 제공 → JSON 경로에서만 문제.
- LH43BEAH 등: 상업용 spec에 VESA/Hz 없음, struct엔 200x200/60Hz/speaker=true.
- S27FG900: struct 블랙 vs spec 머큐리실버.
- 12601323000(LS32FG500): 55 facts 전부 라벨 LS32FG500EKXKR → MODEL_SCOPE_MISMATCH → safe 0.
- "온누리20%대상제품 LH43BEF … 107.9cm(43인치)" → alias `삼성 107.9cm(43인치)`로 LH43BEDH EXACT(제목 코드는 LH43BEFH).

## 6. identity 잔여

- product_facts: product_id exact 조회된 행을 제목 추출 코드 vs 라벨로 비교해 제외 — 24/94 listing, 881건, 23 listing safe 0.
  SKU 접미사 13쌍/733행, 선행 L 5쌍/256행, 표기 체계 상이 7쌍/367행(9707378441 S32GF vs S24C310은 실제 충돌).
- JSON(PF 없는 문의 2,263): alias가 제목 모델코드와 모순 21, 복수 후보 중 alias가 선택 96, 모델코드 없는 크기 alias 선택 78. 모델코드 없는 alias 34/72, 대상 없는 alias 3, 5자 미만 무시 alias 10, 5자 미만 키 41, 정규화 충돌 4쌍, 같은 모델 다중 키 20그룹(값 다름 3), 매칭 실패 613(비상품 포함).

## 7. 원인 분류

| # | 원인 | 규모 | 위치 |
|---|---|---|---|
| C1 | 모델 라벨 정규화 부족 + product_id보다 제목 코드 우선 | 881 / 23 listing | 코드 |
| C2 | 모델코드 없는 alias가 명시 코드보다 우선 | 195 문의 | 코드+데이터 |
| C3 | JSON struct 수기 입력·spec 불일치 | VESA 1, Hz 1, 색상 13 / 무게 63, 색상 2 | 데이터 |
| C4 | 부재를 부정으로 저장 | speaker=false 805 | 데이터+코드 |
| C5 | struct 값 raw 근거 없음 | VESA 45, 무게 28, Hz 41, 해상도 23, 크기 16, 색상 29 | 출처 불명 |
| C6 | JSON 스키마 필드 부재 | Wi-Fi/LAN/BT/리모컨(리모컨 37 미노출) | 스키마 |
| C7 | product_facts 검증 대기 | raw 996 | 상품DB PC |
| C8 | raw 자체 부재 | 9번 | 원천 |

## 8. 일반화 수정 후보 (미착수)

1. product_id exact면 제목 코드 비교로 제외하지 않음. SKU 접미사/선행 L 구조적 동일 처리, 표기 상이는 검토 신호.
2. JSON 매칭에서 명시 모델코드 우선, 모델코드 없는 alias는 복수 후보 시 선택 안 함(78건 UNKNOWN화 — 판단 필요).
3. raw 부정 표기 없는 bool false는 UNKNOWN.
4. struct 값이 spec에서 확인될 때만 인용(커버리지 감소 — 판단 필요).
5. 존재 필드를 명시 토큰 기반 YES로 정규화, 리모컨 필드 노출.
6. raw↔structured 불일치 검사 회귀 테스트화.

## 9. 코드로 해결 불가

raw 부재(VESA 139, 무게 153, Hz 67, 해상도 83, 크기 46, 색상 ~1,500, spec 없음 10), product_facts 검증 대기 996, 데이터 정정 후보(S27D400 VESA, S27FG900 색상, 무게 63 scope, 근거 없는 struct 출처), 실제 identity 충돌·alias 부족(9707378441, BE85F↔LH85BEFH, 대상 없는 alias 3), 카탈로그 미등록(LS32FG500 13279397636).
